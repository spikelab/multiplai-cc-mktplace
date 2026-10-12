"""The per-block review loop keeps every round, fixes accepted findings on a
block that passed, and asks its fix agent where it looked for the same mistake.

Every test here runs the real `_run_integration_and_review` (and, for the
resume test, the real `run_tdd_engine`) against a real git repo. Only the
model calls are replaced: the reviewer, the adjudicator's LLM call and the fix
agent.
"""
import json
import logging
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline import orchestrator, review_rounds
from build_pipeline.gates import parse_searched
from build_pipeline.config import BuildConfig
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    FindingAdjudication,
    FindingVerdict,
    ReviewFinding,
    ReviewResult,
    ReviewScore,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    EXIT_BUILD_FAILURE,
    EXIT_SUCCESS,
    _run_integration_and_review,
    run_tdd_engine,
)

BUGGY = "def add(a, b):\n    return a + b + 1\n"
FIXED = "def add(a, b):\n    return a + b\n"


def _git(project: Path, *argv: str) -> str:
    return subprocess.run(["git", *argv], cwd=project, check=True,
                          capture_output=True, text=True).stdout.strip()


def _review(score: int, findings=()) -> ReviewResult:
    return ReviewResult(
        scores=[ReviewScore(dimension="Q", weight=2, score=score, evidence="e")],
        findings=list(findings),
    )


def _finding(severity="Major", claim="off-by-one in add()") -> ReviewFinding:
    return ReviewFinding(claim=claim, severity=severity, file_path="app.py", line=2,
                         evidence="returns a + b + 1")


def _passing(findings=()):
    return _review(5, findings)


def _failing(findings=()):
    return _review(2, findings)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A one-block build in a real git repo whose suite fails while
    `broken.flag` exists, so a fix agent can turn the tests red."""
    # The integration gate runs the project's test command only in a trusted repo.
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    project_dir = tmp_path / "project"
    (project_dir / "specs" / "changes" / "test").mkdir(parents=True)
    (project_dir / "app.py").write_text(BUGGY)
    _git(project_dir, "init", "-q")
    _git(project_dir, "config", "user.email", "t@example.com")
    _git(project_dir, "config", "user.name", "T")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-qm", "init")
    baseline = _git(project_dir, "rev-parse", "HEAD")

    config = BuildConfig(
        project_dir=project_dir, change_name="test", tier="advanced",
        test_command=(f"{sys.executable} -c \"import pathlib,sys; "
                      f"sys.exit(pathlib.Path('broken.flag').exists())\""),
        config_dir=tmp_path / "config",
        core_memory_files=[], stack_memory_files=[], additional_memory_files=[],
    )
    config.specs_dir = project_dir / "specs"
    block = BlockInfo(number=1, name="Adder", description="add two numbers",
                      status=BlockStatus.IMPLEMENTING, baseline_commit=baseline)
    state = BuildState(
        change_name="test", mode="only", tier="advanced",
        state_file=str(config.state_file_path()),
        tdd=TDDState(blocks=[block], baseline_tests_pass=True),
    )
    state.checkpoint(config.state_file_path())
    progress = ProgressWriter(tmp_path / "progress.md")
    progress.initialize("test", "only", "advanced", 1)
    return SimpleNamespace(dir=project_dir, config=config, state=state, block=block,
                           progress=progress, baseline=baseline)


def _fixer(project_dir: Path, files: dict[str, str] | None = None, output: str = "fixed"):
    """A fake fix agent: writes *files* into the project and reports *output*."""
    def run(**kwargs):
        for rel, text in (files or {}).items():
            (project_dir / rel).write_text(text)
        return AgentResult(success=True, output=output)
    return run


def _engine(reviews, fixer, adjudication: FindingAdjudication | None = None):
    """Patch the three model calls. Returns (stack, review_mock, fixer_mock)."""
    stack = ExitStack()
    review = stack.enter_context(patch(
        "build_pipeline.tdd_engine._run_quality_review", new_callable=AsyncMock,
        side_effect=list(reviews)))
    fix = stack.enter_context(patch(
        "build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock,
        side_effect=fixer))
    stack.enter_context(patch(
        "build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
        return_value=adjudication or FindingAdjudication()))
    return stack, review, fix


def _rounds(p) -> dict[int, dict]:
    out = {}
    for path in sorted((p.config.change_dir / "reviews").glob("block-01-round-*.json")):
        record = json.loads(path.read_text())
        out[record["round"]] = record
    return out


def _unfixed(p) -> str:
    path = p.config.change_dir / "unfixed-findings.md"
    return path.read_text() if path.exists() else ""


# --- Item 10: a record for every round -------------------------------------


class TestRoundRecords:
    @pytest.mark.asyncio
    async def test_round_files_are_written_and_never_overwritten(self, project):
        p = project
        head_at_review = _git(p.dir, "rev-parse", "HEAD")
        reviews = [_failing([_finding(), _finding("Minor", "names are unclear")]), _passing()]
        adjudication = FindingAdjudication(verdicts=[
            FindingVerdict(index=0, accepted=True),
            FindingVerdict(index=1, accepted=False, reason="the spec names them"),
        ])
        fixer = _fixer(p.dir, {"app.py": FIXED},
                       "fixed it\nSEARCHED:\n- off-by-one: both branches of add()\n")
        stack, _, _ = _engine(reviews, fixer, adjudication)
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        rounds = _rounds(p)
        assert sorted(rounds) == [1, 2]
        first = rounds[1]
        assert first["block"] == {"number": 1, "name": "Adder"}
        assert first["head_sha"] == head_at_review
        assert first["gate"]["passed"] is False and first["gate"]["reason"]
        assert first["weighted_average"] == pytest.approx(2.0)
        assert first["scores"][0]["dimension"] == "Q"
        assert [f["claim"] for f in first["accepted"]] == ["off-by-one in add()"]
        assert [f["claim"] for f in first["rejected"]] == ["names are unclear"]
        assert first["rejected"][0]["judge_reason"] == "the spec names them"
        assert "off-by-one in add()" in first["fix_brief"]
        assert "fixed it" in first["fix"]["report"]
        assert first["fix"]["searched"] == ["off-by-one: both branches of add()"]
        # The fix is committed, and the record names that commit.
        assert first["fix"]["commit"] in _git(p.dir, "log", "--format=%H").splitlines()
        assert _git(p.dir, "show", f"{first['fix']['commit']}:app.py") + "\n" == FIXED
        second = rounds[2]
        assert second["gate"]["passed"] is True
        assert second["fix_brief"] is None and second["fix"] is None
        # Committed with the block's commits.
        assert not _git(p.dir, "status", "--porcelain", "--", "specs/changes/test/reviews")
        assert "specs/changes/test/reviews/block-01-round-2.json" in _git(
            p.dir, "ls-files", "specs/changes/test/reviews")

    @pytest.mark.asyncio
    async def test_round_numbers_continue_across_a_resume(self, project):
        p = project
        reviews_dir = p.config.change_dir / "reviews"
        reviews_dir.mkdir()
        earlier = {k: json.dumps({"round": k, "from": "the run that died"})
                   for k in (1, 2)}
        for k, text in earlier.items():
            (reviews_dir / f"block-01-round-{k}.json").write_text(text)

        stack, _, _ = _engine([_passing()], _fixer(p.dir))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        for k, text in earlier.items():
            assert (reviews_dir / f"block-01-round-{k}.json").read_text() == text
        assert _rounds(p)[3]["gate"]["passed"] is True

    @pytest.mark.asyncio
    async def test_the_reviewer_never_sees_the_round_records_in_its_diff(self, project):
        p = project
        seen = []

        async def review(block, config):
            from build_pipeline.tdd_engine import _capture_block_diff
            seen.append(_capture_block_diff(config, block))
            return _failing([_finding()]) if len(seen) == 1 else _passing()

        with patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock,
                   side_effect=_fixer(p.dir, {"app.py": FIXED})), \
             patch("build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
                   return_value=FindingAdjudication()):
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert "return a + b\n" in seen[1]
        assert "block-01-round-1.json" not in seen[1]

    @pytest.mark.asyncio
    async def test_the_whole_build_diff_leaves_the_round_records_out(self, project):
        from build_pipeline.tdd_engine import capture_build_diff
        p = project
        stack, _, _ = _engine([_failing([_finding()]), _passing()],
                              _fixer(p.dir, {"app.py": FIXED}))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert "specs/changes/test/reviews/" in _git(p.dir, "ls-files")
        diff = capture_build_diff(p.config, p.state)
        assert "return a + b\n" in diff
        assert "reviews/block-01-round-1.json" not in diff

    def test_record_round_refuses_to_overwrite(self, tmp_path):
        block = BlockInfo(number=3, name="B", description="d")
        first = review_rounds.record_round(tmp_path, block, head_sha="a" * 40,
                                           review=_passing(), gate_passed=True,
                                           gate_reason="ok", weighted_average=5.0)
        second = review_rounds.record_round(tmp_path, block, head_sha="b" * 40,
                                            review=_passing(), gate_passed=True,
                                            gate_reason="ok", weighted_average=5.0)
        assert first.name == "block-03-round-1.json"
        assert second.name == "block-03-round-2.json"
        assert json.loads(first.read_text())["head_sha"] == "a" * 40


# --- Item 11: fix accepted findings on a block that passed -----------------


class TestFixOnPass:
    @pytest.mark.asyncio
    async def test_fix_on_pass_kept_when_tests_green_and_score_still_passes(self, project):
        p = project
        stack, review, fix = _engine([_passing([_finding()]), _passing()],
                                     _fixer(p.dir, {"app.py": FIXED}))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert fix.await_count == 1
        assert review.await_count == 2
        assert p.block.status == BlockStatus.DONE
        assert (p.dir / "app.py").read_text() == FIXED
        assert _git(p.dir, "show", "HEAD:app.py") + "\n" == FIXED
        rounds = _rounds(p)
        assert rounds[1]["fix"]["kept"] is True
        assert rounds[1]["fix"]["commit"] in _git(p.dir, "log", "--format=%H").splitlines()
        assert rounds[2]["kind"] == "fix_on_pass_review"
        assert _unfixed(p) == ""
        # The pass does not count toward MAX_REVIEW_ITERATIONS.
        assert p.block.review_iterations == 1
        assert p.block.fix_pass is None

    @pytest.mark.asyncio
    async def test_fix_on_pass_rolled_back_when_tests_go_red(self, project):
        p = project
        stack, review, fix = _engine(
            [_passing([_finding()])],
            _fixer(p.dir, {"app.py": FIXED, "broken.flag": "x"}))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert fix.await_count == 1
        assert review.await_count == 1  # tests went red: no second review
        assert p.block.status == BlockStatus.DONE
        assert (p.dir / "app.py").read_text() == BUGGY
        assert not (p.dir / "broken.flag").exists()
        unfixed = _unfixed(p)
        assert unfixed.startswith("## ")
        assert "Block 1 (Adder)" in unfixed
        assert "off-by-one in add()" in unfixed
        assert "fix not kept" in unfixed
        assert _rounds(p)[1]["fix"]["kept"] is False
        # The note is committed with the block.
        assert "specs/changes/test/unfixed-findings.md" in _git(p.dir, "ls-files")

    @pytest.mark.asyncio
    async def test_fix_on_pass_rolled_back_when_the_score_drops(self, project):
        p = project
        stack, review, _ = _engine([_passing([_finding()]), _failing()],
                                   _fixer(p.dir, {"app.py": FIXED}))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert review.await_count == 2
        assert p.block.status == BlockStatus.DONE
        assert (p.dir / "app.py").read_text() == BUGGY
        assert "fix not kept" in _unfixed(p)
        rounds = _rounds(p)
        # The rollback does not take the second round's record with it.
        assert rounds[2]["gate"]["passed"] is False
        assert rounds[1]["fix"]["kept"] is False
        # The block keeps the review it passed with, not the one it failed.
        assert p.block.review_scores.weighted_average == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_a_block_whose_only_findings_are_notes_gets_no_fix_pass(self, project):
        p = project
        stack, review, fix = _engine([_passing([_finding("Note", "consider a docstring")])],
                                     _fixer(p.dir))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        fix.assert_not_awaited()
        assert review.await_count == 1
        assert _unfixed(p) == ""

    @pytest.mark.asyncio
    async def test_the_review_after_the_fix_sees_a_file_the_fix_created(self, project):
        """The fix is committed only after the second review keeps it, so a new
        file the fix agent wrote is still untracked when that review runs. It
        is committed with the fix, so the reviewer and the adjudicator must
        both see it."""
        p = project
        helper = "def helper():\n    return 'HELPER BODY'\n"
        reviewed, adjudicated = [], []

        async def review(block, config):
            from build_pipeline.tdd_engine import _capture_block_diff
            reviewed.append(_capture_block_diff(config, block))
            return _passing([_finding()])

        async def adjudicate(review, block, config, diff, **kwargs):
            adjudicated.append(diff)
            return review

        with patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review), \
             patch("build_pipeline.tdd_engine._adjudicate_review_findings",
                   side_effect=adjudicate), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock,
                   side_effect=_fixer(p.dir, {"app.py": FIXED, "helpers.py": helper})):
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert len(reviewed) == 2 and len(adjudicated) == 2
        for diff in (reviewed[1], adjudicated[1]):
            assert "helpers.py" in diff
            assert "HELPER BODY" in diff
        # And the kept fix commits it.
        fix_commit = _rounds(p)[1]["fix"]["commit"]
        assert "helpers.py" in _git(p.dir, "show", "--name-only", "--format=", fix_commit)

    @pytest.mark.asyncio
    async def test_fix_on_pass_false_keeps_todays_behaviour(self, project):
        p = project
        p.config.fix_on_pass = False
        stack, review, fix = _engine([_passing([_finding()])], _fixer(p.dir))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        fix.assert_not_awaited()
        assert review.await_count == 1
        assert p.block.status == BlockStatus.DONE
        assert _unfixed(p) == ""

    @pytest.mark.asyncio
    async def test_a_build_killed_after_the_fix_agent_rolls_back_and_reruns_the_pass(
        self, project,
    ):
        """Dies after the fix agent and before the second review. The resume
        throws the half-applied fix away and runs the pass again."""
        p = project

        class Killed(BaseException):
            pass

        stack, _, _ = _engine([_passing([_finding()]), Killed()],
                              _fixer(p.dir, {"app.py": "half-applied\n"}))
        with stack, pytest.raises(Killed):
            await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        saved = BuildState.load(p.config.state_file_path())
        pending = saved.tdd.blocks[0].fix_pass
        assert pending is not None
        assert pending.checkpoint == _git(p.dir, "rev-parse", "HEAD")
        assert pending.step == "review"
        assert (p.dir / "app.py").read_text() == "half-applied\n"

        seen_at_fix = []

        def fixer(**kwargs):
            seen_at_fix.append((p.dir / "app.py").read_text())
            (p.dir / "app.py").write_text(FIXED)
            return AgentResult(success=True, output="fixed\nSEARCHED:\n- add(): both branches\n")

        stack, review, fix = _engine([_passing()], fixer)
        with stack, \
             patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock) as writer, \
             patch("build_pipeline.tdd_engine._run_refactor_all", new_callable=AsyncMock,
                   return_value=True), \
             patch("build_pipeline.tdd_engine._run_mutation", new_callable=AsyncMock,
                   return_value=True):
            p.config.gates.e2e_test_entry_point_check = False
            code = await run_tdd_engine(p.config, SimpleNamespace(block=None), standalone=False)

        assert code == EXIT_SUCCESS
        writer.assert_not_awaited()  # the block is not rebuilt, only the pass reruns
        assert seen_at_fix == [BUGGY]  # rolled back before the fix agent ran again
        assert fix.await_count == 1 and review.await_count == 1
        assert (p.dir / "app.py").read_text() == FIXED
        final = BuildState.load(p.config.state_file_path()).tdd.blocks[0]
        assert final.status == BlockStatus.DONE
        assert final.fix_pass is None
        rounds = _rounds(p)
        assert rounds[1]["fix"]["kept"] is True
        assert rounds[1]["fix"]["searched"] == ["add(): both branches"]
        # The dead attempt's report is kept next to the one that finished.
        assert rounds[1]["earlier_fixes"][0]["report"] == "fixed"

    @staticmethod
    async def _kill_after_fix_agent(p):
        """Run the pass and die before the second review, leaving the fix
        agent's half-applied fix in the tree."""
        class Killed(BaseException):
            pass

        stack, _, _ = _engine([_passing([_finding()]), Killed()],
                              _fixer(p.dir, {"app.py": "half-applied\n"}))
        with stack, pytest.raises(Killed):
            await _run_integration_and_review(p.block, p.config, p.state, p.progress)

    @staticmethod
    async def _resume(p, reviews=(), fixer=None):
        """Re-run the engine from its checkpoint; returns (exit code, fix mock)."""
        stack, _, fix = _engine(list(reviews), fixer or _fixer(p.dir, {"app.py": FIXED}))
        with stack, \
             patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock), \
             patch("build_pipeline.tdd_engine._run_refactor_all", new_callable=AsyncMock,
                   return_value=True), \
             patch("build_pipeline.tdd_engine._run_mutation", new_callable=AsyncMock,
                   return_value=True):
            p.config.gates.e2e_test_entry_point_check = False
            code = await run_tdd_engine(p.config, SimpleNamespace(block=None), standalone=False)
        return code, fix

    def _assert_refused(self, p, code, fix, head):
        assert code == EXIT_BUILD_FAILURE
        fix.assert_not_awaited()
        assert _git(p.dir, "rev-parse", "HEAD") == head  # nothing was reset
        saved = BuildState.load(p.config.state_file_path()).tdd.blocks[0]
        assert saved.fix_pass is not None  # the next resume tries the pass again
        assert saved.status == BlockStatus.FAILED and saved.resumable is True
        assert "Did not resume the fix on pass" in p.config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_a_resume_refuses_a_file_the_user_added_after_the_stop(self, project):
        p = project
        await self._kill_after_fix_agent(p)
        head = _git(p.dir, "rev-parse", "HEAD")
        (p.dir / "mine.py").write_text("the user's work\n")

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head)
        assert (p.dir / "mine.py").read_text() == "the user's work\n"
        assert (p.dir / "app.py").read_text() == "half-applied\n"

        # Once the user moves their work away, the resume rolls back and reruns.
        (p.dir / "mine.py").unlink()
        code, fix = await self._resume(p, [_passing()])
        assert code == EXIT_SUCCESS
        assert fix.await_count == 1
        assert (p.dir / "app.py").read_text() == FIXED

    @pytest.mark.asyncio
    async def test_a_resume_refuses_an_edit_to_a_file_the_fix_agent_left(self, project):
        p = project
        await self._kill_after_fix_agent(p)
        head = _git(p.dir, "rev-parse", "HEAD")
        (p.dir / "app.py").write_text("half-applied\n# and the user's line\n")

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head)
        assert (p.dir / "app.py").read_text() == "half-applied\n# and the user's line\n"

    @pytest.mark.asyncio
    async def test_a_resume_refuses_a_commit_added_after_the_stop(self, project):
        p = project
        await self._kill_after_fix_agent(p)
        (p.dir / "app.py").write_text("the user's commit\n")
        _git(p.dir, "commit", "-qam", "user")
        head = _git(p.dir, "rev-parse", "HEAD")

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head)
        assert (p.dir / "app.py").read_text() == "the user's commit\n"

    @pytest.mark.asyncio
    async def test_a_resume_refuses_edits_left_by_a_fix_agent_killed_mid_edit(self, project):
        """Killed while the fix agent ran: no record says which edits are
        the agent's, so none of them is discarded."""
        p = project

        class Killed(BaseException):
            pass

        def dying_fixer(**kwargs):
            (p.dir / "app.py").write_text("half-applied\n")
            raise Killed()

        stack, _, _ = _engine([_passing([_finding()])], dying_fixer)
        with stack, pytest.raises(Killed):
            await _run_integration_and_review(p.block, p.config, p.state, p.progress)
        assert BuildState.load(p.config.state_file_path()).tdd.blocks[0].fix_pass.step == "fix_agent"
        head = _git(p.dir, "rev-parse", "HEAD")

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head)
        assert (p.dir / "app.py").read_text() == "half-applied\n"


# --- Item 12: the fix agent looks for the same mistake ---------------------


class TestFixAgentSelfCheck:
    @pytest.mark.parametrize("reviews", [
        pytest.param([_failing([_finding()]), _passing()], id="failing-score"),
        pytest.param([_passing([_finding()]), _passing()], id="fix-on-pass"),
    ])
    @pytest.mark.asyncio
    async def test_fix_brief_asks_for_the_same_mistake_elsewhere_and_a_diff_reread(
        self, project, reviews,
    ):
        p = project
        stack, _, fix = _engine(reviews, _fixer(p.dir, {"app.py": FIXED}))
        with stack:
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        brief = fix.await_args.kwargs["block_description"]
        assert "off-by-one in add()" in brief
        assert "other branches of the same function" in brief
        assert "git diff" in brief
        assert "SEARCHED:" in brief

    @pytest.mark.asyncio
    async def test_a_missing_searched_section_gives_null_and_a_warning(self, project, caplog):
        p = project
        stack, _, _ = _engine([_failing([_finding()]), _passing()],
                              _fixer(p.dir, {"app.py": FIXED}, "fixed, no section"))
        with stack, caplog.at_level(logging.WARNING, logger="build_pipeline.tdd_engine"):
            assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)

        assert _rounds(p)[1]["fix"]["searched"] is None
        assert any("SEARCHED" in r.getMessage() for r in caplog.records)

    def test_a_searched_section_is_parsed(self):
        report = (
            "STATUS: DONE\nFILES: app.py\n"
            "SEARCHED:\n"
            "- off-by-one in add(): the else branch of add(), and sub() in the same diff\n"
            "* **naming**: every function in app.py\n"
            "\n"
            "SURPRISES: none\n"
        )
        assert parse_searched(report) == [
            "off-by-one in add(): the else branch of add(), and sub() in the same diff",
            "**naming**: every function in app.py",
        ]

    def test_a_searched_section_on_one_line_is_parsed(self):
        assert parse_searched("SEARCHED: add() and sub()") == ["add() and sub()"]

    def test_no_searched_section_is_none(self):
        assert parse_searched("STATUS: DONE\nSURPRISES: none") is None
        assert parse_searched("") is None

    def test_searched_does_not_swallow_into_surprises(self):
        from build_pipeline.gates import parse_implementation_note
        note = parse_implementation_note(
            "SURPRISES: the spec said ints\nSEARCHED:\n- add()\n",
            block_number=1, block_name="B", role="implementer")
        assert note.surprises == "the spec said ints"


# --- PUBLISH hands the unfixed findings to the pr skill --------------------


class TestUnfixedFindingsSection:
    def test_unfixed_findings_file_is_a_pr_section(self, tmp_path):
        assert orchestrator.unfixed_findings_section in orchestrator.PRE_PUBLISH_CONTRIBUTORS
        assert orchestrator.unfixed_findings_section(None, None, tmp_path) is None
        block = BlockInfo(number=2, name="Parser", description="d")
        path = review_rounds.append_unfixed(tmp_path, block, [_finding()], "fix not kept")
        assert orchestrator.unfixed_findings_section(None, None, tmp_path) == path
        text = path.read_text()
        assert text.splitlines()[0].startswith("## ")
        assert "Block 2 (Parser)" in text and "app.py:2" in text

    def test_appending_keeps_earlier_entries(self, tmp_path):
        block = BlockInfo(number=1, name="A", description="d")
        review_rounds.append_unfixed(tmp_path, block, [_finding()], "fix not kept")
        review_rounds.append_unfixed(tmp_path, block, [_finding("Minor", "second")],
                                     "not attempted")
        text = (tmp_path / "unfixed-findings.md").read_text()
        assert text.count("## ") == 1
        assert "off-by-one in add()" in text and "second" in text
        assert "not attempted" in text
