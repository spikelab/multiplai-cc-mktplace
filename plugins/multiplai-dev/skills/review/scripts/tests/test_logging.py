"""A monkeypatched review, then the log files it left.

Under pytest, multiplai_core's `_pytest_guard` redirects the logs directory to
a temp directory, so this reads the same place the run wrote.
"""

from __future__ import annotations

import json

from conftest import CLAIM_HIGH
from multiplai_core.log_utils import _get_logs_dir
from review_pipeline.__main__ import main
from test_orchestrator import FakeAgents


def test_review_logs_events_without_finding_text(fixture_repo, tmp_path, monkeypatch, capsys):
    from review_pipeline import sdk

    repo, base, head = fixture_repo
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    out = tmp_path / "out"
    assert main(["--session-id", "sess-logging", "--out", str(out), "review", "--repo", str(repo),
                 "--range", f"{base}..{head}", "--trust-repo"]) == 0

    logs = _get_logs_dir()
    assert (logs / "review-pipeline.log").is_file()

    slug = f"booking-engine--{base}..{head}"
    records = [json.loads(line) for line in (logs / "activity.jsonl").read_text().splitlines() if line.strip()]
    ours = [r for r in records if r.get("component") == "review" and r.get("target") == slug]
    events = {r["event"] for r in ours}
    assert {"start", "stage", "done"} <= events
    done = next(r for r in ours if r["event"] == "done")
    assert done["counts"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 0}
    assert done["findings_path"].endswith("findings.json")

    for r in records:
        assert CLAIM_HIGH not in json.dumps(r)
    assert CLAIM_HIGH not in (logs / "activity.log").read_text()
