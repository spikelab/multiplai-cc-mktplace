"""recheck: one fresh agent per finding an earlier round left open, then `earlier_status_gate`.

Runs after verify and before merge. The open set is `rounds.open_findings`:
what the latest kept round with a `findings.json` listed, plus its own
`earlier` entries that are not `resolved`, less what the person rejected.
Each agent gets one earlier finding and `git diff <its round's head>..<head>`
of the paths it cites, and answers `resolved`, `still_present`,
`partially_resolved` or `cannot_verify` with citations at head. It never sees
this round's findings, and the finders never see earlier rounds.

`earlier_status_gate` turns any status but `cannot_verify` that has no
repository citation reproducing at head into `cannot_verify`, with the rule
in the status's reason and a `GateCheck`. A finding whose `VERIFY_TRIES`
calls all failed gets `cannot_verify` with `error` set and a line in
`state.errors`; the run goes on, since the recheck is about earlier rounds
and this round's findings still need to reach the review. A hit usage limit,
a trust stop and a budget stop leave the stage, with the answers already
returned kept.

Answers are stored in `state.recheck_answers` by earlier id as they arrive;
the first one per id stands, so a resume asks only for the ids without one.
The stage is skipped on a tree review and when the open set is empty. The
assess stage makes the statuses final (`stages/assess.reconcile_earlier`).
"""

from __future__ import annotations

import logging

from .. import checks, incremental, rounds, sdk
from ..gates import earlier_status_gate, reason_kind
from ..models import AgentCheck, EarlierStatus, GateCheck, RecheckOutput, ReviewState
from ..prompts import recheck as prompt
from ..target import _DIFF_FLAGS, git
from . import RunContext, bounded, fix_citation, save
from .verify import VERIFY_TRIES

log = logging.getLogger(__name__)


def open_set(state: ReviewState, ctx: RunContext) -> list[rounds.OpenFinding]:
    """The earlier findings this round gives a status: none on a tree review or without a target directory."""
    t = state.target
    if ctx.target_dir is None or t.is_tree:
        return []
    elsewhere = rounds.history_elsewhere(ctx.target_dir, t.slug, t.remote_url)
    found = rounds.open_findings(ctx.target_dir, t.head_sha, elsewhere)
    # An incremental round carried these into its own findings (`incremental.py`): not earlier ones.
    carried = set(state.scope.carried_ids) if state.scope else set()
    return [o for o in found if o.id not in carried]


def left_the_change(state: ReviewState, o: rounds.OpenFinding) -> bool:
    """In an incremental round, an earlier finding whose file is no longer in the change."""
    file = o.finding.get("file")
    return (
        incremental.is_incremental(state)
        and bool(file)
        and file not in state.target.files
    )


def changed_since(state: ReviewState, o: rounds.OpenFinding) -> str:
    """`git diff <round head>..<head> -- <cited paths>` in the reviewed repository; a note when git cannot."""
    paths = prompt.cited_paths(o)
    if not paths:
        return ""
    if not o.head_sha:
        return "(the earlier round's head is not known, so no diff is given)"
    proc = git(
        state.target.repo_path,
        "diff",
        *_DIFF_FLAGS,
        f"{o.head_sha}..{state.target.head_sha}",
        "--",
        *paths,
        check=False,
    )
    if proc.returncode != 0:
        return f"(git could not diff {o.head_sha[:12]}..{state.target.head_sha[:12]}: {proc.stderr.strip()[:200]})"
    return proc.stdout


async def run_recheck(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("recheck"):
        return state
    target, cfg = state.target, ctx.config
    found = open_set(state, ctx)
    todo = [o for o in found if o.id not in state.recheck_answers]

    async def _one(
        o: rounds.OpenFinding,
    ) -> tuple[EarlierStatus, AgentCheck | None, GateCheck | None]:
        base = {"earlier_id": o.id, "lineage": o.lineage, "round": o.head_sha}
        if left_the_change(state, o):
            return (
                EarlierStatus(
                    **base, status="resolved", reason=incremental.LEFT_THE_CHANGE
                ),
                None,
                None,
            )
        if not o.finding.get("claim"):
            return (
                EarlierStatus(
                    **base,
                    status="cannot_verify",
                    reason="no kept round holds this finding any more",
                ),
                None,
                None,
            )
        started = checks.now()
        answer, error = None, ""
        with sdk.recording() as rec:
            for attempt in range(1, VERIFY_TRIES + 1):
                try:
                    answer = await sdk.agent_call_structured(
                        prompt.build(target, o, changed_since(state, o)),
                        RecheckOutput,
                        allowed_tools=sdk.VERIFIER_TOOLS,
                        model=cfg.verifier_model,
                        effort=cfg.effort,
                        max_turns=cfg.max_turns,
                        cwd=str(ctx.snapshot),
                        budget_label="recheck",
                    )
                    break
                except (sdk.RepoTrustError, sdk.UsageLimitError):
                    raise
                except sdk.AgentCallError as e:
                    error = f"the recheck failed: {str(e).splitlines()[0][:200]}"
                    log.warning(
                        "recheck failed for earlier finding %s (try %d/%d): %s",
                        o.id,
                        attempt,
                        VERIFY_TRIES,
                        error,
                    )
        check = AgentCheck(
            stage="recheck",
            subject=o.id,
            given=[f"earlier finding {o.id}", f"diff since {o.head_sha[:12]}"],
            calls=[
                checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls
            ],
            turns=rec.turns,
            cost_usd=round(rec.cost_usd, 6),
            started_at=started,
            ended_at=checks.now(),
        )
        if answer is None:
            log.error(
                "recheck failed %d times for earlier finding %s", VERIFY_TRIES, o.id
            )
            check.error = f"{error} ({VERIFY_TRIES} tries)"
            check.outcome = f"failed: {checks.failure_kind(error)}"
            return (
                EarlierStatus(
                    **base,
                    status="cannot_verify",
                    reason="the recheck agent gave no usable answer",
                    error=check.error,
                ),
                check,
                None,
            )
        status = EarlierStatus(
            **base,
            status=answer.status,
            reason=answer.reason.strip(),
            citations=[fix_citation(c, target, ctx.snapshot) for c in answer.citations],
        )
        result = earlier_status_gate(target, status)
        gate_check = GateCheck(
            finding_id=o.id,
            gate="earlier_status_gate",
            passed=result.passed,
            rule="" if result.passed else reason_kind(result.reason),
        )
        check.outcome = status.status
        if not result.passed:
            ctx.gate_reasons.append(result.reason)
            check.outcome = f"cannot_verify (answered {status.status})"
            status = status.model_copy(
                update={
                    "status": "cannot_verify",
                    "reason": f"{result.reason}. The recheck said: {status.reason}",
                }
            )
        return status, check, gate_check

    async def one(o: rounds.OpenFinding) -> None:
        # Stored and saved as each answer arrives: a stop mid-stage, a kill included, keeps what was
        # already paid for.
        status, check, gate_check = await _one(o)
        if check is not None:
            state.checks.append(check)
        if gate_check is not None:
            state.gate_checks.append(gate_check)
        if status.error:
            state.errors.append(
                f"recheck: {o.id}: {status.error}; status cannot_verify"
            )
        state.recheck_answers.setdefault(
            o.id, status
        )  # the first stored answer per id stands
        save(state, ctx)

    await bounded(todo, one, cfg.concurrency)
    answers = [
        state.recheck_answers[o.id].status
        for o in found
        if o.id in state.recheck_answers
    ]
    ctx.counts = {
        "open earlier": len(found),
        "asked": len(todo),
        **{
            s: answers.count(s)
            for s in (
                "resolved",
                "still_present",
                "partially_resolved",
                "cannot_verify",
            )
            if answers.count(s)
        },
    }
    state.stage = "recheck"
    return state
