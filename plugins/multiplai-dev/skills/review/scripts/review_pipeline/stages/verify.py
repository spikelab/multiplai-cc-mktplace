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
without it (the verifier left it out, or the verifier failed) still stands,
and the stage lists those findings in `state.errors`, so the summary says
which ones reach the review with no statement of correct behaviour.
"""

from __future__ import annotations

import logging

from .. import sdk
from ..gates import gated_need, verdict_gate
from ..models import SEVERITIES, Finding, Need, ReviewState, Verdict, lower_severity
from ..prompts import verify as prompt
from . import RunContext, bounded, fix_citation

log = logging.getLogger(__name__)


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

    async def _one(finding: Finding) -> Verdict:
        try:
            verdict = await sdk.agent_call_structured(
                prompt.build(target, finding), Verdict,
                allowed_tools=sdk.VERIFIER_TOOLS, model=cfg.verifier_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="verify",
            )
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("verifier failed for finding %s", finding.id, exc_info=True)
            return Verdict(finding_id=finding.id, status="unverifiable",
                           reason=f"the verifier failed: {str(e).splitlines()[0][:200]}")
        verdict = verdict.model_copy(update={
            "finding_id": finding.id,
            "citations": [fix_citation(c, target, ctx.snapshot) for c in verdict.citations],
        })
        result = verdict_gate(target, verdict)
        if not result.passed:
            ctx.gate_reasons.append(result.reason)
            verdict = verdict.model_copy(update={
                "status": "unverifiable",
                "reason": f"{result.reason}. The verifier said: {verdict.reason}",
            })
        return verdict

    async def one(finding: Finding) -> Verdict:
        # Stored as each answer arrives: a budget stop mid-stage keeps what
        # was already paid for, and the checkpoint saved then carries it.
        verdict = await _one(finding)
        state.verdicts[verdict.finding_id] = verdict
        if verdict.status == "unverifiable":
            state.needs.extend(gated_need(Need(what=n.what, blocks=finding.id, cause=n.cause,
                                               command=n.command, source="verifier"))
                               for n in verdict.needs if n.what.strip())
        return verdict

    await bounded(todo, one, cfg.concurrency)

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
