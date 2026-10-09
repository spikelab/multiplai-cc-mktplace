"""Brand file loading (stages/brand.py) and how render uses it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import brand  # noqa: E402

_FIX = Path(__file__).resolve().parent / "fixtures" / "brand"
SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"


@pytest.mark.skipif(not Path(SERIF).exists(), reason="needs DejaVu Serif Bold")
def test_fixture_brand_loads_with_paths_resolved() -> None:
    b = brand.load(_FIX / "brand.json")
    assert b.caption_font() == SERIF
    assert (b.primary, b.accent, b.background) == ("#F5F0E6", "#FF5A36", "#1B2A41")
    assert b.logo is not None
    assert b.logo.path == str((_FIX / "logo.png").resolve())
    assert (b.logo.position, b.logo.scale) == ("tl", 0.18)


def test_defaults_without_a_brand_file() -> None:
    b = brand.default()
    assert b.caption_font() is None          # render falls back to the title-card font
    assert (b.primary, b.accent, b.logo) == ("#FFFFFF", "#FFD400", None)


def test_bad_colour_and_unknown_key_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "b.json").write_text(json.dumps({"accent": "red"}))
    with pytest.raises(ValueError, match="#RRGGBB"):
        brand.load(tmp_path / "b.json")
    (tmp_path / "c.json").write_text(json.dumps({"colour": "#FFFFFF"}))
    with pytest.raises(ValueError, match="unknown brand keys"):
        brand.load(tmp_path / "c.json")


def test_missing_font_file_is_named(tmp_path: Path) -> None:
    (tmp_path / "b.json").write_text(json.dumps({"font_file": "nope.ttf"}))
    with pytest.raises(FileNotFoundError, match="font_file not found"):
        brand.load(tmp_path / "b.json")


def test_ass_colour_is_bgr() -> None:
    assert brand.ass_colour("#112233") == "&H00332211"
