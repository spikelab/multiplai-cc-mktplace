"""User preferences file (stages/preferences.py): location and append format."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import preferences  # noqa: E402


def test_path_follows_claude_config_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert preferences.path() == tmp_path / "cfg" / "multiplai-media" / "video-edit-preferences.md"


def test_path_defaults_to_home_claude(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert preferences.path() == tmp_path / ".claude" / "multiplai-media" / "video-edit-preferences.md"


def test_append_creates_then_adds_dated_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert preferences.read() == ""
    preferences.append(["Captions: 2 words per line"], today=date(2026, 10, 9))
    preferences.append(["- Headline boxes:   no rounded corners ", ""], today=date(2026, 10, 10))
    text = preferences.read()
    assert text.startswith("# video-edit preferences\n")
    assert text.endswith("- (2026-10-09) Captions: 2 words per line\n"
                         "- (2026-10-10) Headline boxes: no rounded corners\n")


def test_append_keeps_what_the_user_wrote(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    f = preferences.path()
    f.parent.mkdir(parents=True)
    f.write_text("my own notes")
    preferences.append(["x"], today=date(2026, 1, 1))
    assert f.read_text() == "my own notes\n- (2026-01-01) x\n"


def test_append_refuses_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        preferences.append(["  ", ""])
    assert not preferences.path().exists()
