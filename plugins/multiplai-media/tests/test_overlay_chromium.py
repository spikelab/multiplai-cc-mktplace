"""Overlays in a real headless Chromium (stages/overlay.py, static/seek-shim.js).

These start Chromium through Playwright, so they need the video-edit project
environment and a Chromium build Playwright can find:

    cd plugins/multiplai-media
    uv run --project skills/video-edit/scripts --with pytest python -m pytest tests/test_overlay_chromium.py -q

Without playwright importable, or without the browser it expects, every test
here is skipped. The suite's usual `--no-project` run skips them.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

pytest.importorskip("playwright.sync_api")
from stages import overlay

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not on PATH")


@pytest.fixture(scope="module")
def chromium():
    try:
        browser = overlay._Chromium()
    except overlay.OverlayError as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip(f"no Chromium for this Playwright: {e}")
        raise
    yield browser
    browser.close()


def _source(chromium, html: str, mode: str = "alpha", w: int = 320, h: int = 180):
    return overlay.FrameSource(chromium.new_page(w, h), html, mode)


def _eval(src: overlay.FrameSource, expr: str):
    return src.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})["result"]["value"]


CARD = """<!doctype html><style>
body{margin:0}
.box{position:absolute;left:40px;top:40px;width:200px;height:100px;background:#2a6;
     box-shadow:0 10px 30px rgba(0,0,0,.5);animation:in .6s cubic-bezier(.2,.8,.2,1) .1s both}
@keyframes in{from{transform:translateY(60px);opacity:0}to{transform:none;opacity:1}}
</style><div class="box">Prezzo €</div>"""


@pytest.mark.parametrize("mode", ["alpha", "panel"])
def test_the_same_time_gives_identical_bytes(chromium, mode) -> None:
    a, b = _source(chromium, CARD, mode), _source(chromium, CARD, mode)
    first = a.frame(0.37)
    assert b.frame(0.37) == first
    a.frame(0.8)
    assert a.frame(0.37) == first
    assert a.frame(0.2) != first


def test_css_animation_is_seeked(chromium) -> None:
    src = _source(chromium, "<style>div{opacity:0;animation:f 1s linear forwards}"
                            "@keyframes f{to{opacity:1}}</style><div>x</div>")
    src.frame(0.25)
    assert _eval(src, "+getComputedStyle(document.querySelector('div')).opacity") == pytest.approx(0.25, abs=0.01)


def test_web_animation_made_by_a_script_is_seeked(chromium) -> None:
    src = _source(chromium, "<div id=d>x</div><script>d.animate([{opacity:0},{opacity:1}],"
                            "{duration:1000,fill:'forwards'})</script>")
    src.frame(0.6)
    assert _eval(src, "+getComputedStyle(d).opacity") == pytest.approx(0.6, abs=0.01)


def test_requestanimationframe_page_follows_the_virtual_clock(chromium) -> None:
    src = _source(chromium, """<div id=d>x</div><script>
      function tick(now){ d.dataset.now = now; d.dataset.date = Date.now();
                          d.dataset.perf = performance.now(); requestAnimationFrame(tick); }
      requestAnimationFrame(tick);
    </script>""")
    src.frame(1.25)
    assert _eval(src, "[+d.dataset.now, +d.dataset.date, +d.dataset.perf]") == [1250, 1250, 1250]
    src.frame(0.5)
    assert _eval(src, "+d.dataset.now") == 500


def test_gsap_global_timeline_is_paused_and_seeked(chromium) -> None:
    # A stand-in with GSAP's global-timeline API; the real library needs no
    # network either way, because pages must inline it.
    src = _source(chromium, """<div id=d>x</div><script>
      window.gsap = {globalTimeline: {
        paused: false,
        pause() { this.paused = true; },
        totalTime(t, suppress) { d.dataset.t = t; d.dataset.suppress = suppress; }}};
    </script>""")
    src.frame(0.75)
    assert _eval(src, "[+d.dataset.t, d.dataset.suppress, gsap.globalTimeline.paused]") == [0.75, "true", True]


def test_lottie_animations_go_to_the_frame(chromium) -> None:
    src = _source(chromium, """<div id=d>x</div><script>
      const anim = {frameRate: 24, totalFrames: 48, loop: false,
                    goToAndStop(v, isFrame) { d.dataset.v = v; d.dataset.isFrame = isFrame; }};
      window.lottie = {getRegisteredAnimations: () => [anim]};
    </script>""")
    src.frame(0.5)
    assert _eval(src, "[+d.dataset.v, d.dataset.isFrame]") == [12, "true"]
    src.frame(5.0)                                   # past the end: holds the last frame
    assert _eval(src, "+d.dataset.v") == 47


def test_a_page_seek_function_wins(chromium) -> None:
    src = _source(chromium, "<div id=d>x</div><script>window.__seek = t => { d.dataset.t = t; return 7; }</script>")
    src.frame(0.4)
    assert _eval(src, "+d.dataset.t") == 0.4


def test_the_page_cannot_reach_the_network(chromium) -> None:
    src = _source(chromium, """<img id=i src="https://example.com/x.png"><script>
      i.onerror = () => { document.body.dataset.blocked = 1 };
    </script>""")
    src.frame(0)
    assert _eval(src, "document.body.dataset.blocked") == "1"


def _alpha_values(mov: Path, x: int, y: int, w: int, h: int) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mov), "-frames:v", "1",
         "-vf", f"crop={w}:{h}:{x}:{y},alphaextract,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout


def test_half_transparent_box_keeps_partial_alpha_in_prores(tmp_path) -> None:
    page = tmp_path / "half.html"
    page.write_text("<!doctype html><style>body{margin:0;background:#fff}"
                    "div{position:absolute;left:20px;top:20px;width:100px;height:60px;"
                    "background:rgba(255,0,0,.5)}</style><div></div>")
    try:
        mov = overlay.render_overlay(page, 160, 100, 30, 0.1, tmp_path / "cache", mode="alpha",
                                     log=lambda s: None)
    except overlay.OverlayError as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip(str(e))
        raise
    inside = _alpha_values(mov, 30, 30, 80, 40)
    outside = _alpha_values(mov, 130, 0, 30, 100)
    assert all(0 < a < 255 for a in inside)
    assert all(abs(a - 128) <= 2 for a in inside)
    assert set(outside) == {0}                       # the page's white body is gone
