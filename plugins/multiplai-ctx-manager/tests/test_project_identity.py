"""Tests for the project-identity resolver (scripts/lib/project_identity.py).

Covers the config-driven resolution order: project_roots → umbrella_roots →
git (default) → basename, plus the empty/None edge cases.
"""

import shutil
import subprocess

import pytest

from lib.project_identity import (
    WORKSPACE_PROJECT,
    resolve_project,
)

HAS_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(not HAS_GIT, reason="git not installed")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("x")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_cwd_returns_none(self):
        assert resolve_project("", config={}) is None

    @pytest.mark.parametrize("val", ["unknown", "UNKNOWN", "none", "null", "  ", "unknown "])
    def test_null_placeholder_cwds_return_none(self, val):
        # Placeholder cwds written when the real dir was unavailable name no
        # project — must never become a bucket.
        assert resolve_project(val, config={"project_roots": ["/work"]}) is None

    def test_basename_detection(self):
        cfg = {"detection": "basename"}
        assert resolve_project("/a/b/myproj", config=cfg) == "myproj"

    def test_roots_only_no_match_returns_none(self):
        cfg = {"detection": "roots", "project_roots": ["/work/PROJECTS"]}
        assert resolve_project("/somewhere/else", config=cfg) is None


# ---------------------------------------------------------------------------
# project_roots
# ---------------------------------------------------------------------------

class TestProjectRoots:
    def test_direct_child_is_project(self):
        cfg = {"project_roots": ["/work/PROJECTS"]}
        assert resolve_project("/work/PROJECTS/foo", config=cfg) == "foo"

    def test_deep_subdir_resolves_to_first_segment(self):
        cfg = {"project_roots": ["/work/PROJECTS"]}
        assert (
            resolve_project("/work/PROJECTS/foo/sub/deep", config=cfg) == "foo"
        )

    def test_root_itself_is_not_a_project(self):
        # cwd == root has no segment beneath it; with no umbrella + detection
        # roots, that means no attribution.
        cfg = {"detection": "roots", "project_roots": ["/work/PROJECTS"]}
        assert resolve_project("/work/PROJECTS", config=cfg) is None

    def test_longest_matching_root_wins(self):
        cfg = {
            "project_roots": [
                "/work/PROJECTS",
                "/work/PROJECTS/DolceBot",
            ]
        }
        # Nested root is more specific → the segment beneath it.
        assert (
            resolve_project("/work/PROJECTS/DolceBot/Engine/src", config=cfg)
            == "Engine"
        )

    def test_tilde_expansion(self):
        cfg = {"project_roots": ["~/PROJECTS"]}
        from pathlib import Path

        home = Path.home()
        assert (
            resolve_project(str(home / "PROJECTS" / "bar" / "x"), config=cfg)
            == "bar"
        )


# ---------------------------------------------------------------------------
# umbrella_roots
# ---------------------------------------------------------------------------

class TestUmbrellaRoots:
    def test_umbrella_root_itself_is_workspace(self):
        cfg = {"umbrella_roots": ["/work"]}
        assert resolve_project("/work", config=cfg) == WORKSPACE_PROJECT

    def test_under_umbrella_without_project_root_is_workspace(self):
        cfg = {"umbrella_roots": ["/work"]}
        assert resolve_project("/work/INBOX", config=cfg) == WORKSPACE_PROJECT

    def test_project_root_beats_umbrella(self):
        cfg = {
            "project_roots": ["/work/PROJECTS"],
            "umbrella_roots": ["/work"],
        }
        assert (
            resolve_project("/work/PROJECTS/foo/sub", config=cfg) == "foo"
        )
        assert resolve_project("/work/INBOX", config=cfg) == WORKSPACE_PROJECT
        assert resolve_project("/work", config=cfg) == WORKSPACE_PROJECT


# ---------------------------------------------------------------------------
# git (default strategy)
# ---------------------------------------------------------------------------

@requires_git
class TestGitDetection:
    def test_repo_root_resolves_to_repo_name(self, tmp_path):
        repo = tmp_path / "myrepo"
        _init_repo(repo)
        assert resolve_project(str(repo), config={}) == "myrepo"

    def test_subdir_resolves_to_repo_name(self, tmp_path):
        repo = tmp_path / "myrepo"
        _init_repo(repo)
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        assert resolve_project(str(sub), config={}) == "myrepo"

    def test_worktree_collapses_to_main_repo(self, tmp_path):
        repo = tmp_path / "mainrepo"
        _init_repo(repo)
        wt = tmp_path / "wt-feature"
        _git(repo, "worktree", "add", "-q", str(wt))
        assert resolve_project(str(wt), config={}) == "mainrepo"

    def test_non_git_dir_falls_back_to_basename(self, tmp_path):
        plain = tmp_path / "plaindir"
        plain.mkdir()
        assert resolve_project(str(plain), config={}) == "plaindir"


# ---------------------------------------------------------------------------
# config loading from disk
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_load_project_map_reads_workspace_file(
        self, monkeypatch, reset_paths_cache, tmp_path
    ):
        ws = tmp_path / "ws"
        (ws / ".multiplai").mkdir(parents=True)
        (ws / ".multiplai" / "project-map.yaml").write_text(
            "project_roots:\n  - /work/PROJECTS\n"
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_workspace_dir", str(ws))

        from lib.project_identity import load_project_map

        cfg = load_project_map()
        assert cfg.get("project_roots") == ["/work/PROJECTS"]

    def test_missing_map_returns_empty(
        self, monkeypatch, reset_paths_cache, tmp_path
    ):
        ws = tmp_path / "ws2"
        ws.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_workspace_dir", str(ws))

        from lib.project_identity import load_project_map

        assert load_project_map() == {}
