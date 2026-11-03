"""Items 2 and 3 of plan-buildme-epic-speedups-2026-11-03.md: a findings
fixer skips the TDD engine's baseline when a passing whole-suite run is
recorded at the PR head (`code.recorded_pass`, `code.baseline_reuse_for`,
the engine's `BASELINE SKIPPED`), and a red baseline's reason is saved
(`TDDState.baseline_failure`) and named in the park reason.

The rows S1-S8 ("Stop and resume states") run the same findings round
twice, as `test_findings_stop_resume_d1` does: once never stopped, once
stopped at the row's point (a forked child exiting 137) or perturbed, and
resumed. Git is real; every model and GitHub call is a fake.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import git_ops, tdd_engine
from build_pipeline.findings import code, loop
from build_pipeline.gates import RunnerFailed, final_verification, verification_inputs
from build_pipeline.models import AgentResult, GateResult
from build_pipeline.state import BuildState

from tests.row_helpers import assert_same_as_unstopped, repo_state
from tests.test_findings_round import (  # noqa: F401  (fixtures)
    ARCHIVED,
    _git,
    _make_built,
    _origin_with_one_more_commit,
    built,
)
from tests.test_findings_self_check import _log, calls
from tests.test_findings_stop_resume_d1 import (
    _kill_at,
    _killed,
    _Round,
)
from tests.test_tdd_engine import tdd_setup  # noqa: F401  (fixture)

SOURCE = ".build-state.json"
# The recorded pass `_record_pass` writes stays at H, the head before the
# round; `row_helpers` checks a saved pass against the checkout, so it is
# left out of every comparison (both runs hold the same one).
RECORD = ("*/.build-state.json",)
# What differs between a run that skipped its baseline and one that ran it:
# the saved `baseline_reuse` and the progress file's BASELINE line.
REUSE_BOOKKEEPING = (*RECORD, "*.findings-state.json", "build-progress.md")


@pytest.fixture
def built_repo(request):
    """`test_findings_round.built` (a finished, archived build), trusted."""
    return request.getfixturevalue("built")


@pytest.fixture
def engine_setup(request):
    """`test_tdd_engine.tdd_setup`: a one-block change ready for the engine."""
    return request.getfixturevalue("tdd_setup")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _record_pass(path: Path, repo: Path, config, **override) -> dict:
    """Write a passed `verification` into the state file *path*, as a build's
    final verification saves it: the real gate, run at the current HEAD.
    *override* replaces fields of its `metadata.inputs` (or `passed`)."""
    gate = final_verification(
        config.test_command, repo, parallel=config.parallel_tests
    ).model_dump()
    assert gate["passed"], gate
    if "passed" in override:
        gate["passed"] = override.pop("passed")
    gate["metadata"]["inputs"].update(override)
    data = json.loads(path.read_text()) if path.exists() else {}
    data["verification"] = gate
    path.write_text(json.dumps(data, indent=2))
    return gate


def _round_state(change: Path):
    return BuildState.load(change / loop.STATE_FILE).findings.rounds[-1]


def _engine_state(change: Path, n: int = 1) -> BuildState:
    return BuildState.load(change / code.engine_state_name(n))


def _progress(repo: Path) -> str:
    path = repo / "build-progress.md"
    return path.read_text() if path.exists() else ""


class _ReuseRound(_Round):
    """`_Round` with no self-check problem and a passing whole-suite run
    recorded at the PR head in `.build-state.json` (when *record*), logging
    each `recorded_pass` call, each engine run (whether it was handed
    `baseline_reuse`, and whether that names HEAD and the recorded head) and
    each baseline gate run. *red* makes the baseline gate fail with that
    reason."""

    def __init__(self, base: Path, monkeypatch, *, record: bool = True):
        super().__init__(base, monkeypatch, "")
        self.base = base
        self.recorded = _head(self.repo)
        self.red: str | None = None
        if record:
            _record_pass(self.change / SOURCE, self.repo, self.config)

    def spies(self):
        log, repo = self.log, self.repo
        real_engine = tdd_engine.run_tdd_engine
        real_gate = tdd_engine.baseline_test_gate
        real_found = code.recorded_pass

        async def engine(config, args, **kw):
            reuse = kw.get("baseline_reuse")
            if reuse is None:
                _log(log, "engine", "none")
            else:
                _log(
                    log,
                    "engine",
                    "reuse",
                    str(reuse[0] == _head(repo)),
                    str(reuse[1] == self.recorded),
                    reuse[2],
                )
            return await real_engine(config, args, **kw)

        def gate(*a, **kw):
            _log(log, "baseline", "")
            if self.red is not None:
                return GateResult(passed=False, reason=self.red, action="fix_tests")
            return real_gate(*a, **kw)

        def found(change_dir, project_dir, config):
            # The round files this round has written and not committed yet,
            # and whether its tasks file exists.
            dirty = _git(
                repo, "status", "--porcelain", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES
            )
            tasks = (Path(change_dir) / code.tasks_file_name(1)).exists()
            _log(log, "recorded_pass", dirty.strip(), str(tasks))
            return real_found(change_dir, project_dir, config)

        return [
            patch("build_pipeline.tdd_engine.run_tdd_engine", side_effect=engine),
            patch("build_pipeline.tdd_engine.baseline_test_gate", side_effect=gate),
            patch.object(code, "recorded_pass", side_effect=found),
        ]

    def run(self, *, patches=(), **kw) -> int:
        return super().run(patches=[*self.spies(), *patches], **kw)

    def count(self, kind: str) -> int:
        return sum(1 for c in calls(self.log) if c[0] == kind)

    def engines(self) -> list[tuple[str, ...]]:
        return [c[1:] for c in calls(self.log) if c[0] == "engine"]


SKIPPED = ("reuse", "True", "True", SOURCE)


def _clean(tmp_path: Path, monkeypatch, *, record: bool = True) -> _ReuseRound:
    clean = _ReuseRound(tmp_path / "clean", monkeypatch, record=record)
    assert clean.run() == 0, clean.lines
    return clean


def _like_unstopped(stopped, clean, extra_calls=()) -> None:
    """`test_findings_stop_resume_d1._assert_like_unstopped`, with the
    recorded pass left out of the repo comparison (`RECORD`)."""
    assert stopped.lines[-1] == clean.lines[-1], stopped.lines
    assert_same_as_unstopped(stopped.repo, clean.repo, RECORD)
    assert Counter(calls(stopped.log)) == Counter(calls(clean.log)) + Counter(
        extra_calls
    )
    assert stopped.writes() == clean.writes()


def _kill_before_engine():
    """Exit 137 at the round's first engine run, before its checkpoint exists."""

    async def engine(*_a, **_kw):
        os._exit(137)

    return patch("build_pipeline.tdd_engine.run_tdd_engine", side_effect=engine)


def _kill_after_engine_checkpoint(when):
    """Exit 137 right after the engine checkpoint for which *when(tdd)* holds."""
    real = BuildState.checkpoint

    def checkpoint(self, path, *a, **kw):
        real(self, path, *a, **kw)
        if Path(path).name == code.engine_state_name(1) and self.tdd and when(self.tdd):
            os._exit(137)

    return patch.object(BuildState, "checkpoint", checkpoint)


def _set_round_field(change: Path, **fields) -> None:
    path = change / loop.STATE_FILE
    data = json.loads(path.read_text())
    data["findings"]["rounds"][-1].update(fields)
    path.write_text(json.dumps(data, indent=2))


# --- recorded_pass -------------------------------------------------------------


class TestRecordedPass:
    @pytest.mark.parametrize("name", code.RECORDED_PASS_FILES)
    def test_it_matches_from_each_of_the_three_files(self, built_repo, name):
        repo, change, config = built_repo
        _record_pass(change / name, repo, config)
        assert code.recorded_pass(change, repo, config) == (_head(repo), name)

    def test_the_files_are_read_in_order(self, built_repo):
        repo, change, config = built_repo
        for name in code.RECORDED_PASS_FILES:
            _record_pass(change / name, repo, config)
        assert code.recorded_pass(change, repo, config) == (
            _head(repo),
            ".findings-state.json",
        )

    def test_a_later_file_matches_when_an_earlier_one_does_not(self, built_repo):
        repo, change, config = built_repo
        _record_pass(change / ".findings-state.json", repo, config, head="0" * 40)
        _record_pass(change / SOURCE, repo, config)
        assert code.recorded_pass(change, repo, config) == (_head(repo), SOURCE)

    def test_plan_revision_is_not_compared(self, built_repo):
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config, plan_revision="abc123")
        assert code.recorded_pass(change, repo, config) == (_head(repo), SOURCE)

    def test_d_and_item_2_a_reused_record_is_accepted(self, built_repo):
        """Item 6's reused `verification` (`code.reused_end_suite`'s shape):
        passed, `inputs` at HEAD, `reused_from` an older head."""
        repo, change, config = built_repo
        reused = GateResult(
            passed=True,
            reason="reused the end-of-build run at 0123abcd: nothing changed since",
            metadata={
                "inputs": verification_inputs(
                    config.test_command, repo, parallel=config.parallel_tests
                ),
                "reused_from": "0123abcd" * 5,
            },
        )
        (change / ".findings-state.json").write_text(
            json.dumps({"verification": reused.model_dump()})
        )
        assert code.recorded_pass(change, repo, config) == (
            _head(repo),
            ".findings-state.json",
        )

    def test_refused_at_another_head(self, built_repo):
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config)
        (repo / "more.py").write_text("x = 1\n")
        _git(repo, "add", "more.py")
        _git(repo, "commit", "-qm", "one more")
        assert code.recorded_pass(change, repo, config) is None

    @pytest.mark.parametrize("where", ["tracked", "untracked", "round_file"])
    def test_refused_with_a_dirty_tree(self, built_repo, where):
        """`round_file`: the round's findings file, written but not committed,
        is enough to refuse, which is why `do_code` asks before writing it."""
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config)
        if where == "tracked":
            (repo / "calc.py").write_text("def add(a, b):\n    return 0\n")
        elif where == "untracked":
            (repo / "new.py").write_text("y = 2\n")
        else:
            (change / "findings-r1.md").write_text("# Findings, round 1\n")
        assert code.recorded_pass(change, repo, config) is None

    def test_refused_for_another_command(self, built_repo):
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config, command="pytest -q")
        assert code.recorded_pass(change, repo, config) is None

    def test_refused_for_another_parallel(self, built_repo):
        repo, change, config = built_repo
        other = "off" if config.parallel_tests == "auto" else "auto"
        _record_pass(change / SOURCE, repo, config, parallel=other)
        assert code.recorded_pass(change, repo, config) is None

    def test_refused_for_a_different_content_hash(self, built_repo):
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config, content="0" * 64)
        assert code.recorded_pass(change, repo, config) is None

    def test_refused_when_not_passed(self, built_repo):
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config, passed=False)
        assert code.recorded_pass(change, repo, config) is None

    @pytest.mark.parametrize("bad", ["not json", "a directory", "a list"])
    def test_refused_for_an_unreadable_file(self, built_repo, bad):
        repo, change, config = built_repo
        _record_pass(change / SOURCE, repo, config)
        path = change / ".findings-state.json"
        if bad == "not json":
            path.write_text("{not json")
        elif bad == "a directory":
            path.mkdir()
        else:
            path.write_text("[1, 2]")
        assert code.recorded_pass(change, repo, config) is None

    def test_refused_with_no_record(self, built_repo):
        repo, change, config = built_repo
        assert code.recorded_pass(change, repo, config) is None


# --- do_code with a match ----------------------------------------------------------


class TestDoCode:
    def test_a_real_round_saves_the_match_and_skips_the_baseline(
        self, tmp_path, monkeypatch
    ):
        """The round as the loop runs it."""
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        r = _ReuseRound(tmp_path, monkeypatch)
        assert r.run() == 0, r.lines
        # Asked on a clean tree: no round file written yet.
        assert [c for c in calls(r.log) if c[0] == "recorded_pass"] == [
            ("recorded_pass", "", "False")
        ]
        assert _round_state(r.change).baseline_reuse is not None
        assert r.engines() == [SKIPPED]
        assert r.count("baseline") == 0

    def test_the_match_is_saved_before_the_round_files_exist(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        r = _ReuseRound(tmp_path, monkeypatch)
        assert r.run() == 0, r.lines
        # Asked once, at the start of triage, before any round file exists.
        assert [c for c in calls(r.log) if c[0] == "recorded_pass"] == [
            ("recorded_pass", "", "False")
        ]
        assert (r.change / "findings-r1.md").exists()
        assert (r.change / code.tasks_file_name(1)).exists()
        assert _round_state(r.change).baseline_reuse == {
            "head": r.recorded,
            "source": SOURCE,
            "command": r.config.test_command,
            "parallel": r.config.parallel_tests,
        }

    def test_the_engine_gets_baseline_reuse_and_runs_no_baseline(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        r = _ReuseRound(tmp_path, monkeypatch)
        assert r.run() == 0, r.lines
        assert r.engines() == [SKIPPED]
        assert r.count("baseline") == 0
        assert _engine_state(r.change).tdd.baseline_tests_pass
        progress = _progress(r.repo)
        assert progress.count("BASELINE SKIPPED") == 1
        assert (
            f"BASELINE SKIPPED — the whole test command passed at "
            f"{r.recorded[:8]} ({SOURCE})"
        ) in progress

    def test_an_extra_committed_file_between_h_and_head_runs_the_baseline(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        r = _ReuseRound(tmp_path, monkeypatch)
        real = code.commit_round_files

        def commit_and_one_more(project_dir, n, paths):
            sha = real(project_dir, n, paths)
            (r.repo / "extra.py").write_text("z = 3\n")
            _git(r.repo, "add", "extra.py")
            _git(r.repo, "commit", "-qm", "someone else's file")
            return sha

        assert (
            r.run(
                patches=[patch.object(code, "commit_round_files", commit_and_one_more)]
            )
            == 0
        ), r.lines
        assert _round_state(r.change).baseline_reuse is not None
        assert r.engines() == [("none",)]
        assert r.count("baseline") == 1
        assert "BASELINE SKIPPED" not in _progress(r.repo)

    def test_no_record_runs_the_baseline(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        r = _ReuseRound(tmp_path, monkeypatch, record=False)
        assert r.run() == 0, r.lines
        assert _round_state(r.change).baseline_reuse is None
        assert r.engines() == [("none",)]
        assert r.count("baseline") == 1


# --- the engine ---------------------------------------------------------------------


def _failing_writer():
    """Stop the engine right after its baseline section."""
    return patch(
        "build_pipeline.tdd_engine.run_test_writer",
        new_callable=AsyncMock,
        return_value=AgentResult(success=False, error="stop here"),
    )


def _gate_spy():
    return patch(
        "build_pipeline.tdd_engine.baseline_test_gate",
        side_effect=tdd_engine.baseline_test_gate,
    )


class TestEngine:
    @pytest.fixture(autouse=True)
    def _trusted(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")

    @pytest.mark.asyncio
    async def test_the_baseline_is_skipped_with_a_matching_head(self, engine_setup):
        config, args = engine_setup
        head = git_ops.rev_parse_head(config.project_dir)
        recorded = "ab" * 20
        with _failing_writer(), _gate_spy() as gate:
            await tdd_engine.run_tdd_engine(
                config,
                args,
                standalone=False,
                baseline_reuse=(head, recorded, SOURCE),
            )
        gate.assert_not_called()
        tdd = BuildState.load(config.state_file_path()).tdd
        assert tdd.baseline_tests_pass and tdd.baseline_failure == ""
        assert (
            f"BASELINE SKIPPED — the whole test command passed at abababab ({SOURCE})"
            in config.progress_file_path().read_text()
        )

    @pytest.mark.asyncio
    async def test_no_skip_with_a_dirty_tree(self, engine_setup):
        config, args = engine_setup
        head = git_ops.rev_parse_head(config.project_dir)
        (config.project_dir / "stray.py").write_text("s = 1\n")
        with _failing_writer(), _gate_spy() as gate:
            await tdd_engine.run_tdd_engine(
                config, args, standalone=False, baseline_reuse=(head, head, SOURCE)
            )
        gate.assert_called_once()
        assert "BASELINE SKIPPED" not in config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_no_skip_when_head_is_h_but_not_the_checked_head(self, engine_setup):
        config, args = engine_setup
        head = git_ops.rev_parse_head(config.project_dir)
        with _failing_writer(), _gate_spy() as gate:
            await tdd_engine.run_tdd_engine(
                config,
                args,
                standalone=False,
                baseline_reuse=("cd" * 20, head, SOURCE),
            )
        gate.assert_called_once()
        assert "BASELINE SKIPPED" not in config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_no_reuse_runs_the_baseline(self, engine_setup):
        config, args = engine_setup
        with _failing_writer(), _gate_spy() as gate:
            await tdd_engine.run_tdd_engine(config, args, standalone=False)
        gate.assert_called_once()

    @pytest.mark.asyncio
    async def test_baseline_failure_is_saved_on_a_red_baseline(self, engine_setup):
        config, args = engine_setup
        config.test_command = "false"
        code_ = await tdd_engine.run_tdd_engine(config, args, standalone=False)
        assert code_ == tdd_engine.EXIT_BUILD_FAILURE
        tdd = BuildState.load(config.state_file_path()).tdd
        assert not tdd.baseline_tests_pass
        assert tdd.baseline_failure == "Baseline tests failing (exit 1)"

    @pytest.mark.asyncio
    async def test_baseline_failure_is_saved_on_a_timeout(self, engine_setup):
        config, args = engine_setup
        why = "Test command failed to run: timed out after 1800 seconds"
        out = f"{why} (partial output: x.out)\n" + "line\n" * 50
        with patch("build_pipeline.gates._suite_run", return_value=("true", None, out)):
            await tdd_engine.run_tdd_engine(config, args, standalone=False)
        tdd = BuildState.load(config.state_file_path()).tdd
        assert tdd.baseline_failure == f"{why} (partial output: x.out)"

    @pytest.mark.asyncio
    async def test_baseline_failure_is_saved_when_the_runner_fails(self, engine_setup):
        config, args = engine_setup
        with patch(
            "build_pipeline.tdd_engine.baseline_test_gate",
            side_effect=RunnerFailed("pytest -q", [], "boom\nmore"),
        ):
            await tdd_engine.run_tdd_engine(config, args, standalone=False)
        tdd = BuildState.load(config.state_file_path()).tdd
        assert tdd.baseline_failure == (
            "the test runner failed 3 times with no test results (pytest -q); "
            "last output:"
        )

    @pytest.mark.asyncio
    async def test_baseline_failure_is_one_line_of_at_most_300(self, engine_setup):
        config, args = engine_setup
        with patch(
            "build_pipeline.tdd_engine.baseline_test_gate",
            return_value=GateResult(passed=False, reason="\n" + "r" * 400 + "\nmore"),
        ):
            await tdd_engine.run_tdd_engine(config, args, standalone=False)
        assert BuildState.load(config.state_file_path()).tdd.baseline_failure == (
            "r" * 300
        )


class TestParkReason:
    def test_a_red_baseline_s_reason_is_in_the_step_failure(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        r = _ReuseRound(tmp_path, monkeypatch, record=False)
        r.red = "Baseline tests failing (exit 1)"
        assert r.run() == 1, r.lines
        assert r.lines[-1].startswith("FINDINGS:FAILED:code:"), r.lines
        assert (
            "the PR's test suite fails before any fix (the TDD engine's baseline "
            "gate: Baseline tests failing (exit 1)), so no fix can be tested; "
            "make the suite pass, then re-run"
        ) in r.lines[-1]


# --- rows S1-S8 ---------------------------------------------------------------------


class TestRows:
    @pytest.fixture(autouse=True)
    def _trusted(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")

    @pytest.mark.parametrize("files", ["not_written", "written_uncommitted"])
    def test_s1_killed_after_the_match_is_saved_before_the_records_commit(
        self, tmp_path, monkeypatch, files
    ):
        clean = _clean(tmp_path, monkeypatch)
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        if files == "not_written":
            # do_code's first write, right after the match is saved (the
            # triage step's earlier write of the findings file goes through).
            real = loop._write_findings_file

            def write(run, fs, rnd):
                if rnd.baseline_reuse is not None:
                    os._exit(137)
                return real(run, fs, rnd)

            stop = patch.object(loop, "_write_findings_file", write)
        else:
            stop = _kill_at("commit_round_files")
        _killed(lambda: s.run(patches=[stop]))
        assert _round_state(s.change).baseline_reuse["head"] == s.recorded
        assert _head(s.repo) == s.recorded  # no records commit yet
        tasks = s.change / code.tasks_file_name(1)
        assert tasks.exists() == (files != "not_written")
        assert s.run() == 0, s.lines
        # recorded_pass is not asked again: one call in all, as unstopped.
        assert s.count("recorded_pass") == 1
        assert s.engines() == [SKIPPED]
        assert s.count("baseline") == 0
        _like_unstopped(s, clean)

    def test_s2_killed_after_the_records_commit_before_the_engine_checkpoint(
        self, tmp_path, monkeypatch
    ):
        clean = _clean(tmp_path, monkeypatch)
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        _killed(lambda: s.run(patches=[_kill_before_engine()]))
        assert not (s.change / code.engine_state_name(1)).exists()
        records_commit = _head(s.repo)
        assert records_commit != s.recorded
        assert s.run() == 0, s.lines
        assert s.engines() == [SKIPPED]
        assert s.count("baseline") == 0
        # No second records commit: the same history as the unstopped run.
        _like_unstopped(s, clean)

    def test_s3_killed_after_the_skip_is_checkpointed(self, tmp_path, monkeypatch):
        clean = _clean(tmp_path, monkeypatch)
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        _killed(
            lambda: s.run(
                patches=[
                    _kill_after_engine_checkpoint(lambda tdd: tdd.baseline_tests_pass)
                ]
            )
        )
        assert _engine_state(s.change).tdd.baseline_tests_pass
        assert _progress(s.repo).count("BASELINE SKIPPED") == 0  # logged after
        assert s.run() == 0, s.lines
        assert s.count("baseline") == 0
        # The resumed engine starts at the blocks: no second skip is logged.
        # (The first, killed, engine run is logged too.)
        assert s.engines() == [SKIPPED, SKIPPED]
        assert _progress(s.repo).count("BASELINE SKIPPED") == 0
        assert "Running existing test suite" not in _progress(s.repo)
        _like_unstopped(s, clean, extra_calls=[("engine", *SKIPPED)])

    def test_s4_restart_with_no_saved_match_and_head_past_h(
        self, tmp_path, monkeypatch
    ):
        clean = _clean(tmp_path, monkeypatch)
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        _killed(lambda: s.run(patches=[_kill_before_engine()]))
        _set_round_field(s.change, baseline_reuse=None)
        assert _head(s.repo) != s.recorded
        # No record at HEAD any more: the safe fallback.
        assert code.recorded_pass(s.change, s.repo, s.config) is None
        assert s.run() == 0, s.lines
        assert s.engines() == [("none",)]
        assert s.count("baseline") == 1
        assert "BASELINE SKIPPED" not in _progress(s.repo)
        assert_same_as_unstopped(s.repo, clean.repo, REUSE_BOOKKEEPING)

    @pytest.mark.parametrize("how", ["committed", "edited"])
    def test_s5_the_user_changed_files_between_stop_and_resume(
        self, tmp_path, monkeypatch, how
    ):
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        _killed(lambda: s.run(patches=[_kill_before_engine()]))
        if how == "committed":
            (s.repo / "notes.py").write_text("n = 1\n")
            _git(s.repo, "add", "notes.py")
            _git(s.repo, "commit", "-qm", "the user's commit")
            user = _head(s.repo)
            assert s.run() == 0, s.lines
            assert s.engines() == [("none",)]
            assert s.count("baseline") == 1
            # Nothing discarded: the user's commit is still in the branch.
            assert user in _git(s.repo, "rev-list", "HEAD").split()
            assert (s.repo / "notes.py").read_text() == "n = 1\n"
        else:
            (s.repo / "calc.py").write_text(
                "def add(a, b):\n    return a + b  # the user's edit\n"
            )
            before = repo_state(s.repo)
            # Today's behaviour for a dirty tree at a resumed code step is
            # a refusal (row D1.S10); either way no baseline is skipped and
            # nothing is discarded.
            assert s.run() == 1, s.lines
            assert "stash them first" in s.lines[-1], s.lines
            assert repo_state(s.repo) == before
            assert s.engines() == []
            assert s.count("baseline") == 0

    def test_s6_origin_moved_head_to_a_new_commit(self, tmp_path, monkeypatch):
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        _killed(lambda: s.run(patches=[_kill_before_engine()]))
        _origin_with_one_more_commit(s.repo, tmp_path / "origin")
        moved = _head(s.repo)
        assert s.run() == 0, s.lines
        assert s.engines() == [("none",)]
        assert s.count("baseline") == 1
        assert moved in _git(s.repo, "rev-list", "HEAD").split()
        assert "BASELINE SKIPPED" not in _progress(s.repo)

    @pytest.mark.parametrize("what", ["test_command", "parallel"])
    def test_s7_the_command_or_parallel_changed_between_stop_and_resume(
        self, tmp_path, monkeypatch, what
    ):
        clean = _clean(tmp_path, monkeypatch, record=False)
        s = _ReuseRound(tmp_path / "stopped", monkeypatch)
        _killed(lambda: s.run(patches=[_kill_before_engine()]))
        saved = _round_state(s.change).baseline_reuse
        for r in (s, clean):
            if what == "test_command":
                r.config.test_command = "true && true"
            else:
                r.config.parallel_tests = (
                    "off" if saved["parallel"] == "auto" else "auto"
                )
        assert s.run() == 0, s.lines
        assert s.engines() == [("none",)]
        assert s.count("baseline") == 1
        assert "BASELINE SKIPPED" not in _progress(s.repo)
        assert_same_as_unstopped(s.repo, clean.repo, REUSE_BOOKKEEPING)

    def test_s8_killed_after_baseline_failure_is_saved_before_step_failed(
        self, tmp_path, monkeypatch
    ):
        first = "Baseline tests failing (exit 1)"
        second = "Baseline tests failing (exit 2)"
        clean = _ReuseRound(tmp_path / "clean", monkeypatch, record=False)
        clean.red = second
        assert clean.run() == 1, clean.lines
        s = _ReuseRound(tmp_path / "stopped", monkeypatch, record=False)
        s.red = first
        _killed(
            lambda: s.run(
                patches=[
                    _kill_after_engine_checkpoint(lambda tdd: tdd.baseline_failure)
                ]
            )
        )
        assert _engine_state(s.change).tdd.baseline_failure == first
        s.red = second
        assert s.run() == 1, s.lines
        # The baseline ran again and its reason replaced the saved one.
        assert s.count("baseline") == 2
        assert _engine_state(s.change).tdd.baseline_failure == second
        assert s.lines[-1] == clean.lines[-1]
        assert f"baseline gate: {second})" in s.lines[-1]
        assert first not in s.lines[-1]
        assert_same_as_unstopped(s.repo, clean.repo, RECORD)
        # One more engine run and baseline than unstopped: the killed one's.
        assert Counter(calls(s.log)) == Counter(calls(clean.log)) + Counter(
            [("baseline", ""), ("engine", "none")]
        )
