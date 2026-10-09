"""repeats: which shown findings repeat a finding the person rejected in an earlier round.

Runs after merge, on this round's shown (confirmed and unverifiable) findings:

1. The rejected list: every finding in `rounds/*/findings.json` whose id has
   `decision: "reject"` in `viewer/decisions.json`. Empty: the stage is skipped.
2. A shown finding with the same id as a rejected one is a repeat, matched in
   Python with no model call.
3. The rest go to one agent whose only question is which describe the same
   defect as a rejected one. `gate_matches` keeps a match only when its id is
   a shown finding, its `rejected_id` is on the list, the finding has no other
   match, and the reason is not empty. Anything else is dropped with a line in
   `state.errors`.

Matches are stored in `state.repeats` by finding id; `state.repeats_checked`
lists the ids already asked about, so a resume does not ask again. A failed
call leaves every finding unmatched. Nothing is removed: the assess stage
labels each match `repeat`, and the person still decides.

The rejected list is not given to the finders. Telling them not to report it
would hide a repeat without a trace; checking at the end shows each one.
"""

from __future__ import annotations

import logging

from .. import rounds, sdk
from ..models import Finding, Repeat, RepeatMatch, RepeatsOutput, ReviewState
from ..prompts import repeats as prompt
from . import RunContext

log = logging.getLogger(__name__)

SHOWN_STATUSES = ("confirmed", "unverifiable")


def shown_findings(state: ReviewState) -> list[Finding]:
    return [f for f in state.findings if getattr(state.verdicts.get(f.id), "status", "") in SHOWN_STATUSES]


def earlier_findings(state: ReviewState, ctx: RunContext) -> list[rounds.EarlierFinding]:
    if ctx.target_dir is None:
        return []
    return rounds.load_rounds(ctx.target_dir, state.target.head_sha)


def gate_matches(matches: list[RepeatMatch], shown_ids: set[str],
                 rejected_ids: set[str]) -> tuple[list[RepeatMatch], list[str]]:
    """(the matches kept, one line per match dropped)."""
    kept, dropped, used = [], [], set()
    for m in matches:
        if m.id not in shown_ids:
            dropped.append(f"{m.id} is not a finding of this round")
        elif m.rejected_id not in rejected_ids:
            dropped.append(f"{m.id}: {m.rejected_id} is not a rejected earlier finding")
        elif m.id in used:
            dropped.append(f"{m.id} matched more than one rejected finding")
        elif not m.reason.strip():
            dropped.append(f"{m.id}: no reason given")
        else:
            used.add(m.id)
            kept.append(m)
    return kept, dropped


async def run_repeats(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("repeats"):
        return state
    target, cfg = state.target, ctx.config
    rejected = rounds.rejected(earlier_findings(state, ctx))
    if not rejected:
        ctx.counts = {"rejected earlier": 0}
        state.stage = "repeats"
        return state
    by_id = {e.id: e for e in rejected}
    shown = shown_findings(state)

    for f in shown:
        e = by_id.get(f.id)
        if e is not None and f.id not in state.repeats:
            state.repeats[f.id] = Repeat(rejected_id=f.id, round=e.head_sha, note=e.note, by="id",
                                         reason=f"the same finding was rejected in round {e.head_sha[:12]}")

    todo = [f for f in shown if f.id not in state.repeats and f.id not in state.repeats_checked]
    failed = False
    if todo:
        try:
            out = await sdk.agent_call_structured(
                prompt.build(target, todo, rejected), RepeatsOutput,
                allowed_tools=sdk.MERGER_TOOLS, model=cfg.merger_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="repeats",
            )
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("repeats agent failed for %s", target.slug, exc_info=True)
            state.errors.append(f"repeats: {str(e).splitlines()[0][:200]}")
            out, failed = RepeatsOutput(), True
        kept, dropped = gate_matches(out.matches, {f.id for f in todo}, set(by_id))
        for line in dropped:
            state.errors.append(f"repeats: dropped a match: {line}")
        for m in kept:
            e = by_id[m.rejected_id]
            state.repeats[m.id] = Repeat(rejected_id=m.rejected_id, reason=m.reason.strip(), round=e.head_sha,
                                         note=e.note, by="agent")
        # Stored as the answer arrives, so a resume does not ask again.
        state.repeats_checked.extend(f.id for f in todo)

    ctx.counts = {"rejected earlier": len(rejected), "repeats": len(state.repeats),
                  "agent_failures": int(failed)}
    state.stage = "repeats"
    return state
