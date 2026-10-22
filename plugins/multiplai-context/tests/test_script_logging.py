"""A multiplai-context script's log file holds its lib.* and multiplai_core.* lines.

Scripts run by path have no package of their own, so each one names the
packages its modules live in (``setup_logging(..., packages=("lib",
"generators"))``); multiplai_core is always included. Before, every
``lib.*`` INFO line was dropped and its warnings reached stderr unformatted.

Run in a subprocess: this suite's conftest configures logging at import, and
the check is about what a fresh script process does.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_PROBE = """
import importlib, logging, sys
sys.path.insert(0, sys.argv[1])
importlib.import_module(sys.argv[2])  # its module-level setup_logging(<name>, ...)
logging.getLogger("lib.checkpoint").info("lib probe line")
logging.getLogger("generators.base").info("generators probe line")
logging.getLogger("multiplai_core.agent_runner").info("core probe line")
logging.getLogger("lib.checkpoint").warning("lib probe warning")
"""


def _clean_env(data_dir: Path) -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE_PLUGIN") and k not in ("WORKSPACE", "CLAUDE_PROJECT_DIR")
    }
    env["CLAUDE_PLUGIN_OPTION_DATA_DIR"] = str(data_dir)
    env.pop("MULTIPLAI_LOG_LEVEL", None)
    env.pop("MULTIPLAI_DEBUG", None)
    return env


@pytest.mark.parametrize("script", ["session_notification", "dream", "context_manager"])
def test_lib_and_core_lines_reach_the_script_log(tmp_path, script):
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(SCRIPTS_DIR), script],
        env=_clean_env(data_dir), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    log = data_dir / "logs" / f"{script}-{day}.log"
    text = log.read_text()
    assert "[lib.checkpoint]" in text and "INFO: lib probe line" in text
    assert "[generators.base]" in text and "INFO: generators probe line" in text
    assert "[multiplai_core.agent_runner]" in text and "INFO: core probe line" in text
    # WARNING+ also reaches stderr, in the standard format.
    assert "[lib.checkpoint] [session:" in proc.stderr
    assert "WARNING: lib probe warning" in proc.stderr
    assert "lib probe line" not in proc.stderr
