"""check_fix: one fresh agent per fix that passed `fix_gate`.

It is asked one question: does anything that consumes the cited symbols, or
calls the changed lines, break if the fix is applied as described? A refuted
fix is dropped to "no verified fix" with the checker's reason as the question.
"""

from __future__ import annotations

import logging

from .. import sdk
from ..gates import premise_symbols, symbol_hits
from ..models import NO_VERIFIED_FIX, Finding, FixCheck, ReviewState
from ..prompts import check_fix as prompt
from . import RunContext, bounded
from .prescribe import no_fix

log = logging.getLogger(__name__)

MAX_USES_PER_SYMBOL = 20


async def run_check_fix(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("check_fix"):
        return state
    target, cfg = state.target, ctx.config
    by_id = {f.id: f for f in state.findings}
    todo = [by_id[fid] for fid, fix in state.fixes.items()
            if fix.description != NO_VERIFIED_FIX and fid not in state.fix_checks and fid in by_id]

    async def one(finding: Finding) -> FixCheck:
        fix = state.fixes[finding.id]
        consumers: dict[str, list[str]] = {}
        for premise in fix.premises:
            if premise.kind != "in_repo":
                continue
            for symbol in premise_symbols(target, premise):
                _, uses = symbol_hits(target, symbol)
                consumers[symbol] = [f"{p}:{n}" for p, n in uses[:MAX_USES_PER_SYMBOL]]
        try:
            check = await sdk.agent_call_structured(
                prompt.build(target, finding, fix, consumers), FixCheck,
                allowed_tools=sdk.CHECKER_TOOLS, model=cfg.checker_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="check_fix",
            )
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("fix checker failed for finding %s", finding.id, exc_info=True)
            # An unchecked fix does not pass on the prescriber's say-so.
            return FixCheck(finding_id=finding.id, status="refuted",
                            reason=f"the fix could not be checked: {str(e).splitlines()[0][:200]}")
        return check.model_copy(update={"finding_id": finding.id})

    for check in await bounded(todo, one, cfg.concurrency):
        state.fix_checks[check.finding_id] = check
        if check.status == "refuted":
            old = state.fixes[check.finding_id]
            state.fixes[check.finding_id] = no_fix(
                check.finding_id,
                f"A proposed fix ({old.description[:300]}) was refuted by a check: {check.reason}",
            )

    statuses = [c.status for c in state.fix_checks.values()]
    ctx.counts = {"confirmed": statuses.count("confirmed"), "refuted": statuses.count("refuted")}
    state.stage = "check_fix"
    return state
