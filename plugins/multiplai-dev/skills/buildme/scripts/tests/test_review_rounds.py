"""The per-block review loop keeps every round, fixes accepted findings on a
block that passed, and asks its fix agent where it looked for the same mistake.

Every test here runs the real `_run_integration_and_review` (and, for the
resume test, the real `run_tdd_engine`) against a real git repo. Only the
model calls are replaced: the reviewer, the adjudicator's LLM call and the fix
agent.
"""

import json
import logging
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import orchestrator, review_rounds
from build_pipeline.config import BuildConfig
from build_pipeline.gates import parse_searched
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
    return subprocess.run(
        ["git", *argv], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()


def _review(score: int, findings=()) -> ReviewResult:
    return ReviewResult(
        scores=[ReviewScore(dimension="Q", weight=2, score=score, evidence="e")],
        findings=list(findings),
    )


def _finding(severity="Major", claim="off-by-one in add()") -> ReviewFinding:
    return ReviewFinding(
        claim=claim,
        severity=severity,
        file_path="app.py",
        line=2,
        evidence="returns a + b + 1",
    )


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
    return _make_project(tmp_path)


@pytest.fixture
def project_in_subdir(tmp_path, monkeypatch):
    """`project`, in `root/project` of a repo rooted at `root` that also
    holds a committed `other/notes.txt`."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    return _make_project(tmp_path, subdir=True)


def _make_project(base: Path, subdir: bool = False) -> SimpleNamespace:
    """The `project` fixture's build under *base*; with *subdir*, the project
    is `base/root/project` and the repo is rooted at `base/root`."""
    root = base / "root" if subdir else base / "project"
    project_dir = root / "project" if subdir else root
    (project_dir / "specs" / "changes" / "test").mkdir(parents=True)
    (project_dir / "app.py").write_text(BUGGY)
    if subdir:
        (root / "other").mkdir()
        (root / "other" / "notes.txt").write_text("v1\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    baseline = _git(project_dir, "rev-parse", "HEAD")

    config = BuildConfig(
        project_dir=project_dir,
        change_name="test",
        tier="advanced",
        test_command=(
            f'{sys.executable} -c "import pathlib,sys; '
            f"sys.exit(pathlib.Path('broken.flag').exists())\""
        ),
        config_dir=base / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = project_dir / "specs"
    block = BlockInfo(
        number=1,
        name="Adder",
        description="add two numbers",
        status=BlockStatus.IMPLEMENTING,
        baseline_commit=baseline,
    )
    state = BuildState(
        change_name="test",
        tier="advanced",
        state_file=str(config.state_file_path()),
        tdd=TDDState(blocks=[block], baseline_tests_pass=True),
    )
    state.checkpoint(config.state_file_path())
    progress = ProgressWriter(base / "progress.md")
    progress.initialize("test", "advanced", 1)
    return SimpleNamespace(
        dir=project_dir,
        root=root,
        config=config,
        state=state,
        block=block,
        progress=progress,
        baseline=baseline,
    )


def _fixer(
    project_dir: Path, files: dict[str, str] | None = None, output: str = "fixed"
):
    """A fake fix agent: writes *files* into the project and reports *output*."""

    def run(**kwargs):
        for rel, text in (files or {}).items():
            (project_dir / rel).write_text(text)
        return AgentResult(success=True, output=output)

    return run


def _engine(reviews, fixer, adjudication: FindingAdjudication | None = None):
    """Patch the three model calls. Returns (stack, review_mock, fixer_mock)."""
    stack = ExitStack()
    review = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine._run_quality_review",
            new_callable=AsyncMock,
            side_effect=list(reviews),
        )
    )
    fix = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.run_implementer",
            new_callable=AsyncMock,
            side_effect=fixer,
        )
    )
    stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.llm_call_structured",
            new_callable=AsyncMock,
            return_value=adjudication or FindingAdjudication(),
        )
    )
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
        # Round 2 calls round 1's finding resolved, so the block passes with
        # nothing open and no fix on pass.
        resolved = _passing().model_copy(
            update={
                "prior_assessments": [
                    PriorAssessment(finding_id="B01-R1-F1", status="resolved")
                ]
            }
        )
        reviews = [
            _failing([_finding(), _finding("Minor", "names are unclear")]),
            resolved,
        ]
        adjudication = FindingAdjudication(
            verdicts=[
                FindingVerdict(index=0, accepted=True),
                FindingVerdict(index=1, accepted=False, reason="the spec names them"),
            ]
        )
        fixer = _fixer(
            p.dir,
            {"app.py": FIXED},
            "fixed it\nSEARCHED:\n- off-by-one: both branches of add()\n",
        )
        stack, _, _ = _engine(reviews, fixer, adjudication)
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        rounds = _rounds(p)
        assert sorted(rounds) == [1, 2]
        first = rounds[1]
        assert first["block"] == {"number": 1, "name": "Adder"}
        assert first["head_sha"] == head_at_review
        # Nothing was uncommitted when it ran, so HEAD is the tree it read.
        assert (
            first["reviewed_sha"] == head_at_review
            and "reviewed_sha_unknown" not in first
        )
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
        assert not _git(
            p.dir, "status", "--porcelain", "--", "specs/changes/test/reviews"
        )
        assert "specs/changes/test/reviews/block-01-round-2.json" in _git(
            p.dir, "ls-files", "specs/changes/test/reviews"
        )

    @pytest.mark.asyncio
    async def test_round_numbers_continue_across_a_resume(self, project):
        p = project
        reviews_dir = p.config.change_dir / "reviews"
        reviews_dir.mkdir()
        earlier = {
            k: json.dumps({"round": k, "from": "the run that died"}) for k in (1, 2)
        }
        for k, text in earlier.items():
            (reviews_dir / f"block-01-round-{k}.json").write_text(text)

        stack, _, _ = _engine([_passing()], _fixer(p.dir))
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        for k, text in earlier.items():
            assert (reviews_dir / f"block-01-round-{k}.json").read_text() == text
        assert _rounds(p)[3]["gate"]["passed"] is True

    @pytest.mark.asyncio
    async def test_the_reviewer_never_sees_the_round_records_in_its_diff(self, project):
        p = project
        seen = []

        async def review(block, config, **_):
            from build_pipeline.tdd_engine import _capture_block_diff

            seen.append(_capture_block_diff(config, block))
            return _failing([_finding()]) if len(seen) == 1 else _passing()

        with (
            patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                side_effect=_fixer(p.dir, {"app.py": FIXED}),
            ),
            patch(
                "build_pipeline.tdd_engine.llm_call_structured",
                new_callable=AsyncMock,
                return_value=FindingAdjudication(),
            ),
        ):
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert "return a + b\n" in seen[1]
        assert "block-01-round-1.json" not in seen[1]

    @pytest.mark.asyncio
    async def test_the_whole_build_diff_leaves_the_round_records_out(self, project):
        from build_pipeline.tdd_engine import capture_build_diff

        p = project
        stack, _, _ = _engine(
            [_failing([_finding()]), _passing()], _fixer(p.dir, {"app.py": FIXED})
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert "specs/changes/test/reviews/" in _git(p.dir, "ls-files")
        diff = capture_build_diff(p.config, p.state)
        assert "return a + b\n" in diff
        assert "reviews/block-01-round-1.json" not in diff

    def test_record_round_refuses_to_overwrite(self, tmp_path):
        block = BlockInfo(number=3, name="B", description="d")
        first = review_rounds.record_round(
            tmp_path,
            block,
            head_sha="a" * 40,
            review=_passing(),
            gate_passed=True,
            gate_reason="ok",
            weighted_average=5.0,
        )
        second = review_rounds.record_round(
            tmp_path,
            block,
            head_sha="b" * 40,
            review=_passing(),
            gate_passed=True,
            gate_reason="ok",
            weighted_average=5.0,
        )
        assert first.name == "block-03-round-1.json"
        assert second.name == "block-03-round-2.json"
        assert json.loads(first.read_text())["head_sha"] == "a" * 40


# --- Item 11: fix accepted findings on a block that passed -----------------


class TestFixOnPass:
    @pytest.mark.asyncio
    async def test_fix_on_pass_kept_when_tests_green_and_score_still_passes(
        self, project
    ):
        from build_pipeline.models import PriorAssessment

        p = project
        resolved = _passing().model_copy(
            update={
                "prior_assessments": [
                    PriorAssessment(finding_id="B01-R1-F1", status="resolved")
                ]
            }
        )
        stack, review, fix = _engine(
            [_passing([_finding()]), resolved], _fixer(p.dir, {"app.py": FIXED})
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert fix.await_count == 1
        assert review.await_count == 2
        assert p.block.status == BlockStatus.DONE
        assert (p.dir / "app.py").read_text() == FIXED
        assert _git(p.dir, "show", "HEAD:app.py") + "\n" == FIXED
        rounds = _rounds(p)
        assert rounds[1]["fix"]["kept"] is True
        assert (
            rounds[1]["fix"]["commit"] in _git(p.dir, "log", "--format=%H").splitlines()
        )
        assert rounds[2]["kind"] == "fix_on_pass_review"
        assert _unfixed(p) == ""
        # The pass does not count toward MAX_REVIEW_ITERATIONS.
        assert p.block.review_iterations == 1
        assert p.block.fix_pass is None
        assert p.block.fix_on_pass_kept is True

    @pytest.mark.asyncio
    async def test_the_review_after_an_uncommitted_fix_on_pass_names_no_reviewed_commit(
        self, project
    ):
        p = project
        stack, _, _ = _engine(
            [_passing([_finding("Minor", NAMES)]), _passing()],
            _fixer(p.dir, {"app.py": FIXED}),
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )
        terminal = _rounds(p)[2]
        assert terminal["kind"] == "fix_on_pass_review"
        assert terminal["head_sha"] and terminal["reviewed_sha"] is None
        assert "uncommitted" in terminal["reviewed_sha_unknown"]

    @pytest.mark.asyncio
    async def test_a_finding_the_fix_on_pass_left_open_survives_its_review(
        self, project
    ):
        """Round 1 fails on a Major finding, the fix changes nothing, round 2
        passes and says it is still present, the fix on pass changes nothing,
        and the review after it passes with no findings and no assessment."""
        from build_pipeline.models import PriorAssessment
        from build_pipeline.state import BuildState as Loaded

        p = project
        still = _passing().model_copy(
            update={
                "prior_assessments": [
                    PriorAssessment(finding_id="B01-R1-F1", status="still_present")
                ]
            }
        )
        stack, review, fix = _engine(
            [_failing([_finding()]), still, _passing()], _fixer(p.dir)
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert fix.await_count == 2
        assert p.block.status == BlockStatus.DONE
        assert (p.dir / "app.py").read_text() == BUGGY
        # The review after the fix on pass is given the open finding, like any later round.
        assert [
            f.finding_id for f in review.await_args_list[2].kwargs["open_findings"]
        ] == ["B01-R1-F1"]
        assert [f.finding_id for f in p.block.review_scores.findings] == ["B01-R1-F1"]
        rounds = _rounds(p)
        assert rounds[3]["kind"] == "fix_on_pass_review"
        assert [f["finding_id"] for f in rounds[3]["open_findings"]] == ["B01-R1-F1"]
        assert [
            (a["finding_id"], a["status"]) for a in rounds[3]["prior_assessments"]
        ] == [("B01-R1-F1", "cannot_verify")]
        unfixed = _unfixed(p)
        assert "[Major] off-by-one in add()" in unfixed
        assert "still open after the fix on pass (cannot_verify" in unfixed
        saved = Loaded.load(p.config.state_file_path())
        assert [f.finding_id for f in saved.tdd.blocks[0].review_scores.findings] == [
            "B01-R1-F1"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["resolved", "still_present", "partially_resolved", "cannot_verify", None],
    )
    async def test_the_review_after_the_fix_on_pass_retires_only_a_resolved_finding(
        self, project, status
    ):
        from build_pipeline.models import PriorAssessment

        p = project
        final = _passing()
        if status:
            final = final.model_copy(
                update={
                    "prior_assessments": [
                        PriorAssessment(finding_id="B01-R1-F1", status=status)
                    ]
                }
            )
        stack, review, fix = _engine(
            [_passing([_finding()]), final], _fixer(p.dir, {"app.py": FIXED})
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert p.block.fix_on_pass_kept is True
        assert [
            f.finding_id for f in review.await_args_list[1].kwargs["open_findings"]
        ] == ["B01-R1-F1"]
        left = [f.finding_id for f in p.block.review_scores.findings]
        if status == "resolved":
            assert left == [] and _unfixed(p) == ""
        else:
            assert left == ["B01-R1-F1"]
            assert (
                f"still open after the fix on pass ({status or 'cannot_verify'}"
                in _unfixed(p)
            )
        assert [a["status"] for a in _rounds(p)[2]["prior_assessments"]] == [
            status or "cannot_verify"
        ]

    @pytest.mark.asyncio
    async def test_with_no_round_record_the_findings_of_the_fix_on_pass_stay_open(
        self, project
    ):
        """`reviews` is a file, so no round record can be written: a review
        that passes with no assessment cannot retire what the pass worked on."""
        p = project
        (p.config.change_dir / "reviews").write_text("not a directory\n")
        _git(p.dir, "add", ".")
        _git(p.dir, "commit", "-qm", "reviews is a file")
        stack, review, _ = _engine([_passing([_finding()]), _passing()], _fixer(p.dir))
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert p.block.status == BlockStatus.DONE
        assert [f.claim for f in p.block.review_scores.findings] == [
            "off-by-one in add()"
        ]
        assert "off-by-one in add()" in _unfixed(p)
        assert (
            "still open after the fix on pass (no round record to check it against"
            in _unfixed(p)
        )

    @pytest.mark.asyncio
    async def test_an_older_record_does_not_stand_in_for_the_one_that_failed(
        self, project
    ):
        """Round 1 is recorded; round 2's record cannot be written. The review
        after the fix on pass must not take round 1's open list for round 2's."""
        from build_pipeline.models import PriorAssessment

        p = project
        real = review_rounds.record_round
        calls = []

        def record(*args, **kwargs):
            calls.append(kwargs.get("kind"))
            return None if len(calls) == 2 else real(*args, **kwargs)

        resolved_r1 = _passing([_finding("Major", "a second defect")]).model_copy(
            update={
                "prior_assessments": [
                    PriorAssessment(finding_id="B01-R1-F1", status="resolved")
                ]
            }
        )
        stack, review, _ = _engine(
            [_failing([_finding()]), resolved_r1, _passing()],
            _fixer(p.dir, {"app.py": FIXED}),
        )
        with (
            stack,
            patch("build_pipeline.review_rounds.record_round", side_effect=record),
        ):
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert "open_findings" not in review.await_args_list[2].kwargs
        assert [f.claim for f in p.block.review_scores.findings] == ["a second defect"]
        assert "a second defect" in _unfixed(p) and "off-by-one" not in _unfixed(p)
        # The record of the review after the fix says the same as the block and the report.
        terminal = max(_rounds(p).values(), key=lambda r: r["round"])
        assert terminal["kind"] == "fix_on_pass_review"
        assert [f["claim"] for f in terminal["open_findings"]] == ["a second defect"]
        assert terminal["prior_assessments"] == []
        saved = BuildState.load(p.config.state_file_path())
        assert [f.claim for f in saved.tdd.blocks[0].review_scores.findings] == [
            "a second defect"
        ]

    @pytest.mark.asyncio
    async def test_fix_on_pass_rolled_back_when_tests_go_red(self, project):
        p = project
        stack, review, fix = _engine(
            [_passing([_finding()])],
            _fixer(p.dir, {"app.py": FIXED, "broken.flag": "x"}),
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

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
        stack, review, _ = _engine(
            [_passing([_finding()]), _failing()], _fixer(p.dir, {"app.py": FIXED})
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

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
        assert p.block.fix_on_pass_kept is False

    @pytest.mark.asyncio
    async def test_a_block_whose_only_findings_are_notes_gets_no_fix_pass(
        self, project
    ):
        p = project
        stack, review, fix = _engine(
            [_passing([_finding("Note", "consider a docstring")])], _fixer(p.dir)
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

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

        async def review(block, config, **kwargs):
            from build_pipeline.tdd_engine import _capture_block_diff

            reviewed.append(_capture_block_diff(config, block))
            return _passing([_finding()])

        async def adjudicate(review, block, config, diff, **kwargs):
            adjudicated.append(diff)
            return review

        with (
            patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review),
            patch(
                "build_pipeline.tdd_engine._adjudicate_review_findings",
                side_effect=adjudicate,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                side_effect=_fixer(p.dir, {"app.py": FIXED, "helpers.py": helper}),
            ),
        ):
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        assert len(reviewed) == 2 and len(adjudicated) == 2
        for diff in (reviewed[1], adjudicated[1]):
            assert "helpers.py" in diff
            assert "HELPER BODY" in diff
        # And the kept fix commits it.
        fix_commit = _rounds(p)[1]["fix"]["commit"]
        assert "helpers.py" in _git(
            p.dir, "show", "--name-only", "--format=", fix_commit
        )

    @pytest.mark.asyncio
    async def test_fix_on_pass_false_keeps_todays_behaviour(self, project):
        p = project
        p.config.fix_on_pass = False
        stack, review, fix = _engine([_passing([_finding()])], _fixer(p.dir))
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        fix.assert_not_awaited()
        assert review.await_count == 1
        assert p.block.status == BlockStatus.DONE
        assert _unfixed(p) == ""

    @pytest.mark.asyncio
    async def test_row_F2_a_pass_stopped_at_review_rolls_back_and_reruns(
        self,
        project,
        tmp_path,
    ):
        """Dies after the fix agent and before the second review. The resume
        throws the half-applied fix away and runs the pass again."""
        p = project

        class Killed(BaseException):
            pass

        stack, _, _ = _engine(
            [_passing([_finding()]), Killed()],
            _fixer(p.dir, {"app.py": "half-applied\n"}),
        )
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
            return AgentResult(
                success=True, output="fixed\nSEARCHED:\n- add(): both branches\n"
            )

        stack, review, fix = _engine([_passing()], fixer)
        with (
            stack,
            patch(
                "build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock
            ) as writer,
            patch(
                "build_pipeline.tdd_engine._run_refactor_all",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "build_pipeline.tdd_engine._run_mutation",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            p.config.gates.e2e_test_entry_point_check = False
            code = await run_tdd_engine(
                p.config, SimpleNamespace(block=None), standalone=False
            )

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

        clean = _make_project(tmp_path / "unstopped")
        clean_code = await _run_unstopped(
            clean,
            fixer=_fixer(
                clean.dir,
                {"app.py": FIXED},
                "fixed\nSEARCHED:\n- add(): both branches\n",
            ),
        )
        assert clean_code == code
        assert_same_as_unstopped(p.root, clean.root)

    @staticmethod
    async def _kill_after_fix_agent(p):
        """Run the pass and die before the second review, leaving the fix
        agent's half-applied fix in the tree."""

        class Killed(BaseException):
            pass

        stack, _, _ = _engine(
            [_passing([_finding()]), Killed()],
            _fixer(p.dir, {"app.py": "half-applied\n"}),
        )
        with stack, pytest.raises(Killed):
            await _run_integration_and_review(p.block, p.config, p.state, p.progress)

    @staticmethod
    async def _resume(p, reviews=(), fixer=None):
        """Re-run the engine from its checkpoint; returns (exit code, fix mock)."""
        stack, _, fix = _engine(
            list(reviews), fixer or _fixer(p.dir, {"app.py": FIXED})
        )
        with (
            stack,
            patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock),
            patch(
                "build_pipeline.tdd_engine._run_refactor_all",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "build_pipeline.tdd_engine._run_mutation",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            p.config.gates.e2e_test_entry_point_check = False
            code = await run_tdd_engine(
                p.config, SimpleNamespace(block=None), standalone=False
            )
        return code, fix

    def _assert_refused(self, p, code, fix, head, before):
        """The resume refused: nothing ran, the block waits for the next
        resume, and the repo is exactly as it was (`before`, a `repo_state`)."""
        assert repo_state(p.root) == before
        assert code == EXIT_BUILD_FAILURE
        fix.assert_not_awaited()
        assert _git(p.dir, "rev-parse", "HEAD") == head  # nothing was reset
        saved = BuildState.load(p.config.state_file_path()).tdd.blocks[0]
        assert saved.fix_pass is not None  # the next resume tries the pass again
        assert saved.status == BlockStatus.FAILED and saved.resumable is True
        assert (
            "Did not resume the fix on pass"
            in p.config.progress_file_path().read_text()
        )

    @pytest.mark.asyncio
    async def test_row_F2_a_file_the_user_added_after_the_stop_is_refused(
        self, project
    ):
        p = project
        await self._kill_after_fix_agent(p)
        head = _git(p.dir, "rev-parse", "HEAD")
        (p.dir / "mine.py").write_text("the user's work\n")
        before = repo_state(p.root)

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head, before)
        assert (p.dir / "mine.py").read_text() == "the user's work\n"
        assert (p.dir / "app.py").read_text() == "half-applied\n"

        # Once the user moves their work away, the resume rolls back and reruns.
        (p.dir / "mine.py").unlink()
        code, fix = await self._resume(p, [_passing()])
        assert code == EXIT_SUCCESS
        assert fix.await_count == 1
        assert (p.dir / "app.py").read_text() == FIXED

    @pytest.mark.asyncio
    async def test_row_F2_an_edit_to_a_file_the_fix_agent_left_is_refused(
        self, project
    ):
        p = project
        await self._kill_after_fix_agent(p)
        head = _git(p.dir, "rev-parse", "HEAD")
        (p.dir / "app.py").write_text("half-applied\n# and the user's line\n")
        before = repo_state(p.root)

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head, before)
        assert (p.dir / "app.py").read_text() == "half-applied\n# and the user's line\n"

    @pytest.mark.asyncio
    async def test_row_F2_a_commit_added_after_the_stop_is_refused(self, project):
        p = project
        await self._kill_after_fix_agent(p)
        (p.dir / "app.py").write_text("the user's commit\n")
        _git(p.dir, "commit", "-qam", "user")
        head = _git(p.dir, "rev-parse", "HEAD")
        before = repo_state(p.root)

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head, before)
        assert (p.dir / "app.py").read_text() == "the user's commit\n"

    @pytest.mark.asyncio
    async def test_row_F1_edits_left_by_a_fix_agent_killed_mid_edit_are_refused(
        self, project
    ):
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
        assert (
            BuildState.load(p.config.state_file_path()).tdd.blocks[0].fix_pass.step
            == "fix_agent"
        )
        head = _git(p.dir, "rev-parse", "HEAD")
        before = repo_state(p.root)

        code, fix = await self._resume(p)

        self._assert_refused(p, code, fix, head, before)
        assert (p.dir / "app.py").read_text() == "half-applied\n"


# --- Stop-and-resume rows (plan-buildme-discard-safety-2026-10-13.md) ------
#
# Each test stops the fix on pass at its row's point, applies the row's
# perturbation, and resumes. A resume that proceeds is compared with the same
# block run in a second project that never stopped (`assert_same_as_unstopped`);
# a refusal must leave the repo exactly as it was (`repo_state`).

import asyncio

from tests.row_helpers import assert_same_as_unstopped, repo_state, run_killed_at

OUTSIDE = "other/notes.txt"


class Killed(BaseException):
    """A stop no handler on the fix-on-pass path catches, like a kill."""


def _edit_outside(root: Path, staged: bool) -> None:
    (root / OUTSIDE).write_text("v2 user edit\n")
    if staged:
        _git(root, "add", OUTSIDE)


async def _run_unstopped(p, fixer=None) -> int:
    """The block with one accepted finding, its fix on pass kept, and the
    engine run to the end, never stopped."""
    stack, _, _ = _engine(
        [_passing([_finding()]), _passing()], fixer or _fixer(p.dir, {"app.py": FIXED})
    )
    with stack:
        assert await _run_integration_and_review(p.block, p.config, p.state, p.progress)
    code, _ = await TestFixOnPass._resume(p)
    return code


async def _stop_at(p, point: str) -> None:
    """Stop the fix on pass at *point*: `fix_agent` (the agent dies before it
    writes anything, so no record) or `review` (after the agent's edit, with
    the record taken at `tests` and `review`)."""
    if point == "review":
        await TestFixOnPass._kill_after_fix_agent(p)
    else:

        def dying_fixer(**kwargs):
            raise Killed()

        stack, _, _ = _engine([_passing([_finding()])], dying_fixer)
        with stack, pytest.raises(Killed):
            await _run_integration_and_review(p.block, p.config, p.state, p.progress)
    assert (
        BuildState.load(p.config.state_file_path()).tdd.blocks[0].fix_pass.step == point
    )


class TestFixOnPassRows:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
    @pytest.mark.parametrize("point", ["fix_agent", "review"])
    async def test_row_F3_outside_edit_survives_a_resumed_fix_pass(
        self, project_in_subdir, tmp_path, point, staged
    ):
        clean = _make_project(tmp_path / "unstopped", subdir=True)
        _edit_outside(clean.root, staged)
        clean_code = await _run_unstopped(clean)

        p = project_in_subdir
        await _stop_at(p, point)
        _edit_outside(p.root, staged)

        code, fix = await TestFixOnPass._resume(p, [_passing()])

        assert code == clean_code == EXIT_SUCCESS
        assert fix.await_count == 1
        assert (p.root / OUTSIDE).read_text() == "v2 user edit\n"
        assert (
            OUTSIDE in _git(p.root, "diff", "--cached", "--name-only").split()
        ) is staged
        assert_same_as_unstopped(p.root, clean.root)

    @pytest.mark.asyncio
    async def test_row_F1_a_clean_stop_at_the_fix_agent_ends_as_an_unstopped_run(
        self, project, tmp_path
    ):
        clean = _make_project(tmp_path / "unstopped")
        clean_code = await _run_unstopped(clean)

        p = project
        await _stop_at(p, "fix_agent")
        code, fix = await TestFixOnPass._resume(p, [_passing()])

        assert code == clean_code == EXIT_SUCCESS
        assert fix.await_count == 1
        assert_same_as_unstopped(p.root, clean.root)

    @staticmethod
    def _killed_at_the_pass_tests(p):
        """Patch `integration_gate` so its call inside the fix on pass (the
        second: the first is the block's own gate, before the pass) raises
        Killed, as a kill while the suite runs would."""
        from build_pipeline import tdd_engine

        real, calls = tdd_engine.integration_gate, []

        def gate(*a, **k):
            calls.append(a)
            if len(calls) == 2:
                raise Killed()
            return real(*a, **k)

        return patch("build_pipeline.tdd_engine.integration_gate", side_effect=gate)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_stop", ["review", "tests"])
    async def test_row_F4_a_pass_stopped_twice_ends_as_an_unstopped_run(
        self, project, tmp_path, first_stop
    ):
        clean = _make_project(tmp_path / "unstopped")
        clean_code = await _run_unstopped(clean)

        p = project
        if first_stop == "review":
            await TestFixOnPass._kill_after_fix_agent(p)
        else:
            stack, _, _ = _engine(
                [_passing([_finding()])], _fixer(p.dir, {"app.py": "first try\n"})
            )
            with stack, self._killed_at_the_pass_tests(p), pytest.raises(Killed):
                await _run_integration_and_review(
                    p.block, p.config, p.state, p.progress
                )
        assert (
            BuildState.load(p.config.state_file_path()).tdd.blocks[0].fix_pass.step
            == first_stop
        )

        # The rerun pass stops at `review` again, with different bytes from its fix agent.
        with pytest.raises(Killed):
            await TestFixOnPass._resume(
                p, [Killed()], _fixer(p.dir, {"app.py": "second try\n"})
            )
        assert (
            BuildState.load(p.config.state_file_path()).tdd.blocks[0].fix_pass.step
            == "review"
        )

        code, fix = await TestFixOnPass._resume(p, [_passing()])

        assert code == clean_code == EXIT_SUCCESS
        assert fix.await_count == 1
        assert len(_rounds(p)[1]["earlier_fixes"]) == 2
        assert_same_as_unstopped(p.root, clean.root)

    # Rows D6e and D7c: a fix-on-pass rollback stopped inside `discard_to`,
    # after its soft reset (`restore`) or after its restore (`clean`). SIGKILL
    # only: no handler on these paths catches BaseException, so a Ctrl-C there
    # ends the same way.

    EXTRA_ROUND = "specs/changes/test/reviews/block-01-round-3.json"

    @staticmethod
    def _fix_and_new_file(p):
        return _fixer(p.dir, {"app.py": FIXED, "helper.py": "h = 1\n"})

    def _stop_in_rollback(self, p, rollback: str, git_command: str):
        """Stop a rollback at *git_command*. `resume`: the pass is stopped at
        `review`, and the resume's rollback (`_resume_fix_on_pass`) is killed.
        `not_kept`: the review after the fix fails, and the rollback of the
        fix not kept is killed. Returns the reviews the next resume needs."""
        if rollback == "resume":
            stack, _, _ = _engine(
                [_passing([_finding()]), Killed()], self._fix_and_new_file(p)
            )
            with stack, pytest.raises(Killed):
                asyncio.run(
                    _run_integration_and_review(p.block, p.config, p.state, p.progress)
                )
            run_killed_at(
                git_command, lambda: asyncio.run(TestFixOnPass._resume(p, [_passing()]))
            )
            return [_passing()]

        async def not_kept():
            stack, _, _ = _engine(
                [_passing([_finding()]), _failing()], self._fix_and_new_file(p)
            )
            with stack:
                await _run_integration_and_review(
                    p.block, p.config, p.state, p.progress
                )

        run_killed_at(git_command, lambda: asyncio.run(not_kept()))
        return [_failing()]

    def _unstopped(self, base, rollback: str) -> tuple[int, Path]:
        clean = _make_project(base)
        second = _passing() if rollback == "resume" else _failing()
        stack, _, _ = _engine(
            [_passing([_finding()]), second], self._fix_and_new_file(clean)
        )
        with stack:
            assert asyncio.run(
                _run_integration_and_review(
                    clean.block, clean.config, clean.state, clean.progress
                )
            )
        code, _ = asyncio.run(TestFixOnPass._resume(clean))
        return code, clean.root

    def _rerun_after_a_stop_in_rollback(self, p, base, rollback: str, git_command: str):
        """Stop the rollback, run the same block unstopped under *base*, and
        resume. Returns (stopped root, unstopped root, extra files)."""
        reviews = self._stop_in_rollback(p, rollback, git_command)
        clean_code, clean_root = self._unstopped(base, rollback)

        code, fix = asyncio.run(
            TestFixOnPass._resume(p, reviews, self._fix_and_new_file(p))
        )

        assert code == clean_code == EXIT_SUCCESS
        assert fix.await_count == 1
        # The rerun's review after a fix not kept writes one more round record.
        extra = (self.EXTRA_ROUND,) if rollback == "not_kept" else ()
        return p.root, clean_root, extra

    @pytest.mark.parametrize("rollback", ["resume", "not_kept"])
    def test_row_D6e_fix_pass_rollback_killed_mid_discard_reruns(
        self, project, tmp_path, rollback
    ):
        stopped, clean, extra = self._rerun_after_a_stop_in_rollback(
            project, tmp_path / "unstopped", rollback, "restore"
        )
        assert_same_as_unstopped(stopped, clean, extra_files=extra)

    @pytest.mark.parametrize("rollback", ["resume", "not_kept"])
    def test_row_D7c_fix_pass_rollback_killed_before_clean_reruns(
        self, project, tmp_path, rollback
    ):
        stopped, clean, extra = self._rerun_after_a_stop_in_rollback(
            project, tmp_path / "unstopped", rollback, "clean"
        )
        assert_same_as_unstopped(stopped, clean, extra_files=extra)

    def test_row_D6e_fix_pass_with_an_agent_commit_refuses(self, project):
        p = project

        def committing_fixer(**kwargs):
            (p.dir / "app.py").write_text(FIXED)
            _git(p.dir, "commit", "-qam", "the fix agent's commit")
            return AgentResult(success=True, output="fixed")

        stack, _, _ = _engine([_passing([_finding()]), Killed()], committing_fixer)
        with stack, pytest.raises(Killed):
            asyncio.run(
                _run_integration_and_review(p.block, p.config, p.state, p.progress)
            )
        run_killed_at(
            "restore", lambda: asyncio.run(TestFixOnPass._resume(p, [_passing()]))
        )
        head, before = _git(p.dir, "rev-parse", "HEAD"), repo_state(p.root)

        code, fix = asyncio.run(TestFixOnPass._resume(p, [_passing()]))

        TestFixOnPass()._assert_refused(p, code, fix, head, before)


# --- Item 12: the fix agent looks for the same mistake ---------------------


class TestFixAgentSelfCheck:
    @pytest.mark.parametrize(
        "reviews",
        [
            pytest.param([_failing([_finding()]), _passing()], id="failing-score"),
            pytest.param([_passing([_finding()]), _passing()], id="fix-on-pass"),
        ],
    )
    @pytest.mark.asyncio
    async def test_fix_brief_asks_for_the_same_mistake_elsewhere_and_a_diff_reread(
        self,
        project,
        reviews,
    ):
        p = project
        stack, _, fix = _engine(reviews, _fixer(p.dir, {"app.py": FIXED}))
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        brief = fix.await_args.kwargs["block_description"]
        assert "off-by-one in add()" in brief
        assert "other branches of the same function" in brief
        assert "git diff" in brief
        assert "SEARCHED:" in brief

    @pytest.mark.asyncio
    async def test_a_missing_searched_section_gives_null_and_a_warning(
        self, project, caplog
    ):
        p = project
        stack, _, _ = _engine(
            [_failing([_finding()]), _passing()],
            _fixer(p.dir, {"app.py": FIXED}, "fixed, no section"),
        )
        with (
            stack,
            caplog.at_level(logging.WARNING, logger="build_pipeline.tdd_engine"),
        ):
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

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
            block_number=1,
            block_name="B",
            role="implementer",
        )
        assert note.surprises == "the spec said ints"


# --- PUBLISH hands the unfixed findings to the pr skill --------------------


class TestUnfixedFindingsSection:
    def test_unfixed_findings_file_is_a_pr_section(self, tmp_path):
        assert (
            orchestrator.unfixed_findings_section
            in orchestrator.PRE_PUBLISH_CONTRIBUTORS
        )
        assert orchestrator.unfixed_findings_section(None, None, tmp_path) is None
        block = BlockInfo(number=2, name="Parser", description="d")
        path = review_rounds.append_unfixed(
            tmp_path, block, [_finding()], "fix not kept"
        )
        assert orchestrator.unfixed_findings_section(None, None, tmp_path) == path
        text = path.read_text()
        assert text.splitlines()[0].startswith("## ")
        assert "Block 2 (Parser)" in text and "app.py:2" in text

    def test_appending_keeps_earlier_entries(self, tmp_path):
        block = BlockInfo(number=1, name="A", description="d")
        review_rounds.append_unfixed(tmp_path, block, [_finding()], "fix not kept")
        review_rounds.append_unfixed(
            tmp_path, block, [_finding("Minor", "second")], "not attempted"
        )
        text = (tmp_path / "unfixed-findings.md").read_text()
        assert text.count("## ") == 1
        assert "off-by-one in add()" in text and "second" in text
        assert "not attempted" in text


@pytest.mark.asyncio
async def test_fix_on_pass_budget_denial_remains_a_stop(project):
    from build_pipeline import budget, tdd_engine

    p = project
    p.block.review_scores = _passing([_finding()])
    budget.configure(max_usd=0.1)
    budget.get_budget().record(SimpleNamespace(cost_usd=0.2))
    with pytest.raises(budget.BudgetExceededError):
        await tdd_engine._fix_on_pass(p.block, p.config, p.state, p.progress, 0, None)
    assert p.block.fix_pass is None


# --- PR B (plan-buildme-review-convergence-2026-10-20.md) -------------------
#
# B1: every accepted finding gets a stable id in its round record, and the
# loop reads the open findings of the last round back from disk.

from build_pipeline.models import PriorAssessment


def _record(change_dir: Path, block: BlockInfo, review: ReviewResult, **memory) -> Path:
    return review_rounds.record_round(
        change_dir,
        block,
        head_sha="abc1234",
        review=review,
        gate_passed=False,
        gate_reason="low",
        weighted_average=2.0,
        **memory,
    )


class TestFindingIds:
    def test_record_round_gives_each_accepted_finding_its_id(self, tmp_path):
        block = BlockInfo(number=3, name="B", description="d")
        _record(tmp_path, block, _failing())
        path = _record(
            tmp_path, block, _failing([_finding(), _finding("Minor", "second")])
        )
        record = json.loads(path.read_text())
        assert [f["finding_id"] for f in record["accepted"]] == [
            "B03-R2-F1",
            "B03-R2-F2",
        ]

    def test_an_id_a_finding_already_has_is_kept(self, tmp_path):
        block = BlockInfo(number=1, name="B", description="d")
        carried = _finding().model_copy(update={"finding_id": "B01-R1-F1"})
        path = _record(tmp_path, block, _failing([carried, _finding("Minor", "new")]))
        assert [f["finding_id"] for f in json.loads(path.read_text())["accepted"]] == [
            "B01-R1-F1",
            "B01-R1-F2",
        ]

    def test_a_round_record_holds_the_new_fields(self, tmp_path):
        block = BlockInfo(number=1, name="B", description="d")
        path = _record(tmp_path, block, _failing([_finding()]))
        record = json.loads(path.read_text())
        # Round 1: every accepted finding is open, nothing was assessed or
        # left outside the fix's scope.
        assert [f["finding_id"] for f in record["open_findings"]] == ["B01-R1-F1"]
        assert record["prior_assessments"] == []
        assert record["outside_fix_scope"] == []
        assert record["accepted"][0]["defect_class"] == ""
        assert record["accepted"][0]["other_locations"] == []

    def test_open_and_outside_lists_are_written_with_the_records_ids(self, tmp_path):
        block = BlockInfo(number=1, name="B", description="d")
        carried = _finding("Major", "still open").model_copy(
            update={"finding_id": "B01-R1-F1"}
        )
        review = _failing(
            [_finding("Major", "new in scope"), _finding("Minor", "elsewhere")]
        )
        _record(tmp_path, block, _failing())
        path = _record(
            tmp_path,
            block,
            review,
            carried_open=[carried],
            new_open=[0, 1],
            outside=[1],
            prior_assessments=[
                PriorAssessment(finding_id="B01-R1-F1", status="still_present")
            ],
        )
        record = json.loads(path.read_text())
        # The carried finding keeps its id; the new ones get this round's,
        # and the one outside the fix's scope is open too.
        assert [f["finding_id"] for f in record["open_findings"]] == [
            "B01-R1-F1",
            "B01-R2-F1",
            "B01-R2-F2",
        ]
        assert [f["claim"] for f in record["outside_fix_scope"]] == ["elsewhere"]
        assert (
            record["outside_fix_scope"][0]["finding_id"]
            == record["accepted"][1]["finding_id"]
        )
        assert record["prior_assessments"] == [
            {"finding_id": "B01-R1-F1", "status": "still_present", "evidence": ""}
        ]


class TestOpenState:
    def test_no_records_means_round_one(self, tmp_path):
        assert review_rounds.open_state(tmp_path, 1) == ([], [], None)

    def test_an_old_format_record_falls_back_to_accepted_with_ids(self, tmp_path):
        reviews = tmp_path / "reviews"
        reviews.mkdir()
        old = {
            "kind": "review",
            "round": 2,
            "head_sha": "abc",
            "accepted": [_finding().model_dump(mode="json")],
            "rejected": [
                {
                    **_finding("Minor", "nope").model_dump(mode="json"),
                    "judge_reason": "the spec says so",
                }
            ],
        }
        (reviews / "block-01-round-2.json").write_text(json.dumps(old))
        open_, rejected, last = review_rounds.open_state(tmp_path, 1)
        assert [(f.finding_id, f.claim) for f in open_] == [
            ("B01-R2-F1", "off-by-one in add()")
        ]
        assert [(r["finding_id"], r["judge_reason"]) for r in rejected] == [
            ("B01-R2-X1", "the spec says so")
        ]
        assert last["round"] == 2

    def test_a_new_format_record_gives_its_open_findings(self, tmp_path):
        block = BlockInfo(number=1, name="B", description="d")
        carried = _finding("Major", "carried").model_copy(
            update={"finding_id": "B01-R1-F1"}
        )
        _record(tmp_path, block, _failing([_finding("Major", "first")]))
        _record(
            tmp_path,
            block,
            _failing([_finding("Minor", "dup")]),
            carried_open=[carried],
            new_open=[],
            outside=[],
        )
        open_, _, last = review_rounds.open_state(tmp_path, 1)
        assert [(f.finding_id, f.claim) for f in open_] == [("B01-R1-F1", "carried")]
        assert last["round"] == 2

    def test_rejected_is_the_union_across_the_blocks_review_records(self, tmp_path):
        block = BlockInfo(number=1, name="B", description="d")
        for claim in ("one", "two"):
            review = _failing().model_copy(
                update={
                    "rejected_findings": [
                        _finding("Minor", claim).model_copy(
                            update={"evidence": f"e\nREJECTED: not {claim}"}
                        )
                    ]
                }
            )
            _record(tmp_path, block, review)
        _, rejected, _ = review_rounds.open_state(tmp_path, 1)
        assert [(r["finding_id"], r["claim"], r["judge_reason"]) for r in rejected] == [
            ("B01-R1-X1", "one", "not one"),
            ("B01-R2-X1", "two", "not two"),
        ]

    def test_only_this_blocks_review_records_count(self, tmp_path):
        block = BlockInfo(number=1, name="B", description="d")
        other = BlockInfo(number=2, name="C", description="d")
        _record(tmp_path, block, _failing([_finding("Major", "review round")]))
        review_rounds.record_round(
            tmp_path,
            block,
            head_sha="x",
            review=_passing([_finding("Minor", "fop")]),
            gate_passed=True,
            gate_reason="ok",
            weighted_average=5.0,
            kind="fix_on_pass_review",
        )
        _record(tmp_path, other, _failing([_finding("Major", "other block")]))
        (tmp_path / "reviews" / "block-01-round-9.json").write_text("{not json")
        open_, _, last = review_rounds.open_state(tmp_path, 1)
        assert [f.claim for f in open_] == ["review round"]
        assert last["round"] == 1


# B2-B4: from round 2 on, the loop remembers the open findings, merges
# restatements, keeps new findings within the last fix's reach, escalates
# the last fix, and names each defect's kind and other locations.


def _memory_engine(reviews, fixer, adjudications):
    """`_engine` with one adjudication result (or exception) per round.
    Returns (stack, review_mock, fixer_mock, judge_mock)."""
    stack = ExitStack()
    review = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine._run_quality_review",
            new_callable=AsyncMock,
            side_effect=list(reviews),
        )
    )
    fix = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.run_implementer",
            new_callable=AsyncMock,
            side_effect=fixer,
        )
    )
    judge = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.llm_call_structured",
            new_callable=AsyncMock,
            side_effect=list(adjudications),
        )
    )
    return stack, review, fix, judge


def _assess(review: ReviewResult, *pairs) -> ReviewResult:
    return review.model_copy(
        update={
            "prior_assessments": [
                PriorAssessment(finding_id=fid, status=status) for fid, status in pairs
            ]
        }
    )


def _verdicts(*verdicts) -> FindingAdjudication:
    return FindingAdjudication(verdicts=list(verdicts))


OFF_BY_ONE = "off-by-one in add()"
NAMES = "names are unclear"
NEW = "sub() is missing"


async def _two_rounds(
    p,
    round2: ReviewResult,
    verdict2: FindingAdjudication | Exception,
    round1: ReviewResult | None = None,
    verdict1: FindingAdjudication | None = None,
    third=None,
):
    """Round 1 fails with OFF_BY_ONE accepted and NAMES rejected, a fix
    lands, round 2 is *round2* judged by *verdict2*; then *third* (passing
    by default) ends the loop, with no fix on pass. Returns (passed, records,
    review, fix, judge)."""
    p.config.fix_on_pass = False
    round1 = round1 or _failing([_finding(), _finding("Minor", NAMES)])
    verdict1 = verdict1 or _verdicts(
        FindingVerdict(index=0, accepted=True),
        FindingVerdict(index=1, accepted=False, reason="the spec names them"),
    )
    reviews = [round1, round2] + ([] if round2.passed else [third or _passing()])
    judges = [verdict1, verdict2, FindingAdjudication()]
    stack, review, fix, judge = _memory_engine(
        reviews, _fixer(p.dir, {"app.py": FIXED}), judges
    )
    with stack:
        passed = await _run_integration_and_review(
            p.block, p.config, p.state, p.progress
        )
    return passed, _rounds(p), review, fix, judge


def _ids(entries) -> list[str]:
    return [e["finding_id"] for e in entries]


def _brief(record: dict) -> str:
    """A round's fix brief without the escalation's finding history before
    it (round 2's fix is the loop's last, so it is escalated)."""
    return "Fix review issues:" + record["fix_brief"].split("Fix review issues:", 1)[1]


class TestRoundMemory:
    @pytest.mark.asyncio
    async def test_round_one_is_reviewed_and_judged_as_today(self, project):
        from build_pipeline.prompts.review import FINDING_ADJUDICATION_PROMPT

        p = project
        _, rounds, review, _, judge = await _two_rounds(
            p, _passing(), FindingAdjudication()
        )
        assert review.await_args_list[0].kwargs == {}
        prompt = judge.await_args_list[0].args[0]
        assert "Findings still open from earlier rounds" not in prompt
        assert "duplicate_of" not in prompt and "in_fix_scope" not in prompt
        assert prompt.endswith(
            FINDING_ADJUDICATION_PROMPT.split("{round_memory}", 1)[1].format()
        )
        assert _ids(rounds[1]["open_findings"]) == ["B01-R1-F1"]

    @pytest.mark.asyncio
    async def test_round_two_reviewer_is_given_the_open_findings(self, project):
        p = project
        _, _, review, _, _ = await _two_rounds(p, _passing(), FindingAdjudication())
        given = review.await_args_list[1].kwargs["open_findings"]
        assert [(f.finding_id, f.claim) for f in given] == [("B01-R1-F1", OFF_BY_ONE)]

    @pytest.mark.asyncio
    async def test_round_two_adjudicator_sees_open_rejected_and_the_scope_diff(
        self, project
    ):
        p = project
        await_ = await _two_rounds(
            p, _failing([_finding("Major", NEW)]), FindingAdjudication()
        )
        prompt = await_[4].await_args_list[1].args[0]
        assert "B01-R1-F1" in prompt and OFF_BY_ONE in prompt
        assert "B01-R1-X1" in prompt and "the spec names them" in prompt
        # The scope diff is everything since round 1's head: the fix commit.
        scope = prompt.split("## Changes since the previous review", 1)[1]
        assert "-    return a + b + 1" in scope and "+    return a + b" in scope
        assert "block-01-round-1.json" not in scope
        assert "duplicate_of" in prompt and "in_fix_scope" in prompt

    @pytest.mark.asyncio
    async def test_a_first_review_of_uncommitted_code_gives_the_next_round_no_scope_diff(
        self, project
    ):
        p = project
        # Code the integration fixer could have written: there when round 1 reads the tree, not in HEAD.
        (p.dir / "app.py").write_text(BUGGY + "\n# uncommitted\n")
        await_ = await _two_rounds(
            p, _failing([_finding("Major", NEW)]), FindingAdjudication()
        )
        first = await_[1][1]
        assert (
            first["reviewed_sha"] is None
            and "uncommitted" in first["reviewed_sha_unknown"]
        )
        prompt = await_[4].await_args_list[1].args[0]
        assert "## Changes since the previous review" not in prompt

    @pytest.mark.asyncio
    async def test_uncommitted_bookkeeping_and_round_records_leave_the_reviewed_commit_known(
        self, project
    ):
        p = project
        head = _git(p.dir, "rev-parse", "HEAD")
        (p.config.change_dir / "build-progress.md").write_text("progress\n")
        (p.config.change_dir / "reviews").mkdir()
        (p.config.change_dir / "reviews" / "block-09-round-1.json").write_text("{}\n")
        _, rounds, *_ = await _two_rounds(
            p, _failing([_finding("Major", NEW)]), FindingAdjudication()
        )
        assert rounds[1]["reviewed_sha"] == head

    def test_a_record_with_no_reviewed_sha_gives_no_scope_diff(self, project):
        from build_pipeline.tdd_engine import _scope_diff

        p = project
        head = _git(p.dir, "rev-parse", "HEAD")
        assert (
            _scope_diff(p.config, {"head_sha": head}) is None
        )  # written before 0.96.6
        assert _scope_diff(p.config, {"head_sha": head, "reviewed_sha": None}) is None
        assert _scope_diff(p.config, {"head_sha": head, "reviewed_sha": head}) == ""

    @pytest.mark.asyncio
    async def test_a_missing_assessment_is_cannot_verify_and_stays_open(self, project):
        p = project
        _, rounds, *_ = await _two_rounds(
            p, _failing([_finding("Major", NEW)]), FindingAdjudication()
        )
        assert rounds[2]["prior_assessments"] == [
            {"finding_id": "B01-R1-F1", "status": "cannot_verify", "evidence": ""}
        ]
        assert _ids(rounds[2]["open_findings"]) == ["B01-R1-F1", "B01-R2-F1"]

    @pytest.mark.asyncio
    async def test_a_resolved_finding_leaves_the_open_list_and_the_brief(self, project):
        p = project
        _, rounds, *_ = await _two_rounds(
            p,
            _assess(_failing([_finding("Major", NEW)]), ("B01-R1-F1", "resolved")),
            FindingAdjudication(),
        )
        assert _ids(rounds[2]["open_findings"]) == ["B01-R2-F1"]
        assert OFF_BY_ONE not in _brief(rounds[2])
        assert NEW in _brief(rounds[2])

    @pytest.mark.asyncio
    async def test_a_finding_still_present_is_in_the_brief_with_its_status(
        self, project
    ):
        p = project
        _, rounds, *_ = await _two_rounds(
            p,
            _assess(_failing(), ("B01-R1-F1", "still_present")),
            FindingAdjudication(),
        )
        brief = _brief(rounds[2])
        section = brief.split("## Findings still open from earlier rounds", 1)[1]
        assert (
            "B01-R1-F1" in section
            and OFF_BY_ONE in section
            and "still_present" in section
        )

    @pytest.mark.asyncio
    async def test_a_restatement_of_an_open_finding_is_merged_into_it(self, project):
        p = project
        restated = _finding("Major", "add() returns one too many").model_copy(
            update={"evidence": "line 2 adds 1"}
        )
        _, rounds, *_ = await _two_rounds(
            p,
            _failing([restated]),
            _verdicts(FindingVerdict(index=0, accepted=True, duplicate_of="B01-R1-F1")),
        )
        assert rounds[2]["accepted"][0]["duplicate_of"] == "B01-R1-F1"
        assert _ids(rounds[2]["open_findings"]) == ["B01-R1-F1"]
        assert "line 2 adds 1" in rounds[2]["open_findings"][0]["evidence"]
        adjudicated = _brief(rounds[2]).split("## Adjudicated findings", 1)
        assert len(adjudicated) == 1 or "one too many" not in adjudicated[1]

    @pytest.mark.asyncio
    async def test_a_restatement_of_a_rejected_finding_stays_accepted(self, project):
        p = project
        _, rounds, *_ = await _two_rounds(
            p,
            _failing([_finding("Minor", "the names are still unclear")]),
            _verdicts(FindingVerdict(index=0, accepted=True, duplicate_of="B01-R1-X1")),
        )
        entry = rounds[2]["accepted"][0]
        assert entry["duplicate_of"] == "B01-R1-X1"
        assert entry["earlier_judge_reason"] == "the spec names them"
        assert "B01-R2-F1" in _ids(rounds[2]["open_findings"])
        assert "the names are still unclear" in _brief(rounds[2])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "duplicate_of", [None, "B07-R9-F9"], ids=["missing", "unknown"]
    )
    async def test_no_known_duplicate_means_a_new_finding(self, project, duplicate_of):
        p = project
        _, rounds, *_ = await _two_rounds(
            p,
            _failing([_finding("Major", NEW)]),
            _verdicts(
                FindingVerdict(index=0, accepted=True, duplicate_of=duplicate_of)
            ),
        )
        assert rounds[2]["accepted"][0]["duplicate_of"] is None
        assert "B01-R2-F1" in _ids(rounds[2]["open_findings"])
        assert NEW in _brief(rounds[2])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severity", ["Minor", "Major", "Critical"])
    async def test_a_finding_outside_the_fix_scope_is_labelled_and_fixed(
        self, project, severity
    ):
        p = project
        _, rounds, *_ = await _two_rounds(
            p,
            _failing([_finding(severity, NEW)]),
            _verdicts(FindingVerdict(index=0, accepted=True, in_fix_scope=False)),
        )
        assert [f["claim"] for f in rounds[2]["outside_fix_scope"]] == [NEW]
        assert "B01-R2-F1" in _ids(rounds[2]["open_findings"])
        assert NEW in _brief(rounds[2])
        assert NEW not in _unfixed(p)

    @pytest.mark.asyncio
    async def test_a_missing_scope_verdict_means_in_scope(self, project):
        p = project
        _, rounds, *_ = await _two_rounds(
            p,
            _failing([_finding("Minor", NEW)]),
            _verdicts(FindingVerdict(index=0, accepted=True)),
        )
        assert rounds[2]["outside_fix_scope"] == []
        assert NEW in _brief(rounds[2])

    @pytest.mark.asyncio
    async def test_an_adjudicator_error_in_round_two_keeps_everything_open(
        self, project
    ):
        p = project
        _, rounds, *_ = await _two_rounds(
            p, _failing([_finding("Minor", NEW)]), RuntimeError("judge down")
        )
        assert _ids(rounds[2]["open_findings"]) == ["B01-R1-F1", "B01-R2-F1"]
        assert rounds[2]["outside_fix_scope"] == []

    @pytest.mark.asyncio
    async def test_a_reviewer_cannot_set_ids_or_scope_itself(self, project):
        p = project
        sneaky = _finding("Minor", NEW).model_copy(
            update={
                "finding_id": "B01-R1-F1",
                "duplicate_of": "B01-R1-F1",
                "in_fix_scope": False,
            }
        )
        _, rounds, *_ = await _two_rounds(p, _failing([sneaky]), FindingAdjudication())
        entry = rounds[2]["accepted"][0]
        assert entry["finding_id"] == "B01-R2-F1"
        assert entry["duplicate_of"] is None and entry["in_fix_scope"] is None
        assert NEW in _brief(rounds[2])

    @pytest.mark.asyncio
    async def test_the_fix_on_pass_works_on_the_open_set_and_the_out_of_scope(
        self, project
    ):
        p = project
        p.config.fix_on_pass = True
        reviews = [
            _failing([_finding()]),
            _passing([_finding("Minor", NEW)]),
            _passing(),
        ]
        stack, _, fix, _ = _memory_engine(
            reviews,
            _fixer(p.dir, {"app.py": FIXED}),
            [
                FindingAdjudication(),
                _verdicts(FindingVerdict(index=0, accepted=True, in_fix_scope=False)),
                FindingAdjudication(),
            ],
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )
        on_pass = fix.await_args_list[1].kwargs["block_description"]
        assert OFF_BY_ONE in on_pass and NEW in on_pass

    @pytest.mark.asyncio
    async def test_reviewer_section_lists_open_findings_only_from_round_two(
        self, project
    ):
        from build_pipeline.tdd_engine import _run_quality_review

        p = project
        carried = _finding().model_copy(update={"finding_id": "B01-R1-F1"})
        with patch(
            "build_pipeline.tdd_engine.run_code_review",
            new_callable=AsyncMock,
            return_value=_passing(),
        ) as code_review:
            await _run_quality_review(p.block, p.config)
            await _run_quality_review(p.block, p.config, open_findings=[carried])
        first, second = (
            c.kwargs["implementer_report"] for c in code_review.await_args_list
        )
        assert "Findings still open from earlier rounds" not in first
        section = second.split("### Findings still open from earlier rounds", 1)[1]
        assert "B01-R1-F1" in section and "[Major]" in section
        assert OFF_BY_ONE in section and "app.py:2" in section


class TestDefectClass:
    def test_findings_text_renders_class_and_other_locations(self):
        f = _finding().model_copy(
            update={
                "defect_class": "off-by-one",
                "other_locations": ["app.py:7", "lib.py:3"],
            }
        )
        text = ReviewResult(
            scores=[], findings=[f, _finding("Minor", NAMES)]
        ).findings_text()
        assert "class: off-by-one; also at: app.py:7, lib.py:3" in text
        assert text.count("class:") == 1

    @pytest.mark.asyncio
    async def test_the_brief_asks_for_every_location_with_a_test(self, project):
        p = project
        located = _finding().model_copy(update={"other_locations": ["app.py:9"]})
        p.config.fix_on_pass = False
        stack, _, fix = _engine(
            [_failing([located]), _passing()], _fixer(p.dir, {"app.py": FIXED})
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )
        brief = fix.await_args.kwargs["block_description"]
        assert "also at: app.py:9" in brief
        assert "Fix every listed location, and cover each one with a test." in brief


# B3: the last fix questions the approach, and an exhausted loop says which
# findings kept coming back.

from build_pipeline.gates import parse_recurring
from build_pipeline.prompts.implementation import REVIEW_FIX_ESCALATION

ESCALATION_HEAD = REVIEW_FIX_ESCALATION.split("\n", 1)[0]
RECURRING_REPORT = (
    "fixed\nRECURRING: B01-R1-F1, B01-R2-F1 — add() never checks its inputs\n"
    "SEARCHED:\n- add(): both branches\n"
)


async def _three_failing_rounds(p, reports=("fixed", RECURRING_REPORT)):
    """Three failing reviews, two fixes. Round 2 keeps B01-R1-F1 and adds
    one; round 3 keeps both. Returns (records, fix mock)."""
    p.config.review_model = "claude-haiku-4-5"
    reviews = [
        _failing([_finding()]),
        _assess(_failing([_finding("Major", NEW)]), ("B01-R1-F1", "still_present")),
        _assess(
            _failing(),
            ("B01-R1-F1", "still_present"),
            ("B01-R2-F1", "partially_resolved"),
        ),
    ]
    outputs = iter(reports)

    def fixer(**kwargs):
        (p.dir / "app.py").write_text(
            FIXED
            if kwargs["block_description"].startswith("Fix review")
            else "def add(a, b):\n    return b + a\n"
        )
        return AgentResult(success=True, output=next(outputs))

    stack, _, fix, _ = _memory_engine(reviews, fixer, [FindingAdjudication()] * 3)
    with stack:
        assert not await _run_integration_and_review(
            p.block, p.config, p.state, p.progress
        )
    return _rounds(p), fix


class TestEscalation:
    @pytest.mark.asyncio
    async def test_the_last_fix_runs_escalated_on_the_review_model(self, project):
        p = project
        rounds, fix = await _three_failing_rounds(p)
        first, last = (c.kwargs for c in fix.await_args_list)
        assert first["model"] == p.config.model_for("review_fix")
        assert (
            last["model"]
            == p.config.model_for("review_fix_escalated")
            == "claude-haiku-4-5"
        )
        assert last["effort"] == p.config.effort_for("review_fix_escalated")
        assert not first["block_description"].startswith(ESCALATION_HEAD)
        assert last["block_description"].startswith(ESCALATION_HEAD)

    @pytest.mark.asyncio
    async def test_the_escalation_lists_every_finding_with_its_history(self, project):
        p = project
        _, fix = await _three_failing_rounds(p)
        brief = fix.await_args_list[1].kwargs["block_description"]
        history = brief.split("Fix review issues:", 1)[0]
        assert f"B01-R1-F1 [Major] {OFF_BY_ONE}" in history
        assert "round 1: raised; round 2: still_present" in history
        assert f"B01-R2-F1 [Major] {NEW}" in history
        assert "RECURRING:" in history and "question" in history.lower()
        assert "Fix only the findings in the brief" in history
        # The ordinary brief still follows, self-check included.
        assert "SEARCHED:" in brief

    @pytest.mark.asyncio
    async def test_the_round_record_marks_the_escalated_fix(self, project):
        p = project
        rounds, _ = await _three_failing_rounds(p)
        assert "escalated" not in rounds[1]["fix"]
        assert rounds[2]["fix"]["escalated"] is True
        assert (
            rounds[2]["fix"]["recurring"]
            == "B01-R1-F1, B01-R2-F1 — add() never checks its inputs"
        )
        assert rounds[2]["fix"]["searched"] == ["add(): both branches"]
        assert rounds[2]["fix_brief"].startswith(ESCALATION_HEAD)

    @pytest.mark.asyncio
    async def test_a_missing_recurring_slot_is_null(self, project):
        p = project
        rounds, _ = await _three_failing_rounds(p, reports=("fixed", "fixed, no slot"))
        assert (
            rounds[2]["fix"]["escalated"] is True
            and rounds[2]["fix"]["recurring"] is None
        )

    @pytest.mark.asyncio
    async def test_exhaustion_names_the_findings_that_kept_coming_back(self, project):
        p = project
        await _three_failing_rounds(p)
        progress = p.progress.path.read_text()
        diagnosis = progress.split("DIAGNOSIS (Adder)", 1)[1]
        assert "B01-R1-F1" in diagnosis  # still_present in rounds 2 and 3
        assert "B01-R2-F1" not in diagnosis.split("RECURRING", 1)[0]  # only once
        assert "add() never checks its inputs" in diagnosis

    def test_recurring_is_parsed(self):
        assert parse_recurring("STATUS: DONE\nRECURRING: none\n") == "none"
        assert parse_recurring(
            "**RECURRING:** A, B — same cause\nSEARCHED:\n- x\n"
        ) == ("A, B — same cause")
        assert parse_recurring("STATUS: DONE") is None

    def test_searched_stops_at_a_recurring_slot(self):
        assert parse_searched("SEARCHED:\n- add()\nRECURRING: none\n") == ["add()"]


# --- Stop-and-resume rows S4-S13 (plan-buildme-review-convergence-2026-10-20.md)
#
# A stop is a `Killed` raised at the row's point. The "new process" loads the
# checkpoint from disk and runs the block's review loop again, which is what
# a resumed build reaches for a block stopped in review (it starts again at
# iteration 0). Each row is compared with the same block run in a second
# project that never stopped.

from contextlib import contextmanager

MEMORY_FIELDS = (
    "accepted",
    "rejected",
    "open_findings",
    "prior_assessments",
    "outside_fix_scope",
    "gate",
)


def _memory_of(record: dict) -> dict:
    return {k: record[k] for k in MEMORY_FIELDS}


@contextmanager
def _dies_at(target: str, call: int):
    """Patch *target* (a `review_rounds` function) so its *call*-th call
    raises Killed instead of running; the others run for real."""
    real = getattr(review_rounds, target)
    calls = []

    def wrapper(*a, **k):
        calls.append(a)
        if len(calls) == call:
            raise Killed()
        return real(*a, **k)

    with patch(f"build_pipeline.review_rounds.{target}", side_effect=wrapper):
        yield


def _round_one():
    return _failing([_finding()])


def _resolved_pass():
    """Round 1's finding resolved, and one new Note, so the adjudicator runs
    (it is not called for a review with no findings)."""
    return _assess(_passing([_finding("Note", "a note")]), ("B01-R1-F1", "resolved"))


async def _unstopped(
    base: Path, reviews=None, judges=None, fixer=None, review_model=None
):
    """The scenario run without a stop: round 1 fails on OFF_BY_ONE, the fix
    lands, round 2 calls it resolved and passes. No fix on pass."""
    clean = _make_project(base)
    clean.config.fix_on_pass = False
    clean.config.review_model = review_model
    reviews = reviews or [_round_one(), _resolved_pass()]
    stack, review, fix, judge = _memory_engine(
        reviews,
        fixer or _fixer(clean.dir, {"app.py": FIXED}),
        judges or [FindingAdjudication()] * len(reviews),
    )
    with stack:
        ok = await _run_integration_and_review(
            clean.block, clean.config, clean.state, clean.progress
        )
    return SimpleNamespace(p=clean, ok=ok, review=review, fix=fix, judge=judge)


async def _stopped(p, reviews, fixer=None, judges=None, kill=None):
    """Run the loop until it raises Killed."""
    p.config.fix_on_pass = False
    stack, *_ = _memory_engine(
        reviews,
        fixer or _fixer(p.dir, {"app.py": FIXED}),
        judges or [FindingAdjudication()] * len(reviews),
    )
    with stack, kill or ExitStack(), pytest.raises(Killed):
        await _run_integration_and_review(p.block, p.config, p.state, p.progress)


async def _resumed(p, reviews, fixer=None, judges=None):
    """The new process: the block and state as the checkpoint holds them."""
    state = BuildState.load(p.config.state_file_path())
    block = state.tdd.blocks[0]
    stack, review, fix, judge = _memory_engine(
        reviews,
        fixer or _fixer(p.dir, {"app.py": FIXED}),
        judges or [FindingAdjudication()] * len(reviews),
    )
    with stack:
        ok = await _run_integration_and_review(block, p.config, state, p.progress)
    return SimpleNamespace(ok=ok, block=block, review=review, fix=fix, judge=judge)


def _open_given(review_mock, call: int) -> list[str]:
    return [
        f.finding_id
        for f in review_mock.await_args_list[call].kwargs.get("open_findings", [])
    ]


def _scope_of(judge_mock, call: int) -> str:
    prompt = judge_mock.await_args_list[call].args[0]
    return prompt.split("## Changes since the previous review", 1)[1].split(
        "## Restatements", 1
    )[0]


def _state_without(p, *keys) -> dict:
    """The saved block, commit shas named by their place in the history
    (`row_helpers`, as the repo comparison names them), without *keys*."""
    from tests.row_helpers import _norm_json, _tokens

    block = (
        BuildState.load(p.config.state_file_path())
        .tdd.blocks[0]
        .model_dump(mode="json")
    )
    block = _norm_json(block, p.root, _tokens(p.root))
    return {k: v for k, v in block.items() if k not in keys}


def _rel_state(p) -> str:
    return p.config.state_file_path().relative_to(p.root).as_posix()


def _stop_before_the_fix(p):
    """Row S5's stop: round 1 recorded, killed as the fix agent starts."""

    def dying_fixer(**kwargs):
        raise Killed()

    return _stopped(p, [_round_one()], dying_fixer)


class TestReviewMemoryRows:
    @pytest.mark.asyncio
    async def test_review_memory_resume_S4_stop_before_record_round(
        self, project, tmp_path
    ):
        clean = await _unstopped(tmp_path / "unstopped")
        p = project
        await _stopped(
            p, [_round_one(), _resolved_pass()], kill=_dies_at("record_round", 2)
        )
        assert sorted(_rounds(p)) == [1]  # nothing new on disk

        resumed = await _resumed(p, [_resolved_pass()])

        assert resumed.ok is clean.ok is True
        assert (
            _open_given(resumed.review, 0)
            == _open_given(clean.review, 1)
            == ["B01-R1-F1"]
        )
        assert _rounds(p).keys() == _rounds(clean.p).keys() == {1, 2}
        assert _memory_of(_rounds(p)[2]) == _memory_of(_rounds(clean.p)[2])
        # Same records, commits and tree. The saved state differs in
        # review_iterations (the new process counts from 0) and in the fix's
        # report, which today's loop adds to implementer_report in memory
        # after the last checkpoint: both as today.
        assert_same_as_unstopped(p.root, clean.p.root, extra_files=(_rel_state(p),))
        assert _state_without(
            p, "review_iterations", "implementer_report"
        ) == _state_without(clean.p, "review_iterations", "implementer_report")

    @pytest.mark.asyncio
    async def test_review_memory_resume_S5_stop_after_record_round(
        self, project, tmp_path
    ):
        clean = await _unstopped(tmp_path / "unstopped")
        p = project
        await _stop_before_the_fix(p)
        first = _rounds(p)[1]
        assert first["fix"] is None and first["fix_brief"]
        assert _ids(first["open_findings"]) == ["B01-R1-F1"]

        resumed = await _resumed(p, [_resolved_pass()])

        assert resumed.ok is True
        # The first review of the new process reads record 1 as `last`.
        assert _open_given(resumed.review, 0) == _open_given(clean.review, 1)
        assert "(no changes since the previous review)" in _scope_of(resumed.judge, 0)
        # ...and writes record 2, with the memory an unstopped round 2 has.
        assert sorted(_rounds(p)) == [1, 2]
        assert _memory_of(_rounds(p)[2]) == _memory_of(_rounds(clean.p)[2])

    @pytest.mark.asyncio
    async def test_review_memory_resume_S6_stop_while_the_fix_runs(
        self, project, tmp_path
    ):
        clean = await _unstopped(tmp_path / "unstopped")
        p = project

        def dying_mid_edit(**kwargs):
            (p.dir / "app.py").write_text(FIXED)
            raise Killed()

        await _stopped(p, [_round_one()], dying_mid_edit)
        assert _git(p.dir, "status", "--porcelain", "app.py")  # uncommitted edit

        seen = []

        async def review(block, config, **kw):
            seen.append((p.dir / "app.py").read_text())
            return _resolved_pass()

        with (
            patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review),
            patch(
                "build_pipeline.tdd_engine.llm_call_structured",
                new_callable=AsyncMock,
                return_value=FindingAdjudication(),
            ),
        ):
            state = BuildState.load(p.config.state_file_path())
            assert await _run_integration_and_review(
                state.tdd.blocks[0], p.config, state, p.progress
            )
        assert seen == [FIXED]  # the edits were kept, as today
        assert _memory_of(_rounds(p)[2]) == _memory_of(_rounds(clean.p)[2])
        assert (p.dir / "app.py").read_text() == (clean.p.dir / "app.py").read_text()
        assert not _git(p.dir, "status", "--porcelain", "app.py")

    @pytest.mark.asyncio
    async def test_review_memory_resume_S7_stop_before_amend_fix(
        self, project, tmp_path
    ):
        clean = await _unstopped(tmp_path / "unstopped")
        p = project
        await _stopped(p, [_round_one()], kill=_dies_at("amend_fix", 1))
        first = _rounds(p)[1]
        assert first["fix"]["report"] == "fixed" and first["fix"]["commit"] is None

        resumed = await _resumed(p, [_resolved_pass()])

        # The scope diff runs from record 1's head, so it holds the fix
        # commit the record does not name: the same diff as unstopped.
        scope = _scope_of(resumed.judge, 0)
        assert "+    return a + b\n" in scope
        assert scope == _scope_of(clean.judge, 1)
        assert _memory_of(_rounds(p)[2]) == _memory_of(_rounds(clean.p)[2])
        assert resumed.ok is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("point", ["while_running", "before_amend_fix"])
    async def test_review_memory_resume_S8_stop_in_the_escalated_fix(
        self, project, tmp_path, point
    ):
        still = lambda r: _assess(r, ("B01-R1-F1", "still_present"))
        unstopped_reviews = [_round_one(), still(_failing()), _passing()]
        clean = await _unstopped(
            tmp_path / "unstopped", unstopped_reviews, review_model="claude-haiku-4-5"
        )
        p = project
        p.config.review_model = "claude-haiku-4-5"
        calls = []

        def fixer(**kwargs):
            calls.append(kwargs["model"])
            if point == "while_running" and len(calls) == 2:
                raise Killed()
            (p.dir / "app.py").write_text(
                FIXED if len(calls) == 1 else "def add(a, b):\n    return b + a\n"
            )
            return AgentResult(success=True, output="fixed\nRECURRING: none\n")

        kill = _dies_at("amend_fix", 2) if point == "before_amend_fix" else None
        await _stopped(p, [_round_one(), still(_failing())], fixer, kill=kill)
        second = _rounds(p)[2]
        assert second["fix_brief"].startswith(ESCALATION_HEAD)
        if point == "while_running":
            assert second["fix"] is None  # `escalated` missing
        else:
            assert (
                second["fix"]["escalated"] is True and second["fix"]["commit"] is None
            )

        resumed_reviews = [still(_failing()), still(_failing()), _passing()]
        resumed = await _resumed(p, resumed_reviews, _fixer(p.dir, {"app.py": FIXED}))

        # Counted from 0 in the new process: its own last fix is the escalated
        # one, whatever record 2 says.
        models = [c.kwargs["model"] for c in resumed.fix.await_args_list]
        assert (
            models
            == [c.kwargs["model"] for c in clean.fix.await_args_list]
            == [
                p.config.model_for("review_fix"),
                p.config.model_for("review_fix_escalated"),
            ]
        )
        descriptions = [
            c.kwargs["block_description"] for c in resumed.fix.await_args_list
        ]
        assert not descriptions[0].startswith(ESCALATION_HEAD)
        assert descriptions[1].startswith(ESCALATION_HEAD)
        assert resumed.ok is clean.ok is True
        assert "escalated" not in _rounds(p)[3]["fix"]

    @pytest.mark.asyncio
    async def test_review_memory_resume_S9_user_edits_between_stop_and_resume(
        self, project, tmp_path
    ):
        p = project
        await _stop_before_the_fix(p)
        # The user moves the flagged line and adds a file.
        (p.dir / "app.py").write_text("# moved\n" + BUGGY)
        (p.dir / "notes.md").write_text("mine\n")

        resumed = await _resumed(p, [_passing()])

        assert resumed.ok is True  # nothing new refuses
        assert _open_given(resumed.review, 0) == ["B01-R1-F1"]
        assert _rounds(p)[2]["prior_assessments"] == [
            {"finding_id": "B01-R1-F1", "status": "cannot_verify", "evidence": ""}
        ]
        assert (p.dir / "app.py").read_text() == "# moved\n" + BUGGY
        assert (p.dir / "notes.md").read_text() == "mine\n"

    @pytest.mark.asyncio
    async def test_review_memory_resume_S10_origin_moved(self, project, tmp_path):
        still_here = _make_project(tmp_path / "unmoved")
        await _stop_before_the_fix(still_here)
        unmoved = await _resumed(still_here, [_resolved_pass()])

        p = project
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        _git(p.root, "remote", "add", "origin", str(origin))
        _git(p.root, "push", "-q", "origin", "HEAD:refs/heads/main")
        await _stop_before_the_fix(p)
        other = tmp_path / "other-clone"
        _git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(other))
        _git(
            other,
            "-c",
            "user.email=o@example.com",
            "-c",
            "user.name=O",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "someone else's commit",
        )
        _git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(p.root, "fetch", "-q", "origin")

        resumed = await _resumed(p, [_resolved_pass()])

        assert resumed.ok is unmoved.ok is True
        assert _open_given(resumed.review, 0) == _open_given(unmoved.review, 0)
        assert _scope_of(resumed.judge, 0) == _scope_of(unmoved.judge, 0)
        assert _memory_of(_rounds(p)[2]) == _memory_of(_rounds(still_here)[2])

    @pytest.mark.asyncio
    async def test_review_memory_resume_S11_the_same_stop_twice(
        self, project, tmp_path
    ):
        clean = await _unstopped(
            tmp_path / "unstopped",
            [_round_one(), _failing([_finding("Major", NEW)]), _resolved_pass()],
        )
        p = project
        await _stop_before_the_fix(p)

        def dying_fixer(**kwargs):
            raise Killed()

        state = BuildState.load(p.config.state_file_path())
        stack, *_ = _memory_engine(
            [_failing([_finding("Major", NEW)])], dying_fixer, [FindingAdjudication()]
        )
        with stack, pytest.raises(Killed):
            await _run_integration_and_review(
                state.tdd.blocks[0], p.config, state, p.progress
            )
        assert sorted(_rounds(p)) == [1, 2]

        resumed = await _resumed(p, [_resolved_pass()])

        # The latest record (2) is `last`: its open list, as unstopped round 3's.
        assert (
            _open_given(resumed.review, 0)
            == _open_given(clean.review, 2)
            == ["B01-R1-F1", "B01-R2-F1"]
        )
        assert resumed.ok is True

    @pytest.mark.asyncio
    async def test_review_memory_resume_S11_a_rolled_back_fix_keeps_its_findings_open(
        self, project
    ):
        p = project
        stack, _, _ = _engine(
            [_passing([_finding()]), _failing()], _fixer(p.dir, {"app.py": FIXED})
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )
        assert _rounds(p)[1]["fix"]["kept"] is False  # the fix on pass was rolled back
        open_, _, last = review_rounds.open_state(p.config.change_dir, 1)
        assert last["round"] == 1
        assert [f.finding_id for f in open_] == ["B01-R1-F1"]

    @pytest.mark.asyncio
    async def test_review_memory_resume_S12_the_stopped_process_is_still_alive(
        self, project
    ):
        p = project
        p.config.fix_on_pass = False
        second_review = []

        async def fixer(**kwargs):
            if len(second_review) == 0:
                # Process A's fix agent is still running when B resumes the block.
                second_review.append("B")
                state = BuildState.load(p.config.state_file_path())
                assert await _run_integration_and_review(
                    state.tdd.blocks[0], p.config, state, p.progress
                )
            (p.dir / "app.py").write_text(FIXED)
            return AgentResult(success=True, output="fixed")

        reviews = [
            _round_one(),  # A, round 1
            _assess(
                _passing([_finding("Minor", NEW)]), ("B01-R1-F1", "still_present")
            ),  # B
            _resolved_pass(),  # A, after its fix
        ]
        stack, review, _, _ = _memory_engine(
            reviews, fixer, [FindingAdjudication()] * 3
        )
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        rounds = _rounds(p)
        assert sorted(rounds) == [1, 2, 3]  # distinct numbers, no lock
        assert _open_given(review, 1) == ["B01-R1-F1"]  # B read record 1
        # A's next review reads the latest record, B's round 2.
        assert (
            _open_given(review, 2)
            == _ids(rounds[2]["open_findings"])
            == ["B01-R1-F1", "B01-R2-F1"]
        )
        assert [a["finding_id"] for a in rounds[3]["prior_assessments"]] == [
            "B01-R1-F1",
            "B01-R2-F1",
        ]

    @pytest.mark.asyncio
    async def test_review_memory_resume_S13_finding_fields_survive_the_stop(
        self, project, tmp_path
    ):
        located = _finding().model_copy(
            update={"defect_class": "off-by-one", "other_locations": ["app.py:9"]}
        )
        first = _failing([located, _finding("Minor", NAMES)])
        judge1 = _verdicts(
            FindingVerdict(index=0, accepted=True),
            FindingVerdict(index=1, accepted=False, reason="the spec names them"),
        )
        restated = _assess(
            _failing([_finding("Minor", "names are still unclear")]),
            ("B01-R1-F1", "still_present"),
        )
        judge2 = _verdicts(
            FindingVerdict(index=0, accepted=True, duplicate_of="B01-R1-X1")
        )
        clean = await _unstopped(
            tmp_path / "unstopped",
            [first, restated, _resolved_pass()],
            [judge1, judge2, FindingAdjudication()],
        )
        p = project

        def dying_fixer(**kwargs):
            raise Killed()

        await _stopped(p, [first], dying_fixer, judges=[judge1])
        resumed = await _resumed(
            p, [restated, _resolved_pass()], judges=[judge2, FindingAdjudication()]
        )

        assert resumed.ok is True
        second = _rounds(p)[2]
        assert _memory_of(second) == _memory_of(_rounds(clean.p)[2])
        carried, new = second["open_findings"]
        assert carried["defect_class"] == "off-by-one" and carried[
            "other_locations"
        ] == ["app.py:9"]
        assert new["duplicate_of"] == "B01-R1-X1"
        assert new["earlier_judge_reason"] == "the spec names them"
        assert "class: off-by-one; also at: app.py:9" in _brief(second)
        assert "Fix every listed location, and cover each one with a test." in _brief(
            second
        )
        reread, _, _ = review_rounds.open_state(p.config.change_dir, 1)
        assert {f.finding_id: f.earlier_judge_reason for f in reread}["B01-R2-F1"] == (
            "the spec names them"
        )


class TestReviewedRevision:
    """`_reviewed_revision`: HEAD is the reviewed tree only when git shows nothing else in the project."""

    @staticmethod
    def _config(repo):
        return SimpleNamespace(
            project_dir=repo, change_dir=repo / "specs" / "changes" / "c"
        )

    @staticmethod
    def _sub(tmp_path, repo, name="vendor"):
        source = tmp_path / f"{name}-source"
        source.mkdir()
        _git(source, "init", "-q")
        for text in ("one\n", "two\n"):
            (source / "a.txt").write_text(text)
            _git(source, "add", "-A")
            _git(
                source,
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-qm",
                text,
            )
        _git(
            repo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            name,
        )
        _git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            name,
        )

    @staticmethod
    def _repo(tmp_path):
        repo = tmp_path / "r"
        (repo / "specs" / "changes" / "c").mkdir(parents=True)
        (repo / "app.py").write_text("x = 1\n")
        (repo / "specs" / "changes" / "c" / "tasks.md").write_text("t\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "init",
        )
        return repo

    def test_a_clean_tree_and_a_dirty_one(self, tmp_path):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")
        assert _reviewed_revision(self._config(repo), head) == (head, None)
        (repo / "specs" / "changes" / "c" / "reviews").mkdir()
        (
            repo / "specs" / "changes" / "c" / "reviews" / "block-01-round-1.json"
        ).write_text("{}\n")
        assert _reviewed_revision(self._config(repo), head) == (head, None)
        # The reviewer's diff holds the rest of the change dir: a prototype's source, the plan.
        (repo / "specs" / "changes" / "c" / "tasks.md").write_text("edited\n")
        assert _reviewed_revision(self._config(repo), head)[0] is None
        _git(repo, "checkout", "--", "specs")
        (repo / "new.py").write_text("y = 2\n")
        assert _reviewed_revision(self._config(repo), head)[0] is None
        assert _reviewed_revision(self._config(repo), None) == (
            None,
            "git could not read HEAD",
        )

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_a_file_git_is_told_not_to_check_makes_it_unknown(self, tmp_path, flag):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-index", flag, "app.py")
        (repo / "app.py").write_text("x = 2\n")
        reviewed, why = _reviewed_revision(self._config(repo), head)
        assert reviewed is None and "app.py" in why

    def test_a_file_a_submodule_tells_git_not_to_check_makes_it_unknown(self, tmp_path):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        self._sub(tmp_path, repo)
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo / "vendor", "update-index", "--assume-unchanged", "a.txt")
        (repo / "vendor" / "a.txt").write_text("edited\n")
        reviewed, why = _reviewed_revision(self._config(repo), head)
        assert reviewed is None and "vendor/a.txt" in why

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_a_moved_submodule_git_is_told_not_to_check_makes_it_unknown(
        self, tmp_path, flag
    ):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        self._sub(tmp_path, repo)
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-index", flag, "vendor")
        _git(repo / "vendor", "checkout", "-q", "--detach", "HEAD~1")
        reviewed, why = _reviewed_revision(self._config(repo), head)
        assert reviewed is None and "vendor" in why

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    @pytest.mark.parametrize("hidden", ["gitlink", "file"])
    def test_a_nested_submodule_entry_git_is_told_not_to_check_makes_it_unknown(
        self, tmp_path, flag, hidden
    ):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        self._sub(tmp_path, repo)
        vendor = repo / "vendor"
        self._sub(tmp_path, vendor, "inner")
        _git(repo, "add", "vendor")
        _git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "nest",
        )
        head = _git(repo, "rev-parse", "HEAD")
        assert _reviewed_revision(self._config(repo), head) == (head, None)
        if hidden == "gitlink":
            _git(vendor, "update-index", flag, "inner")
            _git(vendor / "inner", "checkout", "-q", "--detach", "HEAD~1")
        else:
            _git(vendor / "inner", "update-index", flag, "a.txt")
            (vendor / "inner" / "a.txt").write_text("edited\n")
        reviewed, why = _reviewed_revision(self._config(repo), head)
        assert (
            reviewed is None
            and ("vendor/inner" + ("" if hidden == "gitlink" else "/a.txt")) in why
        )

    @pytest.mark.parametrize("where", ["git", "stat", "iterdir", "resolve"])
    def test_a_submodule_the_filesystem_refuses_makes_it_unknown(
        self, tmp_path, monkeypatch, where
    ):
        from build_pipeline import git_ops
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        self._sub(tmp_path, repo)
        head = _git(repo, "rev-parse", "HEAD")
        vendor = repo / "vendor"
        if where == "git":
            run = git_ops._run

            def refuse(argv, cwd=None, **kw):
                if cwd is not None and Path(cwd) == vendor:
                    raise PermissionError(13, "Permission denied", str(cwd))
                return run(argv, cwd=cwd, **kw)

            monkeypatch.setattr(git_ops, "_run", refuse)
        else:
            name = {"stat": "is_dir", "iterdir": "iterdir", "resolve": "resolve"}[where]
            real = getattr(Path, name)

            def refuse(self, *a, **kw):
                if self == vendor:
                    raise PermissionError(13, "Permission denied", str(self))
                return real(self, *a, **kw)

            monkeypatch.setattr(Path, name, refuse)
            if where == "iterdir":  # reached only when git cannot read the checkout
                monkeypatch.setattr(
                    git_ops,
                    "_run",
                    lambda argv, cwd=None, _r=git_ops._run, **kw: (
                        _r(["false"])
                        if "--show-toplevel" in argv
                        else _r(argv, cwd=cwd, **kw)
                    ),
                )
        assert _reviewed_revision(self._config(repo), head) == (
            None,
            "git could not list the index",
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
    def test_an_unreadable_submodule_directory_makes_it_unknown(self, tmp_path):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        self._sub(tmp_path, repo)
        head = _git(repo, "rev-parse", "HEAD")
        (repo / "vendor").chmod(0)
        try:
            assert _reviewed_revision(self._config(repo), head)[0] is None
        finally:
            (repo / "vendor").chmod(0o755)

    def test_a_moved_submodule_the_parent_ignores_makes_it_unknown(self, tmp_path):
        from build_pipeline.tdd_engine import _reviewed_revision

        repo = self._repo(tmp_path)
        self._sub(tmp_path, repo)
        _git(repo, "config", "submodule.vendor.ignore", "all")
        head = _git(repo, "rev-parse", "HEAD")
        assert _reviewed_revision(self._config(repo), head) == (head, None)
        _git(repo / "vendor", "checkout", "-q", "--detach", "HEAD~1")
        assert _reviewed_revision(self._config(repo), head)[0] is None


def test_a_scope_diff_from_a_reviewed_commit_to_an_uncommitted_tree_is_none(project):
    from build_pipeline.tdd_engine import _scope_diff

    p = project
    head = _git(p.dir, "rev-parse", "HEAD")
    (p.dir / "app.py").write_text(FIXED)
    assert _scope_diff(p.config, {"head_sha": head, "reviewed_sha": head}) is None


# --- The integration gate throws a refactor away before fixing --------------


class TestRefactorDiscardedOnRedGate:
    """The refactor check runs only the block's tests, so the integration gate
    is the first whole run after a refactor. When it is red, the refactor
    commit is thrown away and the gate runs again before a fix agent runs."""

    @staticmethod
    def _commit(p, files: dict[str, str], subject: str) -> str:
        for rel, text in files.items():
            (p.dir / rel).write_text(text)
        _git(p.dir, "add", "-A")
        _git(p.dir, "commit", "-qm", subject)
        return _git(p.dir, "rev-parse", "HEAD")

    @staticmethod
    def _integration_fixer(p, removes_flag: bool):
        def run(**kwargs):
            if removes_flag:
                (p.dir / "broken.flag").unlink(missing_ok=True)
            return AgentResult(success=True, output="fixed")

        return patch(
            "build_pipeline.tdd_engine.run_integration_fix",
            new_callable=AsyncMock,
            side_effect=run,
        )

    @pytest.mark.asyncio
    async def test_red_gate_after_a_refactor_discards_it_without_a_fix_agent(
        self, project
    ):
        p = project
        p.block.impl_commit = self._commit(p, {"app.py": FIXED}, "impl")
        p.block.refactor_commit = self._commit(p, {"broken.flag": ""}, "refactor")

        stack, _, _ = _engine([_passing()], _fixer(p.dir))
        with stack, self._integration_fixer(p, removes_flag=True) as fixer:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        fixer.assert_not_called()
        assert not (p.dir / "broken.flag").exists()
        assert p.block.refactor_commit is None
        assert p.block.impl_commit in _git(p.dir, "log", "--format=%H").splitlines()
        assert "refactor" not in _git(p.dir, "log", "--format=%s").splitlines()
        saved = json.loads(p.config.state_file_path().read_text())
        assert saved["tdd"]["blocks"][0]["refactor_commit"] is None

    @pytest.mark.asyncio
    async def test_red_without_the_refactor_still_goes_to_the_fix_agent(self, project):
        p = project
        p.block.impl_commit = self._commit(
            p, {"app.py": FIXED, "broken.flag": ""}, "impl"
        )
        p.block.refactor_commit = self._commit(
            p,
            {"app.py": "def add(a: int, b: int) -> int:\n    return a + b\n"},
            "refactor",
        )

        stack, _, _ = _engine([_passing()], _fixer(p.dir))
        with stack, self._integration_fixer(p, removes_flag=True) as fixer:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        fixer.assert_called_once()
        assert p.block.refactor_commit is None
        assert (p.dir / "app.py").read_text() == FIXED

    @pytest.mark.asyncio
    async def test_without_a_refactor_commit_nothing_is_discarded(self, project):
        p = project
        p.block.impl_commit = self._commit(
            p, {"app.py": FIXED, "broken.flag": ""}, "impl"
        )

        stack, _, _ = _engine([_passing()], _fixer(p.dir))
        with stack, self._integration_fixer(p, removes_flag=True) as fixer:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )

        fixer.assert_called_once()
        assert _git(p.dir, "rev-parse", "HEAD") != p.baseline
        assert "impl" in _git(p.dir, "log", "--format=%s").splitlines()


# --- PUBLISH says in the PR body when no test gate ran ---------------------


class TestNoTestCommandSection:
    def test_no_test_command_is_a_pr_section(self, tmp_path):
        from build_pipeline.gates import NO_TEST_COMMAND_WARNING

        assert (
            orchestrator.no_test_command_section
            in orchestrator.PRE_PUBLISH_CONTRIBUTORS
        )
        config = SimpleNamespace(test_command="")
        path = orchestrator.no_test_command_section(config, None, tmp_path)
        assert path is not None
        text = path.read_text()
        assert text.splitlines()[0].startswith("## ")
        assert NO_TEST_COMMAND_WARNING in text

    def test_a_test_command_writes_no_section(self, tmp_path):
        config = SimpleNamespace(test_command="pytest -q")
        assert orchestrator.no_test_command_section(config, None, tmp_path) is None
        assert not list(tmp_path.rglob("*.md"))
