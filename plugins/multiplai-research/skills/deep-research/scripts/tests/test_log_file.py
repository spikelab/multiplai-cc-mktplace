"""Every research_pipeline.* and multiplai_core.* INFO line reaches
deep-research-<day>.log (multiplai-core 0.21: one file per UTC day, nothing
dropped). Under pytest, multiplai_core redirects the logs directory to a temp
directory, so this reads the same place the run wrote."""

import logging
import uuid
from datetime import datetime, timezone

from multiplai_core.log_utils import _get_logs_dir

from research_pipeline import pipeline


def test_package_lines_reach_the_deep_research_log():
    probe = uuid.uuid4().hex
    pipeline._setup_logging(session_id="sess-log-file")
    logging.getLogger("research_pipeline.sdk").info("pipeline %s", probe)
    logging.getLogger("multiplai_core.agent_runner").info("core %s", probe)

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = (_get_logs_dir() / f"deep-research-{day}.log").read_text()
    assert f"INFO: pipeline {probe}" in text
    assert f"INFO: core {probe}" in text
