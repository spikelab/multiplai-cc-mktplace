"""verify: one fresh agent per surviving finding, then `verdict_gate`.

- confirmed → stays in the review, and goes on to merge.
- refuted → the appendix, with the verifier's reason.
- unverifiable → stays in the review, severity lowered one step.

An `unverifiable` verdict can name what the verifier could not read, each
with a command a person would run to get it. Those go to `state.needs`
(blocking that finding) through `need_gate`. Such a finding is not lowered
below MEDIUM: it is waiting on a person, and should not sink below the
findings a person can already act on.

A confirmed or unverifiable verdict should carry `expected_behaviour`. One
without it still stands, and the stage lists those findings in
`state.errors`, so the summary says which ones reach the review with no
statement of correct behaviour.

Every answer but `refuted` must carry `impact` and `topic`
(`VerifierAnswer`); one without either fails the parse and is re-asked. A
call that still fails is run again, up to `VERIFY_TRIES` times per finding.
A finding whose every try failed gets no verdict: the stage lets the other
verifiers finish, keeps their verdicts, then raises `VerifyIncomplete`, and
`resume` asks only for the findings left without one. No finding reaches the
review unverified or with no impact or topic. A finding about `tests` never
breaks users or the business: a `breaks-*` impact on one is lowered to
`correctness-only`, with a line in `state.errors`.
"""

from __future__ import annotations

import logging

from .. import checks, sdk
from ..gates import gated_need, reason_kind, verdict_gate
from ..models import (CRITICAL_IMPACTS, SEVERITIES, AgentCheck, Finding, GateCheck, Need, ReviewState, Verdict,
                      VerifierAnswer, lower_severity)
from ..prompts import verify as prompt
from . import RunContext, bounded, fix_citation

log = logging.getLogger(__name__)

# Verify calls per finding before the run stops for `resume`; each call
# already re-asks once when its answer does not parse (`sdk.py`).
VERIFY_TRIES = 3


class VerifyIncomplete(Exception):
    """Some findings have no verdict after VERIFY_TRIES calls each; the rest are kept."""

    def __init__(self, finding_ids: list[str], last_error: str) -> None:
        n = len(finding_ids)
        super().__init__(
            f"the verifier gave no usable answer for {n} finding{'' if n == 1 else 's'} after "
            f"{VERIFY_TRIES} tries each ({', '.join(finding_ids)}); last error: {last_error}")
        self.finding_ids = finding_ids


def unverifiable_severity(severity: str, *, has_need: bool) -> str:
    """One step down; with a need, not below MEDIUM (a LOW stays LOW)."""
    lowered = lower_severity(severity)
    floor = SEVERITIES.index("MEDIUM")
    if has_need and SEVERITIES.index(lowered) > floor:
        return severity if SEVERITIES.index(severity) > floor else "MEDIUM"
    return lowered


async def run_verify(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("verify"):
        return state
    target, cfg = state.target, ctx.config
    todo = [f for f in state.findings if f.id not in state.verdicts]

    failed: dict[str, str] = {}

    async def _one(finding: Finding) -> tuple[Verdict | None, AgentCheck, GateCheck | None]:
        started = checks.now()
        given = checks.prompt_labels(target, extra=[f"finding {finding.id}"])
        answer, error = None, ""
        with sdk.recording() as rec:
            for attempt in range(1, VERIFY_TRIES + 1):
                try:
                    answer = await sdk.agent_call_structured(
                        prompt.build(target, finding), VerifierAnswer,
                        allowed_tools=sdk.VERIFIER_TOOLS, model=cfg.verifier_model, effort=cfg.effort,
                        max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="verify",
                    )
                    break
                except sdk.RepoTrustError:
                    raise
                except sdk.AgentCallError as e:
                    error = f"the verifier failed: {str(e).splitlines()[0][:200]}"
                    log.warning("verifier failed for finding %s (try %d/%d): %s", finding.id, attempt,
                                VERIFY_TRIES, error)
        check = AgentCheck(
            stage="verify", subject=finding.id, given=given,
            calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
            turns=rec.turns, cost_usd=round(rec.cost_usd, 6), started_at=started, ended_at=checks.now(),
        )
        if answer is None:
            log.error("verifier failed %d times for finding %s", VERIFY_TRIES, finding.id)
            check.error = f"{error} ({VERIFY_TRIES} tries)"
            check.outcome = f"failed: {checks.failure_kind(error)}"
            return None, check, None
        verdict = Verdict.model_validate(answer.model_dump()).model_copy(update={
            "finding_id": finding.id,
            "citations": [fix_citation(c, target, ctx.snapshot) for c in answer.citations],
        })
        if verdict.topic == "tests" and verdict.impact in CRITICAL_IMPACTS:
            state.errors.append(f"verify: {finding.id} is about tests; impact {verdict.impact} lowered to "
                                "correctness-only")
            verdict = verdict.model_copy(update={"impact": "correctness-only"})
        answered = verdict.status
        result = verdict_gate(target, verdict)
        gate_check = GateCheck(finding_id=finding.id, gate="verdict_gate", passed=result.passed,
                               rule="" if result.passed else reason_kind(result.reason))
        if not result.passed:
            ctx.gate_reasons.append(result.reason)
            verdict = verdict.model_copy(update={
                "status": "unverifiable",
                "reason": f"{result.reason}. The verifier said: {verdict.reason}",
            })
        # The verifier saw no diff: its prompt holds the finding and its cited lines.
        shown = checks.cited_ranges(finding.citations)
        check.outcome = verdict.status if result.passed else f"{verdict.status} (answered {answered})"
        check.verdict = {
            "status": verdict.status, "reason": verdict.reason,
            "citations": [checks.marked_citation(c, rec.tool_calls, {}, target, ctx.snapshot, shown)
                          for c in verdict.citations],
            "lowered": not result.passed,
        }
        return verdict, check, gate_check

    async def one(finding: Finding) -> Verdict | None:
        # Stored as each answer arrives: a budget stop mid-stage keeps what
        # was already paid for, and the checkpoint saved then carries it.
        verdict, check, gate_check = await _one(finding)
        state.checks.append(check)
        if verdict is None:
            failed[finding.id] = check.error
            return None
        state.verdicts[verdict.finding_id] = verdict
        if gate_check:
            state.gate_checks.append(gate_check)
        if verdict.status == "unverifiable":
            state.needs.extend(gated_need(Need(what=n.what, blocks=finding.id, cause=n.cause,
                                               command=n.command, where=n.where, source="verifier"))
                               for n in verdict.needs if n.what.strip())
        return verdict

    await bounded(todo, one, cfg.concurrency)
    if failed:
        raise VerifyIncomplete(sorted(failed), next(iter(failed.values())))

    lowered = []
    waiting = {n.blocks for n in state.needs}
    for i, finding in enumerate(state.findings):
        verdict = state.verdicts.get(finding.id)
        if verdict and verdict.status == "unverifiable" and finding.id not in state.original_severity:
            state.original_severity[finding.id] = finding.severity
            lowered.append(finding.id)
            severity = unverifiable_severity(finding.severity, has_need=finding.id in waiting)
            state.findings[i] = finding.model_copy(update={"severity": severity})

    statuses = [state.verdicts[f.id].status for f in state.findings if f.id in state.verdicts]
    ctx.counts = {s: statuses.count(s) for s in ("confirmed", "refuted", "unverifiable")}
    missing = [f.id for f in state.findings
               if (v := state.verdicts.get(f.id)) and v.status != "refuted" and not v.expected_behaviour.strip()]
    if missing:
        ctx.counts["no_expected_behaviour"] = len(missing)
        state.errors.append(f"verify: no expected behaviour for {len(missing)} finding"
                            f"{'' if len(missing) == 1 else 's'} ({', '.join(missing)})")
    state.stage = "verify"
    return state
