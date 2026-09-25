"""verify: one fresh agent per surviving finding, then `verdict_gate`.

- confirmed → goes on to prescribe.
- refuted → the appendix, with the verifier's reason.
- unverifiable → stays in the review, severity lowered one step.
"""

from __future__ import annotations

import logging

from .. import sdk
from ..gates import verdict_gate
from ..models import Finding, ReviewState, Verdict, lower_severity
from ..prompts import verify as prompt
from . import RunContext, bounded, fix_citation

log = logging.getLogger(__name__)


async def run_verify(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("verify"):
        return state
    target, cfg = state.target, ctx.config
    todo = [f for f in state.findings if f.id not in state.verdicts]

    async def one(finding: Finding) -> Verdict:
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

    for verdict in await bounded(todo, one, cfg.concurrency):
        state.verdicts[verdict.finding_id] = verdict

    lowered = []
    for i, finding in enumerate(state.findings):
        verdict = state.verdicts.get(finding.id)
        if verdict and verdict.status == "unverifiable" and finding.id not in state.original_severity:
            state.original_severity[finding.id] = finding.severity
            lowered.append(finding.id)
            state.findings[i] = finding.model_copy(update={"severity": lower_severity(finding.severity)})

    statuses = [state.verdicts[f.id].status for f in state.findings if f.id in state.verdicts]
    ctx.counts = {s: statuses.count(s) for s in ("confirmed", "refuted", "unverifiable")}
    state.stage = "verify"
    return state
