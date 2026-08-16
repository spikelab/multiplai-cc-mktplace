"""Deterministic post-proposal validation gate for dream routing.

Pure code, no LLM. Two checks over a drafted proposal:

1. **Section-registry check** — two questions off one index, deliberately
   scoped differently (issue #200).

   *Does this section exist?* spans **H2–H4**. Memory files carry most of
   their sections at H3 — measured 2026-08-13 on the reporting corpus: 299 H2
   against 347 H3, so an H2-only index is blind to 54% of real sections. It
   reported "section X does not exist" for sections plainly present and then
   suggested rerouting the item to whichever *other* file happened to own an
   H2 by that name. Because a flagged item is never applied silently, each
   false positive converted an auto-applicable item into a manual routing
   question, pointed at the wrong file.

   *Would this new section collide?* stays **H2-only**. The uniqueness
   invariant in ``memory/CLAUDE.md`` is about H2 specifically — duplicate H2
   names break deterministic routing — and an H3 of the same name elsewhere
   does not break it. Widening the collision check would trade the old false
   positives for new ones.

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
verdict — and a clean run is not a certificate. The measure keeps code spans as
of issue #199 but still does not stem (stemming was backtested and made the
ratio worse), so recall is better than it was and still partial. Nothing is
dropped on its say-so; who holds the pen does not change.
"""

import logging
import re
from collections.abc import Set as AbstractSet
from pathlib import Path

from lib import taxonomy
from lib.conflict_edits import (
    MIN_OVERLAP,
    content_words,
    overlap_sets,
    screenable_lines,
)

logger = logging.getLogger(__name__)

NGRAM_SIZE = 8
# Fraction of a proposed insert's n-grams that must appear in a memory file
# before we call the insert "already present" there. 0.5 tolerates light
# rephrasing while ignoring incidental shared phrases.
DUPLICATE_RATIO = 0.5

# Near-duplicate warnings quote the corpus line they matched — that is what
# makes a lead actionable without opening the file. Past this many, the quote is
# dropped and the warning keeps its label, location and score. `dream_prescreen`
# carries the measurement this bound exists for: printing every item with its
# body and neighbours cost 411,614 bytes (~103k tokens) on the 602-item backlog,
# inside the very review whose context window it was meant to protect.
#
# The count of warnings is deliberately *not* capped, only their size.
# `dream_triage.flagged_by_routing` reads this section to decide which items may
# be written unreviewed, so dropping a warning would silently widen what
# auto-applies — the opposite of what a cap is for.
NEAR_DUPLICATE_QUOTES = 40

# H2 through H4. Deeper than H4 is a sub-point inside a section, not a routing
# target, and indexing it would make short generic names ("Notes", "Gotchas")
# collide across the corpus for no gain.
_HEADING_RE = re.compile(r"^(?P<hashes>#{2,4}) +(?P<name>.+?)\s*$", re.MULTILINE)
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

def index_sections(contents: dict[str, str]) -> dict[str, list[tuple[str, int]]]:
    """Map each H2–H4 section name to ``[(filename, heading level)]``.

    One pass, both questions. Callers that ask *does this section exist*
    read every entry; callers enforcing the H2-uniqueness invariant filter to
    ``level == 2``. Keeping the level here rather than building two indexes is
    what lets the two checks disagree on scope without disagreeing on the
    corpus they read (issue #200).
    """
    index: dict[str, list[tuple[str, int]]] = {}
    for name, content in contents.items():
        for match in _HEADING_RE.finditer(content):
            index.setdefault(match.group("name"), []).append(
                (name, len(match.group("hashes")))
            )
    return index


def _owners(entries: list[tuple[str, int]], *, level: int | None = None) -> list[str]:
    """Filenames from an index entry list, de-duplicated, order preserved.

    A file that carries the same section name at two depths (or twice at one)
    must not be named twice in a warning.
    """
    seen: list[str] = []
    for filename, heading_level in entries:
        if level is not None and heading_level != level:
            continue
        if filename not in seen:
            seen.append(filename)
    return seen


def build_section_registry(memory_dir: Path) -> dict[str, list[str]]:
    """Map each H2–H4 section name to the memory file(s) that contain it.

    Existence semantics — the same scope :func:`index_sections` gives, flattened
    for callers that do not care what depth a section sits at. For the
    H2-uniqueness invariant use :func:`index_sections` and filter on level;
    this function cannot answer that question and no longer pretends to.
    """
    contents: dict[str, str] = {}
    if not memory_dir.exists():
        return {}
    for f in sorted(memory_dir.glob("*.md")):
        if not f.is_file():
            continue
        try:
            contents[f.name] = f.read_text()
        except OSError:
            continue
    return {
        section: _owners(entries)
        for section, entries in index_sections(contents).items()
    }


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


def _file_gram_index(content: str, n: int = NGRAM_SIZE) -> dict[tuple[str, ...], int]:
    """Map each n-gram in *content* to the 1-indexed line where it starts.

    Built once per corpus file per :func:`validate_proposal` call — see
    :func:`_corpus_gram_index` for why that is done by the caller rather than by
    a cache on this function.
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


def _corpus_gram_index(
    contents: dict[str, str],
) -> dict[str, dict[tuple[str, ...], int]]:
    """N-gram index per corpus file, built once for a whole proposal.

    This used to be an ``lru_cache(maxsize=64)`` on :func:`_file_gram_index`,
    keyed by file content. That is the wrong shape for this access pattern and
    fails at a cliff rather than degrading: every entry is scored against every
    file in a fixed cycle, so an LRU smaller than the corpus evicts each entry
    before the next pass reaches it and the hit rate goes to *zero*. The corpus
    is no longer bounded either — :func:`lib.memory_corpus.bank_paths` globs
    every ``*.md`` in every subscribed bank — so one shared bank could cross 64
    files and take a run from seconds to minutes. Building both indexes once per
    call has no cliff to sit near, and drops the whole corpus when the call
    returns instead of pinning it in a module-level cache.
    """
    return {name: _file_gram_index(content) for name, content in contents.items()}


def _duplicate_hits(
    grams: set[tuple[str, ...]],
    index: dict[str, dict[tuple[str, ...], int]],
    ratio: float,
) -> list[tuple[str, int, float]]:
    """The :func:`find_duplicate_content` scan against a prebuilt index."""
    if not grams:
        return []
    hits: list[tuple[str, int, float]] = []
    for name, file_index in index.items():
        shared = grams & file_index.keys()
        if not shared:
            continue
        overlap = len(shared) / len(grams)
        if overlap >= ratio:
            hits.append((name, min(file_index[g] for g in shared), overlap))
    hits.sort(key=lambda h: h[2], reverse=True)
    return hits


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
    return _duplicate_hits(
        _ngrams(_tokenize(text)), _corpus_gram_index(memory_contents), ratio
    )


# ---------------------------------------------------------------------------
# Near-duplicate detection (reworded restatements the n-gram check misses)
# ---------------------------------------------------------------------------


def _corpus_word_lines(
    contents: dict[str, str],
) -> dict[str, list[tuple[int, str, frozenset[str]]]]:
    """Screenable lines per corpus file, built once for a whole proposal.

    Which lines are screenable, and how they are tokenized, is
    :func:`lib.conflict_edits.screenable_lines` — the same function
    ``dream_prescreen`` calls at review time. One constant, one filter, one
    tokenizer: a run that is clean under the gate and dirty under the lens (or
    the reverse) is the failure :mod:`lib.memory_corpus` exists to prevent, and
    a second copy of the screen here would reintroduce it one level down.

    Built per call rather than memoized, for the reason given in
    :func:`_corpus_gram_index`.
    """
    return {name: screenable_lines(content) for name, content in contents.items()}


def _best_near_duplicate(
    words: AbstractSet[str],
    index: dict[str, list[tuple[int, str, frozenset[str]]]],
    threshold: float,
) -> tuple[str, int, str, float] | None:
    """The :func:`find_near_duplicate_line` scan against a prebuilt index."""
    if not words:
        return None
    n_words = len(words)
    best: tuple[str, int, str, float] | None = None
    for name, lines in index.items():
        for lineno, line, line_words in lines:
            # Jaccard is bounded above by the size ratio of the two sets, so a
            # line that cannot reach the threshold on length alone is skipped
            # before the intersection is computed. Pure speed, no effect on the
            # result — most corpus lines are far shorter than a proposal item.
            n_line = len(line_words)
            lo, hi = (n_words, n_line) if n_words < n_line else (n_line, n_words)
            if hi == 0 or lo < threshold * hi:
                continue
            score = overlap_sets(words, line_words)
            if score >= threshold and (best is None or score > best[3]):
                best = (name, lineno, line, score)
    return best


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

    Because one global best is returned, a caller that wants some file left out
    must leave it out of *contents* — filtering the result afterwards discards
    the entry's coverage of every other file. :func:`validate_proposal` does
    exactly that; see the note there.

    **Recall is partial, and the pair that motivated issue #199 is still a
    miss.** ``content_words`` now keeps code spans, which is worth +15 true
    positives on the 602-item backtest — but it did *not* rescue this pair, and
    that is worth knowing. "Never scatter worktrees inside project directories
    …" against ``git-policy.md:93`` "Worktrees live under
    ``$WORKSPACE/.worktrees/<name>`` (never inside project dirs)" scored 0.29
    when code spans were stripped and scores **0.27** now: the freed backtick
    content tokenizes as ``workspace/.worktrees`` and ``name``, which match
    nothing and grow the union. What actually separates the two is stemming —
    worktree/worktrees, dirs/directories, live/lives — and stemming was
    backtested in the same run and made the overall ratio *worse* at every
    threshold (12.7:1 against 13.2 baseline at 0.35).

    So this pair is a genuine miss the measure cannot reach, not a defect
    waiting on a config change. It is the reason to read the items rather than
    trust a clean run. Lowering the threshold instead would trade precision the
    same constant buys ``conflict_edits`` for supersede edits, which is a
    calibration question, not a tweak. Treat a clean result as "no cheap hit",
    never as "no duplicate".
    """
    return _best_near_duplicate(
        content_words(text), _corpus_word_lines(contents), threshold
    )


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
    by_target: dict[str, list[tuple[dict, frozenset[str]]]] = {}
    for entry in entries:
        words = content_words(entry.get("text") or "")
        if words:
            by_target.setdefault(entry["target"], []).append((entry, frozenset(words)))

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
            n_a = len(words_a)
            for j in range(i + 1, len(members)):
                words_b = members[j][1]
                # The same size-ratio bound the corpus scan uses, and it earns
                # more here: that scan is linear in corpus lines, this one is
                # quadratic in entries aimed at one file. 600 items on a single
                # target is ~180,000 pairs, and most are rejected by two lengths
                # rather than an intersection.
                n_b = len(words_b)
                lo, hi = (n_a, n_b) if n_a < n_b else (n_b, n_a)
                if hi == 0 or lo < threshold * hi:
                    continue
                score = overlap_sets(words_a, words_b)
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
    section_index = index_sections(memory_contents)

    dedup_extra = dedup_extra or {}
    dedup_contents = {**memory_contents, **dedup_extra}
    # Both indexes are built once here, not once per entry: a 600-entry proposal
    # scores every entry against every corpus file, and re-deriving either index
    # inside that loop is what made the naive version quadratic in minutes.
    gram_index = _corpus_gram_index(dedup_contents)
    word_index = _corpus_word_lines(dedup_contents)

    warnings: list[str] = []
    quoted = abbreviated = 0
    entries = parse_proposal_entries(proposal)
    for entry in entries:
        label = f"`{entry['target']}` #{entry['number']} ({entry['title']})"
        section, is_new = _parse_section_field(entry["section"])

        if section:
            found_in = section_index.get(section, [])
            if is_new:
                # H2-only: the uniqueness invariant this warns about is an H2
                # invariant. An H3 of the same name elsewhere is not a collision.
                collisions = [
                    o for o in _owners(found_in, level=2) if o != entry["target"]
                ]
                if collisions:
                    warnings.append(
                        f"{label}: proposed new section \"{section}\" collides with an "
                        f"existing section in `{', '.join(collisions)}` — section names "
                        f"must stay unique across memory files; reroute or rename."
                    )
            else:
                # Existence spans H2-H4, so a section the target really has at
                # H3 no longer reads as missing and no longer gets a reroute
                # suggestion pointing at some other file's H2.
                owners = _owners(found_in)
                if owners and entry["target"] not in owners:
                    # Name every owner, but *suggest* the H2 one. Section names
                    # are unique at H2 by invariant, which is what makes routing
                    # deterministic — so the H2 owner is where the item belongs.
                    # `owners` is filename order at any depth, so `owners[0]` is
                    # otherwise whichever file happens to sort first: measured
                    # on one real corpus, 8 section names had more than one
                    # owner and in 6 of them the alphabetically-first owner held
                    # the name at H3 while a *different* file held it at H2
                    # (`Architecture`, `Overview`, `Skill Architecture`, …).
                    h2_owners = _owners(found_in, level=2)
                    suggested = h2_owners[0] if h2_owners else owners[0]
                    warnings.append(
                        f"{label}: section \"{section}\" does not exist in "
                        f"`{entry['target']}` but does in `{', '.join(owners)}` — "
                        f"suggested reroute to `{suggested}`."
                    )

        # An update/replace entry legitimately overlaps the old text in its
        # own target file — only cross-file hits are signal for those.
        revises_target = entry["change"] in ("update", "replace")
        verbatim_hits = set()
        for name, line, overlap in _duplicate_hits(
            _ngrams(_tokenize(entry["text"])), gram_index, DUPLICATE_RATIO
        ):
            if revises_target and name == entry["target"]:
                continue
            verbatim_hits.add(name)
            warnings.append(
                f"{label}: proposed text already present in {_where(name, entry, dedup_extra)} "
                f"`{name}:{line}` ({overlap:.0%} n-gram overlap) — apply only if "
                f"this is an intentional update of that entry."
            )

        # Same question, reworded-tolerant, over the corpus **minus** the files a
        # warning from would be noise: one the n-gram check already named for
        # this entry (two warnings about one location is review cost, and this
        # check exists to remove review cost), and — for an update/replace — the
        # entry's own target, which it legitimately overlaps.
        #
        # Excluded from the *search*, not from the result. `_best_near_duplicate`
        # returns one global best, so dropping it afterwards when it landed in an
        # exempt file threw away the entry's coverage of every other file: an
        # `update` entry scores highest against the very line it revises, so it
        # got no cross-file near-duplicate check at all — including against the
        # always-loaded `CLAUDE.md`s, which is the 12-of-17 case this exists for.
        suppressed = set(verbatim_hits)
        if revises_target:
            suppressed.add(entry["target"])
        near = _best_near_duplicate(
            content_words(entry["text"]),
            {k: v for k, v in word_index.items() if k not in suppressed},
            MIN_OVERLAP,
        )
        if near:
            name, line, line_text, score = near
            head = (
                f"{label}: near-duplicate of an existing line in "
                f"{_where(name, entry, dedup_extra)} `{name}:{line}` "
                f"({score:.0%} word overlap)"
            )
            if quoted < NEAR_DUPLICATE_QUOTES:
                quoted += 1
                warnings.append(
                    f"{head} — \"{line_text[:120]}\". Read both lines: if it "
                    f"restates that line, merge or drop it."
                )
            else:
                abbreviated += 1
                warnings.append(f"{head} — open both lines; quote omitted, see below.")

    for cluster, score in find_batch_near_duplicate_groups(entries):
        numbers = ", ".join(f"#{e['number']}" for e in cluster)
        warnings.append(
            f"`{cluster[0]['target']}` {numbers} are near-duplicates of each other "
            f"(up to {score:.0%} word overlap; first is \"{cluster[0]['title']}\") — "
            f"merge into one entry rather than applying each."
        )

    if abbreviated:
        # Stated, never silent: a bound the reader cannot see reads as "the gate
        # found nothing more to say".
        warnings.append(
            f"{abbreviated} near-duplicate warning(s) above carry no quote — only "
            f"the first {NEAR_DUPLICATE_QUOTES} do, so this section stays readable "
            f"inside the review it is read in. Nothing was dropped: every flagged "
            f"item still has its own line above. For the quotes and their "
            f"neighbours, run `dream_prescreen.py --all --verbose`."
        )
    return warnings


def _where(name: str, entry: dict, dedup_extra: dict[str, str]) -> str:
    """Name the kind of file a hit landed in, for the warning line.

    A shared-bank label is ``bank/file.md`` and an always-loaded one is
    ``CLAUDE.md (global)`` / ``CLAUDE.md (workspace)``, so the ``/`` tells them
    apart without threading a second mapping through. Worth telling apart: the
    reviewer's instructions treat a rule already in an always-loaded file as the
    most re-proposed shape there is and drop it, where a bank hit is a routing
    and contribution question.
    """
    if name == entry["target"]:
        return "target file"
    if name in dedup_extra:
        return "a SHARED BANK file" if "/" in name else "an ALWAYS-LOADED file"
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
