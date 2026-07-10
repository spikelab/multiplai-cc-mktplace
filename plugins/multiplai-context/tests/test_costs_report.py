"""Unit tests for costs_report.py — branch grouping and filtering."""

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import costs_report  # noqa: E402
from multiplai_core.costing import append_records  # noqa: E402


@pytest.fixture(autouse=True)
def _workspace(monkeypatch, tmp_path):
    """Isolated workspace so ledger reads/writes land in tmp."""
    for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_PLUGIN_OPTION_workspace_dir", "CLAUDE_PLUGIN_OPTION_data_dir"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    from multiplai_core.paths import _reset_cache
    _reset_cache()
    yield
    _reset_cache()


def _rec(msg_id, *, session="sess-1", branch=None, cost=1.0, ts="2026-07-01T10:00:00Z",
         model="claude-opus-4-8", sidechain=False, span=None):
    rec = {
        "ts": ts, "source": "transcript", "session": session, "project": "p",
        "model": model, "msg_id": msg_id, "sidechain": sidechain, "span": span,
        "component": "", "tokens": {"in": 1, "out": 1, "cw5m": 0, "cw1h": 0, "cr": 0},
        "cost_usd": cost,
    }
    if branch is not None:
        rec["branch"] = branch
    return rec


def _run(monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["costs_report.py", *argv])
    code = costs_report.main()
    return code, capsys.readouterr().out


def _seed():
    append_records([
        _rec("m1", session="sess-1", branch="main", cost=1.0),
        _rec("m2", session="sess-1", branch="feat/x", cost=2.0),   # mid-session switch
        _rec("m3", session="sess-2", branch="feat/x", cost=4.0),
        _rec("m4", session="sess-2", branch="feat/x", cost=0.5, sidechain=True,
             span={"kind": "agent", "name": "Explore"}),
        _rec("m5", session="sess-3", cost=8.0),                    # no branch → (none)
    ])


def test_by_branch_grouping(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--all", "--by", "branch", "--json")
    assert code == 0
    assert json.loads(out) == {"feat/x": 6.5, "main": 1.0, "(none)": 8.0}


def test_branch_filter_summary(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "feat/x", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["branch"] == "feat/x"
    assert data["records"] == 3
    assert data["total_usd"] == 6.5
    assert data["main_usd"] == 6.0
    assert data["subagents_usd"] == 0.5
    assert data["spans"] == {"agent:Explore": 0.5}
    assert set(data["sessions"]) == {"sess-1", "sess-2"}


def test_branch_filter_none_bucket(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "(none)", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["records"] == 1
    assert data["total_usd"] == 8.0


def test_branch_plus_session_splits_switched_session(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "feat/x", "--session", "sess-1", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["session"] == "sess-1"
    assert data["total_usd"] == 2.0  # only sess-1's feat/x record, not the main one


def test_branch_filter_no_match_errors(monkeypatch, capsys):
    _seed()
    code, _ = _run(monkeypatch, capsys, "--branch", "no-such-branch")
    assert code == 1


def test_branch_report_text_output(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "feat/x")
    assert code == 0
    assert "Branch feat/x" in out
    assert "$6.50" in out
