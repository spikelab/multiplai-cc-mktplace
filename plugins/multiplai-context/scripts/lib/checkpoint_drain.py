"""Deferred *checkpoint* queue — the half of SessionEnd a detached spawn can't do.

``session_end.py`` spawns the checkpoint writer directly when the reason means
the container keeps running (``clear``, ``resume``). For every other reason the
container is exiting under ``docker run --rm``, so a detached child is killed
with PID 1 and a spawn would be a lie. Those ends drop a JSON marker into
``<data_dir>/pending_checkpoints/`` instead, and this module is what turns the
marker into a real ``checkpoint.md`` — from the host, after the container is
gone.

The dequeue is not reimplemented here: it is
:func:`lib.extraction_drain.claim_pending_markers`, the same atomic rename,
mtime refresh and stale-marker recovery the extraction queue uses. Two queues,
one implementation of the thing that makes a queue safe.

Best-effort throughout: every function returns rather than raises. The writer
never producing a file costs a rebuild; an exception escaping into
``session_end.py`` or the launcher's drain costs the diary too.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from lib import checkpoint as cp
from lib.extraction_drain import DrainResult, _launch_queue

logger = logging.getLogger(__name__)


def pending_dir(data_dir: Path) -> Path:
    return data_dir / "pending_checkpoints"


def processing_dir(data_dir: Path) -> Path:
    return data_dir / "processing_checkpoints"


def pending_checkpoint_count(data_dir: Path) -> int:
    """How many end-of-session checkpoints are waiting to be written."""
    try:
        return len(list(pending_dir(data_dir).glob("*.json")))
    except OSError:
        return 0


def queue_pending_checkpoint(data_dir: Path, payload: dict) -> Path | None:
    """Record that *payload*'s session needs a checkpoint written for it.

    Keyed by session id, so a session that somehow ends twice queues one job
    rather than two. Returns the marker path, or None if it could not be
    written.
    """
    session_id = str(payload.get("session_id") or "")
    if not session_id or "/" in session_id:
        return None
    pdir = pending_dir(data_dir)
    marker = pdir / f"{session_id}.json"
    record = dict(payload)
    record["queued_at"] = datetime.now(timezone.utc).isoformat()
    try:
        pdir.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(marker))
    except OSError:
        logger.exception("Could not queue pending checkpoint for %s", session_id)
        return None
    return marker


def process_pending_checkpoints(
    data_dir: Path, writer_script: Path, *, wait: bool = False
) -> DrainResult:
    """Launch ``checkpoint_writer.py`` for every queued end-of-session marker.

    The child is handed the marker's own payload plus ``marker_path`` so it can
    delete it on the way out. With *wait*, block on each child and count the
    nonzero exits — for a human running the drain by hand, never for a hook.

    The launch loop is :func:`lib.extraction_drain._launch_queue` — the same
    claim/spawn/pipe/account sequence the extraction queue runs, with this
    queue's own ``failed_checkpoints/`` quarantine (M12) and one extra rule:
    a session whose ``writing.marker`` is fresh (a hook-spawned writer is
    mid-write, per ``checkpoint.writer_inflight``'s staleness window) has its
    marker requeued rather than a second child racing the same state.json.
    """

    def build_payload(dest: Path, marker: dict) -> dict:
        payload = dict(marker)
        payload["marker_path"] = str(dest)
        return payload

    def should_skip(marker: dict) -> str | None:
        session_id = str(marker.get("session_id") or "")
        if session_id and cp.writer_inflight(data_dir, session_id):
            return f"Writer in flight for {session_id}"
        return None

    return _launch_queue(
        pending_dir(data_dir),
        processing_dir(data_dir),
        writer_script,
        failed_dir=data_dir / "failed_checkpoints",
        build_payload=build_payload,
        should_skip=should_skip,
        wait=wait,
    )
