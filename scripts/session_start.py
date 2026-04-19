"""Session start hook for multiplai plugin.

Loads memory files, injects context, logs client selection,
records session start timestamp and initializes session state.
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.config import read_memory_files
from lib.log_utils import setup_logging

logger = setup_logging("session_start")


def _log_client_selection() -> str:
    """Log which model client is available for this session.

    Uses the model_client module's detect_client_type() to determine
    which backend will be used (AgentSDK vs API key fallback).
    """
    from lib.model_client import detect_client_type
    client_type = detect_client_type()
    logger.info("Model client selected: %s", client_type)
    return client_type


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

    logger.info("Session started: %s (loaded %d memory files)", session_id, len(memory_context))


if __name__ == "__main__":
    main()
