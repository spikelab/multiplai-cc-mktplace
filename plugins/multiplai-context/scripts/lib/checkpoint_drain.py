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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from lib import checkpoint as cp
from lib.extraction_drain import DrainResult, claim_pending_markers
from lib.runtime import uv_run_argv

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
    """
    if not writer_script.exists():
        return DrainResult(0, 0)

    pdir = pending_dir(data_dir)
    procdir = processing_dir(data_dir)

    launched = 0
    children: list[subprocess.Popen] = []
    for dest, payload in claim_pending_markers(pdir, procdir):
        # Single-flight against a live writer. writing.marker is a liveness
        # signal with its own staleness window (checkpoint._WRITER_STALE_S):
        # a fresh one means a writer spawned by a hook is mid-write for this
        # session right now, and launching a second child here would have
        # both racing the same state.json / checkpoint.md. Requeue the
        # marker; a later drain picks it up once the writer finishes or its
        # marker goes stale.
        session_id = str(payload.get("session_id") or "")
        if session_id and cp.writer_inflight(data_dir, session_id):
            logger.info(
                "Writer in flight for %s; requeueing its end-of-session marker",
                session_id,
            )
            try:
                os.replace(str(dest), str(pdir / dest.name))
            except OSError:
                logger.exception(
                    "Could not requeue marker %s past in-flight writer", dest.name
                )
            continue
        payload["marker_path"] = str(dest)

        try:
            proc = subprocess.Popen(
                uv_run_argv(writer_script),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None if wait else subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            logger.exception("Failed to launch queued checkpoint writer")
            try:
                os.replace(str(dest), str(pdir / dest.name))
            except OSError:
                logger.exception("Could not requeue checkpoint marker after launch failure")
            continue

        # A child now exists, so a failure must NOT requeue: the marker belongs
        # to that child, and requeueing would let a second drain launch a
        # duplicate. A broken pipe leaves it for recover_stale_processing.
        try:
            if proc.stdin is not None:
                proc.stdin.write(json.dumps(payload).encode("utf-8"))
                proc.stdin.close()
        except OSError:
            logger.warning(
                "Checkpoint child for %s died before reading its payload; "
                "leaving marker for stale recovery", dest.name,
            )
        children.append(proc)
        launched += 1

    failed = 0
    if wait:
        for proc in children:
            try:
                if proc.wait() != 0:
                    failed += 1
            except Exception:
                logger.exception("Waiting on checkpoint child failed")
                failed += 1

    return DrainResult(launched, failed)
