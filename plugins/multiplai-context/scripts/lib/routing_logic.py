"""Pure helpers for bundle and co_retrieve_for expansion.

After the router picks a set of catalog entries by intent, two
metadata-driven expansions widen the picks before content is loaded:

  - **Bundles**: catalog entries can declare ``"bundle": "<name>"``.
    Picking any one entry in a bundle pulls in all bundle siblings.
    Used when a group of files is only useful together (e.g., a
    voice/style pair where one without the other gives a misleading
    half-picture).

  - **co_retrieve_for**: an entry can list filenames of other
    entries in the same corpus that should be loaded with it. Used
    when one file is a natural companion to another (e.g., an
    overview + a deep-dive).

Both expansions respect ``anti_domains`` — if a candidate's
anti_domains match the prompt, the router has already filtered it
out, but expansion shouldn't re-introduce excluded entries. We
encode that by accepting an ``excluded`` set the caller passes.

Entry-key extraction follows the same precedence the router uses
(``source`` → ``path`` → ``file``) for compatibility with all three
catalog shapes.
"""

from __future__ import annotations

from collections.abc import Iterable


def _entry_filename(entry: dict) -> str:
    """Mirror ``memory_router._entry_filename`` so callers can use either."""
    return entry.get("source") or entry.get("path") or entry.get("file", "")


def expand_bundles(
    picks: Iterable[str],
    catalog_entries: Iterable[dict],
    *,
    excluded: set[str] | None = None,
) -> list[str]:
    """Expand picks to include all bundle siblings.

    For each picked filename, finds its catalog entry, reads its
    ``bundle`` field, and returns every entry that shares the bundle
    value (including the original picks). Entries in ``excluded`` are
    never re-introduced.

    Order: original picks first (preserved), then siblings in catalog
    order. Duplicates removed.
    """
    excluded = excluded or set()
    picks_list = [p for p in picks if p]
    pick_set = set(picks_list)

    by_filename: dict[str, dict] = {
        _entry_filename(e): e for e in catalog_entries if _entry_filename(e)
    }

    bundles_to_expand: set[str] = set()
    for filename in picks_list:
        entry = by_filename.get(filename)
        if not entry:
            continue
        bundle = entry.get("bundle")
        if isinstance(bundle, str) and bundle.strip():
            bundles_to_expand.add(bundle.strip())

    if not bundles_to_expand:
        # Filter excluded just in case the caller passed one through.
        return [p for p in picks_list if p not in excluded]

    # Walk catalog in order, append bundle siblings not already picked.
    expanded = [p for p in picks_list if p not in excluded]
    for entry in catalog_entries:
        filename = _entry_filename(entry)
        if not filename or filename in pick_set or filename in excluded:
            continue
        bundle = entry.get("bundle")
        if isinstance(bundle, str) and bundle.strip() in bundles_to_expand:
            expanded.append(filename)
            pick_set.add(filename)
    return expanded


def expand_co_retrieve(
    picks: Iterable[str],
    catalog_entries: Iterable[dict],
    *,
    excluded: set[str] | None = None,
) -> list[str]:
    """Expand picks to include companions from ``co_retrieve_for``.

    For each picked entry with ``co_retrieve_for: [<filename>, ...]``,
    the listed companions in the same corpus are added to the picks.
    Filenames not present in the corpus are silently skipped. Entries
    in ``excluded`` are never re-introduced.

    The kit also used ``co_retrieve_for`` with corpus-name strings
    ("diary", "skills"); those are silently skipped here because we
    expand within a single corpus only.
    """
    excluded = excluded or set()
    picks_list = [p for p in picks if p]
    pick_set = set(picks_list)

    by_filename: dict[str, dict] = {
        _entry_filename(e): e for e in catalog_entries if _entry_filename(e)
    }

    expanded = [p for p in picks_list if p not in excluded]
    for filename in picks_list:
        entry = by_filename.get(filename)
        if not entry:
            continue
        companions = entry.get("co_retrieve_for") or []
        if not isinstance(companions, list):
            continue
        for companion in companions:
            if not isinstance(companion, str):
                continue
            companion = companion.strip()
            if not companion or companion in pick_set or companion in excluded:
                continue
            if companion in by_filename:
                expanded.append(companion)
                pick_set.add(companion)
    return expanded


def expand_picks(
    picks: Iterable[str],
    catalog_entries: Iterable[dict],
    *,
    excluded: set[str] | None = None,
) -> list[str]:
    """Apply bundle expansion then co_retrieve expansion.

    Order matters: bundles widen the pick set first (a bundle sibling
    might itself declare co_retrieve companions), then co_retrieve
    pulls in companions for the widened set.

    **Expansion never crosses a bank boundary.** Both mechanisms are driven by
    fields a *bank committer* controls in that bank's own ``catalog.json``, so a
    bank that declares ``"bundle": "identity"`` — squatting a bundle name the
    personal corpus already uses — would have its file pulled into every prompt
    that picked any personal member of that bundle, from then on. The content is
    still fenced as untrusted, so this is persistence and amplification rather
    than a fence break; but "the router picks bank content on relevance alone"
    stops being true, and the lever is entirely bank-controlled.

    Filtering afterwards rather than inside each expander keeps the rule in one
    place and applies it to both mechanisms identically.
    """
    original = [p for p in picks if p]
    # Materialised once: it is now walked three times, and a generator would be
    # empty by the second pass.
    entries = list(catalog_entries)
    after_bundles = expand_bundles(original, entries, excluded=excluded)
    expanded = expand_co_retrieve(after_bundles, entries, excluded=excluded)
    return _same_bank_only(original, expanded, entries)


def _bank_of_entry(entry: dict) -> str:
    """The bank a catalog entry belongs to. Mirrors ``router_prompt._bank_of``."""
    declared = str(entry.get("bank") or "").strip()
    if declared:
        return declared
    ref = str(_entry_filename(entry) or "")
    return ref.split("/", 1)[0] if "/" in ref else "personal"


def _bank_of_pick(pick: str) -> str:
    """The bank a pick names, from its ref prefix."""
    text = str(pick or "")
    return text.split("/", 1)[0] if "/" in text else "personal"


def _same_bank_only(
    original: list[str], expanded: list[str], catalog_entries: Iterable[dict]
) -> list[str]:
    """Drop expansion results whose bank no original pick belongs to.

    Originally-picked entries are always kept — the router chose them, and this
    function is about what *metadata* dragged in, not about what was picked.
    """
    picked_banks = {_bank_of_pick(p) for p in original}
    banks_by_ref = {
        _entry_filename(e): _bank_of_entry(e)
        for e in catalog_entries
        if _entry_filename(e)
    }
    originals = set(original)
    kept: list[str] = []
    for ref in expanded:
        if ref in originals:
            kept.append(ref)
            continue
        bank = banks_by_ref.get(ref) or _bank_of_pick(ref)
        if bank in picked_banks:
            kept.append(ref)
    return kept
