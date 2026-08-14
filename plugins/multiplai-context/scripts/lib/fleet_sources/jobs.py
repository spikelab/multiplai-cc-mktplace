"""Background jobs, read from the shared Claude Code config directory.

Claude Code records background work in ``$CLAUDE_CONFIG_DIR``:

* ``jobs/<short>/state.json`` — ``state``, a one-line ``detail``, in-flight and
  queued task counts, tokens, and ``output.result`` when it finished.
* ``daemon/roster.json`` — the live worker roster: session id, cwd, start time.

Under the multiplai kit that directory is a host path bind-mounted into every
container, so **one container can read every container's jobs**. That is worth
stating plainly because the obvious assumption is the opposite one.

Two constraints this module exists to enforce:

**Read, never drive.** The control sockets are per-container
(``/tmp/cc-daemon-501/…/rv/<short>.sock``), so a job is observable from
anywhere and steerable only from where it was started. Nothing here may
suggest otherwise.

**Judge liveness by mtime, never by pid.** The roster's pids belong to another
process namespace; probing one from here is meaningless and would confidently
report long-dead jobs as running. Staleness comes from the file's own clock.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.fsio import claude_config_dir

logger = logging.getLogger(__name__)

# Past this, a roster entry describes a process that is almost certainly gone.
# Deliberately generous: the cost of calling a live job stale is a wrong label,
# and the cost of calling a dead one live is a fire you think you have.
STALE_AFTER_HOURS = 6

# Terminal states as Claude Code records them. Anything else is in flight.
_FINISHED = frozenset({"done", "failed", "error", "cancelled", "canceled", "killed"})


@dataclass
class BackgroundJob:
    short: str
    session_id: str = ""
    state: str = ""
    detail: str = ""
    cwd: str = ""
    tokens: int = 0
    in_flight: int = 0
    queued: int = 0
    updated: datetime | None = None
    stale: bool = False
    in_roster: bool = False

    @property
    def finished(self) -> bool:
        return self.state.lower() in _FINISHED

    @property
    def running(self) -> bool:
        """In flight *and* recently heard from. Both halves are required."""
        return not self.finished and not self.stale


def config_dir() -> Path | None:
    """The Claude Code config directory, or ``None`` when it doesn't exist.

    ``lib.fsio.claude_config_dir`` resolves ``$CLAUDE_CONFIG_DIR`` with the
    documented ``~/.claude`` fallback; this wrapper additionally requires the
    directory to exist, because every consumer here reads files out of it.
    """
    path = claude_config_dir()
    return path if path.is_dir() else None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _roster(cfg: Path) -> dict[str, dict]:
    """``{short id: worker record}`` from ``daemon/roster.json``."""
    try:
        raw = json.loads((cfg / "daemon" / "roster.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    workers = raw.get("workers") if isinstance(raw, dict) else None
    return workers if isinstance(workers, dict) else {}


def collect_jobs(
    cfg: Path | None = None,
    now: datetime | None = None,
    stale_after_hours: float = STALE_AFTER_HOURS,
) -> list[BackgroundJob]:
    """Every background job on disk, newest first. Never raises."""
    cfg = cfg or config_dir()
    if cfg is None:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = timedelta(hours=stale_after_hours)
    roster = _roster(cfg)

    jobs: list[BackgroundJob] = []
    try:
        entries = sorted(p for p in (cfg / "jobs").iterdir() if p.is_dir())
    except OSError:
        entries = []

    for entry in entries:
        state_file = entry / "state.json"
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue

        worker = roster.get(entry.name) or {}
        in_flight_raw = raw.get("inFlight")
        in_flight: dict = in_flight_raw if isinstance(in_flight_raw, dict) else {}
        output_raw = raw.get("output")
        output: dict = output_raw if isinstance(output_raw, dict) else {}

        updated = _mtime(state_file)
        job = BackgroundJob(
            short=entry.name,
            session_id=str(worker.get("sessionId") or raw.get("sessionId") or ""),
            state=str(raw.get("state") or ""),
            detail=str(raw.get("detail") or output.get("result") or "").strip(),
            cwd=str(worker.get("cwd") or raw.get("cwd") or ""),
            tokens=int(raw.get("tokens") or 0),
            in_flight=int(in_flight.get("tasks") or 0),
            queued=int(in_flight.get("queued") or 0),
            updated=updated,
            stale=updated is None or (now - updated) > cutoff,
            in_roster=entry.name in roster,
        )
        jobs.append(job)

    jobs.sort(
        key=lambda j: (j.updated or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return jobs
