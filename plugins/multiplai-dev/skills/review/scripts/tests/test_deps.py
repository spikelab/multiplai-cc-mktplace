from __future__ import annotations

from deps_fixtures import git_lock_entry, lock_entry, reviewed_repo

from review_pipeline import deps
from review_pipeline.deps import Dep

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
