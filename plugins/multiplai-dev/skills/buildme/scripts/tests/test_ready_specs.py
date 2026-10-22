"""buildme builds only from pm-spec's ready specs (PR 4 of the pm-spec split).

Rows S10-S15 of the plan's stop/resume table, the start refusal, old
checkpoints (item 21) and the three ways a build sends a requirements problem
back to pm-spec (item 20). Every model call is a fake; the repos, the bare
origin and the worktrees are real temp repos. pm-spec's own steps (marking
ready, pushing the feature branch, `revise`) are played with
`multiplai_core.specs`, the only code the two tools share, and git.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from build_pipeline import readiness
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.llm_steps.spec_steps import AuditFindings
from build_pipeline.models import (
    BlockInfo,
    BuildPhase,
    PlanReviewResult,
    PriorAssessment,
)
from build_pipeline.orchestrator import run_orchestrator
from build_pipeline.state import BuildState, TDDState
from multiplai_core.specs import clear_spec_issues, mark_ready, read_change_meta

from tests.spec_fixtures import (
    REQUIREMENT,
    commit_specs,
    publish,
    ready_specs,
    write_specs,
)
from tests.test_git_ops import make_bare_origin, make_repo

pytestmark = pytest.mark.no_handoff

CHANGE = "feat"
FIXTURES = Path(__file__).parent / "fixtures"
ISSUE_LINE = (
    "SPEC_ISSUE: requirements/fixture.md: Behavior: a value cannot be returned "
    "without a store; add a requirement for the store"
)
REVISED = (
    REQUIREMENT + "\n### Requirement: Store\nThe system SHALL keep the value.\n\n"
    "#### Scenario: Kept\n- **WHEN** a value is set\n- **THEN** it is kept\n"
)


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    from build_pipeline import orchestrator

    monkeypatch.setattr(
        orchestrator, "_run_spec_extract_step", lambda config, state, progress: None
    )
    path = make_repo(tmp_path / "proj")
    make_bare_origin(path, tmp_path / "origin.git")
    _git(path, "push", "-q", "origin", "main")
    return path


def _merging(w: Path) -> bool:
    """Whether a merge is in progress in the worktree *w*."""
    return (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=w,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _handoff(repo: Path) -> Path:
    """pm-spec's handoff: the specs ready and committed, and pushed to
    `origin/ENG-1-<change>`. Returns the checkout's change dir."""
    change_dir = _source_change(repo)
    ready_specs(change_dir, repo)
    assert publish(repo, CHANGE) == f"ENG-1-{CHANGE}"
    return change_dir


def _revise_on_branch(repo: Path, at: Path) -> Path:
    """`pm-spec revise` in its own worktree of the feature branch: the
    requirement fixed, marked ready again, committed and pushed."""
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "worktree", "add", "-q", "--detach", str(at), f"origin/ENG-1-{CHANGE}")
    change_dir = at / "specs" / "changes" / CHANGE
    (change_dir / "requirements" / "fixture.md").write_text(REVISED)
    mark_ready(change_dir)
    assert commit_specs(at, change_dir, "specs: revised on the branch")
    _git(at, "push", "-q", "origin", f"HEAD:refs/heads/ENG-1-{CHANGE}")
    return change_dir


def _source_change(repo: Path) -> Path:
    return repo / "specs" / "changes" / CHANGE


def _worktree(tmp_path: Path) -> Path:
    return tmp_path / "ws" / ".worktrees" / f"buildme-{CHANGE}"


def _config(repo: Path, **over) -> BuildConfig:
    fields = dict(
        project_dir=repo,
        change_name=CHANGE,
        auto=True,
        spec_only=True,
        skip_research=True,
        skip_explainers=True,
        git=GitToggles(worktree=True, push=False, pr="none"),
    )
    fields.update(over)
    config = BuildConfig(**fields)
    config.specs_dir = repo / "specs"
    config.test_command = "true"
    return config


def _args(repo: Path, **over) -> argparse.Namespace:
    base = dict(
        change=CHANGE,
        project_dir=str(repo),
        auto=True,
        spec_only=True,
        skip_research=True,
        research_path="",
        session_id="",
        accept_size=False,
        force=False,
        block=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class Models:
    """The planning steps' model calls, as fakes that count what they were asked."""

    def __init__(
        self, design: list[str] | None = None, audit: list[list[dict]] | None = None
    ):
        self.designs = list(design or ["# Design\n\n## Decisions\nReturn the value.\n"])
        self.audits = list(audit or [[]])
        self.prompts: list[str] = []
        self.generate = AsyncMock(side_effect=self._generate)
        self.design_audit = AsyncMock(side_effect=self._audit)

    async def _generate(self, artifact_id, context, config, **kwargs):
        from build_pipeline.llm_steps.spec_steps import _build_prompt

        self.prompts.append(_build_prompt(artifact_id, context, config, **kwargs))
        if artifact_id == "design":
            return self.designs.pop(0) if len(self.designs) > 1 else self.designs[0]
        return "## 1. Behavior\nReturn a value\n"

    async def _audit(self, *args, **kwargs):
        return self.audits.pop(0) if len(self.audits) > 1 else self.audits[0]

    def patches(self):
        return (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact", self.generate
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                AsyncMock(return_value=[]),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                self.design_audit,
            ),
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                AsyncMock(return_value=PlanReviewResult(findings=[])),
            ),
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric\nCorrect value\n"),
            ),
        )


async def _build(repo: Path, models: Models, **config_over) -> int:
    p = models.patches()
    with p[0], p[1], p[2], p[3], p[4]:
        return await run_orchestrator(
            _config(repo, **config_over),
            _args(repo, spec_only=config_over.get("spec_only", True)),
        )


def _revise(project: Path, change_dir: Path) -> None:
    """What `pm-spec revise` leaves: the requirement fixed, `spec_issues`
    cleared, spec-issues.md moved aside, the specs marked ready again, the
    change dir committed."""
    (change_dir / "requirements" / "fixture.md").write_text(REVISED)
    clear_spec_issues(change_dir)
    issues = change_dir / readiness.ISSUES_FILE
    if issues.exists():
        issues.rename(change_dir / "spec-issues.resolved-1.md")
    mark_ready(change_dir)
    assert commit_specs(project, change_dir, "specs: revised")


def _tree(path: Path) -> dict[str, bytes]:
    return {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


# --- the start refusal (item 18, row S10) ---------------------------------------


class TestStartRefusal:
    @pytest.mark.asyncio
    async def test_S10_no_specs_refuse_and_write_nothing(self, repo, tmp_path, capsys):
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == readiness.EXIT_NOT_READY == 2
        assert "SPEC:NOT_READY:no .change.yaml" in out
        assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out
        assert not _source_change(repo).exists()
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_S10_specs_never_marked_ready_refuse_with_pm_spec_resume(
        self, repo, tmp_path, capsys
    ):
        """pm-spec's `resume` adopts specs it did not write: it checks them
        and marks them ready without rewriting them."""
        change_dir = write_specs(_source_change(repo))
        commit_specs(repo, change_dir)
        before = _tree(change_dir)
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:no .change.yaml" in out
        assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
        assert _tree(change_dir) == before
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_S10_specs_edited_after_ready_refuse_with_an_old_checkpoint_too(
        self, repo, tmp_path, capsys
    ):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        (change_dir / "requirements" / "fixture.md").write_text(REVISED)
        state = BuildState(
            change_name=CHANGE,
            tier="advanced",
            phase=BuildPhase.RESEARCH,
            state_file=str(change_dir / ".build-state.json"),
        )
        state.checkpoint()
        before = _tree(change_dir)

        rc = await _build(repo, Models())

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:the specs changed after they were marked ready" in out
        assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out
        assert _tree(change_dir) == before

    @pytest.mark.asyncio
    async def test_specs_ready_only_in_the_checkout_refuse_with_pm_spec_resume(
        self, repo, tmp_path, capsys
    ):
        """T8: a handoff from before pm-spec pushed (ready and committed in the
        checkout, no feature branch on origin) is refused: the build starts
        from the feature branch, and `pm-spec resume` moves the specs there."""
        ready_specs(_source_change(repo), repo)
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:" in out
        assert f"origin has no ENG-1-{CHANGE}" in out
        assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
        assert not (tmp_path / "ws").exists()
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_specs_are_read_on_the_feature_branch_not_in_the_checkout(
        self, repo, tmp_path, capsys
    ):
        """T4: run from a checkout that lacks the change dir, the build finds
        the specs on `origin/ENG-1-<change>` and starts from that branch (its
        PR's target: `test_completion_contract`)."""
        change_dir = _handoff(repo)
        pushed = _git(repo, "rev-parse", f"origin/ENG-1-{CHANGE}")
        _git(repo, "reset", "-q", "--hard", "HEAD~1")
        assert not change_dir.exists()
        config = _config(repo)
        p = Models().patches()
        with p[0], p[1], p[2], p[3], p[4]:
            rc = await run_orchestrator(config, _args(repo))

        assert rc == 0, capsys.readouterr().out
        assert config.feature_branch == f"ENG-1-{CHANGE}"
        wt = _worktree(tmp_path)
        assert _git(wt, "merge-base", "--is-ancestor", pushed, "HEAD") == ""
        assert (wt / "specs" / "changes" / CHANGE / "design.md").is_file()
        assert not change_dir.exists(), "the checkout is never written"

    @pytest.mark.asyncio
    async def test_a_conflict_merging_main_into_the_feature_branch_builds_nothing(
        self, repo, tmp_path, capsys
    ):
        """T6 for one build: main and the feature branch changed the same
        line. The build prints the marker, exits 3, leaves no merge in
        progress and the branch as it was, and cuts no worktree."""
        _handoff(repo)
        (repo / "README.md").write_text("on main\n")
        _git(repo, "commit", "-qam", "main's readme")
        _git(repo, "push", "-q", "origin", "HEAD:main")
        _git(repo, "checkout", "-q", "-b", "other", f"origin/ENG-1-{CHANGE}")
        (repo / "README.md").write_text("on the branch\n")
        _git(repo, "commit", "-qam", "the branch's readme")
        _git(repo, "push", "-q", "origin", f"HEAD:ENG-1-{CHANGE}")
        before = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 3
        assert f"FEATURE_BRANCH:CONFLICT:ENG-1-{CHANGE}:README.md" in out.splitlines()
        assert _git(repo, "rev-parse", f"origin/ENG-1-{CHANGE}") == before
        merger = tmp_path / "ws" / ".worktrees" / f"buildme-feature-ENG-1-{CHANGE}"
        assert _git(merger, "status", "--porcelain") == ""
        assert not _merging(merger)
        assert not _worktree(tmp_path).exists()
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ready_uncommitted_specs_build_in_place_with_no_worktree(
        self, repo, capsys
    ):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo, commit=False)
        rc = await _build(
            repo, Models(), git=GitToggles(worktree=False, push=False, pr="none")
        )
        assert rc == 0, capsys.readouterr().err
        assert (change_dir / "design.md").is_file()

    @pytest.mark.asyncio
    async def test_ready_committed_specs_build_and_buildme_never_writes_them(
        self, repo, tmp_path, capsys
    ):
        change_dir = _handoff(repo)
        what = {
            name: (change_dir / name).read_bytes()
            for name in ("proposal.md", "use-cases.md", "requirements/fixture.md")
        }
        models = Models()

        rc = await _build(repo, models)

        assert rc == 0, capsys.readouterr().err
        built = _worktree(tmp_path) / "specs" / "changes" / CHANGE
        assert (
            _git(
                _worktree(tmp_path),
                "merge-base",
                "--is-ancestor",
                f"origin/ENG-1-{CHANGE}",
                "HEAD",
            )
            == ""
        )
        for name, data in what.items():
            assert (built / name).read_bytes() == data, name
        assert (built / "design.md").is_file() and (built / "tasks.md").is_file()
        generated = [c.args[0] for c in models.generate.await_args_list]
        assert not set(generated) & {"proposal", "use-cases", "requirements"}


# --- requirements problems go back to pm-spec (item 20, rows S11, S12, S15) ----


class TestDesignSpecIssue:
    @pytest.mark.asyncio
    async def test_S11_a_design_spec_issue_stops_and_resumes_after_revise(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        models = Models(
            design=[
                f"# Design\n\nDraft.\n\n{ISSUE_LINE}\n",
                "# Design\n\n## Decisions\nKeep the value in a store.\n",
            ]
        )

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        worktree = _worktree(tmp_path)
        built = worktree / "specs" / "changes" / CHANGE
        issues = built / "spec-issues.md"
        assert rc == readiness.EXIT_SPEC_ISSUES == 4
        assert f"SPEC:ISSUES:{issues}" in out
        assert (
            f"Run /multiplai-pm:pm-spec revise {CHANGE} --project-dir {worktree}" in out
        )
        text = issues.read_text()
        assert text.count("\n## ") == 1
        assert "## Design: requirements/fixture.md: Behavior" in text
        assert "add a requirement for the store" in text
        assert read_change_meta(built)["spec_issues"] == "spec-issues.md"
        assert not (built / "design.md").exists(), (
            "a design with spec issues is not published"
        )
        state = BuildState.load(built / ".build-state.json")
        assert state.phase != BuildPhase.FAILED
        assert not state.is_phase_complete(BuildPhase.SPEC_GENERATION)

        _revise(worktree, built)
        asked = len(models.prompts)
        rc = await _build(repo, models)
        assert rc == 0, capsys.readouterr().err
        assert "Keep the value in a store" in (built / "design.md").read_text()
        assert "### Requirement: Store" in models.prompts[asked], (
            "the design reads the revised requirements"
        )
        assert not (built / "spec-issues.md").exists()

    @pytest.mark.asyncio
    async def test_S15_a_stopped_design_step_stopped_twice_gives_the_same_refusal_and_spends_nothing(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        models = Models(design=[f"# Design\n\n{ISSUE_LINE}\n"])
        assert await _build(repo, models) == 4
        first = capsys.readouterr().out.strip().splitlines()[-2:]
        built = _worktree(tmp_path) / "specs" / "changes" / CHANGE
        spent = BuildState.load(built / ".build-state.json").budget
        issues = (built / "spec-issues.md").read_text()
        calls = models.generate.await_count

        for _ in range(2):
            assert await _build(repo, models) == 4
            assert capsys.readouterr().out.strip().splitlines()[-2:] == first
        assert models.generate.await_count == calls
        assert BuildState.load(built / ".build-state.json").budget == spent
        assert (built / "spec-issues.md").read_text() == issues

    @pytest.mark.asyncio
    async def test_S14_base_branch_moved_between_stop_and_resume_changes_nothing(
        self, repo, tmp_path, capsys
    ):
        """Both tools hash files, not git refs: a commit on the source's main
        after the stop does not refuse the resume. The resume merges it into
        the feature branch it builds on (Decision 5), in the build's own
        worktree, and the build's push takes it to origin."""
        _handoff(repo)
        models = Models(
            design=[f"# Design\n\n{ISSUE_LINE}\n", "# Design\n\n## Decisions\nStore.\n"]
        )
        assert await _build(repo, models) == 4
        (repo / "LATER.md").write_text("later\n")
        _git(repo, "add", "LATER.md")
        _git(repo, "commit", "-qm", "later on main")
        _git(repo, "push", "-q", "origin", "main")
        worktree = _worktree(tmp_path)
        _revise(worktree, worktree / "specs" / "changes" / CHANGE)
        capsys.readouterr()

        assert await _build(repo, models) == 0, capsys.readouterr().err
        log = _git(worktree, "log", "--format=%s")
        assert "later on main" in log
        assert f"Merge origin/main into ENG-1-{CHANGE}" in log


class TestDesignAuditSpecIssue:
    GAP = {
        "category": "spec-feasibility",
        "severity": "major",
        "description": "Behavior needs a store no requirement asks for",
        "suggestion": "add a Store requirement",
        "needs_requirements_change": True,
        "requirement": "requirements/fixture.md: Behavior",
    }

    @pytest.mark.asyncio
    async def test_S11_an_audit_gap_needing_a_requirements_change_stops_for_pm_spec(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        models = Models(audit=[[self.GAP], []])

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        worktree = _worktree(tmp_path)
        built = worktree / "specs" / "changes" / CHANGE
        assert rc == 4
        assert f"SPEC:ISSUES:{built / 'spec-issues.md'}" in out
        text = (built / "spec-issues.md").read_text()
        assert "## Design audit: requirements/fixture.md: Behavior" in text
        assert "Suggested change: add a Store requirement" in text
        state = BuildState.load(built / ".build-state.json")
        step = state.spec_gen.steps["design_audit"]
        assert step.outcome == "blocked"
        assert models.generate.await_count == 2, (
            "no rewrite of design or tasks was paid for"
        )

        _revise(worktree, built)
        asked = len(models.prompts)
        assert await _build(repo, models) == 0, capsys.readouterr().err
        assert "### Requirement: Store" in models.prompts[asked], (
            "the design is stale and written again"
        )
        assert BuildState.load(
            next(built.glob(".build-state*.json"))
        ).spec_gen.step_passed("design_audit")

    @pytest.mark.asyncio
    async def test_an_audit_gap_the_design_can_fix_is_not_a_spec_issue(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        gap = {**self.GAP, "needs_requirements_change": False}
        models = Models(
            audit=[
                [gap],
                AuditFindings(
                    [], [PriorAssessment(finding_id="DA-F1", status="resolved")]
                ),
            ]
        )
        assert await _build(repo, models) == 0, capsys.readouterr().err
        assert not (
            _worktree(tmp_path) / "specs" / "changes" / CHANGE / "spec-issues.md"
        ).exists()


CONTRADICTION = (
    "STATUS: DONE\nSURPRISES: the value cannot be returned without a store\n"
    "SPEC_IMPACT: contradicts\n"
)


class TestTddContradiction:
    @staticmethod
    def _engine(contradict: bool = True):
        """The TDD phase: one block whose implementer reports a contradiction,
        recorded by the real `_record_implementation_note`."""
        from build_pipeline.progress import ProgressWriter
        from build_pipeline.tdd_engine import (
            EXIT_BUILD_FAILURE,
            _record_implementation_note,
        )

        async def engine(config, args, standalone=True):
            path = config.state_file_path()
            state = BuildState.load(path)
            state.tdd = state.tdd or TDDState(
                blocks=[
                    BlockInfo(
                        number=1,
                        name="Behavior",
                        description="d",
                        satisfies=["fixture: Behavior"],
                    )
                ]
            )
            state.checkpoint(path)
            config.gates.respec_halt_on_contradiction = True
            ok = _record_implementation_note(
                state.tdd.blocks[0],
                config,
                state,
                ProgressWriter(config.progress_file_path()),
                "implementer",
                CONTRADICTION if contradict else "",
            )
            return 0 if ok else EXIT_BUILD_FAILURE

        return engine

    @pytest.mark.asyncio
    async def test_S12_a_contradiction_under_halt_stops_for_pm_spec(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        with patch("build_pipeline.tdd_engine.run_tdd_engine", self._engine()):
            rc = await _build(repo, Models(), spec_only=False)

        out = capsys.readouterr().out
        worktree = _worktree(tmp_path)
        built = worktree / "specs" / "changes" / CHANGE
        assert rc == 4
        assert f"SPEC:ISSUES:{built / 'spec-issues.md'}" in out
        text = (built / "spec-issues.md").read_text()
        assert "## Block 1 (Behavior): implementer SPEC_IMPACT: contradicts" in text
        assert "Satisfies: fixture: Behavior" in text
        assert "cannot be returned without a store" in text
        state = BuildState.load(built / ".build-state.json")
        assert state.phase != BuildPhase.FAILED and state.tdd is not None

        # While spec_issues is set, the build refuses as S11.
        assert await _build(repo, Models(), spec_only=False) == 4

        # pm-spec revise changed the requirements after TDD began.
        _revise(worktree, built)
        with patch(
            "build_pipeline.tdd_engine.run_tdd_engine", self._engine(contradict=False)
        ):
            rc = await _build(repo, Models(), spec_only=False)
        err = capsys.readouterr().err
        assert rc == 1
        assert "Planning inputs changed after implementation" in err


# --- old checkpoints (item 21, row S13) -------------------------------------------


class TestOldCheckpoints:
    @staticmethod
    def _old(change_dir: Path, fixture: str) -> None:
        raw = json.loads((FIXTURES / fixture).read_text())
        raw.update(change_name=CHANGE, state_file=str(change_dir / ".build-state.json"))
        (change_dir / ".build-state.json").write_text(json.dumps(raw))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fixture", ["build-state-pre-docs-update.json", "build-state-pre-findings.json"]
    )
    async def test_S13_past_spec_generation_with_specs_on_disk_advises_pm_spec_resume(
        self, repo, tmp_path, capsys, fixture
    ):
        change_dir = write_specs(_source_change(repo))
        self._old(change_dir, fixture)
        assert BuildState.load(change_dir / ".build-state.json").is_phase_complete(
            BuildPhase.SPEC_GENERATION
        )
        before = _tree(change_dir)

        rc = await _build(repo, Models())

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:no .change.yaml" in out
        assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
        assert _tree(change_dir) == before
        assert not (tmp_path / "ws").exists()

    @pytest.mark.asyncio
    async def test_S13_before_spec_generation_advises_pm_spec(
        self, repo, tmp_path, capsys
    ):
        change_dir = write_specs(_source_change(repo))
        self._old(change_dir, "build-state-pre-codebase-analysis.json")
        before = _tree(change_dir)

        rc = await _build(repo, Models())

        out = capsys.readouterr().out
        assert rc == 2
        assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out
        assert "pm-spec resume" not in out
        assert _tree(change_dir) == before

    @pytest.mark.asyncio
    async def test_past_spec_generation_without_requirements_advises_pm_spec(
        self, repo, capsys
    ):
        change_dir = _source_change(repo)
        change_dir.mkdir(parents=True)
        self._old(change_dir, "build-state-pre-docs-update.json")
        rc = await _build(repo, Models())
        out = capsys.readouterr().out
        assert rc == 2
        assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fixture, line",
        [
            (
                "build-state-pre-codebase-analysis.json",
                "pm-spec {change} --project-dir {wt} first",
            ),
            (
                "build-state-pre-findings.json",
                "pm-spec resume {change} --project-dir {wt} first",
            ),
        ],
    )
    async def test_S13_a_checkpoint_in_the_builds_worktree_names_the_worktree(
        self, repo, tmp_path, capsys, fixture, line
    ):
        """The old checkpoint is in the build's worktree, whose copy of the
        specs has no ready record, while the user's checkout is ready: the
        advice names `--project-dir <worktree>`, and a pm-spec run there
        (played by marking that copy ready and committing it) lets the build
        go on. Run twice, the refusal is the same and changes nothing."""
        _handoff(repo)
        assert await _build(repo, Models(design=[f"# Design\n\n{ISSUE_LINE}\n"])) == 4
        wt = _worktree(tmp_path)
        built = wt / "specs" / "changes" / CHANGE
        raw = json.loads((FIXTURES / fixture).read_text())
        raw.update(
            change_name=CHANGE,
            state_file=str(built / ".build-state.json"),
            worktree_path=str(wt),
            source_repo=str(repo),
            branch=f"buildme/{CHANGE}",
        )
        (built / ".build-state.json").write_text(json.dumps(raw))
        (built / readiness.ISSUES_FILE).unlink()
        (built / ".change.yaml").write_text("{}\n")
        _git(wt, "add", "-A", "specs")
        _git(wt, "commit", "-qm", "an old build's change dir")
        capsys.readouterr()
        before = _tree(built)

        for _ in range(2):
            rc = await _build(repo, Models())
            out = capsys.readouterr().out
            assert rc == 2, out
            assert (
                "SPEC:NOT_READY:no spec_status" in out
                or "SPEC:NOT_READY:spec_status is not set" in out
            )
            assert f"Run /multiplai-pm:{line.format(change=CHANGE, wt=wt)}" in out
            assert _tree(built) == before

        if fixture == "build-state-pre-codebase-analysis.json":
            mark_ready(built)
            assert commit_specs(wt, built, "specs: ready (pm-spec in the worktree)")
            models = Models()
            rc = await _build(repo, models)
            out = capsys.readouterr().out
            assert rc == 0, out
            assert "SPEC:NOT_READY" not in out
            assert (built / "design.md").is_file() and (built / "tasks.md").is_file()


# --- the copy the build reads: --base-ref and a resumed worktree (S7, S10) ----


class TestTheCopyTheBuildReads:
    @pytest.mark.asyncio
    async def test_S10_a_base_ref_holding_specs_never_marked_ready_refuses(
        self, repo, tmp_path, capsys
    ):
        """The checkout's specs are ready, but the worktree is cut from
        `--base-ref`, whose copy is not: refused before the worktree is cut."""
        change_dir = write_specs(_source_change(repo))
        commit_specs(repo, change_dir, "specs: draft, never marked ready")
        draft = _git(repo, "rev-parse", "HEAD")
        ready_specs(change_dir, repo)
        models = Models()

        rc = await _build(repo, models, base_ref=draft)

        out = capsys.readouterr().out
        assert rc == 2, out
        assert f"SPEC:NOT_READY:specs/changes/{CHANGE} at {draft}" in out
        assert "no .change.yaml" in out
        assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_base_ref_holding_the_ready_specs_builds(
        self, repo, tmp_path, capsys
    ):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        rc = await _build(repo, Models(), base_ref=_git(repo, "rev-parse", "HEAD"))
        assert rc == 0, capsys.readouterr().out
        assert (
            _worktree(tmp_path) / "specs" / "changes" / CHANGE / "design.md"
        ).is_file()

    @staticmethod
    async def _failed_at_tasks(repo: Path, models: Models) -> None:
        real = models.generate.side_effect
        failed = []

        async def once(artifact_id, *a, **k):
            if artifact_id == "tasks" and not failed:
                failed.append(artifact_id)
                raise RuntimeError("stop at tasks")
            return await real(artifact_id, *a, **k)

        models.generate.side_effect = once
        assert await _build(repo, models) == 1

    @pytest.mark.asyncio
    async def test_S7_specs_revised_on_the_feature_branch_refuse_a_resume_naming_it(
        self, repo, tmp_path, capsys
    ):
        """pm-spec revised and pushed the specs on the feature branch after
        the build's worktree was cut: the resume refuses and names the branch,
        instead of building its older copy. Following the advice (merge the
        branch into the build's) lets it go on, from them."""
        _handoff(repo)
        models = Models()
        await self._failed_at_tasks(repo, models)
        wt = _worktree(tmp_path)
        built = wt / "specs" / "changes" / CHANGE
        _revise_on_branch(repo, tmp_path / "pm-spec")
        capsys.readouterr()
        asked = len(models.prompts)
        before = _tree(built)

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2, out
        assert (
            f"SPEC:NOT_READY:the specs in origin/ENG-1-{CHANGE} were marked ready again"
            in out
        )
        assert (
            f"Merge origin/ENG-1-{CHANGE} into the build's branch in {wt} "
            f"(`git -C {wt} merge origin/ENG-1-{CHANGE}`)" in out
        )
        assert len(models.prompts) == asked and _tree(built) == before

        _git(wt, "merge", "-q", "--no-edit", f"origin/ENG-1-{CHANGE}")
        rc = await _build(repo, models)
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Requirement: Store" in "\n".join(models.prompts[asked:])

    @pytest.mark.asyncio
    async def test_S7_specs_revised_in_the_worktree_resume_from_the_worktree(
        self, repo, tmp_path, capsys
    ):
        """The other way round: the worktree's copy was revised (`pm-spec
        revise --project-dir <worktree>`) and the feature branch is as the
        worktree was cut from it. Nothing is newer elsewhere, so the resume
        goes on."""
        _handoff(repo)
        models = Models()
        await self._failed_at_tasks(repo, models)
        built = _worktree(tmp_path) / "specs" / "changes" / CHANGE
        _revise(_worktree(tmp_path), built)
        asked = len(models.prompts)

        rc = await _build(repo, models)

        assert rc == 0, capsys.readouterr().out
        assert "Requirement: Store" in "\n".join(models.prompts[asked:])


# --- the pieces --------------------------------------------------------------------


class TestSpecIssuesFile:
    def test_sections_are_appended_once_and_spec_issues_is_set(self, tmp_path):
        change_dir = write_specs(tmp_path / "specs" / "changes" / CHANGE)
        path = readiness.write_issues(change_dir, [("Design: a", "one")], change=CHANGE)
        readiness.write_issues(
            change_dir, [("Design: a", "one"), ("Design: b", "two")], change=CHANGE
        )
        text = path.read_text()
        assert text.startswith(f"# Spec issues: {CHANGE}\n")
        assert f"/multiplai-pm:pm-spec revise {CHANGE}" in text
        assert text.count("## Design: a") == 1 and text.count("## Design: b") == 1
        assert read_change_meta(change_dir)["spec_issues"] == "spec-issues.md"
        assert readiness.open_issues(change_dir) == path

    def test_a_title_cannot_start_a_second_section(self, tmp_path):
        change_dir = write_specs(tmp_path / "c")
        path = readiness.write_issues(change_dir, [("a\n## b", "x")], change="c")
        assert path.read_text().count("\n## ") == 1

    def test_design_spec_issue_lines_are_parsed(self):
        from build_pipeline.spec_generator import design_spec_issues

        text = f"# Design\n\nBody mentions SPEC_ISSUE: in prose.\n`{ISSUE_LINE}`\nSPEC_ISSUE:\n"
        issues = design_spec_issues(text)
        assert [t for t, _ in issues] == ["Design: requirements/fixture.md: Behavior"]
        assert "add a requirement for the store" in issues[0][1]
        assert design_spec_issues("# Design\nNo issues.\n") == []

    def test_only_actionable_gaps_flagged_for_the_requirements_are_spec_issues(self):
        from build_pipeline.spec_generator import audit_spec_issues

        gap = TestDesignAuditSpecIssue.GAP
        assert [t for t, _ in audit_spec_issues([gap])] == [
            "Design audit: requirements/fixture.md: Behavior"
        ]
        assert audit_spec_issues([{**gap, "severity": "minor"}]) == []
        assert audit_spec_issues([{**gap, "needs_requirements_change": False}]) == []
        assert audit_spec_issues([{**gap, "needs_requirements_change": "yes"}]) == []

    def test_the_design_prompt_asks_for_spec_issue_lines(self):
        from build_pipeline.prompts.spec_generation import (
            DESIGN_PROMPT,
            SPEC_ISSUE_PREFIX,
        )

        assert (
            f"`{SPEC_ISSUE_PREFIX} requirements/<capability>.md: <requirement name>:"
            in DESIGN_PROMPT
        )
        assert "Flag any spec requirements that seem infeasible" not in DESIGN_PROMPT

    def test_the_design_audit_prompt_asks_whether_a_fix_needs_the_requirements(self):
        from build_pipeline.prompts.design_audit import DESIGN_AUDIT_PROMPT

        assert '"needs_requirements_change": false' in DESIGN_AUDIT_PROMPT


def test_buildme_never_imports_pm_spec_pipeline():
    """The two tools share only multiplai_core.specs and the change dir's files."""
    root = Path(__file__).resolve().parent.parent / "build_pipeline"
    for path in root.rglob("*.py"):
        assert "pm_spec_pipeline" not in path.read_text(), path


class TestBranchNames:
    """T10 and T11 of plan-spec-feature-branch-2026-10-22.md, in buildme:
    the branches it makes are `<TICKET>-<change>`, the ticket the change's
    own ref, else its nearest parent's."""

    @staticmethod
    def _push_as(repo: Path, branch: str) -> None:
        _git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        _git(repo, "fetch", "-q", "origin")

    @pytest.mark.asyncio
    async def test_T10_several_feature_branches_for_the_change_refuse_naming_branch(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        # A second handoff of the change, under another ticket.
        at = tmp_path / "second"
        _git(repo, "worktree", "add", "-q", "--detach", str(at), "HEAD")
        meta = at / "specs" / "changes" / CHANGE / ".change.yaml"
        data = yaml.safe_load(meta.read_text())
        data["ticket_refs"] = {"epic": "ENG-2", "children": []}
        meta.write_text(yaml.safe_dump(data))
        _git(at, "commit", "-qam", "specs: under ENG-2")
        self._push_as(at, f"ENG-2-{CHANGE}")
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2
        assert (
            f"origin has 2 branches named <TICKET>-{CHANGE}: ENG-1-{CHANGE}, ENG-2-{CHANGE}"
            in out
        )
        assert "--branch" in out
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_origin_branch_not_holding_the_change_is_not_its_feature_branch(
        self, repo, tmp_path, capsys
    ):
        """An origin branch whose name merely ends in `-<change>` (another
        change's, here `ENG-40-auth-feat`, cut before these specs existed) is
        not the change's feature branch: the build neither builds from it nor
        merges into it, and refuses the specs ready only in the checkout."""
        other = f"ENG-40-auth-{CHANGE}"
        self._push_as(repo, other)
        before = _git(repo, "rev-parse", f"origin/{other}")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "main moves")
        _git(repo, "push", "-q", "origin", "main")
        ready_specs(_source_change(repo), repo)
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2
        assert "on no feature branch" in out
        _git(repo, "fetch", "-q", "origin")
        assert _git(repo, "rev-parse", f"origin/{other}") == before
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_T10_a_branch_flag_without_the_ticket_prefix_is_refused(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        self._push_as(repo, f"wip-{CHANGE}")
        models = Models()

        rc = await _build(repo, models, feature_branch=f"wip-{CHANGE}")

        out = capsys.readouterr().out
        assert rc == 2
        assert (
            f"--branch wip-{CHANGE} does not start with the change's ticket id (ENG-1-)"
            in out
        )
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_T11_specs_with_no_ticket_ref_refuse_naming_ticket_ref(
        self, repo, tmp_path, capsys
    ):
        change_dir = write_specs(_source_change(repo))
        mark_ready(change_dir)
        commit_specs(repo, change_dir)
        self._push_as(repo, f"X-{CHANGE}")
        models = Models()

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2
        assert "records no ticket ref" in out
        assert f"pm-spec resume {CHANGE} --ticket-ref <ticket id>" in out
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @staticmethod
    def _epic(repo: Path) -> str:
        """An epic `auth` with ticket ENG-40 on origin's `ENG-40-auth`: child
        `auth-a` has no ticket of its own, child `auth-b` has ENG-41, and
        `auth-c` has 'ENG[3'. Returns the feature branch."""
        from multiplai_core.specs import write_change_meta

        changes = repo / "specs" / "changes"
        parent = changes / "auth"
        parent.mkdir(parents=True)
        write_change_meta(
            parent,
            {
                "ticket_refs": {"epic": "ENG-40", "children": []},
                "children": ["auth-a", "auth-b", "auth-c"],
            },
        )
        for cid, ticket in (("auth-a", ""), ("auth-b", "ENG-41"), ("auth-c", "ENG[3")):
            meta = {"parent": "auth"}
            if ticket:
                meta["ticket_refs"] = {"epic": ticket, "children": []}
            (changes / cid).mkdir()
            write_change_meta(changes / cid, meta)
            ready_specs(changes / cid, repo, commit=False)
        _git(repo, "add", "specs")
        _git(repo, "commit", "-qm", "specs: auth")
        branch = "ENG-40-auth"
        TestBranchNames._push_as(repo, branch)
        _git(repo, "reset", "-q", "--hard", "HEAD~1")
        return branch

    @staticmethod
    def _child_config(repo: Path, cid: str, branch: str) -> BuildConfig:
        config = _config(repo, change_name=cid)
        config.epic_child = True
        config.feature_branch = branch  # the coordinator's --branch
        config.base_ref = f"origin/{branch}"
        return config

    @pytest.mark.parametrize(
        ("cid", "expected"), [("auth-a", "ENG-40-auth-a"), ("auth-b", "ENG-41-auth-b")]
    )
    def test_T10_a_childs_build_branch_is_its_ticket_else_its_parents(
        self, repo, cid, expected
    ):
        from build_pipeline import orchestrator

        branch = self._epic(repo)
        config = self._child_config(repo, cid, branch)
        state = BuildState(
            change_name=cid, tier="advanced", state_file=str(config.state_file_path())
        )

        assert orchestrator._find_feature_branch(config) is None
        orchestrator._setup_worktree(config, state)

        assert state.branch == expected
        assert _git(Path(state.worktree_path), "branch", "--show-current") == expected

    def test_T10_a_ticket_git_refuses_in_a_branch_name_is_refused_with_the_reason(
        self, repo
    ):
        from build_pipeline import git_ops, orchestrator

        branch = self._epic(repo)
        config = self._child_config(repo, "auth-c", branch)
        state = BuildState(
            change_name="auth-c",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        orchestrator._find_feature_branch(config)

        with pytest.raises(git_ops.GitLifecycleError) as err:
            orchestrator._setup_worktree(config, state)

        assert (
            "the ticket id 'ENG[3' cannot start the branch name 'ENG[3-auth-c': it holds "
            "'[', which git refuses in a branch name" in str(err.value)
        )
        assert "--ticket-ref" in str(err.value)
        assert "ENG[3-auth-c" not in _git(repo, "branch", "--list")


class TestSingleChangeCheckout:
    """A single change builds on its feature branch itself (Decision 3), so
    the build refuses to take it from somebody's checkout or work."""

    @pytest.mark.asyncio
    async def test_the_feature_branch_checked_out_elsewhere_refuses(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        mine = tmp_path / "mine"
        _git(repo, "worktree", "add", "-q", str(mine), f"ENG-1-{CHANGE}")
        models = Models()

        rc = await _build(repo, models)

        err = capsys.readouterr().err
        assert rc == 1
        assert f"ENG-1-{CHANGE} is checked out in {mine}" in err
        assert _git(mine, "branch", "--show-current") == f"ENG-1-{CHANGE}"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_local_feature_branch_ahead_of_origin_refuses(
        self, repo, tmp_path, capsys
    ):
        _handoff(repo)
        _git(repo, "branch", "-q", f"ENG-1-{CHANGE}", f"origin/ENG-1-{CHANGE}")
        mine = tmp_path / "mine"
        _git(repo, "worktree", "add", "-q", str(mine), f"ENG-1-{CHANGE}")
        (mine / "local.txt").write_text("not pushed\n")
        _git(mine, "add", "local.txt")
        _git(mine, "commit", "-qm", "local work")
        _git(repo, "worktree", "remove", str(mine))
        ahead = _git(repo, "rev-parse", f"ENG-1-{CHANGE}")
        models = Models()

        rc = await _build(repo, models)

        err = capsys.readouterr().err
        assert rc == 1
        assert f"The local branch ENG-1-{CHANGE} holds commits" in err
        assert _git(repo, "rev-parse", f"ENG-1-{CHANGE}") == ahead
        models.generate.assert_not_awaited()
