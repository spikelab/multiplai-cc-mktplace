"""prescribe: one agent per confirmed finding, then `fix_gate`.

A fix that fails the gate is re-asked once with the gate's reason. A second
failure replaces it with "no verified fix" and the reason as an open question.
"""

from __future__ import annotations

import logging

from .. import sdk
from ..gates import fix_gate
from ..models import NO_VERIFIED_FIX, Finding, Fix, ReviewState
from ..prompts import prescribe as prompt
from . import RunContext, bounded, fix_citation

log = logging.getLogger(__name__)


def no_fix(finding_id: str, question: str) -> Fix:
    return Fix(finding_id=finding_id, description=NO_VERIFIED_FIX, open_questions=[question])


async def run_prescribe(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("prescribe"):
        return state
    target, cfg = state.target, ctx.config
    todo = [f for f in state.findings
            if f.id not in state.fixes and getattr(state.verdicts.get(f.id), "status", "") == "confirmed"]
    rejected = 0

    async def one(finding: Finding) -> Fix:
        nonlocal rejected
        reason = ""
        for _attempt in (1, 2):
            try:
                fix = await sdk.agent_call_structured(
                    prompt.build(target, finding, state.verdicts.get(finding.id), reason), Fix,
                    allowed_tools=sdk.PRESCRIBER_TOOLS, model=cfg.prescriber_model, effort=cfg.effort,
                    max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="prescribe",
                )
            except sdk.RepoTrustError:
                raise
            except sdk.AgentCallError as e:
                log.error("prescriber failed for finding %s", finding.id, exc_info=True)
                return no_fix(finding.id, f"the prescriber failed: {str(e).splitlines()[0][:200]}")
            fix = fix.model_copy(update={
                "finding_id": finding.id,
                "premises": [p.model_copy(update={"citation": fix_citation(p.citation, target, ctx.snapshot)})
                             for p in fix.premises],
            })
            result = fix_gate(target, fix)
            if result.passed:
                return fix
            rejected += 1
            ctx.gate_reasons.append(result.reason)
            reason = result.reason
        return no_fix(finding.id, f"the proposed fix failed the citation gates twice: {reason}")

    for fix in await bounded(todo, one, cfg.concurrency):
        state.fixes[fix.finding_id] = fix

    verified = sum(1 for f in state.fixes.values() if f.description != NO_VERIFIED_FIX)
    ctx.counts = {"fixes": verified, "no_verified_fix": len(state.fixes) - verified, "gate_rejects": rejected}
    state.stage = "prescribe"
    return state
