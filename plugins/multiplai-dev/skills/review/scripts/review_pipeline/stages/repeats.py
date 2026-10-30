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

The same stage then matches the `--earlier-decisions` file (`ReviewConfig.earlier_decisions`):
decisions an earlier reviewer of the change made, which buildme writes from its block
reviews. The same two steps run: equal ids in Python, then one agent call for the rest,
through `gate_matches`. Matches go to `state.earlier_decisions`, the ids asked about to
`state.earlier_checked`. A match is not a repeat: it gets no label, stays listed and
blocking, and findings.json and the markdown say which decision it matches.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from .. import checks, rounds, sdk
from ..models import (
    AgentCheck,
    EarlierDecision,
    EarlierDecisionMatch,
    EarlierMatch,
    EarlierMatchesOutput,
    Finding,
    Repeat,
    RepeatMatch,
    RepeatsOutput,
    ReviewState,
)
from ..prompts import repeats as prompt
from . import RunContext

log = logging.getLogger(__name__)

SHOWN_STATUSES = ("confirmed", "unverifiable", "unverified")

_DECISIONS = TypeAdapter(list[EarlierDecision])


def load_earlier_decisions(path: Path) -> list[EarlierDecision]:
    """The `--earlier-decisions` file: a JSON list of `EarlierDecision`, ids unique. Raises
    ValueError naming the file and what is wrong with it."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"cannot read --earlier-decisions {path}: {e}") from e
    except ValueError as e:
        raise ValueError(f"--earlier-decisions {path} is not JSON: {e}") from e
    try:
        decisions = _DECISIONS.validate_python(raw)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "the top level"
        raise ValueError(
            f"--earlier-decisions {path}: {where}: {first['msg']} "
            f"({e.error_count()} error{'' if e.error_count() == 1 else 's'})"
        ) from e
    seen: set[str] = set()
    for d in decisions:
        if d.id in seen:
            raise ValueError(f"--earlier-decisions {path}: id {d.id!r} appears twice")
        seen.add(d.id)
    return decisions


def shown_findings(state: ReviewState) -> list[Finding]:
    return [
        f
        for f in state.findings
        if getattr(state.verdicts.get(f.id), "status", "") in SHOWN_STATUSES
    ]


def earlier_findings(
    state: ReviewState, ctx: RunContext
) -> list[rounds.EarlierFinding]:
    if ctx.target_dir is None:
        return []
    t = state.target
    elsewhere = rounds.history_elsewhere(ctx.target_dir, t.slug, t.remote_url)
    return rounds.load_rounds(ctx.target_dir, t.head_sha, elsewhere)


def gate_matches(
    matches: list[RepeatMatch] | list[EarlierMatch],
    shown_ids: set[str],
    rejected_ids: set[str],
    *,
    key: str = "rejected_id",
    listed: str = "rejected earlier finding",
) -> tuple[list, list[str]]:
    """(the matches kept, one line per match dropped). *key* is the match's field naming the
    listed item (`rejected_id`, or an earlier decision's `decision_id`); *listed* what it is."""
    kept, dropped, used = [], [], set()
    for m in matches:
        other = getattr(m, key)
        if m.id not in shown_ids:
            dropped.append(f"{m.id} is not a finding of this round")
        elif other not in rejected_ids:
            dropped.append(f"{m.id}: {other} is not a {listed}")
        elif m.id in used:
            dropped.append(f"{m.id} matched more than one {listed.split()[-1]}")
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
        ctx.counts = {
            "rejected earlier": 0,
            **await match_earlier_decisions(state, ctx),
        }
        state.stage = "repeats"
        return state
    by_id = {e.id: e for e in rejected}
    shown = shown_findings(state)

    for f in shown:
        e = by_id.get(f.id)
        if e is not None and f.id not in state.repeats:
            state.repeats[f.id] = Repeat(
                rejected_id=f.id,
                round=e.head_sha,
                note=e.note,
                by="id",
                reason=f"the same finding was rejected in round {e.head_sha[:12]}",
            )

    todo = [
        f
        for f in shown
        if f.id not in state.repeats and f.id not in state.repeats_checked
    ]
    failed = False
    if todo:
        started = checks.now()
        error = ""
        with sdk.recording() as rec:
            try:
                out = await sdk.agent_call_structured(
                    prompt.build(target, todo, rejected),
                    RepeatsOutput,
                    allowed_tools=sdk.MERGER_TOOLS,
                    model=cfg.merger_model,
                    effort=cfg.effort,
                    max_turns=cfg.max_turns,
                    cwd=str(ctx.snapshot),
                    budget_label="repeats",
                )
            except (sdk.RepoTrustError, sdk.UsageLimitError):
                raise
            except sdk.AgentCallError as e:
                log.error("repeats agent failed for %s", target.slug, exc_info=True)
                error = f"repeats: {str(e).splitlines()[0][:200]}"
                state.errors.append(error)
                out, failed = RepeatsOutput(), True
        kept, dropped = gate_matches(out.matches, {f.id for f in todo}, set(by_id))
        check = AgentCheck(
            stage="repeats",
            subject="repeats",
            given=[f"findings ({len(todo)})", f"rejected earlier ({len(rejected)})"],
            calls=[
                checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls
            ],
            turns=rec.turns,
            cost_usd=round(rec.cost_usd, 6),
            started_at=started,
            ended_at=checks.now(),
            outcome="; ".join(f"{m.id} repeats {m.rejected_id}" for m in kept)
            or "no repeats",
        )
        if error:
            check.error, check.outcome = error, f"failed: {checks.failure_kind(error)}"
        state.checks.append(check)
        for line in dropped:
            state.errors.append(f"repeats: dropped a match: {line}")
        for m in kept:
            e = by_id[m.rejected_id]
            state.repeats[m.id] = Repeat(
                rejected_id=m.rejected_id,
                reason=m.reason.strip(),
                round=e.head_sha,
                note=e.note,
                by="agent",
            )
        # Stored as the answer arrives, so a resume does not ask again.
        state.repeats_checked.extend(f.id for f in todo)

    ctx.counts = {
        "rejected earlier": len(rejected),
        "repeats": len(state.repeats),
        "agent_failures": int(failed),
        **await match_earlier_decisions(state, ctx),
    }
    state.stage = "repeats"
    return state


async def match_earlier_decisions(
    state: ReviewState, ctx: RunContext
) -> dict[str, int]:
    """Match this round's shown findings against `ctx.config.earlier_decisions`; the counts for
    the stage event, none without a file. Equal ids match in Python; the rest not yet asked
    about (`state.earlier_checked`) go to one agent call, whose matches pass `gate_matches`."""
    decisions = ctx.config.earlier_decisions
    if not decisions:
        return {}
    target, cfg = state.target, ctx.config
    by_id = {d.id: d for d in decisions}
    shown = shown_findings(state)
    for f in shown:
        d = by_id.get(f.id)
        if d is not None and f.id not in state.earlier_decisions:
            state.earlier_decisions[f.id] = EarlierDecisionMatch(
                id=d.id,
                decision=d.decision,
                reason=d.reason,
                match_reason="the same id",
                by="id",
            )
    todo = [
        f
        for f in shown
        if f.id not in state.earlier_decisions and f.id not in state.earlier_checked
    ]
    failed = False
    if todo:
        started = checks.now()
        error = ""
        with sdk.recording() as rec:
            try:
                out = await sdk.agent_call_structured(
                    prompt.build_earlier(target, todo, decisions),
                    EarlierMatchesOutput,
                    allowed_tools=sdk.MERGER_TOOLS,
                    model=cfg.merger_model,
                    effort=cfg.effort,
                    max_turns=cfg.max_turns,
                    cwd=str(ctx.snapshot),
                    budget_label="repeats",
                )
            except (sdk.RepoTrustError, sdk.UsageLimitError):
                raise
            except sdk.AgentCallError as e:
                log.error(
                    "earlier-decisions agent failed for %s", target.slug, exc_info=True
                )
                error = f"earlier decisions: {str(e).splitlines()[0][:200]}"
                state.errors.append(error)
                out, failed = EarlierMatchesOutput(), True
        kept, dropped = gate_matches(
            out.matches,
            {f.id for f in todo},
            set(by_id),
            key="decision_id",
            listed="known earlier decision",
        )
        check = AgentCheck(
            stage="repeats",
            subject="earlier-decisions",
            given=[f"findings ({len(todo)})", f"earlier decisions ({len(decisions)})"],
            calls=[
                checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls
            ],
            turns=rec.turns,
            cost_usd=round(rec.cost_usd, 6),
            started_at=started,
            ended_at=checks.now(),
            outcome="; ".join(f"{m.id} matches {m.decision_id}" for m in kept)
            or "no matches",
        )
        if error:
            check.error, check.outcome = error, f"failed: {checks.failure_kind(error)}"
        state.checks.append(check)
        for line in dropped:
            state.errors.append(f"earlier decisions: dropped a match: {line}")
        for m in kept:
            d = by_id[m.decision_id]
            state.earlier_decisions[m.id] = EarlierDecisionMatch(
                id=d.id,
                decision=d.decision,
                reason=d.reason,
                match_reason=m.reason.strip(),
                by="agent",
            )
        # Stored as the answer arrives, so a resume does not ask again.
        state.earlier_checked.extend(f.id for f in todo)
    return {
        "earlier decisions": len(decisions),
        "earlier matches": len(state.earlier_decisions),
        "earlier_failures": int(failed),
    }
