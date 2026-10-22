"""Proof shots: a screenshot of a real web page, and where one line of it is.

`pipeline.py capture <url>` loads the page in headless Chromium (the
launcher overlays use, stages/overlay.py), waits for the network to go
quiet, and screenshots the viewport or, with --full-page, the whole page.
With --highlight it finds the first visible element whose text contains the
given words and records that element's box in page pixels. The page itself
is never changed: no DOM edits, no injected styles, no text replaced. The
highlight is drawn later, on top, by the card page (stages/proof_card.py).

Unlike an overlay, the page here is given the network: it is a real page
fetched from the web, with whatever it loads.

Writes, to the output directory:
  shot.png   the screenshot
  shot.json  {url, captured_at (UTC), width, height, source,
              highlight: {text, x, y, w, h} | null}

Exit codes (the CLI's, carried on CaptureError.code):
  0  captured
  1  capture could not run (no Playwright or Chromium, bad arguments)
  2  the page could not be captured as it is: an HTTP error, a login wall,
     a captcha, or no answer at all. The fix is always the same: save a
     screenshot by hand and pass it with --from-file.
  3  captured, but the highlight text was not found in the screenshot; the
     shot is written with `highlight: null`.

Text read from the page (the matched element's text) is untrusted: the CLI
prints it inside an untrusted-content fence (docs/untrusted-content.md) and
never writes it to shot.json.
"""
from __future__ import annotations

import json
import re
import struct
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_WIDTH = 1280                 # viewport, CSS pixels (= image pixels at scale 1)
DEFAULT_HEIGHT = 800
LOAD_TIMEOUT_S = 20.0                # for the first answer, and again for network idle
MATCH_PRINT_LIMIT = 500              # characters of matched page text shown in the fence
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_NO_MATCH = 3
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FROM_FILE_FIX = ("save a screenshot of the page yourself and pass it with "
                 "`--from-file shot.png` (give the line to highlight as `--box X,Y,W,H`, "
                 "in the screenshot's pixels)")
# A final URL path that is a sign-in page: /login, /signin, /i/flow/login, …
LOGIN_PATH_RE = re.compile(r"(?:^|/)(?:log-?in|sign-?in|sign_in|signup|sign-up|"
                           r"auth|i/flow/login|accounts/login)(?:/|$)", re.IGNORECASE)

# Read-only look at the loaded page for the walls a screenshot would show
# instead of the content: a visible password field, or a captcha widget or
# challenge page. Nothing on the page is changed.
WALL_PROBE = """() => {
  const shown = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const password = [...document.querySelectorAll('input[type=password]')].some(shown);
  const frame = [...document.querySelectorAll('iframe')].some(f =>
      /recaptcha|hcaptcha|challenges\\.cloudflare\\.com|turnstile|arkoselabs|funcaptcha/i.test(f.src || ''));
  const widget = !!document.querySelector(
      '.g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form, #cf-challenge-running, [data-sitekey]');
  const title = /^\\s*(just a moment|attention required|verify you are human)/i.test(document.title || '');
  return {password, captcha: frame || widget || title, path: location.pathname};
}"""
SCROLL_PROBE = "() => [window.scrollX, window.scrollY]"


class CaptureError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    def inside(self, width: int, height: int) -> bool:
        return (self.x >= 0 and self.y >= 0 and self.w > 0 and self.h > 0
                and self.x + self.w <= width and self.y + self.h <= height)


@dataclass
class Shot:
    dir: Path
    meta: dict
    matched: str | None = None       # untrusted: the matched element's text, for the fence
    warnings: list[str] | None = None


def parse_box(text: str) -> Box:
    """`X,Y,W,H` in pixels."""
    parts = [p.strip() for p in text.split(",")]
    try:
        x, y, w, h = (int(round(float(p))) for p in parts)
    except ValueError:
        raise CaptureError(f"--box {text!r}: give four numbers, X,Y,W,H in pixels") from None
    if w <= 0 or h <= 0:
        raise CaptureError(f"--box {text!r}: the width and height must be more than 0")
    return Box(x, y, w, h)


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != PNG_MAGIC or data[12:16] != b"IHDR":
        raise ValueError("not a PNG file")
    return struct.unpack(">II", data[16:24])


def domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_line(e: BaseException) -> str:
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


def _write(out_dir: Path, png: bytes, url: str, captured_at: str, source: str,
           highlight: dict | None) -> dict:
    w, h = png_size(png)
    meta = {"url": url, "captured_at": captured_at, "width": w, "height": h,
            "source": source, "highlight": highlight}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "shot.png").write_bytes(png)
    (out_dir / "shot.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def _launch() -> Any:
    """The overlay launcher, with messages that name capture's own fix."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        from stages.overlay import SCRIPTS_DIR
        raise CaptureError(
            "capture loads the page in headless Chromium through Playwright, and the playwright "
            "package is not importable here. Run the pipeline through its project, which declares "
            f"it: uv run --project {SCRIPTS_DIR} python3 {SCRIPTS_DIR / 'pipeline.py'} capture …; "
            f"or {FROM_FILE_FIX}.") from None
    from stages import overlay
    try:
        return overlay._Chromium()
    except overlay.OverlayError as e:
        raise CaptureError(f"{e} Or {FROM_FILE_FIX}.") from None


def _find(page: Any, text: str) -> tuple[Box | None, str | None]:
    """The first visible element containing `text`, its box in page pixels and its text."""
    found = page.get_by_text(text, exact=False).filter(visible=True)
    if found.count() == 0:
        return None, None
    el = found.first
    bb = el.bounding_box()
    if not bb:
        return None, None
    sx, sy = page.evaluate(SCROLL_PROBE)
    box = Box(round(bb["x"] + sx), round(bb["y"] + sy), round(bb["width"]), round(bb["height"]))
    return box, el.inner_text()


def capture(url: str, out_dir: Path, highlight: str | None = None,
            width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, full_page: bool = False,
            launch: Callable[[], Any] | None = None, now: Callable[[], str] = _now) -> Shot:
    """Screenshot `url` and, with `highlight`, record where that text is.

    Raises CaptureError with code 2 when the page answers with an HTTP error,
    shows a login wall or a captcha, or does not answer; nothing is written
    then. When the highlight text is not in the screenshot the shot is still
    written, and the Shot carries a warning; the CLI exits 3.
    """
    warnings: list[str] = []
    browser = (launch or _launch)()
    try:
        page = browser.new_page(width, height)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_S * 1000)
        except Exception as e:
            raise CaptureError(f"{url} did not load ({_first_line(e)}). Check the address; if it opens "
                               f"in your own browser, {FROM_FILE_FIX}.", EXIT_REFUSED) from None
        status = getattr(response, "status", None)
        if status is not None and status >= 400:
            reason = getattr(response, "status_text", "") or ""
            raise CaptureError(f"{url} answered HTTP {status}{' ' + reason if reason else ''}, so there "
                               f"is no page to show. If the page opens in your own browser, "
                               f"{FROM_FILE_FIX}.", EXIT_REFUSED)
        try:
            page.wait_for_load_state("networkidle", timeout=LOAD_TIMEOUT_S * 1000)
        except Exception:
            warnings.append(f"the page was still loading after {LOAD_TIMEOUT_S:.0f} s; "
                            "it was captured as it was then")
        final = getattr(page, "url", None) or url
        wall = page.evaluate(WALL_PROBE) or {}
        if wall.get("captcha"):
            raise CaptureError(f"{url} showed a captcha or a bot check instead of the page. "
                               f"{FROM_FILE_FIX[0].upper()}{FROM_FILE_FIX[1:]}.", EXIT_REFUSED)
        if wall.get("password") or LOGIN_PATH_RE.search(wall.get("path") or urlparse(final).path):
            what = "a visible password field" if wall.get("password") else f"a sign-in address ({final})"
            raise CaptureError(f"{url} showed a login wall ({what}) instead of the page; capture "
                               f"never signs in. {FROM_FILE_FIX[0].upper()}{FROM_FILE_FIX[1:]}.",
                               EXIT_REFUSED)
        box, matched = (_find(page, highlight) if highlight else (None, None))
        png = page.screenshot(full_page=full_page, type="png")
    finally:
        browser.close()

    w, h = png_size(png)
    if highlight and box is None:
        warnings.append(f"no visible element on the page contains {highlight!r}; the shot is "
                        "saved without a highlight")
    elif box is not None and not box.inside(w, h):
        hint = "" if full_page else "; re-run with --full-page to capture the whole page"
        warnings.append(f"{highlight!r} is at x={box.x}, y={box.y}, outside the {w}x{h} "
                        f"screenshot{hint}")
        box = None
    hl = {"text": highlight, **asdict(box)} if box else None
    if final.rstrip("/") != url.rstrip("/"):
        warnings.append(f"the address redirected to {final}; shot.json records that address")
    meta = _write(out_dir, png, final, now(), "browser", hl)
    return Shot(out_dir, meta, matched if box else None, warnings)


def from_file(url: str, png_path: Path, out_dir: Path, box: Box | None = None,
              label: str | None = None) -> Shot:
    """A screenshot the user saved, with the highlight given as a box."""
    try:
        png = Path(png_path).read_bytes()
    except OSError as e:
        raise CaptureError(f"cannot read {png_path}: {e.strerror or e}") from None
    try:
        w, h = png_size(png)
    except ValueError:
        raise CaptureError(f"{png_path} is not a PNG file; save the screenshot as PNG") from None
    if box is not None and not box.inside(w, h):
        raise CaptureError(f"--box {box.x},{box.y},{box.w},{box.h} is not inside the {w}x{h} "
                           f"screenshot {png_path}")
    taken = datetime.fromtimestamp(Path(png_path).stat().st_mtime, UTC)
    hl = {"text": label, **asdict(box)} if box else None
    meta = _write(out_dir, png, url, taken.strftime("%Y-%m-%dT%H:%M:%SZ"), "file", hl)
    return Shot(out_dir, meta, None, [])


def fenced(text: str, source: str) -> list[str]:
    """Page text as the untrusted-content contract delivers it: the fence
    from multiplai_core.untrusted, then the data-never-instructions notice."""
    from multiplai_core.untrusted import bracket_notice, fence
    lines = fence(text, source, limit=MATCH_PRINT_LIMIT)
    return lines + [bracket_notice("text read from the captured web page")] if lines else []


def default_dir(url: str, job: str | None) -> Path:
    """<job output dir>/proof/<host and path>/, the job defaulting to the domain."""
    from stages import outdir
    parsed = urlparse(url)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{parsed.hostname or 'page'}{parsed.path}").strip("-")
    return outdir.job_dir(job or domain(url) or "proof") / "proof" / (slug[:60] or "page")
