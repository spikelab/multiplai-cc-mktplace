"""Rules that memory already held, and that a later session learned again.

``dream.py`` drops an item as ``redundant`` when the judge finds the corpus
already says it, and ``rejections`` logs the drop. Read one way that is
housekeeping: the backlog proposed something memory had, the pipeline refused
it, nothing was lost. Read the other way it is the only signal this system
produces about whether a *written* rule is doing any work.

Because a redundant RULE is not a duplicate. It is a rule that was in memory,
that a session did not follow, that the user corrected in-session, that
extraction captured — and that consolidation then deleted precisely *because*
memory already contained the rule nobody obeyed. Every step behaved correctly
and the outcome is that the evidence of the failure is thrown away. The drafting
prompt says so outright (``dream.py``, drafting rules): "Deduplicate: if the same
lesson appears multiple times, merge into one entry. (Don't annotate the count.)"

So this module does not detect anything new. It re-reads the rejection log with
the count kept.

**What a hit means, and what it does not.** A redundant RULE says the rule was
re-derived, not that it was disobeyed — a session can restate a rule it followed
perfectly well. That is why the diagnosis below leans on injection telemetry
rather than on the count alone, and why every verdict this module emits is
:data:`INCONCLUSIVE` unless the telemetry actually answers the question.

**Three fixes, and the evidence that separates them.** A rule that keeps coming
back is failing in one of three ways, and they take opposite repairs:

* :data:`NOT_ROUTED` — the memory file was never injected on the day the
  learning was captured. The rule is written where the router does not look for
  it; the fix is a routing keyword or promotion to always-loaded. Rewriting the
  rule would change nothing.
* :data:`ROUTED_IGNORED` — the file *was* injected and the utilisation judge
  scored it unused. The rule was in the context window and did not land. The fix
  is wording — a rule naming an intent without naming the permitted mechanism is
  the documented shape of this failure — or deleting whatever contradicts it.
* :data:`INCONCLUSIVE` — the file was injected and used, or there is no
  telemetry for that day. File-level "used" cannot tell us whether *this* rule
  fired, and inventing a verdict from that would be the failure this whole
  pipeline exists to prevent.

**The join is by day, and that is a real limit.** A rejection cites its source
learning as ``<YYYY-MM-DD>.md:<line>``; ``/dream-remember`` step 5 then deletes
that file, so the session id inside it is usually gone by the time anyone runs
this. The date in the filename survives, and ``utilisation.jsonl`` is stamped
per session, so the question this module can actually answer is "was that file
injected in *any* session that day" — not "in the session that produced this
learning". It is reported as :data:`SAME_DAY` for that reason. On a one-session
day the two coincide; on a busy day the evidence is weaker than it looks, and
overstating it would be worse than the coarse answer.

**No third similarity measure.** Grouping re-learns of one rule uses
``conflict_edits.overlap`` at that module's own ``MIN_OVERLAP`` — the same
measure and threshold the routing gate and ``dream_prescreen`` already screen
with. A rule re-learned in different words is one group; scoping the comparison
to a single target file keeps the unrelated near-misses out.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from lib.conflict_edits import MIN_OVERLAP, overlap

__all__ = [
    "NOT_ROUTED",
    "ROUTED_IGNORED",
    "INCONCLUSIVE",
    "SAME_DAY",
    "NO_TELEMETRY",
    "Relearn",
    "RelearnGroup",
    "redundant_records",
    "injection_index",
    "group_relearns",
    "analyse",
    "render_section",
]

#: The memory file was not injected on the day the learning was captured.
NOT_ROUTED = "not-routed"
#: It was injected, and the utilisation judge scored it unused.
ROUTED_IGNORED = "routed-but-ignored"
#: Injected and used, or no telemetry for that day. No verdict.
INCONCLUSIVE = "inconclusive"

#: Evidence strength labels, so a reader never reads more into a verdict than
#: the join can carry.
SAME_DAY = "same-day sessions"
NO_TELEMETRY = "no telemetry for that day"

#: Only ``redundant`` is a retention signal. ``judge-drop`` means the judge
#: thought the item wrong, thin or unsupported — a drafting problem, and a
#: different report.
_REDUNDANT = "redundant"

#: A rule is normative, so re-deriving one says something about behaviour. A
#: re-derived FACT usually says the world was re-checked, which is cheap and
#: often correct; it is counted but never ranked above a rule.
_RULE = "RULE"

_SOURCE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md")


@dataclass(frozen=True)
class Relearn:
    """One redundant drop, with the fields the report reads."""

    target: str
    title: str
    text: str
    kind: str
    source: str
    ts: str
    judge_reason: str = ""

    @property
    def learning_date(self) -> str:
        """The day the source learning was captured, or ``""``.

        Read from the cited learnings *filename*, which is all that survives
        ``/dream-remember`` step 5 deleting the file itself.
        """
        match = _SOURCE_DATE.search(self.source or "")
        return match.group(1) if match else ""


@dataclass
class RelearnGroup:
    """One memory rule, and every occasion it was learned again."""

    target: str
    title: str
    occurrences: list[Relearn] = field(default_factory=list)
    verdict: str = INCONCLUSIVE
    evidence: str = NO_TELEMETRY

    @property
    def count(self) -> int:
        return len(self.occurrences)

    @property
    def dates(self) -> list[str]:
        return sorted({o.learning_date for o in self.occurrences if o.learning_date})


def redundant_records(
    records: Iterable[Mapping], *, kinds: Optional[Sequence[str]] = (_RULE,)
) -> list[Relearn]:
    """The redundant drops, as :class:`Relearn`, newest last.

    *kinds* defaults to rules alone. Pass ``None`` to keep every kind — the
    report offers that as ``--all-kinds``, because a re-derived FACT is worth
    seeing occasionally even though it rarely means what a re-derived rule
    means.
    """
    wanted = {k.upper() for k in kinds} if kinds else None
    out: list[Relearn] = []
    for record in records:
        if str(record.get("reason", "")) != _REDUNDANT:
            continue
        kind = str(record.get("kind", "") or "").upper()
        if wanted is not None and kind not in wanted:
            continue
        out.append(
            Relearn(
                target=str(record.get("target", "") or ""),
                title=str(record.get("title", "") or ""),
                text=str(record.get("text", "") or ""),
                kind=kind,
                source=str(record.get("source", "") or ""),
                ts=str(record.get("ts", "") or ""),
                judge_reason=str(record.get("judge_reason", "") or ""),
            )
        )
    return out


def injection_index(rows: Iterable[Mapping]) -> dict[str, dict[str, bool]]:
    """``{date: {memory_file: was_judged_used}}`` from ``utilisation.jsonl``.

    A file counts as injected on a day if any session that day injected it, and
    as used if any of those sessions' judges scored it used. Both are ORs over
    the day because the join is a day (see the module docstring): the weaker
    claim is the one the data supports, and OR is the direction that avoids
    accusing a rule of being ignored when some session did use its file.
    """
    index: dict[str, dict[str, bool]] = {}
    for row in rows:
        day = str(row.get("ts", ""))[:10]
        if not day:
            continue
        seen = index.setdefault(day, {})
        for entry in row.get("injected") or []:
            name = str((entry or {}).get("file", "") or "")
            if name:
                seen.setdefault(name, False)
        for entry in row.get("judge") or []:
            name = str((entry or {}).get("file", "") or "")
            if name and (entry or {}).get("used"):
                seen[name] = True
    return index


def _diagnose(
    group: RelearnGroup, index: Mapping[str, Mapping[str, bool]]
) -> tuple[str, str]:
    """Verdict and evidence label for one group.

    Resolved over every occurrence, not just the first: one occasion where the
    file was not routed at all is a routing hole worth naming even if another
    occasion had it loaded, so :data:`NOT_ROUTED` outranks the rest. Below that,
    a day where the file was injected and unused is the ignored case. Anything
    else declines to answer.
    """
    verdicts: list[str] = []
    saw_telemetry = False
    for occurrence in group.occurrences:
        day = index.get(occurrence.learning_date)
        if day is None:
            continue
        saw_telemetry = True
        if group.target not in day:
            verdicts.append(NOT_ROUTED)
        elif not day[group.target]:
            verdicts.append(ROUTED_IGNORED)
        else:
            verdicts.append(INCONCLUSIVE)
    if not saw_telemetry:
        return INCONCLUSIVE, NO_TELEMETRY
    if NOT_ROUTED in verdicts:
        return NOT_ROUTED, SAME_DAY
    if ROUTED_IGNORED in verdicts:
        return ROUTED_IGNORED, SAME_DAY
    return INCONCLUSIVE, SAME_DAY


def group_relearns(relearns: Sequence[Relearn]) -> list[RelearnGroup]:
    """Cluster re-learns of one rule, scoped to a target file.

    Greedy single-pass clustering on ``conflict_edits.overlap`` — the repo's one
    calibrated similarity, at its own threshold. Greedy is right here because
    the groups are tiny and a reader checks them by eye; the cost of a wrong
    merge is one line of a report, not a memory write.

    **Title and text are scored together, not just the title.** ``overlap_sets``
    returns 0.0 when either side holds fewer than ``MIN_CONTENT_WORDS`` (4)
    content words, and a rejection title is routinely thinner than that
    ("Worktree location convention" has three). Scoring the title alone
    therefore silently refuses to match exactly the short, sharp rules most
    worth catching. The concatenation clears the floor — measured minimum 12
    content words, median 24, over the 63 rule drops on this workspace — and
    the title is still scored on its own as a booster, because two re-learns
    often share a title while their bodies diverge.

    No stemming, deliberately. ``content_words`` does not collapse
    ``worktree``/``worktrees``, and that is a decision with a backtest behind it
    (see ``conflict_edits.content_words``): singular collapse made the
    precision/recall ratio worse at every threshold over a 602-item backlog. A
    pair it would obviously fix is not evidence against that measurement.

    Sorted for review: most re-learned first, then by file, so the rule that
    keeps coming back is the first thing read.
    """
    groups: list[RelearnGroup] = []
    for item in relearns:
        for group in groups:
            if group.target != item.target:
                continue
            head = group.occurrences[0]
            score = max(
                overlap(head.title, item.title),
                overlap(
                    f"{head.title} {head.text}",
                    f"{item.title} {item.text}",
                ),
            )
            if score >= MIN_OVERLAP:
                group.occurrences.append(item)
                break
        else:
            groups.append(RelearnGroup(target=item.target, title=item.title,
                                       occurrences=[item]))
    groups.sort(key=lambda g: (-g.count, g.target, g.title))
    return groups


def analyse(
    records: Iterable[Mapping],
    utilisation_rows: Iterable[Mapping] = (),
    *,
    kinds: Optional[Sequence[str]] = (_RULE,),
) -> list[RelearnGroup]:
    """Group the redundant drops and diagnose each group."""
    index = injection_index(utilisation_rows)
    groups = group_relearns(redundant_records(records, kinds=kinds))
    for group in groups:
        group.verdict, group.evidence = _diagnose(group, index)
    return groups


def _fix_for(verdict: str) -> str:
    if verdict == NOT_ROUTED:
        return "route it — add a keyword, or promote to always-loaded"
    if verdict == ROUTED_IGNORED:
        return "reword it — name the permitted mechanism, or delete what contradicts it"
    return "read it — telemetry cannot say which"


def render_section(groups: Sequence[RelearnGroup], *, limit: int = 20) -> str:
    """The ``## Rules Re-learned`` section, for the dream proposal.

    Empty input renders a one-line all-clear rather than nothing: a missing
    section reads as "not run", and those are different facts.
    """
    lines = ["## Rules Re-learned", ""]
    if not groups:
        lines.append(
            "No rule in memory was re-derived and dropped as redundant since the "
            "last run. Nothing to fix here."
        )
        return "\n".join(lines) + "\n"

    repeated = [g for g in groups if g.count > 1]
    events = sum(g.count for g in groups)
    lines.append(
        f"{events} item(s) were dropped as already-in-memory, covering "
        f"{len(groups)} distinct rule(s); {len(repeated)} came back more than "
        f"once. Each one is a rule memory already held and a session derived "
        f"again — the count is the signal, so it is kept here rather than "
        f"merged away."
    )
    lines.append("")

    by_verdict = Counter(g.verdict for g in groups)
    lines.append(
        f"Diagnosis: {by_verdict.get(NOT_ROUTED, 0)} not routed, "
        f"{by_verdict.get(ROUTED_IGNORED, 0)} routed but unused, "
        f"{by_verdict.get(INCONCLUSIVE, 0)} inconclusive. Evidence is a "
        f"same-day join against session telemetry, never the exact session."
    )
    lines.append("")
    lines.append("| × | File | Rule | Verdict | Fix |")
    lines.append("|---|---|---|---|---|")
    for group in groups[:limit]:
        title = group.title.replace("|", "\\|")[:70]
        lines.append(
            f"| {group.count} | `{group.target}` | {title} | "
            f"{group.verdict} | {_fix_for(group.verdict)} |"
        )
    if len(groups) > limit:
        lines.append("")
        lines.append(f"_{len(groups) - limit} more not shown; run "
                     f"`relearn_report.py --limit 0` for the full list._")
    return "\n".join(lines) + "\n"


def read_utilisation(path: Path) -> list[dict]:
    """Utilisation rows, oldest first. Unparseable lines are skipped.

    Mirrors ``rejections.read``: this report is a lens over telemetry, and a
    torn line must cost that row rather than the report.
    """
    import json

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
