"""Decide which proposed memory entries a human must read, and which need not be.

A proposal is not too long to *review*; it is too long to review **item by
item**. At ~190 items the review costs a session's whole context window and gets
abandoned partway — which is why the pending backlog reached 380 bullets rather
than shrinking.

The first version of this module answered that with eight deterministic gates.
Measured against the real 2026-08-05 proposal it split 194 items 74 auto / 120
review, and **90 of those 120 were flagged for one reason**: the text contained
a word from a normative-language regex. That gate fires on "the API always
returns UTF-8", because the difference between a fact and an instruction is
semantic and a regex is not. Shape gates can judge an item's *form*; they can
never judge whether it is *true*, whether its citation supports it, or whether
the target file already says it. So the classifier is now a model
(``lib/memory_judge.py``) and this module holds what a model must not decide.

## The three layers, in the order they run

**1. The rubric — code, from the taxonomy pair.** Provenance sets confidence,
kind sets blast radius, and auto-apply is the intersection:

===========================  ========  ============  ========
                             ``FACT``  ``DECISION``  ``RULE``
===========================  ========  ============  ========
``CORRECTION``/``DECLARATION``  apply     apply       review
``EMPIRICAL``/``RESEARCH``      apply*    review      review
``INFERENCE``                   review    review      review
===========================  ========  ============  ========

\\* only when the judge reports the citation actually supports the claim.

``kind: RULE`` is ``review`` in every row **and every mode**. The reason is
blast radius, not confidence: a wrong fact is one you notice later, a wrong rule
changes what you notice. That holds even for a ``CORRECTION`` straight from the
user — the most trustworthy input in the system — because trustworthiness is not
the axis being managed.

**2. The judge — a model, and it may only lower.** It re-derives the pair, checks
the citation, checks for redundancy, and returns ``apply``/``review``/``drop``.
An item is applied only when the rubric permits it **and** the judge affirms it:
no verdict means no write, so a failed batch, an unparseable reply, or a missing
SDK all produce exactly the partition ``review`` mode produces. Failure goes
toward more human review, never less, and that is a property of the data flow
rather than of an error handler someone has to remember to write.

**3. The floor — code, and it can only refuse** (``lib/memory_write_floor.py``).
Path containment, reserved filenames, append-only, parse integrity. It runs
*after* the verdict, on the concrete write, so nothing a model returns can clear
it.

## What was deleted, and why

``_NORMATIVE_RE`` (75% of the review burden, and semantically wrong) and
``RECALL_FILES`` — an 18-name hand-maintained allowlist that went stale and was
invisible to every memory file added after it was written. The ``rule-proposal``
and ``low-confidence`` gates are superseded upstream: the first by ``kind:
RULE``, the second by the judge's own verdict. What replaces them judges content
instead of counting words.

The asymmetry that shaped the old gates still shapes these: a false "needs
review" costs one line of reading; a false "apply" writes something nobody
agreed to. Every ambiguity below resolves toward the human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from multiplai_core.plugin_options import option

from lib import taxonomy
from lib.memory_write_floor import (
    ADDITIVE_CHANGES,
    floor_check,
    is_reserved_target,
    is_safe_target,
    target_bank,
)
from lib.routing_validation import parse_proposal_entries

__all__ = [
    "ADDITIVE_CHANGES",
    "DEFAULT_WRITE_MODE",
    "Item",
    "REASON_LABELS",
    "REJECTION_DETAIL_LIMIT",
    "Triage",
    "WRITE_MODES",
    "apply_verdicts",
    "auto_slice",
    "classify",
    "flagged_by_routing",
    "floor_check",
    "has_routing_section",
    "is_reserved_target",
    "is_safe_target",
    "reconciled_pair",
    "render_receipt",
    "render_summary",
    "rubric_verdict",
    "shared_bank_items",
    "write_mode",
]

# `- \`file.md\` #3 (title): …` — the label routing_validation renders per
# warning. We only need the (file, number) pair to match warnings to items.
_WARNING_LABEL_RE = re.compile(r"^-\s+`(?P<target>[^`]+)`\s+#(?P<number>\d+)\b")


# --- the write modes --------------------------------------------------------

WRITE_MODES: tuple[str, ...] = ("review", "triage", "auto")

# `triage`, not `auto`. Auto-apply without a measured revert rate is a promise
# nobody has evidence for; `triage` is what produces that evidence, and flipping
# the default later is a config change rather than a redesign.
DEFAULT_WRITE_MODE = "triage"


def write_mode() -> str:
    """The configured ``memory_write_mode``, or the safe value.

    Read through :mod:`multiplai_core.plugin_options`, which uppercases the key
    once — Claude Code exports ``CLAUDE_PLUGIN_OPTION_MEMORY_WRITE_MODE``, and
    reading it under the key's own spelling misses *silently* (#148 cost eight
    days that way).

    A value outside :data:`WRITE_MODES` falls back to ``review``, not to the
    default. A typo in a config file must not be able to widen what gets
    written unattended, and ``review`` is the mode where nothing does.
    """
    raw = option("memory_write_mode", DEFAULT_WRITE_MODE).strip().lower()
    return raw if raw in WRITE_MODES else "review"


# --- the rubric -------------------------------------------------------------

# P2 deliberately exported no ordering: ranking is a judgement about what may be
# written unreviewed, and it belongs to whatever makes that decision. This is
# that decision. Higher means *more* caution, and reconciliation takes the max
# of the extractor's label and the judge's, so a disagreement always resolves
# toward the more conservative reading.
#
# Only the tier boundaries are policy. CORRECTION and DECLARATION are one tier
# because both come from the user; EMPIRICAL and RESEARCH are one tier because
# both are re-verifiable without asking them; INFERENCE stands alone because
# nothing verified it at all.
PROVENANCE_CAUTION: dict[str, int] = {
    "CORRECTION": 0,
    "DECLARATION": 0,
    "EMPIRICAL": 1,
    "RESEARCH": 1,
    "INFERENCE": 2,
}

# Blast radius, least to most. A wrong FACT is noticed when it contradicts
# something; a wrong RULE changes what gets noticed at all.
KIND_BLAST: dict[str, int] = {
    "FACT": 0,
    "DECISION": 1,
    "INTENTION": 2,
    "RULE": 3,
}

# `taxonomy.LEGACY_TYPE_MAP` maps `RULE-PROPOSAL` to `(None, "RULE")` — a
# genuinely absent provenance, which P2 refused to substitute a default for
# because the old vocabulary never recorded one. The substitution is the
# consumer's to make, so it is made here, explicitly: a missing provenance reads
# as INFERENCE. Note this changes no outcome — every INFERENCE cell of the
# rubric is already `review` — which is exactly why it is the safe reading.
UNLABELLED_PROVENANCE = "INFERENCE"
# And a missing kind reads as RULE, matching `taxonomy.UNCLEAR_KIND`: the widest
# blast radius, so an unlabelled item lands in front of a human. Every proposal
# drafted before the taxonomy existed has no pair at all, and the 921 KB corpus
# is unlabelled by design — an absent pair is a legitimate state, not an error.
UNLABELLED_KIND = taxonomy.UNCLEAR_KIND


def rubric_verdict(provenance: Optional[str], kind: Optional[str]) -> str:
    """``"apply"`` or ``"review"`` for a ``(provenance, kind)`` pair.

    Nine cells, no model. ``apply`` here is **permission**, not a decision: the
    judge still has to affirm it and the floor still has to allow it.
    """
    p = (provenance or UNLABELLED_PROVENANCE).upper()
    k = (kind or UNLABELLED_KIND).upper()
    if k in ("RULE", "INTENTION"):
        return "review"
    if PROVENANCE_CAUTION.get(p, 2) == 0:
        return "apply"  # from the user: FACT and DECISION both clear
    if PROVENANCE_CAUTION.get(p, 2) == 1:
        return "apply" if k == "FACT" else "review"
    return "review"  # INFERENCE, or anything unrecognised


def rubric_reason(provenance: Optional[str], kind: Optional[str]) -> str:
    """The reason code explaining a ``review`` from :func:`rubric_verdict`."""
    k = (kind or UNLABELLED_KIND).upper()
    p = (provenance or UNLABELLED_PROVENANCE).upper()
    if k == "RULE":
        return "kind-rule"
    if PROVENANCE_CAUTION.get(p, 2) >= 2:
        return "weak-provenance"
    return "rubric-review"


def reconciled_pair(item, verdict) -> tuple[str, str, bool]:
    """The pair to judge *item* by: the more conservative of the two readings.

    The extractor saw the whole session; the judge sees the item as it will
    land. Neither is authoritative alone, so each half is taken from whichever
    source is more cautious about it, and the disagreement is reported so it can
    be counted rather than silently absorbed.
    """
    ext_p = (getattr(item, "provenance", "") or "").upper()
    ext_k = (getattr(item, "kind", "") or "").upper()
    jud_p = (getattr(verdict, "provenance", "") or "").upper() if verdict else ""
    jud_k = (getattr(verdict, "kind", "") or "").upper() if verdict else ""

    def _pick(a: str, b: str, ranks: Mapping[str, int]) -> str:
        if not a:
            return b
        if not b:
            return a
        return a if ranks.get(a, 99) >= ranks.get(b, 99) else b

    provenance = _pick(ext_p, jud_p, PROVENANCE_CAUTION)
    kind = _pick(ext_k, jud_k, KIND_BLAST)
    disagreed = bool(
        (ext_p and jud_p and ext_p != jud_p) or (ext_k and jud_k and ext_k != jud_k)
    )
    return provenance, kind, disagreed


# --- reason codes -----------------------------------------------------------

# Ordered by how much they should worry a reader. An item can trip several;
# `Item.reasons` keeps all of them in this order, and the first is the bucket it
# appears under.
REASON_LABELS = {
    # The code floor. These are refusals, never judgements about content.
    "shared-bank-write": (
        "belongs to a shared memory bank — it leaves as a pull request, never "
        "as a local write"
    ),
    "unsafe-target": "target filename is not a plain memory filename",
    "reserved-filename": "targets a reserved instruction file (CLAUDE.md / AGENTS.md)",
    "not-additive": "revises or replaces existing memory",
    "unparsed": "block did not parse — no change verb, or no text",
    # The judge.
    "redundant": "already stated in the target file",
    "judge-drop": "the judge says it does not belong in memory",
    "citation-unsupported": "the cited source does not support the claim",
    "judge-doubt": "the judge escalated it for a human despite the rubric",
    "unjudged": "no model verdict — the judge was unavailable or its batch failed",
    # The rubric, from the provenance/kind pair.
    "kind-rule": "a standing rule — never applied without you reading it",
    "weak-provenance": "inferred or unlabelled — nobody confirmed it",
    "rubric-review": "its provenance/kind pair does not clear the auto-apply rubric",
    # Evidence the judge is shown. Not a veto: an explicit judge `apply` clears
    # it, because the judge was told the gate had fired and applied anyway.
    "routing-warning": "flagged by the routing gate",
}

# Above this many rejections the receipt shows grouped counts instead of every
# item. A 200-line rejection list recreates exactly the review fatigue this
# whole programme exists to remove.
REJECTION_DETAIL_LIMIT = 25


@dataclass(frozen=True)
class Item:
    """One ``### N.`` entry under a ``## Updates for `file` `` group."""

    target: str
    number: int
    title: str
    section: str
    change: str
    text: str
    source: str
    reasons: tuple[str, ...]
    # The two-axis taxonomy (lib/taxonomy.py), carried from the learning this
    # item came from. Empty for items with no pair — every proposal drafted
    # before the taxonomy shipped, and any half the drafter could not read off
    # its source. The rubric reads an absent half as its most cautious value.
    provenance: str = ""
    kind: str = ""
    # Did the routing gate name this item? Evidence for the judge, not a veto.
    routing_flagged: bool = False
    # What the rubric alone permits, before the judge and before the floor.
    rubric: str = "review"
    # The judge's one-line English, carried so a receipt can say *why* without
    # the reader going back to the proposal.
    judge_reason: str = ""

    @property
    def auto(self) -> bool:
        return not self.reasons

    @property
    def pair(self) -> str:
        """``PROVENANCE/KIND`` for display, or ``""`` when neither is known."""
        if not (self.provenance or self.kind):
            return ""
        return f"{self.provenance or '?'}/{self.kind or '?'}"

    @property
    def label(self) -> str:
        return f"`{self.target}` #{self.number}"


@dataclass(frozen=True)
class Triage:
    """The partition of a proposal's pending items.

    ``auto`` is **empty until** :func:`apply_verdicts` has run: :func:`classify`
    computes what the rubric permits, and permission is not a decision. That is
    the fail-closed property stated structurally — a caller that forgets to
    judge, or whose judging failed entirely, gets nothing to apply rather than
    getting the rubric's permissive cells.
    """

    auto: tuple[Item, ...]
    review: tuple[Item, ...]
    # Conflict Resolutions live under their own heading and revise lines that
    # already exist, so they are never auto-appliable and never parsed as
    # update entries. Counted here only so the caller can tell the reviewer
    # they are still waiting.
    conflict_resolutions: int
    dropped: tuple[Item, ...] = ()
    judged: bool = False
    # How many items kept their conservative default because their judge batch
    # failed, timed out, or was never made. Contract C4 requires this be
    # visible: a silent fallback is indistinguishable from a working judge.
    unjudged: int = 0

    @property
    def total(self) -> int:
        return len(self.auto) + len(self.review) + len(self.dropped)

    def auto_by_file(self) -> dict[str, list[Item]]:
        return _group(self.auto)

    def review_by_reason(self) -> dict[str, list[Item]]:
        out: dict[str, list[Item]] = {}
        for item in self.review:
            # Bucket by the first (most significant) reason so an item appears
            # once; `item.reasons` still carries the rest for the detail line.
            out.setdefault(item.reasons[0] if item.reasons else "unjudged", []).append(item)
        return out

    def dropped_by_reason(self) -> dict[str, list[Item]]:
        out: dict[str, list[Item]] = {}
        for item in self.dropped:
            out.setdefault(item.reasons[0] if item.reasons else "judge-drop", []).append(item)
        return out


def _group(items) -> dict[str, list[Item]]:
    """Group items by the memory **filename** they will be written to.

    Keyed on the resolved filename, not the raw target. ``personal/dev.md`` is a
    spelling the write floor deliberately accepts — a model shown bank-qualified
    refs will sometimes qualify the personal one too — but keying on the raw
    string sent the applier looking for ``<memory>/personal/dev.md``, which does
    not exist, so the item was silently dropped after the floor had said yes.
    Two spellings of one file also grouped as two files.
    """
    out: dict[str, list[Item]] = {}
    for item in items:
        _bank, filename = target_bank(item.target)
        out.setdefault(filename or item.target, []).append(item)
    return out


def has_routing_section(proposal: str) -> bool:
    """Did the routing gate actually run on this proposal?

    ``dream.py``'s ``_with_routing_warnings`` is fail-open: on any exception it
    returns the proposal with no warnings section at all. An absent section is
    therefore indistinguishable from a clean one to
    :func:`flagged_by_routing`, and treating it as clean would hide the gate's
    evidence from the judge on exactly the proposals where it matters. The
    renderer always writes ``(none)`` when it finds nothing, so "clean" and
    "never ran" *are* distinguishable — but only if somebody checks, which is
    what this exists for.
    """
    return any(
        line.startswith("## Routing Warnings") for line in proposal.splitlines()
    )


def flagged_by_routing(proposal: str) -> set[tuple[str, int]]:
    """``(target, number)`` pairs named in the ``## Routing Warnings`` section.

    Reads the rendered section rather than re-running the validator: the
    section is what the reviewer sees and what dream.py already wrote, so a
    mismatch between the two is impossible by construction.
    """
    flagged: set[tuple[str, int]] = set()
    in_section = False
    for line in proposal.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## Routing Warnings")
            continue
        if not in_section:
            continue
        m = _WARNING_LABEL_RE.match(line)
        if m:
            flagged.add((m.group("target"), int(m.group("number"))))
    return flagged


def _count_conflict_resolutions(proposal: str) -> int:
    count = 0
    in_section = False
    for line in proposal.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## Conflict Resolutions")
            continue
        if in_section and line.startswith("### "):
            count += 1
    return count


def classify(proposal: str) -> Triage:
    """Partition *proposal*'s pending items **before** any model has seen them.

    Model-free by construction: this is the rubric and the floor, nothing else.
    Every item lands in ``review`` — including the ones the rubric permits,
    which carry the reason ``unjudged`` until :func:`apply_verdicts` replaces it
    with a verdict. So a caller that never judges applies nothing.

    Items already under ``## Processed`` are excluded: the entry parser only
    reads blocks under a ``## Updates for`` heading, so triaging a
    partly-reviewed proposal is safe and idempotent.
    """
    flagged = flagged_by_routing(proposal)
    review: list[Item] = []

    for entry in parse_proposal_entries(proposal):
        try:
            number = int(entry["number"])
        except (TypeError, ValueError):
            continue  # not an `### N.` update entry; nothing to decide

        item = Item(
            target=entry["target"],
            number=number,
            title=entry["title"],
            section=entry["section"],
            change=entry["change"],
            text=entry["text"],
            source=entry.get("source", ""),
            reasons=(),
            provenance=entry.get("provenance", ""),
            kind=entry.get("kind", ""),
            routing_flagged=(entry["target"], number) in flagged,
        )
        rubric = rubric_verdict(item.provenance, item.kind)
        reasons: list[str] = []
        refusal = floor_check(item)
        if refusal:
            reasons.append(refusal)
        if rubric != "apply":
            reasons.append(rubric_reason(item.provenance, item.kind))
        if item.routing_flagged:
            reasons.append("routing-warning")
        if not reasons:
            reasons.append("unjudged")
        review.append(replace(item, reasons=tuple(reasons), rubric=rubric))

    return Triage(
        auto=(),
        review=tuple(review),
        conflict_resolutions=_count_conflict_resolutions(proposal),
        judged=False,
        unjudged=len(review),
    )


def apply_verdicts(
    triage: Triage,
    verdicts: Mapping[tuple[str, int], object],
    *,
    mode: str = DEFAULT_WRITE_MODE,
) -> Triage:
    """Fold the judge's verdicts into *triage*. Pure — no model, no I/O.

    The only-lower rule, stated once: an item is applied when **the rubric
    permits it and the judge affirms it and the floor allows it**. A judge
    ``apply`` on an item the rubric refused therefore changes nothing, which is
    what makes ``kind: RULE`` unreachable from a prompt. A judge ``review`` or
    ``drop`` always takes effect, in either direction of the rubric.

    Verdicts for a ``(target, number)`` that is not in the proposal are ignored.
    Items with no verdict at all keep ``unjudged`` and stay in ``review`` —
    which is how a failed batch, an unparseable reply and an absent SDK all
    produce the same partition ``review`` mode produces.

    ``mode`` widens exactly one thing: in ``auto``, an item whose *rubric*
    reason was its provenance still applies when its kind is ``FACT``. It never
    widens past ``kind: RULE``, never past the judge's own escalation, and never
    past the floor.
    """
    auto: list[Item] = []
    review: list[Item] = []
    dropped: list[Item] = []
    unjudged = 0

    for item in list(triage.auto) + list(triage.review) + list(triage.dropped):
        verdict = verdicts.get((item.target, item.number))
        outcome, reasons, judge_reason = _decide(item, verdict, mode=mode)
        if verdict is None:
            unjudged += 1
        provenance, kind, _ = reconciled_pair(item, verdict)
        decided = replace(
            item,
            reasons=tuple(reasons),
            provenance=provenance or item.provenance,
            kind=kind or item.kind,
            judge_reason=judge_reason,
        )
        if outcome == "apply":
            auto.append(decided)
        elif outcome == "drop":
            dropped.append(decided)
        else:
            review.append(decided)

    return Triage(
        auto=tuple(auto),
        review=tuple(review),
        conflict_resolutions=triage.conflict_resolutions,
        dropped=tuple(dropped),
        judged=True,
        unjudged=unjudged,
    )


def _decide(item: Item, verdict, *, mode: str) -> tuple[str, list[str], str]:
    """``(outcome, reasons, judge_reason)`` for one item. The whole policy."""
    provenance, kind, _ = reconciled_pair(item, verdict)
    rubric = rubric_verdict(provenance, kind)
    judge_reason = (getattr(verdict, "reason", "") or "") if verdict else ""
    reasons: list[str] = []

    # 1. The judge's own escalations win outright, in either direction of the
    #    rubric. Redundancy is checked before the verdict word because a judge
    #    that reports `redundant=yes` and then says `apply` has contradicted
    #    itself, and the conservative half of a contradiction is the answer.
    if verdict is not None and getattr(verdict, "redundant", False):
        return "drop", ["redundant"], judge_reason
    if verdict is not None and getattr(verdict, "verdict", "") == "drop":
        return "drop", ["judge-drop"], judge_reason

    # 2. The floor. Recorded whatever the outcome, enforced below.
    refusal = floor_check(item)
    if refusal:
        reasons.append(refusal)

    # 3. No verdict — a failed batch, an unparseable reply, or no SDK at all.
    if verdict is None:
        if rubric != "apply":
            reasons.append(rubric_reason(provenance, kind))
        if item.routing_flagged:
            reasons.append("routing-warning")
        if not reasons:
            reasons.append("unjudged")
        return "review", reasons, judge_reason

    # 4. The judge escalated.
    if getattr(verdict, "verdict", "") != "apply":
        reasons.append("judge-doubt")
        if item.routing_flagged:
            reasons.append("routing-warning")
        return "review", reasons, judge_reason

    # 5. The judge affirmed. The rubric now has to permit it.
    permitted = rubric == "apply" or (mode == "auto" and (kind or "").upper() == "FACT")
    if not permitted:
        reasons.append(rubric_reason(provenance, kind))
        return "review", reasons, judge_reason

    # 6. The one cell the rubric conditions on the judge's citation check.
    if (
        (kind or "").upper() == "FACT"
        and PROVENANCE_CAUTION.get((provenance or "").upper(), 2) == 1
        and getattr(verdict, "citation", "none") != "supported"
    ):
        reasons.append("citation-unsupported")
        return "review", reasons, judge_reason

    # 7. The floor is the last word, and it can only refuse.
    if refusal:
        return "review", reasons, judge_reason
    return "apply", [], judge_reason


def shared_bank_items(triage: Triage) -> tuple[Item, ...]:
    """Items the floor refused because they target a shared bank.

    These are not rejections. They are the items that should leave as a pull
    request on the bank (``lib/bank_proposals``), and they are read off the
    same partition every other outcome is read off — so an item can be
    *either* applied locally *or* proposed to a bank, never both, and never
    neither by accident.

    Read from ``review`` only. The floor puts a refused item there, whereas
    ``dropped`` means the judge said something about the *content* — and a
    dropped item must not become a pull request on somebody else's repo just
    because it also happened to be bank-bound.
    """
    return tuple(
        item for item in triage.review if "shared-bank-write" in item.reasons
    )


def auto_slice(items: list[Item]) -> str:
    """Render the auto-appliable items for one file as applier instructions.

    The applier is handed a *rebuilt* section rather than the proposal's own
    text, because the proposal's section still contains the review items and an
    applier given those would write them. Rebuilding from the parsed fields is
    the only form where "what was sent to the applier" and "what the receipt
    claims was applied" cannot diverge.
    """
    if not items:
        return ""
    target = items[0].target
    lines = [f"## Updates for `{target}`", ""]
    for item in items:
        lines.append(f"### {item.number}. {item.title}")
        if item.section:
            lines.append(f"**Section:** {item.section}")
        lines.append(f"**Change:** {item.change}")
        if item.pair:
            lines.append(f"**Provenance:** {item.pair}")
        lines.append("")
        for text_line in item.text.splitlines():
            lines.append(f"> {text_line}")
        if item.source:
            lines.append("")
            lines.append(f"**Source:** {item.source}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_item_detail(out: list[str], item: Item, *, drop_reason: str = "") -> None:
    out.append(f"### {item.number}. {item.title}")
    if item.section:
        out.append(f"**Section:** {item.section}")
    if item.pair:
        out.append(f"**Provenance:** {item.pair}")
    if drop_reason:
        out.append(f"**Dropped:** {REASON_LABELS.get(drop_reason, drop_reason)}")
    if item.judge_reason:
        out.append(f"**Judge:** {item.judge_reason}")
    out.append("")
    for text_line in item.text.splitlines():
        out.append(f"> {text_line}")
    out.append("")
    out.append(f"**Source:** {item.source or '(none recorded)'}")
    out.append("")


def render_receipt(
    triage: Triage,
    *,
    proposal_name: str,
    applied: dict[str, list[Item]],
    failed: dict[str, str],
    generated: str,
    mode: str = DEFAULT_WRITE_MODE,
    rejections_log: str = "",
) -> str:
    """The audit trail for everything a model decided without a human reading it.

    This file is the other half of the bargain. Items skip review only because
    every one of them is written down here — target, section, text, source, the
    labels it was judged under and the judge's own sentence — next to a memory
    directory under git. Without the receipt this is silent mutation; with it,
    it is a reviewable commit and a one-command revert.

    The ``Rejected`` section is not decoration. Auditing refusals is how a judge
    earns the delegation: a pass that only reports what it wrote is
    indistinguishable from one that quietly discards good items.
    """
    total_applied = sum(len(v) for v in applied.values())
    out = [
        f"# Dream triage receipt — {generated}",
        "",
        f"**Proposal:** `{proposal_name}`",
        f"**Mode:** `{mode}`",
        f"**Applied:** {total_applied} item(s) across {len(applied)} file(s)",
        f"**Rejected (dropped):** {len(triage.dropped)} item(s)",
        f"**Left for review:** {len(triage.review)} item(s)",
        f"**Kept a conservative default because no verdict arrived:** "
        f"{triage.unjudged} item(s)",
        "",
        "Every item under **Applied** was classified by a **model** and written "
        "**without a human reading it**. The containment is this receipt plus git: "
        "`git -C .multiplai/memory log -1` names the commit and "
        "`git -C .multiplai/memory revert <sha>` undoes the whole batch.",
        "",
    ]
    if rejections_log:
        out += [f"**Rejection log:** `{rejections_log}`", ""]

    if failed:
        out += [
            "## Failed to apply",
            "",
            "These cleared every check but the applier did not produce a safe "
            "result, so **nothing was written** for them and their items stay "
            "pending in the proposal.",
            "",
        ]
        out += [f"- `{name}` — {reason}" for name, reason in sorted(failed.items())]
        out.append("")

    out += ["## Applied", ""]
    if not applied:
        out += ["(nothing)", ""]
    for target in sorted(applied):
        items = applied[target]
        out += [f"### `{target}` — {len(items)} item(s)", ""]
        for item in items:
            _render_item_detail(out, item)

    out += ["## Rejected", ""]
    if not triage.dropped:
        out += ["(nothing dropped)", ""]
    elif len(triage.dropped) <= REJECTION_DETAIL_LIMIT:
        out += [
            "Dropped means **not promoted to memory** — the source learning is "
            "untouched and every one of these is in the rejection log, so a drop "
            "can be read back and overruled.",
            "",
        ]
        for item in triage.dropped:
            _render_item_detail(out, item, drop_reason=item.reasons[0] if item.reasons else "")
    else:
        out += [
            f"{len(triage.dropped)} items were dropped — too many to read here "
            f"(the limit is {REJECTION_DETAIL_LIMIT}; a list this long recreates "
            "the review fatigue triage exists to remove). Grouped by reason; every "
            "one is in the rejection log in full.",
            "",
        ]
        for reason, items in sorted(triage.dropped_by_reason().items()):
            label = REASON_LABELS.get(reason, reason)
            out.append(f"- **{label}** ({len(items)})")
        out.append("")

    if triage.review:
        out += [
            "## Left for you",
            "",
            "Not applied — these are in the proposal, still pending:",
            "",
        ]
        for reason, items in sorted(triage.review_by_reason().items()):
            label = REASON_LABELS.get(reason, reason)
            refs = ", ".join(item.label for item in items)
            out.append(f"- **{label}** ({len(items)}): {refs}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_summary(
    triage: Triage,
    *,
    applied_count: int,
    receipt_path: str,
    dry_run: bool = False,
    mode: str = DEFAULT_WRITE_MODE,
) -> str:
    """The short console block the reviewing skill shows instead of 190 items."""
    heading = "WOULD APPLY" if dry_run else "APPLIED"
    lines = [f"mode: {mode}"]
    lines.append(f"{heading} ({applied_count})  → receipt: {receipt_path}")
    for target, items in sorted(triage.auto_by_file().items()):
        lines.append(f"    {target:<28} {len(items)}")
    lines.append("")
    lines.append(f"DROPPED ({len(triage.dropped)})")
    for reason, items in sorted(triage.dropped_by_reason().items()):
        label = REASON_LABELS.get(reason, reason)
        refs = ", ".join(i.label for i in items[:6])
        more = f", +{len(items) - 6} more" if len(items) > 6 else ""
        lines.append(f"    {label}: {refs}{more}")
    lines.append("")
    lines.append(f"NEEDS YOU ({len(triage.review)})")
    for reason, items in sorted(triage.review_by_reason().items()):
        label = REASON_LABELS.get(reason, reason)
        # Qualified refs, not bare numbers: item numbering restarts per file,
        # so a bare "#19, #19" names two different items and tells the reader
        # nothing about where to look.
        refs = ", ".join(i.label for i in items[:6])
        more = f", +{len(items) - 6} more" if len(items) > 6 else ""
        lines.append(f"    {label}: {refs}{more}")
    if triage.conflict_resolutions:
        lines.append(
            f"    conflict resolutions: {triage.conflict_resolutions} "
            f"(own section — always reviewed)"
        )
    if triage.unjudged:
        lines.append("")
        lines.append(
            f"    {triage.unjudged} item(s) kept a conservative default because no "
            f"judge verdict arrived."
        )
    return "\n".join(lines)


def rejection_records(triage: Triage, *, proposal_name: str, key_of=None) -> list[dict]:
    """Rejection-log records for every dropped item. Import-light helper.

    ``key_of`` computes an item's content hash (``memory_judge.item_key``); it
    is injected rather than imported so this module stays free of the judge.
    """
    from lib import rejections

    return [
        rejections.record_for(
            item,
            proposal=proposal_name,
            reason=item.reasons[0] if item.reasons else "judge-drop",
            judge_reason=item.judge_reason,
            item_key=key_of(item) if key_of else "",
        )
        for item in triage.dropped
    ]
