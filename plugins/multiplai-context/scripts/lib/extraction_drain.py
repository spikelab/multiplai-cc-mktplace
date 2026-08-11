"""Deferred-extraction drain — the one place markers are dequeued.

``session_end.py`` / ``pre_compact.py`` are kill-within-seconds hooks, so they
do not run extraction themselves: they drop a JSON *marker* into
``<data_dir>/pending_extractions/`` naming the session and its transcript.
Something later has to pick those markers up and launch the real (multi-minute,
LLM-backed) ``extract_learnings.py``. This module is that something.

Two callers, one code path — this is the whole point of the module:

* ``session_start.py`` — the in-container drain. Historically the *only*
  consumer, which meant a marker written when the last tab closed on Friday
  sat untouched until a session opened on Monday.
* ``drain_extractions.py`` — a standalone entry point the launcher runs on the
  host *after* the container exits, so the walk-away moment produces its diary
  entry that evening.

The dequeue is an atomic rename from ``pending_extractions/`` to
``processing_extractions/``, so the two callers racing each other (a host drain
and a fresh session start firing at the same moment) hand each marker to at
most one of them. The rename alone is *not* enough, though: ``os.rename``
preserves mtime, and staleness recovery measures marker age by mtime — so the
dequeue also refreshes the marker's mtime, making "stale" mean "launched and
not finished", never "written long ago". Recovery itself claims a marker by
atomic rename before touching it, so two recoverers can't double-increment
its attempt count.

Best-effort throughout. Every function returns rather than raises — the
in-container caller is a hook that must never break a session start.
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from lib.runtime import uv_run_argv

logger = logging.getLogger(__name__)

# Retry policy for markers left in ``processing_extractions/`` (and, via
# ``lib.checkpoint_drain``, in ``processing_checkpoints/``). Markers older
# than this with no completion are assumed orphaned and requeued, capped at
# MAX_ATTEMPTS before being quarantined.
#
# Derived from the slowest legitimate child, not from a hunch: the checkpoint
# child runs ``run_agent(timeout_s=cfg.timeout_s (600), max_attempts=2)`` with
# the timeout applied per attempt plus retry backoff, so a slow-but-alive
# writer can legitimately run past 1200s; the extraction child has no
# wall-clock bound at all (one LLM call per transcript chunk). The previous
# 900s window sat *inside* that range, so a live child could be declared
# stale, its marker requeued, and a duplicate launched against the same
# state.json — paying the model cost twice. 1800s clears the checkpoint
# worst case with margin; a genuinely dead marker waiting half an hour for
# recovery costs nothing.
STALE_SECONDS = 1800
MAX_ATTEMPTS = 3


class DrainResult(NamedTuple):
    """What a drain pass did.

    ``launched`` is the number of extraction children spawned. ``failed`` is
    how many of them exited nonzero — only ever counted with ``wait=True``,
    because a fire-and-forget caller never reaps its children (it is always
    0 without ``wait``).
    """

    launched: int
    failed: int


def recover_stale_processing(
    processing_dir: Path, pending_dir: Path, failed_dir: Path | None = None
) -> None:
    """Requeue (or fail) markers stuck in ``processing_extractions/``.

    A detached extraction child deletes its own marker on success. If the
    child died (venv re-exec failure, crash, no model client) the marker
    lingers here. Markers older than the stale window are requeued for
    retry, capped at ``MAX_ATTEMPTS`` before being moved to *failed_dir*
    so a permanently-bad transcript can't loop forever and stays visible
    for debugging.

    *failed_dir* must be passed per queue (M12): this helper serves the
    checkpoint queue too, and the old derived default filed dead
    *checkpoint* markers under ``failed_extractions/`` — indistinguishable
    from failed extractions to log-doctor and to anyone debugging.
    Defaults to ``failed_extractions/`` beside the queue for compatibility
    with extraction-queue callers.
    """
    if not processing_dir.exists():
        return
    if failed_dir is None:
        failed_dir = processing_dir.parent / "failed_extractions"
    now = time.time()
    for m in list(processing_dir.glob("*.json")):
        try:
            if now - m.stat().st_mtime < STALE_SECONDS:
                continue  # a live child may still be working on it
        except OSError:
            continue
        # Claim the marker by atomic rename BEFORE touching its contents.
        # Two drains can both see the same stale marker; exactly one wins
        # this rename, the loser gets FileNotFoundError and moves on.
        # Rewriting ``m`` in place here (the pre-0.11.0 behavior) let the
        # loser's write_text *recreate* the file after the winner had
        # already requeued it, double-incrementing ``attempts`` toward the
        # quarantine cap. The claim name doesn't match the ``*.json`` glob,
        # so no other pass ever sees it.
        claim = m.with_name(f"{m.name}.recovering.{os.getpid()}")
        try:
            os.rename(str(m), str(claim))
        except OSError:
            continue  # another drain claimed it first
        try:
            data = json.loads(claim.read_text())
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
        attempts = int(data.get("attempts", 0)) + 1
        data["attempts"] = attempts
        try:
            claim.write_text(json.dumps(data, indent=2))
            if attempts > MAX_ATTEMPTS:
                failed_dir.mkdir(parents=True, exist_ok=True)
                os.replace(str(claim), str(failed_dir / m.name))
                logger.warning(
                    "Deferred extraction permanently failed after %d attempts: %s",
                    attempts - 1, m.name,
                )
            else:
                os.replace(str(claim), str(pending_dir / m.name))
                logger.info(
                    "Requeued stale extraction marker (attempt %d): %s",
                    attempts, m.name,
                )
        except OSError:
            logger.exception("Could not recover stale marker %s", m.name)
            # Put the claim back under its queue name so the marker isn't
            # stranded under a name no drain will ever glob.
            try:
                os.replace(str(claim), str(m))
            except OSError:
                pass


def claim_pending_markers(
    pending_dir: Path, processing_dir: Path, failed_dir: Path | None = None
):
    """Dequeue every marker in *pending_dir*, yielding ``(path, payload)``.

    **The one implementation of the dequeue**, used by this module's
    extraction drain and by ``lib.checkpoint_drain``'s end-of-session
    checkpoint drain. Both queues need the identical three steps and a second
    copy would drift:

    * atomic rename into *processing_dir*, so two drains racing hand each
      marker to at most one of them;
    * ``dest.touch()``, because ``os.rename`` preserves mtime and staleness
      recovery measures age by mtime — without it a marker written on Friday
      and drained on Monday looks stale the instant it is claimed, and a
      concurrent recovery launches a duplicate child;
    * a parse, discarding anything unreadable (it will never succeed).

    Stale markers left by a dead child are recovered into *pending_dir* first,
    so this pass picks them up. *failed_dir* is where recovery quarantines a
    marker that exhausted its retries — pass the right one per queue (M12).

    The caller owns what happens next, including requeueing on a launch
    failure — that decision differs per queue.
    """
    try:
        pending_dir.mkdir(parents=True, exist_ok=True)
        processing_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    recover_stale_processing(processing_dir, pending_dir, failed_dir)

    for marker_file in list(pending_dir.glob("*.json")):
        dest = processing_dir / marker_file.name
        try:
            os.rename(str(marker_file), str(dest))
        except OSError:
            continue
        try:
            dest.touch()
        except OSError:
            logger.warning("Could not refresh mtime on %s", dest.name)
        try:
            payload = json.loads(dest.read_text())
            if not isinstance(payload, dict):
                raise ValueError(dest.name)
        except (json.JSONDecodeError, OSError, ValueError):
            # Unparseable marker will never succeed — discard it.
            dest.unlink(missing_ok=True)
            continue
        yield dest, payload


def pending_count(data_dir: Path) -> int:
    """How many markers are waiting in ``pending_extractions/``.

    Cheap enough for a caller that wants to decide whether launching a drain
    is worth it at all (the launcher checks this before spending a process).

    **Not the whole queue.** A marker orphaned by a container that died
    mid-extraction sits in ``processing_extractions/`` and is invisible here
    until :func:`recover_stale_processing` requeues it — so a caller deciding
    whether there is work to do must consult :func:`processing_count` as well.
    """
    try:
        return len(list((data_dir / "pending_extractions").glob("*.json")))
    except OSError:
        return 0


def processing_count(data_dir: Path) -> int:
    """How many markers are in flight in ``processing_extractions/``.

    Non-zero means either a live child is working (normal, transient) or a
    child died and left its marker behind — which is real work, recoverable
    only through :func:`recover_stale_processing`. Reporting it separately
    from :func:`pending_count` is what stops a drain that is about to recover
    an orphan from announcing "0 marker(s) pending" first.
    """
    try:
        return len(list((data_dir / "processing_extractions").glob("*.json")))
    except OSError:
        return 0


def _launch_queue(
    pending_dir: Path,
    processing_dir: Path,
    script: Path,
    *,
    failed_dir: Path,
    build_payload,
    should_skip=None,
    wait: bool = False,
) -> DrainResult:
    """The launch loop both queues share: claim, spawn, pipe, account.

    This used to exist twice — here and in ``lib.checkpoint_drain`` —
    duplicated verbatim, which is how the checkpoint copy quietly kept the
    wrong ``failed_dir`` (M12). One implementation, two parameterizations:

    * *build_payload(dest, marker)* → the dict piped to the child's stdin.
    * *should_skip(marker)* → a reason string to requeue the marker without
      launching (or ``None``); the checkpoint queue uses it to defer to an
      in-flight writer.
    * *failed_dir* — where stale recovery quarantines exhausted markers,
      named per queue.

    With *wait*, block until every launched child exits and let its stderr
    through to this process's stderr. Hooks never do this — they must return
    in seconds — but a human running the drain by hand needs to see whether
    the children actually worked.
    """
    if not script.exists():
        return DrainResult(0, 0)

    launched = 0
    children: list[subprocess.Popen] = []
    # The dequeue itself (rename, mtime refresh, stale recovery, parse) lives
    # in claim_pending_markers — see its docstring for why it is shared.
    for dest, marker in claim_pending_markers(pending_dir, processing_dir, failed_dir):
        if should_skip is not None:
            reason = should_skip(marker)
            if reason:
                logger.info("%s; requeueing %s", reason, dest.name)
                try:
                    os.replace(str(dest), str(pending_dir / dest.name))
                except OSError:
                    logger.exception("Could not requeue skipped marker %s", dest.name)
                continue
        payload = build_payload(dest, marker)

        try:
            proc = subprocess.Popen(
                uv_run_argv(script),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None if wait else subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            logger.exception("Failed to launch drain child for %s", dest.name)
            # Launch failed — return the marker to the queue so a later
            # drain retries it instead of losing the session.
            try:
                os.replace(str(dest), str(pending_dir / dest.name))
            except OSError:
                logger.exception("Could not requeue marker after launch failure")
            continue

        # From here a child EXISTS, so a failure must NOT requeue: the
        # marker in *processing_dir* now belongs to that child, and
        # requeueing it would let a second drain launch a duplicate.
        # A broken pipe means the child died before reading its payload
        # (e.g. ``uv`` present but interpreter bootstrap failed) — the
        # marker stays put for :func:`recover_stale_processing` to retry.
        try:
            if proc.stdin is not None:
                proc.stdin.write(json.dumps(payload).encode("utf-8"))
                proc.stdin.close()
        except OSError:
            logger.warning(
                "Drain child for %s died before reading its payload; "
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
                logger.exception("Waiting on drain child failed")
                failed += 1

    return DrainResult(launched, failed)


def process_deferred_extractions(
    data_dir: Path, extract_script: Path, *, wait: bool = False
) -> DrainResult:
    """Drain pending extraction markers left by previous SessionEnd hooks.

    Each marker is atomically moved from ``pending_extractions/`` to
    ``processing_extractions/`` and piped (with the transcript, if still
    readable) to a detached ``extract_learnings.py``. The child deletes
    its own marker on success; failed/crashed children leave the marker
    for :func:`recover_stale_processing` to retry. Returns a
    :class:`DrainResult` — ``launched`` markers this run, and (with *wait*
    only) how many children exited nonzero.

    Atomic rename guarantees at-most-once dequeue if two drains race.
    """

    def build_payload(dest: Path, marker: dict) -> dict:
        # Pass the transcript PATH, not its contents: the child distills it
        # into token-bounded chunks before the LLM call. Piping a raw
        # multi-MB transcript here previously forced a single >200K-token
        # request that tripped the long-context billing gate (429).
        return {
            "session_id": marker.get("session_id", ""),
            "cwd": marker.get("cwd", ""),
            "transcript_path": marker.get("transcript_path", ""),
            # What enqueued this marker ("pre_compact" vs a session-end path).
            # The child needs it: a PreCompact-deferred extraction runs against
            # a session that is STILL LIVE after compaction, so its checkpoint
            # must not be retired the way a finished session's is.
            "trigger": marker.get("trigger", ""),
            # The child removes this marker once the session is handled.
            "marker_path": str(dest),
        }

    return _launch_queue(
        data_dir / "pending_extractions",
        data_dir / "processing_extractions",
        extract_script,
        failed_dir=data_dir / "failed_extractions",
        build_payload=build_payload,
        wait=wait,
    )
