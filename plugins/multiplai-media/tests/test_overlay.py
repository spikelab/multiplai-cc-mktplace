"""Motion graphics (stages/overlay.py, EDL overlays, composite.overlay_graph).

The renderer runs against a fake browser, page and CDP session that log every
call and answer each capture with bytes naming its time, and a fake ffmpeg
that records what reaches its stdin; no browser starts and no ffmpeg runs.
The real-browser checks are in test_overlay_chromium.py.
"""
from __future__ import annotations

import base64
import builtins
import sys
import threading
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import composite, overlay
from stages.edl import EDL, Overlay, Region

PAGE = "<!doctype html><style>div{animation:a 1s forwards}@keyframes a{to{opacity:1}}</style><div>Prezzo €</div>"


class FakeRoute:
    def __init__(self, url: str):
        self.request = type("Req", (), {"url": url})()
        self.answer: tuple | None = None

    def fulfill(self, **kw) -> None:
        self.answer = ("fulfill", kw)

    def abort(self) -> None:
        self.answer = ("abort",)


class FakeCDP:
    def __init__(self, world: World):
        self.world = world
        self.t: float | None = None

    def send(self, method: str, params: dict | None = None) -> dict:
        w = self.world
        with w.lock:
            w.calls.append(("cdp", method, params))
        if method == "Runtime.evaluate":
            self.t = float(params["expression"][len("__veFrame("):-1])
            if w.throw_at is not None and abs(self.t - w.throw_at) < 1e-5:
                return {"exceptionDetails": {"exception": {"description": "TypeError: boom"}}}
            return {"result": {"type": "number", "value": 1}}
        if method == "Page.captureScreenshot":
            with w.lock:
                w.captures += 1
                if w.fail_at_capture == w.captures:
                    raise RuntimeError("capture failed")
            return {"data": base64.b64encode(f"<{self.t:.6f}>".encode()).decode()}
        return {}


class FakePage:
    def __init__(self, world: World, width: int, height: int):
        self.world, self.size = world, (width, height)
        self.routes: list = []
        self.context = self

    def _log(self, *call) -> None:
        with self.world.lock:
            self.world.calls.append(call)

    def add_init_script(self, script: str) -> None:
        self._log("add_init_script", script)

    def route(self, url: str, handler) -> None:
        self._log("route", url)
        self.routes.append(handler)

    def goto(self, url: str) -> None:
        self._log("goto", url)
        for u in (url, "https://cdn.example.com/gsap.js"):
            r = FakeRoute(u)
            self.routes[0](r)
            self.world.answers[u] = r.answer

    def evaluate(self, expression: str):
        self._log("evaluate", expression)
        return self.world.ready

    def add_style_tag(self, content: str) -> None:
        self._log("add_style_tag", content)

    def new_cdp_session(self, page) -> FakeCDP:
        self._log("new_cdp_session")
        return FakeCDP(self.world)


class FakeBrowser:
    def __init__(self, world: World):
        self.world = world

    def new_page(self, width: int, height: int) -> FakePage:
        with self.world.lock:
            self.world.calls.append(("new_page", width, height))
        return FakePage(self.world, width, height)

    def close(self) -> None:
        with self.world.lock:
            self.world.calls.append(("close",))
            self.world.closed += 1


class FakeStdin:
    def __init__(self, proc: FakeFfmpeg):
        self.proc = proc

    def write(self, data: bytes) -> None:
        self.proc.written.append(data)

    def close(self) -> None:
        self.proc.stdin_closed = True


class FakeFfmpeg:
    """subprocess.Popen stand-in: records stdin, writes its output file on wait()."""

    def __init__(self, world: World, cmd: list[str], **kw):
        self.cmd, self.kw, self.world = cmd, kw, world
        self.written: list[bytes] = []
        self.stdin = FakeStdin(self)
        self.stdin_closed = False
        self.returncode: int | None = None
        world.ffmpeg.append(self)

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def wait(self) -> int:
        if self.returncode is None:
            ok = not self.world.ffmpeg_fails
            Path(self.cmd[-1]).write_bytes(b"mov" if ok else b"half")
            self.returncode = 0 if ok else 1
        return self.returncode


class World:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list = []
        self.answers: dict = {}
        self.captures = 0
        self.closed = 0
        self.launched = 0
        self.ready = "default"
        self.throw_at: float | None = None
        self.fail_at_capture: int | None = None
        self.ffmpeg_fails = False
        self.ffmpeg: list[FakeFfmpeg] = []

    def launch(self) -> FakeBrowser:
        with self.lock:
            self.launched += 1
        return FakeBrowser(self)

    def popen(self, cmd, **kw) -> FakeFfmpeg:
        return FakeFfmpeg(self, cmd, **kw)


@pytest.fixture
def world() -> World:
    return World()


def _render(world: World, tmp_path: Path, duration=0.1, fps=30, mode="keyed", workers=1, log=None):
    out = tmp_path / "ov" / "overlay.mov"
    n = overlay.render_to(PAGE, 640, 360, fps, duration, mode, out, launch=world.launch,
                          popen=world.popen, workers=workers, log=log or (lambda s: None))
    return n, out


def _frame(t: float) -> bytes:
    return f"<{t:.6f}>".encode()


# --- the page conversation -----------------------------------------------------------

def test_render_drives_the_page_then_closes(world, tmp_path) -> None:
    n, out = _render(world, tmp_path)
    assert n == 3
    names = [c[0] if c[0] != "cdp" else c[1] for c in world.calls]
    assert names == [
        "new_page", "add_init_script", "route", "goto", "evaluate", "new_cdp_session",
        "Runtime.evaluate", "Page.captureScreenshot",
        "Runtime.evaluate", "Page.captureScreenshot",
        "Runtime.evaluate", "Page.captureScreenshot",
        "close"]
    assert world.calls[0] == ("new_page", 640, 360)
    assert world.calls[1] == ("add_init_script", overlay.SEEK_SHIM)
    assert world.calls[3] == ("goto", overlay.PAGE_URL)
    assert "document.fonts.ready" in world.calls[4][1] and "__veReady" in world.calls[4][1]
    seeks = [c[2]["expression"] for c in world.calls if c[:2] == ("cdp", "Runtime.evaluate")]
    assert seeks == ["__veFrame(0.000000)", "__veFrame(0.033333)", "__veFrame(0.066667)"]
    assert world.ffmpeg[0].written == [_frame(0), _frame(1 / 30), _frame(2 / 30)]
    assert out.read_bytes() == b"mov"


def test_the_page_is_served_as_utf8_and_gets_no_network(world, tmp_path) -> None:
    _render(world, tmp_path)
    kind, kw = world.answers[overlay.PAGE_URL]
    assert kind == "fulfill" and kw["body"] == PAGE and "charset=utf-8" in kw["content_type"]
    assert world.answers["https://cdn.example.com/gsap.js"] == ("abort",)


def test_seek_shim_is_installed_before_the_page_loads() -> None:
    shim = overlay.SEEK_SHIM
    for hook in ("window.__veFrame", "window.__veReady", "requestAnimationFrame",
                 "performance", "proto.animate", "globalTimeline", "getRegisteredAnimations",
                 "animationDelay", "window.__seek"):
        assert hook in shim, hook
    assert "Apache-2.0" in shim


@pytest.mark.parametrize("mode,params", [
    ("alpha", {"format": "png", "optimizeForSpeed": False}),
    ("panel", {"format": "jpeg", "quality": 90, "optimizeForSpeed": True}),
    ("keyed", {"format": "png", "optimizeForSpeed": True}),
])
def test_capture_format_follows_the_mode(world, tmp_path, mode, params) -> None:
    _render(world, tmp_path, mode=mode)
    shots = [c[2] for c in world.calls if c[:2] == ("cdp", "Page.captureScreenshot")]
    assert shots and all(s == params for s in shots)


def test_alpha_mode_makes_the_background_transparent(world, tmp_path) -> None:
    _render(world, tmp_path, mode="alpha")
    assert ("cdp", "Emulation.setDefaultBackgroundColorOverride",
            {"color": {"r": 0, "g": 0, "b": 0, "a": 0}}) in world.calls
    assert ("add_style_tag", overlay.TRANSPARENT_CSS) in world.calls
    assert "background:transparent!important" in overlay.TRANSPARENT_CSS


@pytest.mark.parametrize("mode", ["panel", "keyed"])
def test_opaque_modes_keep_the_page_background(world, tmp_path, mode) -> None:
    _render(world, tmp_path, mode=mode)
    assert not [c for c in world.calls if c[0] == "add_style_tag" or
                (c[0] == "cdp" and c[1] == "Emulation.setDefaultBackgroundColorOverride")]


def test_several_pages_interleave_frames_and_ffmpeg_gets_them_in_order(world, tmp_path) -> None:
    n, _ = _render(world, tmp_path, duration=1.0, workers=3)
    assert n == 30 and world.launched == 3 and world.closed == 3
    assert world.ffmpeg[0].written == [_frame(i / 30) for i in range(30)]


def test_never_more_pages_than_frames(world, tmp_path) -> None:
    _render(world, tmp_path, duration=0.05, workers=4)      # 2 frames
    assert world.launched == 2


def test_a_failing_capture_stops_every_page_and_leaves_no_file(world, tmp_path) -> None:
    world.fail_at_capture = 3
    with pytest.raises(RuntimeError, match="capture failed"):
        _render(world, tmp_path, duration=1.0, workers=2)
    assert world.closed == world.launched == 2
    assert not list((tmp_path / "ov").glob("*.mov"))
    assert world.ffmpeg[0].returncode == -9


def test_a_page_that_throws_while_seeking_is_reported(world, tmp_path) -> None:
    world.throw_at = 1 / 30
    with pytest.raises(overlay.OverlayError, match=r"threw while seeking to 0.033s: TypeError: boom"):
        _render(world, tmp_path)


def test_a_page_without_the_shim_is_refused(world, tmp_path) -> None:
    world.ready = None
    with pytest.raises(overlay.OverlayError, match="did not load the seek shim"):
        _render(world, tmp_path)
    assert world.captures == 0


def test_missing_playwright_names_the_uv_command(monkeypatch) -> None:
    real_import = builtins.__import__

    def no_playwright(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_playwright)
    with pytest.raises(overlay.OverlayError) as e:
        overlay._Chromium()
    msg = str(e.value)
    assert f"uv run --project {overlay.SCRIPTS_DIR} python3" in msg
    assert "container" not in msg.lower() and "kit" not in msg.lower() and "ab " not in msg


def test_missing_chromium_names_the_install_command(monkeypatch) -> None:
    class Launcher:
        def launch(self, **kw):
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at /x/chrome\nmore")

    class PW:
        chromium = Launcher()
        stopped = False

        def stop(self):
            PW.stopped = True

    fake = type(sys)("playwright.sync_api")
    fake.sync_playwright = lambda: type("S", (), {"start": lambda self: PW()})()
    monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    with pytest.raises(overlay.OverlayError) as e:
        overlay._Chromium()
    msg = str(e.value)
    assert "Executable doesn't exist at /x/chrome" in msg and "more" not in msg
    assert msg.endswith("playwright install chromium-headless-shell")
    assert PW.stopped
    assert "container" not in msg.lower() and "kit" not in msg.lower()


def test_estimate_is_printed_before_rendering(world, tmp_path) -> None:
    lines: list[str] = []
    _render(world, tmp_path, duration=2.0, workers=2, log=lines.append)
    assert lines[0] == (f"→ overlay: 60 frames at 640x360, 2 page(s), "
                        f"about {60 * overlay.SEC_PER_FRAME / 2 ** 0.5:.0f}s")
    assert world.captures == 60


@pytest.mark.parametrize("env,expect", [("3", 3), ("1", 1)])
def test_workers_can_be_set_from_the_environment(monkeypatch, env, expect) -> None:
    monkeypatch.setenv(overlay.WORKERS_ENV, env)
    assert overlay.default_workers() == expect


@pytest.mark.parametrize("env", ["0", "two", "-1"])
def test_bad_worker_counts_are_refused(monkeypatch, env) -> None:
    monkeypatch.setenv(overlay.WORKERS_ENV, env)
    with pytest.raises(overlay.OverlayError, match=overlay.WORKERS_ENV):
        overlay.default_workers()


def test_default_workers_leave_two_cores_and_cap_at_four(monkeypatch) -> None:
    monkeypatch.delenv(overlay.WORKERS_ENV, raising=False)
    for cpus, expect in ((1, 1), (3, 1), (5, 3), (16, 4)):
        monkeypatch.setattr(overlay.os, "cpu_count", lambda c=cpus: c)
        assert overlay.default_workers() == expect


def test_frame_count() -> None:
    assert overlay.frame_count(1.0, 30) == 30
    assert overlay.frame_count(0.1, 30) == 3
    assert overlay.frame_count(0.0, 30) == 1


def test_content_hash_covers_page_parameters_and_mode() -> None:
    base = overlay.content_hash(PAGE, 640, 360, 30, 2.0)
    assert base == overlay.content_hash(PAGE, 640, 360, 30, 2.0, "keyed")
    assert len({base,
                overlay.content_hash(PAGE + " ", 640, 360, 30, 2.0),
                overlay.content_hash(PAGE, 642, 360, 30, 2.0),
                overlay.content_hash(PAGE, 640, 360, 25, 2.0),
                overlay.content_hash(PAGE, 640, 360, 30, 2.5),
                overlay.content_hash(PAGE, 640, 360, 30, 2.0, "alpha")}) == 6


def test_cached_overlay_is_not_rendered_again(tmp_path) -> None:
    html = tmp_path / "card.html"
    html.write_text(PAGE)
    key = overlay.content_hash(PAGE, 640, 360, 30, 2.0, "alpha")
    cached = tmp_path / "cache" / "overlays" / key / "overlay.mov"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"mov")

    def explode(*a, **k):
        raise AssertionError("must not run anything on a cache hit")
    got = overlay.render_overlay(html, 640, 360, 30, 2.0, tmp_path / "cache", mode="alpha",
                                 launch=explode, popen=explode, log=lambda s: None)
    assert got == cached


def test_encode_pipes_into_prores_4444_with_alpha(world, tmp_path) -> None:
    _, out = _render(world, tmp_path, mode="alpha")
    ff = world.ffmpeg[0]
    cmd = ff.cmd
    assert cmd[cmd.index("-f") + 1] == "image2pipe" and cmd[cmd.index("-i") + 1] == "-"
    assert cmd[cmd.index("-c:v") + 1] == "png"
    assert cmd[cmd.index("-c:v", cmd.index("-i")) + 1] == "prores_ks"
    assert cmd[cmd.index("-profile:v") + 1] == "4444"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuva444p10le"
    assert cmd[cmd.index("-colorspace") + 1] == "bt709"
    assert cmd[cmd.index("-vf") + 1] == f"{overlay.TO_BT709},format=yuva444p10le"
    assert ff.kw["stdin"] == overlay.subprocess.PIPE and ff.stdin_closed
    assert cmd[-1] != str(out)                      # ffmpeg writes a temporary name …
    assert out.read_bytes() == b"mov"               # … renamed into place on success
    assert not Path(cmd[-1]).exists()


def test_panel_jpeg_is_converted_from_bt601_full_range(world, tmp_path) -> None:
    _render(world, tmp_path, mode="panel")
    cmd = world.ffmpeg[0].cmd
    assert cmd[cmd.index("-c:v") + 1] == "mjpeg"
    assert cmd[cmd.index("-vf") + 1].startswith("scale=in_color_matrix=bt601:in_range=pc")


def test_failed_encode_leaves_no_cached_overlay(world, tmp_path) -> None:
    html = tmp_path / "card.html"
    html.write_text(PAGE)
    world.ffmpeg_fails = True
    with pytest.raises(overlay.subprocess.CalledProcessError):
        overlay.render_overlay(html, 640, 360, 30, 0.1, tmp_path / "cache",
                               launch=world.launch, popen=world.popen, log=lambda s: None)
    out_dir = tmp_path / "cache" / "overlays" / overlay.content_hash(PAGE, 640, 360, 30, 0.1)
    assert not list(out_dir.glob("*.mov"))
    # the next render encodes again instead of reusing a half-written file
    world.ffmpeg_fails = False
    out = overlay.render_overlay(html, 640, 360, 30, 0.1, tmp_path / "cache",
                                 launch=world.launch, popen=world.popen, log=lambda s: None)
    assert out.read_bytes() == b"mov" and len(world.ffmpeg) == 2


def test_ffmpeg_exiting_early_is_an_encode_failure(world, tmp_path) -> None:
    class Broken(FakeStdin):
        def write(self, data):
            raise BrokenPipeError

    def popen(cmd, **kw):
        p = FakeFfmpeg(world, cmd, **kw)
        p.stdin = Broken(p)
        world.ffmpeg_fails = True
        return p
    with pytest.raises(overlay.subprocess.CalledProcessError):
        overlay.render_to(PAGE, 640, 360, 30, 0.1, "alpha", tmp_path / "o.mov",
                          launch=world.launch, popen=popen, workers=1, log=lambda s: None)
    assert not list(tmp_path.glob("*.mov"))


# --- EDL overlays ------------------------------------------------------------------------

def _edl(overlays: list[dict], **out) -> EDL:
    return EDL.from_dict({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 20}],
                          "output": {"width": 1080, "height": 1920, **out}, "overlays": overlays})


def _ov(**kw) -> dict:
    return {"html": "/o/card.html", "style": "clean-tech", "start": 2, "end": 6, **kw}


def test_overlays_parse() -> None:
    e = _edl([_ov(mode="panel", region={"x": 90, "y": 520, "w": 900, "h": 560}),
              _ov(start=7, end=9, key_color="#0000FF")])
    assert e.overlays[0] == Overlay("/o/card.html", 2, 6, "panel", Region(90, 520, 900, 560), "#00FF00", "clean-tech")
    assert e.overlays[1].rect(1080, 1920) == Region(0, 0, 1080, 1920)
    assert e.validate() == []


@pytest.mark.parametrize("ov,match", [
    (_ov(start=15, end=21), "within the video"),
    (_ov(start=-1, end=3), "within the video"),
    (_ov(start=5, end=5), "within the video"),
    (_ov(mode="floating"), "mode"),
    (_ov(region={"x": 0, "y": 1800, "w": 1080, "h": 200}), "not inside"),
    (_ov(region={"x": 0, "y": 0, "w": 1081, "h": 200}), "not inside"),
    (_ov(region={"x": 0, "y": 0, "w": 501, "h": 200}), "even"),
    (_ov(key_color="green"), "key_color"),
])
def test_invalid_overlays_are_rejected(ov, match) -> None:
    with pytest.raises(ValueError, match=match):
        _edl([ov]).validate()


def test_overlays_overlapping_in_time_and_place_are_rejected() -> None:
    a = _ov(region={"x": 0, "y": 0, "w": 1080, "h": 600})
    b = _ov(start=5, end=9, region={"x": 0, "y": 500, "w": 1080, "h": 600})
    with pytest.raises(ValueError, match="overlays 0 and 1 cover the same part of the frame.*5.00–6.00s"):
        _edl([a, b]).validate()


def test_overlays_in_separate_regions_or_times_are_fine() -> None:
    top = _ov(region={"x": 0, "y": 0, "w": 1080, "h": 600})
    bottom = _ov(region={"x": 0, "y": 1400, "w": 1080, "h": 300})
    later = _ov(start=6, end=9, region={"x": 0, "y": 0, "w": 1080, "h": 600})
    assert _edl([top, bottom, later]).validate() == []


def test_overlay_end_uses_the_rendered_duration_with_title_and_crossfades() -> None:
    e = EDL.from_dict({"source": "/rec/x.mp4", "title": {"line1": "T", "duration": 3},
                       "segments": [{"src_start": 0, "src_end": 10}, {"src_start": 20, "src_end": 30}],
                       "overlays": [_ov(start=0, end=22.0)]})
    # 3 s title + 10 + 10, minus a 0.5 s crossfade at each of the two joins
    assert e.output_duration() == pytest.approx(22.0)
    e.validate()
    e.overlays[0].end = 22.1
    with pytest.raises(ValueError, match="within the video"):
        e.validate()


def test_overlay_without_style_warns() -> None:
    o = _ov()
    del o["style"]
    assert any("names no style" in w for w in _edl([o]).validate())


# --- filter graph --------------------------------------------------------------------------

def test_panel_and_keyed_filter_strings() -> None:
    e = _edl([_ov(start=10, end=17, mode="panel", region={"x": 90, "y": 520, "w": 900, "h": 560}),
              _ov(start=11, end=16, mode="keyed", region={"x": 0, "y": 1500, "w": 1080, "h": 300})])
    graph, label = composite.overlay_graph(e.overlays, [Path("p.mov"), Path("k.mov")], 1, "vsub0", 1080, 1920)
    assert graph == (
        "[1:v]setpts=PTS-STARTPTS+10/TB[ov0]; "
        "[vsub0][ov0]overlay=90:520:enable='between(t,10,17)':eof_action=pass[vov0]; "
        "[2:v]setpts=PTS-STARTPTS+11/TB,colorkey=0x00FF00:0.3:0.08,despill=type=green[ov1]; "
        "[vov0][ov1]overlay=0:1500:enable='between(t,11,16)':eof_action=pass[vov1]")
    assert label == "vov1"


@pytest.mark.parametrize("key,spill", [("#00FF00", "green"), ("#0047BB", "blue"), ("#FF00FF", None)])
def test_despill_follows_the_key_colour(key, spill) -> None:
    assert composite._despill_type(key) == spill
    e = _edl([_ov(key_color=key)])
    graph, _ = composite.overlay_graph(e.overlays, [Path("k.mov")], 1, "v", 1080, 1920)
    assert (f"despill=type={spill}" in graph) if spill else ("despill" not in graph)


class _Done:
    returncode = 0


def test_render_adds_overlay_inputs_after_the_segments(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(composite.subprocess, "run", lambda cmd, *a, **k: calls.append([str(c) for c in cmd]) or _Done())
    monkeypatch.setattr(composite, "_render_overlays", lambda edl: [Path("/c/p.mov"), Path("/c/k.mov")])
    monkeypatch.setattr(composite, "lint_overlays", lambda edl: None)    # the pages do not exist
    e = EDL.from_dict({"source": "/rec/x.mp4",
                       "segments": [{"src_start": 0, "src_end": 10}, {"src_start": 20, "src_end": 30}],
                       "overlays": [_ov(mode="panel", region={"x": 0, "y": 0, "w": 640, "h": 360}),
                                    _ov(start=7, end=9)]})
    composite.render(e, Path("<OUT>/o.mp4"), work_dir=tmp_path)
    final = calls[-1]
    inputs = [final[i + 1] for i, a in enumerate(final) if a == "-i"]
    assert inputs[-2:] == ["/c/p.mov", "/c/k.mov"]
    fc = final[final.index("-filter_complex") + 1]
    assert "[2:v]setpts=PTS-STARTPTS+2/TB[ov0]" in fc and "[3:v]setpts=PTS-STARTPTS+7/TB,colorkey" in fc
    assert final[final.index("-map") + 1] == "[vov1]"


def test_alpha_overlays_are_laid_over_without_a_key() -> None:
    e = _edl([_ov(mode="alpha", region={"x": 0, "y": 1500, "w": 1080, "h": 300})])
    assert e.validate() == []
    graph, _ = composite.overlay_graph(e.overlays, [Path("a.mov")], 1, "v", 1080, 1920)
    assert graph == ("[1:v]setpts=PTS-STARTPTS+2/TB[ov0]; "
                     "[v][ov0]overlay=0:1500:enable='between(t,2,6)':eof_action=pass[vov0]")


def test_an_overlay_without_a_mode_is_keyed() -> None:
    assert _edl([_ov()]).overlays[0].mode == "keyed"


def test_mode_error_names_all_three_modes() -> None:
    with pytest.raises(ValueError, match="alpha, panel, keyed"):
        _edl([_ov(mode="floating")]).validate()


def test_each_overlay_renders_in_its_own_mode(monkeypatch) -> None:
    seen = []
    from stages import prep
    monkeypatch.setattr(prep, "cache_dir_for", lambda source: Path("/cache"))
    monkeypatch.setattr(overlay, "render_overlay",
                        lambda html, w, h, fps, d, cache, mode: seen.append((Path(html).name, w, h, mode)) or Path("x"))
    e = _edl([_ov(mode="alpha", region={"x": 0, "y": 0, "w": 640, "h": 360}), _ov(start=7, end=9)])
    composite._render_overlays(e)
    assert seen == [("card.html", 640, 360, "alpha"), ("card.html", 1080, 1920, "keyed")]
