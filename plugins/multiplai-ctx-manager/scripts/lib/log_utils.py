"""Logging utilities for multiplai plugin.

Implements ``reference/dev/logging-standard.md``:

- UTC ISO-8601 line format with component + session id
- ``MULTIPLAI_DEBUG`` / ``MULTIPLAI_LOG_LEVEL`` env-driven level
- Date-rotated per-component logs (7-day retention)
- Shared ``hook-errors.log`` for ERROR+ across all components

On top of the standard, ``log_event()`` writes a curated, human-readable
activity stream (``activity-YYYY-MM-DD.log``) plus a machine-parseable
mirror (``activity-YYYY-MM-DD.jsonl``). This is the human-in-the-loop
view: one narrative line per meaningful thing the plugin does (context
injected, nudge fired, diary written, learnings captured, catalog
rebuilt). It is written regardless of log level — it is the signal, not
the debug noise.

All log files live under the plugin data directory via the path resolver.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

# How long activity-*.log/.jsonl files are kept before opportunistic prune.
_ACTIVITY_RETENTION_DAYS = 14

# Prune runs at most once per process.
_pruned = False


def _get_logs_dir() -> Path:
    """Get logs directory from path resolver (imported lazily)."""
    from lib.paths import get_paths
    return get_paths().logs_dir()


def resolve_level() -> int:
    """Resolve the log level from the environment per the logging standard.

    Precedence:
        1. ``MULTIPLAI_DEBUG`` truthy (1/true/yes/on) → DEBUG
        2. ``MULTIPLAI_LOG_LEVEL`` (DEBUG|INFO|WARNING|ERROR)
        3. INFO (default)
    """
    if os.environ.get("MULTIPLAI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        return logging.DEBUG
    name = os.environ.get("MULTIPLAI_LOG_LEVEL", "").strip().upper()
    return _LEVELS.get(name, logging.INFO)


class _StandardFormatter(logging.Formatter):
    """Emit ``[ts] [component] [session:xxxxxxxx] LEVEL: message``.

    Timestamp is UTC, ISO-8601, always suffixed ``Z``. Session id is the
    first 8 chars of the Claude Code session id, or ``--------`` if
    unknown.
    """

    def __init__(self, session_id: str | None = None):
        super().__init__()
        sid = (session_id or "")[:8]
        self._sid = sid if sid else "--------"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        line = (
            f"[{ts}] [{record.name}] [session:{self._sid}] "
            f"{record.levelname}: {record.getMessage()}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(
    name: str = "multiplai",
    level: int | None = None,
    session_id: str | None = None,
) -> logging.Logger:
    """Set up logging for a multiplai script.

    Configures (idempotently) a stderr handler, a date-rotated per-component
    file handler (7-day retention), and a shared ``hook-errors.log`` handler
    for ERROR+. When *level* is omitted it is resolved from the environment
    via :func:`resolve_level` so ``MULTIPLAI_DEBUG=1`` makes every script
    verbose without code changes.
    """
    logger = logging.getLogger(name)
    resolved = level if level is not None else resolve_level()
    logger.setLevel(resolved)

    if logger.handlers:
        return logger

    fmt = _StandardFormatter(session_id)

    # Stderr handler for immediate feedback (visible under `claude --debug`
    # and to anything tailing the hook's stderr).
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(resolved)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)

    try:
        logs_dir = _get_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Date-rotated per-component log, 7-day retention (replaces the old
        # unbounded {name}-{date}.log scheme).
        file_handler = TimedRotatingFileHandler(
            logs_dir / f"{name}.log",
            when="midnight",
            utc=True,
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setLevel(resolved)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        # Shared ERROR+ sink across all components (append-only).
        error_handler = logging.FileHandler(
            logs_dir / "hook-errors.log", encoding="utf-8"
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(fmt)
        logger.addHandler(error_handler)
    except Exception:
        logger.debug("Could not set up file logging", exc_info=True)

    return logger


def _prune_old_activity(logs_dir: Path, days: int = _ACTIVITY_RETENTION_DAYS) -> None:
    """Delete ``activity-*`` files older than *days* (best-effort)."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    for pattern in ("activity-*.log", "activity-*.jsonl"):
        for f in logs_dir.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def log_event(
    component: str,
    event: str,
    message: str,
    *,
    session_id: str | None = None,
    level: str = "INFO",
    **fields: object,
) -> None:
    """Append one curated event to the activity log and its JSONL mirror.

    This is the human-in-the-loop signal — what the plugin actually did,
    in plain language. Written regardless of configured log level and
    never raises (a logging failure must not break a hook).

    Args:
        component: Short subsystem tag (e.g. ``context``, ``nudge``,
            ``diary``, ``learnings``, ``catalog``, ``session``).
        event: Stable machine key for the JSONL mirror (e.g.
            ``inject``, ``dream``, ``write``).
        message: Human-readable sentence describing what happened.
        session_id: Claude Code session id (first 8 chars are recorded).
        level: Severity label for the JSONL record (INFO/WARNING/ERROR).
        **fields: Structured key/values appended to both sinks.
    """
    try:
        logs_dir = _get_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        sid = (session_id or "")[:8] or "--------"

        # The human line is the message, verbatim — a clean sentence the
        # call site is responsible for making self-contained. Structured
        # fields enrich the JSONL mirror only (no noisy key=value tail).
        human = f"{now.strftime('%H:%M:%S')} [{component}] {message}"
        with (logs_dir / f"activity-{date}.log").open("a", encoding="utf-8") as fh:
            fh.write(human + "\n")

        record: dict[str, object] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component": component,
            "event": event,
            "level": level,
            "session": sid,
            "msg": message,
        }
        record.update(fields)
        with (logs_dir / f"activity-{date}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

        global _pruned
        if not _pruned:
            _pruned = True
            _prune_old_activity(logs_dir)
    except Exception:
        # Observability must never break the thing it observes.
        pass
