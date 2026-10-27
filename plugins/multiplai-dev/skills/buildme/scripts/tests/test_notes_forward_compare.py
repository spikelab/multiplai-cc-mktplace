"""The notes-forward benchmark harness, offline: fixtures, transcript reading,
the decision rules, and a killed run counted as a failed observation (row S11
of plan-buildme-carry-block-notes-2026-10-27.md)."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "evals" / "notes_forward_compare.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("notes_forward_compare", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_check_passes_for_every_fixture(bench):
    result = bench.self_check()
    assert result["ok"], result
    assert {r["fixture"] for r in result["fixtures"]} == set(bench.MANIFEST)


def _session(cwd, prompt, usages, model="claude-x"):
    rows = [
        {"type": "user", "cwd": cwd, "timestamp": "t0", "message": {"content": prompt}}
    ]
    for i, usage in enumerate(usages):
        rows.append(
            {
                "type": "assistant",
                "cwd": cwd,
                "message": {"id": f"m{i}", "model": model, "usage": usage},
            }
        )
    return rows


def test_transcripts_give_turns_and_end_context_per_block(bench, tmp_path):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    folder = (
        tmp_path / "config" / "projects" / bench.re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    )
    folder.mkdir(parents=True)
    implementer = "You are an implementation agent working in a TDD pipeline.\n\n## Block: Money\n"
    usage = {
        "input_tokens": 1,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 9,
    }
    sessions = {
        "a.jsonl": _session(str(cwd), implementer, [usage, usage]),
        "b.jsonl": _session(str(cwd), "You are a test writer", [usage]),
        "c.jsonl": _session(str(tmp_path / "elsewhere"), implementer, [usage]),
    }
    for name, rows in sessions.items():
        (folder / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    calls = bench.implementer_calls(bench.transcripts_for(cwd, tmp_path / "config"))
    assert calls["blocks"] == {
        "Money": [{"started": "t0", "turns": 2, "end_context_tokens": 110}]
    }
    assert calls["models"] == ["claude-x"]


def _row(fixture, arm, repeat, *, ok=True, cost=1.0, turns=10, claimed=None):
    return {
        "fixture": fixture,
        "arm": arm,
        "repeat": repeat,
        "acceptance": ok,
        "claimed_success": ok if claimed is None else claimed,
        "false_success": (ok if claimed is None else claimed) and not ok,
        "timeout": False,
        "cost_usd": cost,
        "served_models": ["claude-x"],
        "blocks": [
            {
                "number": n,
                "implementer_turns": turns,
                "implementer_end_context_tokens": 1000,
            }
            for n in (1, 2, 3, 4)
        ],
    }


def _sweep(notes_ok=True, notes_cost=1.0, notes_turns=10, false_success=False):
    rows = []
    for fixture in ("string-ids", "control-text"):
        for repeat in range(3):
            rows.append(_row(fixture, "current", repeat, ok=repeat > 0))
            rows.append(
                _row(
                    fixture,
                    "notes",
                    repeat,
                    ok=notes_ok,
                    cost=notes_cost,
                    turns=notes_turns,
                    claimed=True if false_success else None,
                )
            )
    return rows


def test_verdict_turns_the_default_on_for_a_clear_win(bench):
    v = bench.verdict(_sweep(notes_turns=8), window_tokens=None)
    assert v["planted_acceptance_gain"] == 1
    assert v["planted_dependent_turn_drop"] == pytest.approx(0.2)
    assert v["default_on"] is True
    assert v["propose_session_resume"] is None


def test_verdict_keeps_it_off_when_cost_rises_more_than_10pct(bench):
    assert (
        bench.verdict(_sweep(notes_turns=8, notes_cost=1.2), None)["default_on"]
        is False
    )


def test_verdict_keeps_it_off_on_a_false_success(bench):
    v = bench.verdict(_sweep(notes_ok=False, notes_turns=8, false_success=True), None)
    assert v["no_false_success"] is False
    assert v["default_on"] is False


def test_verdict_proposes_resume_only_when_it_misses_and_runs_fit(bench):
    rows = _sweep(notes_turns=10)
    assert bench.verdict(rows, window_tokens=100_000)["propose_session_resume"] is True
    assert bench.verdict(rows, window_tokens=5_000)["propose_session_resume"] is False


def test_comparable_needs_every_pair_and_one_served_model(bench):
    rows = _sweep()
    assert bench.comparable(rows, 3) == (True, [])
    rows[-1]["served_models"] = ["claude-y"]
    ok, reasons = bench.comparable(rows[:-2] + rows[-1:], 3)
    assert not ok
    assert any("2 of 3 runs" in r for r in reasons)
    assert any("served models differ" in r for r in reasons)


def test_markdown_starts_with_the_comparability_line(bench):
    rows = _sweep(notes_turns=8)
    report = {
        "comparable": False,
        "not_comparable_reasons": ["x"],
        "model": "claude-x",
        "effort": "high",
        "repetitions": 3,
        "verdict": bench.verdict(rows, None),
    }
    text = bench.markdown(report)
    assert text.splitlines()[0] == "NOT COMPARABLE"
    assert "Default: on" in text


def test_s11_a_killed_run_is_a_failed_observation(bench, tmp_path, monkeypatch):
    """S11: a run that hits the timeout is killed with its process group and
    counted as failed; nothing resumes it."""
    monkeypatch.setattr(
        bench,
        "build_argv",
        lambda checkout: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    args = argparse.Namespace(model="m", effort="high", per_run_usd=1.0, timeout=1)
    row = bench.measure("control-text", "notes", 0, tmp_path, args)
    assert row["timeout"] is True
    assert row["claimed_success"] is False
    assert row["acceptance"] is False
    assert row["cost_usd"] is None
    config = json.loads(
        (tmp_path / "control-text-notes-0" / "specs" / "config.yaml").read_text()
    )
    assert config["carry_block_notes"] is True
