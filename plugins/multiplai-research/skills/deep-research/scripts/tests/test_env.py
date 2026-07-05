"""Tests for the .env loader — project root detection + loading."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from research_pipeline.env import find_project_root, load_env


class TestFindProjectRoot:
    def test_finds_claude_code_multiplai_root(self, tmp_path: Path) -> None:
        """A directory with .env.example + dotfiles/ is the project root."""
        root = tmp_path / "my-project"
        (root / "dotfiles").mkdir(parents=True)
        (root / ".env.example").write_text("# example")
        (root / "dotfiles" / "skills" / "x" / "scripts").mkdir(parents=True)
        start = root / "dotfiles" / "skills" / "x" / "scripts"

        found = find_project_root(start=start)
        assert found == root

    def test_fallback_to_any_env_file(self, tmp_path: Path) -> None:
        """If no project marker, fall back to first ancestor with .env."""
        root = tmp_path / "other-project"
        (root / "nested" / "deep").mkdir(parents=True)
        (root / ".env").write_text("X=1")
        start = root / "nested" / "deep"

        found = find_project_root(start=start)
        assert found == root

    def test_returns_none_if_nothing_found(self, tmp_path: Path) -> None:
        deep = tmp_path / "empty" / "nested"
        deep.mkdir(parents=True)
        # No .env or project markers anywhere up the tree within tmp_path
        # (the real filesystem may have one, so we can't assert None strictly —
        # but we can assert the returned path is outside tmp_path or None)
        found = find_project_root(start=deep)
        # Either None or a real ancestor above tmp_path — both are valid
        if found is not None:
            assert tmp_path not in found.parents and found != tmp_path


class TestLoadEnv:
    @pytest.fixture(autouse=True)
    def _isolate_multiplai_home(self):
        """CLAUDE_MULTIPLAI_HOME short-circuits root discovery — keep tests
        hermetic regardless of the environment they run in (e.g. in-container,
        where claude.sh exports it)."""
        saved = os.environ.pop("CLAUDE_MULTIPLAI_HOME", None)
        yield
        if saved is not None:
            os.environ["CLAUDE_MULTIPLAI_HOME"] = saved

    def test_multiplai_home_wins_over_walkup(self, tmp_path: Path) -> None:
        """$CLAUDE_MULTIPLAI_HOME/.env is preferred over the walk-up root."""
        home = tmp_path / "kit"
        home.mkdir()
        (home / ".env").write_text('HOME_WINS_TEST="from-home"\n')
        other = tmp_path / "other"
        other.mkdir()
        (other / ".env").write_text('HOME_WINS_TEST="from-walkup"\n')

        os.environ["CLAUDE_MULTIPLAI_HOME"] = str(home)
        os.environ.pop("HOME_WINS_TEST", None)
        try:
            with patch("research_pipeline.env.find_project_root", return_value=other):
                assert load_env() is True
            assert os.environ.get("HOME_WINS_TEST") == "from-home"
        finally:
            os.environ.pop("HOME_WINS_TEST", None)

    def test_multiplai_home_without_env_falls_back(self, tmp_path: Path) -> None:
        """A CLAUDE_MULTIPLAI_HOME without .env falls back to the walk-up root."""
        home = tmp_path / "kit-empty"
        home.mkdir()
        root = tmp_path / "proj"
        (root / "dotfiles").mkdir(parents=True)
        (root / ".env.example").write_text("# example")
        (root / ".env").write_text('FALLBACK_TEST="from-walkup"\n')

        os.environ["CLAUDE_MULTIPLAI_HOME"] = str(home)
        os.environ.pop("FALLBACK_TEST", None)
        try:
            with patch("research_pipeline.env.find_project_root", return_value=root):
                assert load_env() is True
            assert os.environ.get("FALLBACK_TEST") == "from-walkup"
        finally:
            os.environ.pop("FALLBACK_TEST", None)

    def test_loads_env_vars_from_file(self, tmp_path: Path) -> None:
        """Values in .env are loaded into os.environ."""
        root = tmp_path / "proj"
        (root / "dotfiles").mkdir(parents=True)
        (root / ".env.example").write_text("# example")
        (root / ".env").write_text(
            'TEST_KEY_FROM_ENV_FILE="loaded-value"\n'
            'ANOTHER_TEST_KEY="another"\n'
        )
        start = root / "dotfiles"

        # Clear these vars first
        for k in ("TEST_KEY_FROM_ENV_FILE", "ANOTHER_TEST_KEY"):
            os.environ.pop(k, None)

        with patch("research_pipeline.env.find_project_root", return_value=root):
            result = load_env()

        try:
            assert result is True
            assert os.environ.get("TEST_KEY_FROM_ENV_FILE") == "loaded-value"
            assert os.environ.get("ANOTHER_TEST_KEY") == "another"
        finally:
            os.environ.pop("TEST_KEY_FROM_ENV_FILE", None)
            os.environ.pop("ANOTHER_TEST_KEY", None)

    def test_existing_env_vars_not_overridden(self, tmp_path: Path) -> None:
        """Environment variables set externally take precedence over .env."""
        root = tmp_path / "proj"
        (root / "dotfiles").mkdir(parents=True)
        (root / ".env.example").write_text("# example")
        (root / ".env").write_text('OVERRIDE_TEST="from-file"\n')

        os.environ["OVERRIDE_TEST"] = "from-shell"
        try:
            with patch("research_pipeline.env.find_project_root", return_value=root):
                load_env()
            assert os.environ["OVERRIDE_TEST"] == "from-shell"
        finally:
            os.environ.pop("OVERRIDE_TEST", None)

    def test_no_env_file_returns_false(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        with patch("research_pipeline.env.find_project_root", return_value=root):
            assert load_env() is False

    def test_no_project_root_returns_false(self) -> None:
        with patch("research_pipeline.env.find_project_root", return_value=None):
            assert load_env() is False
