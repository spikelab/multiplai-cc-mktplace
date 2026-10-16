"""spec-extract now configures logging: every spec_extract_pipeline.*,
code_facts.* and multiplai_core.* INFO line reaches
spec-extract-pipeline-<day>.log. Before, it never called setup_logging, so
those lines went nowhere. Under pytest, multiplai_core redirects the logs
directory to a temp directory, so this reads the same place the run wrote."""

import logging
import uuid
from datetime import datetime, timezone

from multiplai_core.log_utils import _get_logs_dir

from spec_extract_pipeline import __main__ as cli


def test_package_lines_reach_the_spec_extract_log(monkeypatch):
    probe = uuid.uuid4().hex

    def fake_run(args, now):
        logging.getLogger("spec_extract_pipeline.explore").info("pipeline %s", probe)
        logging.getLogger("code_facts.explore").info("code_facts %s", probe)
        logging.getLogger("multiplai_core.agent_runner").info("core %s", probe)
        return cli.EXIT_DONE

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["run", "--tree"]) == cli.EXIT_DONE

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = (_get_logs_dir() / f"spec-extract-pipeline-{day}.log").read_text()
    for line in (f"pipeline {probe}", f"code_facts {probe}", f"core {probe}"):
        assert f"INFO: {line}" in text
