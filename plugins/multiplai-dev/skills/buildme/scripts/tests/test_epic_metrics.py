"""The epic metrics script, offline: every metric on a one-child fixture, the
open-PR and merged-PR diff ranges, missing sources, and a killed write (row S1
of plan-factory-merged-2026-10-29.md)."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "evals" / "epic_metrics.py"


@pytest.fixture(scope="module")
def metrics():
    spec = importlib.util.spec_from_file_location("epic_metrics", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_check_passes(metrics):
    result = metrics.self_check()
    assert result == {"ok": True, "wrong": {}}


def test_every_metric_on_an_open_pr(metrics, tmp_path):
    fixture = metrics.build_fixture(tmp_path)
    result = metrics.compute(fixture["epic_dir"], fixture["projects"], fixture["csv"])
    (row,) = result["children"]
    for key, want in fixture["expected"].items():
        assert row[key] == want, key
    # The PR is open, so its diff is the feature branch...HEAD.
    assert row["diff_range"].startswith("origin/feat...")
    assert row["fix_merge_usd"] == 1.5
    assert row["cache_write_5m_share"] == 0.0
    assert result["totals"]["usd_per_accepted"] == 18.5


def test_unstarted_child_is_left_out(metrics, tmp_path):
    fixture = metrics.build_fixture(tmp_path)
    result = metrics.compute(fixture["epic_dir"], fixture["projects"], fixture["csv"])
    assert [c["child"] for c in result["children"]] == ["c1"]


def test_test_minutes_missing_without_a_test_run_line(metrics, tmp_path):
    fixture = metrics.build_fixture(tmp_path)
    log = fixture["epic_dir"].parents[2] / "logs" / "build-pipeline-2026-10-28.log"
    log.write_text("[2026-10-28T01:00:00Z] [x] INFO: [change:chg/c1] DONE block=1\n")
    (row,) = metrics.compute(fixture["epic_dir"], fixture["projects"])["children"]
    assert row["test_minutes"] is None
    assert row["test_minutes_missing"] is True
    assert row["hand_interventions"] == 0


def test_a_review_round_after_a_passed_round_counts_whatever_its_kind(
    metrics, tmp_path
):
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    for name, kind, passed in (
        ("block-01-round-1.json", "review", True),
        ("block-01-round-2.json", "fix_on_pass_review", True),
        ("block-02-round-1.json", "review", False),
        ("block-02-round-2.json", "review", False),
    ):
        (reviews / name).write_text(
            json.dumps({"kind": kind, "gate": {"passed": passed}})
        )
    assert metrics.block_rounds(tmp_path) == {
        "block_review_rounds": 4,
        "rounds_after_pass": 1,
    }


def test_idle_gaps_count_log_lines_of_hand_run_builds(metrics, tmp_path):
    log = tmp_path / "build-pipeline-2026-10-28.log"
    log.write_text(
        "[2026-10-28T03:00:00Z] [x] INFO: [change:chg/c1] START block=1\n"
        "[2026-10-28T03:20:00Z] [x] INFO: [change:other/c1] START block=1\n"
    )
    records = [
        {
            "at": "2026-10-28T01:00:00+00:00",
            "result": {"at": "2026-10-28T02:00:00+00:00"},
        },
        {
            "at": "2026-10-28T05:00:00+00:00",
            "result": {"at": "2026-10-28T05:10:00+00:00"},
        },
    ]
    gaps = metrics.idle_gaps(records, metrics.log_activity([log], "chg"))
    assert [g["hours"] for g in gaps] == [1.0, 2.0]


def test_s1_killed_while_writing_leaves_previous_output(metrics, tmp_path, monkeypatch):
    fixture = metrics.build_fixture(tmp_path)
    out = tmp_path / "out" / "metrics.json"
    argv = [
        "--epic-dir",
        str(fixture["epic_dir"]),
        "--transcripts-root",
        str(fixture["projects"]),
    ]
    assert metrics.main([*argv, "--output", str(out)]) == 0
    first = out.read_text()

    def killed(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(metrics.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        metrics.main([*argv, "--output", str(out)])
    monkeypatch.undo()
    # The previous output is untouched and no temp file is left behind.
    assert out.read_text() == first
    assert sorted(p.name for p in out.parent.iterdir()) == ["metrics.json"]
    # Resume: run again; the output equals an uninterrupted run's.
    assert metrics.main([*argv, "--output", str(out)]) == 0
    assert json.loads(out.read_text()) == json.loads(first)


def test_merged_pr_diff_uses_the_merge_commits_first_parent(metrics, tmp_path):
    fixture = metrics.build_fixture(tmp_path)
    wt = fixture["wt"]

    def git(*args):
        subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True)

    git("checkout", "-q", "feat")
    git("merge", "-q", "--no-ff", "-m", "merge child", "child")
    git("push", "-q", "origin", "feat")
    git("checkout", "-q", "child")
    git("fetch", "-q", "origin")
    (row,) = metrics.compute(fixture["epic_dir"], fixture["projects"])["children"]
    assert not row["diff_range"].startswith("origin/")
    assert row["added_lines"] == 5


def test_cli_refuses_a_directory_without_epic_json(metrics, tmp_path):
    with pytest.raises(SystemExit) as exc:
        metrics.main(["--epic-dir", str(tmp_path), "--transcripts-root", str(tmp_path)])
    assert exc.value.code == 2


def test_self_check_exit_code(metrics):
    assert metrics.main(["--self-check"]) == 0
    assert os.path.exists(MODULE)


def test_empty_search_pattern_matches_the_review_skills(metrics):
    """The script's copy of the review's `round_metrics.EMPTY_SEARCH_RE`, read
    from the repo tree (buildme never imports review_pipeline)."""
    import ast

    source = (
        MODULE.parents[3]
        / "review"
        / "scripts"
        / "review_pipeline"
        / "round_metrics.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "EMPTY_SEARCH_RE" for t in node.targets
        ):
            pattern = ast.literal_eval(node.value.args[0])
            break
    else:
        pytest.skip("the review skill has no EMPTY_SEARCH_RE yet (PR #467 adds it)")
    assert metrics.EMPTY_SEARCH_RE.pattern == pattern
