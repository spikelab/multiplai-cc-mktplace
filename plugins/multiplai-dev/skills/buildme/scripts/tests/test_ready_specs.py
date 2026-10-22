"""buildme builds only from pm-spec's ready specs (PR 4 of the pm-spec split).

Rows S10-S15 of the plan's stop/resume table, the start refusal, old
checkpoints (item 21) and the three ways a build sends a requirements problem
back to pm-spec (item 20). Every model call is a fake; the repos and worktrees
are real temp repos. pm-spec's own steps (marking ready, `revise`) are played
with `multiplai_core.specs`, the only code the two tools share.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
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

from tests.spec_fixtures import REQUIREMENT, commit_specs, ready_specs, write_specs
from tests.test_git_ops import make_repo

pytestmark = pytest.mark.no_handoff

CHANGE = "feat"
FIXTURES = Path(__file__).parent / "fixtures"
ISSUE_LINE = ("SPEC_ISSUE: requirements/fixture.md: Behavior: a value cannot be returned "
              "without a store; add a requirement for the store")
REVISED = REQUIREMENT + "\n### Requirement: Store\nThe system SHALL keep the value.\n\n" \
    "#### Scenario: Kept\n- **WHEN** a value is set\n- **THEN** it is kept\n"


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    from build_pipeline import orchestrator
    monkeypatch.setattr(orchestrator, "_run_spec_extract_step", lambda config, state, progress: None)
    return make_repo(tmp_path / "proj")


def _source_change(repo: Path) -> Path:
    return repo / "specs" / "changes" / CHANGE


def _worktree(tmp_path: Path) -> Path:
    return tmp_path / "ws" / ".worktrees" / f"buildme-{CHANGE}"


def _config(repo: Path, **over) -> BuildConfig:
    fields = dict(project_dir=repo, change_name=CHANGE, auto=True, spec_only=True,
                  skip_research=True, skip_explainers=True,
                  git=GitToggles(worktree=True, push=False, pr="none"))
    fields.update(over)
    config = BuildConfig(**fields)
    config.specs_dir = repo / "specs"
    config.test_command = "true"
    return config


def _args(repo: Path, **over) -> argparse.Namespace:
    base = dict(change=CHANGE, project_dir=str(repo), auto=True, spec_only=True,
                skip_research=True, research_path="", session_id="", accept_size=False,
                force=False, block=None)
    base.update(over)
    return argparse.Namespace(**base)


class Models:
    """The planning steps' model calls, as fakes that count what they were asked."""

    def __init__(self, design: list[str] | None = None, audit: list[list[dict]] | None = None):
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
            patch("build_pipeline.llm_steps.spec_steps.generate_artifact", self.generate),
            patch("build_pipeline.llm_steps.spec_steps.run_tasks_audit", AsyncMock(return_value=[])),
            patch("build_pipeline.llm_steps.spec_steps.run_design_audit", self.design_audit),
            patch("build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                  AsyncMock(return_value=PlanReviewResult(findings=[]))),
            patch("build_pipeline.spec_generator.generate_rubric",
                  AsyncMock(return_value="# Rubric\nCorrect value\n")),
        )


async def _build(repo: Path, models: Models, **config_over) -> int:
    p = models.patches()
    with p[0], p[1], p[2], p[3], p[4]:
        return await run_orchestrator(_config(repo, **config_over),
                                      _args(repo, spec_only=config_over.get("spec_only", True)))


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
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


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
    async def test_S10_specs_never_marked_ready_refuse_with_pm_spec_resume(self, repo, tmp_path, capsys):
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
            self, repo, tmp_path, capsys):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        (change_dir / "requirements" / "fixture.md").write_text(REVISED)
        state = BuildState(change_name=CHANGE, tier="advanced", phase=BuildPhase.RESEARCH,
                           state_file=str(change_dir / ".build-state.json"))
        state.checkpoint()
        before = _tree(change_dir)

        rc = await _build(repo, Models())

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:the specs changed after they were marked ready" in out
        assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out
        assert _tree(change_dir) == before

    @pytest.mark.asyncio
    async def test_ready_but_uncommitted_specs_refuse_a_worktree_build(self, repo, tmp_path, capsys):
        """The worktree branches from HEAD, which would not hold them."""
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo, commit=False)

        rc = await _build(repo, Models())

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:" in out and "uncommitted changes" in out
        assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
        assert not (tmp_path / "ws").exists()

    @pytest.mark.asyncio
    async def test_ready_uncommitted_specs_build_in_place_with_no_worktree(self, repo, capsys):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo, commit=False)
        rc = await _build(repo, Models(), git=GitToggles(worktree=False, push=False, pr="none"))
        assert rc == 0, capsys.readouterr().err
        assert (change_dir / "design.md").is_file()

    @pytest.mark.asyncio
    async def test_ready_committed_specs_build_and_buildme_never_writes_them(self, repo, tmp_path, capsys):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        what = {name: (change_dir / name).read_bytes()
                for name in ("proposal.md", "use-cases.md", "requirements/fixture.md")}
        models = Models()

        rc = await _build(repo, models)

        assert rc == 0, capsys.readouterr().err
        built = _worktree(tmp_path) / "specs" / "changes" / CHANGE
        for name, data in what.items():
            assert (built / name).read_bytes() == data, name
        assert (built / "design.md").is_file() and (built / "tasks.md").is_file()
        generated = [c.args[0] for c in models.generate.await_args_list]
        assert not set(generated) & {"proposal", "use-cases", "requirements"}


# --- requirements problems go back to pm-spec (item 20, rows S11, S12, S15) ----

class TestDesignSpecIssue:
    @pytest.mark.asyncio
    async def test_S11_a_design_spec_issue_stops_and_resumes_after_revise(self, repo, tmp_path, capsys):
        ready_specs(_source_change(repo), repo)
        models = Models(design=[f"# Design\n\nDraft.\n\n{ISSUE_LINE}\n",
                                "# Design\n\n## Decisions\nKeep the value in a store.\n"])

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        worktree = _worktree(tmp_path)
        built = worktree / "specs" / "changes" / CHANGE
        issues = built / "spec-issues.md"
        assert rc == readiness.EXIT_SPEC_ISSUES == 4
        assert f"SPEC:ISSUES:{issues}" in out
        assert f"Run /multiplai-pm:pm-spec revise {CHANGE} --project-dir {worktree}" in out
        text = issues.read_text()
        assert text.count("\n## ") == 1
        assert "## Design: requirements/fixture.md: Behavior" in text
        assert "add a requirement for the store" in text
        assert read_change_meta(built)["spec_issues"] == "spec-issues.md"
        assert not (built / "design.md").exists(), "a design with spec issues is not published"
        state = BuildState.load(built / ".build-state.json")
        assert state.phase != BuildPhase.FAILED
        assert not state.is_phase_complete(BuildPhase.SPEC_GENERATION)

        _revise(worktree, built)
        asked = len(models.prompts)
        rc = await _build(repo, models)
        assert rc == 0, capsys.readouterr().err
        assert "Keep the value in a store" in (built / "design.md").read_text()
        assert "### Requirement: Store" in models.prompts[asked], "the design reads the revised requirements"
        assert not (built / "spec-issues.md").exists()

    @pytest.mark.asyncio
    async def test_S15_a_stopped_design_step_stopped_twice_gives_the_same_refusal_and_spends_nothing(
            self, repo, tmp_path, capsys):
        ready_specs(_source_change(repo), repo)
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
            self, repo, tmp_path, capsys):
        """Both tools hash files, not git refs: a commit on the source's main
        after the stop neither refuses the resume nor reaches the build."""
        ready_specs(_source_change(repo), repo)
        models = Models(design=[f"# Design\n\n{ISSUE_LINE}\n", "# Design\n\n## Decisions\nStore.\n"])
        assert await _build(repo, models) == 4
        (repo / "LATER.md").write_text("later\n")
        _git(repo, "add", "LATER.md")
        _git(repo, "commit", "-qm", "later on main")
        worktree = _worktree(tmp_path)
        _revise(worktree, worktree / "specs" / "changes" / CHANGE)
        capsys.readouterr()

        assert await _build(repo, models) == 0, capsys.readouterr().err
        assert "later on main" not in _git(worktree, "log", "--format=%s")


class TestDesignAuditSpecIssue:
    GAP = {"category": "spec-feasibility", "severity": "major",
           "description": "Behavior needs a store no requirement asks for",
           "suggestion": "add a Store requirement", "needs_requirements_change": True,
           "requirement": "requirements/fixture.md: Behavior"}

    @pytest.mark.asyncio
    async def test_S11_an_audit_gap_needing_a_requirements_change_stops_for_pm_spec(
            self, repo, tmp_path, capsys):
        ready_specs(_source_change(repo), repo)
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
        assert models.generate.await_count == 2, "no rewrite of design or tasks was paid for"

        _revise(worktree, built)
        asked = len(models.prompts)
        assert await _build(repo, models) == 0, capsys.readouterr().err
        assert "### Requirement: Store" in models.prompts[asked], "the design is stale and written again"
        assert BuildState.load(next(built.glob(".build-state*.json"))).spec_gen.step_passed("design_audit")

    @pytest.mark.asyncio
    async def test_an_audit_gap_the_design_can_fix_is_not_a_spec_issue(self, repo, tmp_path, capsys):
        ready_specs(_source_change(repo), repo)
        gap = {**self.GAP, "needs_requirements_change": False}
        models = Models(audit=[[gap], AuditFindings(
            [], [PriorAssessment(finding_id="DA-F1", status="resolved")])])
        assert await _build(repo, models) == 0, capsys.readouterr().err
        assert not (_worktree(tmp_path) / "specs" / "changes" / CHANGE / "spec-issues.md").exists()


CONTRADICTION = ("STATUS: DONE\nSURPRISES: the value cannot be returned without a store\n"
                 "SPEC_IMPACT: contradicts\n")


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
            state.tdd = state.tdd or TDDState(blocks=[BlockInfo(number=1, name="Behavior",
                                                                description="d",
                                                                satisfies=["fixture: Behavior"])])
            state.checkpoint(path)
            config.gates.respec_halt_on_contradiction = True
            ok = _record_implementation_note(state.tdd.blocks[0], config, state,
                                             ProgressWriter(config.progress_file_path()),
                                             "implementer", CONTRADICTION if contradict else "")
            return 0 if ok else EXIT_BUILD_FAILURE
        return engine

    @pytest.mark.asyncio
    async def test_S12_a_contradiction_under_halt_stops_for_pm_spec(self, repo, tmp_path, capsys):
        ready_specs(_source_change(repo), repo)
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
        with patch("build_pipeline.tdd_engine.run_tdd_engine", self._engine(contradict=False)):
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
    @pytest.mark.parametrize("fixture", ["build-state-pre-docs-update.json",
                                         "build-state-pre-findings.json"])
    async def test_S13_past_spec_generation_with_specs_on_disk_advises_pm_spec_resume(
            self, repo, tmp_path, capsys, fixture):
        change_dir = write_specs(_source_change(repo))
        self._old(change_dir, fixture)
        assert BuildState.load(change_dir / ".build-state.json").is_phase_complete(BuildPhase.SPEC_GENERATION)
        before = _tree(change_dir)

        rc = await _build(repo, Models())

        out = capsys.readouterr().out
        assert rc == 2
        assert "SPEC:NOT_READY:no .change.yaml" in out
        assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
        assert _tree(change_dir) == before
        assert not (tmp_path / "ws").exists()

    @pytest.mark.asyncio
    async def test_S13_before_spec_generation_advises_pm_spec(self, repo, tmp_path, capsys):
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
    async def test_past_spec_generation_without_requirements_advises_pm_spec(self, repo, capsys):
        change_dir = _source_change(repo)
        change_dir.mkdir(parents=True)
        self._old(change_dir, "build-state-pre-docs-update.json")
        rc = await _build(repo, Models())
        out = capsys.readouterr().out
        assert rc == 2
        assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fixture, line", [
        ("build-state-pre-codebase-analysis.json", "pm-spec {change} --project-dir {wt} first"),
        ("build-state-pre-findings.json", "pm-spec resume {change} --project-dir {wt} first"),
    ])
    async def test_S13_a_checkpoint_in_the_builds_worktree_names_the_worktree(
            self, repo, tmp_path, capsys, fixture, line):
        """The old checkpoint is in the build's worktree, whose copy of the
        specs has no ready record, while the user's checkout is ready: the
        advice names `--project-dir <worktree>`, and a pm-spec run there
        (played by marking that copy ready and committing it) lets the build
        go on. Run twice, the refusal is the same and changes nothing."""
        ready_specs(_source_change(repo), repo)
        assert await _build(repo, Models(design=[f"# Design\n\n{ISSUE_LINE}\n"])) == 4
        wt = _worktree(tmp_path)
        built = wt / "specs" / "changes" / CHANGE
        raw = json.loads((FIXTURES / fixture).read_text())
        raw.update(change_name=CHANGE, state_file=str(built / ".build-state.json"),
                   worktree_path=str(wt), source_repo=str(repo), branch=f"buildme/{CHANGE}")
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
            assert "SPEC:NOT_READY:no spec_status" in out or "SPEC:NOT_READY:spec_status is not set" in out
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
    async def test_S10_a_base_ref_holding_specs_never_marked_ready_refuses(self, repo, tmp_path, capsys):
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
        assert f"SPEC:NOT_READY:specs/changes/{CHANGE} at --base-ref {draft}" in out
        assert "no .change.yaml" in out
        assert "Build from a --base-ref that holds the ready specs" in out
        assert not (tmp_path / "ws").exists(), "no worktree"
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_S10_a_base_ref_holding_an_older_ready_revision_refuses(self, repo, tmp_path, capsys):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        older = _git(repo, "rev-parse", "HEAD")
        _revise(repo, change_dir)
        models = Models()

        rc = await _build(repo, models, base_ref=older)

        out = capsys.readouterr().out
        assert rc == 2, out
        assert "holds other specs than the ones marked ready in" in out
        assert not (tmp_path / "ws").exists()
        models.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_base_ref_holding_the_ready_specs_builds(self, repo, tmp_path, capsys):
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        rc = await _build(repo, Models(), base_ref=_git(repo, "rev-parse", "HEAD"))
        assert rc == 0, capsys.readouterr().out
        assert (_worktree(tmp_path) / "specs" / "changes" / CHANGE / "design.md").is_file()

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
    async def test_S7_specs_revised_in_the_checkout_refuse_a_resume_naming_them(self, repo, tmp_path, capsys):
        """pm-spec revised and marked ready the specs in the user's checkout
        after the build's worktree was cut: the resume refuses and names
        where the newer specs are, instead of building its older copy.
        Following the advice (copy them in, commit) lets it go on, from them."""
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
        models = Models()
        await self._failed_at_tasks(repo, models)
        built = _worktree(tmp_path) / "specs" / "changes" / CHANGE
        _revise(repo, change_dir)
        capsys.readouterr()
        asked = len(models.prompts)
        before = _tree(built)

        rc = await _build(repo, models)

        out = capsys.readouterr().out
        assert rc == 2, out
        assert f"SPEC:NOT_READY:the specs in {change_dir} were marked ready again" in out
        assert f"Copy proposal.md, use-cases.md, requirements/ and .change.yaml from {change_dir} " \
               f"into {built}, commit them there" in out
        assert len(models.prompts) == asked and _tree(built) == before

        for name in ("proposal.md", "use-cases.md", "requirements/fixture.md", ".change.yaml"):
            (built / name).write_bytes((change_dir / name).read_bytes())
        assert commit_specs(_worktree(tmp_path), built, "specs: from the checkout")
        rc = await _build(repo, models)
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Requirement: Store" in "\n".join(models.prompts[asked:])

    @pytest.mark.asyncio
    async def test_S7_specs_revised_in_the_worktree_resume_from_the_worktree(self, repo, tmp_path, capsys):
        """The other way round: the worktree's copy was revised (`pm-spec
        revise --project-dir <worktree>`) and the checkout is as the worktree
        was cut from it. Nothing is newer elsewhere, so the resume goes on."""
        change_dir = _source_change(repo)
        ready_specs(change_dir, repo)
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
        readiness.write_issues(change_dir, [("Design: a", "one"), ("Design: b", "two")], change=CHANGE)
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
            "Design audit: requirements/fixture.md: Behavior"]
        assert audit_spec_issues([{**gap, "severity": "minor"}]) == []
        assert audit_spec_issues([{**gap, "needs_requirements_change": False}]) == []
        assert audit_spec_issues([{**gap, "needs_requirements_change": "yes"}]) == []

    def test_the_design_prompt_asks_for_spec_issue_lines(self):
        from build_pipeline.prompts.spec_generation import (
            DESIGN_PROMPT,
            SPEC_ISSUE_PREFIX,
        )
        assert f"`{SPEC_ISSUE_PREFIX} requirements/<capability>.md: <requirement name>:" in DESIGN_PROMPT
        assert "Flag any spec requirements that seem infeasible" not in DESIGN_PROMPT

    def test_the_design_audit_prompt_asks_whether_a_fix_needs_the_requirements(self):
        from build_pipeline.prompts.design_audit import DESIGN_AUDIT_PROMPT
        assert '"needs_requirements_change": false' in DESIGN_AUDIT_PROMPT


def test_buildme_never_imports_pm_spec_pipeline():
    """The two tools share only multiplai_core.specs and the change dir's files."""
    root = Path(__file__).resolve().parent.parent / "build_pipeline"
    for path in root.rglob("*.py"):
        assert "pm_spec_pipeline" not in path.read_text(), path
