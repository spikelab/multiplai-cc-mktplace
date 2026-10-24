"""One case per row of the stop-and-resume table in plan-review-pieces-dimensions-2026-10-24.md.

Each case stops a change review at that row's point, or applies its change,
resumes it, and compares the result with a run of the same repository that
never stopped: the `finder_reports` keys, the pieces, the dimensions and the
names of the `report-*.md` files, which match unless the row's "Resume does"
says otherwise. Row S13 (the stopped process still alive) is the run lock's,
tested in `test_data_dir.py`
(`test_a_review_running_now_is_not_read_as_interrupted_and_nothing_starts_a_second_run_in_its_directory`).

The model is `test_reported_finders.Model`, faked at the SDK's two calls; the
usage-window rows (S7, S8) fake `run_agent` instead, so every call passes the
window check, as in `test_window.py`.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest
import test_reported_finders
import test_window
from test_reported_finders import (
    DIMENSIONS,
    git,
    make_repo,
    name,
    resume,
    review,
    saved,
    two_file_repo,
)
from test_window import Endpoint, answer, finder_labels, use

# The fixtures this file shares with the files that define them.
model = test_reported_finders.model
real_read = test_window.real_read
run_agent = test_window.run_agent


TWO_DIMENSIONS = "dimensions: [diff-bugs, callers]\n"


def shape(out: Path) -> dict:
    """What a resumed run must share with one that never stopped."""
    target_dir, state = saved(out)
    return {
        "keys": sorted(state.finder_reports),
        "pieces": [p.model_dump() for p in state.pieces or []],
        "dimensions": state.dimensions,
        "reports": sorted(p.name for p in target_dir.glob("report-*.md")),
    }


def reference(repo, base, head, tmp_path, make, *, yaml: str = "", **kwargs) -> dict:
    """The same review, never stopped: a fresh fake with *kwargs*, in an output directory of its own."""
    out = tmp_path / "whole"
    make(**kwargs)
    assert review(repo, base, head, out, yaml=yaml) == 0
    return shape(out)


def asked(fake) -> list[str]:
    return [c["key"] for c in fake.report_calls]


def assert_asked_once(fake, *stopped: str) -> None:
    """Every report key was asked once, but each *stopped* key: asked when the stop hit it, then again."""
    keys = asked(fake)
    for key in set(keys):
        assert keys.count(key) == (2 if key in stopped else 1), key


# --- S1 --------------------------------------------------------------------------------------------------------


def test_s1_a_stop_after_prepare_reuses_the_stored_pieces_and_dimensions(
    tmp_path, model, monkeypatch
):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model)
    orchestrator = importlib.import_module("review_pipeline.orchestrator")
    real = dict(orchestrator.STAGE_FUNCTIONS)["find"]

    async def killed(state, ctx):
        raise RuntimeError("killed before find")

    stages = list(orchestrator.STAGE_FUNCTIONS)
    monkeypatch.setattr(
        orchestrator,
        "STAGE_FUNCTIONS",
        [(n, killed if n == "find" else f) for n, f in stages],
    )
    fake = model()
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="killed"):
        review(repo, base, head, out)
    target_dir, stopped = saved(out)
    assert not stopped.past("find") and stopped.finder_reports == {}
    assert [p.model_dump() for p in stopped.pieces] == whole["pieces"]
    assert stopped.dimensions == whole["dimensions"] == list(DIMENSIONS)
    monkeypatch.setattr(
        orchestrator,
        "STAGE_FUNCTIONS",
        [(n, real if n == "find" else f) for n, f in stages],
    )
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert_asked_once(fake)


# --- S2, S3, S12 ---------------------------------------------------------------------------------------------


def test_s2_a_stop_between_keys_asks_only_the_keys_with_no_converted_report(
    tmp_path, model
):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model)
    fake = model(
        stop_report=2
    )  # diff-bugs on the second piece: piece 1's six keys are done
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, stopped = saved(out)
    assert sorted(
        k for k in stopped.finder_reports if not k.startswith("path@")
    ) == sorted(f"{d}@1" for d in DIMENSIONS)
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert_asked_once(fake, "diff-bugs@2")


def test_s3_a_stop_between_a_report_and_its_convert_asks_only_the_convert(
    tmp_path, model
):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model)
    fake = model(stop_convert="diff-bugs@2")
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, stopped = saved(out)
    report = stopped.finder_reports["diff-bugs@2"]
    assert report.text and not report.converted
    converts = len(fake.convert_calls)
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert_asked_once(fake)
    again = [c["key"] for c in fake.convert_calls[converts:]]
    assert again.count("diff-bugs@2") == 1 and "diff-bugs@1" not in again


def test_s12_the_same_step_stopped_twice_asks_nothing_twice(tmp_path, model):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model)
    fake = model(stop_report=2)
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, _ = saved(out)
    fake.stop_report = len(fake.text_calls) + 2  # after diff-bugs@2 again: piece 3's
    assert resume(target_dir) == 4
    _, twice = saved(out)
    assert {"diff-bugs@1", "diff-bugs@2"} <= set(twice.finder_reports)
    assert "diff-bugs@3" not in twice.finder_reports
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert_asked_once(fake, "diff-bugs@2", "diff-bugs@3")


# --- S4, S5: a piece cut in two ----------------------------------------------------------------------------


def test_s4_a_stop_after_the_halves_are_saved_asks_each_half_and_cuts_nothing_again(
    tmp_path, model
):
    repo, base, head = two_file_repo(tmp_path)
    whole = reference(repo, base, head, tmp_path, model, turn_limit=("callers@2",))
    fake = model(turn_limit=("callers@2",), stop_key="callers@4")
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, stopped = saved(out)
    by_id = {p.id: p for p in stopped.pieces}
    assert by_id[2].split_into == [4, 5]
    assert by_id[4].split_for == by_id[5].split_for == ["callers"]
    assert not {"callers@4", "callers@5"} & set(stopped.finder_reports)
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert (
        asked(fake).count("callers@2") == 1
    )  # the original is not asked again for callers
    assert_asked_once(fake, "callers@4")


def test_s5_a_stop_after_one_half_asks_only_the_other(tmp_path, model):
    repo, base, head = two_file_repo(tmp_path)
    whole = reference(repo, base, head, tmp_path, model, turn_limit=("callers@2",))
    fake = model(turn_limit=("callers@2",), stop_key="callers@5")
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, stopped = saved(out)
    assert stopped.finder_reports["callers@4"].converted
    assert "callers@5" not in stopped.finder_reports
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert_asked_once(fake, "callers@5")


# --- S6: a checkpoint written before this change ---------------------------------------------------------


def legacy_finding() -> dict:
    quote = f"{name(3)}_2 = 2"
    return {
        "claim": "the old callers finder's finding on p03",
        "severity": "MEDIUM",
        "dimension": "callers",
        "file": f"{name(3)}/m.py",
        "line_start": 2,
        "line_end": 2,
        "failure_scenario": "a caller reads p03_2",
        "citations": [
            {"path": f"{name(3)}/m.py", "line_start": 2, "line_end": 2, "quote": quote}
        ],
        "finder": "callers",
    }


def test_s6_an_old_checkpoint_loads_with_the_default_dimensions_and_keeps_its_results(
    tmp_path, model
):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model)
    fake = model(stop_report=2)
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, _ = saved(out)
    # Rewrite the checkpoint as multiplai-dev 0.105 wrote it: a piece finder's `piece@1`, a bare
    # `callers` result, no `dimensions` and no split fields.
    path = target_dir / "review-state.json"
    data = json.loads(path.read_text())
    reports = data["finder_reports"]
    old = reports.pop("diff-bugs@1")
    reports["piece@1"] = {**old, "key": "piece@1"}
    reports.pop("callers@1")
    data["finder_results"] = {"callers": {"findings": [legacy_finding()]}}
    data.pop("dimensions")
    for piece in data["pieces"]:
        for field in ("split_from", "split_into", "split_for", "hunks"):
            piece.pop(field, None)
    path.write_text(json.dumps(data))
    (target_dir / "report-diff-bugs-1.md").rename(target_dir / "report-piece-1.md")

    assert resume(target_dir) == 0
    resumed = shape(out)
    assert resumed["dimensions"] == list(DIMENSIONS)
    assert resumed["keys"] == whole["keys"] and resumed["pieces"] == whole["pieces"]
    assert resumed["reports"] == sorted([*whole["reports"], "report-piece-1.md"])
    keys = asked(fake)
    assert keys.count("diff-bugs@1") == 1  # treated as diff-bugs@1: not asked again
    assert keys.count("callers@1") == 2  # dropped from the old checkpoint: asked again
    _, state = saved(out)
    assert "the old callers finder's finding on p03" in [
        f.claim for f in state.findings
    ]


# --- S7, S8: the usage window -----------------------------------------------------------------------------


def window_reference(repo, base, head, tmp_path, monkeypatch) -> dict:
    use(monkeypatch, Endpoint(answer(utilization=20.0, remaining=500.0)))
    out = tmp_path / "whole"
    assert review(repo, base, head, out) == 0
    return shape(out)


def test_s7_a_full_window_stops_before_a_call_and_resume_asks_only_the_missing_keys(
    tmp_path, real_read, monkeypatch, run_agent, capsys
):
    repo, base, head = make_repo(tmp_path, 2)
    whole = window_reference(repo, base, head, tmp_path, monkeypatch)
    before = len(finder_labels(run_agent))
    # Room for the first piece's six reports, then the window reads full.
    use(
        monkeypatch,
        Endpoint(*[answer(utilization=20.0, remaining=500.0)] * 7, answer(99.0, 0.5)),
    )
    monkeypatch.setattr(
        importlib.import_module("review_pipeline.window"), "READ_EVERY_S", 0.0
    )
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 6
    err = capsys.readouterr().err
    assert "five_hour usage window" in err and "utilization 99%" in err
    target_dir, stopped = saved(out)
    done = set(stopped.finder_reports)
    assert done and not stopped.past("find")
    first = finder_labels(run_agent)[before:]
    use(monkeypatch, Endpoint(answer(utilization=99.0, remaining=0.5)))
    assert resume(target_dir) == 6  # still full: stops again before starting a call
    assert finder_labels(run_agent)[before:] == first
    use(monkeypatch, Endpoint(answer(utilization=20.0, remaining=500.0)))
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert len(finder_labels(run_agent)) - before == 2 * len(DIMENSIONS) + 1


def test_s8_an_unreadable_endpoint_at_resume_prints_the_notice_and_runs_on(
    tmp_path, real_read, monkeypatch, run_agent, capsys, caplog
):
    caplog.set_level(logging.INFO)
    repo, base, head = make_repo(tmp_path, 2)
    whole = window_reference(repo, base, head, tmp_path, monkeypatch)
    use(monkeypatch, Endpoint(answer(utilization=99.0, remaining=0.5)))
    real_read[0] += 60  # past the reading the reference run kept
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 6
    capsys.readouterr()
    target_dir, _ = saved(out)
    use(monkeypatch, Endpoint(error=TimeoutError("timed out")))
    real_read[0] += 60
    assert resume(target_dir) == 0
    err = capsys.readouterr().err
    assert err.count("NOTICE: usage window not readable (TimeoutError)") == 1
    assert shape(out) == whole


# --- S9 ------------------------------------------------------------------------------------------------------


def test_s9_resume_keeps_the_stopped_runs_dimensions_over_a_changed_review_yaml(
    tmp_path, model, capsys
):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model, yaml=TWO_DIMENSIONS)
    fake = model(stop_report=2)
    out = tmp_path / "out"
    assert review(repo, base, head, out, yaml=TWO_DIMENSIONS) == 4
    target_dir, _ = saved(out)
    (out / "review.yaml").write_text("concurrency: 1\n")  # the default dimensions now
    capsys.readouterr()
    assert resume(target_dir) == 0
    assert (
        "NOTICE: dimensions diff-bugs, callers kept from the stopped run; review.yaml says "
        "diff-bugs, callers, history, conventions"
    ) in capsys.readouterr().err
    assert shape(out) == whole
    assert {k.partition("@")[0] for k in asked(fake)} == {"diff-bugs", "callers"}


# --- S10, S11 -------------------------------------------------------------------------------------------------


def test_s10_s11_resume_reviews_the_stored_commits_after_the_branch_and_working_tree_move(
    tmp_path, model
):
    repo, base, head = make_repo(tmp_path, 3)
    whole = reference(repo, base, head, tmp_path, model)
    fake = model(stop_report=2)
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 4
    target_dir, stopped = saved(out)
    # The branch moves on, and the working tree holds an edit nobody committed.
    (repo / name(1) / "late.py").write_text("late = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a later commit")
    (repo / name(2) / "m.py").write_text("edited in the working tree\n")
    asked_before = len(fake.report_calls)
    assert resume(target_dir) == 0
    _, resumed = saved(out)
    assert (resumed.target.base_sha, resumed.target.head_sha) == (base, head)
    assert (stopped.target.base_sha, stopped.target.head_sha) == (base, head)
    later = fake.report_calls[asked_before:]
    assert later and not any(
        "late.py" in c["prompt"] or "edited in the working tree" in c["prompt"]
        for c in later
    )
    assert shape(out) == whole
