"""Tests for the git lifecycle module.

Real temp git repos throughout (a local bare repo stands in for `origin`, which
proves the push path without touching the network). Only `gh` is mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from build_pipeline import git_ops
from build_pipeline.git_ops import GitLifecycleError, GitResult


# --- helpers --------------------------------------------------------------


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argv], cwd=str(repo), capture_output=True, text=True, check=True
    )


def make_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=str(path), capture_output=True, check=True,
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    if commit:
        (path / "README.md").write_text("hello\n")
        _git(path, "add", "README.md")
        _git(path, "commit", "-m", "chore: init")
    return path


def make_bare_origin(repo: Path, bare: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", str(bare)], capture_output=True, check=True
    )
    _git(repo, "remote", "add", "origin", str(bare))
    return bare


# --- inspection -----------------------------------------------------------


class TestInspection:
    def test_repo_root_and_is_git_repo(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.is_git_repo(repo)
        assert git_ops.repo_root(repo) == repo.resolve()

    def test_repo_root_none_outside_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert git_ops.repo_root(plain) is None
        assert not git_ops.is_git_repo(plain)

    def test_has_commits_false_before_first_commit(self, tmp_path):
        repo = make_repo(tmp_path / "proj", commit=False)
        assert not git_ops.has_commits(repo)
        (repo / "a.txt").write_text("a")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-m", "chore: first")
        assert git_ops.has_commits(repo)

    def test_current_and_default_branch(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.current_branch(repo) == "main"
        assert git_ops.default_branch(repo) == "main"

    def test_is_dirty_ignores_untracked(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        (repo / "scratch.txt").write_text("noise")
        assert not git_ops.is_dirty(repo), "untracked files must not count as dirty"
        (repo / "README.md").write_text("changed\n")
        assert git_ops.is_dirty(repo)

    def test_branch_exists_and_unmerged_count(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert not git_ops.branch_exists(repo, "feature")
        _git(repo, "checkout", "-b", "feature")
        (repo / "f.txt").write_text("f")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", "feat: f")
        _git(repo, "checkout", "main")
        assert git_ops.branch_exists(repo, "feature")
        assert git_ops.unmerged_commit_count(repo, "feature", "main") == 1

    def test_has_remote(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert not git_ops.has_remote(repo)
        make_bare_origin(repo, tmp_path / "origin.git")
        assert git_ops.has_remote(repo)


# --- naming ---------------------------------------------------------------


class TestNaming:
    def test_branch_and_dir_names_are_normalized(self):
        assert git_ops.branch_name_for("My Change!") == "buildme/my-change"
        assert git_ops.worktree_dir_name("../../etc") == "buildme-etc"

    def test_worktrees_root_prefers_workspace(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        assert git_ops.worktrees_root(repo) == tmp_path / "ws" / ".worktrees"

    def test_worktrees_root_falls_back_next_to_repo(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WORKSPACE", raising=False)
        repo = tmp_path / "nested" / "repo"
        assert git_ops.worktrees_root(repo) == tmp_path / "nested" / ".worktrees"

    def test_worktree_dest_uses_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        repo = tmp_path / "repo"
        assert git_ops.worktree_dest(repo, "my-change").name == "buildme-my-change"
        assert git_ops.worktree_dest(repo, "my-change", 3).name == "buildme-my-change-3"


class TestResolveNewBranch:
    def test_first_choice_when_free(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        branch, dest = git_ops.resolve_new_branch(repo, "my-change", "main")
        assert branch == "buildme/my-change"
        assert dest == tmp_path / "ws" / ".worktrees" / "buildme-my-change"

    def test_suffixes_on_empty_collision(self, tmp_path, monkeypatch):
        """An existing branch with nothing on it gets a -2, never reused."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        _git(repo, "branch", "buildme/my-change")  # same commit as main
        branch, dest = git_ops.resolve_new_branch(repo, "my-change", "main")
        assert branch == "buildme/my-change-2"
        assert dest.name == "buildme-my-change-2"

    def test_refuses_branch_with_unmerged_commits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        _git(repo, "checkout", "-b", "buildme/my-change")
        (repo / "work.txt").write_text("work")
        _git(repo, "add", "work.txt")
        _git(repo, "commit", "-m", "feat: prior work")
        _git(repo, "checkout", "main")
        with pytest.raises(GitLifecycleError) as exc:
            git_ops.resolve_new_branch(repo, "my-change", "main")
        assert "buildme/my-change" in str(exc.value)
        assert "Refusing" in str(exc.value)


# --- preflight refusals ---------------------------------------------------


class TestPreflight:
    def test_clean_repo_passes(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.preflight(repo, worktree_requested=True) == repo.resolve()

    def test_refuses_non_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GitLifecycleError) as exc:
            git_ops.preflight(plain, worktree_requested=True)
        assert "not a git repository" in str(exc.value)
        assert "--no-worktree" in str(exc.value)

    def test_refuses_repo_without_commits(self, tmp_path):
        repo = make_repo(tmp_path / "proj", commit=False)
        with pytest.raises(GitLifecycleError) as exc:
            git_ops.preflight(repo, worktree_requested=True)
        assert "no commits" in str(exc.value)

    def test_refuses_dirty_tree(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        (repo / "README.md").write_text("dirty\n")
        with pytest.raises(GitLifecycleError) as exc:
            git_ops.preflight(repo, worktree_requested=True)
        assert "uncommitted changes" in str(exc.value)


# --- worktree + commits ---------------------------------------------------


class TestWorktree:
    def test_create_worktree_leaves_source_checkout_untouched(self, tmp_path, monkeypatch):
        """Criterion 10 at the git_ops level: the caller's checkout is never written to."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        branch, dest = git_ops.resolve_new_branch(repo, "iso", "main")
        git_ops.create_worktree(repo, branch, dest)

        assert dest.is_dir()
        assert git_ops.current_branch(dest) == "buildme/iso"
        assert git_ops.current_branch(repo) == "main"

        # Write and commit inside the worktree only
        (dest / "new.txt").write_text("x")
        sha = git_ops.commit_paths(dest, "feat: add new", ["new.txt"])
        assert sha

        status = subprocess.run(
            ["git", "status", "--short"], cwd=str(repo), capture_output=True, text=True
        )
        assert status.stdout.strip() == "", f"source repo dirty: {status.stdout!r}"
        assert not (repo / "new.txt").exists()

    def test_create_worktree_raises_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        dest = tmp_path / "ws" / ".worktrees" / "x"
        git_ops.create_worktree(repo, "buildme/x", dest)
        with pytest.raises(GitLifecycleError):
            git_ops.create_worktree(repo, "buildme/x", dest)  # branch taken

    def test_worktree_paths_counts_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        assert len(git_ops.worktree_paths(repo)) == 1
        branch, dest = git_ops.resolve_new_branch(repo, "c", "main")
        git_ops.create_worktree(repo, branch, dest)
        assert len(git_ops.worktree_paths(repo)) == 2


class TestCommitPaths:
    def test_stages_only_the_given_paths(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        (repo / "wanted.txt").write_text("w")
        (repo / "unwanted.txt").write_text("u")
        sha = git_ops.commit_paths(repo, "feat: wanted", ["wanted.txt"])
        assert sha
        files = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.split()
        assert files == ["wanted.txt"]
        assert (repo / "unwanted.txt").exists()  # still untracked, not committed

    def test_returns_none_when_nothing_to_commit(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.commit_paths(repo, "chore: noop", ["README.md"]) is None

    def test_returns_none_for_empty_paths(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.commit_paths(repo, "chore: noop", []) is None

    def test_commit_stage_noops_without_pipeline_branch(self, tmp_path):
        from build_pipeline.config import BuildConfig

        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(project_dir=repo, change_name="c")
        (repo / "f.txt").write_text("f")
        assert config.pipeline_branch is None
        assert git_ops.commit_stage(config, "feat: x", ["f.txt"]) is None

        config.pipeline_branch = "buildme/c"
        assert git_ops.commit_stage(config, "feat: x", ["f.txt"]) is not None


# --- push -----------------------------------------------------------------


class TestPush:
    def test_push_to_local_bare_origin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        bare = make_bare_origin(repo, tmp_path / "origin.git")
        branch, dest = git_ops.resolve_new_branch(repo, "pushme", "main")
        git_ops.create_worktree(repo, branch, dest)
        (dest / "p.txt").write_text("p")
        git_ops.commit_paths(dest, "feat: p", ["p.txt"])

        res = git_ops.push_branch(dest, branch)
        assert res.ok, res.stderr

        log = subprocess.run(
            ["git", "log", "--oneline", branch],
            cwd=str(bare), capture_output=True, text=True,
        )
        assert log.returncode == 0
        assert "feat: p" in log.stdout

    def test_push_without_remote_fails_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        branch, dest = git_ops.resolve_new_branch(repo, "nopush", "main")
        git_ops.create_worktree(repo, branch, dest)
        res = git_ops.push_branch(dest, branch)
        assert not res.ok  # returns a result, never raises


# --- PR (gh mocked) -------------------------------------------------------


class TestOpenPR:
    def test_success_parses_url_and_passes_draft(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake_gh(argv, cwd=None, timeout=None):
            seen["argv"] = argv
            seen["cwd"] = cwd
            return GitResult(argv, 0, "https://github.com/o/r/pull/42\n", "")

        monkeypatch.setattr(git_ops, "_run_gh", fake_gh)
        url, res = git_ops.open_pr(tmp_path, "buildme: c", "body", draft=True)
        assert url == "https://github.com/o/r/pull/42"
        assert res.ok
        assert seen["argv"][:3] == ["gh", "pr", "create"]
        assert "--draft" in seen["argv"]

    def test_ready_omits_draft_flag(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake_gh(argv, cwd=None, timeout=None):
            seen["argv"] = argv
            return GitResult(argv, 0, "https://github.com/o/r/pull/7", "")

        monkeypatch.setattr(git_ops, "_run_gh", fake_gh)
        git_ops.open_pr(tmp_path, "t", "b", draft=False)
        assert "--draft" not in seen["argv"]

    def test_failure_returns_none_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_ops, "_run_gh",
            lambda argv, cwd=None, timeout=None: GitResult(argv, 1, "", "gh: not authenticated"),
        )
        url, res = git_ops.open_pr(tmp_path, "t", "b")
        assert url is None
        assert not res.ok
        assert "not authenticated" in res.stderr


class TestManualFinishCommands:
    def test_contains_exact_push_and_pr_commands(self, tmp_path):
        text = git_ops.manual_finish_commands(tmp_path / "wt", "buildme/c", "buildme: c")
        assert "git push -u origin buildme/c" in text
        assert "gh pr create --title 'buildme: c'" in text
        assert "--draft" in text

    def test_omits_pr_when_not_requested(self, tmp_path):
        text = git_ops.manual_finish_commands(
            tmp_path / "wt", "buildme/c", "t", include_pr=False
        )
        assert "gh pr create" not in text
        assert "git push -u origin buildme/c" in text


class TestSafetyInvariants:
    def test_remove_worktree_is_never_called_by_the_pipeline(self):
        """`remove_worktree` exists as a documented helper for a calling
        session. No pipeline module may reference it."""
        pkg = Path(git_ops.__file__).parent
        offenders = []
        for path in pkg.rglob("*.py"):
            if path.name == "git_ops.py":
                continue
            if "remove_worktree" in path.read_text():
                offenders.append(str(path))
        assert offenders == [], f"remove_worktree referenced outside git_ops: {offenders}"

    def test_no_shell_true_or_force_in_git_ops(self):
        text = Path(git_ops.__file__).read_text()
        assert "shell=True" not in text
        assert "--force" not in text
        assert "git merge" not in text
