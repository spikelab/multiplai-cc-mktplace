"""Deterministic post-proposal validation gate for dream routing.

Pure code, no LLM. Two checks over a drafted proposal:

1. **Section-registry check** — every H2 section name is unique across the
   memory files (enforced by the workspace reorg), so a section name maps to
   exactly one file. An entry whose ``Section:`` exists in a *different* file
   than its target is a misroute; a "new section" whose name collides with
   another file's section would break the registry invariant.

2. **Cross-file dedup check** — normalized 8-gram token overlap of each
   proposed insert against *all* memory files (not just the target), catching
   content that already lives elsewhere before it gets applied twice.

The gate only *warns* (a ``## Routing Warnings`` section appended to the
proposal for human review during dream-remember) — it never rewrites the
proposal. Callers wrap it fail-open + loud: a crash here must never lose a
generated proposal.
"""

import functools
import logging
import re
from pathlib import Path

from lib import taxonomy

logger = logging.getLogger(__name__)

NGRAM_SIZE = 8
# Fraction of a proposed insert's n-grams that must appear in a memory file
# before we call the insert "already present" there. 0.5 tolerates light
# rephrasing while ignoring incidental shared phrases.
DUPLICATE_RATIO = 0.5

_H2_RE = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
_ENTRY_RE = re.compile(r"^### +(?P<num>A?\d+)\.\s*(?P<title>.*)$")
_UPDATES_FOR_RE = re.compile(r"^## Updates for `(?P<file>[^`]+)`")
_SECTION_FIELD_RE = re.compile(r"^\*\*Section:\*\*\s*(?P<value>.+?)\s*$")
_CHANGE_FIELD_RE = re.compile(r"^\*\*Change:\*\*\s*(?P<value>.+?)\s*$")
_SOURCE_FIELD_RE = re.compile(r"^\*\*Source:\*\*\s*(?P<value>.+?)\s*$")
_PROVENANCE_FIELD_RE = re.compile(r"^\*\*Provenance:\*\*\s*(?P<value>.+?)\s*$")
_NEW_SECTION_RE = re.compile(r"^new section\b\s*[:—\-]?\s*", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


# ---------------------------------------------------------------------------
# Section registry
# ---------------------------------------------------------------------------

def build_section_registry(memory_dir: Path) -> dict[str, list[str]]:
    """Map each H2 section name to the memory file(s) that contain it.

    With unique section names the value lists are singletons; a multi-file
    value means the workspace invariant is already broken (worth surfacing,
    but not this module's job to fix).
    """
    registry: dict[str, list[str]] = {}
    if not memory_dir.exists():
        return registry
    for f in sorted(memory_dir.glob("*.md")):
        if not f.is_file():
            continue
        try:
            content = f.read_text()
        except OSError:
            continue
        for match in _H2_RE.finditer(content):
            registry.setdefault(match.group(1), []).append(f.name)
    return registry


# ---------------------------------------------------------------------------
# Proposal parsing
# ---------------------------------------------------------------------------

def parse_proposal_entries(proposal: str) -> list[dict]:
    """Extract memory-update entries from a drafted proposal.

    Returns dicts with ``target`` (filename), ``number``, ``title``,
    ``section`` (the ``**Section:**`` value, may be empty), ``change``
    (the ``**Change:**`` value lowercased — add/update/replace, may be
    empty), ``source`` (the ``**Source:**`` citation, may be empty),
    ``provenance`` and ``kind`` (the two halves of the ``**Provenance:**``
    pair, each empty when absent or not a recognised value) and
    ``text`` (the blockquoted insert text, unquoted). Action
    items (``### A{N}.``) and non-update sections (Filtered Out, Action
    Items) are skipped.

    ``provenance``/``kind`` are validated against ``lib.taxonomy``'s closed
    sets and come back empty rather than as whatever string was written, so a
    consumer never sees a label the vocabulary does not define. Both halves are
    empty for a proposal drafted before the taxonomy existed — an absent pair
    is a legitimate state, not a parse failure.
    """
    entries: list[dict] = []
    current_file: str | None = None
    entry: dict | None = None

    def _flush():
        nonlocal entry
        if entry is not None:
            entry["text"] = "\n".join(entry.pop("_text_lines")).strip()
            entries.append(entry)
            entry = None

    for line in proposal.splitlines():
        m = _UPDATES_FOR_RE.match(line)
        if m:
            _flush()
            current_file = m.group("file")
            continue
        if line.startswith("## "):
            _flush()
            current_file = None
            continue
        if current_file is None:
            continue
        m = _ENTRY_RE.match(line)
        if m:
            _flush()
            if m.group("num").startswith("A"):
                continue  # action item — not a memory update
            entry = {
                "target": current_file,
                "number": m.group("num"),
                "title": m.group("title").strip(),
                "section": "",
                "change": "",
                "source": "",
                "provenance": "",
                "kind": "",
                "_text_lines": [],
            }
            continue
        if entry is None:
            continue
        m = _SECTION_FIELD_RE.match(line)
        if m:
            entry["section"] = m.group("value")
            continue
        m = _CHANGE_FIELD_RE.match(line)
        if m:
            entry["change"] = m.group("value").lower()
            continue
        # Provenance. Kept because an item applied without a human reading it
        # is only auditable if the receipt can say where it came from.
        m = _SOURCE_FIELD_RE.match(line)
        if m:
            entry["source"] = m.group("value")
            continue
        # The two-axis taxonomy, carried through from the learning this entry
        # was distilled from. Nothing in this module acts on it — it is parsed
        # here because this is the function every consumer of a proposal item
        # already calls, and a pair that stops at the learnings file cannot be
        # used by anything downstream.
        m = _PROVENANCE_FIELD_RE.match(line)
        if m:
            provenance, kind = taxonomy.parse_pair(m.group("value"))
            entry["provenance"] = provenance or ""
            entry["kind"] = kind or ""
            continue
        if line.startswith(">"):
            entry["_text_lines"].append(line.lstrip("> ").rstrip())
    _flush()
    return entries


def _parse_section_field(value: str) -> tuple[str, bool]:
    """Return (section_name, is_new) from a ``**Section:**`` field value.

    "New section" markers come in variants: ``New section``,
    ``New section: "Name"``, ``New section — Name``. The name may be empty.
    Quotes and backticks around names are stripped in all cases.
    """
    value = value.strip()
    m = _NEW_SECTION_RE.match(value)
    if m:
        return value[m.end():].strip().strip("\"'`"), True
    return value.strip("\"'`"), False


# ---------------------------------------------------------------------------
# Cross-file duplicate detection
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _ngrams(tokens: list[str], n: int = NGRAM_SIZE) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


@functools.lru_cache(maxsize=64)
def _file_gram_index(content: str, n: int = NGRAM_SIZE) -> dict[tuple[str, ...], int]:
    """Map each n-gram in *content* to the 1-indexed line where it starts.

    Cached on the content itself: :func:`validate_proposal` calls this once per
    corpus file **per proposal entry**, so a 583-entry proposal re-indexed the
    same files 583 times. Python memoizes a ``str``'s hash after the first
    lookup, so the key is cheap; the values are dicts nothing mutates.
    """
    index: dict[tuple[str, ...], int] = {}
    tokens: list[str] = []
    token_lines: list[int] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for tok in _tokenize(line):
            tokens.append(tok)
            token_lines.append(lineno)
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i:i + n])
        if gram not in index:
            index[gram] = token_lines[i]
    return index


def find_duplicate_content(
    text: str,
    memory_contents: dict[str, str],
    *,
    ratio: float = DUPLICATE_RATIO,
) -> list[tuple[str, int, float]]:
    """Find memory files that already contain *text* (or most of it).

    Returns ``[(filename, line, overlap_ratio)]`` for every file where at
    least ``ratio`` of the text's n-grams already appear, sorted by overlap
    descending. Texts too short to form a single n-gram return no hits —
    short one-liners produce too many false positives to gate on.
    """
    grams = _ngrams(_tokenize(text))
    if not grams:
        return []
    hits: list[tuple[str, int, float]] = []
    for name, content in memory_contents.items():
        index = _file_gram_index(content)
        shared = grams & index.keys()
        overlap = len(shared) / len(grams)
        if overlap >= ratio:
            hits.append((name, min(index[g] for g in shared), overlap))
    hits.sort(key=lambda h: h[2], reverse=True)
    return hits


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_proposal(
    proposal: str,
    memory_contents: dict[str, str],
    *,
    dedup_extra: dict[str, str] | None = None,
) -> list[str]:
    """Run both deterministic checks; return human-readable warning lines.

    ``memory_contents`` is ``{filename: content}`` for all memory files (the
    same mapping dream already loads). An empty list means the proposal is
    clean under both checks.

    ``dedup_extra`` is ``{label: content}`` for corpus that is **dedup evidence
    only** — the always-loaded ``CLAUDE.md`` files and the shared memory banks
    (see :mod:`lib.memory_corpus`). It is deliberately kept out of the section
    registry: an H2 in a ``CLAUDE.md`` is not a memory section, so registering
    it would turn every ordinary heading name into a phantom misroute.
    """
    registry: dict[str, list[str]] = {}
    for name, content in memory_contents.items():
        for match in _H2_RE.finditer(content):
            registry.setdefault(match.group(1), []).append(name)

    dedup_extra = dedup_extra or {}
    dedup_contents = {**memory_contents, **dedup_extra}
    warnings: list[str] = []
    for entry in parse_proposal_entries(proposal):
        label = f"`{entry['target']}` #{entry['number']} ({entry['title']})"
        section, is_new = _parse_section_field(entry["section"])

        if section:
            owners = registry.get(section, [])
            if is_new:
                collisions = [o for o in owners if o != entry["target"]]
                if collisions:
                    warnings.append(
                        f"{label}: proposed new section \"{section}\" collides with an "
                        f"existing section in `{', '.join(collisions)}` — section names "
                        f"must stay unique across memory files; reroute or rename."
                    )
            elif owners and entry["target"] not in owners:
                warnings.append(
                    f"{label}: section \"{section}\" does not exist in "
                    f"`{entry['target']}` but does in `{', '.join(owners)}` — "
                    f"suggested reroute to `{owners[0]}`."
                )

        # An update/replace entry legitimately overlaps the old text in its
        # own target file — only cross-file hits are signal for those.
        revises_target = entry["change"] in ("update", "replace")
        for name, line, overlap in find_duplicate_content(entry["text"], dedup_contents):
            if revises_target and name == entry["target"]:
                continue
            if name == entry["target"]:
                where = "target file"
            elif name in dedup_extra:
                where = "an ALWAYS-LOADED file"
            else:
                where = "ANOTHER file"
            warnings.append(
                f"{label}: proposed text already present in {where} "
                f"`{name}:{line}` ({overlap:.0%} n-gram overlap) — apply only if "
                f"this is an intentional update of that entry."
            )
    return warnings


def render_warnings_section(warnings: list[str]) -> str:
    """Render the ``## Routing Warnings`` block to append to a proposal.

    Always rendered — "(none)" when clean — so dream-remember (and the human)
    can tell "validated clean" apart from "gate didn't run".
    """
    body = "\n".join(f"- {w}" for w in warnings) if warnings else "(none)"
    return f"\n\n---\n\n## Routing Warnings\n\n{body}\n"


def append_routing_warnings(
    proposal: str,
    memory_contents: dict[str, str],
    *,
    dedup_extra: dict[str, str] | None = None,
) -> str:
    """Validate *proposal* and append its ``## Routing Warnings`` section."""
    warnings = validate_proposal(proposal, memory_contents, dedup_extra=dedup_extra)
    return proposal.rstrip() + render_warnings_section(warnings)
