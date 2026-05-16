"""Tests for /dream memory-dir git auto-commit.

Covers the memory-dir git awareness added post-Block 9:
- When memory_dir is inside a git repo, dream auto-commits changed files
  after consolidation and catalog regeneration complete.
- When memory_dir is not a git repo, dream logs a warning and continues.
- When there are no changes to commit, no empty commit is created.
"""

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import SCRIPTS_DIR, PLUGIN_ROOT


def _init_git(dir_path: Path) -> None:
    """Initialize a git repo at dir_path with a throwaway identity.

    Must run before creating any tracked state so commits work without
    reading the user's real git config.
    """
    subprocess.run(["git", "-C", str(dir_path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(dir_path), "config", "user.email", "test@multiplai.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dir_path), "config", "user.name", "Multiplai Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dir_path), "config", "commit.gpgsign", "false"],
        check=True,
    )


def _git_log_count(dir_path: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(dir_path), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or 0)


def _load_dream_module(alias: str):
    """Load dream.py as an isolated module (fresh paths cache) per test."""
    from lib.paths import _reset_cache
    _reset_cache()
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS_DIR / "dream.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dream_env(tmp_path, monkeypatch):
    """Set up a full dream environment rooted at tmp_path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    (data_dir / "catalogs").mkdir()

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_dir", str(diary_dir))

    # Ensure pending learnings exist so dream has work to do.
    # paths.learnings_file() resolves to learnings_dir / "{today}.md".
    from lib.paths import _reset_cache, get_paths
    _reset_cache()
    paths = get_paths()
    learnings_today = paths.learnings_file()
    learnings_today.parent.mkdir(parents=True, exist_ok=True)
    learnings_today.write_text("- learned a thing\n- learned another\n")
    (memory_dir / "technical-pref.md").write_text("# Technical Preferences\n")

    return {
        "data_dir": data_dir,
        "memory_dir": memory_dir,
        "diary_dir": diary_dir,
    }


def _mocked_client_context():
    """Patch create_client to return an AsyncMock producing deterministic updates."""
    client = AsyncMock()
    # _update_memory_file reads .content off the response object.
    response = MagicMock()
    response.content = "# Technical Preferences\n\nAuto-updated by dream.\n"
    client.query = AsyncMock(return_value=response)
    return client


class TestDreamAutoCommit:
    """Dream auto-commits memory changes when memory_dir is a git repo."""

    def test_commits_when_memory_dir_is_git_repo(self, dream_env):
        """A fresh commit lands when dream mutates files in a tracked memory_dir."""
        memory_dir: Path = dream_env["memory_dir"]
        _init_git(memory_dir)
        subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit", "-q", "-m", "initial"],
            check=True,
        )
        initial_commits = _git_log_count(memory_dir)
        assert initial_commits == 1

        with patch("generators.dispatcher.generate_catalogs", new=AsyncMock(return_value=[])), \
             patch("lib.model_client.create_client", new=AsyncMock(return_value=_mocked_client_context())):
            mod = _load_dream_module("dream_commit")
            asyncio.run(mod.dream())

        assert _git_log_count(memory_dir) == initial_commits + 1, (
            "dream() must create a new commit after mutating memory files"
        )

        subject = subprocess.run(
            ["git", "-C", str(memory_dir), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert subject.startswith("dream: consolidate "), (
            f"auto-commit subject should start with 'dream: consolidate ', got: {subject}"
        )

    def test_no_commit_when_memory_dir_is_not_git_repo(self, dream_env, caplog):
        """Dream still completes and logs a warning when memory_dir is plain."""
        memory_dir: Path = dream_env["memory_dir"]
        # No _init_git — memory_dir is just a directory.
        assert not (memory_dir / ".git").exists()

        with patch("generators.dispatcher.generate_catalogs", new=AsyncMock(return_value=[])), \
             patch("lib.model_client.create_client", new=AsyncMock(return_value=_mocked_client_context())):
            mod = _load_dream_module("dream_nogit")
            with caplog.at_level("WARNING", logger="dream"):
                asyncio.run(mod.dream())

        # No .git was created, no commit happened — verify explicitly.
        assert not (memory_dir / ".git").exists()
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("Memory auto-commit skipped" in m and "not a git repository" in m
                   for m in messages), (
            f"Expected an auto-commit-skipped warning, got: {messages}"
        )

    def test_no_empty_commit_when_no_changes(self, dream_env):
        """If dream makes no memory-file changes, no commit is created."""
        memory_dir: Path = dream_env["memory_dir"]
        # Wipe pending learnings BEFORE the initial commit so the tracked
        # state matches what dream will observe — otherwise the empty-write
        # itself becomes a tracked change and gets committed.
        from lib.paths import get_paths
        get_paths().learnings_file().write_text("")
        _init_git(memory_dir)
        subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit", "-q", "-m", "initial"],
            check=True,
        )
        initial_commits = _git_log_count(memory_dir)

        with patch("generators.dispatcher.generate_catalogs", new=AsyncMock(return_value=[])), \
             patch("lib.model_client.create_client", new=AsyncMock(return_value=_mocked_client_context())):
            mod = _load_dream_module("dream_noop")
            asyncio.run(mod.dream())

        assert _git_log_count(memory_dir) == initial_commits, (
            "dream() must not create an empty commit when nothing changed"
        )
