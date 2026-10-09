"""assess: one agent reads the shown findings together and labels each.

Runs after `repeats`. Every shown (confirmed or unverifiable) finding ends
with an `Assessment` in `state.assessments`:

- `repeat`: matched by the repeats stage to a finding the person rejected in
  an earlier round. Not sent to the agent.
- `still-open`: the same defect as an earlier finding the person accepted or
  has not decided (deferred counts as not decided), so it is not fixed yet.
- `low-value`: true, but not worth acting on, for one named rule
  (`prompts.assess.LOW_VALUE_RULES`: context, covered, speculative).
- `useful`: everything else, and the fallback for anything the gate rejects.

`gate_assessments` checks the agent's answer in Python: every id is a shown
finding; an `earlier_id` exists in `rounds/`; `still-open` needs an
`earlier_id` the person accepted, deferred or has not decided; `low-value` needs a
reason naming a rule. A finding left out, or a label that fails, becomes
`useful` with a line in `state.errors`.

The agent may also name duplicates; they are merged with the merge stage's
`usable_sets` and `merge_set`, so a duplicate keeps every citation and
finder. The stage never deletes a finding and never changes a verdict or a
severity. It is skipped when fewer than two findings are shown and there are
no earlier rounds.
"""

from __future__ import annotations

import logging
import re

from .. import rounds, sdk
from ..models import AssessItem, AssessOutput, Assessment, Finding, ReviewState
from ..prompts import assess as prompt
from . import RunContext
from .merge import apply_duplicate_sets
from .repeats import earlier_findings, shown_findings

log = logging.getLogger(__name__)

LABELS = ("useful", "still-open", "low-value")
# What `rounds.accepted_or_open` gives the agent: accepted, deferred (not decided yet), or no decision.
OPEN_DECISIONS = ("accept", "defer", "")
_RULE = re.compile(r"\b(" + "|".join(prompt.LOW_VALUE_RULES) + r")\b", re.IGNORECASE)


def gate_assessments(items: list[AssessItem], candidates: list[Finding],
                     earlier: list[rounds.EarlierFinding]) -> tuple[dict[str, Assessment], list[str]]:
    """(an Assessment for every candidate, one line per item the gate turned into `useful`)."""
    ids = {f.id for f in candidates}
    by_earlier = {e.id: e for e in earlier}
    out: dict[str, Assessment] = {}
    errors: list[str] = []
    for item in items:
        why = ""
        e = by_earlier.get(item.earlier_id) if item.earlier_id else None
        if item.id not in ids:
            errors.append(f"{item.id} is not a finding of this round")
            continue
        if item.id in out:
            errors.append(f"{item.id} was labelled twice; the first label stands")
            continue
        if item.label not in LABELS:
            why = f"unknown label {item.label!r}"
        elif item.earlier_id and e is None:
            why = f"earlier_id {item.earlier_id} is not in an earlier round"
        elif item.label == "still-open" and e is None:
            why = "still-open without an earlier_id"
        elif item.label == "still-open" and e.decision not in OPEN_DECISIONS:
            why = f"still-open, but the person's decision on {e.id} is {e.decision}"
        elif item.label == "low-value" and not _RULE.search(item.reason or ""):
            why = "low-value without naming a rule (context, covered, speculative)"
        if why:
            errors.append(f"{item.id}: {why}; labelled useful")
            out[item.id] = Assessment(label="useful")
            continue
        out[item.id] = Assessment(
            label=item.label, reason=item.reason.strip(), earlier_id=item.earlier_id if e else "",
            earlier_round=e.head_sha if e else "", earlier_decision=e.decision if e else "",
            earlier_note=e.note if e else "")
    missing = [f.id for f in candidates if f.id not in out]
    if missing:
        errors.append(f"no label for {', '.join(missing)}; labelled useful")
        for fid in missing:
            out[fid] = Assessment(label="useful")
    return out, errors


def _repeat_assessments(state: ReviewState) -> dict[str, Assessment]:
    shown = {f.id for f in shown_findings(state)}
    return {fid: Assessment(label="repeat", reason=r.reason, earlier_id=r.rejected_id, earlier_round=r.round,
                            earlier_decision="reject", earlier_note=r.note)
            for fid, r in state.repeats.items() if fid in shown}


async def run_assess(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("assess"):
        return state
    target, cfg = state.target, ctx.config
    earlier = earlier_findings(state, ctx)
    repeats = _repeat_assessments(state)
    candidates = [f for f in shown_findings(state) if f.id not in repeats]

    if not candidates or (len(candidates) < 2 and not earlier):
        state.assessments = {**{f.id: Assessment(label="useful") for f in candidates}, **repeats}
        ctx.counts = {"skipped": 1, "repeats": len(repeats)}
        state.stage = "assess"
        return state

    prior = rounds.accepted_or_open(earlier)
    failed = False
    if state.assess_answer is None:
        try:
            answer = await sdk.agent_call_structured(
                prompt.build(target, candidates, state.verdicts, prior), AssessOutput,
                allowed_tools=sdk.MERGER_TOOLS, model=cfg.merger_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="assess",
            )
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("assess agent failed for %s", target.slug, exc_info=True)
            state.errors.append(f"assess: {str(e).splitlines()[0][:200]}")
            answer, failed = AssessOutput(), True
        # Stored as it arrives (empty when the call failed), so a resume does not ask twice.
        state.assess_answer = answer
    answer = state.assess_answer

    if failed:
        labels = {f.id: Assessment(label="useful") for f in candidates}
    else:
        labels, errors = gate_assessments(answer.assessments, candidates, prior)
        state.errors.extend(f"assess: {line}" for line in errors)

    removed = apply_duplicate_sets(state, [(candidates, answer.duplicate_sets)], by="assess")
    state.assessments = {**{k: v for k, v in labels.items() if k not in removed}, **repeats}

    values = [a.label for a in state.assessments.values()]
    ctx.counts = {"useful": values.count("useful"), "still_open": values.count("still-open"),
                  "low_value": values.count("low-value"), "repeats": values.count("repeat"),
                  "merged": len(removed), "agent_failures": int(failed)}
    state.stage = "assess"
    return state
