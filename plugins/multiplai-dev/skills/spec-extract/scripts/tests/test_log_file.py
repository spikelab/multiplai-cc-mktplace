"""spec-extract now configures logging: every spec_extract_pipeline.*,
code_facts.* and multiplai_core.* INFO line reaches
<data_dir>/logs/spec-extract-pipeline-<UTC day>.log. Before, it never called
setup_logging, so those lines went nowhere. Run in a subprocess with
CLAUDE_PLUGIN_OPTION_DATA_DIR pointed at a temp dir: logging is configured once
per process."""

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
from spec_extract_pipeline import __main__ as cli

def fake_run(args, now):
    logging.getLogger("spec_extract_pipeline.explore").info("pipeline probe")
    logging.getLogger("code_facts.explore").info("code_facts probe")
    logging.getLogger("multiplai_core.agent_runner").info("core probe")
    return cli.EXIT_DONE

cli._run = fake_run
sys.exit(cli.main(["run", "--tree"]))
"""


def test_package_lines_reach_the_spec_extract_log(tmp_path):
    proc = _run_probe(_PROBE, tmp_path / "data")
    assert proc.returncode == 0, proc.stderr
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    text = (tmp_path / "data" / "logs" / f"spec-extract-pipeline-{day}.log").read_text()
    assert "[spec_extract_pipeline.explore]" in text and "INFO: pipeline probe" in text
    assert "[code_facts.explore]" in text and "INFO: code_facts probe" in text
    assert "[multiplai_core.agent_runner]" in text and "INFO: core probe" in text
