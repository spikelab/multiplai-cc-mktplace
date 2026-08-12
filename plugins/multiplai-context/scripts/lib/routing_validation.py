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

3. **Near-duplicate check** — the same question at line level, with a measure
   that survives rewording. Eight consecutive tokens is a near-verbatim test:
   an item that *restates* an existing rule in its own words shares almost no
   8-grams with it and passes check 2 untouched. That is issue #195 — the
   drafter is shown only each memory file's headers, never its bullets, so it
   re-proposes what is already there and the reviewer pays for it. Measured on
   the 2026-08-10 backlog: 1 of 14 items on ``python.md``, 6 of 39 on
   ``git-policy.md``, and 12 of 17 rejections on ``claude-code-tools.md``
   restated a line in an always-loaded ``CLAUDE.md``.

   Two directions, same measure: each item against every corpus line, and each
   item against the other items targeting the same file (near-duplicates within
   one batch were surviving the drafting-time merge).

The gate only *warns* (a ``## Routing Warnings`` section appended to the
proposal for human review during dream-remember) — it never rewrites the
proposal. Callers wrap it fail-open + loud: a crash here must never lose a
generated proposal.

A near-duplicate warning is a **lead to verify by reading both lines**, not a
verdict — and a clean run is not a certificate: the measure strips code spans
and does not stem, so measured recall is roughly half (see
:func:`find_near_duplicate_line` for a real pair it misses, and
``dream_prescreen`` for the same caveat at review time). Nothing is dropped on
its say-so; who holds the pen does not change.
"""

import functools
import logging
import re
from pathlib import Path

from lib import taxonomy
from lib.conflict_edits import MIN_OVERLAP, content_words, overlap_sets

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
# Near-duplicate detection (reworded restatements the n-gram check misses)
# ---------------------------------------------------------------------------

# Corpus lines shorter than this carry too little to score: a heading or a
# one-word bullet matches everything a bit and nothing usefully. Same constant,
# same reasoning, as `dream_prescreen.MIN_LINE_LEN`.
MIN_LINE_LEN = 40


@functools.lru_cache(maxsize=64)
def _file_word_lines(content: str) -> tuple[tuple[int, str, frozenset[str]], ...]:
    """``(lineno, text, content_words)`` for each substantive line of *content*.

    Cached on the content itself for the same reason as :func:`_file_gram_index`:
    :func:`validate_proposal` scores every entry against every corpus line, and
    re-tokenizing the corpus per entry is what made the naive version quadratic
    in minutes rather than seconds.
    """
    out = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if len(stripped) > MIN_LINE_LEN:
            out.append((lineno, stripped, frozenset(content_words(stripped))))
    return tuple(out)


def find_near_duplicate_line(
    text: str,
    contents: dict[str, str],
    *,
    threshold: float = MIN_OVERLAP,
) -> tuple[str, int, str, float] | None:
    """Best-scoring corpus line that *text* restates, or ``None``.

    Returns ``(filename, line, line_text, overlap)``. Only the single best hit
    is returned: an item that restates a rule usually scores against several
    lines of the same section, and one location is what a reviewer needs to
    open. The measure is :func:`lib.conflict_edits.overlap_sets` — symmetric
    Jaccard over content words, at that module's own calibrated threshold, so
    this introduces no third tokenizer and no third number to tune.

    **Recall is partial and the misses are systematic.** ``content_words``
    strips code spans and does not stem, so a real duplicate pair measured on
    this workspace — "Never scatter worktrees inside project directories …"
    against ``git-policy.md:93`` "Worktrees live under
    ``$WORKSPACE/.worktrees/<name>`` (never inside project dirs)" — scores 0.29
    and is *not* flagged: the distinctive tokens are inside backticks, and
    worktree/worktrees, dirs/directories are separate words. Lowering the
    threshold or stemming here would trade precision the same constant buys
    ``conflict_edits`` for supersede edits, which is a calibration question, not
    a tweak. Treat a clean result as "no cheap hit", never as "no duplicate".
    """
    words = content_words(text)
    if not words:
        return None
    best: tuple[str, int, str, float] | None = None
    for name, content in contents.items():
        for lineno, line, line_words in _file_word_lines(content):
            # Jaccard is bounded above by the size ratio of the two sets, so a
            # line that cannot reach the threshold on length alone is skipped
            # before the intersection is computed. Pure speed, no effect on the
            # result — most corpus lines are far shorter than a proposal item.
            lo, hi = sorted((len(words), len(line_words)))
            if hi == 0 or lo / hi < threshold:
                continue
            score = overlap_sets(words, set(line_words))
            if score >= threshold and (best is None or score > best[3]):
                best = (name, lineno, line, score)
    return best


def find_batch_near_duplicate_groups(
    entries: list[dict],
    *,
    threshold: float = MIN_OVERLAP,
) -> list[tuple[list[dict], float]]:
    """Clusters of *entries* aimed at the same file that say the same thing.

    The drafting pass is told to merge duplicate lessons and the cross-slice
    pass merges across chunks, but both work on model judgment over a batch it
    reads in pieces — and both missed near-duplicate pairs sourced from
    different dates (issue #195, secondary finding). This is the deterministic
    backstop: same measure, restricted to entries sharing a target, because two
    items landing in the same file are what a reviewer must merge rather than
    apply twice.

    **Clusters, not pairs**, and that is not cosmetic: a proposal with 200
    restatements of one rule has ~20,000 duplicate *pairs*, and emitting a
    warning per pair would bury the proposal it is appended to. One warning per
    cluster is bounded by the number of entries. Returns
    ``[(entries, max_overlap)]``, each cluster in entry order, clusters ordered
    by their first entry.
    """
    by_target: dict[str, list[tuple[dict, set[str]]]] = {}
    for entry in entries:
        words = content_words(entry.get("text") or "")
        if words:
            by_target.setdefault(entry["target"], []).append((entry, words))

    groups: list[tuple[list[dict], float]] = []
    for members in by_target.values():
        # Union-find over the near-duplicate relation. Transitive by
        # construction: A~B and B~C put all three in one cluster even when A
        # and C fall under the threshold, which is the right answer for a
        # reviewer who has to merge them into one entry anyway.
        parent = list(range(len(members)))

        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        scored_pairs: list[tuple[int, float]] = []
        for i, (_, words_a) in enumerate(members):
            for j in range(i + 1, len(members)):
                score = overlap_sets(words_a, members[j][1])
                if score >= threshold:
                    ri, rj = _find(i), _find(j)
                    if ri != rj:
                        parent[rj] = ri
                    scored_pairs.append((i, score))

        clusters: dict[int, list[dict]] = {}
        for i, (entry, _) in enumerate(members):
            clusters.setdefault(_find(i), []).append(entry)
        # Roots are only final once every union is done, so the per-cluster max
        # is folded in afterwards rather than while merging.
        best: dict[int, float] = {}
        for i, score in scored_pairs:
            root = _find(i)
            best[root] = max(best.get(root, 0.0), score)
        for root, cluster in clusters.items():
            if len(cluster) > 1:
                groups.append((cluster, best.get(root, threshold)))
    return groups


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
    entries = parse_proposal_entries(proposal)
    for entry in entries:
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
        verbatim_hits = set()
        for name, line, overlap in find_duplicate_content(entry["text"], dedup_contents):
            if revises_target and name == entry["target"]:
                continue
            verbatim_hits.add(name)
            warnings.append(
                f"{label}: proposed text already present in {_where(name, entry, dedup_extra)} "
                f"`{name}:{line}` ({overlap:.0%} n-gram overlap) — apply only if "
                f"this is an intentional update of that entry."
            )

        # Same question, reworded-tolerant. Reported only when the n-gram check
        # did not already name that file for this entry: two warnings about one
        # location is review cost, and this check exists to remove review cost.
        near = find_near_duplicate_line(entry["text"], dedup_contents)
        if near:
            name, line, line_text, score = near
            if name not in verbatim_hits and not (revises_target and name == entry["target"]):
                warnings.append(
                    f"{label}: near-duplicate of an existing line in "
                    f"{_where(name, entry, dedup_extra)} `{name}:{line}` "
                    f"({score:.0%} word overlap) — \"{line_text[:120]}\". "
                    f"Read both lines: if it restates that line, merge or drop it."
                )

    for cluster, score in find_batch_near_duplicate_groups(entries):
        numbers = ", ".join(f"#{e['number']}" for e in cluster)
        warnings.append(
            f"`{cluster[0]['target']}` {numbers} are near-duplicates of each other "
            f"(up to {score:.0%} word overlap; first is \"{cluster[0]['title']}\") — "
            f"merge into one entry rather than applying each."
        )
    return warnings


def _where(name: str, entry: dict, dedup_extra: dict[str, str]) -> str:
    """Name the kind of file a hit landed in, for the warning line."""
    if name == entry["target"]:
        return "target file"
    if name in dedup_extra:
        return "an ALWAYS-LOADED file"
    return "ANOTHER file"


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
