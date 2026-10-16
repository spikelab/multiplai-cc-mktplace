"""Every build_pipeline.*, code_facts.* and multiplai_core.* INFO line reaches
build-pipeline-<day>.log (multiplai-core 0.21: one file per UTC day, nothing
dropped). Under pytest, multiplai_core redirects the logs directory to a temp
directory, so this reads the same place the run wrote."""

import logging
import uuid
from datetime import datetime, timezone

from multiplai_core.log_utils import _get_logs_dir

from build_pipeline import setup as setup_mod
from build_pipeline.__main__ import main


def test_package_lines_reach_the_build_pipeline_log(monkeypatch):
    probe = uuid.uuid4().hex

    def fake_config_get(args):
        logging.getLogger("build_pipeline.setup").info("pipeline %s", probe)
        logging.getLogger("code_facts.explore").info("code_facts %s", probe)
        logging.getLogger("multiplai_core.agent_runner").info("core %s", probe)
        return 0

    monkeypatch.setattr(setup_mod, "config_get", fake_config_get)
    assert main(["config-get", "interview"]) == 0

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = (_get_logs_dir() / f"build-pipeline-{day}.log").read_text()
    for line in (f"pipeline {probe}", f"code_facts {probe}", f"core {probe}"):
        assert f"INFO: {line}" in text
