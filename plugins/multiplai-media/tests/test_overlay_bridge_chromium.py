"""static/overlay-bridge.js in a real headless Chromium: seek, transparency, pick and applyStyle.

The page is loaded on its own, so it is its own parent: messages it posts to
itself come from `window.parent`, as the Review tab's do, and the bridge's
answers arrive on the same window. Needs Playwright's Chromium, as
tests/test_overlay_chromium.py does:

    cd plugins/multiplai-media
    uv run --no-project --with pytest --with playwright python -m pytest tests/test_overlay_bridge_chromium.py -q

Without playwright importable, or without its browser, every test here is skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

sync_api = pytest.importorskip("playwright.sync_api")
from stages import review_server as rs  # noqa: E402

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:400px;height:200px;overflow:hidden;background:#00FF00;font-family:sans-serif}
.card{position:absolute;left:20px;top:20px;width:300px;height:120px;background:#fff}
.t{font-size:40px;color:rgb(31, 42, 68);opacity:0;animation:in .5s linear 1s forwards}
.t + .t{font-size:20px}
@keyframes in{to{opacity:1}}
</style></head><body><div class="card"><div class="t">Done in 3 steps</div><div class="t">second</div></div></body></html>"""


@pytest.fixture(scope="module")
def page():
    html = rs.inject_scripts(PAGE, [(rs.STATIC_DIR / n).read_text() for n in rs.OVERLAY_SCRIPTS])
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 — Playwright raises a plain Error without its browser
            pytest.skip(f"no Chromium for this Playwright: {e}")
        pg = browser.new_page(viewport={"width": 400, "height": 200})
        pg.route("http://bridge.test/", lambda route: route.fulfill(body=html, content_type="text/html"))
        pg.add_init_script("window.__got = []; addEventListener('message', (e) => { if (e.data && e.data.type !== undefined"
                           " && ['ready', 'picked'].includes(e.data.type)) window.__got.push(e.data); });")
        pg.goto("http://bridge.test/")
        pg.wait_for_function("window.__got.some((m) => m.type === 'ready')", polling=50)
        yield pg
        browser.close()


# The seek shim takes over requestAnimationFrame, so waits poll on a timer, never on frames.
def _send(pg, msg: dict) -> None:
    pg.evaluate("(m) => new Promise((ok) => { window.postMessage(m, '*'); setTimeout(ok, 50); })", msg)


def test_ready_reports_the_page_size(page) -> None:
    ready = page.evaluate("window.__got.find((m) => m.type === 'ready')")
    assert (ready["width"], ready["height"], ready["seek"]) == (400, 200, "default")


def test_seek_moves_the_page_clock(page) -> None:
    _send(page, {"type": "seek", "t": 0.5})
    assert page.evaluate("getComputedStyle(document.querySelector('.t')).opacity") == "0"
    _send(page, {"type": "seek", "t": 1.25})
    assert page.evaluate("getComputedStyle(document.querySelector('.t')).opacity") == "0.5"


def test_init_transparent_clears_the_key_background(page) -> None:
    _send(page, {"type": "init", "transparent": True})
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgba(0, 0, 0, 0)"
    _send(page, {"type": "init", "transparent": False})
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(0, 255, 0)"


def test_pick_answers_with_the_element_and_keeps_the_click_from_the_page(page) -> None:
    page.evaluate("window.__clicks = 0; document.querySelector('.t').addEventListener('click', () => window.__clicks++)")
    _send(page, {"type": "seek", "t": 2})
    _send(page, {"type": "pick", "on": True})
    page.mouse.click(60, 45)
    page.wait_for_function("window.__got.some((m) => m.type === 'picked')", polling=50)
    picked = page.evaluate("window.__got.find((m) => m.type === 'picked')")
    assert picked["selector"] == "body > div.card > div.t:nth-of-type(1)"
    assert page.evaluate("(s) => document.querySelectorAll(s).length", picked["selector"]) == 1
    assert picked["styles"]["color"] == "rgb(31, 42, 68)" and picked["styles"]["font-size"] == "40px"
    assert set(picked["styles"]) == set(rs.RESTYLE_PROPS)
    assert picked["text"] == "Done in 3 steps" and picked["box"]["x"] == 20
    assert page.evaluate("window.__clicks") == 0
    _send(page, {"type": "pick", "on": False})
    page.mouse.click(60, 45)
    assert page.evaluate("window.__clicks") == 1


def test_apply_style_wins_over_the_animation_and_clear_takes_it_back(page) -> None:
    sel = "body > div.card > div.t:nth-of-type(1)"
    _send(page, {"type": "applyStyle", "selector": sel, "changes": {"color": "#FF3B30", "opacity": "0.25",
                                                                    "width": "1px"}})
    style = "(s) => { const c = getComputedStyle(document.querySelector(s)); return [c.color, c.opacity, c.width]; }"
    color, opacity, width = page.evaluate(style, sel)
    assert (color, opacity) == ("rgb(255, 59, 48)", "0.25") and width != "1px"     # only the restyle properties
    _send(page, {"type": "clearStyle"})
    assert page.evaluate(style, sel)[:2] == ["rgb(31, 42, 68)", "1"]


def test_messages_from_anything_but_the_parent_are_ignored(page) -> None:
    page.evaluate("""() => new Promise((ok) => {
        const f = document.createElement('iframe'); document.body.append(f);
        f.contentWindow.eval("parent.postMessage({type: 'applyStyle', selector: 'div.card', changes: {color: '#123456'}}, '*')");
        setTimeout(ok, 50); })""")
    assert page.evaluate("getComputedStyle(document.querySelector('.card')).color") != "rgb(18, 52, 86)"
