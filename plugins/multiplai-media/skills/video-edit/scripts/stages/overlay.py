"""Motion graphics: render an HTML/CSS animation to a ProRes 4444 clip.

The page is loaded into headless Chromium, which this module launches itself
through Playwright. For every output frame the page is seeked to that time
and captured, and the captures are piped straight into one ffmpeg process.

How a page is driven:

    launch Chromium (LAUNCH_ARGS), open a page at the overlay's size
    add_init_script(static/seek-shim.js)     before any page script runs
    goto(PAGE_URL), answered with the page; every other request is aborted
    document.fonts.ready, then __veReady()
    alpha mode only: transparent default background, html/body transparent
    per frame, over one CDP session:
        Runtime.evaluate    __veFrame(t)
        Page.captureScreenshot

`__veFrame(t)` sets the page's virtual clock (Date, performance.now and
requestAnimationFrame) and seeks CSS, Web Animations, GSAP and Lottie to t,
which makes two captures at the same t byte-identical. A page may define its
own `window.__seek(t)`; that is called instead of the default seek.

Modes (the EDL overlay's `mode`):
  - `alpha`: PNG captures with real transparency (optimizeForSpeed off, since
    the fast path rounds alpha to 0 or 255). Soft shadows and anti-aliased
    edges survive over the video.
  - `panel`: opaque JPEG captures (quality 90), the fastest.
  - `keyed`: opaque PNG captures on the page's key colour, which the composite
    removes (colorkey + despill). PNG, not JPEG, because JPEG's halved colour
    resolution smears the key colour into the graphic's edges.
Every mode is encoded to BT.709 limited range, the range the video uses.

Several pages render at once: page k of N takes frames k, k+N, …; a reorder
buffer hands the frames to ffmpeg in order. No frame is written to disk.

Browser handling, adapted from HyperFrames (heygen-com/hyperframes @ bb9e507,
Apache-2.0): launch flags from engine/src/services/browserManager.ts:932-978,
transparent capture from engine/src/services/screenshotService.ts:217-222,288-310,
frame interleaving from engine/src/services/parallelCoordinator.ts:422-474,
colour conversion from engine/src/utils/sdrCaptureColor.ts.
"""
from __future__ import annotations

import base64
import hashlib
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

# The estimate printed before a render. Measured for a 1080x1920 alpha stat
# card on 10 cores: 0.044 s/frame with one page, 0.025 s with four. Pages do
# not scale linearly (they share ffmpeg and the CPU), so the estimate divides
# by the square root of the page count.
SEC_PER_FRAME = 0.05
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SEEK_SHIM = (SCRIPTS_DIR / "static" / "seek-shim.js").read_text(encoding="utf-8")
TRANSPARENT_CSS = ("html,body{background:transparent!important;"
                   "background-color:transparent!important;background-image:none!important}")
LAUNCH_ARGS = [
    "--font-render-hinting=none",
    "--force-color-profile=srgb",
    "--force-device-scale-factor=1",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]
JPEG_QUALITY = 90
# Chrome's JPEG is BT.601 full range; PNG is RGB. Both become BT.709 limited.
TO_BT709 = "scale=out_color_matrix=bt709:out_range=tv:flags=neighbor"
JPEG_TO_BT709 = f"scale=in_color_matrix=bt601:in_range=pc:flags=neighbor,format=gbrp,{TO_BT709}"
PAGE_URL = "http://overlay.invalid/"   # never resolved: the route below answers it
WORKERS_ENV = "VIDEO_EDIT_OVERLAY_WORKERS"
AHEAD_PER_WORKER = 4          # frames a page may run ahead of the encoder

Runner = Callable[..., Any]


class OverlayError(RuntimeError):
    pass


class Page(Protocol):
    """The part of a Playwright page this module uses."""
    context: Any

    def add_init_script(self, script: str) -> None: ...
    def route(self, url: str, handler: Callable[[Any], None]) -> None: ...
    def goto(self, url: str) -> Any: ...
    def evaluate(self, expression: str) -> Any: ...
    def add_style_tag(self, content: str) -> Any: ...


class Browser(Protocol):
    def new_page(self, width: int, height: int) -> Page: ...
    def close(self) -> None: ...


def _install_hint() -> str:
    return (f"uv run --project {SCRIPTS_DIR} playwright install chromium-headless-shell")


class _Chromium:
    """A Chromium this process launched, and the Playwright driver behind it."""

    def __init__(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise OverlayError(
                "Motion graphics render in headless Chromium through Playwright, and the "
                "playwright package is not importable here. Run the pipeline through its "
                f"project, which declares it: uv run --project {SCRIPTS_DIR} python3 "
                f"{SCRIPTS_DIR / 'pipeline.py'} …") from None
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
        except Exception as e:
            self._pw.stop()
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            raise OverlayError(
                f"Playwright could not start Chromium ({first}). Install the browser it "
                f"expects with: {_install_hint()}") from None

    def new_page(self, width: int, height: int) -> Page:
        return self._browser.new_page(viewport={"width": width, "height": height},
                                      device_scale_factor=1)

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def content_hash(html: str, width: int, height: int, fps: int, duration: float,
                 mode: str = "keyed") -> str:
    h = hashlib.sha256()
    h.update(html.encode("utf-8"))
    h.update(f"|{width}x{height}@{fps}|{duration:.3f}|{mode}".encode())
    return h.hexdigest()[:16]


def frame_count(duration: float, fps: int) -> int:
    return max(1, math.ceil(round(duration * fps, 6)))


def default_workers() -> int:
    env = os.environ.get(WORKERS_ENV, "").strip()
    if env:
        try:
            n = int(env)
        except ValueError:
            raise OverlayError(f"{WORKERS_ENV}={env!r} is not a whole number.") from None
        if n < 1:
            raise OverlayError(f"{WORKERS_ENV}={env!r} must be 1 or more.")
        return n
    return max(1, min((os.cpu_count() or 1) - 2, 4))


def capture_params(mode: str) -> dict:
    if mode == "alpha":
        return {"format": "png", "optimizeForSpeed": False}
    if mode == "panel":
        return {"format": "jpeg", "quality": JPEG_QUALITY, "optimizeForSpeed": True}
    return {"format": "png", "optimizeForSpeed": True}


class FrameSource:
    """One page with the overlay loaded, seeked and captured over CDP."""

    def __init__(self, page: Page, html: str, mode: str):
        def serve(route: Any) -> None:
            # The page gets no network: only its own URL is answered.
            if route.request.url == PAGE_URL:
                route.fulfill(body=html, content_type="text/html; charset=utf-8")
            else:
                route.abort()

        page.add_init_script(SEEK_SHIM)
        page.route("**/*", serve)
        page.goto(PAGE_URL)
        seek = page.evaluate("document.fonts.ready.then(() => window.__veReady())")
        if seek not in ("page", "default"):
            raise OverlayError(f"the overlay page did not load the seek shim: {seek!r}")
        self.cdp = page.context.new_cdp_session(page)
        if mode == "alpha":
            self.cdp.send("Emulation.setDefaultBackgroundColorOverride",
                          {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
            page.add_style_tag(content=TRANSPARENT_CSS)
        self.params = capture_params(mode)

    def frame(self, t: float) -> bytes:
        r = self.cdp.send("Runtime.evaluate", {"expression": f"__veFrame({t:.6f})",
                                               "returnByValue": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text") or d
            raise OverlayError(f"the overlay page threw while seeking to {t:.3f}s: {msg}")
        shot = self.cdp.send("Page.captureScreenshot", self.params)
        return base64.b64decode(shot["data"])


class _Stopped(Exception):
    pass


class _InOrder:
    """Frames arrive from several pages in any order; take() returns them in order."""

    def __init__(self, ahead: int):
        self.cv = threading.Condition()
        self.frames: dict[int, bytes] = {}
        self.next = 0
        self.ahead = ahead
        self.error: BaseException | None = None
        self.stopped = False

    def put(self, i: int, data: bytes) -> None:
        with self.cv:
            while not self.stopped and i - self.next >= self.ahead:
                self.cv.wait()
            if self.stopped:
                raise _Stopped
            self.frames[i] = data
            self.cv.notify_all()

    def fail(self, e: BaseException) -> None:
        with self.cv:
            self.error = self.error or e
            self.stopped = True
            self.cv.notify_all()

    def stop(self) -> None:
        with self.cv:
            self.stopped = True
            self.cv.notify_all()

    def take(self) -> bytes:
        with self.cv:
            while self.next not in self.frames and self.error is None:
                self.cv.wait()
            if self.error is not None:
                raise self.error
            data = self.frames.pop(self.next)
            self.next += 1
            self.cv.notify_all()
            return data


def _worker(k: int, workers: int, n: int, fps: int, html: str, width: int, height: int,
            mode: str, launch: Callable[[], Browser], sink: _InOrder) -> None:
    browser = None
    try:
        browser = launch()
        source = FrameSource(browser.new_page(width, height), html, mode)
        for i in range(k, n, workers):
            sink.put(i, source.frame(i / fps))
    except _Stopped:
        pass
    except BaseException as e:
        sink.fail(e)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def encode_command(mode: str, fps: int, out: Path) -> list[str]:
    codec = "mjpeg" if mode == "panel" else "png"
    convert = JPEG_TO_BT709 if mode == "panel" else TO_BT709
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-framerate", str(fps), "-c:v", codec, "-i", "-",
            "-vf", f"{convert},format=yuva444p10le",
            "-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0",
            "-pix_fmt", "yuva444p10le",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-color_range", "tv", str(out)]


def render_to(html: str, width: int, height: int, fps: int, duration: float, mode: str,
              out: Path, launch: Callable[[], Browser] = _Chromium,
              popen: Runner = subprocess.Popen, workers: int | None = None,
              log: Callable[[str], None] = print) -> int:
    """Capture every frame and encode them to `out`; returns the frame count.

    ffmpeg writes a temporary name that is renamed to `out` only once it
    succeeds, so a failed or interrupted render never leaves a file at `out`
    that render_overlay would take for a cached overlay.
    """
    n = frame_count(duration, fps)
    workers = max(1, min(workers or default_workers(), n))
    log(f"→ overlay: {n} frames at {width}x{height}, {workers} page(s), "
        f"about {n * SEC_PER_FRAME / math.sqrt(workers):.0f}s")
    partial = out.with_name(f"{out.stem}.partial{out.suffix}")
    partial.unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = encode_command(mode, fps, partial)
    proc = popen(cmd, stdin=subprocess.PIPE)
    sink = _InOrder(AHEAD_PER_WORKER * workers)
    threads = [threading.Thread(target=_worker, daemon=True,
                                args=(k, workers, n, fps, html, width, height, mode, launch, sink))
               for k in range(workers)]
    try:
        for t in threads:
            t.start()
        try:
            for _ in range(n):
                frame = sink.take()
                try:
                    proc.stdin.write(frame)
                except BrokenPipeError:     # ffmpeg exited; its own message is on stderr
                    raise subprocess.CalledProcessError(proc.wait(), cmd) from None
        finally:
            sink.stop()
            for t in threads:
                t.join()
        proc.stdin.close()
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, out)
    return n


def render_overlay(html_path: Path, width: int, height: int, fps: int, duration: float,
                   cache_root: Path, mode: str = "keyed",
                   launch: Callable[[], Browser] = _Chromium, popen: Runner = subprocess.Popen,
                   log: Callable[[str], None] = print) -> Path:
    """The overlay as ProRes 4444, cached by content hash under cache_root/overlays/."""
    html = Path(html_path).read_text(encoding="utf-8")
    key = content_hash(html, width, height, fps, duration, mode)
    out = cache_root / "overlays" / key / "overlay.mov"
    if out.exists():
        log(f"→ overlay {Path(html_path).name}: cached ({key})")
        return out
    t0 = time.monotonic()
    render_to(html, width, height, fps, duration, mode, out, launch, popen, log=log)
    took = time.monotonic() - t0
    log(f"✓ overlay {Path(html_path).name}: {took:.1f}s for {duration:.1f}s "
        f"({took / max(duration, 1e-6):.1f} render-s per overlay-s)")
    return out
