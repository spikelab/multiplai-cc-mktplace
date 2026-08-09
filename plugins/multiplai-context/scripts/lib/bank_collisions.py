"""Cross-bank collision detection — a defect report, never a resolution.

The rule the whole banks design rests on is that **a fact lives in exactly one
bank**. Subscribing to a bank means its file *replaces* the user's on that
topic; it is not a second opinion layered on top. So two banks claiming the
same domain is not a conflict to arbitrate at injection time — it is a
**defect in the corpus**, and the only useful moment to say so is when the
catalog is built, before either copy has been injected anywhere.

That is stricter than the July design doc, which surfaced cross-bank
contradictions to the user mid-session. Under no-duplication they should never
reach a session at all.

Three signals, cheapest first:

* **A duplicate filename.** ``dev.md`` in the personal bank and ``dev.md`` in
  a team bank is the collision in its most literal form, and the one adoption
  exists to remove.
* **Overlapping declared domains.** The catalog's ``intent_domains`` are what
  the router matches on, so two entries sharing them are two entries competing
  to answer the same prompt.
* **A duplicate H2 name.** P1's lint already reports this *within* the
  personal corpus and it found a real collision there. Merging another
  corpus in makes it strictly likelier, so the same check runs across banks —
  where it also catches the case a filename check misses: the same section
  living in two differently-named files.

Nothing here edits anything. It reports.
"""

from __future__ import annotations

import dataclasses
from typing import Iterable, Mapping, Optional, Sequence

from lib.banks import PERSONAL_BANK, split_ref
from lib.section_loader import h2_names

__all__ = [
    "COLLISION_KINDS",
    "Collision",
    "find_collisions",
    "render_report",
    "to_json",
]

#: How many normalised ``intent_domains`` two entries must share before the
#: overlap is reported. One shared phrase is ordinary — two unrelated files can
#: both be relevant to "debugging python"; two is a claim on the same territory.
MIN_SHARED_DOMAINS = 2

COLLISION_KINDS: tuple[str, ...] = (
    "duplicate-filename",
    "domain-overlap",
    "duplicate-h2",
)


@dataclasses.dataclass(frozen=True)
class Collision:
    """One reported overlap between entries in two different banks."""

    kind: str
    refs: tuple[str, ...]
    banks: tuple[str, ...]
    detail: str

    @property
    def label(self) -> str:
        return " ↔ ".join(f"`{r}`" for r in self.refs)


def _normalise(phrase: object) -> str:
    return " ".join(str(phrase or "").lower().split())


def _domains(entry: Mapping) -> set[str]:
    raw = entry.get("intent_domains")
    if not isinstance(raw, (list, tuple)):
        return set()
    return {d for d in (_normalise(x) for x in raw) if d}


def _entry_ref(entry: Mapping) -> str:
    for field in ("source", "path", "file"):
        value = entry.get(field)
        if value:
            return str(value)
    return ""


def _bank_of(entry: Mapping, ref: str) -> str:
    declared = str(entry.get("bank") or "").strip()
    if declared:
        return declared
    bank, _, _ = split_ref(ref)
    return bank or PERSONAL_BANK


def _pairs(items: Sequence[tuple[str, str, Mapping]]):
    """Every cross-bank pair, once, in stable order."""
    for i, (ref_a, bank_a, entry_a) in enumerate(items):
        for ref_b, bank_b, entry_b in items[i + 1:]:
            if bank_a == bank_b:
                continue
            yield (ref_a, bank_a, entry_a), (ref_b, bank_b, entry_b)


def find_collisions(
    entries: Iterable[Mapping],
    *,
    texts: Optional[Mapping[str, str]] = None,
    min_shared_domains: int = MIN_SHARED_DOMAINS,
) -> list[Collision]:
    """Every cross-bank overlap in *entries*, in a stable order.

    *entries* are catalog entries from **all** banks, each identified by its
    ``source`` ref and attributed by a ``bank`` field (falling back to the
    ref's own prefix). *texts*, when given, maps a ref to that file's content
    and enables the duplicate-H2 check; without it that check is skipped
    rather than guessed at.

    Same-bank overlaps are not reported here. Inside one corpus they are
    ``lib/memory_lint``'s business and have different remedies.
    """
    items: list[tuple[str, str, Mapping]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        ref = _entry_ref(entry)
        if not ref:
            continue
        items.append((ref, _bank_of(entry, ref), entry))
    items.sort(key=lambda t: (t[1] != PERSONAL_BANK, t[1], t[0]))

    out: list[Collision] = []

    # 1. Duplicate filenames.
    for (ref_a, bank_a, _), (ref_b, bank_b, _) in _pairs(items):
        _, file_a, _ = split_ref(ref_a)
        _, file_b, _ = split_ref(ref_b)
        if file_a and file_a.lower() == file_b.lower():
            out.append(
                Collision(
                    kind="duplicate-filename",
                    refs=(ref_a, ref_b),
                    banks=(bank_a, bank_b),
                    detail=(
                        f"`{file_a}` exists in both `{bank_a}` and `{bank_b}`. "
                        "Under no-duplication exactly one of them owns this "
                        "topic — adopt the bank's copy or rename yours."
                    ),
                )
            )

    # 2. Overlapping declared domains.
    for (ref_a, bank_a, entry_a), (ref_b, bank_b, entry_b) in _pairs(items):
        dom_a, dom_b = _domains(entry_a), _domains(entry_b)
        shared = sorted(dom_a & dom_b)
        if not shared:
            continue
        subsumed = len(shared) == min(len(dom_a), len(dom_b))
        if len(shared) < min_shared_domains and not subsumed:
            continue
        out.append(
            Collision(
                kind="domain-overlap",
                refs=(ref_a, ref_b),
                banks=(bank_a, bank_b),
                detail=(
                    f"both claim {len(shared)} of the same routing domain(s): "
                    + "; ".join(shared)
                ),
            )
        )

    # 3. Duplicate H2 names across banks.
    if texts:
        by_ref = {ref: bank for ref, bank, _ in items}
        seen: dict[str, list[tuple[str, str]]] = {}
        for ref in sorted(texts):
            bank = by_ref.get(ref) or _bank_of({}, ref)
            for name in h2_names(texts[ref] or ""):
                seen.setdefault(name.strip().lower(), []).append((ref, bank))
        for lowered, owners in sorted(seen.items()):
            banks_present = {bank for _, bank in owners}
            if len(owners) < 2 or len(banks_present) < 2:
                continue
            out.append(
                Collision(
                    kind="duplicate-h2",
                    refs=tuple(ref for ref, _ in owners),
                    banks=tuple(sorted(banks_present)),
                    detail=(
                        f'the H2 "{lowered}" appears in files from '
                        f"{len(banks_present)} different banks — a section-level "
                        "pick cannot tell them apart"
                    ),
                )
            )

    return out


def to_json(collisions: Sequence[Collision]) -> list[dict]:
    """Serialisable form, for the ``collisions`` key of a catalog."""
    return [dataclasses.asdict(c) for c in collisions]


def render_report(collisions: Sequence[Collision]) -> str:
    """Human report. Empty string when there is nothing to say."""
    if not collisions:
        return ""
    by_kind: dict[str, list[Collision]] = {}
    for c in collisions:
        by_kind.setdefault(c.kind, []).append(c)
    lines = [
        f"# Cross-bank collisions — {len(collisions)} found",
        "",
        "A fact is meant to live in exactly one bank. Each line below is two "
        "banks claiming the same ground. Nothing has been changed; resolve by "
        "adopting one copy (`/memory-bank adopt`) or by narrowing one entry's "
        "`intent_domains`.",
        "",
    ]
    for kind in COLLISION_KINDS:
        found = by_kind.get(kind)
        if not found:
            continue
        lines.append(f"## {kind} ({len(found)})")
        lines.append("")
        for c in found:
            lines.append(f"- {c.label} — {c.detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
