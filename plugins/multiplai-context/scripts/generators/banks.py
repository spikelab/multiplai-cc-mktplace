"""Shared-bank catalog generator — merges committed fragments, no LLM.

The personal corpus keeps its own generator (``generators/memory.py``) and its
own catalog (``memory.json``) untouched. This one produces ``banks.json``: the
same entry shape, one extra ``bank`` field, for every file in every *shared*
bank. ``context_manager`` reads both and hands the union to the router, which
scores a bank entry exactly like a personal one — relevance is relevance.

## Why shared banks commit their catalog

A catalog entry is an LLM summary. Regenerating one per subscriber would cost
every member of a team a model pass over content none of them wrote, and would
yield slightly *different* routing for each of them from identical files —
which makes "why did it not load the deployment notes for me?" unanswerable.

So a bank commits a ``catalog.json`` fragment, and whoever lands a content
change regenerates it. Consumers adopt it verbatim. Identical content,
identical routing, one model pass per change instead of one per person.

## Degradation when a bank has no fragment

A bank you have just cloned, or one whose owner has not regenerated, still has
to route. It gets a **deterministic** entry per file: the first paragraph as
the summary, H2 names as section anchors, no keywords and no declared domains.
That routes weakly — token overlap against a summary — but it routes, it needs
no model, and it cannot fail. The missing fragment is reported on every run so
it does not become the silent steady state.

There is deliberately **no LLM path here at all**. A bank sync is a background
event; making it able to spend money and take minutes would put a model call
on the session-start path, which the degradation contract forbids.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from multiplai_core.log_utils import log_event
from multiplai_core.paths import Paths

from generators.base import CATALOG_SCHEMA_VERSION, GenerationResult
from generators.memory import MemoryGenerator
from lib.bank_collisions import find_collisions, render_report, to_json
from lib.banks import CATALOG_FRAGMENT, MemoryBank, shared_banks
from lib.section_loader import h2_names

logger = logging.getLogger(__name__)

__all__ = ["BANKS_CATALOG", "BanksGenerator", "COLLISION_REPORT", "derive_entry"]

#: The catalog this generator writes.
BANKS_CATALOG = "banks.json"

#: Where the human-readable collision report lands.
COLLISION_REPORT = "bank-collisions.md"

#: Files in a bank that are not memory content.
_NOT_CONTENT = {"catalog.json", "readme.md", "bank.md"}

#: Cap on the derived summary, so a bank with no fragment cannot bloat the
#: catalog the router reads on every prompt.
_SUMMARY_CHARS = 320


def _first_paragraph(text: str) -> str:
    """The file's first prose paragraph, headings and metadata lines skipped."""
    para: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if para:
                break
            continue
        if line.startswith("#") or line.startswith(">"):
            if para:
                break
            continue
        if line.startswith("**Last Updated:**"):
            continue
        para.append(line)
    summary = " ".join(para)
    if len(summary) > _SUMMARY_CHARS:
        summary = summary[:_SUMMARY_CHARS].rstrip() + "…"
    return summary


def derive_entry(bank: MemoryBank, path: Path, text: str) -> dict[str, Any]:
    """A model-free catalog entry for one bank file.

    Carries only what can be read off the file itself. ``intent_domains`` is
    deliberately absent rather than guessed: an invented domain would make the
    router confident about a claim nobody made, and the collision detector
    reads the same field.
    """
    entry: dict[str, Any] = {
        "source": bank.ref(path.name),
        "bank": bank.name,
        "summary": _first_paragraph(text) or f"{path.name} from shared bank {bank.name}",
        "topics": [],
        "keywords": [],
        "derived": True,
    }
    sections = MemoryGenerator.anchorable_sections(text)
    if sections:
        entry["section_anchors"] = [{"name": name} for name in sections]
    else:
        # Below the anchoring thresholds the whole file is one pick's worth of
        # context, so H2 names are recorded as plain sections for the router's
        # metadata use and nothing else.
        names = h2_names(text)
        if names:
            entry["sections"] = names
    return entry


def _bank_files(bank: MemoryBank) -> list[Path]:
    if not bank.path.exists() or not bank.path.is_dir():
        return []
    return [
        p
        for p in sorted(bank.path.glob("*.md"))
        if p.is_file() and p.name.lower() not in _NOT_CONTENT
    ]


def _read_fragment(bank: MemoryBank) -> Optional[list[dict]]:
    """The bank's committed entries, or ``None`` when there is no usable fragment."""
    fragment = bank.path / CATALOG_FRAGMENT
    if not fragment.exists():
        return None
    try:
        data = json.loads(fragment.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Bank %s has an unreadable %s — deriving entries instead",
                       bank.name, CATALOG_FRAGMENT)
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        logger.warning("Bank %s %s has no entries list — deriving instead",
                       bank.name, CATALOG_FRAGMENT)
        return None
    return [e for e in entries if isinstance(e, dict)]


def _adopt_fragment_entry(bank: MemoryBank, entry: dict, present: set[str]) -> Optional[dict]:
    """Re-key one committed entry onto this consumer's bank namespace.

    The bank authored ``"source": "dev.md"``; every consumer needs
    ``"<bank>/dev.md"`` so the ref is unambiguous once merged. A fragment
    entry naming a file the bank does not (any longer) contain is dropped —
    the working tree is the authority on what exists, not the fragment.
    """
    raw = str(entry.get("source") or entry.get("path") or entry.get("file") or "").strip()
    filename = raw.rsplit("/", 1)[-1]
    if not filename or filename not in present:
        return None
    adopted = {k: v for k, v in entry.items() if k not in ("path", "file")}
    adopted["source"] = bank.ref(filename)
    adopted["bank"] = bank.name
    adopted.pop("derived", None)
    return adopted


class BanksGenerator:
    """Builds ``banks.json`` from the shared banks' own committed catalogs.

    Duck-typed to the dispatcher's generator protocol (``name``,
    ``catalog_filename``, ``run``) but deliberately **not** a
    :class:`~generators.base.GeneratorBase` subclass: that template method is
    "one LLM call per changed source", and this generator makes no LLM calls
    at all. Sharing the base class would mean carrying a model client, a
    per-source hash state and a retry policy for work that has none of those.
    """

    name = "banks"
    catalog_filename = BANKS_CATALOG

    def __init__(self, *, config=None, model_client=None):
        # Accepted for uniform dispatch; neither is used. A model client here
        # would be a session-start model call, which the degradation contract
        # forbids — see the module docstring.
        self._config = config

    @property
    def _catalogs_dir(self) -> Path:
        return Paths.resolve().catalogs_dir()

    def _personal_entries(self) -> list[dict]:
        """The personal catalog's entries, for collision detection only."""
        path = self._catalogs_dir / "memory.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        out = []
        for entry in entries:
            if isinstance(entry, dict):
                out.append({**entry, "bank": entry.get("bank") or "personal"})
        return out

    async def run(
        self, *, force: bool = False, dry_run: bool = False, force_enable: bool = False
    ) -> GenerationResult:
        """Rebuild ``banks.json``. Cheap, deterministic, always safe to re-run.

        ``force`` is accepted and ignored: with no model calls and no
        per-source hashes there is nothing to skip, so every run is a full
        rebuild. That is also why a bank content change needs no cache
        invalidation to take effect.
        """
        banks = shared_banks()
        if not banks:
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0, generated=0,
                pruned=0, errors=[], dry_run=dry_run,
            )

        entries: list[dict] = []
        texts: dict[str, str] = {}
        errors: list[str] = []
        total = 0
        derived = 0

        for bank in banks:
            files = _bank_files(bank)
            if not bank.path.exists():
                errors.append(
                    f"bank '{bank.name}' is not present at {bank.path} — "
                    "run `/memory-bank sync` (or clone it) before it can route"
                )
                continue
            total += len(files)
            present = {p.name for p in files}
            for path in files:
                try:
                    texts[bank.ref(path.name)] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    errors.append(f"bank '{bank.name}': could not read {path.name}")

            fragment = _read_fragment(bank)
            adopted: list[dict] = []
            if fragment is not None:
                for entry in fragment:
                    kept = _adopt_fragment_entry(bank, entry, present)
                    if kept is not None:
                        adopted.append(kept)
            covered = {str(e.get("source")) for e in adopted}
            for path in files:
                ref = bank.ref(path.name)
                if ref in covered:
                    continue
                text = texts.get(ref)
                if text is None:
                    continue
                adopted.append(derive_entry(bank, path, text))
                derived += 1
            if fragment is None and files:
                errors.append(
                    f"bank '{bank.name}' ships no {CATALOG_FRAGMENT} — routing "
                    f"its {len(files)} file(s) from derived summaries only; ask "
                    "its owner to regenerate and commit the fragment"
                )
            entries.extend(adopted)

        collisions = find_collisions(self._personal_entries() + entries, texts=texts)

        if not dry_run:
            self._write(entries, collisions)

        if collisions:
            log_event(
                "catalog", "bank-collisions",
                f"{len(collisions)} cross-bank collision(s) — a fact should live "
                "in exactly one bank",
                level="WARNING", count=len(collisions),
            )
            logger.warning(
                "%d cross-bank collision(s); see %s",
                len(collisions), self._catalogs_dir / COLLISION_REPORT,
            )

        return GenerationResult(
            generator=self.name,
            total_sources=total,
            skipped=max(total - derived, 0),
            generated=len(entries),
            pruned=0,
            errors=errors,
            dry_run=dry_run,
        )

    def _write(self, entries: list[dict], collisions) -> None:
        self._catalogs_dir.mkdir(parents=True, exist_ok=True)
        catalog = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
            "collisions": to_json(collisions),
        }
        (self._catalogs_dir / self.catalog_filename).write_text(
            json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
        )
        report = self._catalogs_dir / COLLISION_REPORT
        if collisions:
            report.write_text(render_report(collisions), encoding="utf-8")
        elif report.exists():
            # A stale report claiming resolved collisions is worse than none.
            try:
                report.unlink()
            except OSError:
                pass
