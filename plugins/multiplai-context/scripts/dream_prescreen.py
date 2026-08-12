#!/usr/bin/env python3
"""Show the reviewer which pending items already exist somewhere in the corpus.

This is a **lens, not a gate.** The gate is ``lib.routing_validation``, which
runs at draft time and writes ``## Routing Warnings`` into the proposal; it
screens the personal memory files, the always-loaded ``CLAUDE.md`` files and
the shared memory banks (``lib.memory_corpus``), for near-verbatim restatements
(8-gram) *and*, since 0.47.0, for reworded ones at line level — the same
measure this script uses.

So the overlap with the gate is now deliberate, and what is left for review
time is what the gate could not know: the corpus has changed since the proposal
was drafted (a long backlog is triaged over days, and each applied item becomes
new corpus), the threshold wants moving for one pass, or the reviewer wants the
bodies and neighbouring lines rather than one warning line. Same measure either
way — ``lib.conflict_edits.overlap``, symmetric Jaccard over content words, at
that module's own calibrated threshold. No third tokenizer, no third threshold,
and the parsing is ``routing_validation.parse_proposal_entries``, so a heading
this script accepts is exactly one the rest of the pipeline accepts.

A hit is a **lead to verify** by opening both lines — never a verdict. So is a
miss: ``content_words`` strips code spans (they are identical boilerplate across
memory files), so a rule whose distinctive tokens live inside backticks — most
tool-usage rules — scores lower here than it reads. Measured recall against the
real backlog is roughly half; this narrows the reading, it does not replace it.

Usage::

    dream_prescreen.py <target-file.md> --proposal PATH   # one file
    dream_prescreen.py --all --proposal PATH              # every target, one run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import memory_corpus  # noqa: E402
from lib.conflict_edits import MIN_OVERLAP, content_words, overlap_sets  # noqa: E402
from lib.dream_processed import latest_pending_proposal  # noqa: E402
from lib.routing_validation import parse_proposal_entries  # noqa: E402
from multiplai_core.paths import get_paths  # noqa: E402

# Corpus lines shorter than this carry too little to score: a heading or a
# one-word bullet matches everything a bit and nothing usefully.
MIN_LINE_LEN = 40

# Why symmetric Jaccard and not a containment measure, measured rather than
# assumed. The real 602-item backlog was scored twice: against memory *after*
# it was consolidated (those items are in there — flags should be high) and
# against memory as of the commit *before* (they are not — flags are false).
#
#   threshold 0.35   Jaccard  316 true / 25 false     (12.6 : 1)
#                    coverage 523 true / 417 false     (1.3 : 1)
#
# ``|item ∩ line| / |line|`` looks like the right question — "does this long
# item already say what this short line says" — and is near-useless: any short
# line whose few content words all appear somewhere in a 60-word item scores
# 1.0. Jaccard's length penalty is the thing doing the work.

# Neighbours shown per flagged item. Two is enough to tell "one line says this"
# from "the whole section says this" without turning the report into the corpus.
NEIGHBOURS = 2


def corpus_lines(paths) -> list[tuple[str, int, str, set[str]]]:
    """Every substantive corpus line, as ``(label, lineno, text, words)``.

    The corpus is the personal memory files **plus** everything
    :mod:`lib.memory_corpus` adds — the always-loaded ``CLAUDE.md`` files and
    the shared banks. Same files the draft-time gate screens against.

    Content words are computed here, once per line, because every entry is
    scored against every line.
    """
    memory_dir = Path(paths.memory_dir)
    labelled: list[tuple[str, Path]] = [
        (f.name, f) for f in sorted(memory_dir.glob("*.md")) if f.name != "learnings.md"
    ]
    labelled += memory_corpus.claude_md_paths(paths)
    labelled += memory_corpus.bank_paths(paths)

    out: list[tuple[str, int, str, set[str]]] = []
    for label, text in memory_corpus.read_files(labelled).items():
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if len(stripped) > MIN_LINE_LEN:
                out.append((label, lineno, stripped, content_words(stripped)))
    return out


def pending_items(proposal_text: str, target: str | None) -> list[dict]:
    """Pending entries, optionally narrowed to one ``## Updates for`` target.

    Delegates to the pipeline's own parser: it tolerates a suffixed heading
    (``## Updates for `python.md` (2 items)``), skips ``### A{N}.`` action
    items, and stops at ``## Processed`` because that heading ends the group.
    """
    entries = parse_proposal_entries(proposal_text)
    if target is None:
        return entries
    return [e for e in entries if e["target"] == target]


def screen(entry: dict, lines: list[tuple[str, int, str, set[str]]], threshold: float) -> dict:
    """Score one entry against every corpus line.

    Returns ``{"scored": [(score, label, lineno, text)], "flagged": bool,
    "unscreenable": bool}``. **Unscreenable is not clean**: an entry whose
    insert text did not parse (no blockquote, or a fenced/indented body) scores
    nothing at all, and silently counting that as 0.00 is how an unchecked item
    reads as a checked one.
    """
    text = (entry.get("text") or "").strip()
    words = content_words(text)
    if not text or len(words) < 1:
        return {"scored": [], "flagged": False, "unscreenable": True}

    scored = sorted(
        (
            (overlap_sets(words, line_words), label, no, line)
            for label, no, line, line_words in lines
        ),
        key=lambda s: s[0],
        reverse=True,
    )[:NEIGHBOURS]
    flagged = bool(scored) and scored[0][0] >= threshold
    return {"scored": scored, "flagged": flagged, "unscreenable": False}


def report(entries: list[dict], lines, threshold: float, verbose: bool) -> tuple[int, int]:
    """Print the screen; return ``(flagged, unscreenable)`` counts.

    Two levels, because this runs *inside* the review whose context window it
    exists to protect. Printing every item with its body and two neighbours cost
    a measured 411,614 bytes (~103k tokens) on the 602-item backlog; one line per
    lead costs about 38 KB and still names both locations to open.

    Nothing is dropped silently: every item is screened, the counts below cover
    all of them, and ``--verbose`` prints the bodies.
    """
    flagged = unscreenable = 0
    for entry in entries:
        result = screen(entry, lines, threshold)
        head = f"{entry['target']} #{entry['number']} {entry['title'][:70]}"

        if result["unscreenable"]:
            unscreenable += 1
            print(f"UNSCREENABLE  {head}  — no insert text parsed, read it yourself")
            continue
        if result["flagged"]:
            flagged += 1
            best = result["scored"][0]
            print(f"[{best[0]:.2f}] {head}  ~  {best[1]}:{best[2]}")
        elif verbose:
            best_score = result["scored"][0][0] if result["scored"] else 0.0
            print(f"[{best_score:.2f}] {head}")
        else:
            continue

        if verbose:
            print(f"    {(entry.get('text') or '')[:240]}")
            for score, name, no, text in result["scored"]:
                print(f"      [{score:.2f}] {name}:{no}  {text[:150]}")
    return flagged, unscreenable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target", nargs="?", help="memory filename, e.g. dev.md (or bank/dev.md)"
    )
    parser.add_argument("--all", action="store_true", help="screen every target in the proposal")
    parser.add_argument(
        "--proposal",
        type=Path,
        help="proposal path (default: newest pending by mtime — pass the exact "
             "path you are reviewing, a same-day re-run writes another one)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=MIN_OVERLAP,
        help=f"score at or above which an item is flagged (default: {MIN_OVERLAP}, "
             "lib.conflict_edits.MIN_OVERLAP)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every item and its neighbours, not just flagged ones",
    )
    args = parser.parse_args()

    if not args.all and not args.target:
        parser.error("give a target file, or --all to screen the whole proposal")

    paths = get_paths()
    proposal = args.proposal or latest_pending_proposal(paths.dreams_dir())
    if proposal is None:
        print("no pending proposal under .multiplai/dreams/", file=sys.stderr)
        return 1
    if not proposal.is_file():
        print(f"proposal not found: {proposal}", file=sys.stderr)
        return 1

    proposal_text = proposal.read_text(encoding="utf-8")
    target = None if args.all else args.target
    entries = pending_items(proposal_text, target)

    if not entries:
        scope = "any target" if args.all else f"`{target}`"
        # Distinguish "clean" from "nothing matched" — a target named in the
        # proposal with no pending items is a different fact from a target
        # this proposal never mentions, and only the first is good news.
        known = sorted({e["target"] for e in parse_proposal_entries(proposal_text)})
        print(f"no pending items for {scope} in {proposal.name}")
        if target is not None and target not in known:
            print(
                f"  NOTE: `{target}` has no `## Updates for` section in this proposal. "
                f"Targets present: {', '.join(known) or '(none)'}",
                file=sys.stderr,
            )
            return 2
        return 0

    lines = corpus_lines(paths)
    labels = sorted({label for label, _, _, _ in lines})
    print(f"screening {len(entries)} pending item(s) against {len(lines)} lines "
          f"in {len(labels)} file(s)")
    if args.verbose:
        print(f"  corpus: {', '.join(labels)}")

    flagged, unscreenable = report(entries, lines, args.threshold, args.verbose)

    print(
        f"\n{len(entries)} pending item(s); {flagged} flagged at threshold "
        f"{args.threshold}; {unscreenable} unscreenable."
    )
    if not args.verbose:
        print("Only flagged and unscreenable items are shown — pass --verbose for all.")
    print("A flag is a lead to verify by reading both lines — not a verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
