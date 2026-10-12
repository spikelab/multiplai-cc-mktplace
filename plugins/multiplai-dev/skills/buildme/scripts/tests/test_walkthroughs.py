"""Stale walkthroughs after the build: the check, the PR-body section, and regeneration.

The end-to-end test runs codebase-walkthrough's real, model-free `check`
through `uv run --directory`, as the build does. Every regeneration test stubs
the walkthrough subprocess: no test starts a real (paid) walkthrough run, and
the autouse `no_real_walkthrough_run` guard in conftest.py enforces it.
"""

import shutil
from pathlib import Path

import pytest

from build_pipeline import git_ops, walkthroughs
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.git_ops import GitResult
from build_pipeline.orchestrator import _run_bootstrap, _run_publish
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState
from tests.test_git_ops import _git, make_bare_origin, make_repo

CALC = "def add(a, b):\n    return a + b\n"
MD_NAME = "calc-walkthrough.md"


def _walkthrough(sha: str, *, repo_key: str = "proj", rows: str = "", header: bool = True) -> str:
    """A walkthrough as codebase-walkthrough writes it, in docs/walkthroughs/, citing src/calc.py:2."""
    fields = ("- Module directory: `../../src`\n- Depth: `standard`\n- Mode: `human`\n\n" if header else "")
    return (f"# calc — codebase walkthrough\n\n{fields}"
            "## Commits read\n\n| Repo | Path | Commit |\n|---|---|---|\n"
            f"| {repo_key} | ../.. | `{sha}` |\n{rows}\n"
            f"## Overview\n\n`add` sums its operands ([`{repo_key}/src/calc.py:2`](../../src/calc.py#L2 "
            '"return a + b")).\n')


def _project(path: Path) -> Path:
    repo = make_repo(path)
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text(CALC)
    _git(repo, "add", "src")
    _git(repo, "commit", "-m", "feat: calc")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "docs" / "walkthroughs").mkdir(parents=True)
    (repo / "docs" / "walkthroughs" / MD_NAME).write_text(_walkthrough(sha))
    _git(repo, "add", "docs")
    _git(repo, "commit", "-m", "docs: calc walkthrough")
    return repo


def _move_the_cited_line(repo: Path) -> None:
    (repo / "src" / "calc.py").write_text("# calculator\n" + CALC)
    _git(repo, "commit", "-am", "feat: block 1")


@pytest.fixture
def recorded(monkeypatch):
    """Every walkthrough subprocess argv, passed on to the conftest guard (real `check`, no `run`)."""
    seen: list[list[str]] = []
    inner = git_ops._run_walkthrough_skill

    def record(argv, cwd=None, timeout=None):
        seen.append(argv)
        return inner(argv, cwd=cwd)

    monkeypatch.setattr(git_ops, "_run_walkthrough_skill", record)
    return seen


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the walkthrough check runs through uv")


class TestAfterABuild:
    @pytest.fixture
    def captured(self, monkeypatch):
        seen: dict = {}

        def fake_pr(argv, cwd=None, timeout=None):
            sections = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--section"]
            seen["sections"] = {p.name: p.read_text() for p in sections}
            return GitResult(argv, 0, "PR:https://github.com/o/r/pull/5\n", "")

        monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
        return seen

    async def _build(self, tmp_path, change: str):
        repo = _project(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")
        config = BuildConfig(project_dir=repo, change_name=change)
        config.specs_dir = repo / "specs"
        state = BuildState(change_name=change, mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        await _run_bootstrap(config, state, ChangeManager(config.specs_dir), config.state_file_path())
        return config, state, ProgressWriter(config.progress_file_path())

    @needs_uv
    @pytest.mark.asyncio
    async def test_a_build_that_moves_a_cited_line_lists_the_walkthrough_as_stale(
            self, tmp_path, monkeypatch, captured, recorded, capsys):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await self._build(tmp_path, "moves")
        _move_the_cited_line(Path(state.worktree_path))

        assert _run_publish(config, state, progress) is True

        out = capsys.readouterr().out
        assert f"WALKTHROUGH:STALE:docs/walkthroughs/{MD_NAME}" in out
        assert f"WALKTHROUGH_FAIL:docs/walkthroughs/{MD_NAME}: proj/src/calc.py:2-2: quote not at cited lines" in out
        assert "WALKTHROUGHS:1 checked, 1 stale" in out
        body = captured["sections"]["walkthroughs.md"]
        assert body.startswith("## Stale walkthroughs\n")
        assert f"`docs/walkthroughs/{MD_NAME}`: 1 citation(s) failed" in body
        assert "proj/src/calc.py:2-2: quote not at cited lines" in body
        # regenerate_walkthroughs is off by default: only the check ran, no walkthrough run.
        assert config.regenerate_walkthroughs is False
        assert recorded and all(argv[argv.index("walkthrough_pipeline") + 1] == "check" for argv in recorded)

    @needs_uv
    @pytest.mark.asyncio
    async def test_a_build_that_leaves_the_cited_lines_adds_no_section(
            self, tmp_path, monkeypatch, captured, capsys):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await self._build(tmp_path, "keeps")
        assert _run_publish(config, state, progress) is True
        out = capsys.readouterr().out
        assert f"WALKTHROUGH:FRESH:docs/walkthroughs/{MD_NAME}" in out
        assert "WALKTHROUGHS:1 checked, 0 stale" in out
        assert "walkthroughs.md" not in captured["sections"]


class TestNoWalkthroughs:
    def test_no_directory_prints_one_line_and_adds_no_section(self, tmp_path, recorded, capsys):
        config = BuildConfig(project_dir=make_repo(tmp_path / "p"), change_name="c")
        assert walkthroughs.pr_section(config, None, tmp_path / "docs") is None
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1 and out[0].startswith("WALKTHROUGHS:NONE:")
        assert recorded == []


def _stub(monkeypatch, *, check_rc: int = 1, run_rc: int = 0, write=None):
    """A walkthrough subprocess: `check` reports one failed citation, `run` exits *run_rc*."""
    seen: list[list[str]] = []

    def fake(argv, cwd=None, timeout=None):
        seen.append(argv)
        verb = argv[argv.index("walkthrough_pipeline") + 1]
        if verb == "check":
            out = "FAIL proj/src/calc.py:2-2: quote not at cited lines\ncheck: 1 checked, 1 failed\n"
            return GitResult(argv, check_rc, out if check_rc == 1 else "check: 1 checked, 0 failed\n", "")
        if write and run_rc == 0:
            write()
        return GitResult(argv, run_rc, "", "STOPPED: budget" if run_rc == 2 else "")

    monkeypatch.setattr(git_ops, "_run_walkthrough_skill", fake)
    return seen


class TestRegenerate:
    @pytest.fixture
    def stale(self, tmp_path, monkeypatch):
        repo = _project(tmp_path / "proj")
        _move_the_cited_line(repo)
        config = BuildConfig(project_dir=repo, change_name="c", regenerate_walkthroughs=True,
                             walkthrough_max_usd=3.0)
        config.pipeline_branch = "main"
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        return config

    def _runs(self, seen):
        return [a for a in seen if a[a.index("walkthrough_pipeline") + 1] == "run"]

    def test_reruns_it_as_its_header_says_and_commits_the_new_files(self, stale, monkeypatch, tmp_path, capsys):
        md = stale.project_dir / "docs" / "walkthroughs" / MD_NAME

        def write():
            md.write_text(md.read_text() + "\nregenerated\n")
            md.with_suffix(".json").write_text("{}\n")

        seen = _stub(monkeypatch, write=write)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        (run,) = self._runs(seen)
        assert run[run.index("walkthrough_pipeline") + 2] == str((stale.project_dir / "src").resolve())
        assert run[run.index("--depth") + 1] == "standard" and run[run.index("--mode") + 1] == "human"
        assert run[run.index("--name") + 1] == "calc"
        assert run[run.index("--output") + 1] == str(md.parent)
        assert run[run.index("--max-usd") + 1] == "3"
        assert "--trust-repo" in run and run[run.index("--repos") + 1] == str(stale.project_dir.resolve())
        assert _git(stale.project_dir, "log", "-1", "--format=%s").stdout.strip() == \
            f"docs(walkthroughs): regenerate {MD_NAME}"
        assert _git(stale.project_dir, "status", "--porcelain").stdout == ""
        assert "regenerated and committed" in path.read_text()
        assert f"WALKTHROUGH:STALE:docs/walkthroughs/{MD_NAME} — regenerated and committed" in capsys.readouterr().out

    def test_a_run_stopped_by_its_spend_limit_leaves_the_old_file(self, stale, monkeypatch, tmp_path):
        md = stale.project_dir / "docs" / "walkthroughs" / MD_NAME
        before = md.read_text()
        seen = _stub(monkeypatch, run_rc=2)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        assert len(self._runs(seen)) == 1, "a budget stop is reported, not retried"
        assert md.read_text() == before
        assert "spend limit ($3)" in path.read_text()

    def test_not_without_buildmes_trust(self, stale, monkeypatch, tmp_path):
        monkeypatch.delenv("BUILDME_TRUST_REPO")
        seen = _stub(monkeypatch)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        assert self._runs(seen) == []
        assert "trust check did not pass" in path.read_text()

    def test_not_when_it_reads_other_repositories(self, stale, monkeypatch, tmp_path):
        md = stale.project_dir / "docs" / "walkthroughs" / MD_NAME
        sha = "0" * 40
        md.write_text(_walkthrough(sha, rows=f"| caller | ../../../caller | `{sha}` |\n"))
        seen = _stub(monkeypatch)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        assert self._runs(seen) == []
        assert "reads other repositories (caller)" in path.read_text()

    def test_not_when_the_header_does_not_say_how_it_was_made(self, stale, monkeypatch, tmp_path):
        md = stale.project_dir / "docs" / "walkthroughs" / MD_NAME
        md.write_text(_walkthrough("0" * 40, header=False))
        seen = _stub(monkeypatch)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        assert self._runs(seen) == []
        assert "does not record the module directory, depth and mode" in path.read_text()

    def test_off_by_default_nothing_runs(self, stale, monkeypatch, tmp_path):
        stale.regenerate_walkthroughs = False
        seen = _stub(monkeypatch)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        assert self._runs(seen) == []
        assert "`regenerate_walkthroughs` is off" in path.read_text()

    def test_a_check_that_could_not_run_is_reported_as_not_checked(self, stale, monkeypatch, tmp_path):
        seen = _stub(monkeypatch, check_rc=127)
        path = walkthroughs.pr_section(stale, None, tmp_path / "docs")
        assert self._runs(seen) == []
        assert "not checked; the check did not run (exit 127)" in path.read_text()
