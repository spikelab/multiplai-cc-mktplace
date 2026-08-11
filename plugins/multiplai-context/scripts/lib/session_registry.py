"""Session registry writes for the multiplai hub ("hub input contract").

The multiplai hub (the multiplai-gui repo, ``docs/api-contract.md`` → "Hub
input contract") discovers live sessions from per-session JSON entries written by
the lifecycle hooks — hooks are the source of truth because Claude Code keeps
no open fd on its transcript JSONL, so file ownership is undiscoverable by
inspection. Entries live at ``<data_dir>/sessions/<session_id>.json``:

    {session_id, hostname, in_container, cwd, project?, workspace, started_at,
     last_event: {ts, kind: start|stop|notification|end}}

``hostname`` equals the container name in kit containers ($HOSTNAME) and the
plain machine hostname otherwise — it is how the launcher wrapper maps a
container back to its session. ``in_container`` says which of those two it is:
the string alone cannot tell you, and the fleet view uses it to decide whether
this entry may be judged against the host's live-container roster at all.

The hub additionally writes
``<session_id>.adopt`` markers beside the entries; this module never touches
those beyond GC of orphans, and updates preserve any keys it doesn't own
(read-merge-write) so hub-written fields survive. Concurrent writers
serialize on an advisory flock of ``<session_id>.lock`` beside the entry —
a hub that edits entries must take the same lock, or a racing hook update
can silently revert its keys. The lock is best-effort/fail-open (readonly
or flock-less filesystems proceed unlocked) because crash-safety beats
consistency inside a hook.

Degradation: with no hub installed the files are simply never read. Every
public function is best-effort — it returns rather than raises, because it
runs inside kill-within-seconds hooks that must never break a session.
"""

import fcntl
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.fsio import atomic_write_json

logger = logging.getLogger(__name__)

# Registry entries whose session ended more than this long ago are GC'd on
# SessionStart (per the hub input contract).
GC_AFTER_DAYS = 7

# Fallback age-out for entries that never saw a SessionEnd. Only a clean quit
# fires that hook; a container killed by a reboot, a closed terminal, `docker
# kill` or the OOM killer records nothing at all, and without this those would
# accumulate forever as ghost "idle" sessions.
# Generous window: an idle session with no event at all for this long is dead.
GC_LIVE_AFTER_DAYS = 30

_EVENT_KINDS = ("start", "stop", "notification", "end")

# How the session was LEFT — a different axis from whether its process is
# running. ``last_event.kind`` and the hub's ``Session.status``
# (working | waiting_input | idle | ended) are liveness; this is intent. A
# session can be ``end``-ed and ``parked``, or quiet and ``done``. Written by
# the extraction pass from the closing exchange (see ``lib/extraction.py`` →
# ``parse_disposition``), under its own key so the frozen status vocabulary is
# never overloaded.
DISPOSITION_KEY = "disposition"
_DISPOSITIONS = ("active", "parked", "done")


def registry_dir(data_dir: Path) -> Path:
    """Session registry directory: ``<data_dir>/sessions``."""
    return data_dir / "sessions"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hostname() -> str:
    """$HOSTNAME (container name in kit containers), else the OS hostname."""
    env = os.environ.get("HOSTNAME", "").strip()
    if env:
        return env
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _in_container() -> bool:
    """Is this session running inside a container?

    Recorded so a reader can tell whether ``hostname`` is a *container name* or
    a machine name. Both look like an identifier and neither says which it is,
    and the fleet view now judges liveness by asking the host which containers
    exist — so mistaking a laptop's hostname for a missing container would
    declare a perfectly live bare session dead. ``--local`` mode and
    `scripts/claude-wrapped` make that a real configuration, not a hypothetical.

    ``/.dockerenv`` is the marker the runtime itself creates; a pure file check,
    which matters because this runs inside kill-within-seconds hooks. Entries
    written before this field existed simply lack it, and a reader that finds no
    answer must fall back rather than guess — which is why the field is written
    as an explicit ``True``/``False`` and never inferred from its absence.
    """
    try:
        return Path("/.dockerenv").exists()
    except OSError:
        return False


def _workspace_root(data_dir: Path) -> str:
    """Workspace root the entry belongs to.

    ``data_dir`` is ``<workspace>/.multiplai/data`` whenever a workspace is
    configured (see multiplai_core.paths), so two parents up is the root.
    Standalone installs resolve to the home directory — harmless, since a
    hub only reads registries inside a workspace it serves.
    """
    return str(data_dir.parent.parent)


def _resolve_project(cwd: str) -> str | None:
    """Project name for *cwd* via the shared resolver; ``None`` on any failure."""
    if not cwd:
        return None
    try:
        from lib.project_identity import resolve_project

        return resolve_project(cwd)
    except Exception:
        logger.debug("project resolution failed for %s", cwd, exc_info=True)
        return None


def _lock_entry(path: Path) -> int | None:
    """Exclusive advisory flock on ``<sid>.lock``; fd, or None when unavailable.

    Serializes concurrent read-merge-write cycles (Notification vs Stop hook,
    hook vs hub) so neither silently reverts the other's keys. The lock file
    is separate from the entry because the atomic-rename write swaps inodes —
    a flock on the entry itself would not serialize with the next opener.
    Fail-open: any OSError (readonly FS, flock unsupported) returns None and
    the caller proceeds unlocked. The blocking wait is bounded by the lock
    holder's own read-merge-write, i.e. milliseconds.
    """
    try:
        fd = os.open(
            str(path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o644
        )
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _unlock_entry(fd: int | None) -> None:
    """Release a :func:`_lock_entry` fd (closing drops the flock)."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _ensure_data_gitignore(data_dir: Path) -> None:
    """Drop a ``*`` .gitignore at the data-dir root if none exists.

    Registry entries contain workspace paths and must stay on-disk and
    untracked; mirroring multiplai_core.paths, the whole data bucket is
    git-ignored by mechanism rather than by a workspace-level rule a
    standalone checkout might lack. Best-effort.
    """
    gi = data_dir / ".gitignore"
    try:
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def record_event(data_dir: Path, hook_input: dict, kind: str) -> bool:
    """Write/update this session's registry entry with a lifecycle event.

    *hook_input* is the parsed hook stdin payload (``session_id``, ``cwd``).
    Creates the entry when missing (hooks may be installed mid-session, in
    which case ``started_at`` falls back to the event time — a lower bound).
    Existing keys this module doesn't own are preserved. Returns True when
    the entry was written; never raises.
    """
    try:
        if kind not in _EVENT_KINDS:
            logger.warning("Unknown registry event kind %r; skipped", kind)
            return False
        session_id = str(hook_input.get("session_id") or "").strip()
        # Path-safety: a session id is a UUID; refuse anything that could
        # escape the registry dir.
        if not session_id or "/" in session_id or session_id in (".", ".."):
            return False

        rdir = registry_dir(data_dir)
        rdir.mkdir(parents=True, exist_ok=True)
        _ensure_data_gitignore(data_dir)

        path = rdir / f"{session_id}.json"
        lock_fd = _lock_entry(path)
        try:
            entry: dict = {}
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    entry = existing
            except (OSError, json.JSONDecodeError, ValueError):
                entry = {}

            now = _now_iso()
            cwd = str(hook_input.get("cwd") or "").strip()

            entry["session_id"] = session_id
            # Hostname is refreshed on every event: a session resumed in a
            # new container must not keep the dead container's name, or the
            # launcher would map/adopt against a container that no longer
            # exists.
            hostname = _hostname()
            if hostname:
                entry["hostname"] = hostname
            else:
                entry.setdefault("hostname", "")
            # Refreshed alongside hostname and for the same reason: a session
            # resumed outside a container must stop being judged against the
            # container roster.
            entry["in_container"] = _in_container()
            entry.setdefault("workspace", _workspace_root(data_dir))
            entry.setdefault("started_at", now)
            if cwd:
                entry["cwd"] = cwd
            else:
                entry.setdefault("cwd", "")
            if not entry.get("project"):
                project = _resolve_project(entry.get("cwd", ""))
                if project:
                    entry["project"] = project
            entry["last_event"] = {"ts": now, "kind": kind}
            # A "start" is the user picking the session back up (resume /
            # new window on the same id), which makes the old departure
            # label obsolete by definition: a resumed `parked` session must
            # group by live status again (and re-enter "Needs you" when
            # waiting), and a resumed `done` one must be visible. The next
            # extraction pass re-labels how it is left this time.
            if kind == "start":
                entry.pop(DISPOSITION_KEY, None)

            atomic_write_json(path, entry)
            return True
        finally:
            _unlock_entry(lock_fd)
    except Exception:
        logger.warning("Could not record session registry event", exc_info=True)
        return False


def record_disposition(data_dir: Path, session_id: str, state: str, reason: str = "") -> bool:
    """Record how a session was left: ``active | parked | done``.

    A **new key**, never an overload of ``last_event.kind`` or the hub's
    ``Session.status`` — those are frozen as liveness in the multiplai-gui API
    contract, and a session can perfectly well be ``ended`` *and* ``parked``.

    Takes the same per-entry flock as :func:`record_event` and preserves every
    key it does not own, because this write races the hooks: extraction runs
    minutes after ``SessionEnd``, and the session may have been resumed in the
    meantime.

    Refuses to create an entry that does not exist. A disposition without a
    session is not a parked session — it is a stray file that GC would have to
    learn about. Never raises; returns True when written.
    """
    try:
        if state not in _DISPOSITIONS:
            logger.warning("Unknown disposition %r; not recorded", state)
            return False
        session_id = str(session_id or "").strip()
        if not session_id or "/" in session_id or session_id in (".", ".."):
            return False

        path = registry_dir(data_dir) / f"{session_id}.json"
        if not path.exists():
            # A dropped `parked`/`done` is user-visible (the session vanishes
            # from or lingers in AGENTS.md), so it warrants a warning; a
            # dropped `active` changes nothing — absent means active.
            if state != "active":
                logger.warning(
                    "No registry entry for %s; %s disposition not recorded",
                    session_id, state,
                )
            else:
                logger.debug("No registry entry for %s; disposition not recorded", session_id)
            return False

        lock_fd = _lock_entry(path)
        try:
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                if state != "active":
                    logger.warning(
                        "Registry entry for %s unreadable under lock; "
                        "%s disposition not recorded", session_id, state,
                    )
                return False
            if not isinstance(entry, dict):
                if state != "active":
                    logger.warning(
                        "Registry entry for %s is not an object; "
                        "%s disposition not recorded", session_id, state,
                    )
                return False

            entry[DISPOSITION_KEY] = {
                "state": state,
                "reason": str(reason or "")[:500],
                "ts": _now_iso(),
            }
            atomic_write_json(path, entry)
            return True
        finally:
            _unlock_entry(lock_fd)
    except Exception:
        logger.warning("Could not record session disposition", exc_info=True)
        return False


def entry_disposition_block(entry: dict) -> tuple[str, str]:
    """``(state, reason)`` of a parsed entry; state defaults to ``active``.

    The one parse of the disposition block every consumer shares — state and
    reason must come from the same read or they drift (the fleet view once
    parsed the block twice with divergent logic). A reason is only meaningful
    alongside a valid non-default state, so a malformed block yields
    ``("active", "")``, never a stray reason.
    """
    block = entry.get(DISPOSITION_KEY)
    if isinstance(block, dict):
        state = str(block.get("state") or "").strip().lower()
        if state in _DISPOSITIONS:
            return state, str(block.get("reason") or "")
    return "active", ""


def entry_disposition(entry: dict) -> str:
    """The disposition state of a parsed entry, defaulting to ``active``."""
    return entry_disposition_block(entry)[0]


def _entry_is_stale(
    path: Path,
    cutoff_ended: datetime,
    cutoff_live: datetime,
    dead_sids: frozenset[str] | set[str] = frozenset(),
    dead_before: datetime | None = None,
) -> bool:
    """Staleness of one registry entry (see :func:`gc_stale` for the policy).

    A missing entry (vanished mid-scan) is NOT stale — there is nothing to
    remove. Read errors other than absence propagate as OSError for the
    caller's per-path handler.
    """
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        # A parked session is never stale. Measured asymmetry this fixes:
        # transcripts survive a year (`cleanupPeriodDays = 365`), so
        # `claude --resume <id>` works months later — but the registry entry
        # vanished in 7 to 30 days. A parked idea therefore stayed *resumable*
        # while becoming *invisible*, which is the original complaint. The
        # session record IS the parked idea; nothing is copied anywhere.
        if entry_disposition(entry) == "parked":
            return False
        last = entry.get("last_event") or {}
        ts = datetime.fromisoformat(str(last.get("ts") or ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # Observation beats both clocks. *dead_sids* are entries whose container
        # the host looked for and did not find (`lib.fleet.roster_dead_sids`);
        # the two windows below exist only because nothing used to be able to
        # tell a killed session from a quiet one.
        #
        # Two guards, both load-bearing. `parked` is checked first, above:
        # parking outranks liveness, since a parked session's process being gone
        # is the normal case. And the entry must not have spoken since the scan
        # began (*dead_before*) — the deletion below happens under a lock this
        # pass may have waited on, and a writer that refreshed the entry
        # meanwhile is a session that came back. Without this the re-check under
        # the lock would pass a set it can no longer contradict.
        if str(entry.get("session_id") or path.stem) in dead_sids and (
            dead_before is None or ts <= dead_before
        ):
            return True
        if last.get("kind") == "end":
            return ts < cutoff_ended
        return ts < cutoff_live
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return mtime < cutoff_ended


def _pending_extraction_sids(data_dir: Path) -> set[str]:
    """Session ids whose deferred extraction is still queued or in flight.

    Extraction is what writes the disposition, and it always runs *after* GC
    within a SessionStart (GC is synchronous, the drain spawns children that
    finish minutes later). Without this scan, a session parked on day 0 whose
    owner next opens Claude on day 8 lost its entry before the drain could
    label it ``parked`` — and :func:`record_disposition` refuses to recreate
    entries. Markers are on disk before either step runs, so reading them at
    GC time is the "marker scan before GC" ordering.

    Quarantined markers (``failed_extractions/``) deliberately do NOT protect
    an entry: after ``MAX_ATTEMPTS`` the drain has given up, so the entry ages
    out normally rather than living forever behind a dead marker.
    """
    sids: set[str] = set()
    for sub in ("pending_extractions", "processing_extractions"):
        try:
            markers = list((data_dir / sub).glob("*.json"))
        except OSError:
            continue
        for m in markers:
            try:
                data = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict):
                sid = str(data.get("session_id") or "").strip()
                if sid:
                    sids.add(sid)
    return sids


def _sweep_orphans(rdir: Path, cutoff: datetime) -> int:
    """Remove ``.adopt``/``.lock``/``.tmp-*`` files whose entry is gone.

    Left behind when GC ran without the entry flock (fail-open) or a hub
    crashed between marker and entry. Age-gated by mtime (older than the
    ended cutoff) so an in-progress writer that has created its lock file
    but not yet the entry is never touched. ``.lock`` files are removed
    lock-then-unlink: a NON-blocking flock is taken first — a held lock
    means a writer is inside its critical section, so skip and let the
    next GC retry; after acquiring, the ``.json`` absence is re-checked
    under the lock before unlinking.

    ``.tmp-*`` files are :func:`lib.fsio.atomic_write` staging files
    orphaned by a SIGKILL between mkstemp and rename (P10) — and a
    container kill IS the normal session end here, so they accumulate.
    They belong to no entry, so they get only the age gate.
    """
    removed = 0
    for orphan in [
        *rdir.glob("*.adopt"),
        *rdir.glob("*.lock"),
        *rdir.glob(".tmp-*"),
    ]:
        try:
            is_tmp = orphan.name.startswith(".tmp-")
            if not is_tmp and orphan.with_suffix(".json").exists():
                continue
            mtime = datetime.fromtimestamp(orphan.stat().st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                continue
            if orphan.suffix == ".lock":
                fd = os.open(str(orphan), os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Re-check under the lock: a writer serialized on this
                    # lock may have just (re)created the entry.
                    if not orphan.with_suffix(".json").exists():
                        orphan.unlink(missing_ok=True)
                        removed += 1
                finally:
                    os.close(fd)
            else:
                orphan.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def gc_stale(
    data_dir: Path,
    days: int = GC_AFTER_DAYS,
    live_days: int = GC_LIVE_AFTER_DAYS,
    dead_sids: frozenset[str] | set[str] | None = None,
) -> int:
    """Delete registry entries whose session ended more than *days* ago.

    *dead_sids* are entries an outside observer has confirmed dead — the
    caller passes what :func:`lib.fleet.roster_dead_sids` read off the host's
    container roster. Those are collected on this pass regardless of age, and
    the reason is that the age windows below are not really about age: they are
    two guesses standing in for the fact that a session cannot report its own
    death. Where that fact is available, a guess is not needed. Passed in rather
    than read here so this module keeps no opinion about containers and no
    import of the fleet view (which imports this one).

    Entries whose last event is anything other than ``end`` age out after
    *live_days* instead — a session killed without firing ``SessionEnd``
    would otherwise linger forever as an adoptable ghost, and nothing can
    tell that apart from a session that is merely quiet.
    Unparseable entries older than the *days* window (by mtime) are removed
    too — they can never become readable again and would otherwise accumulate
    forever. A removed entry's orphaned ``.adopt`` marker goes with it.

    **Entries with ``disposition: parked`` are never collected**, at any age.
    Parking a session is the user saying "I am coming back to this", and the
    entry is the only record of it — see :func:`_entry_is_stale`.

    **Entries with a pending/in-flight extraction marker are never collected
    either** — the deferred extraction is what writes the disposition, and it
    runs after GC within the same SessionStart. Collecting first would delete
    the entry a day-8 drain is about to label ``parked`` — see
    :func:`_pending_extraction_sids`.

    Concurrency: removal takes the same per-entry flock the writers use and
    RE-CHECKS staleness under it, so an entry can't be deleted out from
    under a hook/hub mid read-merge-write (the writer we serialized behind
    refreshes the timestamp, and the re-check keeps the entry). The
    ``.lock`` file itself is removed only while its flock is held
    (lock-then-unlink); when the flock is unavailable (fail-open filesystem)
    the lock file is left behind — the orphan sweep collects it later. The
    sweep also removes ``.adopt``/``.lock`` files whose entry is gone.

    Returns the number of entries removed (orphan-file sweeps are not
    counted); never raises.
    """
    removed = 0
    try:
        rdir = registry_dir(data_dir)
        if not rdir.is_dir():
            return 0
        now = datetime.now(timezone.utc)
        cutoff_ended = now - timedelta(days=days)
        cutoff_live = now - timedelta(days=live_days)
        protected = _pending_extraction_sids(data_dir)
        dead = frozenset(dead_sids or ())
        for path in list(rdir.glob("*.json")):
            try:
                if path.stem in protected:
                    continue
                if not _entry_is_stale(path, cutoff_ended, cutoff_live, dead, now):
                    continue
                lock_fd = _lock_entry(path)
                try:
                    # Re-check under the lock — the writer we may have just
                    # waited on could have refreshed the entry.
                    if not _entry_is_stale(path, cutoff_ended, cutoff_live, dead, now):
                        continue
                    path.unlink(missing_ok=True)
                    path.with_suffix(".adopt").unlink(missing_ok=True)
                    if lock_fd is not None:
                        # We HOLD the flock, so no writer is inside its
                        # critical section; one blocked on it will simply
                        # recreate the entry (it is alive by definition).
                        path.with_suffix(".lock").unlink(missing_ok=True)
                finally:
                    _unlock_entry(lock_fd)
                removed += 1
            except OSError:
                continue
        orphans = _sweep_orphans(rdir, cutoff_ended)
        if removed or orphans:
            logger.info(
                "GC'd %d stale session registry entr(y/ies), %d orphaned marker/lock file(s)",
                removed, orphans,
            )
    except Exception:
        logger.warning("Session registry GC failed", exc_info=True)
    return removed
