"""Resolving a target and choosing its run folder (plan, Decided 14 and item 2)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    FIXED,
    Killed,
    commit_file,
    everything,
    fake_state,
    git,
    make_repo,
    read_state,
    run,
    source_head,
    write,
)
from spec_extract_pipeline import target as target_mod

# These tests are about resolve, scope and explore: the run ends after explore, as it did
# before the group and write steps existed.
pytestmark = pytest.mark.usefixtures("stops_after_explore")

OTHER = "https://github.com/other/widgets.git"


def _hex6(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:6]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widgets")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "out"


def head12(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")[:12]


# --- names ----------------------------------------------------------------------


def test_whole_tree(repo, root, capsys):
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    folder = root / "widgets" / f"tree@{head12(repo)}"
    assert f"SPEC_EXTRACT:RESOLVED {folder}" in capsys.readouterr().out
    state = read_state(folder)
    assert state["steps_done"] == ["resolve", "scope", "explore"]
    assert state["target"]["kind"] == "tree"
    assert state["target"]["head_sha"] == git(repo, "rev-parse", "HEAD")
    assert state["target"]["remote_url"] == "https://github.com/acme/widgets.git"
    assert (folder / "snapshot" / "src" / "billing" / "charge.py").is_file()
    assert (folder / "snapshot" / ".spec-extract-head").read_text().strip() == state[
        "target"
    ]["head_sha"]
    assert (root / "widgets" / ".locks" / f"tree@{head12(repo)}.lock").is_file()


def test_path_is_cleaned(repo, root):
    assert (
        run(
            "run",
            "--repo",
            repo,
            "--tree",
            "HEAD",
            "--path",
            "src/billing/",
            "--out",
            root,
        )
        == 0
    )
    folder = root / "widgets" / f"tree-src_billing@{head12(repo)}"
    assert read_state(folder)["target"]["path"] == "src/billing"


def test_path_from_a_subdirectory_is_repo_relative(repo, root):
    assert (
        run(
            "run",
            "--repo",
            repo / "src",
            "--tree",
            "HEAD",
            "--path",
            "billing",
            "--out",
            root,
        )
        == 0
    )
    assert (
        read_state(root / "widgets" / f"tree-src_billing@{head12(repo)}")["target"][
            "path"
        ]
        == "src/billing"
    )


def test_path_outside_the_repo_is_refused(repo, root, capsys):
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--path", "../x", "--out", root)
        == 2
    )
    assert "outside" in capsys.readouterr().err


def test_no_origin_uses_the_cleaned_directory_name(tmp_path, root):
    repo = make_repo(tmp_path / "My Repo!", origin=None)
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    folder = root / "My_Repo" / f"tree@{head12(repo)}"
    assert read_state(folder)["target"]["remote_url"] is None


def test_legacy_repo_gets_its_owner(tmp_path, root):
    repo = make_repo(tmp_path / "legacy", origin="git@github.com:o/legacy.git")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert (root / "legacy@o" / f"tree@{head12(repo)}" / "state.json").is_file()


def test_legacy_repo_without_owner_is_local(tmp_path, root):
    repo = make_repo(tmp_path / "legacy", origin=None)
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert (root / "legacy@local" / f"tree@{head12(repo)}" / "state.json").is_file()


def test_unknown_commit_is_refused(repo, root, capsys):
    assert run("run", "--repo", repo, "--tree", "nope", "--out", root) == 2
    assert "nope" in capsys.readouterr().err
    assert not root.exists()


def test_exactly_one_target(repo, root, capsys):
    assert run("run", "--repo", repo, "--tree", "HEAD", "--pr", "3", "--out", root) == 2
    assert run("run", "--repo", repo, "--out", root) == 2
    assert run("run", "--repo", repo, "--pr", "3", "--path", "src", "--out", root) == 2


# --- folders another run wrote ----------------------------------------------------


def test_pr_folder_of_another_remote_moves_to_owner(repo, root, pr_view):
    base = git(repo, "rev-parse", "HEAD")
    head = commit_file(
        repo, "src/billing/charge.py", "def charge(amount):\n    return amount * 2\n"
    )
    pr_view.info[12] = {"headRefOid": head, "baseRefOid": base}
    theirs = root / "widgets" / f"pr-12@{head[:12]}"
    fake_state(theirs, kind="pr", remote_url=OTHER)
    before = (theirs / "state.json").read_bytes()
    assert run("run", "--repo", repo, "--pr", "12", "--out", root) == 0
    ours = root / "widgets@acme" / f"pr-12@{head[:12]}"
    assert read_state(ours)["target"]["pr"] == 12
    assert (theirs / "state.json").read_bytes() == before
    assert sorted(p.name for p in theirs.iterdir()) == ["state.json"]


def test_same_remote_in_another_url_form_is_this_remote(repo, root):
    sha = git(repo, "rev-parse", "HEAD")
    folder = root / "widgets" / f"tree@{sha[:12]}"
    fake_state(
        folder,
        kind="tree",
        repo=str(repo),
        head_sha=sha,
        path="",
        remote_url="git@github.com:ACME/widgets.git",
    )
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    state = read_state(folder)  # this target's folder: its state is resumed
    assert state["steps_done"] == ["resolve", "scope", "explore"]
    assert state["target"]["remote_url"] == "git@github.com:ACME/widgets.git"
    assert not (root / "widgets@acme").exists()


def test_folder_naming_no_remote_is_used(repo, root):
    sha = git(repo, "rev-parse", "HEAD")
    folder = root / "widgets" / f"tree@{sha[:12]}"
    fake_state(folder, kind="tree", repo=str(repo), head_sha=sha, path="")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert read_state(folder)["steps_done"] == ["resolve", "scope", "explore"]
    assert not (root / "widgets@acme").exists()


def test_two_paths_that_clean_to_one_name(repo, root):
    sha12 = head12(repo)
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--path", "src_a", "--out", root)
        == 0
    )
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--path", "src/a", "--out", root)
        == 0
    )
    first = root / "widgets" / f"tree-src_a@{sha12}"
    second = root / "widgets" / f"tree-src_a-{_hex6('src/a')}@{sha12}"
    assert read_state(first)["target"]["path"] == "src_a"
    assert read_state(second)["target"]["path"] == "src/a"
    assert not (root / "widgets@acme").exists()
    assert not (root / "widgets@local").exists()
    # each resumes in its own folder
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--path", "src/a", "--out", root)
        == 0
    )
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--path", "src_a", "--out", root)
        == 0
    )
    assert sorted(p.name for p in (root / "widgets").iterdir()) == sorted(
        [".locks", first.name, second.name]
    )


def test_legacy_folder_of_another_remote_is_refused(tmp_path, root, capsys):
    repo = make_repo(tmp_path / "legacy", origin="git@github.com:o/legacy.git")
    folder = root / "legacy@o" / f"tree@{head12(repo)}"
    fake_state(
        folder, kind="tree", path="", remote_url="https://github.com/p/legacy.git"
    )
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 2
    err = capsys.readouterr().err
    assert str(folder) in err and "--out" in err


def test_every_candidate_taken_is_refused(repo, root, capsys):
    sha12 = head12(repo)
    fake_state(
        root / "widgets" / f"tree@{sha12}", kind="tree", path="", remote_url=OTHER
    )
    fake_state(
        root / "widgets@acme" / f"tree@{sha12}", kind="tree", path="", remote_url=OTHER
    )
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 2
    err = capsys.readouterr().err
    assert str(root / "widgets" / f"tree@{sha12}") in err
    assert str(root / "widgets@acme" / f"tree@{sha12}") in err
    assert "--out" in err


def test_every_path_candidate_taken_is_refused(repo, root, capsys):
    sha12 = head12(repo)
    first = root / "widgets" / f"tree-src_a@{sha12}"
    hashed = root / "widgets" / f"tree-src_a-{_hex6('src/a')}@{sha12}"
    fake_state(first, kind="tree", path="src_a")
    fake_state(hashed, kind="tree", path="src-a-other")
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--path", "src/a", "--out", root)
        == 2
    )
    err = capsys.readouterr().err
    assert str(first) in err and str(hashed) in err


def test_this_targets_folder_found_at_a_later_candidate(repo, root):
    """The walk would take `widgets/tree@…` (it does not exist); the run's own state is
    in `widgets@acme/tree@…`, where an earlier run went, so it resumes there."""
    sha = git(repo, "rev-parse", "HEAD")
    ours = root / "widgets@acme" / f"tree@{sha[:12]}"
    fake_state(
        ours,
        kind="tree",
        repo=str(repo),
        head_sha=sha,
        path="",
        remote_url="https://github.com/acme/widgets.git",
    )
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert read_state(ours)["steps_done"] == ["resolve", "scope", "explore"]
    assert not (root / "widgets").exists()


def test_another_targets_held_folder_is_skipped(repo, root):
    """Another remote's folder, its lock held by another process: the run moves on to
    `widgets@acme/` instead of exiting 5."""
    from test_resume_matrix import _hold, _release

    sha = git(repo, "rev-parse", "HEAD")
    theirs = root / "widgets" / f"tree@{sha[:12]}"
    fake_state(theirs, kind="tree", path="", remote_url=OTHER)
    holder = _hold(theirs)
    try:
        assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    finally:
        _release(holder)
    assert read_state(root / "widgets@acme" / f"tree@{sha[:12]}")["steps_done"] == [
        "resolve",
        "scope",
        "explore",
    ]


def test_absolute_path_with_a_subdirectory_repo(repo, root):
    """An absolute `--path` gives the folder its relative form gives."""
    sha = git(repo, "rev-parse", "HEAD")
    assert (
        run(
            "run",
            "--repo",
            repo / "src",
            "--tree",
            "HEAD",
            "--path",
            (repo / "src" / "billing").resolve(),
            "--out",
            root,
        )
        == 0
    )
    assert (
        read_state(root / "widgets" / f"tree-src_billing@{sha[:12]}")["target"]["path"]
        == "src/billing"
    )


def test_unknown_field_inside_the_saved_target_is_refused(repo, root, capsys):
    """A field a newer version saved inside `target` is refused (exit 1), not dropped."""
    sha = git(repo, "rev-parse", "HEAD")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    folder = root / "widgets" / f"tree@{sha[:12]}"
    state = read_state(folder)
    state["target"]["newer_field"] = 1
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    before = (folder / "state.json").read_bytes()
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 1
    assert "--restart" in capsys.readouterr().err
    assert (folder / "state.json").read_bytes() == before


def test_pr_without_gh_is_refused_with_the_install_hint(
    repo, root, monkeypatch, capsys
):
    monkeypatch.setattr(target_mod.shutil, "which", lambda name: None)
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 2
    assert "cli.github.com" in capsys.readouterr().err
    assert not root.exists()


def test_unreadable_state_at_a_later_candidate_stops_the_run(repo, root, capsys):
    """No readable state names this target and `widgets@acme/tree@…`'s cannot be read: it
    may be this target's run, so the run exits 1 there (R23) and starts no folder."""
    sha = git(repo, "rev-parse", "HEAD")
    ours = root / "widgets@acme" / f"tree@{sha[:12]}"
    ours.mkdir(parents=True)
    (ours / "state.json").write_text("{not json", encoding="utf-8")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 1
    assert str(ours / "state.json") in capsys.readouterr().err
    assert not (root / "widgets").exists()


# --- --pr -----------------------------------------------------------------------


def _pr_repo(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    fork = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit_file(repo, "src/billing/refund.py", "def refund():\n    return 0\n")
    git(repo, "checkout", "-q", "main")
    base_tip = commit_file(repo, "README.md", "moved on\n")
    return repo, fork, head, base_tip


def test_pr_base_sha_is_the_merge_base(tmp_path, root, pr_view):
    repo, fork, head, base_tip = _pr_repo(tmp_path)
    pr_view.info[7] = {"headRefOid": head, "baseRefOid": base_tip}
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 0
    t = read_state(root / "widgets" / f"pr-7@{head[:12]}")["target"]
    assert t["base_sha"] == git(repo, "merge-base", base_tip, head) == fork
    assert t["head_sha"] == head and t["pr"] == 7 and t["kind"] == "pr"


def test_pr_resume_keeps_the_saved_base(tmp_path, root, pr_view):
    repo, fork, head, base_tip = _pr_repo(tmp_path)
    pr_view.info[7] = {"headRefOid": head, "baseRefOid": base_tip}
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 0
    git(
        repo, "merge", "-q", "--no-ff", "-m", "merge", "feature"
    )  # the PR merged: base tip contains head
    pr_view.info[7] = {"headRefOid": head, "baseRefOid": git(repo, "rev-parse", "HEAD")}
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 0
    assert (
        read_state(root / "widgets" / f"pr-7@{head[:12]}")["target"]["base_sha"] == fork
    )


def test_pr_head_not_in_clone_is_refused_with_the_fetch_command(
    repo, root, pr_view, capsys
):
    pr_view.info[9] = {
        "headRefOid": "a" * 40,
        "baseRefOid": git(repo, "rev-parse", "HEAD"),
    }
    assert run("run", "--repo", repo, "--pr", "9", "--out", root) == 2
    err = capsys.readouterr().err
    assert "git -C" in err and "fetch origin" in err and "--fetch" in err
    assert not root.exists()


def test_pr_base_not_in_clone_is_refused(repo, root, pr_view, capsys):
    pr_view.info[9] = {
        "headRefOid": git(repo, "rev-parse", "HEAD"),
        "baseRefOid": "b" * 40,
    }
    assert run("run", "--repo", repo, "--pr", "9", "--out", root) == 2
    assert "fetch origin" in capsys.readouterr().err


def test_pr_fetch_fetches_origin_and_the_pr_head(tmp_path, root, pr_view):
    upstream = make_repo(tmp_path / "up")
    git(upstream, "checkout", "-q", "-b", "feature")
    head = commit_file(upstream, "src/x.py", "X = 1\n")
    git(upstream, "update-ref", "refs/pull/4/head", head)
    git(upstream, "checkout", "-q", "main")
    clone = tmp_path / "widgets"
    git(
        tmp_path,
        "clone",
        "-q",
        "--no-local",
        "--single-branch",
        "--branch",
        "main",
        str(upstream),
        str(clone),
    )
    pr_view.info[4] = {
        "headRefOid": head,
        "baseRefOid": git(clone, "rev-parse", "HEAD"),
    }
    assert run("run", "--repo", clone, "--pr", "4", "--out", root) == 2
    assert run("run", "--repo", clone, "--pr", "4", "--fetch", "--out", root) == 0


def _fake_gh(tmp_path: Path, monkeypatch, stdout: str, code: int = 0) -> Path:
    """Put a `gh` in place that saves its argv to the returned file and prints *stdout*."""
    argv_file = tmp_path / "gh-argv.json"
    gh = tmp_path / "bin" / "gh"
    write(
        gh,
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.write({stdout!r})\nsys.exit({code})\n",
    )
    gh.chmod(0o755)
    which = target_mod.shutil.which
    monkeypatch.setattr(
        target_mod.shutil,
        "which",
        lambda name: str(gh) if name == "gh" else which(name),
    )
    return argv_file


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/acme/widgets.git",
        "git@github.com:acme/widgets.git",
        "ssh://git@github.com/acme/widgets",
    ],
)
def test_pr_asks_gh_about_origins_repository(tmp_path, root, monkeypatch, origin):
    """In a fork clone gh would pick `upstream` over `origin`; the fetch, the hints and the
    folder all name origin, so gh is told origin's repository."""
    repo, fork, head, base_tip = _pr_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", origin)
    git(repo, "remote", "add", "upstream", OTHER)
    argv_file = _fake_gh(
        tmp_path,
        monkeypatch,
        json.dumps(
            {
                "headRefOid": head,
                "baseRefOid": base_tip,
                "url": "https://github.com/acme/widgets/pull/7",
            }
        ),
    )
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[:3] == ["pr", "view", "7"]
    assert argv[argv.index("--repo") + 1] == "acme/widgets"
    assert (
        read_state(root / "widgets" / f"pr-7@{head[:12]}")["target"]["base_sha"] == fork
    )


def test_pr_of_another_repository_than_origin_is_refused(
    tmp_path, root, monkeypatch, capsys
):
    """origin is not on github.com, so gh chooses the repository; a PR of another one than
    origin is refused rather than fetched from and filed under origin."""
    repo, _, head, base_tip = _pr_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", "https://git.example.com/acme/widgets.git")
    argv_file = _fake_gh(
        tmp_path,
        monkeypatch,
        json.dumps(
            {
                "headRefOid": head,
                "baseRefOid": base_tip,
                "url": "https://git.example.com/other/widgets/pull/7",
            }
        ),
    )
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 2
    assert "--repo" not in json.loads(argv_file.read_text())
    err = capsys.readouterr().err
    assert "other/widgets" in err and "acme/widgets" in err
    assert not root.exists()


@pytest.mark.parametrize("stdout,code", [("", 1), ("not json", 0), ("[1]", 0)])
def test_pr_gh_failure_is_refused(repo, root, monkeypatch, capsys, stdout, code):
    _fake_gh(repo.parent, monkeypatch, stdout, code)
    assert run("run", "--repo", repo, "--pr", "7", "--out", root) == 2
    assert "gh pr view 7" in capsys.readouterr().err
    assert not root.exists()


# --- --dir ----------------------------------------------------------------------


def _source(tmp_path) -> Path:
    src = tmp_path / "x" / "y"
    write(src / "pkg" / "mod.py", "def f():\n    return 1\n")
    write(src / "node_modules" / "dep.js", "ignored\n")
    return src


def test_dir_names_and_import(tmp_path, root):
    src = _source(tmp_path)
    assert run("run", "--dir", src, "--out", root) == 0
    folder = root / "y" / f"dir-y-{_hex6(str(src.resolve()))}"
    t = read_state(folder)["target"]
    assert t["kind"] == "dir" and t["source"] == str(src.resolve())
    assert (
        t["repo"] == str(folder / "source")
        and t["remote_url"] is None
        and t["base_sha"] is None
    )
    assert t["head_sha"] == source_head(folder)
    assert (folder / "source" / "pkg" / "mod.py").is_file()
    assert not (folder / "source" / "node_modules").exists()
    assert (folder / "snapshot" / "pkg" / "mod.py").is_file()
    assert sorted(p.name for p in src.iterdir()) == [
        "node_modules",
        "pkg",
    ]  # nothing written there


def test_dir_import_commits_what_a_copied_gitignore_names(tmp_path, root):
    """GitHub's Python `.gitignore` ignores `lib/`: copied code it names is still in the
    import commit, so in the snapshot and in what the gates read."""
    src = tmp_path / "x" / "y"
    write(src / "lib" / "core" / "a.py", "A = 1\n")
    write(src / "app" / "b.py", "B = 2\n")
    write(src / "app" / ".gitignore", "*\n")
    write(src / ".gitignore", "lib/\n")
    assert run("run", "--dir", src, "--out", root) == 0
    folder = root / "y" / f"dir-y-{_hex6(str(src.resolve()))}"
    assert sorted(git(folder / "source", "ls-files").splitlines()) == [
        ".gitignore",
        "app/.gitignore",
        "app/b.py",
        "lib/core/a.py",
    ]
    assert git(folder / "source", "status", "--porcelain", "--ignored") == ""
    assert (folder / "snapshot" / "lib" / "core" / "a.py").read_text() == "A = 1\n"
    assert (folder / "snapshot" / "app" / "b.py").read_text() == "B = 2\n"


def test_dir_import_is_the_same_commit_for_the_same_files_and_clock(tmp_path):
    src = _source(tmp_path)
    assert run("run", "--dir", src, "--out", tmp_path / "a") == 0
    assert run("run", "--dir", src, "--out", tmp_path / "b") == 0
    name = f"dir-y-{_hex6(str(src.resolve()))}"
    assert source_head(tmp_path / "a" / "y" / name) == source_head(
        tmp_path / "b" / "y" / name
    )


def test_dir_keeps_its_first_import(tmp_path, root):
    src = _source(tmp_path)
    assert run("run", "--dir", src, "--out", root) == 0
    folder = root / "y" / f"dir-y-{_hex6(str(src.resolve()))}"
    first = source_head(folder)
    write(src / "pkg" / "mod.py", "def f():\n    return 2\n")
    assert run("run", "--dir", src, "--out", root) == 0
    assert source_head(folder) == first
    assert "return 1" in (folder / "source" / "pkg" / "mod.py").read_text()


def test_dir_reimports_a_source_without_a_commit(tmp_path, root, monkeypatch):
    src = _source(tmp_path)
    real = target_mod._import_git

    def stop_at_commit(dest, *args, **kw):
        if args[0] == "commit":
            raise Killed
        return real(dest, *args, **kw)

    monkeypatch.setattr(target_mod, "_import_git", stop_at_commit)
    with pytest.raises(Killed):
        run("run", "--dir", src, "--out", root)
    monkeypatch.setattr(target_mod, "_import_git", real)
    folder = root / "y" / f"dir-y-{_hex6(str(src.resolve()))}"
    assert not (folder / "state.json").exists()
    assert run("run", "--dir", src, "--out", root) == 0
    assert source_head(folder) == read_state(folder)["target"]["head_sha"]


def test_dir_inside_a_git_work_tree_is_refused(repo, root, capsys):
    assert run("run", "--dir", repo / "src", "--out", root) == 2
    err = capsys.readouterr().err
    assert f"--repo {repo.resolve()} --tree --path src" in err
    assert not root.exists()


def test_dir_output_inside_the_source_is_refused(tmp_path, capsys):
    src = _source(tmp_path)
    assert run("run", "--dir", src, "--out", src / "out") == 2
    assert "inside" in capsys.readouterr().err
    assert not (src / "out").exists()


@pytest.mark.parametrize("out", ["source", "parent"])
def test_dir_run_folder_at_or_under_the_source_is_refused(tmp_path, capsys, out):
    """`--out` the source's parent puts `<root>/y/` at the source itself."""
    src = _source(tmp_path)
    before = everything(tmp_path / "x")
    assert (
        run("run", "--dir", src, "--out", src if out == "source" else src.parent) == 2
    )
    assert "inside" in capsys.readouterr().err
    assert everything(tmp_path / "x") == before


def test_dir_run_folder_beside_the_source_is_used(tmp_path):
    """`--out` the source's parent with another basename is outside the source."""
    src = _source(tmp_path)
    assert run("run", "--dir", src, "--out", tmp_path / "x" / "z") == 0
    assert sorted(p.name for p in src.iterdir()) == ["node_modules", "pkg"]


# --- snapshot, state and --restart ---------------------------------------------------


def _blobs(repo: Path, sha: str) -> dict[str, bytes]:
    """Every regular file at *sha* with its bytes as git stores them."""
    out = {}
    for line in git(repo, "ls-tree", "-r", sha).splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind == "blob" and mode != "120000":
            out[path] = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "blob", oid],
                capture_output=True,
                check=True,
            ).stdout
    return out


def _files(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and not p.is_symlink() and p.name != ".spec-extract-head"
    }


def test_snapshot_ignores_export_attributes(repo, root):
    """export-ignore, export-subst and eol conversion change what `git archive` writes;
    the snapshot holds the bytes git stores, which the gates read."""
    write(
        repo / ".gitattributes",
        "/tests export-ignore\npkg/v.py export-subst\n*.txt text eol=crlf\n",
    )
    write(repo / "tests" / "test_m.py", "def test_m():\n    pass\n")
    write(repo / "pkg" / "v.py", 'V = "$Format:%H$"\n')
    sha = commit_file(repo, "notes.txt", "one\ntwo\n")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    snap = root / "widgets" / f"tree@{sha[:12]}" / "snapshot"
    assert _files(snap) == _blobs(repo, sha)


def test_snapshot_keeps_the_executable_bit(repo, root):
    write(repo / "bin" / "tool.py", "print(1)\n")
    os.chmod(repo / "bin" / "tool.py", 0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tool")
    sha = git(repo, "rev-parse", "HEAD")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    snap = root / "widgets" / f"tree@{sha[:12]}" / "snapshot"
    assert os.access(snap / "bin" / "tool.py", os.X_OK)
    assert not os.access(snap / "src" / "a" / "one.py", os.X_OK)


def test_snapshot_skips_links_out_of_the_tree(repo, root, capsys):
    """An absolute link, or one that leaves the tree, is not extracted; the run goes on and
    names it. A link inside the tree is kept."""
    os.symlink("/etc/hostname", repo / "src" / "abs_link")
    os.symlink("../../../outside", repo / "src" / "a" / "up_link")
    os.symlink("one.py", repo / "src" / "a" / "in_link")
    os.symlink("..", repo / "src" / "d")  # the top: inside
    os.symlink("d/..", repo / "src" / "c")  # through d, above the top
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")
    sha = git(repo, "rev-parse", "HEAD")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    err = capsys.readouterr().err
    assert "src/abs_link" in err and "src/a/up_link" in err and "in_link" not in err
    snap = root / "widgets" / f"tree@{sha[:12]}" / "snapshot"
    assert not (snap / "src" / "abs_link").is_symlink()
    assert not (snap / "src" / "a" / "up_link").is_symlink()
    assert os.readlink(snap / "src" / "a" / "in_link") == "one.py"
    assert os.readlink(snap / "src" / "d") == ".." and "src/c -> d/.." in err
    assert not (snap / "src" / "c").is_symlink()
    assert (snap / ".spec-extract-head").read_text().strip() == sha


def test_dir_with_a_link_out_of_the_tree_resolves(tmp_path, root):
    src = _source(tmp_path)
    os.symlink("/etc/hostname", src / "pkg" / "cfg")
    assert run("run", "--dir", src, "--out", root) == 0
    folder = root / "y" / f"dir-y-{_hex6(str(src.resolve()))}"
    assert (folder / "snapshot" / "pkg" / "mod.py").is_file()
    assert not (folder / "snapshot" / "pkg" / "cfg").is_symlink()


def test_snapshot_is_reused_when_its_marker_matches(repo, root):
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    snap = root / "widgets" / f"tree@{head12(repo)}" / "snapshot"
    (snap / "sentinel").write_text("kept")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert (snap / "sentinel").is_file()


def test_snapshot_without_marker_is_extracted_again(repo, root):
    """R2: a snapshot stopped part-way (no marker) is deleted and extracted again."""
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    snap = root / "widgets" / f"tree@{head12(repo)}" / "snapshot"
    (snap / ".spec-extract-head").unlink()
    (snap / "sentinel").write_text("partial")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert not (snap / "sentinel").exists()
    assert (snap / ".spec-extract-head").is_file()


def test_a_leftover_state_temp_file_is_never_touched(repo, root):
    """R25: the run reads `state.json` only and never touches a `.state-*.tmp`."""
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    folder = root / "widgets" / f"tree@{head12(repo)}"
    tmp = folder / ".state-abc123.tmp"
    tmp.write_text("half a state")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    assert tmp.read_text() == "half a state"


def test_restart_renames_and_never_deletes(repo, root):
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    folder = root / "widgets" / f"tree@{head12(repo)}"
    (folder / "mine.txt").write_text("1")
    assert run("run", "--repo", repo, "--tree", "HEAD", "--restart", "--out", root) == 0
    stamp = FIXED.strftime("%Y%m%d%H%M%S")
    old = folder.parent / f"{folder.name}.old-{stamp}"
    assert (old / "mine.txt").read_text() == "1"
    assert read_state(folder)["steps_done"] == ["resolve", "scope", "explore"]
    assert not (folder / "mine.txt").exists()
    assert run("run", "--repo", repo, "--tree", "HEAD", "--restart", "--out", root) == 0
    assert (folder.parent / f"{folder.name}.old-{stamp}-2" / "state.json").is_file()
    assert (old / "mine.txt").read_text() == "1"
    assert (root / "widgets" / ".locks" / f"{folder.name}.lock").is_file()


def test_restart_with_no_folder_just_runs(repo, root):
    assert run("run", "--repo", repo, "--tree", "HEAD", "--restart", "--out", root) == 0
    assert [p.name for p in (root / "widgets").iterdir() if p.name != ".locks"] == [
        f"tree@{head12(repo)}"
    ]


def test_restart_reimports_a_dir(tmp_path, root):
    src = _source(tmp_path)
    assert run("run", "--dir", src, "--out", root) == 0
    write(src / "pkg" / "mod.py", "def f():\n    return 2\n")
    assert run("run", "--dir", src, "--restart", "--out", root) == 0
    folder = root / "y" / f"dir-y-{_hex6(str(src.resolve()))}"
    assert "return 2" in (folder / "source" / "pkg" / "mod.py").read_text()


def test_default_out_is_under_the_data_dir(monkeypatch, tmp_path):
    from spec_extract_pipeline import __main__ as cli

    class Paths:
        data_dir = tmp_path / "data"

    monkeypatch.setattr("multiplai_core.paths.get_paths", lambda: Paths)
    assert cli.default_out() == tmp_path / "data" / "spec-extract"


def test_unknown_flag_is_refused(repo, root):
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root, "--bogus") == 2
    assert not os.path.exists(root)
