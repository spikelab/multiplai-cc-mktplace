"""Session start hook for multiplai plugin.

Loads memory files, injects context, logs client selection,
records session start timestamp and initializes session state.

Also checks the AutoDream 24h gate: when more than 24 hours have
elapsed since the last dream run and fresh learnings are pending,
emits a system nudge so Spike is prompted to run ``/multiplai:dream``
instead of the consolidation silently falling out of rhythm.
"""

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.config import load_yaml, read_memory_files
from lib.log_utils import setup_logging

logger = setup_logging("session_start")

_AUTODREAM_GATE_HOURS = 24


def _log_client_selection() -> str:
    """Log which model client is available for this session.

    Uses the model_client module's detect_client_type() to determine
    which backend will be used (AgentSDK vs API key fallback).
    """
    from lib.model_client import detect_client_type
    client_type = detect_client_type()
    logger.info("Model client selected: %s", client_type)
    return client_type


def _autodream_gate_open(dream_state_file: Path) -> bool:
    """Return True when >=24h have passed since the last dream run.

    Missing state or an unparseable timestamp is treated as gate-open
    (first run or recovery). Any YAML parse failure is swallowed — a
    corrupt state file shouldn't block session start; the user can
    recover by running ``/multiplai:dream`` manually.
    """
    try:
        state = load_yaml(dream_state_file) or {}
    except Exception:
        logger.warning("Could not read dream state %s; treating gate as open", dream_state_file)
        return True

    last_run = state.get("last_run")
    if not last_run:
        return True

    try:
        last_dt = datetime.fromisoformat(last_run)
    except (ValueError, TypeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=_AUTODREAM_GATE_HOURS)


def _learnings_pending(learnings_file: Path, dream_state_file: Path) -> bool:
    """Return True if learnings.md has content newer than the last dream run."""
    if not learnings_file.exists():
        return False
    if learnings_file.stat().st_size == 0:
        return False

    try:
        state = load_yaml(dream_state_file) or {}
    except Exception:
        return True

    last_run = state.get("last_run")
    if not last_run:
        return True

    try:
        last_dt = datetime.fromisoformat(last_run)
    except (ValueError, TypeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)

    learnings_mtime = datetime.fromtimestamp(
        learnings_file.stat().st_mtime, tz=timezone.utc,
    )
    return learnings_mtime > last_dt


def _emit_autodream_nudge() -> None:
    """Print an additionalContext nudge prompting Spike to run /multiplai:dream."""
    print(
        "\n--- SYSTEM NUDGE ---\n"
        "AutoDream gate is open (>24h since last consolidation) with "
        "unprocessed learnings on disk. Surface this to Spike at the next "
        "natural stopping point: 'Dream reports look due — worth running "
        "/multiplai:dream?'"
    )


def main() -> None:
    paths = get_paths()
    data_dir = paths.plugin_data()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Log which model client is available
    client_type = _log_client_selection()

    # Load memory files for context injection
    memory_dir = paths.memory_dir()
    memory_context = read_memory_files(memory_dir)

    session_id = str(uuid.uuid4())[:8]
    session_state = {
        "session_id": session_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "plugin_mode": paths.is_plugin_mode(),
        "client_type": client_type,
        "memory_files_loaded": list(memory_context.keys()),
    }

    state_file = data_dir / "session_state.json"
    state_file.write_text(json.dumps(session_state, indent=2))

    # Inject memory context to stdout for Claude Code to consume
    if memory_context:
        for filename, content in memory_context.items():
            print(f"\n## Memory: {filename}\n{content}")

    # AutoDream gate: emit a nudge when the 24h window has elapsed and
    # fresh learnings are waiting. The nudge is additionalContext only —
    # the actual dream still runs via /multiplai:dream when Spike
    # chooses.
    dream_state_file = paths.dream_state_file()
    learnings_file = paths.learnings_file()
    if (
        _autodream_gate_open(dream_state_file)
        and _learnings_pending(learnings_file, dream_state_file)
    ):
        logger.info("AutoDream gate open with pending learnings; emitting nudge")
        _emit_autodream_nudge()

    logger.info("Session started: %s (loaded %d memory files)", session_id, len(memory_context))


if __name__ == "__main__":
    main()
