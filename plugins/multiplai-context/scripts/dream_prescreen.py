#!/usr/bin/env python3
"""Pre-screen a dream proposal's pending items against the whole memory corpus.

The triage judge in ``dream.py --triage`` checks an item for redundancy against
*its own target file*. That leaves two duplicate shapes uncaught:

* the item already exists in a **different** memory file, and
* the item restates a rule that lives in an always-loaded ``CLAUDE.md`` — which
  the drafter never sees, so it re-proposes it on every run.

This script scores each pending item against every line in the corpus (memory
files *and* the CLAUDE.md files) by content-word overlap, and prints the two
nearest lines per item. A high score is a lead to verify by opening both lines,
never a verdict on its own.

Usage::

    dream_prescreen.py <target-file.md> [--proposal PATH] [--threshold 0.45]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiplai_core.paths import get_paths  # noqa: E402

# Words carrying no topical signal. Kept deliberately small: over-pruning
# collapses distinct items onto the same neighbours and hides real duplicates.
STOP = set(
    """the a an and or of to in for is are be that this it with as on at by not
from when if then than but do does don't no never always must should can could
will before after over under into out up down only just also more most other
same such each its their they them we you your our i me my rather instead which
what who how why where use used using make makes made get gets got has have had
was were been being one two three new old real via per e.g eg ie""".split()
)

TOKEN_RE = re.compile(r"[a-z0-9_\-.]+")
ITEM_RE = re.compile(r"^### ", re.M)
NUM_RE = re.compile(r"(\d+)\.")
MIN_LINE_LEN = 40


def tokens(text: str) -> set[str]:
    return {
        w for w in TOKEN_RE.findall(text.lower()) if w not in STOP and len(w) > 2
    }


def corpus_lines(mem_dir: Path, workspace: Path) -> list[tuple[str, int, str, set[str]]]:
    """Every substantive line in the corpus, as (label, lineno, text, tokens).

    Includes the always-loaded CLAUDE.md files alongside ``memory/*.md``: rules
    living there are the single largest source of re-proposed duplicates.
    """
    out: list[tuple[str, int, str, set[str]]] = []
    paths = sorted(mem_dir.glob("*.md"))
    for extra in (mem_dir / "CLAUDE.md", workspace / "CLAUDE.md"):
        if extra.is_file() and extra not in paths:
            paths.append(extra)

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        label = path.name if path.parent == mem_dir else f"{path.parent.name}/{path.name}"
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if len(stripped) > MIN_LINE_LEN:
                out.append((label, lineno, stripped, tokens(stripped)))
    return out


def pending_items(proposal: Path, target: str) -> list[tuple[str, str]]:
    """(heading, body) for each still-pending item under ``target``'s section."""
    text = proposal.read_text(encoding="utf-8").split("\n## Processed", 1)[0]
    section = re.search(
        r"^## Updates for `" + re.escape(target) + r"`\n(.*?)(?=^## |\Z)",
        text,
        re.S | re.M,
    )
    if not section:
        return []
    items = []
    for chunk in ITEM_RE.split(section.group(1))[1:]:
        heading, _, rest = chunk.partition("\n")
        body = " ".join(
            line[1:].strip() for line in rest.splitlines() if line.startswith(">")
        )
        items.append((heading.strip(), body))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="memory filename, e.g. dev.md")
    parser.add_argument("--proposal", type=Path, help="proposal path (default: newest)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="score at or above which an item is flagged (default: 0.45)",
    )
    args = parser.parse_args()

    paths = get_paths()
    mem_dir = paths.memory_dir()
    # memory lives at <workspace>/.multiplai/memory — two levels up is the root.
    workspace = mem_dir.parent.parent

    proposal = args.proposal
    if proposal is None:
        candidates = sorted(paths.dreams_dir().glob("processed-learnings-*.md"))
        if not candidates:
            print("no proposal found under .multiplai/dreams/", file=sys.stderr)
            return 1
        proposal = candidates[-1]
    if not proposal.is_file():
        print(f"proposal not found: {proposal}", file=sys.stderr)
        return 1

    items = pending_items(proposal, args.target)
    if not items:
        print(f"no pending items for {args.target} in {proposal.name}")
        return 0

    lines = corpus_lines(mem_dir, workspace)
    flagged = 0

    for heading, body in items:
        item_tokens = tokens(body)
        scored = sorted(
            (
                (len(item_tokens & line_tokens) / max(len(item_tokens), 1), label, no, text)
                for label, no, text, line_tokens in lines
            ),
            reverse=True,
        )[:2]
        hit = bool(scored) and scored[0][0] >= args.threshold
        flagged += hit
        print(f"\n### {heading}{'   <<< LIKELY ALREADY PRESENT' if hit else ''}")
        print(f"    {body[:240]}")
        for score, label, no, text in scored:
            print(f"      [{score:.2f}] {label}:{no}  {text[:150]}")

    print(
        f"\n{len(items)} pending item(s) for {args.target}; "
        f"{flagged} flagged at threshold {args.threshold}."
    )
    print("A flag is a lead to verify by reading both lines — not a verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
