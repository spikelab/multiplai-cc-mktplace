"""Row tests for PR D1 of plan-buildme-epic-mode-2026-10-12.md ("Stop and
resume states", table "PR D1"): one case per row marked yes.

Each case runs the same findings round twice, in two temporary roots: once
never stopped, once stopped at the row's point (a SIGKILL, as a forked child
exiting 137) and resumed. It then compares the two repos
(`row_helpers.assert_same_as_unstopped`), the launches (the check agent, the
engine's agent functions, the triage seam) and the writing calls (pushes,
replies, body edits, posts), allowing exactly the differences the row names.
A refusal row instead checks the exit, the message and that the repo is
unchanged (`row_helpers.repo_state`).
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from build_pipeline.findings import code, loop
from build_pipeline.state import BuildState
from tests.row_helpers import assert_same_as_unstopped, repo_state
from tests.test_findings_round import (
    FIX,
    FakeReview,
    FakeTriage,
    _install_pr,
    _make_built,
    _run,
    _Stubs,
)
from tests.test_findings_self_check import PROBLEM, FakeCheck, _log, block_stubs, calls

FIX_BLOCK = "Fix finding ab12"
CHECK_BLOCK = "Fix what the check of this round's fixes found"


def _die() -> None:
    os._exit(137)


def _killed(fn) -> None:
    """Run fn() in a forked child; the seam it patches exits 137, as SIGKILL would."""
    pid = os.fork()
    if pid == 0:
        try:
            fn()
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 137, "the child never reached the stop"


class _Round:
    """One findings round on a fresh fixture repo under *base*: one code-fix
    thread, a check agent answering *report*, and every launch logged to a
    file outside the repo (a killed child's calls must survive it)."""

    def __init__(self, base: Path, monkeypatch, report: str):
        self.repo, self.change, self.config = _make_built(base)
        self.log = base / "calls.log"
        self.report = report
        self.pr = _install_pr(monkeypatch)
        self.pr.add_thread("alice", "add subtracts", fid="ab12")
        self.review = FakeReview(self.pr)
        self.lines: list[str] = []

    def run(self, *, stop_at=None, check_stop=None, patches=()) -> int:
        triage = FakeTriage({"add subtracts": FIX})

        async def triage_call(prompt, schema, **kw):
            _log(self.log, "triage", "")
            return await triage(prompt, schema, **kw)

        run, lines = _run(self.config, self.change, triage_call=triage_call, review=self.review,
                          self_check=FakeCheck([self.report], log=self.log, stop=check_stop))
        stubs = block_stubs(self.repo, self.log, stop_at=stop_at, stop=_die if stop_at else None)
        with _Stubs([*stubs, *patches]):
            exit_code = asyncio.run(loop.run_findings(run))
        self.lines = lines
        return exit_code

    def writes(self) -> list[tuple]:
        """The writing calls, without the shas and bodies that name commits."""
        return [e[:2] for e in self.pr.events] + [("post", p) for p in self.review.posts]


def _unstopped(tmp_path: Path, monkeypatch, report: str) -> _Round:
    clean = _Round(tmp_path / "clean", monkeypatch, report)
    assert clean.run() == 0, clean.lines
    return clean


def _assert_like_unstopped(stopped: _Round, clean: _Round, extra_calls=()) -> None:
    assert stopped.lines[-1] == clean.lines[-1], stopped.lines
    assert_same_as_unstopped(stopped.repo, clean.repo)
    assert Counter(calls(stopped.log)) == Counter(calls(clean.log)) + Counter(extra_calls)
    assert stopped.writes() == clean.writes()


def _checks(r: _Round) -> int:
    return sum(1 for c in calls(r.log) if c[0] == "check")


def _wip_lines(r: _Round) -> list[str]:
    return [ln for ln in r.lines if ln.startswith("FINDINGS:WIP:")]


def _kill_after_saving_the_check():
    """Exit 137 right after the checkpoint that first holds `self_check`."""
    real = BuildState.checkpoint

    def checkpoint(self, *a, **kw):
        real(self, *a, **kw)
        rnd = self.findings.rounds[-1] if self.findings and self.findings.rounds else None
        if rnd is not None and rnd.self_check is not None and "code" not in rnd.finished:
            os._exit(137)

    return patch.object(BuildState, "checkpoint", checkpoint)


def _kill_at(name: str, when=lambda *a, **k: True):
    """Exit 137 at the first call of `code.<name>` for which *when* is true."""
    real = getattr(code, name)

    def seam(*a, **kw):
        if when(*a, **kw):
            os._exit(137)
        return real(*a, **kw)

    return patch.object(code, name, seam)


class TestD1Rows:
    def test_row_D1_S2_stopped_during_the_check_agent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        clean = _unstopped(tmp_path, monkeypatch, PROBLEM)
        s = _Round(tmp_path / "stopped", monkeypatch, PROBLEM)
        _killed(lambda: s.run(check_stop=_die))
        assert s.run() == 0, s.lines
        assert not _wip_lines(s)
        _assert_like_unstopped(s, clean, extra_calls=[("check", "")])

    def test_row_D1_S3_check_saved_records_not_committed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        clean = _unstopped(tmp_path, monkeypatch, PROBLEM)
        s = _Round(tmp_path / "stopped", monkeypatch, PROBLEM)
        report = code.self_check_file_name(1)
        _killed(lambda: s.run(patches=[_kill_at(
            "commit_round_files", lambda d, n, paths: any(Path(p).name == report for p in paths))]))
        assert (s.change / report).exists()            # written, not committed
        assert _checks(s) == 1
        assert s.run() == 0, s.lines
        assert [ln.split(":")[2] for ln in _wip_lines(s)] == ["refs/buildme/wip/calc/r1-code-stopped"]
        _assert_like_unstopped(s, clean)

    def test_row_D1_S4_records_committed_block_not_added(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        clean = _unstopped(tmp_path, monkeypatch, PROBLEM)
        s = _Round(tmp_path / "stopped", monkeypatch, PROBLEM)
        _killed(lambda: s.run(patches=[_kill_at("add_block")]))
        assert [b.number for b in BuildState.load(s.change / code.engine_state_name(1)).tdd.blocks] == [1]
        assert s.run() == 0, s.lines
        assert not _wip_lines(s)
        _assert_like_unstopped(s, clean)

    def test_row_D1_S5_stopped_during_block_k(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        clean = _unstopped(tmp_path, monkeypatch, PROBLEM)
        s = _Round(tmp_path / "stopped", monkeypatch, PROBLEM)
        _killed(lambda: s.run(stop_at=("implementer", CHECK_BLOCK)))
        assert s.run() == 0, s.lines
        assert [ln.split(":")[2] for ln in _wip_lines(s)] == [
            "refs/buildme/wip/calc/r1-code-block2-stopped"]
        assert _checks(s) == 1
        _assert_like_unstopped(s, clean, extra_calls=[("test_writer", CHECK_BLOCK),
                                                      ("implementer", CHECK_BLOCK)])

    def test_row_D1_S6_check_found_nothing_saved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        clean = _unstopped(tmp_path, monkeypatch, "")
        s = _Round(tmp_path / "stopped", monkeypatch, "")
        _killed(lambda: s.run(patches=[_kill_after_saving_the_check()]))
        assert s.run() == 0, s.lines
        assert not _wip_lines(s)
        _assert_like_unstopped(s, clean)

    def test_row_D1_S10_user_edit_outside_the_change_dir_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        s = _Round(tmp_path / "stopped", monkeypatch, PROBLEM)
        _killed(lambda: s.run(check_stop=_die))
        (s.repo / "calc.py").write_text("def add(a, b):\n    return a + b  # the user's edit\n")
        before = repo_state(s.repo)
        assert s.run() == 1, s.lines
        assert s.lines[-1].startswith("FINDINGS:FAILED:code:code step: "), s.lines
        assert "calc.py" in s.lines[-1] and "stash them first" in s.lines[-1]
        assert repo_state(s.repo) == before
        assert _checks(s) == 1

    @pytest.mark.parametrize("where", ["check", "block_k"])
    def test_row_D1_S12_the_same_step_stopped_twice(self, tmp_path, monkeypatch, where):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        clean = _unstopped(tmp_path, monkeypatch, PROBLEM)
        s = _Round(tmp_path / "stopped", monkeypatch, PROBLEM)
        for _ in range(2):
            if where == "check":
                _killed(lambda: s.run(check_stop=_die))
            else:
                _killed(lambda: s.run(stop_at=("implementer", CHECK_BLOCK)))
        assert s.run() == 0, s.lines
        if where == "check":
            _assert_like_unstopped(s, clean, extra_calls=[("check", "")] * 2)
        else:
            assert _checks(s) == 1                     # never again once saved
            _assert_like_unstopped(s, clean, extra_calls=[("test_writer", CHECK_BLOCK),
                                                          ("implementer", CHECK_BLOCK)] * 2)
