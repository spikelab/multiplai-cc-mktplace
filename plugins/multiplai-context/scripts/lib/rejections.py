"""The persistent record of what the judge refused to put into memory.

Delegating a decision to a model is only worth anything if the refusals can be
audited. A judge that silently discards items is indistinguishable from a judge
that silently discards *good* items, and the difference is the whole question of
whether it deserves the delegation. So every ``drop`` is written here, in full,
forever — one JSON object per line in ``.multiplai/data/rejections.jsonl``.

**Only ``drop`` is logged.** ``review`` is deferred work: it stays in the
proposal, in front of the human, where it belongs. Merging the two would hide
real pending items inside a list that gets skimmed — which is precisely the
review fatigue this whole programme exists to remove.

**A dropped item is not deleted from history.** ``drop`` means "not promoted to
memory", not "erased". The record carries the item's content hash, so the source
learning is still findable through the ledger, and the item's text is stored
verbatim so a rejection can be read and overruled without going back to the
proposal at all.

Appending, not rewriting. A read-modify-write of a growing log is a file two
concurrent dream runs can truncate between them; ``O_APPEND`` on a line-oriented
file is atomic for the writes this makes, and a torn line loses one record
rather than the file. (``learnings_ledger`` uses temp-file-plus-rename instead
because it rewrites a whole JSON document; the pattern follows the shape of the
data, not habit.)
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

__all__ = [
    "REJECTIONS_FILENAME",
    "default_path",
    "record_for",
    "append",
    "read",
    "aggregate",
]

REJECTIONS_FILENAME = "rejections.jsonl"

# Bound on the stored text. A dropped item is one memory bullet; the cap exists
# so a pathological drafter cannot turn an append-only log into a disk problem.
_TEXT_LIMIT = 4000


def default_path(data_dir: Path) -> Path:
    return Path(data_dir) / REJECTIONS_FILENAME


def record_for(
    item,
    *,
    proposal: str,
    reason: str,
    judge_reason: str = "",
    item_key: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Build the record for one dropped *item*.

    *reason* is the machine-readable reason code (``redundant``,
    ``judge-drop``); *judge_reason* is the judge's own one-line English, which
    is what a human actually reads when deciding to overrule.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "ts": stamp,
        "item_key": item_key,
        "proposal": proposal,
        "target": getattr(item, "target", ""),
        "number": getattr(item, "number", 0),
        "title": getattr(item, "title", ""),
        "section": getattr(item, "section", ""),
        "text": (getattr(item, "text", "") or "")[:_TEXT_LIMIT],
        "provenance": getattr(item, "provenance", ""),
        "kind": getattr(item, "kind", ""),
        "source": getattr(item, "source", ""),
        "reason": reason,
        "judge_reason": judge_reason,
    }


def append(path: Path, records: Iterable[Mapping]) -> int:
    """Append *records* as JSON lines. Returns how many were written.

    Best-effort by contract: the caller has already applied or refused the
    items, and losing the log must never undo that. An unwritable log is
    reported by the exception it raises to the caller, which logs and carries
    on — it does not roll anything back.
    """
    records = list(records)
    if not records:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(records)


def read(path: Path) -> list[dict]:
    """Every readable record, oldest first. Unparseable lines are skipped.

    A truncated final line — the one failure mode an append-only log actually
    has — costs that record and nothing else.
    """
    return list(_iter_records(path))


def _iter_records(path: Path) -> Iterator[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def aggregate(records: Iterable[Mapping]) -> dict[str, int]:
    """Counts by reason code, for the receipt's grouped view."""
    return dict(Counter(str(r.get("reason", "")) or "(unknown)" for r in records))
