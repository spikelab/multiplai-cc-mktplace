"""The component review (`code_review.component_review`, item 61 of
plan-factory-merged-2026-10-29.md) and its stop/resume rows S44 and S45.

Every test runs the real code against a real git repo. Only the model calls
are replaced: the reviewer (`agent_call_structured`), the adjudicator's LLM
call and the fix agent.
"""

import json
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import review_rounds
from build_pipeline.config import BuildConfig
from build_pipeline.findings.code import round_config
from build_pipeline.gates import block_severity_gate
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    FindingAdjudication,
    ReviewFinding,
    ReviewResult,
    ReviewScore,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    _run_component_review,
    _run_integration_and_review,
)

BUGGY = "def add(a, b):\n    return a + b + 1\n"
FIXED = "def add(a, b):\n    return a + b\n"


class Killed(BaseException):
    """A stop no handler catches, like a kill."""


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _finding(severity: str, claim: str, path: str = "app.py") -> ReviewFinding:
    return ReviewFinding(
        claim=claim, severity=severity, file_path=path, line=2, evidence="a + b + 1"
    )


def _review(score: int, findings=()) -> ReviewResult:
    return ReviewResult(
        scores=[ReviewScore(dimension="Q", weight=2, score=score, evidence="e")],
        findings=list(findings),
    )


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


def _project(base: Path, *, built: bool = True, app: str = "app.py"):
    """A one-block build. *built*: the block is done and committed (the
    component review's starting point); otherwise it is about to be
    reviewed. The suite fails while `broken.flag` exists."""
    project_dir = base / "project"
    change = project_dir / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (change / "rubric.md").write_text("# Rubric\n")
    (project_dir / "README.md").write_text("x\n")
    _git(project_dir, "init", "-q")
    _git(project_dir, "config", "user.email", "t@example.com")
    _git(project_dir, "config", "user.name", "T")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-qm", "init")
    baseline = _git(project_dir, "rev-parse", "HEAD")
    (project_dir / app).parent.mkdir(parents=True, exist_ok=True)
    (project_dir / app).write_text(BUGGY)
    if built:
        _git(project_dir, "add", "-A")
        _git(project_dir, "commit", "-qm", "impl(block-1): Adder")
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
    config.component_review = True
    block = BlockInfo(
        number=1,
        name="Adder",
        description="add two numbers",
        status=BlockStatus.DONE if built else BlockStatus.IMPLEMENTING,
        baseline_commit=baseline,
    )
    state = BuildState(
        change_name="test",
        tier="advanced",
        state_file=str(config.state_file_path()),
        tdd=TDDState(
            blocks=[block], baseline_tests_pass=True, current_block=1 if built else 0
        ),
    )
    state.checkpoint(config.state_file_path())
    progress = ProgressWriter(base / "progress.md")
    progress.initialize("test", "advanced", 1)
    return SimpleNamespace(
        dir=project_dir, config=config, state=state, block=block, progress=progress
    )


def _fixer(p, files=None, output="fixed"):
    async def run(**kwargs):
        for rel, text in (files if files is not None else {"app.py": FIXED}).items():
            (p.dir / rel).write_text(text)
        return AgentResult(success=True, output=output)

    return run


def _models(reviews, fixer):
    """Patch the reviewer, the adjudicator and the fix agent. Returns
    (stack, reviewer mock, fixer mock)."""
    stack = ExitStack()
    reviewer = stack.enter_context(
        patch(
            "build_pipeline.llm_steps.review_steps.agent_call_structured",
            new=AsyncMock(side_effect=list(reviews)),
        )
    )
    stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.llm_call_structured",
            new=AsyncMock(return_value=FindingAdjudication()),
        )
    )
    fix = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.run_implementer",
            new=AsyncMock(side_effect=fixer),
        )
    )
    return stack, reviewer, fix


def _record(p) -> dict:
    path = p.config.change_dir / "reviews" / "component-review.json"
    record = json.loads(path.read_text())
    record.pop("generated_at")
    return record


def _unfixed(p) -> str:
    path = p.config.change_dir / "unfixed-findings.md"
    return path.read_text() if path.exists() else ""


def _subjects(p) -> list[str]:
    return _git(p.dir, "log", "--format=%s").splitlines()


def _status(p) -> str:
    """`git status --porcelain`, without buildme's own checkpoint file."""
    return "\n".join(
        line
        for line in _git(p.dir, "status", "--porcelain").splitlines()
        if not line.endswith(".build-state.json")
    )


def _outcome(p) -> dict:
    """What a run leaves: commit subjects, the file the fix edits, the
    record (commit ids replaced by their subjects), unfixed-findings.md and
    the saved state."""
    record = _record(p)
    if record.get("fix") and record["fix"].get("commit"):
        record["fix"]["commit"] = _git(
            p.dir, "log", "-1", "--format=%s", record["fix"]["commit"]
        )
    record.pop("head_sha")
    if record.get("fix") and "not_kept_reason" in record["fix"]:
        reason = record["fix"]["not_kept_reason"]
        record["fix"]["not_kept_reason"] = reason.split(" (output:")[0]
    saved = BuildState.load(p.config.state_file_path()).tdd
    return {
        "subjects": _subjects(p),
        "app": (p.dir / "app.py").read_text(),
        "record": record,
        "unfixed": re.sub(r" \(output: [^)]*\)", "", _unfixed(p)),
        "done": saved.component_review_done,
        "in_flight": saved.component_review,
        "clean": _status(p),
    }


async def _run(p, reviews, fixer):
    stack, reviewer, fix = _models(reviews, fixer)
    with stack:
        ok = await _run_component_review(p.config, p.state, p.progress)
    return ok, reviewer, fix


# --- Config -----------------------------------------------------------------


class TestConfig:
    def _load(self, tmp_path, text: str) -> BuildConfig:
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(text)
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        return config

    def test_defaults_are_todays_behaviour(self):
        config = BuildConfig()
        assert config.component_review is False
        assert config.block_gate_severities == ("critical", "major")

    def test_both_settings_load(self, tmp_path):
        config = self._load(
            tmp_path,
            "code_review:\n  component_review: true\n"
            "  block_gate_severities: [Critical]\n",
        )
        assert config.component_review is True
        assert config.block_gate_severities == ("critical",)

    def test_an_unknown_severity_keeps_the_default(self, tmp_path, caplog):
        config = self._load(
            tmp_path, "code_review:\n  block_gate_severities: [Critical, Urgent]\n"
        )
        assert config.block_gate_severities == ("critical", "major")
        assert "block_gate_severities" in caplog.text

    def test_a_findings_round_runs_no_component_review(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config.component_review = True
        change_dir = tmp_path / "specs" / "changes" / "test"
        assert round_config(config, change_dir, 1).component_review is False


# --- The lighter per-block gate --------------------------------------------


class TestBlockGate:
    def test_fails_only_on_the_gating_severities(self):
        review = _review(1, [_finding("Minor", "m")])
        minor = [_finding("Minor", "m")]
        assert block_severity_gate(review, minor, ("critical", "major")).passed
        major = [_finding("Major", "M")]
        assert not block_severity_gate(review, major, ("critical", "major")).passed

    def test_the_spec_compliance_floor_stays(self):
        review = _review(5).model_copy(update={"missing": ["scenario 1"]})
        gate = block_severity_gate(review, [], ("critical", "major"))
        assert not gate.passed and "Spec-compliance" in gate.reason

    @pytest.mark.asyncio
    async def test_a_low_score_with_only_minor_findings_passes_without_a_fix_on_pass(
        self, tmp_path
    ):
        p = _project(tmp_path, built=False)
        stack, reviewer, fix = _models([_review(1, [_finding("Minor", "vague")])], None)
        with stack:
            assert await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )
        assert reviewer.await_count == 1
        fix.assert_not_awaited()
        assert p.block.status == BlockStatus.DONE
        assert _unfixed(p) == ""

    @pytest.mark.asyncio
    async def test_at_most_two_rounds_and_the_fix_gets_only_gating_findings(
        self, tmp_path
    ):
        p = _project(tmp_path, built=False)
        reviews = [
            _review(5, [_finding("Major", "off by one"), _finding("Minor", "vague")]),
            _review(5, [_finding("Major", "still off by one", "other.py")]),
        ]
        stack, reviewer, fix = _models(reviews, _fixer(p, {"README.md": "y\n"}))
        with stack:
            ok = await _run_integration_and_review(
                p.block, p.config, p.state, p.progress
            )
        assert not ok
        assert reviewer.await_count == 2
        assert fix.await_count == 1
        brief = fix.await_args.kwargs["failing_tests"]
        assert "off by one" in brief and "vague" not in brief
        assert "after 2 iterations" in (tmp_path / "progress.md").read_text()


# --- The component review ---------------------------------------------------


class TestComponentReview:
    @pytest.mark.asyncio
    async def test_off_runs_nothing(self, tmp_path):
        p = _project(tmp_path)
        p.config.component_review = False
        ok, reviewer, _ = await _run(p, [], None)
        assert ok
        reviewer.assert_not_awaited()
        assert (
            BuildState.load(p.config.state_file_path()).tdd.component_review_done
            is False
        )
        assert not (p.config.change_dir / "reviews").exists()

    @pytest.mark.asyncio
    async def test_a_green_fix_is_kept_and_minor_findings_go_to_the_pr_body(
        self, tmp_path
    ):
        p = _project(tmp_path)
        review = _review(
            3, [_finding("Critical", "off by one"), _finding("Minor", "vague")]
        )
        ok, reviewer, fix = await _run(p, [review], _fixer(p))
        assert ok
        prompt = reviewer.await_args.args[0]
        assert "You are reviewing a whole component" in prompt
        assert "+    return a + b + 1" in prompt
        assert "off by one" in fix.await_args.kwargs["failing_tests"]
        assert "vague" not in fix.await_args.kwargs["failing_tests"]
        out = _outcome(p)
        assert out["app"] == FIXED
        assert out["record"]["fix"]["kept"] is True
        assert out["record"]["accepted"][0]["finding_id"] == "C-F1"
        assert "Component review: [Minor] vague" in out["unfixed"]
        assert "off by one" not in out["unfixed"]
        assert out["done"] and out["in_flight"] is None and out["clean"] == ""
        assert out["subjects"][:3] == [
            "docs(component): component review",
            "fix(component): component review findings",
            "docs(component): component review",
        ]

    @pytest.mark.asyncio
    async def test_a_red_fix_is_thrown_away(self, tmp_path):
        p = _project(tmp_path)
        review = _review(3, [_finding("Major", "off by one")])
        ok, _, _ = await _run(
            p, [review], _fixer(p, {"app.py": FIXED, "broken.flag": ""})
        )
        assert ok
        out = _outcome(p)
        assert out["app"] == BUGGY
        assert not (p.dir / "broken.flag").exists()
        assert out["record"]["fix"]["kept"] is False
        assert "fix not kept: tests failed after the fix" in out["unfixed"]
        assert "fix(component): component review findings" not in out["subjects"]

    @pytest.mark.asyncio
    async def test_no_critical_or_major_means_no_fix_round(self, tmp_path):
        p = _project(tmp_path)
        ok, _, fix = await _run(p, [_review(3, [_finding("Minor", "vague")])], None)
        assert ok
        fix.assert_not_awaited()
        assert "[Minor] vague" in _unfixed(p)
        assert _status(p) == ""

    @pytest.mark.asyncio
    async def test_access_checklist_only_for_auth_role_or_tenant_code(self, tmp_path):
        plain = _project(tmp_path / "a")
        _, reviewer, _ = await _run(plain, [_review(5)], None)
        assert (
            "Checklist: this component touches auth" not in reviewer.await_args.args[0]
        )
        access = _project(tmp_path / "b", app="auth/permissions.py")
        _, reviewer, _ = await _run(access, [_review(5)], None)
        assert "Checklist: this component touches auth" in reviewer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_requirements_question_goes_to_spec_issues(self, tmp_path):
        p = _project(tmp_path)
        report = (
            "fixed\nSPEC_IMPACT: contradicts\nSURPRISES: the spec says add adds one"
        )
        ok, _, _ = await _run(
            p, [_review(3, [_finding("Major", "off by one")])], _fixer(p, output=report)
        )
        assert ok
        issues = (p.config.change_dir / "spec-issues.md").read_text()
        assert "Component review: findings that need a requirements change" in issues
        assert "C-F1" in issues
        assert "spec-issues.md" in _unfixed(p)

    @pytest.mark.asyncio
    async def test_block_findings_below_the_gate_reach_the_component_reviewer(
        self, tmp_path
    ):
        p = _project(tmp_path)
        review_rounds.record_round(
            p.config.change_dir,
            p.block,
            head_sha=None,
            review=_review(5, [_finding("Minor", "the name add is vague")]),
            gate_passed=True,
            gate_reason="ok",
            weighted_average=5.0,
        )
        _git(p.dir, "add", "-A")
        _git(p.dir, "commit", "-qm", "fix(block-1): Adder")
        _, reviewer, _ = await _run(p, [_review(5)], None)
        prompt = reviewer.await_args.args[0]
        assert "B01-R1-F1 [Minor] the name add is vague" in prompt


# --- Stop/resume rows -------------------------------------------------------


async def _unstopped(base: Path, extra_files=None):
    p = _project(base)
    review = _review(
        3, [_finding("Critical", "off by one"), _finding("Minor", "vague")]
    )
    files = {"app.py": FIXED, **(extra_files or {})}
    ok, _, _ = await _run(p, [review], _fixer(p, files))
    assert ok
    return p


class TestComponentReviewRows:
    @pytest.mark.asyncio
    async def test_s44_stopped_after_the_review_runs_the_fix_round_on_the_saved_review(
        self, tmp_path
    ):
        expected = _outcome(await _unstopped(tmp_path / "unstopped"))

        p = _project(tmp_path / "stopped")
        review = _review(
            3, [_finding("Critical", "off by one"), _finding("Minor", "vague")]
        )

        async def killed(**kwargs):
            raise Killed()

        with pytest.raises(Killed):
            await _run(p, [review], killed)
        saved = BuildState.load(p.config.state_file_path())
        assert saved.tdd.component_review.step == "fix"

        # Resumed: no new review (the reviewer has nothing to return).
        ok, reviewer, fix = await _run(p, [], _fixer(p))
        assert ok
        reviewer.assert_not_awaited()
        assert "off by one" in fix.await_args.kwargs["failing_tests"]
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("suite", ["green", "red"])
    async def test_s45_stopped_after_the_fix_commit_runs_the_suite(
        self, tmp_path, suite
    ):
        extra = {"broken.flag": ""} if suite == "red" else {}
        expected = _outcome(await _unstopped(tmp_path / "unstopped", extra))

        p = _project(tmp_path / "stopped")
        review = _review(
            3, [_finding("Critical", "off by one"), _finding("Minor", "vague")]
        )
        with (
            patch(
                "build_pipeline.tdd_engine._component_suite_gate", side_effect=Killed()
            ),
            pytest.raises(Killed),
        ):
            await _run(p, [review], _fixer(p, {"app.py": FIXED, **extra}))
        saved = BuildState.load(p.config.state_file_path()).tdd.component_review
        assert saved.step == "suite"
        assert saved.fix_commit == _git(p.dir, "rev-parse", "HEAD")

        ok, reviewer, fix = await _run(p, [], None)
        assert ok
        reviewer.assert_not_awaited()
        fix.assert_not_awaited()
        assert _outcome(p) == expected
        assert (p.dir / "app.py").read_text() == (BUGGY if suite == "red" else FIXED)

    @pytest.mark.asyncio
    async def test_s53_a_resume_over_someone_elses_edit_changes_nothing(self, tmp_path):
        p = _project(tmp_path)
        review = _review(3, [_finding("Critical", "off by one")])

        async def killed(**kwargs):
            raise Killed()

        with pytest.raises(Killed):
            await _run(p, [review], killed)
        (p.dir / "README.md").write_text("user edit\n")
        head = _git(p.dir, "rev-parse", "HEAD")
        ok, _, fix = await _run(p, [], _fixer(p))
        assert not ok
        fix.assert_not_awaited()
        assert (p.dir / "README.md").read_text() == "user edit\n"
        assert _git(p.dir, "rev-parse", "HEAD") == head
        assert "did not resume" in (tmp_path / "progress.md").read_text()
