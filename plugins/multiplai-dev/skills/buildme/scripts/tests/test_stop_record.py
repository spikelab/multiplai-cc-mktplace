"""`stop_record`: the one check a resume runs before it keeps or discards a
stopped step's work. One test per rule of its contract, in a real git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from build_pipeline import git_ops, stop_record
from build_pipeline.stop_record import StepStop

SCRIPTS = Path(__file__).resolve().parent.parent


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A project at the root of its repo, with app.py and lib.py committed."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("x = 1\n")
    (root / "lib.py").write_text("y = 1\n")
    (root / "records").mkdir()
    (root / "records" / "keep.json").write_text("{}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


# --- rule 1: scope -------------------------------------------------------------


def test_bookkeeping_and_the_ignored_dir_are_not_refused(repo):
    (repo / "build-progress.md").write_text("# progress\n")
    (repo / ".build-state.json").write_text("{}\n")
    (repo / "records" / "keep.json").write_text('{"round": 2}\n')
    (repo / "records" / "new.json").write_text("{}\n")
    assert stop_record.others_work(repo, None, ignore="records", head=_head(repo)) is None


def test_an_edit_outside_a_subdirectory_project_is_not_refused(repo):
    proj = repo / "pkg"
    proj.mkdir()
    (proj / "mod.py").write_text("z = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pkg")
    (repo / "app.py").write_text("x = 2  # the user's edit outside the project\n")
    _git(repo, "add", "app.py")
    (repo / "lib.py").write_text("y = 2\n")
    assert stop_record.others_work(proj, None, head=_head(repo)) is None
    (proj / "mod.py").write_text("z = 2\n")
    assert "mod.py" in stop_record.others_work(proj, None, head=_head(repo))


# --- rule 2: git fails -------------------------------------------------------


def test_git_failing_to_list_changes_refuses(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    reason = stop_record.others_work(plain, None)
    assert "git could not" in reason and str(plain) in reason


def test_git_failing_to_read_head_refuses(repo, monkeypatch):
    monkeypatch.setattr(git_ops, "rev_parse_head", lambda _repo: None)
    reason = stop_record.others_work(repo, None, head="a" * 40)
    assert "git could not" in reason and str(repo) in reason


def test_record_is_none_when_git_fails(tmp_path, repo, monkeypatch):
    assert stop_record.record(tmp_path / "nowhere") is None
    monkeypatch.setattr(git_ops, "rev_parse_head", lambda _repo: None)
    assert stop_record.record(repo) is None


def test_record_never_raises(repo, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr(git_ops, "dirty_files", boom)
    assert stop_record.record(repo) is None


def test_others_work_never_raises(repo, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr(git_ops, "dirty_files", boom)
    assert "git could not" in stop_record.others_work(repo, None)


# --- rule 3: no record -------------------------------------------------------


def test_no_record_refuses_any_uncommitted_path(repo):
    (repo / "app.py").write_text("x = 2\n")
    reason = stop_record.others_work(repo, None)
    assert "uncommitted" in reason and "app.py" in reason
    assert "cannot be told apart" in reason and "stash" in reason
    assert "\n" not in reason


def test_no_record_on_a_clean_tree_passes(repo):
    assert stop_record.others_work(repo, None, head=_head(repo)) is None


def test_no_record_names_at_most_five_paths(repo):
    for i in range(7):
        (repo / f"f{i}.py").write_text("n\n")
    reason = stop_record.others_work(repo, None)
    assert "f4.py" in reason and "f5.py" not in reason and "and 2 more" in reason


# --- rule 4: record ----------------------------------------------------------


def test_the_recorded_edits_pass(repo):
    (repo / "app.py").write_text("x = 2  # the agent's edit\n")
    stop = stop_record.record(repo)
    assert stop is not None and stop.head == _head(repo) and set(stop.edits) == {"app.py"}
    assert stop_record.others_work(repo, stop, head=stop.head) is None


def test_a_path_missing_from_the_record_is_refused(repo):
    (repo / "app.py").write_text("x = 2  # the agent's edit\n")
    stop = stop_record.record(repo)
    (repo / "mine.py").write_text("the user's file\n")
    reason = stop_record.others_work(repo, stop, head=stop.head)
    assert "uncommitted" in reason and "did not leave" in reason
    assert "mine.py" in reason and "app.py" not in reason


def test_a_recorded_path_changed_after_the_stop_is_refused(repo):
    (repo / "app.py").write_text("x = 2  # the agent's edit\n")
    stop = stop_record.record(repo)
    (repo / "app.py").write_text("x = 2  # the agent's edit\n# and the user's line\n")
    reason = stop_record.others_work(repo, stop, head=stop.head)
    assert "uncommitted" in reason and "did not leave" in reason and "app.py" in reason


def test_a_recorded_path_now_clean_is_not_refused(repo):
    (repo / "app.py").write_text("x = 2\n")
    stop = stop_record.record(repo)
    (repo / "app.py").write_text("x = 1\n")
    assert stop_record.others_work(repo, stop, head=stop.head) is None


def test_a_recorded_path_now_staged_with_the_same_content_passes(repo):
    (repo / "app.py").write_text("x = 2\n")
    stop = stop_record.record(repo)
    _git(repo, "add", "app.py")
    assert "index changed" in stop_record.others_work(repo, stop, head=stop.head)


# --- rule 5: HEAD --------------------------------------------------------------


def test_head_equal_passes_and_none_is_not_checked(repo):
    stop = stop_record.record(repo)
    (repo / "lib.py").write_text("y = 2\n")
    _git(repo, "commit", "-qam", "the user's commit")
    assert stop_record.others_work(repo, stop, head=None) is None
    assert stop_record.others_work(repo, stop_record.record(repo), head=_head(repo)) is None


def test_a_commit_after_the_stop_is_refused(repo):
    stop = stop_record.record(repo)
    (repo / "lib.py").write_text("y = 2\n")
    _git(repo, "commit", "-qam", "the user's commit")
    reason = stop_record.others_work(repo, stop, head=stop.head)
    assert "commit" in reason and stop.head[:12] in reason and "another branch" in reason
    assert "\n" not in reason


def test_head_wholly_on_origin_passes(repo, tmp_path):
    stop = stop_record.record(repo)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    branch = _git(repo, "branch", "--show-current")
    (repo / "lib.py").write_text("y = 2\n")
    _git(repo, "commit", "-qam", "on origin")
    _git(repo, "push", "-q", "origin", branch)
    _git(repo, "fetch", "-q", "origin")
    ref = f"refs/remotes/origin/{branch}"
    assert stop_record.others_work(repo, stop, head=stop.head, origin_ref=ref) is None
    (repo / "lib.py").write_text("y = 3\n")
    _git(repo, "commit", "-qam", "not on origin")
    assert "commit" in stop_record.others_work(repo, stop, head=stop.head, origin_ref=ref)


# --- one copy ------------------------------------------------------------------


def test_only_stop_record_compares_stop_records():
    """`dirty_files(` appears only at its definition and in `stop_record`, so
    no other module grows its own copy of the check."""
    users = sorted(p.relative_to(SCRIPTS).as_posix()
                   for p in (SCRIPTS / "build_pipeline").rglob("*.py")
                   if "dirty_files(" in p.read_text())
    assert users == ["build_pipeline/git_ops.py", "build_pipeline/stop_record.py"]


def test_state_still_exports_stepstop():
    from build_pipeline.state import StepStop as FromState
    assert FromState is StepStop


def test_staged_user_blob_hidden_by_restored_worktree_is_refused(repo):
    (repo / "app.py").write_text("agent\n")
    stop = stop_record.record(repo)
    (repo / "app.py").write_text("user\n")
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text("agent\n")
    before = _git(repo, "ls-files", "--stage")
    assert "index" in stop_record.others_work(repo, stop)
    assert _git(repo, "ls-files", "--stage") == before
    assert _git(repo, "show", ":app.py") == "user"
    assert (repo / "app.py").read_text() == "agent\n"


def test_legacy_record_cannot_authorize_discard(repo):
    stop = StepStop(head=_head(repo), edits={})
    assert "legacy" in stop_record.others_work(repo, stop)


def test_wip_keeps_index_and_worktree_without_staging(repo):
    (repo / "app.py").write_text("staged\n")
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text("working\n")
    before = (_head(repo), _git(repo, "ls-files", "--stage"))
    ref = git_ops.save_wip_ref(repo, "refs/buildme/wip/test/stop", "save")
    assert ref
    assert (_head(repo), _git(repo, "ls-files", "--stage")) == before
    assert _git(repo, "show", f"{ref}:app.py") == "working"
    assert _git(repo, "show", f"{ref}^2:app.py") == "staged"
