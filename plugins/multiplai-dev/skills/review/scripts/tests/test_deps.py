from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from deps_fixtures import commit_files, dependency, git_lock_entry, init, lock_entry, push, reviewed_repo

from review_pipeline import deps
from review_pipeline.deps import Dep
from review_pipeline.gates import gated_need
from review_pipeline.models import DEPS_DIR

SHA_A = "a" * 40
SHA_B = "b" * 40


# --- locked_git_deps ------------------------------------------------------------


def test_only_git_sources_are_dependencies(tmp_path):
    lock = (lock_entry("requests", '{ registry = "https://pypi.org/simple" }')
            + lock_entry("local-lib", '{ path = "libs/local" }')
            + lock_entry("editable-lib", '{ editable = "libs/editable" }')
            + git_lock_entry("multiplai-core", "https://github.com/spikelab/multiplai-core", SHA_A)
            + lock_entry("other", f'{{ git = "ssh://git@example.com/org/other.git#{SHA_B}" }}'))
    target = reviewed_repo(tmp_path, {"uv.lock": lock})
    assert deps.locked_git_deps(target) == [
        Dep("multiplai-core", "https://github.com/spikelab/multiplai-core", SHA_A),
        Dep("other", "ssh://git@example.com/org/other.git", SHA_B),
    ]


def test_the_same_pin_in_two_lock_files_is_one_dependency(tmp_path):
    entry = git_lock_entry("multiplai-core", "https://github.com/spikelab/multiplai-core", SHA_A)
    target = reviewed_repo(tmp_path, {"uv.lock": entry, "sub/project/uv.lock": entry})
    assert deps.locked_git_deps(target) == [
        Dep("multiplai-core", "https://github.com/spikelab/multiplai-core", SHA_A)]


def test_a_commit_that_is_not_40_hex_is_skipped(tmp_path, caplog):
    lock = (lock_entry("short", '{ git = "https://example.com/a/short?rev=abc#abc123" }')
            + lock_entry("nohash", '{ git = "https://example.com/a/nohash?branch=main" }'))
    target = reviewed_repo(tmp_path, {"uv.lock": lock})
    with caplog.at_level("INFO"):
        assert deps.locked_git_deps(target) == []
    assert "short" in caplog.text


def test_no_lock_file_means_no_dependencies(tmp_path):
    target = reviewed_repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    assert deps.locked_git_deps(target) == []


def test_a_lock_file_that_is_not_toml_is_skipped(tmp_path):
    target = reviewed_repo(tmp_path, {"uv.lock": "this is [ not toml"})
    assert deps.locked_git_deps(target) == []


# --- materialize ----------------------------------------------------------------


def _git_calls(monkeypatch) -> list[list[str]]:
    """Record every argv deps runs, still running it."""
    calls: list[list[str]] = []
    real = deps.subprocess.run

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(deps.subprocess, "run", run)
    return calls


def _verbs(calls: list[list[str]]) -> list[str]:
    """The git subcommand of each recorded call (the word after the -c/-C options)."""
    out = []
    for argv in calls:
        words, i = argv[1:], 0
        while i < len(words) and words[i] in ("-c", "-C"):
            i += 2
        out.append(words[i] if i < len(words) else "")
    return out


def test_extracts_the_pinned_commit_not_a_later_one(tmp_path, allow_file_urls):
    url, v1, work = dependency(tmp_path, "lib", {"lib/core.py": "VERSION = 1\n"})
    commit_files(work, {"lib/core.py": "VERSION = 2\n"}, "v2")
    push(work, url)
    snapshot = tmp_path / "snap"
    manifest = deps.materialize([Dep("lib", url, v1)], snapshot, head_sha="h" * 40)
    folder = snapshot / DEPS_DIR / f"lib@{v1[:12]}"
    assert (folder / "lib/core.py").read_text() == "VERSION = 1\n"
    assert [(e.name, e.status) for e in manifest.deps] == [("lib", "ok")]
    saved = json.loads((snapshot / DEPS_DIR / deps.MANIFEST_FILE).read_text())
    assert saved["head_sha"] == "h" * 40
    assert saved["deps"][0] | {"cache": ""} == {"name": "lib", "url": url, "commit": v1, "cache": "",
                                                 "status": "ok", "reason": ""}
    assert Path(saved["deps"][0]["cache"]).is_dir()
    assert not deps.needs(manifest)


def test_an_unreachable_url_is_a_need_and_the_others_still_extract(tmp_path, allow_file_urls):
    url, sha, _ = dependency(tmp_path, "good", {"g.py": "x = 1\n"})
    missing = f"file://{tmp_path}/nowhere.git"
    snapshot = tmp_path / "snap"
    manifest = deps.materialize([Dep("gone", missing, "c" * 40), Dep("good", url, sha)], snapshot,
                                head_sha="h" * 40)
    assert [(e.name, e.status) for e in manifest.deps] == [("gone", "failed"), ("good", "ok")]
    (need,) = deps.needs(manifest)
    assert need.blocks == "review" and need.source == "pipeline" and need.cause == "unreachable"
    assert f"gone@{'c' * 12}" in need.what
    assert need.command == f"git ls-remote {missing}"
    assert gated_need(need).command == need.command  # need_gate keeps it: a read-only form
    assert (snapshot / DEPS_DIR / f"good@{sha[:12]}" / "g.py").is_file()


@pytest.mark.parametrize("url", ["ext::sh -c touch% /tmp/pwned", "http://example.com/a/b.git",
                                 "-uhttps://example.com/a.git", "fd::3"])
def test_a_url_outside_the_allowed_schemes_runs_no_git(tmp_path, monkeypatch, url):
    calls = _git_calls(monkeypatch)
    manifest = deps.materialize([Dep("evil", url, "d" * 40)], tmp_path / "snap", head_sha="h" * 40)
    assert calls == []
    assert manifest.deps[0].status == "failed" and "not allowed" in manifest.deps[0].reason
    (need,) = deps.needs(manifest)
    assert need.command == "" and need.where == ""


def test_file_urls_are_refused_unless_a_test_allows_them(tmp_path, monkeypatch):
    url, sha, _ = dependency(tmp_path, "lib", {"a.py": "a\n"})
    calls = _git_calls(monkeypatch)
    manifest = deps.materialize([Dep("lib", url, sha)], tmp_path / "snap", head_sha="h" * 40)
    assert calls == [] and manifest.deps[0].status == "failed"


def test_git_runs_with_only_the_allowed_protocols(tmp_path, monkeypatch, allow_file_urls):
    url, sha, _ = dependency(tmp_path, "lib", {"a.py": "a\n"})
    calls = _git_calls(monkeypatch)
    deps.materialize([Dep("lib", url, sha)], tmp_path / "snap", head_sha="h" * 40)
    assert calls
    for argv in calls:
        joined = " ".join(argv)
        assert "-c protocol.allow=never" in joined
        assert "-c protocol.https.allow=always" in joined and "-c protocol.ssh.allow=always" in joined


def test_a_symlink_pointing_outside_is_not_extracted(tmp_path, allow_file_urls):
    work = init(tmp_path / "lib-work")
    (work / "escape").symlink_to("../../../../etc/passwd")
    (work / "inside").symlink_to("ok.py")
    sha = commit_files(work, {"ok.py": "ok\n"})
    bare = tmp_path / "lib.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    snapshot = tmp_path / "snap"
    manifest = deps.materialize([Dep("lib", f"file://{bare}", sha)], snapshot, head_sha="h" * 40)
    folder = snapshot / DEPS_DIR / f"lib@{sha[:12]}"
    assert manifest.deps[0].status == "ok"
    assert (folder / "ok.py").is_file() and (folder / "inside").is_symlink()
    assert not (folder / "escape").exists() and not (folder / "escape").is_symlink()


def test_a_stale_cache_missing_the_commit_fetches_once(tmp_path, monkeypatch, allow_file_urls):
    url, v1, work = dependency(tmp_path, "lib", {"a.py": "v1\n"})
    deps.materialize([Dep("lib", url, v1)], tmp_path / "snap1", head_sha="h" * 40)
    v2 = commit_files(work, {"a.py": "v2\n"}, "v2")
    push(work, url)
    calls = _git_calls(monkeypatch)
    manifest = deps.materialize([Dep("lib", url, v2)], tmp_path / "snap2", head_sha="h" * 40)
    assert manifest.deps[0].status == "ok"
    assert _verbs(calls).count("fetch") == 1 and "clone" not in _verbs(calls)
    assert (tmp_path / "snap2" / DEPS_DIR / f"lib@{v2[:12]}" / "a.py").read_text() == "v2\n"


def test_a_cache_holding_the_commit_fetches_nothing(tmp_path, monkeypatch, allow_file_urls):
    url, sha, _ = dependency(tmp_path, "lib", {"a.py": "v1\n"})
    deps.materialize([Dep("lib", url, sha)], tmp_path / "snap1", head_sha="h" * 40)
    calls = _git_calls(monkeypatch)
    deps.materialize([Dep("lib", url, sha)], tmp_path / "snap2", head_sha="h" * 40)
    assert "fetch" not in _verbs(calls) and "clone" not in _verbs(calls)


def test_an_archive_over_the_size_limit_is_skipped(tmp_path, allow_file_urls):
    url, sha, _ = dependency(tmp_path, "big", {"blob.txt": "x" * (2 * 1024 * 1024)})
    snapshot = tmp_path / "snap"
    manifest = deps.materialize([Dep("big", url, sha)], snapshot, head_sha="h" * 40, max_mb=1)
    assert manifest.deps[0].status == "skipped" and "MB" in manifest.deps[0].reason
    assert not (snapshot / DEPS_DIR / f"big@{sha[:12]}").exists()
    (need,) = deps.needs(manifest)
    assert need.cause == "lookup-failed"


def test_cache_paths_stay_under_the_cache_root(tmp_path):
    root = deps.cache_root()
    for url in ("https://github.com/spikelab/multiplai-core", "https://user:tok@github.com/a/b.git",
                "git@github.com:a/../../b.git", "ssh://git@example.com:22/org/x", "file:///tmp/../x.git"):
        path = deps.cache_path(url)
        assert path.resolve().is_relative_to(root.resolve()), url
        assert path.name.endswith(".git") and "tok" not in str(path)
    assert deps.cache_path("https://github.com/spikelab/multiplai-core") == \
        root / "github.com" / "spikelab" / "multiplai-core.git"
