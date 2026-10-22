"""Every build_pipeline.*, code_facts.* and multiplai_core.* INFO line reaches
<data_dir>/logs/build-pipeline-<UTC day>.log (multiplai-core 0.21: one file per
day, nothing dropped). Run in a subprocess with CLAUDE_PLUGIN_OPTION_DATA_DIR
pointed at a temp dir: logging is configured once per process."""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _run_probe(code: str, data_dir) -> subprocess.CompletedProcess:
    """Run *code* in a fresh interpreter whose data directory is *data_dir*."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE_PLUGIN", "PYTEST"))
        and k not in ("WORKSPACE", "CLAUDE_PROJECT_DIR")
    }
    env["CLAUDE_PLUGIN_OPTION_DATA_DIR"] = str(data_dir)
    env.pop("MULTIPLAI_LOG_LEVEL", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPTS_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


_PROBE = """
import logging, sys
from build_pipeline import setup as setup_mod
from build_pipeline.__main__ import main

def fake_config_get(args):
    logging.getLogger("build_pipeline.setup").info("pipeline probe")
    logging.getLogger("code_facts.explore").info("code_facts probe")
    logging.getLogger("multiplai_core.agent_runner").info("core probe")
    return 0

setup_mod.config_get = fake_config_get
sys.exit(main(["config-get", "interview"]))
"""


def test_package_lines_reach_the_build_pipeline_log(tmp_path):
    proc = _run_probe(_PROBE, tmp_path / "data")
    assert proc.returncode == 0, proc.stderr
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    text = (tmp_path / "data" / "logs" / f"build-pipeline-{day}.log").read_text()
    assert "[build_pipeline.setup]" in text and "INFO: pipeline probe" in text
    assert "[code_facts.explore]" in text and "INFO: code_facts probe" in text
    assert "[multiplai_core.agent_runner]" in text and "INFO: core probe" in text
