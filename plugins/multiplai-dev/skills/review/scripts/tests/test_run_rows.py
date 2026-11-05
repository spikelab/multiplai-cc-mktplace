"""Each review run that ends appends one row to `<data_dir>/runs.jsonl` (`multiplai_core.runs`).

conftest points `ledger.data_dir` at `tmp_path / "data"`, and the row is written
to `ledger.data_dir() / "runs.jsonl"`, so nothing here touches a real data directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from review_pipeline import sdk
from review_pipeline.__main__ import main
from review_pipeline.export import producer
from test_orchestrator import FakeAgents, _review_args


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> FakeAgents:
        fake = FakeAgents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake

    return install


def _rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "data" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _folder(out: Path, base: str, head: str) -> Path:
    return out / "booking-engine" / f"range-{base}..{head}"


def test_a_succeeded_run_appends_one_row_with_its_target_and_run(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    out = tmp_path / "data" / "reviews"
    agents(cost=1.0)
    assert main(_review_args(repo, base, head, out)) == 0

    [row] = _rows(tmp_path)
    folder = _folder(out, base, head)
    assert row["row_version"] == 1
    assert row["producer"] == producer()
    assert row["kind"] == "review"
    assert row["outcome"] == "succeeded"
    assert row["cause"] is None
    assert row["requested_by"] is None
    assert row["folder"] == f"reviews/booking-engine/range-{base}..{head}"
    target = row["target"]
    assert target["repo"] == "booking-engine"
    assert target["object"] == folder.name
    assert target["kind"] == "range"
    assert target["head_sha"] == head
    assert target["base_sha"] == base
    assert target["pr"] is None
    assert "slug" in target and "remote" in target
    # The row's run is the ledger's run.
    finished = [
        e
        for e in map(
            json.loads,
            (tmp_path / "data" / "reviews" / "ledger.jsonl").read_text().splitlines(),
        )
        if e["event"] == "finished"
    ]
    assert row["run"] == finished[-1]["run"]
    assert row["spend_usd"] == row["run"]["cost_usd"] > 0
    assert row["started_at"] <= row["run"]["started_at"]
    assert row["finished_at"] == row["run"]["ended_at"]


def test_a_run_outside_the_data_dir_names_its_folder_by_absolute_path(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    [row] = _rows(tmp_path)
    assert row["folder"] == str(_folder(out, base, head).resolve())


def test_a_budget_stop_appends_a_stopped_row_with_its_cause(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    agents(budget_stop_at=3)
    assert main(_review_args(repo, base, head, tmp_path / "out")) == 4
    [row] = _rows(tmp_path)
    assert row["outcome"] == "stopped"
    assert row["cause"] == "budget"


def test_a_usage_limit_stop_then_resume_appends_a_row_for_each_run(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    fake = agents(limit_at=3)
    args = _review_args(repo, base, head, out, "--requested-by", "DB-1-epic:child-a")
    assert main(args) == 6
    fake.limit_at = None
    folder = _folder(out, base, head)
    assert (
        main(
            [
                "resume",
                str(folder),
                "--trust-repo",
                "--requested-by",
                "DB-1-epic:child-a",
            ]
        )
        == 0
    )
    stopped, resumed = _rows(tmp_path)
    assert (stopped["outcome"], stopped["cause"]) == ("stopped", "usage-limit")
    assert (resumed["outcome"], resumed["cause"]) == ("succeeded", None)
    for row in (stopped, resumed):
        assert row["requested_by"] == {"branch": "DB-1-epic", "child": "child-a"}


def test_a_resumed_review_row_holds_only_that_run_s_spend_and_start(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    fake = agents(limit_at=3, cost=1.0)
    assert main(_review_args(repo, base, head, out)) == 6
    fake.limit_at = None
    state_file = _folder(out, base, head) / "review-state.json"
    saved = json.loads(state_file.read_text())
    first_start = "2026-01-01T00:00:00Z"
    saved["timings"]["run"][0]["started_at"] = first_start
    state_file.write_text(json.dumps(saved))
    assert main(["resume", str(state_file.parent), "--trust-repo"]) == 0

    stopped, resumed = _rows(tmp_path)
    assert stopped["spend_usd"] > 0
    total = resumed["run"]["cost_usd"]
    assert total > stopped["spend_usd"]
    assert stopped["spend_usd"] + resumed["spend_usd"] == pytest.approx(total)
    assert resumed["started_at"] > first_start


def test_requested_by_without_a_child_has_a_null_child(fixture_repo, tmp_path, agents):
    repo, base, head = fixture_repo
    agents()
    args = _review_args(repo, base, head, tmp_path / "out", "--requested-by", "DB-7-x")
    assert main(args) == 0
    [row] = _rows(tmp_path)
    assert row["requested_by"] == {"branch": "DB-7-x", "child": None}


def test_a_run_that_raises_appends_a_failed_row_and_the_error_still_propagates(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    agents(kill_at="merge")
    with pytest.raises(RuntimeError, match="killed"):
        main(_review_args(repo, base, head, tmp_path / "out"))
    [row] = _rows(tmp_path)
    assert (row["outcome"], row["cause"]) == ("failed", "RuntimeError")


def test_a_row_that_cannot_be_written_does_not_fail_the_review(
    fixture_repo, tmp_path, agents, monkeypatch
):
    from multiplai_core import runs

    def broken(**kwargs):
        raise ValueError("bad row")

    monkeypatch.setattr(runs, "make_row", broken)
    repo, base, head = fixture_repo
    agents()
    assert main(_review_args(repo, base, head, tmp_path / "out")) == 0
    assert _rows(tmp_path) == []
