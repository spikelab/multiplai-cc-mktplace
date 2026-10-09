"""merge: one agent per group of overlapping findings, asked which are one defect.

The finders run independently, one per dimension, and `find` drops only
word-for-word copies, so one defect reported by three finders in three
wordings survives as three findings. This stage groups the confirmed and
unverifiable findings in each file whose line ranges overlap or nearly touch,
and asks one agent per group of two or more which of them describe the same
defect. Each such set becomes one finding that keeps:

- the highest severity in the set;
- every citation, and every finder that reported it;
- the id, claim and verdict of the finding it is merged into, which is a
  confirmed one where the set has one, then the most severe, then the first.

Every finding merged away goes to `state.merged` with the id it was merged
into. An agent failure leaves its group unmerged and is listed in
`state.errors`.
"""

from __future__ import annotations

import logging

from .. import checks, sdk
from ..models import SEVERITIES, AgentCheck, DuplicateSet, Finding, Merged, MergeOutput, ReviewState
from ..prompts import merge as prompt
from . import RunContext, bounded

log = logging.getLogger(__name__)

# Two ranges whose lines are at most this far apart are grouped as if they
# overlapped: finders often cite a statement and the line after it.
MERGE_LINE_GAP = 2
MERGED_STATUSES = ("confirmed", "unverifiable")


def _near(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Two line ranges overlap or are at most MERGE_LINE_GAP lines apart."""
    return a_start <= b_end + MERGE_LINE_GAP and b_start <= a_end + MERGE_LINE_GAP


def linked(a: Finding, b: Finding) -> bool:
    """Two findings may be one defect: their anchors overlap in one file, or any
    repository citation of one overlaps any repository citation of the other.

    The second test catches one defect reported from two files: a finding
    anchored in a test and one anchored in the code it tests, both citing the
    same line of a third file. Web citations never link findings.
    """
    if a.file == b.file and _near(a.line_start, a.line_end, b.line_start, b.line_end):
        return True
    return any(ca.path == cb.path and _near(ca.line_start, ca.line_end, cb.line_start, cb.line_end)
               for ca in a.citations if not ca.is_web
               for cb in b.citations if not cb.is_web)


def link_groups(findings: list[Finding]) -> list[list[Finding]]:
    """Groups of 2+ of *findings*, chained by `linked`.

    A group is every finding reachable from another by one link. Groups come
    in the order their first member appears in *findings*; members are in
    file order of first appearance, then line order.
    """
    parent = list(range(len(findings)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            if linked(findings[i], findings[j]):
                parent[root(j)] = root(i)
    members: dict[int, list[int]] = {}
    for i in range(len(findings)):
        members.setdefault(root(i), []).append(i)
    file_order: dict[str, int] = {}
    for f in findings:
        file_order.setdefault(f.file, len(file_order))
    groups = []
    for idx in sorted(members.values(), key=min):
        if len(idx) < 2:
            continue
        groups.append(sorted((findings[i] for i in idx),
                             key=lambda f: (file_order[f.file], f.line_start, f.line_end)))
    return groups


def overlap_groups(state: ReviewState) -> list[list[Finding]]:
    """Groups of 2+ shown (confirmed or unverifiable) findings that `linked` chains together."""
    shown = [f for f in state.findings
             if getattr(state.verdicts.get(f.id), "status", "") in MERGED_STATUSES]
    return link_groups(shown)


def group_key(group: list[Finding]) -> str:
    return ",".join(sorted(f.id for f in group))


def usable_sets(group: list[Finding], sets: list[DuplicateSet]) -> list[tuple[list[str], str]]:
    """The agent's sets, keeping only ids in the group, each id once, sets of 2+."""
    in_group = {f.id for f in group}
    used: set[str] = set()
    out = []
    for s in sets:
        ids = [i for i in dict.fromkeys(s.finding_ids) if i in in_group and i not in used]
        if len(ids) < 2:
            continue
        used.update(ids)
        out.append((ids, s.reason))
    return out


def _citation_key(c) -> tuple[str, int, int, str]:
    return (c.path, c.line_start, c.line_end, " ".join(c.quote.split()))


def merge_set(members: list[Finding], state: ReviewState) -> Finding:
    """The finding *members* become; the first of them in `state.findings` order breaks ties."""
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    status_rank = {"confirmed": 0, "unverifiable": 1}
    survivor = min(members, key=lambda f: (status_rank[state.verdicts[f.id].status], rank[f.severity]))
    severity = min((f.severity for f in members), key=rank.__getitem__)
    citations, seen = [], set()
    finders: list[str] = []
    for f in [survivor] + [m for m in members if m is not survivor]:
        for c in f.citations:
            if _citation_key(c) not in seen:
                seen.add(_citation_key(c))
                citations.append(c)
        for name in f.finders or [f.finder]:
            if name and name not in finders:
                finders.append(name)
    if survivor.id in state.original_severity:
        # Every member is unverifiable (a confirmed one would have survived),
        # so each was lowered; the merged finding was lowered from the highest.
        state.original_severity[survivor.id] = min(
            (state.original_severity.get(f.id, f.severity) for f in members), key=rank.__getitem__)
    return survivor.model_copy(update={"severity": severity, "citations": citations, "finders": finders})


def mark_merged(state: ReviewState) -> None:
    """Set the `merged` fate, with `into`, on every finder entry whose finding was merged away."""
    into = {m.finding.id: m.into for m in state.merged}
    for check in state.checks:
        if check.stage == "find":
            for entry in check.findings:
                if entry.get("fate") == "kept" and entry.get("id") in into:
                    entry.update(fate="merged", into=into[entry["id"]])


def _record_merges(state: ReviewState, groups: list[list[Finding]]) -> None:
    """Each merge entry's outcome, and the `merged` fate in the finders' entries."""
    outcomes: dict[str, list[str]] = {}
    for m in state.merged:
        for group in groups:
            if m.finding.id in {f.id for f in group}:
                outcomes.setdefault(group_key(group), []).append(f"merged {m.finding.id} into {m.into}")
    for check in state.checks:
        if check.stage == "merge" and not check.error:
            check.outcome = "; ".join(outcomes.get(check.subject, [])) or "no duplicates"
    mark_merged(state)


def apply_duplicate_sets(state: ReviewState, answers: list[tuple[list[Finding], list[DuplicateSet]]],
                         *, by: str = "") -> set[str]:
    """Merge each usable set of each (group, answer) into one finding; return the ids merged away.

    *by* names the stage in each merged finding's reason when it is not merge.
    """
    order = {f.id: i for i, f in enumerate(state.findings)}
    by_id = {f.id: f for f in state.findings}
    replaced: dict[str, Finding] = {}
    removed: set[str] = set()
    for group, sets in answers:
        for ids, reason in usable_sets(group, sets):
            ids = [i for i in ids if i in by_id and i not in removed]
            if len(ids) < 2:
                continue
            members = sorted((replaced.get(i, by_id[i]) for i in ids), key=lambda f: order[f.id])
            merged = merge_set(members, state)
            replaced[merged.id] = merged
            for m in members:
                if m.id == merged.id:
                    continue
                removed.add(m.id)
                why = f"the same defect as {merged.id} ({merged.file}:{merged.line_start})"
                if by:
                    why = f"{by}: {why}"
                state.merged.append(Merged(finding=m, into=merged.id,
                                           reason=f"{why}: {reason}" if reason.strip() else why))
    state.findings = [replaced.get(f.id, f) for f in state.findings if f.id not in removed]
    return removed


async def run_merge(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("merge"):
        return state
    target, cfg = state.target, ctx.config
    groups = overlap_groups(state)
    todo = [g for g in groups if group_key(g) not in state.merge_answers]
    failures = 0

    async def one(group: list[Finding]) -> None:
        nonlocal failures
        where = f"{group[0].file}:{group[0].line_start}-{max(f.line_end for f in group)}"
        started = checks.now()
        with sdk.recording() as rec:
            try:
                out = await sdk.agent_call_structured(
                    prompt.build(target, group), MergeOutput,
                    allowed_tools=sdk.MERGER_TOOLS, model=cfg.merger_model, effort=cfg.effort,
                    max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label="merge",
                )
                error = ""
            except sdk.RepoTrustError:
                raise
            except sdk.AgentCallError as e:
                log.error("merge agent failed for %s", where, exc_info=True)
                error = f"merge {where}: {str(e).splitlines()[0][:200]}"
        check = AgentCheck(
            stage="merge", subject=group_key(group), given=[f"findings ({len(group)})"],
            calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
            turns=rec.turns, cost_usd=round(rec.cost_usd, 6), started_at=started, ended_at=checks.now(),
            outcome="no duplicates",
        )
        if error:
            failures += 1
            state.errors.append(error)
            check.error, check.outcome = error, f"failed: {checks.failure_kind(error)}"
            # Stored as an empty answer: the group stays unmerged, and a
            # resume does not pay to ask again.
            state.merge_answers[group_key(group)] = []
            state.checks.append(check)
            return
        # Stored as each answer arrives, so a budget stop keeps it.
        state.merge_answers[group_key(group)] = out.duplicate_sets
        state.checks.append(check)
        if ctx.progress:
            dupes = sum(len(ids) - 1 for ids, _ in usable_sets(group, out.duplicate_sets))
            ctx.progress.line(f"  merge {where}: {len(group)} findings, {dupes} duplicates")

    await bounded(todo, one, cfg.concurrency)

    removed = apply_duplicate_sets(state, [(g, state.merge_answers.get(group_key(g), [])) for g in groups])
    _record_merges(state, groups)

    ctx.counts = {"groups": len(groups), "merged": len(removed), "agent_failures": failures}
    state.stage = "merge"
    return state
