"""Session registry writes for the multiplai hub ("hub input contract").

The multiplai hub (the multiplai-gui repo, ``docs/api-contract.md`` → "Hub
input contract") discovers live sessions from per-session JSON entries written by
the lifecycle hooks — hooks are the source of truth because Claude Code keeps
no open fd on its transcript JSONL, so file ownership is undiscoverable by
inspection. Entries live at ``<data_dir>/sessions/<session_id>.json``:

    {session_id, hostname, cwd, project?, workspace, started_at,
     last_event: {ts, kind: start|stop|notification|end}}

``hostname`` equals the container name in kit containers ($HOSTNAME) and the
plain machine hostname otherwise — it is how the launcher wrapper maps a
container back to its session. The hub additionally writes
``<session_id>.adopt`` markers beside the entries; this module never touches
those beyond GC of orphans, and updates preserve any keys it doesn't own
(read-merge-write) so hub-written fields survive.

Degradation: with no hub installed the files are simply never read. Every
public function is best-effort — it returns rather than raises, because it
runs inside kill-within-seconds hooks that must never break a session.
"""

import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Registry entries whose session ended more than this long ago are GC'd on
# SessionStart (per the hub input contract).
GC_AFTER_DAYS = 7

_EVENT_KINDS = ("start", "stop", "notification", "end")


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


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


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
        entry.setdefault("hostname", _hostname())
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

        _atomic_write(path, entry)
        return True
    except Exception:
        logger.warning("Could not record session registry event", exc_info=True)
        return False


def gc_stale(data_dir: Path, days: int = GC_AFTER_DAYS) -> int:
    """Delete registry entries whose session ended more than *days* ago.

    Unparseable entries older than the window (by mtime) are removed too —
    they can never become readable again and would otherwise accumulate
    forever. A removed entry's orphaned ``.adopt`` marker goes with it.
    Returns the number of entries removed; never raises.
    """
    removed = 0
    try:
        rdir = registry_dir(data_dir)
        if not rdir.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for path in list(rdir.glob("*.json")):
            try:
                stale = False
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    last = entry.get("last_event") or {}
                    if last.get("kind") == "end":
                        ts = datetime.fromisoformat(str(last.get("ts") or ""))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        stale = ts < cutoff
                except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
                    mtime = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    )
                    stale = mtime < cutoff
                if not stale:
                    continue
                path.unlink(missing_ok=True)
                path.with_suffix(".adopt").unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info("GC'd %d stale session registry entr(y/ies)", removed)
    except Exception:
        logger.warning("Session registry GC failed", exc_info=True)
    return removed
