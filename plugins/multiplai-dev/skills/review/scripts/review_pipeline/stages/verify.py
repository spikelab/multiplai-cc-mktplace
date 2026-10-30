"""verify: one fresh agent per surviving HIGH or MEDIUM finding, then `verdict_gate`.

- confirmed → stays in the review, and goes on to merge.
- refuted → the appendix, with the verifier's reason.
- unverifiable → stays in the review, severity lowered one step.
- unverified → a finding the finder rated LOW: no verifier is asked unless
  `verify_low` is on (`review.yaml`, `--verify-low`). The stage writes its
  verdict in code, with an empty reason, no impact, and the topic `tests`
  for a finding of the `tests` dimension (none otherwise). It stays in the
  review and goes on to merge, like a confirmed one, and has no `AgentCheck`.
  A trim finding is verified at any severity: its `important` label below
  needs the verifier's impact.

Every answer but `refuted` also gives a `severity`, rated from the consequence
the verifier checked. After all verdicts, one pass sets each confirmed or
unverifiable finding's severity with `verified_severity`: the verifier's
severity (the finder's when the verdict has none, as in a checkpoint saved
before verifiers gave one), then `hygiene` is LOW, an impact in
`CRITICAL_IMPACTS` is at least MEDIUM, and an unverifiable verdict is lowered
by `unverifiable_severity` as before. No other cap; no status changes. The
finder's severity goes to `state.original_severity` first, for every
unverifiable finding and for a confirmed one whose severity changes, and the
pass skips a finding already there, so a resume never applies it twice.

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
`resume` asks only for the findings left without one. A hit usage limit is
never tried again: it stops the stage at once (`sdk.UsageLimitError`), with
the verdicts already returned kept. No HIGH or MEDIUM finding reaches the
review unverified or with no impact or topic; with `verify_low` on, no finding does. A finding about `tests` never
breaks users or the business: an impact in `CRITICAL_IMPACTS` on one is
lowered to `correctness-only`, with a line in `state.errors`.

A trim finding (`models.is_trim`) is a suggestion to remove, merge or shorten
a test or comment: its impact rates what removing the test risks, so it is
never lowered, the verifier's severity and the impact rules above are not
applied to it, and its severity is set to LOW once the stage ends.
`important_assessments` then labels `important` each shown (confirmed or
unverifiable) trim finding whose test must stay (impact `breaks-*`, or a citation under a `## Critical
paths` glob), which takes it out of the list. Both are recomputed from the
verdicts whenever the stage ends, never stored on their own, so a resume
cannot apply them twice.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase

from .. import checks, sdk
from ..gates import gated_need, reason_kind, verdict_gate
from ..models import (
    CRITICAL_IMPACTS,
    SEVERITIES,
    AgentCheck,
    Assessment,
    Finding,
    GateCheck,
    Need,
    ReviewState,
    Verdict,
    VerifierAnswer,
    is_trim,
    lower_severity,
)
from ..prompts import verify as prompt
from . import RunContext, bounded, fix_citation, save
from .find import critical_paths

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
            f"{VERIFY_TRIES} tries each ({', '.join(finding_ids)}); last error: {last_error}"
        )
        self.finding_ids = finding_ids


def important_assessments(state: ReviewState) -> dict[str, Assessment]:
    """`important` for each shown (confirmed or unverifiable) trim finding that suggests removing a test
    that must stay: the test's behaviour is rated `breaks-users`/`breaks-business`, or it covers a path
    under a `## Critical paths` glob. An unverifiable one counts too: it would otherwise be listed.

    A finding the merge stage folded into one counts as part of it, since the survivor keeps only its own
    verdict. The verifier labels a test `tests` and a comment `docs`; under any other topic the finding
    may be a comment, which cites only its own file, so only a citation of another file (the code a test
    exercises) is matched against the globs.
    """
    into = {m.finding.id: m.into for m in state.merged}

    def survivor(fid: str) -> str:
        seen: set[str] = set()
        while fid in into and fid not in seen:
            seen.add(fid)
            fid = into[fid]
        return fid

    candidates = [
        f
        for f in state.findings
        if is_trim(f)
        and getattr(state.verdicts.get(f.id), "status", "")
        in ("confirmed", "unverifiable", "unverified")
    ]
    members = {f.id: [f] for f in candidates}
    for m in state.merged:
        members.get(survivor(m.finding.id), []).append(m.finding)
    judged = [
        (f.id, m, v)
        for f in candidates
        for m in members[f.id]
        if (v := state.verdicts.get(m.id)) and v.topic != "docs"
    ]
    cited = {
        (fid, m.id): [
            c
            for c in m.citations
            if not c.is_web and (v.topic == "tests" or c.path != m.file)
        ]
        for fid, m, v in judged
    }
    paths = sorted({c.path for cs in cited.values() for c in cs})
    globs = critical_paths(state.target, paths) if paths else []
    out: dict[str, Assessment] = {}
    for fid, m, v in judged:
        if fid in out:
            continue
        hit = next(
            (
                (c.path, glob, where)
                for c in cited[(fid, m.id)]
                for glob, where in globs
                if fnmatchcase(c.path, glob)
            ),
            None,
        )
        if v.impact in CRITICAL_IMPACTS:
            reason = f"the behaviour this test checks is rated {v.impact}; it is never suggested for removal"
        elif hit:
            reason = f"{hit[0]} is a critical path (`{hit[1]}` in {hit[2]}); its tests always count as important"
        else:
            continue
        if m.id != fid:
            reason = f"{m.id}, merged into this suggestion: {reason}"
        out[fid] = Assessment(label="important", reason=reason)
    return out


def unverifiable_severity(severity: str, *, has_need: bool) -> str:
    """One step down; with a need, not below MEDIUM (a LOW stays LOW)."""
    lowered = lower_severity(severity)
    floor = SEVERITIES.index("MEDIUM")
    if has_need and SEVERITIES.index(lowered) > floor:
        return severity if SEVERITIES.index(severity) > floor else "MEDIUM"
    return lowered


def verified_severity(finder: str, verdict: Verdict, *, has_need: bool) -> str:
    """The severity of a confirmed or unverifiable finding, from its verdict.

    The verifier's severity, or *finder* when the verdict has none. Then `hygiene` is LOW, and an
    impact in `CRITICAL_IMPACTS` is at least MEDIUM. An unverifiable verdict is then lowered by
    `unverifiable_severity`.
    """
    severity = verdict.severity or finder
    floor = SEVERITIES.index("MEDIUM")
    if verdict.impact == "hygiene":
        severity = "LOW"
    elif verdict.impact in CRITICAL_IMPACTS and SEVERITIES.index(severity) > floor:
        severity = "MEDIUM"
    if verdict.status == "unverifiable":
        severity = unverifiable_severity(severity, has_need=has_need)
    return severity


def settle_severity(
    state: ReviewState, index: int, finder: str, waiting: set[str]
) -> None:
    """Set `state.findings[index]`'s severity from its verdict, starting from the finder's *finder*.

    Only a confirmed or unverifiable verdict changes anything. *finder* is recorded in
    `state.original_severity` for an unverifiable finding and for one whose severity changes, and
    removed otherwise. A trim finding keeps today's rule: only an unverifiable verdict lowers it.
    """
    finding = state.findings[index]
    verdict = state.verdicts[finding.id]
    if verdict.status not in ("confirmed", "unverifiable"):
        return
    has_need = finding.id in waiting
    if not is_trim(finding):
        severity = verified_severity(finder, verdict, has_need=has_need)
    elif verdict.status == "unverifiable":
        severity = unverifiable_severity(finder, has_need=has_need)
    else:
        severity = finder
    if severity != finder or verdict.status == "unverifiable":
        state.original_severity[finding.id] = finder
    else:
        state.original_severity.pop(finding.id, None)
    state.findings[index] = finding.model_copy(update={"severity": severity})


def unverified_verdict(finding: Finding) -> Verdict:
    """The verdict of a LOW finding no verifier is asked about, made in code."""
    return Verdict(
        finding_id=finding.id,
        status="unverified",
        reason="",
        topic="tests" if finding.dimension == "tests" else "",
    )


async def verify_one(
    state: ReviewState, ctx: RunContext, finding: Finding, *, evidence: str = ""
) -> tuple[Verdict | None, AgentCheck, GateCheck | None]:
    """One verifier call for *finding*, tried up to `VERIFY_TRIES` times, then `verdict_gate`.

    *evidence* is extra text the verifier is given as data (review's `answers`: the outputs of the
    Needs you commands, each fenced). Returns no verdict when every try failed.
    """
    target, cfg = state.target, ctx.config
    started = checks.now()
    given = checks.prompt_labels(target, extra=[f"finding {finding.id}"])
    answer, error = None, ""
    with sdk.recording() as rec:
        for attempt in range(1, VERIFY_TRIES + 1):
            try:
                answer = await sdk.agent_call_structured(
                    prompt.build(target, finding, evidence=evidence),
                    VerifierAnswer,
                    allowed_tools=sdk.VERIFIER_TOOLS,
                    model=cfg.verifier_model,
                    effort=cfg.effort,
                    max_turns=cfg.max_turns,
                    cwd=str(ctx.snapshot),
                    budget_label="verify",
                )
                break
            except (sdk.RepoTrustError, sdk.UsageLimitError):
                raise
            except sdk.AgentCallError as e:
                error = f"the verifier failed: {str(e).splitlines()[0][:200]}"
                log.warning(
                    "verifier failed for finding %s (try %d/%d): %s",
                    finding.id,
                    attempt,
                    VERIFY_TRIES,
                    error,
                )
    check = AgentCheck(
        stage="verify",
        subject=finding.id,
        given=given,
        calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
        turns=rec.turns,
        cost_usd=round(rec.cost_usd, 6),
        started_at=started,
        ended_at=checks.now(),
    )
    if answer is None:
        log.error("verifier failed %d times for finding %s", VERIFY_TRIES, finding.id)
        check.error = f"{error} ({VERIFY_TRIES} tries)"
        check.outcome = f"failed: {checks.failure_kind(error)}"
        return None, check, None
    verdict = Verdict.model_validate(answer.model_dump()).model_copy(
        update={
            "finding_id": finding.id,
            "citations": [
                fix_citation(c, target, ctx.snapshot) for c in answer.citations
            ],
        }
    )
    if (
        verdict.topic == "tests"
        and verdict.impact in CRITICAL_IMPACTS
        and not is_trim(finding)
    ):
        state.errors.append(
            f"verify: {finding.id} is about tests; impact {verdict.impact} lowered to "
            "correctness-only"
        )
        verdict = verdict.model_copy(update={"impact": "correctness-only"})
    answered = verdict.status
    result = verdict_gate(target, verdict)
    gate_check = GateCheck(
        finding_id=finding.id,
        gate="verdict_gate",
        passed=result.passed,
        rule="" if result.passed else reason_kind(result.reason),
    )
    if not result.passed:
        ctx.gate_reasons.append(result.reason)
        verdict = verdict.model_copy(
            update={
                "status": "unverifiable",
                "reason": f"{result.reason}. The verifier said: {verdict.reason}",
            }
        )
    # The verifier saw no diff: its prompt holds the finding and its cited lines.
    shown = checks.cited_ranges(finding.citations)
    check.outcome = (
        verdict.status if result.passed else f"{verdict.status} (answered {answered})"
    )
    check.verdict = {
        "status": verdict.status,
        "reason": verdict.reason,
        "citations": [
            checks.marked_citation(c, rec.tool_calls, {}, target, ctx.snapshot, shown)
            for c in verdict.citations
        ],
        "lowered": not result.passed,
    }
    return verdict, check, gate_check


def keep_verdict(
    state: ReviewState,
    finding: Finding,
    verdict: Verdict,
    check: AgentCheck,
    gate_check: GateCheck | None,
) -> None:
    """Store *verdict* and its records; an unverifiable one's needs go to `state.needs` through `need_gate`."""
    state.checks.append(check)
    state.verdicts[verdict.finding_id] = verdict
    if gate_check:
        state.gate_checks.append(gate_check)
    if verdict.status == "unverifiable":
        state.needs.extend(
            gated_need(
                Need(
                    what=n.what,
                    blocks=finding.id,
                    cause=n.cause,
                    command=n.command,
                    where=n.where,
                    source="verifier",
                )
            )
            for n in verdict.needs
            if n.what.strip()
        )


async def run_verify(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("verify"):
        return state
    cfg = ctx.config
    if not cfg.verify_low:
        unasked = [
            f
            for f in state.findings
            if f.severity == "LOW" and not is_trim(f) and f.id not in state.verdicts
        ]
        for f in unasked:
            state.verdicts[f.id] = unverified_verdict(f)
        if unasked:
            save(state, ctx)
    todo = [f for f in state.findings if f.id not in state.verdicts]

    failed: dict[str, str] = {}

    async def one(finding: Finding) -> Verdict | None:
        # Stored and saved as each answer arrives, so a stop mid-stage, a
        # budget stop or a kill, keeps what was already paid for.
        verdict, check, gate_check = await verify_one(state, ctx, finding)
        if verdict is None:
            state.checks.append(check)
            failed[finding.id] = check.error
            save(state, ctx)
            return None
        keep_verdict(state, finding, verdict, check, gate_check)
        save(state, ctx)
        return verdict

    await bounded(todo, one, cfg.concurrency)
    if failed:
        raise VerifyIncomplete(sorted(failed), next(iter(failed.values())))

    waiting = {n.blocks for n in state.needs}
    for i, finding in enumerate(state.findings):
        if finding.id in state.verdicts and finding.id not in state.original_severity:
            settle_severity(state, i, finding.severity, waiting)
    for i, finding in enumerate(state.findings):
        if is_trim(finding) and finding.severity != "LOW":
            state.findings[i] = finding.model_copy(update={"severity": "LOW"})
    state.assessments.update(important_assessments(state))

    statuses = [
        state.verdicts[f.id].status for f in state.findings if f.id in state.verdicts
    ]
    ctx.counts = {
        s: statuses.count(s)
        for s in ("confirmed", "refuted", "unverifiable", "unverified")
    }
    missing = [
        f.id
        for f in state.findings
        if (v := state.verdicts.get(f.id))
        and v.status in ("confirmed", "unverifiable")
        and not v.expected_behaviour.strip()
    ]
    if missing:
        ctx.counts["no_expected_behaviour"] = len(missing)
        state.errors.append(
            f"verify: no expected behaviour for {len(missing)} finding"
            f"{'' if len(missing) == 1 else 's'} ({', '.join(missing)})"
        )
    state.stage = "verify"
    return state
