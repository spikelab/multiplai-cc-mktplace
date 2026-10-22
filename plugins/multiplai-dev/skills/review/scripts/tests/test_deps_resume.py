"""One case per row S1-S10 of the stop-and-resume table in
INBOX/plan-review-dependency-source-2026-10-16.md.

Each case stops `ensure` / `materialize` at that row's point (or makes the
row's change on disk), runs `ensure` again, and compares `.review-deps/` and
the manifest with a run that never stopped. S6's edited file, S8 and S10 end
as their rows say instead.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from deps_fixtures import (
    commit_files,
    dependency,
    git,
    git_lock_entry,
    push,
    reviewed_repo,
)
from review_pipeline import deps, gates
from review_pipeline import target as target_mod
from review_pipeline.config import ReviewConfig
from review_pipeline.models import DEPS_DIR, Citation

pytestmark = pytest.mark.usefixtures("allow_file_urls")

CONFIG = ReviewConfig()


class Killed(BaseException):
    """Stands in for the process being killed at that point."""


def _two_deps(tmp_path):
    url1, sha1, work1 = dependency(tmp_path, "lib1", {"one/a.py": "A = 1\n"})
    url2, sha2, work2 = dependency(tmp_path, "lib2", {"two/b.py": "B = 2\n"})
    target = reviewed_repo(tmp_path, {"uv.lock": git_lock_entry("lib1", url1, sha1) + git_lock_entry("lib2", url2, sha2)})
    return target, {"lib1": (url1, sha1, work1), "lib2": (url2, sha2, work2)}


def _state(snapshot: Path, cache_root: Path) -> tuple[dict, dict]:
    """What a run left: every path under `.review-deps/` with its bytes or link, and the manifest."""
    root = snapshot / DEPS_DIR
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == deps.MANIFEST_FILE:
            continue
        files[rel] = f"link:{os.readlink(p)}" if p.is_symlink() else (p.read_bytes() if p.is_file() else "dir")
    manifest = json.loads((root / deps.MANIFEST_FILE).read_text())
    for e in manifest["deps"]:
        e["cache"] = Path(e["cache"]).relative_to(cache_root).as_posix() if e["cache"] else ""
    return files, manifest


def _reference(tmp_path, monkeypatch, target, name="ref"):
    """`.review-deps/` and the manifest of a run on its own snapshot and cache that never stopped."""
    cache = tmp_path / f"{name}-cache"
    monkeypatch.setattr(deps, "cache_root", lambda: cache)
    deps.ensure(target, tmp_path / f"{name}-snap", CONFIG)
    monkeypatch.setattr(deps, "cache_root", lambda: tmp_path / "dep-cache")
    return _state(tmp_path / f"{name}-snap", cache)


def _resumed(tmp_path, target, snapshot):
    deps.ensure(target, snapshot, CONFIG)
    return _state(snapshot, tmp_path / "dep-cache")


def _verbs(monkeypatch) -> list[str]:
    """Record the git subcommand of every call deps makes from now on."""
    verbs: list[str] = []
    real = deps._git

    def record(*args, **kwargs):
        words = list(args)
        while words[:1] in (["-C"], ["-c"]):
            words = words[2:]
        verbs.append(words[0] if words else "")
        return real(*args, **kwargs)

    monkeypatch.setattr(deps, "_git", record)
    return verbs


def _partial_clone(entry: Path) -> None:
    """What a `git clone --bare` killed early leaves: a repository with no origin and no objects."""
    entry.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(entry)], check=True, capture_output=True)


def _kill_clone(monkeypatch):
    def killed(url, entry):
        _partial_clone(entry)
        raise Killed
    monkeypatch.setattr(deps, "_clone", killed)


def test_S1_killed_during_clone_resumes_with_one_fresh_clone(tmp_path, monkeypatch):
    target, _ = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    real_clone = deps._clone
    _kill_clone(monkeypatch)
    with pytest.raises(Killed):
        deps.ensure(target, snapshot, CONFIG)
    monkeypatch.setattr(deps, "_clone", real_clone)
    verbs = _verbs(monkeypatch)
    assert _resumed(tmp_path, target, snapshot) == expected
    assert verbs.count("clone") == 2  # lib1: the partial entry deleted and cloned again; lib2: never cloned


def test_S1_a_second_failure_is_a_need(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    url1 = repos["lib1"][0]
    _partial_clone(deps.cache_path(url1))
    subprocess.run(["rm", "-rf", url1.removeprefix("file://")], check=True)  # the clone again fails too
    manifest = deps.ensure(target, tmp_path / "snap", CONFIG)
    assert [(e.name, e.status) for e in manifest.deps] == [("lib1", "failed"), ("lib2", "ok")]
    assert [n.what.split(",")[0] for n in deps.needs(manifest)] == [f"The source of lib1@{repos['lib1'][1][:12]}"]


def test_S2_killed_during_fetch_fetches_again(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    deps.ensure(target, tmp_path / "old-snap", CONFIG)  # the cache holds lib1 v1
    url1, _, work1 = repos["lib1"]
    v2 = commit_files(work1, {"one/a.py": "A = 2\n"}, "v2")
    push(work1, url1)
    url2, sha2, _ = repos["lib2"]
    repo = Path(target.repo_path)
    head = commit_files(repo, {"uv.lock": git_lock_entry("lib1", url1, v2) + git_lock_entry("lib2", url2, sha2)})
    target = target.model_copy(update={"head_sha": head})
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    real = deps._fetched

    def killed(entry, commit):
        raise Killed
    monkeypatch.setattr(deps, "_fetched", killed)
    with pytest.raises(Killed):
        deps.ensure(target, snapshot, CONFIG)
    monkeypatch.setattr(deps, "_fetched", real)
    verbs = _verbs(monkeypatch)
    assert _resumed(tmp_path, target, snapshot) == expected
    assert verbs.count("fetch") == 1 and "clone" not in verbs


def _leave_partial_extract(snapshot: Path, folder: str) -> None:
    """What a run killed while extracting leaves: no manifest, the folder still under its temporary name."""
    root = snapshot / DEPS_DIR
    (root / deps.MANIFEST_FILE).unlink()
    (root / folder).rename(root / f".{folder}.partial-x1y2")
    (root / f".{folder}.partial-x1y2.tar").write_bytes(b"half a tar")


def test_S3_killed_during_extract_redoes_every_dependency(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    deps.ensure(target, snapshot, CONFIG)
    _leave_partial_extract(snapshot, f"lib2@{repos['lib2'][1][:12]}")
    verbs = _verbs(monkeypatch)
    assert _resumed(tmp_path, target, snapshot) == expected  # temporary folders gone too
    assert "clone" not in verbs and "fetch" not in verbs  # from the cache


def test_S4_killed_between_dependencies_redoes_all(tmp_path, monkeypatch):
    target, _ = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    real = deps._materialize_one
    done = []

    def one_then_kill(*args):
        if done:
            raise Killed
        done.append(real(*args))
        return done[0]
    monkeypatch.setattr(deps, "_materialize_one", one_then_kill)
    with pytest.raises(Killed):
        deps.ensure(target, snapshot, CONFIG)
    assert not (snapshot / DEPS_DIR / deps.MANIFEST_FILE).exists()
    monkeypatch.setattr(deps, "_materialize_one", real)
    assert _resumed(tmp_path, target, snapshot) == expected


def test_S5_a_written_manifest_is_reused_unchanged(tmp_path, monkeypatch):
    target, _ = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    deps.ensure(target, snapshot, CONFIG)
    before = (snapshot / DEPS_DIR / deps.MANIFEST_FILE).stat().st_mtime_ns
    verbs = _verbs(monkeypatch)
    assert _resumed(tmp_path, target, snapshot) == expected
    assert verbs == []
    assert (snapshot / DEPS_DIR / deps.MANIFEST_FILE).stat().st_mtime_ns == before


def test_S6_a_deleted_folder_redoes_all(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    deps.ensure(target, snapshot, CONFIG)
    subprocess.run(["rm", "-rf", str(snapshot / DEPS_DIR / f"lib1@{repos['lib1'][1][:12]}")], check=True)
    assert _resumed(tmp_path, target, snapshot) == expected


def test_S6_an_edited_file_is_kept_but_cannot_make_a_false_quote_pass(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    snapshot = tmp_path / "snap"
    manifest = deps.ensure(target, snapshot, CONFIG)
    folder = f"lib1@{repos['lib1'][1][:12]}"
    (snapshot / DEPS_DIR / folder / "one/a.py").write_text("A = 99\n")
    assert deps.ensure(target, snapshot, CONFIG) == manifest  # not detected: reused
    assert (snapshot / DEPS_DIR / folder / "one/a.py").read_text() == "A = 99\n"
    t = target.model_copy(update={"deps": manifest.ok()})
    edited = Citation(path=f"{DEPS_DIR}/{folder}/one/a.py", line_start=1, line_end=1, quote="A = 99")
    pinned = edited.model_copy(update={"quote": "A = 1"})
    assert not gates.citation_gate(t, edited).passed
    assert gates.citation_gate(t, pinned).passed


def test_S7_a_new_head_re_extracts_the_snapshot_and_redoes(tmp_path, monkeypatch):
    target, _ = _two_deps(tmp_path)
    snapshot = tmp_path / "snap"
    target_mod.snapshot_head(target, snapshot)
    deps.ensure(target, snapshot, CONFIG)
    head = commit_files(Path(target.repo_path), {"README.md": "y\n"}, "next round")
    target = target.model_copy(update={"head_sha": head})
    expected = _reference(tmp_path, monkeypatch, target)
    target_mod.snapshot_head(target, snapshot)
    assert not (snapshot / DEPS_DIR).exists()
    assert _resumed(tmp_path, target, snapshot) == expected
    assert json.loads((snapshot / DEPS_DIR / deps.MANIFEST_FILE).read_text())["head_sha"] == head


def _force_push_away(work: Path, url: str) -> None:
    """Replace the remote's history so the pinned commit is no longer on it."""
    bare = url.removeprefix("file://")
    git(work, "checkout", "-q", "--orphan", "rewritten")
    commit_files(work, {"other.py": "z\n"}, "rewritten")
    git(work, "push", "-q", "--force", bare, "rewritten:main")
    git(Path(bare), "reflog", "expire", "--expire=now", "--all")
    git(Path(bare), "gc", "-q", "--prune=now")


def test_S8_a_force_pushed_remote_is_fine_while_the_cache_holds_the_commit(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    deps.ensure(target, tmp_path / "first-snap", CONFIG)
    url1, sha1, work1 = repos["lib1"]
    _force_push_away(work1, url1)
    assert _resumed(tmp_path, target, tmp_path / "snap") == expected


def test_S8_a_force_pushed_remote_without_the_commit_in_the_cache_is_a_need(tmp_path):
    target, repos = _two_deps(tmp_path)
    url1, sha1, work1 = repos["lib1"]
    _force_push_away(work1, url1)
    manifest = deps.ensure(target, tmp_path / "snap", CONFIG)
    assert [(e.name, e.status) for e in manifest.deps] == [("lib1", "failed"), ("lib2", "ok")]
    (need,) = deps.needs(manifest)
    assert f"lib1@{sha1[:12]}" in need.what


def test_S9_clone_killed_twice_clones_once_in_the_run_that_finishes(tmp_path, monkeypatch):
    target, _ = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    real_clone = deps._clone
    _kill_clone(monkeypatch)
    for _ in range(2):  # the first run, then a resume that is killed during its clone again
        with pytest.raises(Killed):
            deps.ensure(target, snapshot, CONFIG)
    monkeypatch.setattr(deps, "_clone", real_clone)
    verbs = _verbs(monkeypatch)
    assert _resumed(tmp_path, target, snapshot) == expected
    assert verbs.count("clone") == 2  # one per dependency: lib1's partial entry re-cloned once


def test_S9_extract_killed_twice_redoes_every_dependency(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    expected = _reference(tmp_path, monkeypatch, target)
    snapshot = tmp_path / "snap"
    deps.ensure(target, snapshot, CONFIG)
    _leave_partial_extract(snapshot, f"lib2@{repos['lib2'][1][:12]}")
    deps.ensure(target, snapshot, CONFIG)
    _leave_partial_extract(snapshot, f"lib1@{repos['lib1'][1][:12]}")
    assert _resumed(tmp_path, target, snapshot) == expected


def test_S10_a_held_cache_lock_is_waited_for_then_the_dependency_is_skipped(tmp_path, monkeypatch):
    target, repos = _two_deps(tmp_path)
    monkeypatch.setattr(deps, "LOCK_WAIT_S", 0.3)
    entry = deps.cache_path(repos["lib1"][0])
    entry.parent.mkdir(parents=True, exist_ok=True)
    lock = entry.with_name(entry.name + ".lock")
    held = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)  # the stopped process, still alive
    try:
        started = time.monotonic()
        manifest = deps.ensure(target, tmp_path / "snap", CONFIG)
        assert time.monotonic() - started >= 0.3
        assert [(e.name, e.status) for e in manifest.deps] == [("lib1", "skipped"), ("lib2", "ok")]
        (need,) = deps.needs(manifest)
        assert f"lib1@{repos['lib1'][1][:12]}" in need.what and need.cause == "lookup-failed"
        other = os.open(lock, os.O_RDWR)
        with pytest.raises(BlockingIOError):  # never broken: the holder still has it
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(other)
    finally:
        os.close(held)
