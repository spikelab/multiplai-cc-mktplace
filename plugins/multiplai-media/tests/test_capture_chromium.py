"""Proof-shot capture in a real headless Chromium (stages/capture.py).

Pages are local files loaded through file:// URLs, so nothing is fetched
from the web. These need the video-edit project environment and a Chromium
build Playwright can find:

    cd plugins/multiplai-media
    uv run --project skills/video-edit/scripts --with pytest python -m pytest tests/test_capture_chromium.py -q

Without playwright importable, or without the browser it expects, every test
here is skipped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

pytest.importorskip("playwright.sync_api")
from stages import capture, overlay  # noqa: E402


@pytest.fixture(scope="module")
def launch():
    try:
        overlay._Chromium().close()
    except overlay.OverlayError as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip(f"no Chromium for this Playwright: {e}")
        raise
    return overlay._Chromium


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:20px/1 sans-serif;height:2000px}
#a{position:absolute;left:100px;top:300px;width:400px;height:40px}
#b{position:absolute;left:60px;top:1500px;width:500px;height:30px}
</style></head><body>
<div id="a">Prices rose 4% in March</div>
<div style="display:none">hidden rose 4% copy</div>
<div id="b">Far below the fold</div>
</body></html>"""


def _page(tmp_path: Path, html: str) -> str:
    p = tmp_path / "page.html"
    p.write_text(html, encoding="utf-8")
    return p.as_uri()


def test_the_highlight_box_is_the_elements_box(tmp_path: Path, launch) -> None:
    url = _page(tmp_path, PAGE)
    shot = capture.capture(url, tmp_path / "out", highlight="rose 4%", launch=launch)
    meta = json.loads((tmp_path / "out" / "shot.json").read_text())
    assert (meta["width"], meta["height"]) == (1280, 800)
    assert meta["highlight"] == {"text": "rose 4%", "x": 100, "y": 300, "w": 400, "h": 40}
    assert shot.matched == "Prices rose 4% in March"


def test_full_page_finds_text_below_the_fold(tmp_path: Path, launch) -> None:
    url = _page(tmp_path, PAGE)
    shot = capture.capture(url, tmp_path / "view", highlight="Far below", launch=launch)
    assert shot.meta["highlight"] is None
    assert any("--full-page" in w for w in shot.warnings)
    shot = capture.capture(url, tmp_path / "full", highlight="Far below", full_page=True, launch=launch)
    assert shot.meta["height"] == 2000
    assert shot.meta["highlight"] == {"text": "Far below", "x": 60, "y": 1500, "w": 500, "h": 30}


def test_a_visible_password_field_is_a_login_wall(tmp_path: Path, launch) -> None:
    url = _page(tmp_path, "<!doctype html><form><input name=u><input type=password name=p></form>")
    with pytest.raises(capture.CaptureError) as e:
        capture.capture(url, tmp_path / "out", launch=launch)
    assert e.value.code == 2 and "--from-file" in str(e.value)
    assert not (tmp_path / "out").exists()


def test_the_page_is_not_changed(tmp_path: Path, launch) -> None:
    # Two captures of the same page give the same pixels with and without a
    # highlight: the box is recorded, never drawn on the page.
    url = _page(tmp_path, PAGE)
    capture.capture(url, tmp_path / "plain", launch=launch)
    capture.capture(url, tmp_path / "marked", highlight="rose 4%", launch=launch)
    assert (tmp_path / "plain" / "shot.png").read_bytes() == (tmp_path / "marked" / "shot.png").read_bytes()
