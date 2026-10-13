"""PR D1 of plan-buildme-epic-mode-2026-10-12.md: the fixer looks for the
same mistake (item 26), the round checks its own commits once (item 27),
`--auto` chooses what gets posted outside the worktree (item 28), and
`findings --max-rounds` (item 29).

Git is real; every model call and every GitHub call is a fake.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline import git_ops, stop_record
from build_pipeline.__main__ import build_parser
from build_pipeline.findings import cli, code, decisions, loop, publish
from build_pipeline.findings.models import PRComment, Triage, TriageRecord, Verdict
from build_pipeline.models import AgentResult, BlockInfo, BlockStatus, ReviewResult, ReviewScore
from build_pipeline.models import TestQualityAudit as QualityAudit  # Test* name breaks collection
from build_pipeline.state import BuildState, FindingsRound, TDDState
from tests.test_findings_round import (  # noqa: F401  (fixtures)
    FIX,
    PR,
    REPO,
    FakePR,
    FakeReview,
    FakeTriage,
    _git,
    _make_built,
    _run,
    _Stubs,
    _subjects,
    built,
    fake_pr,
)

PROBLEM = "calc.py:2 sub() has the same sign mistake as add()"


def _log(path: Path | None, *entry: str) -> None:
    if path is not None:
        with path.open("a") as f:
            f.write(json.dumps(list(entry)) + "\n")


def calls(path: Path) -> list[tuple[str, ...]]:
    if not path.exists():
        return []
    return [tuple(json.loads(line)) for line in path.read_text().splitlines()]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def block_stubs(repo: Path, log: Path | None = None, *, searched: str = "calc.py add and sub; none",
                stop_at: tuple[str, str] | None = None, stop=None):
    """The TDD engine's agents and suite runs, stubbed for any number of
    blocks: each writes from its block's name only, so a block that runs again
    after a discard writes the same bytes. Every agent call is logged as
    (kind, block name). With *stop_at* = (kind, block name), *stop()* runs at
    that call, after it is logged."""

    def hit(kind: str, name: str) -> None:
        _log(log, kind, name)
        if stop is not None and stop_at == (kind, name):
            stop()

    async def test_writer(*_a, **kw):
        name = kw.get("block_name", "")
        hit("test_writer", name)
        p = repo / "tests" / f"test_{_slug(name)}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"from calc import add\n\ndef test_{_slug(name)}():\n    assert add(2, 2) == 4\n")
        return AgentResult(success=True, output="tests written")

    async def implementer(*_a, **kw):
        name = kw.get("block_name", "")
        hit("implementer", name)
        calc = repo / "calc.py"
        if name.startswith("Fix finding"):
            calc.write_text("def add(a, b):\n    return a + b\n")
        else:
            text = calc.read_text()
            line = f"# {name}\n"
            if line not in text:
                calc.write_text(text + line)
        return AgentResult(success=True, output=f"done\n\nSEARCHED: {searched}")

    async def refactorer(*_a, **kw):
        hit("refactorer", kw.get("block_name", ""))
        return AgentResult(success=True, output="nothing to refactor")

    async def review(block, config):
        hit("review", block.name)
        return ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")])

    red = (1, "FAILED tests/test_calc.py::test_add - AssertionError")
    return [
        patch("build_pipeline.tdd_engine.run_test_writer", side_effect=test_writer),
        patch("build_pipeline.tdd_engine.run_implementer", side_effect=implementer),
        patch("build_pipeline.tdd_engine.run_refactorer", side_effect=refactorer),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=red),
        patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
              return_value=QualityAudit(passed=True)),
        patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review),
        patch("build_pipeline.tdd_engine._adjudicate_review_findings", new_callable=AsyncMock,
              side_effect=lambda review, *a, **k: review),
        patch("build_pipeline.tdd_engine._run_refactor_all", new_callable=AsyncMock,
              return_value=True),
    ]


class FakeCheck:
    """The self-check agent: answers *reports* in turn ("" = no problem),
    logging each call and keeping the diff it was given."""

    def __init__(self, reports: list[str], log: Path | None = None, stop=None,
                 diffs_log: Path | None = None):
        self.reports = list(reports)
        self.diffs: list[str] = []
        self.log = log
        self.stop = stop
        self.diffs_log = diffs_log      # each diff, for a run killed in a forked child

    async def __call__(self, config, diff):
        self.diffs.append(diff)
        _log(self.log, "check", "")
        _log(self.diffs_log, diff)
        if self.stop is not None:
            self.stop()
        return self.reports.pop(0) if self.reports else ""


def _state(change: Path) -> BuildState:
    return BuildState.load(change / ".findings-state.json")


def _engine_blocks(change: Path, n: int = 1) -> list[BlockInfo]:
    return BuildState.load(change / code.engine_state_name(n)).tdd.blocks


# --- item 26: the same mistake --------------------------------------------------


def _fix_record(cid=101, finding_id="ab12"):
    return TriageRecord(
        round=1,
        comment=PRComment(id=cid, kind="review", author="alice", allowed=True,
                          finding_id=finding_id, path="calc.py", line=2, reply_to=cid),
        triage=Triage(verdict=Verdict.CODE_FIX, reason="add subtracts",
                      locations=["calc.py:2"], failing_test="add(2, 2) returns 4"),
    )


class TestSameMistake:
    def test_the_brief_asks_for_the_same_mistake_a_searched_section_and_nothing_else(self, tmp_path):
        text, _ = code.render_tasks(1, [_fix_record()])
        path = tmp_path / "t.md"
        path.write_text(text)
        from build_pipeline.tdd_engine import parse_blocks
        task = parse_blocks(path)[0].description
        for needle in ("same function", "its other branches", "its callers",
                       "the rest of the PR's diff", "same mistake", "fix each"):
            assert needle in task, needle
        assert "SEARCHED:" in task
        assert "change nothing else" in task.lower()

    def test_the_fixer_s_last_searched_section_is_read(self):
        report = ("did it\n\nSEARCHED: calc.py only\n\nthen more\n\n"
                  "SEARCHED: calc.py add() and sub(), and main.py; sub() fixed too")
        assert code.searched_text(report) == "calc.py add() and sub(), and main.py; sub() fixed too"

    @pytest.mark.parametrize("marker", ["[review-fix 1]", "[fix on pass]"])
    def test_a_later_agent_s_searched_section_is_not_the_fixer_s(self, marker):
        """A review-fix or fix-on-pass agent also ends with `SEARCHED:`, about
        the block reviewer's findings; the reply quotes the fixer's own."""
        report = ("Implemented the fix.\nSEARCHED:\n- finding ab12: add() and its callers in "
                  f"calc.py; also fixed sub()\n\n{marker}\nRenamed a variable.\nSEARCHED:\n"
                  "- naming nit: the rest of calc.py, nothing else\n")
        assert code.searched_text(report) == \
            "finding ab12: add() and its callers in calc.py; also fixed sub()"

    def test_a_trimmed_report_that_lost_the_fixer_s_part_gives_empty(self, caplog):
        """The engine keeps only the report's last `MAX_EVIDENCE_CHARS`: when
        that starts inside a later agent's output, the fixer's section is gone."""
        from build_pipeline.tdd_engine import MAX_EVIDENCE_CHARS
        tail = "\n\n[review-fix 2]\nmore\nSEARCHED: the reviewer's finding\n"
        head = "x" * MAX_EVIDENCE_CHARS + "\nSEARCHED: review-fix 1's search"
        report = (head + tail)[-MAX_EVIDENCE_CHARS:]
        with caplog.at_level(logging.WARNING):
            assert code.searched_text(report, "review-fix 1") == ""

    def test_a_trimmed_report_with_no_marker_left_gives_empty(self, caplog):
        """A later agent's output longer than `MAX_EVIDENCE_CHARS` cuts away
        its own `[review-fix n]` marker; a trimmed report with no marker cannot
        be shown to be the fixer's, so its search is not quoted."""
        from build_pipeline.tdd_engine import MAX_EVIDENCE_CHARS
        report = ("did it\nSEARCHED: calc.py\n\n[review-fix 1]\n" + "x" * MAX_EVIDENCE_CHARS
                  + "\nSEARCHED:\n- naming nit: rest of calc.py\n")[-MAX_EVIDENCE_CHARS:]
        assert "[review-fix" not in report
        with caplog.at_level(logging.WARNING):
            assert code.searched_text(report, "review-fix 1") == ""

    def test_a_trimmed_report_s_first_marker_is_the_first_later_agent_s(self):
        """With no review fix, `[fix on pass]` is the first later part, so
        the text before it is the fixer's."""
        from build_pipeline.tdd_engine import MAX_EVIDENCE_CHARS
        report = ("x" * MAX_EVIDENCE_CHARS + "\nSEARCHED: calc.py\n\n[fix on pass]\n"
                  "renamed\nSEARCHED: naming nit\n")[-MAX_EVIDENCE_CHARS:]
        assert code.searched_text(report, "fix on pass") == "calc.py"

    def test_an_untrimmed_report_with_no_later_agent_is_the_fixer_s(self):
        from build_pipeline.tdd_engine import MAX_EVIDENCE_CHARS
        report = "x" * (MAX_EVIDENCE_CHARS - 100) + "\nSEARCHED: calc.py"
        assert len(report) < MAX_EVIDENCE_CHARS
        assert code.searched_text(report) == "calc.py"

    def test_a_searched_section_stops_at_a_later_review_fix(self):
        report = "did it\nSEARCHED: calc.py\n\n[review-fix 1]\nfixed the review"
        assert code.searched_text(report) == "calc.py"

    def test_a_report_without_one_gives_empty_and_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert code.searched_text("done, nothing else to say") == ""
        assert any("SEARCHED" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("long, review_iterations, fix_on_pass_kept, searched", [
        (False, 1, False, "calc.py add and sub; none"),
        (True, 1, False, "calc.py add and sub; none"),
        (True, 2, False, ""), (True, 1, True, "")])
    def test_fixes_from_state_carries_searched(self, tmp_path, long, review_iterations,
                                               fix_on_pass_kept, searched):
        """A long report with no marker is the fixer's unless the block had a
        review fix or a kept fix on pass, whose long output cuts its marker."""
        state = BuildState(change_name="calc", mode="only", tier="advanced",
                           state_file=str(tmp_path / "s.json"))
        state.tdd = TDDState(blocks=[BlockInfo(
            number=1, name="Fix finding ab12", description="d", status=BlockStatus.DONE,
            test_commit="t" * 40, impl_commit="i" * 40, review_iterations=review_iterations,
            fix_on_pass_kept=fix_on_pass_kept,
            implementer_report=("x" * 4000 if long else "ok")[-3970:]
            + "\nSEARCHED: calc.py add and sub; none")])
        state.checkpoint(tmp_path / "s.json")
        fixes = code.fixes_from_state(tmp_path / "s.json", {1: "finding:ab12"})
        assert fixes["finding:ab12"]["searched"] == searched

    def _outcome(self, searched: str) -> str:
        r = _fix_record()
        rnd = FindingsRound(number=1, comments=[r.comment], triaged={r.comment.id: r.triage},
                            fixes={r.comment.key: {"test": "a" * 40, "impl": "b" * 40,
                                                   "status": "done", "searched": searched}})
        return publish.outcome(rnd, publish.records_of(rnd)[0])

    def test_the_reply_says_where_it_searched_after_fixed_in(self):
        text = self._outcome("calc.py add and sub; sub fixed too")
        assert "Searched for the same mistake: calc.py add and sub; sub fixed too" in text
        assert text.index("Fixed in") < text.index("Searched for the same mistake")

    def test_no_searched_sentence_when_empty(self):
        assert "Searched" not in self._outcome("")

    def test_the_pr_body_s_section_leaves_the_searched_sentence_out(self):
        """The body is under the pr skill's word limit and lists every round;
        the search is quoted in the thread's reply only."""
        from build_pipeline.state import FindingsState
        r = _fix_record()
        rnd = FindingsRound(number=1, comments=[r.comment], triaged={r.comment.id: r.triage},
                            fixes={r.comment.key: {"test": "a" * 40, "impl": "b" * 40,
                                                   "status": "done", "searched": "calc.py all"}})
        section = publish.render_section(FindingsState(pr=PR, repo=REPO, branch="buildme/calc",
                                                       change_dir="c", rounds=[rnd]))
        assert "Fixed in" in section and "Searched" not in section
        assert "Searched for the same mistake: calc.py all" in publish.reply_body(
            rnd, publish.records_of(rnd)[0])


# --- item 27: check the round's own commits once ---------------------------------


async def _one_round(built, fake_pr, check, stubs=None, review=None):
    repo, change, config = built
    fake_pr.add_thread("alice", "add subtracts", fid="ab12")
    run, lines = _run(config, change, triage_call=FakeTriage({"add subtracts": FIX}),
                      review=review or FakeReview(fake_pr), self_check=check)
    with _Stubs(stubs if stubs is not None else block_stubs(repo)):
        exit_code = await loop.run_findings(run)
    return exit_code, lines


class TestSelfCheck:
    @pytest.mark.asyncio
    async def test_no_problem_writes_nothing(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([""])
        exit_code, lines = await _one_round(built, fake_pr, check)
        assert exit_code == 0, lines
        assert len(check.diffs) == 1
        assert not (change / code.self_check_file_name(1)).exists()
        rnd = _state(change).findings.rounds[0]
        assert rnd.self_check == {"head": rnd.self_check["head"], "report": "", "block": None}
        assert rnd.self_check["head"] == _git(repo, "rev-parse", "HEAD").strip()
        assert [b.number for b in _engine_blocks(change)] == [1]
        assert "## 2." not in (change / code.tasks_file_name(1)).read_text()
        assert not any("self-check" in s for s in _subjects(repo))

    @pytest.mark.asyncio
    async def test_one_problem_adds_one_block_and_one_commit_of_both_records(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([PROBLEM])
        exit_code, lines = await _one_round(built, fake_pr, check)
        assert exit_code == 0, lines
        assert len(check.diffs) == 1
        report = change / code.self_check_file_name(1)
        tasks = change / code.tasks_file_name(1)
        assert PROBLEM in report.read_text()
        assert "## 2. " in tasks.read_text() and PROBLEM in tasks.read_text()
        # Both records in one commit, made before block 2's commits.
        rel = lambda p: p.relative_to(repo).as_posix()  # noqa: E731
        sha = _git(repo, "log", "--format=%H", "-1", "--", rel(report)).strip()
        assert sorted(_git(repo, "show", "--name-only", "--format=", sha).split()) == \
            sorted([rel(report), rel(tasks)])
        assert _git(repo, "log", "--format=%H", "-1", "--", rel(tasks)).strip() == sha
        assert _git(repo, "merge-base", "--is-ancestor", sha,
                    _git(repo, "log", "--format=%H", "-1", "--grep", "^impl(block-2)").strip()) == ""
        assert [b.number for b in _engine_blocks(change)] == [1, 2]
        assert all(b.status == BlockStatus.DONE for b in _engine_blocks(change))
        rnd = _state(change).findings.rounds[0]
        assert rnd.self_check["block"] == 2 and rnd.self_check["report"] == PROBLEM
        assert rnd.blocks == {1: "finding:ab12", 2: "block:2"}
        assert rnd.fixes["block:2"]["impl"]
        # Nothing left to commit: a re-render matches the committed bytes.
        assert git_ops.tree_is_clean(repo)

    @pytest.mark.asyncio
    async def test_the_check_reads_the_round_s_fix_commits(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([""])
        await _one_round(built, fake_pr, check)
        rnd = _state(change).findings.rounds[0]
        records = _git(repo, "log", "--format=%H", "-1", "--grep",
                       r"^chore(findings-r1)").strip()
        assert rnd.code_base == records
        diff = check.diffs[0]
        assert "+    return a + b" in diff
        assert "findings-r1" not in diff          # the change directory is left out

    @pytest.mark.asyncio
    async def test_the_check_never_runs_twice(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([PROBLEM, PROBLEM])
        await _one_round(built, fake_pr, check)
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=FakeReview(fake_pr),
                          self_check=check)
        with _Stubs(block_stubs(repo)):
            assert await loop.run_findings(run) == 0, lines
        assert len(check.diffs) == 1

    @pytest.mark.asyncio
    async def test_a_failed_records_commit_fails_the_step(self, built, fake_pr, monkeypatch):
        repo, change, config = built
        real = git_ops.commit_paths

        def commit_paths(worktree, message, paths):
            if any(code.self_check_file_name(1) in p for p in paths):
                return None
            return real(worktree, message, paths)

        monkeypatch.setattr(git_ops, "commit_paths", commit_paths)
        exit_code, lines = await _one_round(built, fake_pr, FakeCheck([PROBLEM]))
        assert exit_code == 1
        assert lines[-1] == "FINDINGS:FAILED:code:could not commit the self-check records", lines

    @pytest.mark.asyncio
    async def test_a_failed_check_agent_fails_the_step_and_saves_nothing(self, built, fake_pr):
        repo, change, config = built

        async def fails(config, diff):
            raise RuntimeError("the self-check agent failed: timed out")

        exit_code, lines = await _one_round(built, fake_pr, fails)
        assert exit_code == 1
        assert lines[-1].startswith("FINDINGS:FAILED:code:RuntimeError"), lines
        assert _state(change).findings.rounds[0].self_check is None


    @pytest.mark.asyncio
    async def test_an_empty_diff_is_not_checked(self, built, fake_pr):
        """Every fix block failed and was discarded: there is nothing to check,
        and no agent is paid to invent a problem."""
        repo, change, config = built
        stubs = block_stubs(repo)
        writer = next(p for p in stubs if p.attribute == "run_test_writer")

        async def blocked(*a, **kw):
            return AgentResult(success=True, output="Cannot.\n\nSTATUS: BLOCKED\n")

        writer.kwargs["side_effect"] = blocked
        check = FakeCheck([PROBLEM])
        exit_code, lines = await _one_round(built, fake_pr, check, stubs=stubs)
        assert exit_code == 0, lines
        assert check.diffs == []
        rnd = _state(change).findings.rounds[0]
        assert rnd.self_check["report"] == "" and rnd.self_check["block"] is None
        assert not (change / code.self_check_file_name(1)).exists()

    @pytest.mark.asyncio
    async def test_a_failed_block_k_is_answered_and_the_check_runs_once(self, built, fake_pr):
        repo, change, config = built
        stubs = block_stubs(repo)
        writer = next(p for p in stubs if p.attribute == "run_test_writer")
        real = writer.kwargs["side_effect"]

        async def test_writer(*a, **kw):
            if kw.get("block_name", "").startswith("Fix what the check"):
                return AgentResult(success=True, output="Cannot.\n\nSTATUS: BLOCKED\n")
            return await real(*a, **kw)

        writer.kwargs["side_effect"] = test_writer
        check = FakeCheck([PROBLEM, PROBLEM])
        exit_code, lines = await _one_round(built, fake_pr, check, stubs=stubs)
        assert exit_code == 0, lines
        assert lines[-1] == "FINDINGS:DONE:1", lines
        assert len(check.diffs) == 1
        assert "FINDINGS:BLOCK_FAILED:2:block:2" in lines
        rnd = _state(change).findings.rounds[0]
        assert "could not complete block 2" in rnd.fixes["block:2"]["failed"]
        assert rnd.fixes["finding:ab12"]["impl"]
        assert not [s for s in _subjects(repo) if "block-2" in s]

    @pytest.mark.asyncio
    async def test_git_failing_to_show_the_diff_fails_the_step(self, built, fake_pr, monkeypatch):
        repo, change, config = built
        monkeypatch.setattr(git_ops, "diff_range", lambda *a, **k: None)
        check = FakeCheck([PROBLEM])
        exit_code, lines = await _one_round(built, fake_pr, check)
        assert exit_code == 1
        assert lines[-1] == ("FINDINGS:FAILED:code:git could not show the round's fix commits "
                             "to check them"), lines
        assert check.diffs == []
        assert _state(change).findings.rounds[0].self_check is None

    @pytest.mark.asyncio
    async def test_a_records_commit_with_nothing_to_commit_goes_on(self, built, fake_pr,
                                                                    monkeypatch):
        """`commit_round_files` returns None when the records already match
        HEAD; with both clean the step goes on."""
        repo, change, config = built
        real = git_ops.commit_paths

        def commit_paths(worktree, message, paths):
            sha = real(worktree, message, paths)
            return None if any(code.self_check_file_name(1) in p for p in paths) else sha

        monkeypatch.setattr(git_ops, "commit_paths", commit_paths)
        exit_code, lines = await _one_round(built, fake_pr, FakeCheck([PROBLEM]))
        assert exit_code == 0, lines
        assert [b.number for b in _engine_blocks(change)] == [1, 2]


class TestTheCheckAgent:
    @pytest.mark.asyncio
    async def test_read_only_tools_and_its_own_step(self, built):
        repo, change, config = built
        seen = {}

        async def agent_call(prompt, **kw):
            seen.update(kw, prompt=prompt)
            return AgentResult(success=True, output="NO PROBLEMS")

        with patch("build_pipeline.findings.code.agent_call", side_effect=agent_call):
            assert await code.run_self_check(config, "diff --git a/calc.py b/calc.py") == ""
        assert sorted(seen["allowed_tools"]) == ["Glob", "Grep", "Read"]
        assert seen["budget_label"] == "findings_self_check"
        assert seen["model"] == config.model_for("findings_self_check")
        assert "diff --git a/calc.py" in seen["prompt"]

    @pytest.mark.asyncio
    async def test_a_problem_list_is_returned(self, built):
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output=f"- {PROBLEM}\n")):
            assert await code.run_self_check(config, "d") == f"- {PROBLEM}"

    @pytest.mark.parametrize("answer", [
        "NO PROBLEMS",
        "No problems found.",
        "NO PROBLEMS — the fix is complete.",
        "I opened calc.py and its callers.\n\nNO PROBLEMS",
        "I listed one problem, then checked it again.\n\nPROBLEMS:\n- calc.py:2 x\n\nNO PROBLEMS",
        "Checked every caller.\n\nPROBLEMS: none",
        "",
    ])
    @pytest.mark.asyncio
    async def test_no_problems_in_any_wording_or_after_narration(self, built, answer):
        """The agent's output joins the text of every turn, so narration
        before a tool call precedes the answer: the last marker decides."""
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output=answer)):
            assert await code.run_self_check(config, "d") == ""

    @pytest.mark.asyncio
    async def test_the_problems_section_after_narration_is_the_report(self, built):
        repo, change, config = built
        answer = (f"I opened calc.py. No problems in add() so far.\n\nPROBLEMS:\n- {PROBLEM}\n"
                  "- calc.py:5 mul() too\n")
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output=answer)):
            assert await code.run_self_check(config, "d") == f"- {PROBLEM}\n- calc.py:5 mul() too"

    @pytest.mark.parametrize("answer, report", [
        # A single item that starts with "None" or "Nothing" is a problem.
        ("PROBLEMS:\n- None of the callers of add() in main.py were updated (main.py:12)",
         "- None of the callers of add() in main.py were updated (main.py:12)"),
        ("PROBLEMS:\n- Nothing checks b=0 in div() (calc.py:9)",
         "- Nothing checks b=0 in div() (calc.py:9)"),
        # A list item that starts like a marker is not the answer's marker.
        ("PROBLEMS:\n- calc.py:2 add() still wrong\n- No problems are handled for b=0 in div()",
         "- calc.py:2 add() still wrong\n- No problems are handled for b=0 in div()"),
        ("PROBLEMS:\n- calc.py:2 add() still wrong\n- Problem: mul() overflows",
         "- calc.py:2 add() still wrong\n- Problem: mul() overflows"),
        ("PROBLEMS:\n* calc.py:2 add() still wrong\n* No problems in sub() but div() is wrong",
         "* calc.py:2 add() still wrong\n* No problems in sub() but div() is wrong"),
        ("PROBLEMS:\n1. calc.py:2 add() still wrong\n2. Problems: mul() too",
         "1. calc.py:2 add() still wrong\n2. Problems: mul() too"),
        # Continuation lines of an item are indented.
        ("PROBLEMS:\n- calc.py:2 div() divides by zero;\n  no problems are handled for b=0",
         "- calc.py:2 div() divides by zero;\n  no problems are handled for b=0"),
        # A NO PROBLEMS line followed by listed items is not a clean answer.
        ("NO PROBLEMS in add().\n- calc.py:5 mul() overflows",
         "NO PROBLEMS in add().\n- calc.py:5 mul() overflows"),
    ])
    @pytest.mark.asyncio
    async def test_a_listed_problem_is_never_read_as_none(self, built, answer, report):
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output=answer)):
            assert await code.run_self_check(config, "d") == report

    @pytest.mark.parametrize("answer", [
        "**NO PROBLEMS**",
        "## NO PROBLEMS",
        "PROBLEMS:\n- none",
        "PROBLEMS: Nothing.",
    ])
    @pytest.mark.asyncio
    async def test_a_marked_none_is_none(self, built, answer):
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output=answer)):
            assert await code.run_self_check(config, "d") == ""

    @pytest.mark.asyncio
    async def test_the_brief_asks_for_a_marked_last_answer(self, built):
        repo, change, config = built
        seen = {}

        async def agent_call(prompt, **kw):
            seen["prompt"] = prompt
            return AgentResult(success=True, output="NO PROBLEMS")

        with patch("build_pipeline.findings.code.agent_call", side_effect=agent_call):
            await code.run_self_check(config, "d")
        assert "`PROBLEMS:`" in seen["prompt"] and "`NO PROBLEMS`" in seen["prompt"]

    @pytest.mark.asyncio
    async def test_a_long_diff_is_cut_at_the_limit(self, built):
        repo, change, config = built
        prompts = []

        async def agent_call(prompt, **kw):
            prompts.append(prompt)
            return AgentResult(success=True, output="NO PROBLEMS")

        limit = code.MAX_SELF_CHECK_DIFF_CHARS
        long_diff = "a" * limit + "TAIL-NOT-SENT" + "b" * 1000
        short_diff = "c" * (limit - 3) + "END"          # exactly the limit
        with patch("build_pipeline.findings.code.agent_call", side_effect=agent_call):
            await code.run_self_check(config, long_diff)
            await code.run_self_check(config, short_diff)
        assert "a" * limit + "\n... (diff truncated)" in prompts[0]
        assert "TAIL-NOT-SENT" not in prompts[0]
        assert short_diff in prompts[1] and "(diff truncated)" not in prompts[1]

    @pytest.mark.asyncio
    async def test_a_failed_agent_raises(self, built):
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=False, error="timed out")):
            with pytest.raises(RuntimeError, match="self-check"):
                await code.run_self_check(config, "d")


class TestDiffRange:
    def test_a_file_that_is_not_utf8_is_shown_not_raised(self, tmp_path):
        """A fix commit touching a Latin-1 file must not fail every resume of
        the code step with UnicodeDecodeError."""
        repo = tmp_path / "r"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "a.py").write_bytes(b'x = "caf\xe9"\n')
        _git(repo, "add", "a.py")
        _git(repo, "commit", "-q", "-m", "latin-1")
        diff = git_ops.diff_range(repo, base, "HEAD", exclude="specs/changes/x")
        assert diff is not None and "a.py" in diff and "caf" in diff


class TestAddBlock:
    def test_appends_once_as_pending_and_keeps_the_spend(self, tmp_path):
        tasks = tmp_path / "tasks.md"
        text, _ = code.render_tasks(1, [_fix_record()],
                                    self_check={"head": "h", "report": PROBLEM, "block": 2})
        tasks.write_text(text)
        state_path = tmp_path / "s.json"
        state = BuildState(change_name="calc", mode="only", tier="advanced",
                           state_file=str(state_path))
        state.tdd = TDDState(blocks=[BlockInfo(number=1, name="Fix finding ab12", description="d",
                                               status=BlockStatus.DONE)], current_block=1)
        state.checkpoint(state_path)
        before = json.loads(state_path.read_text()).get("budget")
        code.add_block(state_path, tasks, 2)
        code.add_block(state_path, tasks, 2)
        after = BuildState.load(state_path)
        assert [(b.number, b.status) for b in after.tdd.blocks] == [
            (1, BlockStatus.DONE), (2, BlockStatus.PENDING)]
        assert after.tdd.current_block == 1
        assert json.loads(state_path.read_text()).get("budget") == before


# --- item 28: what gets posted ------------------------------------------------------


def _findings_json(review_dir: Path) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "findings.json").write_text(json.dumps({"findings": [
        {"id": "c1", "topic": None, "file": "calc.py", "severity": "LOW", "status": "confirmed"},
        {"id": "c2", "topic": "code", "file": "README.md", "severity": "HIGH", "status": "confirmed"},
        {"id": "t1", "topic": "tests", "file": "calc.py", "severity": "MEDIUM", "status": "confirmed"},
        {"id": "t2", "topic": "tests", "file": "tests/test_calc.py", "severity": "HIGH",
         "status": "confirmed"},
        {"id": "t3", "topic": "tests", "file": "README.md", "severity": "HIGH", "status": "confirmed"},
        {"id": "d1", "topic": "docs", "file": "README.md", "severity": "HIGH", "status": "confirmed"},
    ]}))


class TestDecisions:
    def test_every_code_finding_and_tests_findings_on_source_files(self, tmp_path):
        review_dir = tmp_path / "reviews" / "acme-widgets--pr-7" / "r1"
        _findings_json(review_dir)
        path = decisions.write_decisions(review_dir)
        assert path == review_dir / "buildme-decisions.json"
        assert json.loads(path.read_text()) == {
            "c1": {"decision": "accept"}, "c2": {"decision": "accept"},
            "t1": {"decision": "accept"}}

    @pytest.mark.parametrize("mode", [None, "full", "critical"])
    def test_what_the_review_hides_is_never_accepted(self, tmp_path, mode):
        """`review_pipeline.render.listed`: a repeat of a finding the person
        rejected, a low-value one, one not confirmed or unverifiable, and in
        critical mode one not rated breaking, are not listed, so not posted."""
        def f(fid, **kw):
            return {"id": fid, "topic": "code", "file": "calc.py", "severity": "HIGH",
                    "status": "confirmed", "impact": "breaks-users", **kw}

        doc = {"findings": [
            f("ok"), f("unverif", status="unverifiable"), f("refuted", status="refuted"),
            f("rejected", status="gate-rejected"),
            f("rep", assessment={"label": "repeat"}), f("lv", assessment={"label": "low-value"}),
            f("useful", assessment={"label": "useful"}),
            f("cosmetic", severity="LOW", impact="hygiene"), f("noimpact", impact=None),
            {"id": "t1", "topic": "tests", "file": "calc.py", "severity": "MEDIUM",
             "status": "confirmed", "impact": "correctness-only"},
        ]}
        if mode is not None:
            doc["mode"] = mode
        (tmp_path / "findings.json").write_text(json.dumps(doc))
        got = set(json.loads(decisions.write_decisions(tmp_path).read_text()))
        if mode == "critical":
            assert got == {"ok", "unverif", "useful"}
        else:
            assert got == {"ok", "unverif", "useful", "cosmetic", "noimpact", "t1"}

    @pytest.mark.parametrize("path", [
        "pkg/calc_test.py", "pkg/calc_test.go", "src/calc.test.ts", "src/calc.spec.ts",
        "test/calc.js", "src/__tests__/calc.js", "Sources/CalcTests.swift",
        "src/test/java/CalcTest.java", "app/CalcTests.kt", "spec/calc_spec.rb", "conftest.py",
    ])
    def test_a_tests_finding_on_a_test_file_in_any_language_is_not_accepted(self, tmp_path, path):
        (tmp_path / "findings.json").write_text(json.dumps({"findings": [
            {"id": "t1", "topic": "tests", "file": path, "severity": "HIGH",
             "status": "confirmed"}]}))
        assert json.loads(decisions.write_decisions(tmp_path).read_text()) == {}

    def test_no_findings_json_raises_naming_it_and_writes_nothing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=re.escape(str(tmp_path / "findings.json"))):
            decisions.write_decisions(tmp_path)
        assert not (tmp_path / "buildme-decisions.json").exists()

    def test_an_unreadable_findings_json_raises_value_error(self, tmp_path):
        (tmp_path / "findings.json").write_text("{not json")
        with pytest.raises(ValueError, match=re.escape(str(tmp_path / "findings.json"))):
            decisions.write_decisions(tmp_path)
        assert not (tmp_path / "buildme-decisions.json").exists()

    @pytest.mark.asyncio
    async def test_no_findings_json_fails_the_review_step(self, built, fake_pr):
        repo, change, config = built
        fake_pr.add_thread("alice", "is this right?", fid="c1")
        q = Triage(verdict=Verdict.QUESTION, reason="It is.")
        run, lines = _run(config, change, triage_call=FakeTriage({"is this right": q}),
                          review=FakeReview(fake_pr), decisions=None)
        assert await loop.run_findings(run) == 1
        assert lines[-1].startswith("FINDINGS:FAILED:review:cannot choose what to post: "), lines
        assert "/reviews/r1/findings.json" in lines[-1]

    @pytest.mark.asyncio
    async def test_the_poster_gets_the_decisions_path(self, built, fake_pr, tmp_path):
        repo, change, config = built
        review_dir = tmp_path / "data" / "r1"
        _findings_json(review_dir)
        fake_pr.add_thread("alice", "is this right?", fid="c1")
        q = Triage(verdict=Verdict.QUESTION, reason="It is.")
        got = []
        review = FakeReview(fake_pr)
        review.review = lambda run, rnd: loop.ReviewOutcome(str(review_dir), True)
        review.post = lambda run, run_dir, path: (got.append((run_dir, path)), (True, ""))[1]
        run, lines = _run(config, change, triage_call=FakeTriage({"is this right": q}),
                          review=review, decisions=None)
        assert await loop.run_findings(run) == 0, lines
        assert got == [(str(review_dir), review_dir / "buildme-decisions.json")]
        assert (review_dir / "buildme-decisions.json").exists()

    def test_post_review_passes_the_decisions_file(self, built, monkeypatch, tmp_path):
        repo, change, config = built
        seen = []

        def review_skill(argv, cwd=None, timeout=None):
            seen.append(argv)
            return git_ops.GitResult(argv, 0, "posted", "")

        monkeypatch.setattr(git_ops, "_run_review_skill", review_skill)
        run, _ = _run(config, change, triage_call=FakeTriage({}), review=FakeReview(FakePR()))
        ok, _ = loop.post_review(run, "/data/r1", tmp_path / "buildme-decisions.json")
        assert ok
        argv = seen[0]
        assert argv[argv.index("post") + 1] == "/data/r1"
        assert argv[argv.index("--decisions") + 1] == str(tmp_path / "buildme-decisions.json")


# --- item 29: --max-rounds -----------------------------------------------------------


class TestMaxRounds:
    def test_the_flag_is_listed(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--help"])
        assert "--max-rounds" in capsys.readouterr().out

    def test_it_overrides_the_config(self, built, monkeypatch):
        repo, change, _ = built

        def gh(argv, cwd=None, timeout=None):
            if argv[:3] == ["gh", "repo", "view"]:
                return git_ops.GitResult(argv, 0, REPO + "\n", "")
            return git_ops.GitResult(argv, 0, json.dumps(
                {"headRefName": "buildme/calc", "isCrossRepository": False}), "")

        monkeypatch.setattr(git_ops, "_run_gh", gh)
        args = build_parser().parse_args(
            ["findings", "--pr", str(PR), "--project-dir", str(repo), "--max-rounds", "1"])
        assert cli.resolve(args).config.findings.max_rounds == 1
        args = build_parser().parse_args(["findings", "--pr", str(PR), "--project-dir", str(repo)])
        assert cli.resolve(args).config.findings.max_rounds == 3

    def test_zero_is_refused(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--pr", "1", "--max-rounds", "0"])


# --- two rounds of the real loop leave nothing to refuse ------------------------------


def _with_bare_origin(base: Path, repo: Path) -> Path:
    origin = base / "o" / "calc.git"
    origin.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "buildme/calc")
    return origin


def _assert_nothing_to_refuse(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert stop_record.others_work(repo, None, head=head) is None
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    for line in status.splitlines():
        rel = line[3:]
        assert _git(repo, "status", "--porcelain", "--untracked-files=all", "--", rel,
                    *git_ops.BOOKKEEPING_EXCLUDES) == "", f"{rel} is not bookkeeping"


class TestTwoRounds:
    @pytest.mark.asyncio
    async def test_two_rounds_leave_nothing_to_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        repo, change, config = _make_built(tmp_path)
        config.findings.max_rounds = 2
        _with_bare_origin(tmp_path, repo)
        pr = FakePR()
        monkeypatch.setattr(git_ops, "_run_gh", pr.gh)
        monkeypatch.setattr(git_ops, "_run_pr_skill", pr.pr_skill)   # the push is real
        pr.add_thread("alice", "add subtracts", fid="ab12")
        second = Triage(verdict=Verdict.CODE_FIX, reason="the doc is wrong",
                        locations=["calc.py:1"], failing_test="add(1, 1) returns 2")
        triage_call = FakeTriage({"add subtracts": FIX, "round 1 finding": second})
        data = tmp_path / "data" / "reviews"
        checked: list[int] = []

        def review_run(run, rnd):
            d = data / f"r{rnd.number}"
            _findings_json(d)
            return loop.ReviewOutcome(str(d), True)

        def post(run, run_dir, path):
            _assert_nothing_to_refuse(repo)
            checked.append(1)
            assert Path(path).exists() and data in Path(path).parents
            if len(checked) == 1:
                pr.add_thread("alice", "round 1 finding 0", fid="r10")
            return True, ""

        review = FakeReview(pr)
        review.review, review.post = review_run, post
        check = FakeCheck([PROBLEM, ""])
        run, lines = _run(config, change, triage_call=triage_call, review=review,
                          self_check=check, decisions=None)
        with _Stubs(block_stubs(repo)):
            assert await loop.run_findings(run) == 0, lines
        assert lines[-1] == "FINDINGS:MAX_ROUNDS:2", lines
        assert len(checked) == 2 and len(check.diffs) == 2
        _assert_nothing_to_refuse(repo)
        assert not list(repo.rglob("buildme-decisions.json"))
        assert _git(repo, "rev-parse", "HEAD").strip() == \
            _git(repo, "rev-parse", "origin/buildme/calc").strip()
