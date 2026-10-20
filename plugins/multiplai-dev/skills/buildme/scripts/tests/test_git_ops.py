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


def _write_bookkeeping(repo: Path) -> None:
    (repo / "build-progress.md").write_text("# progress\n")
    change = repo / "specs" / "changes" / "feat"
    change.mkdir(parents=True, exist_ok=True)
    (change / ".build-state.json").write_text("{}\n")
    (change / ".board.json").write_text("{}\n")


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

    def test_unmerged_count_is_none_when_undeterminable(self, tmp_path):
        """A failed rev-list means "unknown", never "empty branch"."""
        repo = make_repo(tmp_path / "proj")
        assert git_ops.unmerged_commit_count(repo, "main", "no-such-base") is None

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

    def test_refuses_when_unmerged_count_is_unknown(self, tmp_path, monkeypatch):
        """A colliding branch whose commit count cannot be determined is
        refused, never classified as empty and shadowed with a suffix."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        _git(repo, "branch", "buildme/my-change")
        with pytest.raises(GitLifecycleError) as exc:
            git_ops.resolve_new_branch(repo, "my-change", "no-such-base")
        assert "cannot be proven empty" in str(exc.value)
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

    def test_commit_stage_commits_a_new_change_dir_holding_a_bookkeeping_file(
        self, tmp_path,
    ):
        """The first spec-stage commit stages specs/changes/<name> while that
        directory is wholly untracked and already holds .board.json. Passing
        the exclude pathspecs to `git add` there staged nothing at all."""
        from build_pipeline.config import BuildConfig

        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(project_dir=repo, change_name="c")
        config.specs_dir = repo / "specs"
        config.pipeline_branch = "buildme/c"
        change_dir = config.change_dir
        change_dir.mkdir(parents=True)
        (change_dir / "interview.md").write_text("# Interview\n")
        (change_dir / ".board.json").write_text("{}\n")
        (change_dir / ".build-state.json").write_text("{}\n")

        sha = git_ops.commit_stage(config, "docs(specs): c", ["specs/changes/c"])
        assert sha
        committed = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.split()
        assert committed == ["specs/changes/c/interview.md"]

    def test_commit_stage_excludes_bookkeeping_at_any_depth(self, tmp_path):
        """Staging a whole change (or archive) directory must never sweep
        .build-state.json / .board.json into the commit."""
        from build_pipeline.config import BuildConfig

        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(project_dir=repo, change_name="c")
        config.specs_dir = repo / "specs"
        config.pipeline_branch = "buildme/c"

        change_dir = config.change_dir
        change_dir.mkdir(parents=True)
        (change_dir / ".change.yaml").write_text("schema: spec-driven\n")
        (change_dir / "proposal.md").write_text("## Why\nx\n")
        (change_dir / ".build-state.json").write_text("{}\n")
        (change_dir / ".board.json").write_text("{}\n")
        archived = config.specs_dir / "archive" / "2026-01-01-c"
        archived.mkdir(parents=True)
        (archived / "design.md").write_text("d\n")
        (archived / ".build-state.json").write_text("{}\n")
        (archived / ".board.json").write_text("{}\n")

        sha = git_ops.commit_stage(config, "docs(specs): c", ["specs"])
        assert sha

        committed = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.split()
        assert "specs/changes/c/proposal.md" in committed
        assert "specs/archive/2026-01-01-c/design.md" in committed
        assert not any(".build-state.json" in f for f in committed)
        assert not any(".board.json" in f for f in committed)
        # Never tracked, so a later cleanup unlink leaves no uncommitted deletion.
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=str(repo), capture_output=True, text=True,
        ).stdout
        assert ".build-state.json" not in tracked
        assert ".board.json" not in tracked


# --- commits and discards stay inside the project (rows C and D) ----------
#
# The project may be a subdirectory of a larger repo (`--no-worktree` in a
# monorepo, or the findings stage's `<worktree>/<project relative to the repo
# root>`). Row ids are the ones in plan-buildme-discard-safety-2026-10-13.md.


def _out(repo: Path, *argv: str) -> str:
    return _git(repo, *argv).stdout


def _project_repo(tmp_path: Path, name: str = "repo", subdir: bool = True) -> tuple[Path, Path]:
    """A repo whose project is ``pkg/app`` (or the root, with ``subdir=False``),
    with a committed change directory, two project files, and two files
    outside the project. Returns ``(root, project)``."""
    root = make_repo(tmp_path / name)
    proj = root / "pkg" / "app" if subdir else root
    change = proj / "specs" / "changes" / "feat"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# proposal\n")
    (proj / "app.py").write_text("x = 1\n")
    (proj / "gone.py").write_text("g = 1\n")
    if subdir:
        (root / "other").mkdir()
        (root / "other" / "notes.txt").write_text("v1\n")
        (root / "other" / "keep.txt").write_text("k1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, proj


def _head(repo: Path) -> str:
    return _out(repo, "rev-parse", "HEAD").strip()


def _do_step_work(proj: Path) -> None:
    """What a step leaves behind: a commit holding an edit, a deletion and a
    new file, then an uncommitted edit, an untracked file and bookkeeping.
    The commit is limited to the project, so it never takes anything else."""
    (proj / "app.py").write_text("x = 2\n")
    (proj / "gone.py").unlink()
    (proj / "added.py").write_text("a = 1\n")
    _git(proj, "add", "-A", "--", ".")
    _git(proj, "commit", "-qm", "step", "--", ".")
    (proj / "app.py").write_text("x = 3\n")
    (proj / "untracked.py").write_text("u = 1\n")
    _write_bookkeeping(proj)


def _assert_project_discarded(proj: Path) -> None:
    assert (proj / "app.py").read_text() == "x = 1\n", "the edit is reverted"
    assert (proj / "gone.py").read_text() == "g = 1\n", "the deleted file is back"
    assert not (proj / "added.py").exists(), "the committed new file is gone"
    assert not (proj / "untracked.py").exists(), "the untracked file is gone"
    assert (proj / "build-progress.md").exists()
    assert (proj / "specs/changes/feat/.build-state.json").exists()
    assert (proj / "specs/changes/feat/.board.json").exists()


def _stage_outside_edits(root: Path) -> None:
    """The user staged one edit outside the project and left another unstaged."""
    (root / "other" / "notes.txt").write_text("v2 user edit, staged\n")
    _git(root, "add", "other/notes.txt")
    (root / "other" / "keep.txt").write_text("k2 user edit, unstaged\n")


def _assert_outside_edits_kept(root: Path) -> None:
    assert (root / "other" / "notes.txt").read_text() == "v2 user edit, staged\n"
    assert "other/notes.txt" in _out(root, "diff", "--cached", "--name-only").split()
    assert (root / "other" / "keep.txt").read_text() == "k2 user edit, unstaged\n"
    assert "other/keep.txt" in _out(root, "diff", "--name-only").split()
    assert "other/keep.txt" not in _out(root, "diff", "--cached", "--name-only").split()


def _repo_view(root: Path) -> dict[str, str]:
    return {
        "status": _out(root, "status", "--porcelain", "--untracked-files=all"),
        "index": _out(root, "ls-files", "-s"),
    }


class TestCommitScope:
    """`commit_tree` and `commit_paths` commit only what they staged, so a
    file the user staged outside the project never goes into buildme's
    commit (rows C1-C5)."""

    @staticmethod
    def _root_work(root: Path) -> None:
        """An edit, a partly staged file, a deletion, a new file, an untracked
        `.build-state.json` and a modified tracked `build-progress.md`."""
        (root / "part.py").write_text("p = 1\n")
        (root / "build-progress.md").write_text("# progress 1\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "more base")
        (root / "app.py").write_text("x = 2\n")
        (root / "part.py").write_text("p = 2\n")
        _git(root, "add", "part.py")
        (root / "part.py").write_text("p = 3\n")
        (root / "gone.py").unlink()
        (root / "new.py").write_text("n = 1\n")
        (root / "specs/changes/feat/.build-state.json").write_text("{}\n")
        (root / "build-progress.md").write_text("# progress 2\n")

    def _two_roots(self, tmp_path: Path) -> tuple[Path, Path]:
        a, _ = _project_repo(tmp_path, "a", subdir=False)
        b, _ = _project_repo(tmp_path, "b", subdir=False)
        self._root_work(a)
        self._root_work(b)
        return a, b

    def test_row_C1_root_commit_tree_matches_a_bare_commit(self, tmp_path):
        _, repo = self._two_roots(tmp_path)
        before = _repo_view(repo)
        head = _head(repo)
        with pytest.raises(git_ops.GitLifecycleError):
            git_ops.commit_tree(repo, "block", "partially staged")
        assert _repo_view(repo) == before
        assert _head(repo) == head
        assert _out(repo, "show", ":part.py") == "p = 2\n"
        assert (repo / "part.py").read_text() == "p = 3\n"

    def test_row_C1_commit_paths_preserves_divergent_versions(self, tmp_path):
        _, repo = self._two_roots(tmp_path)
        before = _repo_view(repo)
        head = _head(repo)
        with pytest.raises(git_ops.GitLifecycleError):
            git_ops.commit_paths(repo, "spec", [".", *git_ops.BOOKKEEPING_EXCLUDES])
        assert _repo_view(repo) == before
        assert _head(repo) == head
        assert _out(repo, "show", ":part.py") == "p = 2\n"
        assert (repo / "part.py").read_text() == "p = 3\n"

    @pytest.mark.parametrize("whole_tree", [False, True])
    def test_commit_refusal_keeps_hidden_staged_work(self, tmp_path, whole_tree):
        root, _ = _project_repo(tmp_path, subdir=False)
        original = (root / "app.py").read_bytes()
        (root / "app.py").write_text("user staged B\n")
        _git(root, "add", "app.py")
        (root / "app.py").write_bytes(original)
        before, head = _repo_view(root), _head(root)
        with pytest.raises(git_ops.GitLifecycleError):
            (git_ops.commit_tree(root, "build") if whole_tree else
             git_ops.commit_paths(root, "plan", ["app.py"]))
        assert _repo_view(root) == before
        assert _head(root) == head
        assert _out(root, "show", ":app.py") == "user staged B\n"

    def test_commit_paths_failed_hook_preserves_active_index(self, tmp_path):
        root, _ = _project_repo(tmp_path, subdir=False)
        (root / "app.py").write_text("new working version\n")
        hooks = root / "test-hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _git(root, "config", "core.hooksPath", str(hooks))
        before, head = _repo_view(root), _head(root)
        with pytest.raises(git_ops.GitLifecycleError):
            git_ops.commit_paths(root, "plan", ["app.py"])
        assert _repo_view(root) == before
        assert _head(root) == head

    @pytest.mark.parametrize("commit", ["commit_tree", "commit_paths", "save_wip_ref"])
    def test_row_C2_a_staged_outside_edit_stays_staged(self, tmp_path, commit):
        root, proj = _project_repo(tmp_path)
        _stage_outside_edits(root)
        (proj / "app.py").write_text("x = 2\n")

        if commit == "commit_tree":
            assert git_ops.commit_tree(proj, "block", "row C2")
        elif commit == "commit_paths":
            assert git_ops.commit_paths(proj, "spec", ["app.py"])
        else:
            assert git_ops.save_wip_ref(proj, "refs/buildme/wip/feat/c2", "wip")

        start, end = ("HEAD", "refs/buildme/wip/feat/c2") if commit == "save_wip_ref" else ("HEAD~1", "HEAD")
        committed = _out(root, "diff", "--name-only", start, end).split()
        assert committed == ["pkg/app/app.py"]
        _assert_outside_edits_kept(root)

    def test_row_C3_a_path_staged_before_commit_paths_stays_staged(self, tmp_path):
        root, _ = _project_repo(tmp_path, subdir=False)
        (root / "part.py").write_text("p = 1\n")
        _git(root, "add", "part.py")
        (root / "app.py").write_text("x = 2\n")

        assert git_ops.commit_paths(root, "spec", ["app.py"])
        assert _out(root, "diff", "--name-only", "HEAD~1", "HEAD").split() == ["app.py"]
        assert _out(root, "diff", "--cached", "--name-only").split() == ["part.py"]

    def test_row_C3_a_staged_bookkeeping_file_stays_staged(self, tmp_path):
        root, _ = _project_repo(tmp_path, subdir=False)
        (root / "build-progress.md").write_text("# progress 1\n")
        _git(root, "add", "build-progress.md")
        _git(root, "commit", "-qm", "track progress")
        (root / "build-progress.md").write_text("# progress 2\n")
        _git(root, "add", "build-progress.md")
        (root / "app.py").write_text("x = 2\n")

        assert git_ops.commit_tree(root, "block", "row C3")
        assert _out(root, "diff", "--name-only", "HEAD~1", "HEAD").split() == ["app.py"]
        assert _out(root, "diff", "--cached", "--name-only").split() == ["build-progress.md"]

    def test_row_C5_commit_during_a_merge_reports_failure(self, tmp_path):
        root, _ = _project_repo(tmp_path, subdir=False)
        _git(root, "checkout", "-qb", "side")
        (root / "side.txt").write_text("s\n")
        _git(root, "add", "side.txt")
        _git(root, "commit", "-qm", "side")
        _git(root, "checkout", "-q", "main")
        _git(root, "merge", "--no-ff", "--no-commit", "side")
        (root / "app.py").write_text("x = 2\n")
        head = _head(root)

        with pytest.raises(git_ops.GitLifecycleError):
            git_ops.commit_tree(root, "block", "row C5")
        assert (root / ".git" / "MERGE_HEAD").exists()
        assert git_ops.save_wip_ref(root, "refs/buildme/wip/feat/c5", "wip") is None
        assert (root / ".git" / "MERGE_HEAD").exists()
        assert _head(root) == head


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


from build_pipeline.orchestrator import _pr_open_argv  # noqa: E402


class TestOpenPRWithSkill:
    """`gh pr create` moved into the pr skill (its tests: pr/scripts/tests/
    test_commands.py: URL, draft by default, --ready, failure exit codes).
    buildme's side runs the skill and reads its stdout lines."""

    def test_success_parses_the_pr_line(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake_pr(argv, cwd=None, timeout=None):
            seen["argv"] = argv
            seen["cwd"] = cwd
            return GitResult(argv, 0, "DIFF:2 files, +3 -1\nPR:https://github.com/o/r/pull/42\n", "")

        monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
        argv = _pr_open_argv(tmp_path, tmp_path / "c", "buildme/c", "buildme: c", draft=True)
        url, diagnoses, res = git_ops.open_pr_with_skill(tmp_path, argv)
        assert url == "https://github.com/o/r/pull/42"
        assert res.ok and diagnoses == []
        assert seen["cwd"] == tmp_path
        assert "--ready" not in seen["argv"]

    def test_ready_passes_ready(self, tmp_path):
        argv = _pr_open_argv(tmp_path, tmp_path / "c", "b", "t", draft=False)
        assert "--ready" in argv and "--draft" not in argv

    def test_draft_passes_draft_so_pr_draft_false_cannot_override_it(self, tmp_path):
        # git.pr: draft is buildme's choice; the skill's pr.draft must not flip it.
        argv = _pr_open_argv(tmp_path, tmp_path / "c", "b", "t", draft=True)
        assert "--draft" in argv and "--ready" not in argv

    def test_failure_returns_none_and_the_diagnosis_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_ops, "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 2, "PR_DIAGNOSIS:gh: not authenticated\n", ""),
        )
        url, diagnoses, res = git_ops.open_pr_with_skill(tmp_path, ["uv"])
        assert url is None
        assert not res.ok
        assert diagnoses == ["gh: not authenticated"]


class TestManualFinishCommands:
    def test_contains_exact_push_and_pr_commands(self, tmp_path):
        text = git_ops.manual_finish_commands(tmp_path / "wt", "buildme/c", "buildme: c")
        assert "git push -u origin buildme/c" in text
        assert "gh pr create --title 'buildme: c'" in text
        assert "--draft" in text

    def test_lists_the_pr_skill_before_the_gh_fallback(self, tmp_path):
        argv = _pr_open_argv(tmp_path / "wt", tmp_path / "c", "buildme/c", "buildme: c")
        text = git_ops.manual_finish_commands(
            tmp_path / "wt", "buildme/c", "buildme: c", pr_skill_argv=argv,
        )
        assert text.index("python -m pr_pipeline open") < text.index("gh pr create")

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


# --- whole-tree operations ------------------------------------------------


class TestWholeTreeOperations:
    """`commit_tree` / `tree_is_clean` / `rev_parse_head` — the TDD block
    loop's only whole-tree git calls. They must never sweep in, or delete,
    buildme's own bookkeeping."""

    def _bookkeeping(self, repo: Path) -> None:
        _write_bookkeeping(repo)

    def test_commit_tree_excludes_bookkeeping_at_every_depth(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        (repo / "module.py").write_text("x = 1\n")
        self._bookkeeping(repo)

        sha = git_ops.commit_tree(repo, "impl(block-1): B", "block=1")
        assert sha == git_ops.rev_parse_head(repo)

        committed = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
        assert "module.py" in committed
        assert "build-progress.md" not in committed
        assert not any(".build-state.json" in f for f in committed)
        assert not any(".board.json" in f for f in committed)

    def test_commit_tree_returns_none_when_there_is_nothing_to_commit(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.commit_tree(repo, "noop", "label") is None

    def test_commit_tree_reports_unavailable_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(git_ops.GitLifecycleError):
            git_ops.commit_tree(plain, "m", "label")

    def test_rev_parse_head_is_none_without_commits(self, tmp_path):
        repo = make_repo(tmp_path / "proj", commit=False)
        assert git_ops.rev_parse_head(repo) is None

    def test_tree_is_clean_ignores_bookkeeping_only(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.tree_is_clean(repo)
        self._bookkeeping(repo)
        assert git_ops.tree_is_clean(repo), "bookkeeping alone is still a clean tree"
        (repo / "module.py").write_text("x = 1\n")
        assert not git_ops.tree_is_clean(repo)

    def test_tree_is_clean_is_false_when_git_cannot_be_asked(self, tmp_path):
        """Unknown must never read as 'safe to reset'."""
        assert not git_ops.tree_is_clean(tmp_path / "nonexistent")

    def test_discard_keeps_bookkeeping_it_would_otherwise_clean(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        # The spec stages have already committed the change directory, which is
        # what the real layout looks like by the time a block runs — an
        # entirely *untracked* directory is removed wholesale by `clean -d`
        # whatever the file-level excludes say.
        change = repo / "specs" / "changes" / "feat"
        change.mkdir(parents=True)
        (change / "proposal.md").write_text("# proposal\n")
        (repo / "module.py").write_text("x = 1\n")
        base = git_ops.commit_tree(repo, "impl", "block=1")
        assert base is not None

        # A failed refactor: a tracked file edited and a new file created,
        # alongside bookkeeping written while the refactor ran.
        (repo / "module.py").write_text("x = 2\n")
        (repo / "extra.py").write_text("y = 1\n")
        self._bookkeeping(repo)

        assert git_ops.discard_to(repo, base, "refactor") is True
        assert (repo / "module.py").read_text() == "x = 1\n"
        assert not (repo / "extra.py").exists()
        assert (repo / "build-progress.md").exists()
        assert (repo / "specs/changes/feat/.build-state.json").exists()
        assert (repo / "specs/changes/feat/.board.json").exists()


class TestDiscardToAncestorGuard:
    """`git_ops.discard_to` may only ever move backwards along the current
    history.

    The refactorer holds `Bash`, so between the moment the impl commit is
    captured and the moment a failed refactor is discarded it can commit, move
    HEAD, or switch branches. A bare `reset --hard <sha>` would then throw away
    whatever HEAD had actually reached — earlier blocks' commits, or under
    `--no-worktree` the user's own work.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        project = make_repo(tmp_path / "proj", commit=False)

        def commit(text):
            (project / "README.md").write_text(text)
            _git(project, "add", "-A")
            _git(project, "commit", "-qm", text)
            return _git(project, "rev-parse", "HEAD").stdout.strip()

        first = commit("one")
        second = commit("two")
        return project, first, second, commit

    def test_discards_to_an_ancestor(self, repo):
        project, first, second, _ = repo
        assert git_ops.discard_to(project, first, "refactor") is True
        assert _git(project, "rev-parse", "HEAD").stdout.strip() == first

    def test_head_itself_is_an_ancestor(self, repo):
        project, _, second, _ = repo
        assert git_ops.is_ancestor_of_head(project, second) is True
        assert git_ops.discard_to(project, second, "refactor") is True

    def test_refuses_a_sha_on_unrelated_history(self, repo):
        """The agent switched branches: the impl sha is no longer reachable."""
        project, _, second, _ = repo
        _git(project, "checkout", "-q", "--orphan", "elsewhere")
        (project / "OTHER.md").write_text("other\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "orphan")
        before = _git(project, "rev-parse", "HEAD").stdout.strip()

        assert git_ops.discard_to(project, second, "refactor") is False
        after = _git(project, "rev-parse", "HEAD").stdout.strip()
        assert after == before, "HEAD must not move when the sha is unreachable"

    def test_refuses_an_unknown_sha(self, repo):
        project, _, _, _ = repo
        assert git_ops.discard_to(project, "0" * 40, "refactor") is False

    def test_refuses_an_empty_sha(self, repo):
        project, _, _, _ = repo
        assert git_ops.is_ancestor_of_head(project, "") is False
        assert git_ops.discard_to(project, "", "refactor") is False

    def test_unknown_ancestry_is_never_treated_as_safe(self, tmp_path):
        """Git could not be asked → "unknown", which must not mean "destroy"."""
        assert git_ops.is_ancestor_of_head(tmp_path / "nonexistent", "a" * 40) is False


class TestDiscardInSubdirectory:
    """`discard_to` throws away the step's work inside the project and
    nothing else, and refuses when a commit it would drop touches a file
    outside the project (rows D1-D7)."""

    @pytest.fixture
    def sub(self, tmp_path):
        root, proj = _project_repo(tmp_path)
        return root, proj, _head(root)

    def test_row_D1_root_project_matches_reset_hard(self, tmp_path):
        ref, _ = _project_repo(tmp_path, "a", subdir=False)
        repo, _ = _project_repo(tmp_path, "b", subdir=False)
        ref_start, start = _head(ref), _head(repo)
        for r in (ref, repo):
            _do_step_work(r)
        _git(ref, "reset", "-q", "--hard", ref_start)
        _git(ref, "clean", "-fdq", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES)

        assert git_ops.discard_to(repo, start, "row D1") is True
        assert _head(repo) == start
        assert _out(repo, "rev-parse", "HEAD^{tree}") == _out(ref, "rev-parse", "HEAD^{tree}")
        assert _out(repo, "log", "--format=%s") == _out(ref, "log", "--format=%s")
        assert _repo_view(repo) == _repo_view(ref)
        _assert_project_discarded(repo)

    def test_row_D2_outside_edit_survives_unstaged(self, sub):
        root, proj, start = sub
        _do_step_work(proj)
        (root / "other" / "keep.txt").write_text("k2 user edit, unstaged\n")

        assert git_ops.discard_to(proj, start, "row D2") is True
        assert _head(root) == start
        assert (root / "other" / "keep.txt").read_text() == "k2 user edit, unstaged\n"
        assert _out(root, "diff", "--name-only").split() == ["other/keep.txt"]
        _assert_project_discarded(proj)

    def test_row_D2_outside_edit_survives_staged(self, sub):
        root, proj, start = sub
        _do_step_work(proj)
        _stage_outside_edits(root)

        assert git_ops.discard_to(proj, start, "row D2") is True
        assert _head(root) == start
        _assert_outside_edits_kept(root)
        _assert_project_discarded(proj)

    def test_row_D2_staged_outside_edit_survives_keep_then_discard(self, sub):
        root, proj, start = sub
        _do_step_work(proj)
        _stage_outside_edits(root)

        ref = git_ops.save_wip_ref(proj, "refs/buildme/wip/feat/d2", "wip")
        assert ref
        assert git_ops.discard_to(proj, start, "row D2") is True
        assert _head(root) == start
        kept = _out(root, "diff", "--name-only", start, ref).split()
        assert kept and all(p.startswith("pkg/app/") for p in kept), kept
        _assert_outside_edits_kept(root)
        _assert_project_discarded(proj)

    def test_row_D3_outside_untracked_survives(self, sub):
        root, proj, start = sub
        _do_step_work(proj)
        (root / "other" / "new.txt").write_text("untracked user file\n")

        assert git_ops.discard_to(proj, start, "row D3") is True
        assert (root / "other" / "new.txt").read_text() == "untracked user file\n"
        _assert_project_discarded(proj)

    def test_row_D4_outside_commit_refuses(self, sub):
        root, proj, start = sub
        _do_step_work(proj)
        (root / "other" / "notes.txt").write_text("v2 committed by the user\n")
        _git(root, "commit", "-qm", "user", "--", "other/notes.txt")
        before = (_head(root), _repo_view(root), _out(root, "diff"),
                  (proj / "app.py").read_text(), (proj / "untracked.py").read_text())

        assert git_ops.discard_to(proj, start, "row D4") is False
        after = (_head(root), _repo_view(root), _out(root, "diff"),
                 (proj / "app.py").read_text(), (proj / "untracked.py").read_text())
        assert after == before
        assert (root / "other" / "notes.txt").read_text() == "v2 committed by the user\n"

    def test_row_D4_inside_commit_discards(self, sub):
        root, proj, start = sub
        _do_step_work(proj)

        assert git_ops.discard_to(proj, start, "row D4") is True
        assert _head(root) == start
        _assert_project_discarded(proj)

    def test_row_D5_agent_outside_edit_left_in_place(self, sub):
        root, proj, start = sub
        _do_step_work(proj)
        (root / "other" / "notes.txt").write_text("v2 agent edit\n")

        assert git_ops.discard_to(proj, start, "row D5") is True
        assert (root / "other" / "notes.txt").read_text() == "v2 agent edit\n"
        assert " M other/notes.txt" in _out(root, "status", "--porcelain").splitlines()
        _assert_project_discarded(proj)

    @staticmethod
    def _fail_at(monkeypatch, git_command: str) -> None:
        real = git_ops._run

        def run(argv, *a, **k):
            if argv[1:2] == [git_command]:
                return GitResult(list(argv), 1, "", f"{git_command} failed (test)")
            return real(argv, *a, **k)

        monkeypatch.setattr(git_ops, "_run", run)

    def test_row_D6_restore_failure_leaves_work_staged_at_sha(self, sub, monkeypatch):
        root, proj, start = sub
        _do_step_work(proj)
        self._fail_at(monkeypatch, "restore")

        assert git_ops.discard_to(proj, start, "row D6") is False
        assert _head(root) == start
        staged = _out(root, "diff", "--cached", "--name-status").splitlines()
        assert sorted(staged) == ["A\tpkg/app/added.py", "D\tpkg/app/gone.py",
                                  "M\tpkg/app/app.py"]
        assert (proj / "app.py").read_text() == "x = 3\n"
        assert (proj / "added.py").exists()
        assert (proj / "untracked.py").exists()

    def test_row_D7_clean_failure_returns_false(self, sub, monkeypatch):
        root, proj, start = sub
        _do_step_work(proj)
        self._fail_at(monkeypatch, "clean")

        assert git_ops.discard_to(proj, start, "row D7") is False
        assert _head(root) == start
        assert (proj / "app.py").read_text() == "x = 1\n"
        assert (proj / "untracked.py").exists()


class TestSaveWipRef:
    """`save_wip_ref` keeps a step's work reachable before a discard. A second
    attempt at the same step must not move the first attempt's ref: its
    commit would then be reachable from nothing and could be collected."""

    def test_a_second_save_under_the_same_name_keeps_the_first(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        ref = "refs/buildme/wip/calc/r1-spec"

        (repo / "attempt.txt").write_text("first\n")
        first = git_ops.save_wip_ref(repo, ref, "wip one")
        assert first == ref
        first_sha = _git(repo, "rev-parse", ref).stdout.strip()
        assert git_ops.discard_to(repo, base, "spec")

        (repo / "attempt.txt").write_text("second\n")
        second = git_ops.save_wip_ref(repo, ref, "wip two")
        assert second == f"{ref}-2"
        assert _git(repo, "rev-parse", ref).stdout.strip() == first_sha
        assert _git(repo, "show", f"{ref}:attempt.txt").stdout == "first\n"
        assert _git(repo, "show", f"{second}:attempt.txt").stdout == "second\n"
        assert git_ops.discard_to(repo, base, "spec")

        (repo / "attempt.txt").write_text("third\n")
        assert git_ops.save_wip_ref(repo, ref, "wip three") == f"{ref}-3"

    def test_saving_the_same_commit_again_reuses_its_ref(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        ref = "refs/buildme/wip/calc/r1-code-stopped"
        (repo / "attempt.txt").write_text("x\n")
        assert git_ops.save_wip_ref(repo, ref, "wip") == ref
        assert git_ops.save_wip_ref(repo, ref, "wip") == ref
        refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/buildme/wip/").stdout
        assert refs.split() == [ref]

    def test_a_ref_outside_the_wip_namespace_is_refused(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        assert git_ops.save_wip_ref(repo, "refs/heads/main", "wip") is None

    def test_a_rejected_wip_commit_writes_no_ref(self, tmp_path, monkeypatch):
        """When the WIP commit fails (a pre-commit hook rejects it), HEAD
        does not hold the edits, so a ref at HEAD would keep nothing and the
        caller's discard would destroy them. No ref, and the edits stay."""
        repo = make_repo(tmp_path / "proj")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (repo / "attempt.txt").write_text("unsaved\n")
        ref = "refs/buildme/wip/calc/r1-code-block1"

        original = git_ops._run
        def fail_commit_tree(argv, **kwargs):
            if argv[:2] == ["git", "commit-tree"]:
                return git_ops.GitResult(argv, 1, "", "object write failed")
            return original(argv, **kwargs)
        monkeypatch.setattr(git_ops, "_run", fail_commit_tree)
        assert git_ops.save_wip_ref(repo, ref, "wip") is None
        assert subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                              cwd=str(repo), capture_output=True).returncode != 0
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
        assert (repo / "attempt.txt").read_text() == "unsaved\n"

    def test_a_clean_tree_with_moved_head_still_gets_a_ref(self, tmp_path):
        """Nothing uncommitted (the step committed its own work) is not a
        failure: the ref names HEAD, which holds everything."""
        repo = make_repo(tmp_path / "proj")
        (repo / "attempt.txt").write_text("committed\n")
        _git(repo, "add", "attempt.txt")
        _git(repo, "commit", "-q", "-m", "step's own commit")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        ref = "refs/buildme/wip/calc/r1-spec"

        assert git_ops.save_wip_ref(repo, ref, "wip") == ref
        assert _git(repo, "rev-parse", ref + "^1").stdout.strip() == head


# --- skill subprocesses: a timeout kills the whole process tree ----------------


def _gone(pid: int, wait: float = 5.0) -> bool:
    """True once *pid* no longer runs (gone, or a zombie nobody reaped yet)."""
    import os
    import time
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z":
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


# The pr skill's seam is refused by conftest; it is the same one-line call to `_run_skill`.
@pytest.mark.parametrize("seam", ["_run_review_skill", "_run_walkthrough_skill", "_run_skill"])
def test_a_skill_timeout_kills_the_launchers_child_too(tmp_path, seam):
    """`uv run` starts the skill as its own child. A timeout that killed only
    `uv` left the review running: it kept the target's run lock (the next
    `review` exited 2, not 4) and later died on a closed pipe, recorded as a
    failure, so the findings stage never resumed it."""
    import os
    import signal
    pidfile = tmp_path / "child.pid"
    # The launcher (sh) starts a child that outlives it unless it is killed.
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    res = getattr(git_ops, seam)(["sh", "-c", script], tmp_path, 1)
    assert res.returncode == 124 and "timed out" in res.stderr
    pid = int(pidfile.read_text())
    try:
        assert _gone(pid), "the launcher's child was left running"
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_skill_run_returns_output_and_exit_code(tmp_path):
    res = git_ops._run_review_skill(["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=tmp_path)
    assert (res.returncode, res.stdout, res.stderr) == (3, "out\n", "err\n")


def test_a_missing_skill_launcher_is_exit_127(tmp_path):
    res = git_ops._run_review_skill(["no-such-launcher-xyz"], cwd=tmp_path)
    assert res.returncode == 127


# A skill runs in a session of its own, so a signal sent to buildme (a closed
# terminal, `kill`, Ctrl-C) no longer reaches it. `_run_skill` must pass the
# signal on by killing the skill's group itself.
_SKILL_RUNNER = """
import asyncio, sys
from build_pipeline import git_ops
script, mode = sys.argv[1], sys.argv[2]
def call():
    return git_ops._run_skill(["sh", "-c", script], None, 120)
if mode == "asyncio":
    async def main():
        call()
    asyncio.run(main())
else:
    call()
"""


@pytest.mark.parametrize("signame,mode", [("SIGTERM", "plain"), ("SIGHUP", "plain"),
                                          ("SIGTERM", "asyncio"), ("SIGINT", "asyncio")])
def test_a_signal_to_buildme_kills_the_skill_it_is_waiting_on(tmp_path, signame, mode):
    """Before, a SIGTERM or SIGHUP ended buildme at once with no cleanup, and
    the review, in its own session, kept running: it spent model budget and
    held the target's run lock, so the next round's review exited 2. Under
    `asyncio.run` the first Ctrl-C only cancels the main task, which a loop
    blocked in the wait never sees, so the review ran to the end."""
    import os
    import signal
    import sys
    import time
    sig = getattr(signal, signame)
    pidfile = tmp_path / "child.pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    scripts_dir = Path(git_ops.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(scripts_dir)}
    runner = subprocess.Popen([sys.executable, "-c", _SKILL_RUNNER, script, mode],
                              cwd=str(scripts_dir), env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    child = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (
                pidfile.exists() and pidfile.read_text().strip()):
            time.sleep(0.05)
        child = int(pidfile.read_text())
        time.sleep(0.3)  # let the runner settle into the wait
        runner.send_signal(sig)
        try:
            runner.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{signame} did not end buildme's wait on the skill")
        assert _gone(child), f"{signame} to buildme left the skill running"
        if sig != signal.SIGINT:
            assert runner.returncode == 128 + sig, runner.stderr.read()
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
        if runner.stderr is not None:
            runner.stderr.close()
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("whole_tree", [False, True])
@pytest.mark.parametrize("rename", [False, True])
def test_commit_preserves_staged_removal_and_recreated_worktree(tmp_path, whole_tree, rename):
    root, _ = _project_repo(tmp_path, subdir=False)
    if rename:
        _git(root, "mv", "app.py", "renamed.py")
    else:
        _git(root, "rm", "--cached", "app.py")
    (root / "app.py").write_text("recreated working version\n")
    before, head = _repo_view(root), _head(root)
    with pytest.raises(git_ops.GitLifecycleError):
        (git_ops.commit_tree(root, "build") if whole_tree else
         git_ops.commit_paths(root, "plan", ["."]))
    assert _repo_view(root) == before
    assert _head(root) == head
    assert "app.py" not in git_ops.index_entries(root)


def test_agent_staging_then_edit_can_commit_after_restart(tmp_path):
    root, _ = _project_repo(tmp_path, subdir=False)
    with git_ops.agent_write_window(root):
        (root / 'app.py').write_text('agent first\n')
        _git(root, 'add', 'app.py')
        (root / 'app.py').write_text('agent final\n')
    # Evidence is on disk, not in a process-global ownership cache.
    assert git_ops.commit_tree(root, 'agent implementation')
    assert _out(root, 'show', 'HEAD:app.py') == 'agent final\n'
    assert git_ops.tree_is_clean(root)


def test_agent_write_window_never_claims_existing_user_staging(tmp_path):
    root, _ = _project_repo(tmp_path, subdir=False)
    (root / 'app.py').write_text('user staged\n')
    _git(root, 'add', 'app.py')
    (root / 'app.py').write_text('user working\n')
    with git_ops.agent_write_window(root):
        (root / 'app.py').write_text('agent edit\n')
    with pytest.raises(git_ops.GitLifecycleError, match='staged and working'):
        git_ops.commit_tree(root, 'must refuse')
    assert _out(root, 'show', ':app.py') == 'user staged\n'
    assert (root / 'app.py').read_text() == 'agent edit\n'


def test_later_external_staging_invalidates_agent_ownership(tmp_path):
    root, _ = _project_repo(tmp_path, subdir=False)
    with git_ops.agent_write_window(root):
        (root / 'app.py').write_text('agent staged\n')
        _git(root, 'add', 'app.py')
    (root / 'app.py').write_text('user staged later\n')
    _git(root, 'add', 'app.py')
    (root / 'app.py').write_text('user working later\n')
    with pytest.raises(git_ops.GitLifecycleError, match='staged and working'):
        git_ops.commit_tree(root, 'must refuse')
    assert _out(root, 'show', ':app.py') == 'user staged later\n'


def test_unstaged_user_edit_after_agent_window_invalidates_ownership(tmp_path):
    root, _ = _project_repo(tmp_path, subdir=False)
    with git_ops.agent_write_window(root):
        (root / 'app.py').write_text('agent staged\n')
        _git(root, 'add', 'app.py')
        (root / 'app.py').write_text('agent working\n')
    (root / 'app.py').write_text('user after stop\n')
    before, head = _repo_view(root), _head(root)
    with pytest.raises(git_ops.GitLifecycleError, match='staged and working'):
        git_ops.commit_tree(root, 'must refuse')
    assert _repo_view(root) == before
    assert _head(root) == head


def test_unrecorded_staging_names_paths_and_can_be_resolved_explicitly(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    source = repo / 'README.md'
    source.write_text('staged before interruption\n')
    _git(repo, 'add', 'README.md')
    source.write_text('working after interruption\n')
    with pytest.raises(GitLifecycleError, match='README.md') as stopped:
        git_ops.commit_tree(repo, 'agent update')
    assert 'git diff --cached' in str(stopped.value)
    assert _git(repo, 'show', ':README.md').stdout == 'staged before interruption\n'
    assert source.read_text() == 'working after interruption\n'
    # The documented action explicitly selects the working version.
    _git(repo, 'add', 'README.md')
    assert git_ops.commit_tree(repo, 'chosen working version')
    assert _git(repo, 'show', 'HEAD:README.md').stdout == 'working after interruption\n'


def test_output_observation_preserves_intervening_user_staging(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    (repo / 'specs').mkdir()
    (repo / 'specs/config.yaml').write_text('test_outputs: [report.txt]\n')
    _git(repo, 'add', 'specs/config.yaml')
    _git(repo, 'commit', '-qm', 'declare report')
    report = repo / 'report.txt'
    with git_ops.agent_write_window(repo):
        report.write_text('agent output\n')
        _git(repo, 'add', 'report.txt')
    with pytest.raises(GitLifecycleError, match='report.txt'):
        with git_ops.test_output_window(repo):
            report.write_text('new user staging\n')
            _git(repo, 'add', 'report.txt')
            report.write_text('test run output\n')
    assert _git(repo, 'show', ':report.txt').stdout == 'new user staging\n'
    assert report.read_text() == 'test run output\n'
