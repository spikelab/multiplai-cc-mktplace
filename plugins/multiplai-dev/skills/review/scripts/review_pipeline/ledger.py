"""Where reviews go, and the append-only ledger of every review run.

Reviews are machine data that accumulate, so they live in core's data
directory (`multiplai_core.paths.get_paths().data_dir`), under `reviews/`:
every pipeline, worktree and session resolves the same root without guessing
a path. `--out` still overrides where a review's directory goes.

`<data_dir>/reviews/ledger.jsonl` gets one line when a run starts
(`event: "started"`) and one when it ends (`event: "finished"`), for every
run: a review, a resume, each target of a batch, a run stopped by the budget
or by the verifier. A run killed before it could write its end line leaves a
`started` line with nothing after it for the same `target_dir` and
`head_sha`: `interrupted()` finds it, and a plain `review` of that target
refuses to start over. `migrate` adds `event: "migrated"` lines. The file is
only ever opened for appending; nothing rewrites or truncates it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REVIEWS_DIR = "reviews"
LEDGER_FILE = "ledger.jsonl"
LEGACY_DIR = "legacy"


def data_dir() -> Path:
    """Core's data directory: `$WORKSPACE/.multiplai/data` in a workspace."""
    from multiplai_core.paths import get_paths

    return Path(get_paths().data_dir)


def reviews_root() -> Path:
    """`<data_dir>/reviews`: the default output directory and the ledger's home."""
    return data_dir() / REVIEWS_DIR


def ledger_path() -> Path:
    return reviews_root() / LEDGER_FILE


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(entry: dict) -> None:
    """Append one JSON line. A failed write is logged, never raised: the ledger must not stop a review."""
    path = ledger_path()
    line = (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as e:
        log.error("cannot append to the review ledger %s: %s", path, e)


def read() -> list[dict]:
    """Every readable line, in file order; a line that does not parse is skipped with a warning."""
    path = ledger_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("cannot read the review ledger %s: %s", path, e)
        return []
    entries = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            log.warning("skipping line %d of %s: not JSON", n, path)
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def interrupted(target_dir: Path, head_sha: str) -> bool:
    """True when the last ledger line for this directory and head is a `started` line."""
    last = None
    for entry in read():
        if entry.get("target_dir") == str(target_dir) and entry.get("head_sha") == head_sha \
                and entry.get("event") in ("started", "finished"):
            last = entry["event"]
    return last == "started"


def started(*, slug: str, target_dir: Path, head_sha: str, session_id: str) -> None:
    append({"event": "started", "generated_at": now(), "slug": slug, "target_dir": str(target_dir),
            "head_sha": head_sha, "session_id": session_id})


def finished(*, producer: str, session_id: str, slug: str, target_dir: Path, round_: str, head_sha: str,
             base_sha: str, kind: str, pr: int | None, stopped: str | None, run: dict | None) -> None:
    append({"event": "finished", "generated_at": now(), "producer": producer, "session_id": session_id,
            "slug": slug, "target_dir": str(target_dir), "round": round_, "head_sha": head_sha,
            "base_sha": base_sha, "kind": kind, "pr": pr, "stopped": stopped, "run": run})
