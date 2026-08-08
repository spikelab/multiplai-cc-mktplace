"""Split a dream proposal into what a human must read and what need not be read.

A proposal is not too long to *review*; it is too long to review **item by
item**. At ~190 items across 14 memory files, the review costs a session's
whole context window and gets abandoned partway — which is why the pending
backlog reached 380 bullets rather than shrinking. Meanwhile the great majority
of those items are the same shape: append one factual bullet to a section that
already exists, in a file that is not a behavioural-rule file, cited to a
learnings line, flagged by nothing.

Triage separates the two populations **deterministically** — no model call, no
judgement — so the human's attention goes only where a mistake would actually
cost something. An item is auto-appliable only when *all* of these hold:

- it does not change how the agent behaves: not a ``[RULE-PROPOSAL]``, not
  phrased as a standing instruction (``_NORMATIVE_RE``), and landing in a file
  that records rather than instructs (``RECALL_FILES``);
- it *appends*. An ``update`` can destroy a line that was right; an ``add``
  cannot;
- its target is a plain memory filename — a model-authored ``../../CLAUDE.md``
  is not a memory file however much it looks like one;
- no gate doubts it: not flagged by routing, not marked low-confidence by the
  drafter, and parsed cleanly with actual text to insert.

"Auto" is not "unreviewed": every applied item is written to a receipt naming
its target, section, text and source citation, and memory lives in git — the
auditable-and-revertible pair is what makes the default-apply safe. The
reviewer reads a 20-item list instead of a 190-item one, and the ones they read
are the ones that matter.

Every gate is **pessimistic**, and the two directions are not symmetric: a false
"needs review" costs one line of reading, a false "auto" writes something nobody
agreed to. So the file check is an allowlist rather than a denylist, the
normative-language check accepts false positives, and anything that does not
parse the way the format promises goes to the human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from lib.routing_validation import parse_proposal_entries

# The drafter emits both `[RULE-PROPOSAL]` and `**[RULE-PROPOSAL]**`, in the
# title or in the body. Match the bare token anywhere in the block.
_RULE_PROPOSAL_RE = re.compile(r"\[RULE-PROPOSAL\]", re.IGNORECASE)
_LOW_CONFIDENCE_RE = re.compile(r"\[warning:?\s*low confidence\]", re.IGNORECASE)
# `- \`file.md\` #3 (title): …` — the label routing_validation renders per
# warning. We only need the (file, number) pair to match warnings to items.
_WARNING_LABEL_RE = re.compile(r"^-\s+`(?P<target>[^`]+)`\s+#(?P<number>\d+)\b")

# Auto-apply targets an **allowlist of recall files**, not a denylist of
# behavioural ones. The first version of this module denied only `CLAUDE.md`,
# and running it against the real 2026-08-05 proposal auto-applied 8 items into
# `git-policy.md`, 3 into `preferences.md` and 2 more into `technical-pref.md`
# and `prompt-eng-guide.md` — files whose own headers say "policy" and
# "principles ... that should always be applied". Standing rules, written with
# nobody's consent, by the very feature whose stated policy is that a
# behavioural change is the human's call.
#
# The direction is the whole point. A denylist fails open: a memory file added
# next month is auto-appliable until someone remembers to classify it. An
# allowlist fails closed — the new file's items wait for a human, which costs a
# few lines of reading once.
#
# Membership test: does this file *record* something (a project's state, a
# person's history, a technical gotcha), or does it *instruct* — voice guides,
# preference and policy files, anything the agent reads to decide how to act?
RECALL_FILES = frozenset({
    # people, projects, money — pure record
    "career-history.md", "dolcebot.md", "dolcedata.md", "dolcesim.md",
    "finances.md", "life.md", "me.md", "multiplai.md", "personal-projects.md",
    "taxes-italy.md", "career-strategy.md",
    # technical knowledge: gotchas, benchmarks, platform behaviour. Normative
    # sentences do turn up in these, which is what `_NORMATIVE_RE` below is for
    # — the file being on this list is necessary, never sufficient.
    "ai-agent-patterns.md", "apple.md", "audiovideo.md", "claude-code-tools.md",
    "dev.md", "infra-patterns.md", "python.md",
})

# An item can carry a rule into a recall file — "always stage with a pathspec",
# "never use bare git add" — and a file-level gate alone would wave it through.
# So the *text* is checked too: anything phrased as a standing instruction goes
# to the human wherever it lands.
#
# Deliberately trigger-happy. These words appear in plenty of harmless factual
# sentences ("the API always returns UTF-8"), and every false positive costs
# one line of reading, while a false negative writes a rule nobody approved.
_NORMATIVE_RE = re.compile(
    r"\b("
    r"always|never|must|should|shall|"
    r"don'?t|do not|avoid|refuse|forbidden|required|mandatory|"
    r"prefer|instead of|rather than|"
    r"make sure|ensure that|remember to|be sure to"
    r")\b",
    re.IGNORECASE,
)

# The only change verb that cannot destroy existing memory. `update`/`replace`
# rewrite a line that may well have been correct; an empty or unrecognised verb
# means the block did not parse the way the format promises, and an unparsed
# item is not one to apply unattended.
ADDITIVE_CHANGES = frozenset({"add"})

# Reason codes, ordered by how much they should worry a reader. An item can
# trip several; `Item.reasons` keeps all of them, and the receipt shows the set.
REASON_LABELS = {
    "rule-proposal": "changes a behavioural rule",
    "not-recall-file": "targets a file that instructs rather than records",
    "normative-language": "reads as a standing instruction, not a fact",
    "unsafe-target": "target filename is not a plain memory filename",
    "routing-warning": "flagged by the routing gate",
    "low-confidence": "drafter marked it low confidence",
    "not-additive": "revises or replaces existing memory",
    "unparsed": "block did not parse — no change verb, or no text",
}


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
    # its source. **Nothing here classifies on them**: `classify` is unchanged,
    # and the pair rides along so the receipt can say where an unreviewed line
    # came from and so a later classifier has the fields to work with.
    provenance: str = ""
    kind: str = ""

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
    auto: tuple[Item, ...]
    review: tuple[Item, ...]
    # Conflict Resolutions live under their own heading and revise lines that
    # already exist, so they are never auto-appliable and never parsed as
    # update entries. Counted here only so the caller can tell the reviewer
    # they are still waiting.
    conflict_resolutions: int

    @property
    def total(self) -> int:
        return len(self.auto) + len(self.review)

    def auto_by_file(self) -> dict[str, list[Item]]:
        return _group(self.auto)

    def review_by_reason(self) -> dict[str, list[Item]]:
        out: dict[str, list[Item]] = {}
        for item in self.review:
            # Bucket by the first (most significant) reason so an item appears
            # once; `item.reasons` still carries the rest for the detail line.
            out.setdefault(item.reasons[0], []).append(item)
        return out


def _group(items) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {}
    for item in items:
        out.setdefault(item.target, []).append(item)
    return out


_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")


def is_safe_target(filename: str) -> bool:
    """Is *filename* a plain memory filename, safe to join onto memory_dir?

    The target comes from a ``## Updates for `x` `` heading written by a model,
    captured as ``[^`]+`` — so it is arbitrary text, and ``memory_dir / target``
    happily resolves ``../../CLAUDE.md`` to the workspace file. The existing
    ``.exists()`` check is no guard at all: the interesting traversal targets
    are precisely the files that exist.

    A basename ending in ``.md``, with no separators and no ``..``. Anything
    else is not a memory file, whatever it claims.
    """
    return (
        bool(_SAFE_TARGET_RE.match(filename))
        and ".." not in filename
        and filename == PurePosixPath(filename).name
    )


def has_routing_section(proposal: str) -> bool:
    """Did the routing gate actually run on this proposal?

    ``dream.py``'s ``_with_routing_warnings`` is fail-open: on any exception it
    returns the proposal with no warnings section at all. An absent section is
    therefore indistinguishable from a clean one to
    :func:`flagged_by_routing`, and treating it as clean would auto-apply every
    item the gate would have flagged — 6 of them in the run this module was
    built against. The renderer always writes ``(none)`` when it finds nothing,
    so "clean" and "never ran" *are* distinguishable — but only if somebody
    checks, which is what this exists for.
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
    """Partition *proposal*'s pending update items into auto-apply and review.

    Items already under ``## Processed`` are excluded: the entry parser only
    reads blocks under a ``## Updates for`` heading, and processed blocks sit
    under ``## Processed``. So triaging a partly-reviewed proposal is safe and
    idempotent — it never re-proposes a decided item.
    """
    flagged = flagged_by_routing(proposal)
    auto: list[Item] = []
    review: list[Item] = []

    for entry in parse_proposal_entries(proposal):
        try:
            number = int(entry["number"])
        except (TypeError, ValueError):
            continue  # not an `### N.` update entry; nothing to decide
        blob = f"{entry['title']}\n{entry['text']}"
        reasons: list[str] = []

        if _RULE_PROPOSAL_RE.search(blob):
            reasons.append("rule-proposal")
        if not is_safe_target(entry["target"]):
            reasons.append("unsafe-target")
        elif entry["target"] not in RECALL_FILES:
            reasons.append("not-recall-file")
        if _NORMATIVE_RE.search(blob):
            reasons.append("normative-language")
        if (entry["target"], number) in flagged:
            reasons.append("routing-warning")
        if _LOW_CONFIDENCE_RE.search(blob):
            reasons.append("low-confidence")
        if not entry["change"]:
            reasons.append("unparsed")
        elif entry["change"] not in ADDITIVE_CHANGES:
            reasons.append("not-additive")
        # A block with a change verb but no quoted body parses "successfully"
        # into an empty text. The applier would then be handed a title and told
        # to apply it, under a prompt that says not to invent — best case a
        # no-op, realistic case a bullet it composes from the title, which is
        # unreviewed, unsourced, and absent from the receipt as well.
        if not entry["text"].strip() and "unparsed" not in reasons:
            reasons.append("unparsed")

        item = Item(
            target=entry["target"],
            number=number,
            title=entry["title"],
            section=entry["section"],
            change=entry["change"],
            text=entry["text"],
            source=entry.get("source", ""),
            reasons=tuple(reasons),
            provenance=entry.get("provenance", ""),
            kind=entry.get("kind", ""),
        )
        (review if reasons else auto).append(item)

    return Triage(
        auto=tuple(auto),
        review=tuple(review),
        conflict_resolutions=_count_conflict_resolutions(proposal),
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


def render_receipt(
    triage: Triage,
    *,
    proposal_name: str,
    applied: dict[str, list[Item]],
    failed: dict[str, str],
    generated: str,
) -> str:
    """The audit trail for everything applied without a human reading it.

    This file is the other half of the bargain: items skip review only because
    every one of them is written down here with its target, section, text and
    source, next to a memory directory under git. Without the receipt this is
    silent mutation; with it, it is a reviewable commit.
    """
    total_applied = sum(len(v) for v in applied.values())
    out = [
        f"# Dream auto-apply receipt — {generated}",
        "",
        f"**Proposal:** `{proposal_name}`",
        f"**Auto-applied:** {total_applied} item(s) across {len(applied)} file(s)",
        f"**Left for review:** {len(triage.review)} item(s)",
        "",
        "Every item below was applied **without human review**, because it is an "
        "additive entry to a non-behavioural memory file that no gate flagged. "
        "Memory is under git: `git -C .multiplai/memory diff` shows exactly what "
        "changed, and reverting is a `git checkout`.",
        "",
    ]
    if failed:
        out += [
            "## Failed to apply",
            "",
            "These were classified auto-appliable but the applier did not produce a "
            "safe result, so **nothing was written** for them and their items stay "
            "pending in the proposal.",
            "",
        ]
        out += [f"- `{name}` — {reason}" for name, reason in sorted(failed.items())]
        out.append("")

    for target in sorted(applied):
        items = applied[target]
        out += [f"## `{target}` — {len(items)} item(s)", ""]
        for item in items:
            out.append(f"### {item.number}. {item.title}")
            if item.section:
                out.append(f"**Section:** {item.section}")
            if item.pair:
                out.append(f"**Provenance:** {item.pair}")
            out.append("")
            for text_line in item.text.splitlines():
                out.append(f"> {text_line}")
            # Provenance is the part that makes an unreviewed line traceable
            # back to the session that produced it.
            out.append("")
            out.append(f"**Source:** {item.source or '(none recorded)'}")
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
    triage: Triage, *, applied_count: int, receipt_path: str, dry_run: bool = False
) -> str:
    """The short console block the reviewing skill shows instead of 190 items."""
    heading = "WOULD AUTO-APPLY" if dry_run else "AUTO-APPLIED"
    lines = [f"{heading} ({applied_count})  → receipt: {receipt_path}"]
    for target, items in sorted(triage.auto_by_file().items()):
        lines.append(f"    {target:<28} {len(items)}")
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
    return "\n".join(lines)
