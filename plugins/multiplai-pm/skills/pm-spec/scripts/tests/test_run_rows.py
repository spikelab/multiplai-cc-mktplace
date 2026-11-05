"""The run row (`pm_spec_pipeline.runrow`): one line in `<data_dir>/runs.jsonl`
per invocation that targeted a change, written once the command has ended.
`runs_file` (conftest) keeps the file in the test's own folder."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import BRANCH, TICKET, run
from multiplai_core import runs
from pm_spec_pipeline import runrow

PLUGIN_JSON = Path(__file__).resolve().parents[4] / ".claude-plugin" / "plugin.json"


def rows(path: Path) -> list[dict]:
    found, skipped = runs.read_rows(path)
    assert skipped == 0
    return found


def test_a_run_stopping_at_a_review_stop_writes_one_waiting_row(repo, runs_file):
    assert run(repo, "new", "throttle", "--interview-mode", "self") == 3
    [row] = rows(runs_file)
    assert row["kind"] == "pm-spec" and row["outcome"] == "waiting"
    assert row["cause"] == "review-proposal"
    assert row["target"] == {
        "repo": "origin",  # the conftest's origin is `<tmp>/origin.git`
        "remote": str(repo.parent / "origin.git"),
        "branch": BRANCH,
        "change": "throttle",
        "ticket": TICKET,
        "subcommand": "new",
    }
    assert row["folder"] is None and row["requested_by"] is None
    assert row["run"]["stop"] == 1 and row["run"]["exit_code"] == 3
    assert row["run"]["steps"]["proposal"] == "passed"
    assert row["spend_usd"] > 0
    assert row["started_at"] <= row["finished_at"]


def test_the_producer_carries_the_plugin_version(repo, runs_file):
    version = json.loads(PLUGIN_JSON.read_text())["version"]
    assert run(repo, "new", "throttle", "--auto") == 0
    [row] = rows(runs_file)
    assert row["producer"] == f"multiplai-pm:pm-spec {version}"


def test_a_handed_off_run_writes_succeeded_at_stop_3(repo, runs_file):
    assert run(repo, "new", "throttle", "--auto") == 0
    [row] = rows(runs_file)
    assert row["outcome"] == "succeeded" and row["cause"] is None
    assert row["run"]["stop"] == 3 and row["run"]["exit_code"] == 0
    assert row["run"]["steps"]["specs"] == "skipped"  # --auto: no review stop


def test_each_invocation_appends_its_own_row(repo, runs_file):
    assert run(repo, "new", "throttle", "--interview-mode", "self") == 3
    assert run(repo, "resume", "throttle") == 3
    first, second = rows(runs_file)
    assert (first["target"]["subcommand"], second["target"]["subcommand"]) == (
        "new",
        "resume",
    )
    assert second["cause"] == "review-components" and second["run"]["stop"] == 2


def test_a_refusal_writes_failed_with_its_cause(repo, runs_file):
    from pm_spec_pipeline.__main__ import main

    assert main(["new", "throttle", "--project-dir", str(repo), "--auto"]) == 2
    [row] = rows(runs_file)
    assert row["outcome"] == "failed" and row["cause"] == "no-ticket"
    assert row["target"]["ticket"] is None and row["target"]["branch"] is None
    assert row["run"] == {"stop": None, "steps": {}, "exit_code": 2}


def test_resume_with_no_run_writes_failed_no_change(repo, runs_file):
    assert run(repo, "resume", "throttle") == 2
    [row] = rows(runs_file)
    assert (row["outcome"], row["cause"]) == ("failed", "no-change")


def test_the_budget_writes_stopped(repo, runs_file):
    assert run(repo, "new", "throttle", "--auto", "--max-usd", "0.001") == 1
    [row] = rows(runs_file)
    assert (row["outcome"], row["cause"]) == ("stopped", "budget")


def test_an_argument_refused_before_the_change_writes_no_row(repo, runs_file):
    assert run(repo, "new", "throttle/api", "--auto") == 2
    assert not runs_file.exists()


def test_a_failing_row_never_fails_the_run(repo, runs_file, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("no row")

    monkeypatch.setattr(runrow, "make", broken)
    assert run(repo, "new", "throttle", "--auto") == 0
    assert not runs_file.exists()


def test_every_cause_a_stop_carries_is_listed():
    """Each `Stop(..., "<cause>")` in the package names a cause in `CAUSES`."""
    import re

    package = Path(runrow.__file__).parent
    found = set()
    for path in package.glob("*.py"):
        text = path.read_text()
        found |= set(re.findall(r'Stop\([^)]*?,\s*"([a-z-]+)"\)', text))
        found |= set(
            re.findall(r'_(?:refuse|fail)\([^;]*?,\s*"([a-z-]+)",?\s*\)', text)
        )
    assert found and found <= set(runrow.CAUSES), found - set(runrow.CAUSES)
    for stage in runrow.STOPS:
        assert f"review-{stage}" in runrow.CAUSES
