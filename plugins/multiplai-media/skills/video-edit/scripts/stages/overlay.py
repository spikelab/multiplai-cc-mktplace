"""Motion graphics: render an HTML/CSS animation to a ProRes 4444 clip.

The page is loaded into a browser, its animations are paused, and for every
output frame the page is seeked to that time and screenshotted. The PNG
sequence is then encoded once.

How a page is driven (each step is one browser command):

    set viewport W H
    open about:blank
    eval --stdin   document.write(atob('<base64 of the page>'))
    eval           document.fonts.ready
    per frame:     eval __seek(t)   then   screenshot <frames>/f_00001.png
    close

`__seek(t)` pauses every animation in `document.getAnimations()` and sets its
`currentTime` to t seconds, which makes two screenshots at the same t
byte-identical. A page may define its own `window.__seek(t)`; otherwise this
default is installed. Only CSS animations and the Web Animations API are
seekable: anything driven by timers or requestAnimationFrame is not.

Pages go in with `document.write`, not `open file://…` (a host browser may
refuse file URLs), and through `eval --stdin`, because Linux caps one
command-line argument at 128 KB and a page with inline images is larger.

Screenshots are opaque. For a graphic over the video, render it on a solid
key colour and composite it with `mode: "keyed"` (colorkey + despill).

Which browser command:
  - inside a container: `ab --session video-edit-<pid>` (the host browser,
    reached over the bridge; it writes screenshots on the host, so the frames
    directory must be under the shared workspace);
  - on a Mac: `agent-browser --session video-edit-<pid>`;
  - anywhere else: an error naming what to install.
It never attaches to a running browser (no `connect`, no `--profile`): the
user's logged-in browser is never touched.
"""
from __future__ import annotations

import base64
import hashlib
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SEC_PER_FRAME = 0.32          # estimate printed before a render; measured 0.17 s at 1080x1920
DENIED_HOST_BROWSER = "DENIED: host browser is not enabled"
ENABLE_HOST_BROWSER = ("mkdir -p ~/.local/state/multiplai && "
                       "touch ~/.local/state/multiplai/host-browser-enabled")
FORBIDDEN_ARGS = ("connect", "--profile", "--cdp", "hb-connect.sh")

DEFAULT_SEEK = """
if (typeof window.__seek !== 'function') {
  window.__seek = function (t) {
    for (const a of document.getAnimations()) { a.pause(); a.currentTime = t * 1000; }
    return document.getAnimations().length;
  };
}
document.fonts.ready.then(() => typeof window.__seek)
""".strip()

Runner = Callable[..., subprocess.CompletedProcess]


class OverlayError(RuntimeError):
    pass


def _in_container() -> bool:
    flag = os.environ.get("MULTIPLAI_CONTAINER", "")
    if flag in ("0", "1"):
        return flag == "1"
    return Path("/.dockerenv").exists()


@dataclass
class Backend:
    name: str               # "ab" or "agent-browser"
    argv: list[str]         # command plus --session …
    shared_root: Path | None  # screenshots must be written under this path (host browser)


def choose_backend(pid: int | None = None) -> Backend:
    session = f"video-edit-{pid or os.getpid()}"
    if _in_container():
        ab = shutil.which("ab")
        if not ab:
            raise OverlayError(
                "Motion graphics need a browser, and no `ab` command is on PATH to reach one. "
                "They render through agent-browser on macOS: run the render on a Mac with "
                "agent-browser installed (`npm i -g agent-browser`).")
        ws = os.environ.get("WORKSPACE")
        return Backend("ab", [ab, "--session", session], Path(ws).resolve() if ws else None)
    if sys.platform == "darwin":
        agent = shutil.which("agent-browser")
        if not agent:
            raise OverlayError(
                "Motion graphics need agent-browser to drive a headless browser: "
                "install it with `npm i -g agent-browser`, then retry.")
        return Backend("agent-browser", [agent, "--session", session], None)
    raise OverlayError(
        "Motion graphics render through agent-browser on macOS (install: "
        f"`npm i -g agent-browser`); this machine ({sys.platform}) is not supported. "
        "Render the overlays on a Mac.")


def content_hash(html: str, width: int, height: int, fps: int, duration: float) -> str:
    h = hashlib.sha256()
    h.update(html.encode("utf-8"))
    h.update(f"|{width}x{height}@{fps}|{duration:.3f}".encode())
    return h.hexdigest()[:16]


def frame_count(duration: float, fps: int) -> int:
    return max(1, math.ceil(round(duration * fps, 6)))


class Browser:
    """One browser session; every call checks for a refusal."""

    def __init__(self, backend: Backend, run: Runner = subprocess.run):
        self.backend = backend
        self.run = run

    def __call__(self, *args: str, stdin: str | None = None) -> str:
        argv = [*self.backend.argv, *args]
        bad = [a for a in args if a in FORBIDDEN_ARGS]
        if bad:
            raise OverlayError(f"refusing browser command {bad[0]!r}: overlays never attach to a running browser")
        p = self.run(argv, input=stdin, capture_output=True, text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.splitlines():
            if line.startswith(DENIED_HOST_BROWSER):
                raise OverlayError(f"{line.strip()}\nEnable it on the Mac with:\n  {ENABLE_HOST_BROWSER}")
            if line.startswith("DENIED:"):
                raise OverlayError(line.strip())
        if p.returncode != 0 or "✗" in out:
            raise OverlayError(f"browser command {' '.join(args[:2])} failed: {out.strip()[:500]}")
        return out


def _load_page(browser: Browser, html: str, width: int, height: int) -> None:
    browser("set", "viewport", str(width), str(height))
    browser("open", "about:blank")
    page = base64.b64encode(html.encode("utf-8")).decode("ascii")
    # atob alone gives Latin-1; decoding its bytes as UTF-8 keeps non-ASCII text.
    browser("eval", "--stdin",
            stdin=("document.open();document.write(new TextDecoder().decode("
                   f"Uint8Array.from(atob('{page}'), c => c.charCodeAt(0))));document.close();'loaded'"))
    out = browser("eval", "--stdin", stdin=DEFAULT_SEEK)
    if "function" not in out:
        raise OverlayError(f"the overlay page has no seekable __seek(t): {out.strip()[:200]}")


def render_frames(html: str, width: int, height: int, fps: int, duration: float,
                  frames_dir: Path, backend: Backend | None = None,
                  run: Runner = subprocess.run, log: Callable[[str], None] = print) -> int:
    """Write f_00001.png … for every frame; returns the count."""
    backend = backend or choose_backend()
    frames_dir = frames_dir.resolve()
    if backend.shared_root and not frames_dir.is_relative_to(backend.shared_root):
        raise OverlayError(f"frames directory {frames_dir} is outside the shared workspace "
                           f"{backend.shared_root}: the host browser cannot write there")
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = frame_count(duration, fps)
    log(f"→ overlay: {n} frames at {width}x{height}, about {n * SEC_PER_FRAME:.0f}s")
    browser = Browser(backend, run)
    try:
        _load_page(browser, html, width, height)
        for i in range(n):
            browser("eval", f"__seek({i / fps:.6f})")
            browser("screenshot", str(frames_dir / f"f_{i + 1:05d}.png"))
    finally:
        try:
            browser("close")
        except OverlayError:
            pass
    return n


def encode_prores(frames_dir: Path, fps: int, out: Path, run: Runner = subprocess.run) -> Path:
    """Encode to a temporary name and rename it to `out` only once ffmpeg
    succeeds, so a failed or interrupted encode never leaves a file at `out`
    that render_overlay would take for a cached overlay."""
    partial = out.with_name(f"{out.stem}.partial{out.suffix}")
    partial.unlink(missing_ok=True)
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-framerate", str(fps), "-i", str(frames_dir / "f_%05d.png"),
             "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
             str(partial)], check=True)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, out)
    return out


def render_overlay(html_path: Path, width: int, height: int, fps: int, duration: float,
                   cache_root: Path, backend: Backend | None = None,
                   run: Runner = subprocess.run, log: Callable[[str], None] = print) -> Path:
    """The overlay as ProRes 4444, cached by content hash under cache_root/overlays/."""
    html = Path(html_path).read_text(encoding="utf-8")
    key = content_hash(html, width, height, fps, duration)
    out_dir = cache_root / "overlays" / key
    out = out_dir / "overlay.mov"
    if out.exists():
        log(f"→ overlay {Path(html_path).name}: cached ({key})")
        return out
    t0 = time.monotonic()
    render_frames(html, width, height, fps, duration, out_dir / "frames", backend, run, log)
    encode_prores(out_dir / "frames", fps, out, run)
    took = time.monotonic() - t0
    log(f"✓ overlay {Path(html_path).name}: {took:.1f}s for {duration:.1f}s "
        f"({took / max(duration, 1e-6):.1f} render-s per overlay-s)")
    return out
