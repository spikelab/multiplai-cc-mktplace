"""The Review tab's live overlays (static/review-live.js) on the real page, in headless Chromium.

A review server runs on loopback over a job with one render and its
.edl.json sidecar, which names one keyed overlay page. The page is driven
end to end: turn the live overlays on, pick an element in the overlay's
frame, edit it, send the `restyle` row, and drop the sidecar. Along the way
it checks that the page reads only "ready" and "picked" messages from the
frames it made (event.source) with an opaque origin, and that the Review
tab's "render" event follows the sidecar.

Media elements are replaced by a stand-in that never fetches or decodes
(MEDIA_STAND_IN): headless Chromium on some arm64 hosts dies with SIGILL as
soon as a <video> loads media, and nothing here needs real frames. Needs
Playwright's Chromium, as tests/test_overlay_bridge_chromium.py does:

    cd plugins/multiplai-media
    uv run --no-project --with pytest --with playwright python -m pytest tests/test_review_live_chromium.py -q

Without playwright importable, or without its browser, every test here is skipped.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

sync_api = pytest.importorskip("playwright.sync_api")
import make_job
from stages import review_server as rs

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:640px;height:200px;overflow:hidden;background:#00FF00;font-family:sans-serif}
.card{position:absolute;left:20px;top:20px;width:600px;height:160px;background:#fff}
.t{font-size:48px;color:rgb(31, 42, 68)}
</style></head><body><div class="card"><div class="t">Deploys went 2 to 40</div></div></body></html>"""
SELECTOR = "body > div.card > div.t"

# Media elements that never fetch or decode: time, size and events are simulated.
MEDIA_STAND_IN = """(() => {
  const P = HTMLMediaElement.prototype, V = HTMLVideoElement.prototype, st = new WeakMap();
  const S = (el) => { if (!st.has(el)) st.set(el, { src: "", t: 0, paused: true, ready: false }); return st.get(el); };
  const fire = (el, n) => el.dispatchEvent(new Event(n));
  const def = (o, k, d) => Object.defineProperty(o, k, { configurable: true, ...d });
  def(P, "src", { get() { return S(this).src; }, set(v) {
    const s = S(this); s.src = String(v); s.t = 0; s.ready = false;
    if (s.src) setTimeout(() => { s.ready = true; ["loadedmetadata", "loadeddata", "canplay", "timeupdate"].forEach((n) => fire(this, n)); }, 20);
  } });
  const setAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (n, v) {
    if (this instanceof HTMLMediaElement && String(n).toLowerCase() === "src") { this.src = v; return; }
    return setAttr.call(this, n, v);
  };
  def(P, "currentSrc", { get() { return S(this).src; } });
  def(P, "currentTime", { get() { return S(this).t; }, set(v) {
    S(this).t = Math.max(0, Math.min(8, Number(v) || 0));
    setTimeout(() => { fire(this, "seeked"); fire(this, "timeupdate"); }, 0);
  } });
  def(P, "duration", { get() { return S(this).ready ? 8 : NaN; } });
  def(P, "paused", { get() { return S(this).paused; } });
  def(P, "readyState", { get() { return S(this).ready ? 4 : 0; } });
  def(V, "videoWidth", { get() { return S(this).ready ? 720 : 0; } });
  def(V, "videoHeight", { get() { return S(this).ready ? 1280 : 0; } });
  P.load = function () {};
  P.play = function () { const s = S(this); if (s.paused) { s.paused = false; fire(this, "play"); } return Promise.resolve(); };
  P.pause = function () { const s = S(this); if (!s.paused) { s.paused = true; fire(this, "pause"); } };
  document.addEventListener("DOMContentLoaded", () => {
    const v = document.getElementById("video");
    if (v) { v.style.height = "640px"; v.style.width = "360px"; }
  });
})();"""


def sidecar(page: Path) -> dict:
    edl = {"source": "/rec/x.mp4", "segments": [], "output": {"width": 720, "height": 1280}, "captions": None,
           "overlays": [{"html": str(page), "start": 1.0, "end": 3.0, "mode": "keyed",
                         "region": {"x": 40, "y": 860, "w": 640, "h": 200}, "style": "flat-vector"}]}
    return {"format": 1, "video": "clip-01.mp4", "edl_file": "/jobs/x/edl.json", "edl": edl,
            "duration": 8.0, "segments": [], "captions_events": []}


@pytest.fixture(scope="module")
def job(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    paths = make_job.build(tmp / "job", video=False, offers=False)
    (paths["dir"] / "clip-01.mp4").write_bytes(b"render")
    page = tmp / "card.html"
    page.write_text(PAGE)
    side = paths["dir"] / "clip-01.edl.json"
    side.write_text(json.dumps(sidecar(page)))
    httpd, token, _urls = rs.serve(paths["dir"], paths["dir"] / "review", host="127.0.0.1", port=19665, span=200)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"port": httpd.server_address[1], "token": token, "dir": paths["dir"], "page": page, "sidecar": side,
           "mailbox": paths["dir"] / "review" / "comments.jsonl"}
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(paths["dir"] / "review")


@pytest.fixture(scope="module")
def page(job):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 — Playwright raises a plain Error without its browser
            pytest.skip(f"no Chromium for this Playwright: {e}")
        pg = browser.new_page(viewport={"width": 1200, "height": 1000})
        pg.add_init_script(MEDIA_STAND_IN)
        pg.goto(f"http://127.0.0.1:{job['port']}/?t={job['token']}")
        pg.click("#tab-review")
        pg.wait_for_selector("#live-toggle")
        _wait(pg, "document.getElementById('video').readyState === 4")
        yield pg
        browser.close()


def _frame(pg):
    return pg.frame_locator("#live-layer iframe")


# The page's CSP (script-src 'self') refuses the string eval wait_for_function polls with,
# so waits poll from here with evaluate, which the CSP does not apply to.
def _wait(pg, js: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not pg.evaluate(js):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {js}")
        time.sleep(0.05)


def _forge_picked(pg) -> None:
    """Well-formed "picked" messages from places review-live.js must not listen to.

    One comes from this page itself (this server's origin). The other is a real pick in a
    second sandboxed frame of the same overlay page that this test made, not review-live.js:
    an opaque origin, like the page's own frames, but not one of them.
    """
    pg.evaluate("""(props) => window.postMessage({type: 'picked', selector: 'body', box: {x: 0, y: 0, w: 10, h: 10},
        styles: Object.fromEntries(props.map((p) => [p, '1'])), text: 'forged'}, '*')""", list(rs.RESTYLE_PROPS))
    pg.evaluate("""() => new Promise((ok) => {
        const f = document.createElement('iframe');
        f.id = 'stranger';
        f.setAttribute('sandbox', 'allow-scripts');
        f.style.cssText = 'position:fixed;left:0;top:0;width:640px;height:200px;z-index:99;border:0';
        f.src = document.querySelector('#live-layer iframe').src;
        f.addEventListener('load', () => setTimeout(() => { f.contentWindow.postMessage({type: 'pick', on: true}, '*'); ok(); }, 200));
        document.body.append(f);
    })""")
    pg.frame_locator("#stranger").locator(".t").click()
    pg.wait_for_timeout(300)
    pg.evaluate("document.getElementById('stranger').remove()")


def test_live_overlay_plays_over_its_region_while_on_screen(page) -> None:
    page.check("#live-toggle")
    _wait(page, "document.querySelectorAll('#live-layer iframe').length === 1")
    frame = page.locator("#live-layer iframe")
    assert frame.get_attribute("sandbox") == "allow-scripts"
    page.evaluate("document.getElementById('video').currentTime = 0.5")
    page.wait_for_timeout(300)
    assert "on" not in (frame.get_attribute("class") or "")             # before the overlay starts
    page.evaluate("document.getElementById('video').currentTime = 1.5")
    _wait(page, "document.querySelector('#live-layer iframe').classList.contains('on')")
    # keyed: the bridge was told to drop the key colour
    assert _frame(page).locator("body").evaluate("(b) => getComputedStyle(b).backgroundColor") == "rgba(0, 0, 0, 0)"


def test_forged_picked_messages_are_ignored(page) -> None:
    page.click("#pick-toggle")
    assert page.get_attribute("#pick-toggle", "aria-pressed") == "true"
    _forge_picked(page)
    assert page.locator("#restyle-form").is_hidden()
    assert page.get_attribute("#pick-toggle", "aria-pressed") == "true"   # still waiting for a real pick


def test_pick_edit_and_send_a_restyle(page, job) -> None:
    _frame(page).locator(".t").click()
    _wait(page, "!document.getElementById('restyle-form').hidden")
    assert page.get_attribute("#pick-toggle", "aria-pressed") == "false"
    assert SELECTOR in page.text_content("#restyle-target")
    assert page.input_value("#restyle-color") == "rgb(31, 42, 68)"
    # Space and the arrows type into the field; they do not play or seek the video.
    page.fill("#restyle-font-family", "")
    page.click("#restyle-font-family")
    page.keyboard.type("Times New Roman, serif")
    page.keyboard.press("ArrowLeft")
    assert page.input_value("#restyle-font-family") == "Times New Roman, serif"
    assert page.evaluate("document.getElementById('video').paused") is True
    assert page.evaluate("document.getElementById('video').currentTime") == 1.5
    page.fill("#restyle-color", "#FF3B30")
    t = _frame(page).locator(".t")
    assert t.evaluate("(e) => getComputedStyle(e).color") == "rgb(255, 59, 48)"     # shown at once
    page.fill("#restyle-note", "warmer title")
    page.click("#restyle-send")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (job["mailbox"].is_file() and job["mailbox"].read_text().strip()):
        time.sleep(0.05)
    [row] = [json.loads(x) for x in job["mailbox"].read_text().splitlines()]
    assert (row["kind"], row["video"], row["version"], row["overlay"]) == ("restyle", "clip-01", 1, 0)
    assert (row["html"], row["selector"], row["t"], row["text"]) == (str(job["page"]), SELECTOR, 1.5, "warmer title")
    assert row["changes"]["color"] == ["rgb(31, 42, 68)", "#FF3B30"]
    assert row["changes"]["font-family"][1] == "Times New Roman, serif"
    # after the send the form starts over from the page as rendered, ready for another edit
    _wait(page, "document.getElementById('restyle-color').value === 'rgb(31, 42, 68)'")
    assert not page.locator("#restyle-form").is_hidden()
    assert t.evaluate("(e) => getComputedStyle(e).color") == "rgb(31, 42, 68)"


def test_live_overlays_follow_the_sidecar(page, job) -> None:
    kept = job["sidecar"].read_text()
    job["sidecar"].unlink()
    page.dispatch_event("#version", "change")          # selecting the render again reloads its sidecar
    _wait(page, "document.querySelectorAll('#live-layer iframe').length === 0")
    assert page.locator("#restyle-form").is_hidden()
    assert "no .edl.json" in page.text_content("#live-panel")
    assert page.is_disabled("#pick-toggle")
    job["sidecar"].write_text(kept)
    page.dispatch_event("#version", "change")
    _wait(page, "document.querySelectorAll('#live-layer iframe').length === 1")
    assert page.is_enabled("#pick-toggle")


def test_a_frame_that_is_not_opaque_is_not_listened_to(page) -> None:
    # The server's CSP sandboxes every overlay page, so its frames are always opaque. Take that
    # away here, and reload the page's own frame with same-origin rights: its messages then carry
    # this server's origin, not "null", and review-live.js ignores its pick.
    def no_csp(route):
        res = route.fetch()
        headers = {k: v for k, v in res.headers.items() if k.lower() != "content-security-policy"}
        route.fulfill(response=res, headers=headers)
    page.route("**/media/overlay?*", no_csp)
    try:
        page.evaluate("""() => { const f = document.querySelector('#live-layer iframe');
            f.setAttribute('sandbox', 'allow-scripts allow-same-origin'); f.src = f.src; }""")
        _wait(page, "(() => { try { return document.querySelector('#live-layer iframe').contentDocument"
                    ".querySelector('.t') !== null; } catch (e) { return false; } })()")
        page.evaluate("document.getElementById('video').currentTime = 1.5")
        _wait(page, "document.querySelector('#live-layer iframe').classList.contains('on')")
        page.click("#pick-toggle")
        _frame(page).locator(".t").click()
        page.wait_for_timeout(300)
        assert page.locator("#restyle-form").is_hidden()
        assert page.get_attribute("#pick-toggle", "aria-pressed") == "true"
    finally:
        page.unroute("**/media/overlay?*")
