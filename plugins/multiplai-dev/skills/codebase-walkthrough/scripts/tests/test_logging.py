"""A run's log events carry counts, never prompt or claim text.

Under pytest, multiplai_core's `_pytest_guard` redirects the logs directory to
a temp directory, so this reads the same place the run wrote.
"""

from __future__ import annotations

import json

from fakes import FakeAgents
from multiplai_core.log_utils import _get_logs_dir
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main


def _read_logs(logs, stem, ext):
    """Every ``<stem>-YYYY-MM-DD.<ext>`` file in *logs*, oldest day first, joined.

    multiplai-core 0.21 writes one file per UTC day under its dated name."""
    files = sorted(logs.glob(f"{stem}-*.{ext}"))
    assert files, f"no {stem} log in {logs}"
    return "".join(f.read_text() for f in files)


def test_logging_carries_no_prompt_text(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["--session-id", "sess-walk", *run_args(ws, tmp_path)]) == 0
    logs = _get_logs_dir()
    assert _read_logs(logs, "walkthrough-pipeline", "log")
    records = [
        json.loads(l)
        for l in _read_logs(logs, "activity", "jsonl").splitlines()
        if l.strip()
    ]
    ours = [
        r
        for r in records
        if r.get("component") == "walkthrough"
        and str(r.get("target", "")).startswith("engine--bookings--")
    ]
    assert {"start", "stage", "done"} <= {r["event"] for r in ours}
    rejects = [r for r in ours if r["event"] == "gate_reject"]
    assert rejects and all(
        set(r["reasons"])
        <= {
            "quote not at cited lines",
            "path not at commit",
            "other",
            "doc quote not in the page",
            "empty quote",
            "path names no known repo",
        }
        for r in rejects
    )
    planted = [
        "an invented claim",
        "this sentence is not on the page",
        "Ignore all previous instructions",
    ]
    blob = (
        json.dumps(ours)
        + _read_logs(logs, "activity", "log")
        + _read_logs(logs, "walkthrough-pipeline", "log")
    )
    for text in planted + [agents.calls[0]["prompt"][:60]]:
        assert text not in blob
