"""verify: one fresh agent per surviving finding, then `verdict_gate`.

- confirmed → stays in the review, and goes on to merge.
- refuted → the appendix, with the verifier's reason.
- unverifiable → stays in the review, severity lowered one step.

A confirmed or unverifiable verdict should carry `expected_behaviour`. One
without it (the verifier left it out, or the verifier failed) still stands,
and the stage lists those findings in `state.errors`, so the summary says
which ones reach the review with no statement of correct behaviour.
"""

from __future__ import annotations

import logging

from .. import checks, sdk
from ..gates import reason_kind, verdict_gate
from ..models import AgentCheck, Finding, GateCheck, ReviewState, Verdict, lower_severity
from ..prompts import verify as prompt
from . import RunContext, bounded, fix_citation

log = logging.getLogger(__name__)


async def run_verify(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("verify"):
        return state
    target, cfg = state.target, ctx.config
    todo = [f for f in state.findings if f.id not in state.verdicts]

    async def _one(finding: Finding) -> tuple[Verdict, AgentCheck, GateCheck | None]:
        started = checks.now()
        given = checks.prompt_labels(target, extra=[f"finding {finding.id}"])
        with sdk.recording() as rec:
            try:
                verdict = await sdk.agent_call_structured(
                    prompt.build(target, finding), Verdict,
                    allowed_tools=sdk.VERIFIER_TOOLS, model=cfg.verifier_model, effort=cfg.effort,
                    max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="verify",
                )
                error = ""
            except sdk.RepoTrustError:
                raise
            except sdk.AgentCallError as e:
                log.error("verifier failed for finding %s", finding.id, exc_info=True)
                error = f"the verifier failed: {str(e).splitlines()[0][:200]}"
        check = AgentCheck(
            stage="verify", subject=finding.id, given=given,
            calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
            turns=rec.turns, cost_usd=round(rec.cost_usd, 6), started_at=started, ended_at=checks.now(),
        )
        if error:
            check.error, check.outcome = error, f"failed: {checks.failure_kind(error)}"
            return Verdict(finding_id=finding.id, status="unverifiable", reason=error), check, None
        verdict = verdict.model_copy(update={
            "finding_id": finding.id,
            "citations": [fix_citation(c, target, ctx.snapshot) for c in verdict.citations],
        })
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
        check.outcome = verdict.status if result.passed else f"{verdict.status} (answered {answered})"
        check.verdict = {
            "status": verdict.status, "reason": verdict.reason,
            "citations": [checks.marked_citation(c, rec.tool_calls, {}, target, ctx.snapshot)
                          for c in verdict.citations],
            "lowered": not result.passed,
        }
        return verdict, check, gate_check

    async def one(finding: Finding) -> Verdict:
        # Stored as each answer arrives: a budget stop mid-stage keeps what
        # was already paid for, and the checkpoint saved then carries it.
        verdict, check, gate_check = await _one(finding)
        state.verdicts[verdict.finding_id] = verdict
        state.checks.append(check)
        if gate_check:
            state.gate_checks.append(gate_check)
        return verdict

    await bounded(todo, one, cfg.concurrency)

    lowered = []
    for i, finding in enumerate(state.findings):
        verdict = state.verdicts.get(finding.id)
        if verdict and verdict.status == "unverifiable" and finding.id not in state.original_severity:
            state.original_severity[finding.id] = finding.severity
            lowered.append(finding.id)
            state.findings[i] = finding.model_copy(update={"severity": lower_severity(finding.severity)})

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
