"""Research, the size gate after plan review, bootstrap and input files.

Every model call is a fake; the git repos are real temp repos (same helpers
as test_git_ops), so "committed on the build branch" is checked with
`git show HEAD:<path>`, not inferred.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.intake import (
    WAITING_EXIT,
    config_commit_state,
)
from build_pipeline.llm_steps.research_steps import ResearchDecision
from build_pipeline.models import (
    AgentResult,
    BuildPhase,
    GateResult,
    PlanReviewResult,
)
from build_pipeline.orchestrator import run_orchestrator
from build_pipeline.state import BuildState

from tests.planning_fakes import accepted_planning  # noqa: F401
from tests.spec_fixtures import ready_specs
from tests.test_git_ops import make_repo


@pytest.fixture(autouse=True)
def _no_spec_extract(monkeypatch):
    """No build here runs spec-extract: its step inside CODEBASE_ANALYSIS lets the phase complete."""
    from build_pipeline import orchestrator

    monkeypatch.setattr(
        orchestrator, "_run_spec_extract_step", lambda config, state, progress: None
    )


def _args(project: Path, change: str, **over) -> argparse.Namespace:
    base = dict(
        change=change,
        project_dir=str(project),
        auto=False,
        spec_only=True,
        skip_research=True,
        research_path="",
        session_id="",
        accept_size=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _config(project: Path, change: str, **over) -> BuildConfig:
    fields = dict(
        project_dir=project, change_name=change, spec_only=True, skip_research=True
    )
    fields.update(over)
    config = BuildConfig(**fields)
    config.specs_dir = project / "specs"
    return config


def _spec_stage_mocks():
    """Everything after research that would call a model, as no-ops."""
    return (
        patch(
            "build_pipeline.spec_generator.run_spec_generator",
            AsyncMock(return_value=0),
        ),
        patch(
            "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
            AsyncMock(return_value=PlanReviewResult(findings=[])),
        ),
    )


def _show(worktree: Path, rel: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )


# --- research ------------------------------------------------------------------


class TestResearch:
    async def _run(self, tmp_path, monkeypatch, args_over, decision=None):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = _config(repo, "csv", skip_research=False)
        args = _args(repo, "csv", skip_research=False, **args_over)
        decide = AsyncMock(return_value=decision)
        mocks = _spec_stage_mocks()
        with (
            mocks[0],
            mocks[1],
            patch(
                "build_pipeline.llm_steps.research_steps.llm_call_structured", decide
            ),
        ):
            rc = await run_orchestrator(config, args)
        return rc, config, decide

    @pytest.mark.asyncio
    async def test_research_path_lands_research_md_in_the_change_dir(
        self,
        tmp_path,
        monkeypatch,
    ):
        src = tmp_path / "x.md"
        src.write_text("# Findings\nThe csv module quotes minimally by default.\n")
        rc, config, decide = await self._run(
            tmp_path, monkeypatch, {"research_path": str(src)}
        )
        assert rc == 0
        decide.assert_not_awaited()
        saved = config.change_dir / "research.md"
        assert saved.is_file()
        assert "quotes minimally" in saved.read_text()

    @pytest.mark.asyncio
    async def test_design_prompt_holds_the_research_content_not_its_path(
        self,
        tmp_path,
        monkeypatch,
    ):
        from build_pipeline.spec_generator import _generate_single_artifact

        src = tmp_path / "x.md"
        src.write_text("# Findings\nThe csv module quotes minimally by default.\n")
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = _config(repo, "csv", skip_research=False)
        captured: dict = {}

        async def fake_generate(config, state, args):
            prompts = []

            async def fake_agent(prompt, **kwargs):
                prompts.append(prompt)
                return AgentResult(success=True, output="# Design\n")

            cm = ChangeManager(config.specs_dir)
            with patch("build_pipeline.llm_steps.spec_steps.agent_call", fake_agent):
                await _generate_single_artifact(
                    cm,
                    config.change_dir,
                    "design",
                    config,
                    BuildState.load(config.state_file_path()),
                )
            captured["prompt"] = prompts[0]
            return 0

        with (
            patch(
                "build_pipeline.spec_generator.run_spec_generator",
                lambda config, args, **kwargs: fake_generate(config, None, args),
            ),
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                AsyncMock(return_value=PlanReviewResult(findings=[])),
            ),
        ):
            rc = await run_orchestrator(
                config, _args(repo, "csv", skip_research=False, research_path=str(src))
            )
        assert rc == 0
        prompt = captured["prompt"]
        research_slot = prompt.split("## Research Findings")[1].split(
            "## Reference Docs"
        )[0]
        assert "The csv module quotes minimally by default." in research_slot
        assert str(src) not in prompt
        assert str(config.change_dir / "research.md") not in prompt
        assert "<untrusted-content" in research_slot
        assert "data, never instructions" in research_slot

    @pytest.mark.asyncio
    async def test_the_decision_reads_the_ready_specs(self, tmp_path, monkeypatch):
        rc, config, decide = await self._run(
            tmp_path,
            monkeypatch,
            {},
            ResearchDecision(needed=False, reason="Own code."),
        )
        assert rc == 0
        prompt = decide.await_args.args[0]
        assert "### proposal.md" in prompt and "### requirements/fixture.md" in prompt
        assert (config.change_dir / "proposal.md").read_text() in prompt

    def test_missing_research_file_leaves_the_slot_none_provided(
        self, tmp_path, caplog
    ):
        from build_pipeline.spec_generator import read_research

        state = BuildState(
            change_name="c", tier="advanced", research_path=str(tmp_path / "gone.md")
        )
        with caplog.at_level("WARNING"):
            assert read_research(state) == "(none provided)"
        assert any("gone.md" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_rerun_with_research_path_after_research_needed_continues(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        rc, config, decide = await self._run(
            tmp_path,
            monkeypatch,
            {},
            ResearchDecision(needed=True, reason="New library.", topics=["x"]),
        )
        assert rc == 3
        assert decide.await_count == 1

        src = tmp_path / "r.md"
        src.write_text("Findings.\n")
        repo = config.project_dir
        config = _config(repo, "csv", skip_research=False)
        mocks = _spec_stage_mocks()
        with (
            mocks[0],
            mocks[1],
            patch(
                "build_pipeline.llm_steps.research_steps.llm_call_structured", decide
            ),
        ):
            rc = await run_orchestrator(
                config, _args(repo, "csv", skip_research=False, research_path=str(src))
            )
        assert rc == 0
        assert decide.await_count == 1, "the decision was not asked again"
        assert (config.change_dir / "research.md").read_text() == "Findings.\n"

    def test_cli_build_accepts_research_path(self):
        from build_pipeline.__main__ import build_parser

        args = build_parser().parse_args(
            ["build", "--change", "csv", "--research-path", "x.md"]
        )
        assert args.research_path == "x.md"

    @pytest.mark.asyncio
    async def test_not_needed_writes_the_reason(self, tmp_path, monkeypatch):
        rc, config, decide = await self._run(
            tmp_path,
            monkeypatch,
            {},
            ResearchDecision(
                needed=False, reason="Pure change to our own export code."
            ),
        )
        assert rc == 0
        assert decide.await_args.kwargs["budget_label"] == "research"
        research = (config.change_dir / "research.md").read_text()
        assert "Pure change to our own export code." in research

    @pytest.mark.asyncio
    async def test_needed_exits_3_with_the_request(self, tmp_path, monkeypatch, capsys):
        rc, config, _ = await self._run(
            tmp_path,
            monkeypatch,
            {},
            ResearchDecision(
                needed=True, reason="New library.", topics=["polars csv writer"]
            ),
        )
        out = capsys.readouterr().out
        assert rc == 3
        assert "RESEARCH:NEEDED:" in out
        request = (config.change_dir / "research-request.md").read_text()
        assert "polars csv writer" in request
        state = BuildState.load(config.state_file_path())
        assert not state.is_phase_complete(BuildPhase.RESEARCH)


# --- size gate -----------------------------------------------------------------


def _block(n: int, produces: str, consumes: str = "") -> str:
    lines = [
        f"## {n}. Part {n}",
        "",
        f"Write part {n} in core/part{n}.py.",
        "",
        "Interfaces:",
        f"- Produces: `{produces}`",
        f"- Consumes: `{consumes}`" if consumes else "- Consumes: (none)",
    ]
    return "\n".join(lines) + "\n"


def _two_group_tasks(blocks: int) -> str:
    """Two chains with no signature crossing between them, *blocks* in all."""
    half = blocks // 2
    out = ["# Tasks", ""]
    for i in range(1, half + 1):
        out.append(_block(i, f"a{i}()", f"a{i - 1}()" if i > 1 else ""))
    for i in range(half + 1, blocks + 1):
        out.append(_block(i, f"b{i}()", f"b{i - 1}()" if i > half + 1 else ""))
    return "\n".join(out)


def _one_group_tasks(blocks: int) -> str:
    out = ["# Tasks", ""]
    for i in range(1, blocks + 1):
        out.append(_block(i, f"a{i}()", f"a{i - 1}()" if i > 1 else ""))
    return "\n".join(out)


def _reviewer_calls_it_oversized(monkeypatch):
    """The plan check's model reports a major `oversized-plan` finding: the
    size gate parks on that finding, not on the code's grouping."""
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import PlanReviewCut, PlanReviewFinding

    finding = PlanReviewFinding(
        category="oversized-plan",
        severity="major",
        claim="Two chains that share nothing.",
        proposed_cut=[
            PlanReviewCut(ticket="chain-a", blocks=list(range(1, 7))),
            PlanReviewCut(ticket="chain-b", blocks=list(range(7, 13))),
        ],
    )
    monkeypatch.setattr(
        plan_review_steps,
        "run_plan_review",
        AsyncMock(return_value=PlanReviewResult(findings=[finding])),
    )


class TestSizeGate:
    def _config(self, tmp_path, tasks: str):
        config = BuildConfig(
            project_dir=tmp_path,
            change_name="big",
            auto=True,
            spec_only=True,
            skip_research=True,
        )
        config.specs_dir = tmp_path / "specs"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change("big")
        (config.change_dir / "tasks.md").write_text(tasks)
        state = BuildState(
            change_name="big",
            tier="advanced",
            phase=BuildPhase.PLAN_REVIEW,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())
        return config

    def _assert_fixture(self, config, groups: int):
        from build_pipeline.llm_steps.plan_review_steps import split_assessment

        _, assessment = split_assessment(config, config.change_dir)
        assert assessment is not None
        assert assessment.block_count > config.plan_split_block_trigger
        assert assessment.split.group_count == groups

    @pytest.mark.asyncio
    async def test_two_groups_above_the_trigger_stop_with_a_proposal(
        self, tmp_path, capsys, monkeypatch
    ):
        _reviewer_calls_it_oversized(monkeypatch)
        config = self._config(tmp_path, _two_group_tasks(12))
        self._assert_fixture(config, groups=2)
        rc = await run_orchestrator(config, _args(tmp_path, "big", auto=True))
        out = capsys.readouterr().out
        assert rc == 3
        assert "SPLIT:REQUIRED:" in out
        proposal = (config.change_dir / "split-proposal.md").read_text()
        assert "chain-a" in proposal and "chain-b" in proposal
        assert "## Proposed PRs — 2 group(s)" in proposal
        assert "--accept-size" in proposal
        state = BuildState.load(config.state_file_path())
        assert state.split_required is True

        # A re-run without --accept-size stops the same way.
        rc = await run_orchestrator(config, _args(tmp_path, "big", auto=True))
        assert rc == 3
        assert "SPLIT:REQUIRED:" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_accept_size_continues(self, tmp_path, capsys, monkeypatch):
        _reviewer_calls_it_oversized(monkeypatch)
        config = self._config(tmp_path, _two_group_tasks(12))
        rc = await run_orchestrator(
            config, _args(tmp_path, "big", auto=True, accept_size=True)
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "SPLIT:REQUIRED" not in out
        assert "RESULT:SUCCESS:spec-only" in out

    @pytest.mark.asyncio
    async def test_accepted_size_holds_on_a_retry_without_the_flag(
        self, tmp_path, capsys, monkeypatch
    ):
        """A build that accepted its size must not stop again on a re-run:
        the wrapper would post the split proposal a second time."""
        _reviewer_calls_it_oversized(monkeypatch)
        config = self._config(tmp_path, _two_group_tasks(12))
        rc = await run_orchestrator(
            config, _args(tmp_path, "big", auto=True, accept_size=True)
        )
        assert rc == 0
        assert (
            BuildState.load(
                config.state_file_path().with_name(".build-state.complete.json")
            ).size_accepted
            is True
        )
        capsys.readouterr()

        rc = await run_orchestrator(config, _args(tmp_path, "big", auto=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "SPLIT:REQUIRED" not in out

    @pytest.mark.asyncio
    async def test_one_group_plan_does_not_stop(self, tmp_path, capsys):
        config = self._config(tmp_path, _one_group_tasks(12))
        self._assert_fixture(config, groups=1)
        rc = await run_orchestrator(config, _args(tmp_path, "big", auto=True))
        assert rc == 0
        assert "SPLIT:REQUIRED" not in capsys.readouterr().out

    def test_cli_build_accepts_the_new_flags(self):
        from build_pipeline.__main__ import build_parser

        args = build_parser().parse_args(["build", "--change", "big", "--accept-size"])
        assert args.accept_size is True


# --- bootstrap ------------------------------------------------------------------


class TestBootstrapConfigCommitted:
    @pytest.mark.asyncio
    async def test_untracked_config_yaml_fails_with_the_not_committed_error(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        (repo / "specs").mkdir()
        (repo / "specs" / "config.yaml").write_text("context: csv tools\n")
        assert config_commit_state(repo) == "uncommitted"
        config = _config(repo, "csv")
        assert config.git.worktree is True

        rc = await run_orchestrator(config, _args(repo, "csv"))
        err = capsys.readouterr().err
        assert rc == 1
        assert (
            "ERROR: specs/config.yaml is not committed; the build's worktree "
            "would not see it. Commit it first."
        ) in err
        assert not (tmp_path / "ws" / ".worktrees").exists()

    @pytest.mark.asyncio
    async def test_failed_build_retry_in_its_worktree_is_not_refused(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """A FAILED build re-binds to its own worktree and re-runs bootstrap.
        The stub config.yaml bootstrap wrote there is untracked; the retry must
        not refuse over it (the user cannot commit it from their repo)."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = _config(repo, "csv")
        failing = patch(
            "build_pipeline.spec_generator.run_spec_generator",
            AsyncMock(return_value=1),
        )
        with failing:
            rc = await run_orchestrator(config, _args(repo, "csv"))
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert state.phase == BuildPhase.FAILED
        worktree = Path(state.worktree_path)
        assert config_commit_state(config.project_dir) == "uncommitted", (
            "precondition: the stub config.yaml in the worktree is not committed"
        )
        capsys.readouterr()

        config = _config(repo, "csv")
        mocks = _spec_stage_mocks()
        with mocks[0], mocks[1]:
            rc = await run_orchestrator(config, _args(repo, "csv"))
        err = capsys.readouterr().err
        assert "not committed" not in err
        assert rc == 0
        assert worktree.is_dir()

    @pytest.mark.asyncio
    async def test_no_worktree_does_not_check(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path / "proj")
        (repo / "specs").mkdir()
        (repo / "specs" / "config.yaml").write_text("context: csv tools\n")
        config = _config(repo, "csv")
        config.git.worktree = False
        mocks = _spec_stage_mocks()
        with mocks[0], mocks[1]:
            rc = await run_orchestrator(config, _args(repo, "csv"))
        assert rc == 0


# --- a FAILED build re-run in its own worktree ------------------------------------


def _sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _is_ancestor(cwd: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=str(cwd)
        ).returncode
        == 0
    )


def _buildme_branches(repo: Path) -> list[str]:
    """The repo's local branches but main: the ones builds made."""
    names = _sh(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    ).split()
    return [n for n in names if n != "main"]


class TestFailedBuildRetry:
    """A FAILED build re-run with the same command (by hand, or the epic
    coordinator's relaunch of a failed child, which repeats its argv) keeps
    its own branch and worktree, with every commit the failed run made.
    Anything uncommitted there must be what the failed run left
    (`BuildState.failed_stop`); anything else stops the re-run untouched."""

    @staticmethod
    def _failing_spec_stage(*, commit: bool = True, leave: str = ""):
        """A spec stage that commits `work.txt` in the build's tree, leaves
        *leave* uncommitted, then fails."""

        async def fail(config, args, **kwargs):
            project = Path(config.project_dir)
            if commit:
                (project / "work.txt").write_text("x = 1\n")
                _sh(project, "add", "work.txt")
                _sh(project, "commit", "-qm", "wip: the failed run's commit")
            if leave:
                (project / leave).write_text("half-written\n")
            return 1

        return patch(
            "build_pipeline.spec_generator.run_spec_generator",
            AsyncMock(side_effect=fail),
        )

    async def _fail(self, repo: Path, *, base_ref: str = "", **kw) -> BuildState:
        config = _config(repo, "csv")
        config.base_ref = base_ref
        with self._failing_spec_stage(**kw):
            rc = await run_orchestrator(config, _args(repo, "csv"))
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert state.phase == BuildPhase.FAILED
        return state

    async def _run(self, repo: Path, *, base_ref: str = "") -> tuple[int, BuildConfig]:
        config = _config(repo, "csv")
        config.base_ref = base_ref
        mocks = _spec_stage_mocks()
        with mocks[0], mocks[1]:
            rc = await run_orchestrator(config, _args(repo, "csv"))
        return rc, config

    @staticmethod
    def _worktrees(tmp_path: Path, ws: str = "ws") -> list[str]:
        return sorted(p.name for p in (tmp_path / ws / ".worktrees").iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("epic_child", [False, True])
    @pytest.mark.parametrize("committed", [True, False])
    async def test_a_rerun_resumes_on_the_failed_builds_branch_and_worktree(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        committed,
        epic_child,
    ):
        """Before, a failed run's commit made the re-run refuse its own branch
        (`Branch ... already exists with 1 commit(s)`), and a failure before
        any commit moved the re-run to a `-2` branch and worktree."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        # An epic child branches from the commit that holds its ready specs.
        ready_specs(repo / "specs" / "changes" / "csv", repo)
        base = _sh(repo, "rev-parse", "HEAD") if epic_child else ""
        state = await self._fail(repo, base_ref=base, commit=committed)
        worktree, branch = Path(state.worktree_path), state.branch
        failed_head = _sh(worktree, "rev-parse", "HEAD")
        capsys.readouterr()

        rc, config = await self._run(repo, base_ref=base)
        out, err = capsys.readouterr()
        assert rc == 0, err
        assert "already exists" not in err
        assert f"WORKTREE:{worktree}" in out
        assert Path(config.project_dir).resolve() == worktree.resolve()
        assert self._worktrees(tmp_path) == [worktree.name]
        assert _buildme_branches(repo) == [branch]
        assert _sh(worktree, "rev-parse", "--abbrev-ref", "HEAD") == branch
        assert _is_ancestor(worktree, failed_head)

        # The same command in a repo where nothing failed commits the same
        # files: the resumed build lost nothing and added only the failed
        # run's own commit.
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws-fresh"))
        fresh_repo = make_repo(tmp_path / "fresh" / "proj")
        ready_specs(fresh_repo / "specs" / "changes" / "csv", fresh_repo)
        fresh_base = _sh(fresh_repo, "rev-parse", "HEAD") if epic_child else ""
        rc, fresh = await self._run(fresh_repo, base_ref=fresh_base)
        assert rc == 0
        tree = set(_sh(worktree, "ls-tree", "-r", "--name-only", "HEAD").split())
        fresh_tree = set(
            _sh(fresh.project_dir, "ls-tree", "-r", "--name-only", "HEAD").split()
        )
        assert tree == fresh_tree | ({"work.txt"} if committed else set())

    @pytest.mark.asyncio
    async def test_an_edit_made_after_the_failure_stops_the_rerun_and_is_kept(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo)
        worktree = Path(state.worktree_path)
        head = _sh(worktree, "rev-parse", "HEAD")
        (worktree / "README.md").write_text("the user's edit\n")
        (worktree / "notes.txt").write_text("mine\n")
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        assert rc == 1
        assert "README.md" in err and "notes.txt" in err
        assert "stash them first" in err
        assert (worktree / "README.md").read_text() == "the user's edit\n"
        assert (worktree / "notes.txt").read_text() == "mine\n"
        assert _sh(worktree, "rev-parse", "HEAD") == head
        assert self._worktrees(tmp_path) == [worktree.name]
        assert _buildme_branches(repo) == [state.branch]
        assert BuildState.load(Path(state.state_file)).phase == BuildPhase.FAILED

        # Committed on the branch, the edit is the build's to build on.
        _sh(worktree, "add", "README.md", "notes.txt")
        _sh(worktree, "commit", "-qm", "the user's fix")
        user_head = _sh(worktree, "rev-parse", "HEAD")
        rc, _ = await self._run(repo)
        assert rc == 0, capsys.readouterr().err
        assert _is_ancestor(worktree, user_head)
        assert (worktree / "notes.txt").read_text() == "mine\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edited_after", [False, True])
    async def test_what_the_failed_run_left_uncommitted_is_recorded_and_kept(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        edited_after,
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo, leave="draft.md")
        assert state.failed_stop is not None
        assert "draft.md" in state.failed_stop.edits
        assert state.failed_stop.head == _sh(
            Path(state.worktree_path), "rev-parse", "HEAD"
        )
        worktree = Path(state.worktree_path)
        if edited_after:
            (worktree / "draft.md").write_text("the user's version\n")
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        if edited_after:
            assert rc == 1
            assert "draft.md" in err
            assert (worktree / "draft.md").read_text() == "the user's version\n"
        else:
            assert rc == 0, err
            assert (worktree / "draft.md").read_text() == "half-written\n"

    @pytest.mark.asyncio
    async def test_a_failed_checkpoint_with_no_record_refuses_any_uncommitted_change(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """A FAILED checkpoint written before the record existed, or when git
        could not be asked: the failed run's leftovers cannot be told from
        anyone else's."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo, leave="draft.md")
        state.failed_stop = None
        state.checkpoint(Path(state.state_file))
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        assert rc == 1
        assert "draft.md" in err
        assert (Path(state.worktree_path) / "draft.md").read_text() == "half-written\n"

    @pytest.mark.asyncio
    async def test_a_checkpoint_with_no_record_and_no_commit_reruns_in_a_new_worktree(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """A build that failed under 0.70.1 or earlier before its first commit
        has no record, and its tree holds the failed run's own bootstrap
        files. It re-runs as 0.70.1 re-ran it, in a new `-2` worktree, and
        the old worktree is left as it was."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo, commit=False, leave="draft.md")
        state.failed_stop = None
        state.checkpoint(Path(state.state_file))
        worktree = Path(state.worktree_path)
        capsys.readouterr()

        rc, config = await self._run(repo)
        assert rc == 0, capsys.readouterr().err
        assert self._worktrees(tmp_path) == [worktree.name, f"{worktree.name}-2"]
        assert (worktree / "draft.md").read_text() == "half-written\n"
        assert Path(config.project_dir).name == f"{worktree.name}-2"

    @pytest.mark.asyncio
    async def test_a_rerun_started_inside_the_worktree_commits_on_the_branch(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """Started from inside the worktree, the state is found in place, so
        nothing re-binds; the re-run must still own the branch, or every
        spec-stage commit is skipped."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo)
        worktree, branch = Path(state.worktree_path), state.branch
        capsys.readouterr()

        rc, config = await self._run(worktree)
        assert rc == 0, capsys.readouterr().err
        assert config.pipeline_branch == branch
        assert _sh(worktree, "rev-parse", "--abbrev-ref", "HEAD") == branch
        assert _sh(worktree, "log", "-1", "--format=%s", branch).startswith(
            "docs(specs): csv"
        )
        committed = set(_sh(worktree, "ls-tree", "-r", "--name-only", branch).split())
        assert "specs/changes/csv/.change.yaml" in committed

    @pytest.mark.asyncio
    async def test_a_worktree_switched_off_the_builds_branch_stops_the_rerun(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """The re-run commits on whatever is checked out, and publish pushes
        `state.branch`: on another branch the build's work would miss its PR."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo)
        worktree, branch = Path(state.worktree_path), state.branch
        tip = _sh(worktree, "rev-parse", branch)
        _sh(worktree, "switch", "-qc", "elsewhere")
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        assert rc == 1
        assert "elsewhere" in err and branch in err
        assert _sh(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "elsewhere"
        assert _sh(worktree, "rev-parse", "elsewhere") == tip
        assert _sh(worktree, "rev-parse", branch) == tip
        assert BuildState.load(Path(state.state_file)).phase == BuildPhase.FAILED

        _sh(worktree, "switch", "-q", branch)
        rc, _ = await self._run(repo)
        assert rc == 0, capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_a_stop_at_bootstrap_that_never_failed_still_resumes(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """A run stopped during research (an exception, a signal) leaves
        the pointer AT BOOTSTRAP with no failure record, and bootstrap's own
        files uncommitted. Its resume is not a FAILED re-run and completes as
        it did before (in a new `-2` worktree), never refused over those files."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = _config(repo, "csv", skip_research=False)
        with patch(
            "build_pipeline.intake.run_research_phase",
            AsyncMock(side_effect=RuntimeError("stopped")),
        ):
            rc = await run_orchestrator(config, _args(repo, "csv", skip_research=False))
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert state.phase == BuildPhase.BOOTSTRAP and state.failed_stop is None
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        assert rc == 0, err
        assert "Did not re-run" not in err

    @pytest.mark.asyncio
    async def test_a_spec_stage_failure_keeps_the_progress_the_stage_saved(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """run_spec_generator checkpoints its own copy of the state. Failing
        must not write the orchestrator's older copy over it, or the re-run,
        which keeps the artifacts, pays for the explainer gate and the tasks
        audit again."""
        from build_pipeline.state import SpecGenState

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")

        async def stage(config, args, **kwargs):
            saved = BuildState.load(config.state_file_path())
            saved.spec_gen = SpecGenState(completed_artifacts=["proposal"])
            saved.spec_gen.record_step("explainers", "passed")
            saved.spec_gen.record_step("plan_review", "passed")
            saved.checkpoint(config.state_file_path())
            return 1

        config = _config(repo, "csv")
        with patch(
            "build_pipeline.spec_generator.run_spec_generator",
            AsyncMock(side_effect=stage),
        ):
            rc = await run_orchestrator(config, _args(repo, "csv"))
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert state.phase == BuildPhase.FAILED
        assert state.failed_stop is not None
        assert state.spec_gen is not None
        assert state.spec_gen.completed_artifacts == ["proposal"]
        assert state.spec_gen.step_passed("explainers") and state.spec_gen.step_passed(
            "plan_review"
        )

    @pytest.mark.asyncio
    async def test_a_rerun_keeps_the_codebase_analysis_it_already_wrote(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """The report is in the change dir the re-run keeps, and the kept
        design was written against it: analysing again pays twice."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        (repo / "app.py").write_text("print('hi')\n")
        _sh(repo, "add", "app.py")
        _sh(repo, "commit", "-qm", "app")
        analysis = AsyncMock(return_value="# Codebase analysis\n\nthe first one\n")
        with patch(
            "build_pipeline.llm_steps.spec_steps.run_codebase_analysis", analysis
        ):
            state = await self._fail(repo)
            assert analysis.await_count == 1
            report = Path(state.spec_gen.codebase_analysis_path)
            capsys.readouterr()

            rc, _ = await self._run(repo)
        assert rc == 0, capsys.readouterr().err
        assert analysis.await_count == 1
        assert "the first one" in report.read_text()

    @pytest.mark.asyncio
    @pytest.mark.no_handoff
    async def test_a_rerun_analyses_the_code_the_not_analysed_report_asked_to_commit(
        self,
        tmp_path,
        capsys,
    ):
        """A `--no-worktree` build started before any commit (ready specs
        that pm-spec could not commit) saves the not-analysed report, which
        tells the user to commit and re-run. The re-run after that commit
        analyses the code instead of reusing it."""
        from build_pipeline.code_analysis import NOT_ANALYSED_HEADING

        project = tmp_path / "proj"
        project.mkdir()
        _sh(project, "init", "-q", "-b", "main")
        (project / "app.py").write_text("print('hi')\n")
        ready_specs(project / "specs" / "changes" / "csv", project, commit=False)

        async def analyse(project_dir, config):
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", "HEAD"],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            if head.returncode != 0:
                return f"{NOT_ANALYSED_HEADING}\n\n- `app.py`\n"
            return "# Codebase analysis\n\nthe analysed one\n"

        analysis = AsyncMock(side_effect=analyse)
        config = _config(project, "csv")
        config.git.worktree = False
        with patch(
            "build_pipeline.llm_steps.spec_steps.run_codebase_analysis", analysis
        ):
            with self._failing_spec_stage(commit=False):
                rc = await run_orchestrator(config, _args(project, "csv"))
            assert rc == 1
            state = BuildState.load(config.state_file_path())
            assert state.phase == BuildPhase.FAILED
            report = Path(state.spec_gen.codebase_analysis_path)
            assert report.read_text().startswith(NOT_ANALYSED_HEADING)

            _sh(project, "add", "-A")
            _sh(project, "commit", "-qm", "the user's commit")
            capsys.readouterr()
            config = _config(project, "csv")
            config.git.worktree = False
            mocks = _spec_stage_mocks()
            with mocks[0], mocks[1]:
                rc = await run_orchestrator(config, _args(project, "csv"))
        assert rc == 0, capsys.readouterr().err
        assert analysis.await_count == 2
        assert report.read_text() == "# Codebase analysis\n\nthe analysed one\n"

    async def _stop_in_research(self, repo: Path) -> BuildState:
        """The same command with research on, stopped by an exception in the
        research step: exit 1 from the generic handler, the pointer left AT
        BOOTSTRAP, never FAILED (a signal leaves the same)."""
        config = _config(repo, "csv", skip_research=False)
        with patch(
            "build_pipeline.intake.run_research_phase",
            AsyncMock(side_effect=RuntimeError("stopped")),
        ):
            rc = await run_orchestrator(config, _args(repo, "csv", skip_research=False))
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert state.phase == BuildPhase.BOOTSTRAP
        return state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recorded", [True, False])
    async def test_a_rerun_stopped_before_its_research_finished_resumes_in_place(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
    ):
        """The re-run's bootstrap moves the pointer to BOOTSTRAP, so a stop in
        its research leaves a build that is no longer FAILED, on a branch
        that holds commits. Its resume keeps the worktree as the re-run did,
        instead of asking `_setup_worktree` for a branch it refuses.
        `recorded=False`: a 0.70.1 checkpoint, whose leftovers the user
        committed before the re-run, as its refusal asked."""
        from build_pipeline import git_ops

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo)
        worktree, branch = Path(state.worktree_path), state.branch
        users = set()
        if not recorded:
            state.failed_stop = None
            state.checkpoint(Path(state.state_file))
            users = set(git_ops.dirty_files(worktree))
            _sh(worktree, "add", *sorted(users))
            _sh(worktree, "commit", "-qm", "the user's commit of the leftovers")
        failed_head = _sh(worktree, "rev-parse", "HEAD")

        stopped = await self._stop_in_research(repo)
        assert Path(stopped.worktree_path) == worktree
        capsys.readouterr()

        rc, config = await self._run(repo)
        out, err = capsys.readouterr()
        assert rc == 0, err
        assert "already exists" not in err and "Did not re-run" not in err
        assert f"WORKTREE:{worktree}" in out
        assert Path(config.project_dir).resolve() == worktree.resolve()
        assert self._worktrees(tmp_path) == [worktree.name]
        assert _buildme_branches(repo) == [branch]
        assert _sh(worktree, "rev-parse", "--abbrev-ref", "HEAD") == branch
        assert _is_ancestor(worktree, failed_head)

        # Compared with the same command where nothing failed or stopped.
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws-fresh"))
        fresh_repo = make_repo(tmp_path / "fresh" / "proj")
        rc, fresh = await self._run(fresh_repo)
        assert rc == 0
        tree = set(_sh(worktree, "ls-tree", "-r", "--name-only", "HEAD").split())
        fresh_tree = set(
            _sh(fresh.project_dir, "ls-tree", "-r", "--name-only", "HEAD").split()
        )
        assert tree == fresh_tree | {"work.txt"} | users

    @pytest.mark.asyncio
    async def test_an_edit_made_while_a_rerun_was_stopped_stops_its_resume(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """The kept re-run's bootstrap records the tree it leaves; an edit made
        after that stop is not the build's, and its resume refuses it."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo)
        worktree = Path(state.worktree_path)
        await self._stop_in_research(repo)
        (worktree / "notes.txt").write_text("mine\n")
        head = _sh(worktree, "rev-parse", "HEAD")
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        assert rc == 1
        assert "notes.txt" in err and "stash them first" in err
        assert (worktree / "notes.txt").read_text() == "mine\n"
        assert _sh(worktree, "rev-parse", "HEAD") == head
        assert BuildState.load(Path(state.state_file)).phase == BuildPhase.BOOTSTRAP

    @pytest.mark.asyncio
    async def test_research_written_before_its_checkpoint_stops_the_resume(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """The re-run stops between writing `research.md` and its RESEARCH
        checkpoint, so the file is outside the tree its bootstrap recorded.
        The resume refuses it, naming the file, and keeps it."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        state = await self._fail(repo)
        worktree = Path(state.worktree_path)

        async def write_then_stop(config, state, args, state_path, progress):
            (Path(config.change_dir) / "research.md").write_text("# Research\n\nS\n")
            raise RuntimeError("stopped before the checkpoint")

        config = _config(repo, "csv", skip_research=False)
        with patch(
            "build_pipeline.intake.run_research_phase",
            AsyncMock(side_effect=write_then_stop),
        ):
            rc = await run_orchestrator(config, _args(repo, "csv", skip_research=False))
        assert rc == 1
        research = Path(config.change_dir) / "research.md"
        assert research.read_text() == "# Research\n\nS\n"
        head = _sh(worktree, "rev-parse", "HEAD")
        capsys.readouterr()

        rc, _ = await self._run(repo)
        err = capsys.readouterr().err
        assert rc == 1
        assert "research.md" in err and "stash them first" in err
        assert research.read_text() == "# Research\n\nS\n"
        assert _sh(worktree, "rev-parse", "HEAD") == head
        assert BuildState.load(Path(state.state_file)).phase == BuildPhase.BOOTSTRAP

    @pytest.mark.asyncio
    @pytest.mark.parametrize("moved_by", ["no-record", "stop-at-bootstrap"])
    async def test_a_build_moved_to_a_new_worktree_reruns_there_after_it_fails(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        moved_by,
    ):
        """Rows R6b and R12 move a build to a `-2` worktree and leave the first
        one there, checkpoint and all. Once the `-2` build commits and fails,
        its re-run binds to it (the newest checkpoint), not to the first
        worktree, whose bootstrap would ask for a `-2` branch and refuse it."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        if moved_by == "no-record":
            first = await self._fail(repo, commit=False)
            first.failed_stop = None
            first.checkpoint(Path(first.state_file))
        else:
            first = await self._stop_in_research(repo)
        old = Path(first.worktree_path)
        state = await self._fail(repo)
        moved = Path(state.worktree_path)
        assert moved.name == f"{old.name}-2"
        failed_head = _sh(moved, "rev-parse", "HEAD")
        capsys.readouterr()

        rc, config = await self._run(repo)
        out, err = capsys.readouterr()
        assert rc == 0, err
        assert "already exists" not in err
        assert f"WORKTREE:{moved}" in out
        assert Path(config.project_dir).resolve() == moved.resolve()
        assert self._worktrees(tmp_path) == [old.name, moved.name]
        assert _sh(moved, "rev-parse", "--abbrev-ref", "HEAD") == state.branch
        assert _is_ancestor(moved, failed_head)
        assert BuildState.load(Path(first.state_file)).phase == first.phase

    @pytest.mark.asyncio
    async def test_a_tdd_failure_reruns_on_the_same_branch_and_keeps_its_files(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        """The e2e report's Defect 1 case: a child that failed in TDD_BUILD
        after a block's commit. The failure reloads the engine's checkpoint
        and records the build's own tree, so the re-run resumes there, on
        the same branch, past the block it committed, with the half-written
        file the engine left."""
        from build_pipeline.state import TDDState

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        seen = []

        async def engine(config, args, standalone=True):
            project = Path(config.project_dir)
            half = project / "half.py"
            saved = BuildState.load(config.state_file_path())
            seen.append(
                (
                    project,
                    _sh(project, "rev-parse", "--abbrev-ref", "HEAD"),
                    half.read_text() if half.is_file() else None,
                    saved.tdd.current_block if saved.tdd else None,
                )
            )
            if len(seen) > 1:
                _sh(project, "add", "half.py")
                _sh(project, "commit", "-qm", "feat: complete fixture work")
                return 0
            (project / "block1.py").write_text("x = 1\n")
            _sh(project, "add", "block1.py")
            _sh(project, "commit", "-qm", "feat: block 1")
            saved.tdd = TDDState(current_block=1, baseline_tests_pass=True)
            saved.checkpoint(config.state_file_path())
            half.write_text("def half(\n")
            return 1

        async def build() -> tuple[int, BuildConfig]:
            config = _config(repo, "csv", spec_only=False)
            mocks = _spec_stage_mocks()
            with (
                mocks[0],
                mocks[1],
                patch(
                    "build_pipeline.tdd_engine.run_tdd_engine",
                    AsyncMock(side_effect=engine),
                ),
                patch(
                    "build_pipeline.llm_steps.docs_steps.run_docs_update",
                    AsyncMock(
                        return_value=(
                            [],
                            GateResult(passed=True, reason="fixture docs"),
                        )
                    ),
                ),
                patch(
                    "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                    AsyncMock(return_value=None),
                ),
                patch(
                    "build_pipeline.llm_steps.spec_steps.run_codebase_analysis",
                    AsyncMock(return_value="# Codebase analysis\n"),
                ),
            ):
                rc = await run_orchestrator(config, _args(repo, "csv", spec_only=False))
            return rc, config

        rc, config = await build()
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert state.phase == BuildPhase.FAILED
        worktree, branch = Path(state.worktree_path), state.branch
        assert state.failed_stop is not None and "half.py" in state.failed_stop.edits
        assert state.tdd is not None and state.tdd.current_block == 1
        block_head = _sh(worktree, "rev-parse", "HEAD")
        capsys.readouterr()

        rc, config = await build()
        assert rc == 0, capsys.readouterr().err
        assert seen[0][3] is None
        assert seen[1] == (worktree, branch, "def half(\n", 1)
        assert Path(config.project_dir).resolve() == worktree.resolve()
        assert self._worktrees(tmp_path) == [worktree.name]
        assert _buildme_branches(repo) == [branch]
        assert _is_ancestor(worktree, block_head)
        assert (worktree / "half.py").read_text() == "def half(\n"


# --- input file paths -----------------------------------------------------------


class TestInputPaths:
    """SKILL.md runs the pipeline under `uv run --directory <scripts>`, so the
    process cwd is never the caller's. Relative input paths are read from
    --project-dir, and a named file that does not exist is a usage error."""

    def _parse(self, *argv):
        from build_pipeline.__main__ import build_parser

        return build_parser().parse_args(["build", "--change", "csv", *argv])

    def test_relative_paths_resolve_against_project_dir(self, tmp_path, monkeypatch):
        from build_pipeline.intake import resolve_input_paths

        project = tmp_path / "proj"
        (project / "notes").mkdir(parents=True)
        (project / "notes" / "research.md").write_text("x\n")
        elsewhere = tmp_path / "scripts"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        args = self._parse(
            "--project-dir", str(project), "--research-path", "notes/research.md"
        )
        assert resolve_input_paths(args) is None
        assert Path(args.research_path).is_absolute()
        assert Path(args.research_path).is_file()

    def test_absolute_paths_are_kept(self, tmp_path):
        from build_pipeline.intake import resolve_input_paths

        research = tmp_path / "r.md"
        research.write_text("x\n")
        args = self._parse(
            "--project-dir", str(tmp_path / "proj"), "--research-path", str(research)
        )
        assert resolve_input_paths(args) is None
        assert args.research_path == str(research)

    @pytest.mark.parametrize("flag", ["--research-path"])
    def test_a_missing_input_file_exits_2_before_the_build_starts(
        self,
        tmp_path,
        capsys,
        monkeypatch,
        flag,
    ):
        from build_pipeline.__main__ import main

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        project = tmp_path / "proj"
        project.mkdir()
        rc = main(
            [
                "build",
                "--change",
                "csv",
                "--project-dir",
                str(project),
                "--no-worktree",
                flag,
                "missing.md",
            ]
        )
        assert rc == 2
        assert not (
            project / "specs" / "changes" / "csv" / ".build-state.json"
        ).exists()
        err = capsys.readouterr().err
        assert flag in err and "missing.md" in err


# --- exit codes ------------------------------------------------------------------


class TestExitCodes:
    def test_waiting_has_a_code_no_other_path_uses(self):
        """A wrapper reads 3 as 'waiting, re-run to resume'. An agent timeout
        reaching `build` through the TDD phase must not look like that.
        SPEC:ISSUES shares 4 with the agent timeout; the stdout marker tells
        the two apart."""
        from build_pipeline.readiness import EXIT_NOT_READY, EXIT_SPEC_ISSUES
        from build_pipeline.tdd_engine import (
            EXIT_AGENT_TIMEOUT,
            EXIT_BUILD_FAILURE,
            EXIT_SUCCESS,
        )

        assert WAITING_EXIT == 3
        assert EXIT_NOT_READY == 2
        assert EXIT_SPEC_ISSUES == EXIT_AGENT_TIMEOUT
        assert EXIT_AGENT_TIMEOUT == 4
        assert (
            len({EXIT_SUCCESS, EXIT_BUILD_FAILURE, 2, WAITING_EXIT, EXIT_AGENT_TIMEOUT})
            == 5
        )
