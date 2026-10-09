"""The title-card font override: $VIDEO_EDIT_FONT, with $SCREEN_DEMO_FONT still read.

0.3.0 renamed the override when screen-demo became video-edit and promised
the old name keeps working. The golden render test pins _find_font, so these
tests are what catch a dropped fallback or the two lookups swapped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages.composite import _find_font  # noqa: E402


@pytest.fixture(autouse=True)
def _no_font_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_EDIT_FONT", raising=False)
    monkeypatch.delenv("SCREEN_DEMO_FONT", raising=False)


@pytest.mark.parametrize("bold", [True, False])
def test_new_name_is_honoured(monkeypatch: pytest.MonkeyPatch, bold: bool) -> None:
    monkeypatch.setenv("VIDEO_EDIT_FONT", "/fonts/new.ttf")
    assert _find_font(bold) == "/fonts/new.ttf"


@pytest.mark.parametrize("bold", [True, False])
def test_old_name_still_read(monkeypatch: pytest.MonkeyPatch, bold: bool) -> None:
    monkeypatch.setenv("SCREEN_DEMO_FONT", "/fonts/old.ttf")
    assert _find_font(bold) == "/fonts/old.ttf"


def test_new_name_wins_over_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_EDIT_FONT", "/fonts/new.ttf")
    monkeypatch.setenv("SCREEN_DEMO_FONT", "/fonts/old.ttf")
    assert _find_font(True) == "/fonts/new.ttf"
