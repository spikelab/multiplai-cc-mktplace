"""A local page for a whole video-edit job, and the mailbox it writes to.

The server is pointed at the job's output directory. The page shows what the
session wrote there (job.json, candidates.json, looks.json, music.json; see
stages/job_files.py) and plays every render in it. Each choice the user makes
(a comment, a drawing or a range on a render, picked clips, speaker boxes, a
look, music) is posted as a mailbox row, appended to `comments.jsonl` in the mailbox directory, which
the session watches. The server never writes a job file.

Standard library only, so the media test suite keeps running without the
workspace environment.

Security follows the review-viewer skill (multiplai-dev): a per-start token,
written only to `server.token` and `open.html` in the mailbox (both 0600,
deleted on exit) and never printed — the session's stdout is its transcript.
Every /api and /media request needs it (header `X-Review-Token`, or `?t=` for
media, which a <video> element cannot send headers for); a request whose
Origin is not this server is refused. In a container the server binds
0.0.0.0, because the browser is on the other side of a network boundary.

Media is served only for files the job names: the source recording job.json
names (`/media/source`), audio and sample pages named by a valid music.json or
looks.json (`/media/audio?path=`, `/media/sample?path=`), and the renders in
the directory (`/media/<name>`). Any other path is refused. A sample page is
an overlay page the session wrote; it plays in a sandboxed frame with the
renderer's seek shim injected, under its own CSP (SAMPLE_CSP), and its address
carries a key for that one page (`&k=`, sample_key) instead of the token.

Video versions: `clip-01.mp4` is version 1, `clip-01.v2.mp4` version 2, and so
on. The page polls the list, so a re-render saved as the next version appears
without a restart. `pipeline.py render` writes `<stem>.edl.json` beside each
render (stages/render_sidecar.py); `/api/edl` serves it, and the Review tab
draws the render's timeline from it.

A drawing on a paused frame comes in as a JPEG on `/api/marks`, saved as
`<mailbox>/marks/<id>.jpg`; the `drawing` row that follows names it.

The Review tab's style panel reads `/api/style`: a render's caption values,
its brand file's colours and each overlay style's values (stages/style_file.py).
Live overlays play the overlay pages a sidecar names (`/media/overlay?path=`)
in sandboxed frames, injected with the seek shim and `overlay-bridge.js` under
SAMPLE_CSP, each opened by its own key (overlay_key) as sample pages are.
Changes the user makes there come back as `style` and `restyle` rows; the
server never edits a style, brand or overlay file.
"""
from __future__ import annotations

import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from stages import brand, job_files, style_file

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
VERSION_RE = re.compile(r"^(?P<clip>.+?)(?:\.v(?P<v>\d+))?$")
MAX_BODY_BYTES = 256 * 1024
MAX_MARK_BYTES = 4 * 1024 * 1024    # one drawn-on frame, JPEG
JPEG_MAGIC = b"\xff\xd8\xff"
MARK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
EDL_SIDECAR = ".edl.json"           # stages/render_sidecar.SUFFIX: <stem>.edl.json beside a render
MAX_TEXT = 4000
CHUNK = 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; media-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
# An overlay sample page plays in the Look tab's <iframe sandbox="allow-scripts">.
# Its own scripts and the two injected below run inline, but it can fetch
# nothing (overlay pages are self-contained: styles/README.md), and only this
# server's page may frame it. The `sandbox allow-scripts` directive gives it an
# opaque origin even when opened directly, so it never gets the page's rights.
SAMPLE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
              "img-src data:; font-src data:; media-src data:; connect-src 'none'; base-uri 'none'; "
              "form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts")
# Injected into every sample page, in this order, before the page's own scripts:
# the renderer's clock (stages/overlay.py installs the same file), then the
# listener that lets the Look tab loop the page over postMessage.
SAMPLE_SCRIPTS = ("seek-shim.js", "sample-loop.js")
# Injected into every overlay page the Review tab plays live: the same clock, then the
# bridge that seeks it, makes a keyed or alpha page's background transparent, and
# lets the user pick an element and restyle it (static/overlay-bridge.js).
OVERLAY_SCRIPTS = ("seek-shim.js", "overlay-bridge.js")
SAMPLE_LINES = 5            # lines per speaker label for /api/transcript?speaker=
SAMPLE_LINE_WORDS = 24
MAX_WORDS_WINDOW_S = 900.0  # /api/transcript?start=&end= returns at most this span of words


# --- where to bind (same rules as review-viewer's netinfo) -------------------

def detect_container() -> bool:
    flag = os.environ.get("MULTIPLAI_CONTAINER", "")
    if flag in ("0", "1"):
        return flag == "1"
    return Path("/.dockerenv").exists()


def bind_host() -> str:
    return os.environ.get("VIDEO_EDIT_REVIEW_HOST") or ("0.0.0.0" if detect_container() else "127.0.0.1")


def _resolves(name: str, timeout: float = 2.0) -> bool:
    """Whether `name` resolves here, giving up after `timeout` (getaddrinfo has none)."""
    found: list[bool] = []

    def look() -> None:
        try:
            socket.getaddrinfo(name, None)
            found.append(True)
        except (OSError, UnicodeError):
            pass
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    return bool(found)


def _first_address() -> str | None:
    try:
        out = subprocess.run(["hostname", "-I"], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split()
    return parts[0] if parts else None


def display_urls(port: int) -> list[str]:
    """URLs a browser may use, best first; open.html redirects to the first.

    VIDEO_EDIT_REVIEW_URL_HOST, when set, comes first. In a container the
    `<hostname>.orb.local` name is offered only when it resolves, because only
    OrbStack serves it; elsewhere the container IP comes first, and 127.0.0.1
    is the last resort (a port published with `-p`).
    """
    urls = []
    override = os.environ.get("VIDEO_EDIT_REVIEW_URL_HOST")
    if override:
        base = override if "://" in override else f"http://{override}"
        urls.append(f"{base.rstrip('/')}:{port}/")
    if not detect_container():
        return urls or [f"http://127.0.0.1:{port}/"]
    orb = f"{socket.gethostname()}.orb.local"
    if _resolves(orb):
        urls.append(f"http://{orb}:{port}/")
    addr = _first_address()
    if addr:
        urls.append(f"http://{addr}:{port}/")
    return urls or [f"http://127.0.0.1:{port}/"]


# --- HTTP Range ----------------------------------------------------------------

class RangeNotSatisfiable(ValueError):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """(first, last) byte positions, inclusive; None means send the whole file.

    Handles one range: `bytes=0-99`, open-ended `bytes=100-`, suffix
    `bytes=-500`. A malformed or unsatisfiable range raises
    RangeNotSatisfiable (the server answers 416). Several ranges at once are
    not supported; the whole file is sent, which RFC 9110 allows.
    """
    if not header:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        raise RangeNotSatisfiable(header)
    if "," in spec:
        return None
    first_s, dash, last_s = spec.strip().partition("-")
    if not dash:
        raise RangeNotSatisfiable(header)
    try:
        if first_s == "":                      # suffix: the last N bytes
            n = int(last_s)
            if n <= 0 or size == 0:
                raise RangeNotSatisfiable(header)
            return max(0, size - n), size - 1
        first = int(first_s)
        last = int(last_s) if last_s else size - 1
    except ValueError:
        raise RangeNotSatisfiable(header) from None
    if first < 0 or first >= size or last < first:
        raise RangeNotSatisfiable(header)
    return first, min(last, size - 1)


# --- videos and comments ---------------------------------------------------------

def list_videos(video_dir: Path, exclude: set[Path] | frozenset[Path] = frozenset()) -> list[dict]:
    """[{clip, versions: [{version, name}]}], clips by name, versions ascending.

    Hidden files are skipped: `pipeline.py render` writes to `.<name>.partial.mp4`
    until ffmpeg finishes. So is any file in `exclude` (resolved paths): the
    source recording, when it sits in the job directory, is not a render.
    """
    clips: dict[str, list[dict]] = {}
    for f in sorted(video_dir.iterdir()):
        if f.name.startswith(".") or f.suffix.lower() not in VIDEO_EXTS or not f.is_file():
            continue
        if exclude and f.resolve() in exclude:
            continue
        m = VERSION_RE.match(f.stem)
        assert m
        clips.setdefault(m["clip"], []).append({"version": int(m["v"] or 1), "name": f.name})
    return [{"clip": c, "versions": sorted(v, key=lambda x: x["version"])} for c, v in sorted(clips.items())]


def speaker_lines(words: list[dict], per_speaker: int = SAMPLE_LINES,
                  max_words: int = SAMPLE_LINE_WORDS) -> list[dict]:
    """[{label, lines: [{start, end, text}]}] — sample lines to tell speakers apart.

    Consecutive words with one label make a turn. Up to `per_speaker` turns per
    label are picked, spread over the recording, preferring turns of four words
    or more; a turn longer than `max_words` is cut with "…". Words with no
    label are skipped, so a transcript without labels gives [].
    """
    turns: dict[str, list[list[dict]]] = {}
    prev = None
    for w in words:
        label = w.get("speaker")
        if not label:
            prev = None
            continue
        if label != prev:
            turns.setdefault(label, []).append([])
        turns[label][-1].append(w)
        prev = label
    out = []
    for label in sorted(turns):
        ts = turns[label]
        long = [t for t in ts if len(t) >= 4]
        pool = long if len(long) >= per_speaker else ts
        if len(pool) > per_speaker:
            step = (len(pool) - 1) / (per_speaker - 1) if per_speaker > 1 else 0
            pool = [pool[round(i * step)] for i in range(per_speaker)]
        lines = []
        for t in pool:
            text = " ".join(str(w.get("text", "")) for w in t[:max_words])
            lines.append({"start": t[0]["start"], "end": t[-1]["end"],
                          "text": text + (" …" if len(t) > max_words else "")})
        out.append({"label": label, "lines": lines})
    return out


def words_between(words: list[dict], start: float, end: float) -> list[dict]:
    """The words that overlap [start, end], as {text, start, end}."""
    return [{"text": w["text"], "start": w["start"], "end": w["end"]}
            for w in words if w["end"] >= start and w["start"] <= end]


# --- overlay sample pages ----------------------------------------------------------

_HEAD_RE = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"^\s*<!doctype[^>]*>", re.IGNORECASE)


def inject_scripts(html: str, scripts: list[str]) -> str:
    """`html` with each of `scripts` as an inline <script>, ahead of the page's own.

    They go right after `<head>`, or after the doctype when there is no head
    (before the doctype would put the page in quirks mode), or first.
    """
    for s in scripts:
        if "</script" in s.lower():
            raise ValueError("an injected script must not contain </script")
    tags = "".join(f"<script>\n{s}\n</script>\n" for s in scripts)
    m = _HEAD_RE.search(html) or _DOCTYPE_RE.match(html)
    return html[:m.end()] + "\n" + tags + html[m.end():] if m else tags + html


def sample_key(token: str, path: str) -> str:
    """The key that opens one sample page instead of the access token.

    A sample page runs scripts the session wrote, inside the Look tab's frame,
    and can read its own address. So its address carries this key, which
    opens that one page and nothing else, never the token itself.
    """
    return hmac.new(token.encode("utf-8"), b"sample\0" + path.encode("utf-8"), "sha256").hexdigest()[:32]


def overlay_key(token: str, path: str) -> str:
    """The key that opens one overlay page a render's sidecar names; as sample_key, for /media/overlay."""
    return hmac.new(token.encode("utf-8"), b"overlay\0" + path.encode("utf-8"), "sha256").hexdigest()[:32]


def style_summary(name: str, outdir: Path) -> dict:
    """The values of the style an overlay names, for the style panel.

    `name` is a style name or a style file's absolute path (stages/edl.py
    Overlay.style). A name is looked up in a valid looks.json first, then
    among the styles shipped in references/styles/. Returns
    {name, status: "ok", file, values}, or {name, status: "missing"|"invalid", error}.
    """
    path: Path | None = None
    if name.endswith(".md") and Path(name).is_absolute():
        path = Path(name)
    else:
        looks = job_files.load(outdir, "looks")
        if looks["status"] == "ok":
            path = next((Path(s["file"]) for s in looks["data"].get("styles", []) if s["name"] == name), None)
        path = path or style_file.shipped(name)
    if path is None or not path.is_file():
        return {"name": name, "status": "missing",
                "error": f"no style file for {name!r}: not in looks.json, not shipped, not a path to a .md file"}
    got = style_file.load(path)
    if got["status"] != "ok":
        return {"name": name, "status": "invalid", "file": str(path), "error": got["error"]}
    return {"name": name, "status": "ok", "file": str(path), "values": got["values"]}


def brand_summary(path: str | None) -> dict:
    """{status: "ok", colours, font} for a brand file, or {status: "invalid", error}.

    `colours` are the four the captions and headline use, defaults filled in;
    `font` is the caption font's file name without its extension, or None
    for the default font. No path gives the defaults a render without a brand uses.
    """
    try:
        b = brand.load(path) if path else brand.default()
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        return {"status": "invalid", "error": str(e)}
    font = b.caption_font()
    return {"status": "ok",
            "colours": {k: getattr(b, k) for k in ("primary", "accent", "background", "caption_outline")},
            "font": Path(font).stem if font else None}


class _JsonCache:
    """Parsed JSON by (path, mtime, size), so polling a large transcript reads it once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[tuple[int, int], Any]] = {}

    def get(self, path: Path) -> Any:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._items.get(str(path))
            if hit and hit[0] == key:
                return hit[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._items[str(path)] = (key, data)
        return data


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def next_version_path(video_dir: Path, clip: str, ext: str = ".mp4") -> Path:
    """Where the next re-render of `clip` goes."""
    versions = [v["version"] for c in list_videos(video_dir) if c["clip"] == clip for v in c["versions"]]
    return video_dir / f"{clip}.v{max(versions, default=0) + 1}{ext}"


class Mailbox:
    def __init__(self, directory: Path):
        self.dir = directory
        self.comments = directory / "comments.jsonl"
        self.token_file = directory / "server.token"
        self.open_html = directory / "open.html"
        self.marks = directory / "marks"

    def create(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.comments.touch(exist_ok=True)

    def append(self, rows: list[dict]) -> None:
        """One write() per batch on an O_APPEND descriptor: a reader never sees half a batch."""
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        fd = os.open(self.comments, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def save_mark(self, jpeg: bytes) -> str:
        """Save a drawn-on frame as marks/<id>.jpg (0600); returns the id."""
        self.marks.mkdir(mode=0o700, exist_ok=True)
        mark_id = uuid.uuid4().hex
        fd = os.open(self.marks / f"{mark_id}.jpg", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, jpeg)
        finally:
            os.close(fd)
        return mark_id

    def read(self) -> list[dict]:
        if not self.comments.exists():
            return []
        rows = []
        for line in self.comments.read_text(encoding="utf-8").split("\n"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


# Mailbox row kinds the page may post: whether the row is about one render
# (`video` and `version`), its own fields (required, then `optional` ones), and
# whether `text` may be empty. A row with no `kind` is a `point` row: that is
# the shape the page posted before rows had kinds.
ROW_KINDS: dict[str, dict] = {
    "point": {"review": True, "fields": ("t", "x", "y"), "needs_text": True},
    "pick": {"review": False, "fields": ("candidate", "decision", "start", "end"), "needs_text": False},
    "panels": {"review": False, "fields": ("panels", "speakers"), "needs_text": False},
    "look": {"review": False, "fields": ("style", "brand", "captions"), "needs_text": False},
    "music": {"review": False, "fields": ("bed", "volume_db", "sfx_set"), "needs_text": False},
    "drawing": {"review": True, "fields": ("t", "image", "shapes"), "needs_text": False},
    "range": {"review": True, "fields": ("t_start", "t_end"), "optional": ("target",), "needs_text": True},
    "style": {"review": True, "fields": ("target", "changes"), "optional": ("save_as",), "needs_text": False},
    "restyle": {"review": True, "fields": ("t", "overlay", "html", "selector", "changes"), "needs_text": False},
}
SHAPE_TOOLS = ("pen", "arrow", "box")
MAX_SHAPES = 100
MAX_PEN_POINTS = 2000
# Points in one drawing row, all shapes together: at about 16 bytes a point as JSON, a full
# drawing stays well inside MAX_BODY_BYTES, so the page can always send it.
MAX_DRAWING_POINTS = 6000
COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# What a range row may point at on the timeline: a lane, or one block in it by index.
TARGET_RE = re.compile(r"^(?:captions|headline|title|(?:segment|overlay|sfx):\d{1,4})$")
# What a style row restyles: the render's captions, its brand, or one overlay style it uses.
STYLE_TARGET_RE = re.compile(r"^(?:captions|brand|style:(?P<name>[^\x00-\x1f\x7f]{1,300}))$")
# A new style or brand file the session saves in the job directory, by name (no path).
SAVE_AS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
BRAND_KEYS = ("primary", "accent", "background", "caption_outline")
# The style values the panel edits (stages/style_file.py panel_values), and their bounds.
STYLE_KEY_RE = re.compile(r"^(?:palette\.[A-Za-z][\w-]{0,39}|fonts\.(?:display|body)|fonts\.weights\.(?:display|body)"
                          r"|shape\.radius_px|motion\.(?:enter_s|exit_s|stagger_s))$")
STYLE_NUMBERS = {"fonts.weights.": (100, 1000), "shape.radius_px": (0, 1000), "motion.": (0, 10)}
# The computed styles the overlay bridge reports for a picked element, and the ones a restyle row may change.
RESTYLE_PROPS = ("color", "background-color", "font-family", "font-size", "font-weight", "opacity", "transform")
MAX_CHANGES = 40
MAX_VALUE = 300
MAX_SELECTOR = 300
# A CSS value or font list the session copies into a page's CSS: one declaration's value, nothing more.
CSS_VALUE_RE = re.compile(r"^[^;{}<>\\\x00-\x1f\x7f]{1,200}$")
URL_RE = re.compile(r"url\s*\(", re.IGNORECASE)   # overlay pages load nothing (styles/README.md)
MAX_PANELS = 8
MAX_NAME = 40
MIN_VOLUME_DB, MAX_VOLUME_DB = -60.0, 0.0
# What the session offers, by the list a look or music row picks from.
OFFERED_LISTS = {"styles": ("looks", "styles"), "brands": ("looks", "brands"),
                 "beds": ("music", "beds"), "sfx_sets": ("music", "sfx_sets")}


def _finite(c: dict, *keys: str) -> list[float]:
    """The values under `keys` as finite floats, or ValueError naming them."""
    try:
        vals = [float(c[k]) for k in keys]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{', '.join(keys)} must be numbers") from None
    # float() takes "nan" and "inf", and json.loads takes bare NaN and Infinity;
    # every comparison with NaN is false, so test finiteness first.
    if any(isinstance(c[k], bool) for k in keys) or not all(map(math.isfinite, vals)):
        raise ValueError(f"{', '.join(keys)} must be finite numbers")
    return vals


def _pick(c: dict, candidates: set[str] | frozenset[str]) -> dict:
    if not isinstance(c["candidate"], str) or c["candidate"] not in candidates:
        raise ValueError(f"unknown candidate {c['candidate']!r}")
    if c["decision"] not in ("accept", "reject"):
        raise ValueError("decision must be accept or reject")
    start, end = _finite(c, "start", "end")
    if start < 0 or end <= start:
        raise ValueError("need 0 <= start < end")
    return {"candidate": c["candidate"], "decision": c["decision"], "start": round(start, 3), "end": round(end, 3)}


def _panels(c: dict) -> dict:
    panels, speakers = c["panels"], c["speakers"]
    if not isinstance(panels, dict) or not 1 <= len(panels) <= MAX_PANELS:
        raise ValueError(f"panels must name 1..{MAX_PANELS} boxes")
    out: dict[str, dict] = {}
    for name, rect in panels.items():
        if not name.strip() or len(name) > MAX_NAME:
            raise ValueError(f"a panel name must be 1..{MAX_NAME} characters")
        if not isinstance(rect, dict) or set(rect) != {"x", "y", "w", "h"}:
            raise ValueError(f"panel {name!r} must be {{x, y, w, h}}")
        x, y, w, h = _finite(rect, "x", "y", "w", "h")
        if x < 0 or y < 0 or w < 1 or h < 1:
            raise ValueError(f"panel {name!r} must have x, y >= 0 and w, h >= 1 (source pixels)")
        out[name] = {"x": round(x), "y": round(y), "w": round(w), "h": round(h)}
    if not isinstance(speakers, dict):
        raise ValueError("speakers must map transcript labels to panel names")
    for label, name in speakers.items():
        if not label or len(label) > 64 or not isinstance(name, str) or name not in out:
            raise ValueError(f"speaker {label!r} must map to one of the panels")
    return {"panels": out, "speakers": dict(speakers)}


def _chosen(c: dict, field: str, offered: set[str] | frozenset[str], what: str) -> str | None:
    """A name the session offers in looks.json or music.json, or None for none."""
    v = c[field]
    if v is None:
        return None
    if not isinstance(v, str) or v not in offered:
        raise ValueError(f"{field} {v!r} is not a {what} the session offers")
    return v


def _look(c: dict, offered: dict[str, set[str]]) -> dict:
    captions = c["captions"]
    if captions is not None:
        node = job_files.schema("looks")["properties"]["captions"]["items"]["properties"]["values"]
        errors = job_files.check(captions, node, where="captions")
        if errors:
            raise ValueError(errors[0])
    row = {"style": _chosen(c, "style", offered.get("styles", set()), "style"),
           "brand": _chosen(c, "brand", offered.get("brands", set()), "brand"),
           "captions": captions}
    if all(v is None for v in row.values()):
        raise ValueError("a look row must pick a style, a brand or caption values")
    return row


def _music(c: dict, offered: dict[str, set[str]]) -> dict:
    (volume,) = _finite(c, "volume_db")
    if not MIN_VOLUME_DB <= volume <= MAX_VOLUME_DB:
        raise ValueError(f"volume_db must be within {MIN_VOLUME_DB:g}..{MAX_VOLUME_DB:g}")
    row = {"bed": _chosen(c, "bed", offered.get("beds", set()), "music bed"),
           "volume_db": round(volume, 1),
           "sfx_set": _chosen(c, "sfx_set", offered.get("sfx_sets", set()), "sound-effect set")}
    if row["bed"] is None and row["sfx_set"] is None:
        raise ValueError("a music row must pick a bed or a sound-effect set")
    return row


def _shapes(shapes: Any) -> list[dict]:
    """Pen, arrow and box shapes with points as fractions of the picture (0..1)."""
    if not isinstance(shapes, list) or not 1 <= len(shapes) <= MAX_SHAPES:
        raise ValueError(f"shapes must be a list of 1..{MAX_SHAPES} shapes")
    out = []
    for i, sh in enumerate(shapes):
        if not isinstance(sh, dict) or set(sh) != {"tool", "color", "points"}:
            raise ValueError(f"shape {i} must be {{tool, color, points}}")
        if sh["tool"] not in SHAPE_TOOLS:
            raise ValueError(f"shape {i}: tool must be one of {', '.join(SHAPE_TOOLS)}")
        if not isinstance(sh["color"], str) or not COLOUR_RE.match(sh["color"]):
            raise ValueError(f"shape {i}: color must be #RRGGBB")
        pts = sh["points"]
        want = (1, MAX_PEN_POINTS) if sh["tool"] == "pen" else (2, 2)
        if not isinstance(pts, list) or not want[0] <= len(pts) <= want[1]:
            raise ValueError(f"shape {i}: a {sh['tool']} has {want[0]}..{want[1]} points")
        clean = []
        for pt in pts:
            if not isinstance(pt, list) or len(pt) != 2:
                raise ValueError(f"shape {i}: each point is [x, y]")
            x, y = _finite({"x": pt[0], "y": pt[1]}, "x", "y")
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"shape {i}: points are fractions of the picture, within 0..1")
            clean.append([round(x, 4), round(y, 4)])
        out.append({"tool": sh["tool"], "color": sh["color"].upper(), "points": clean})
    if sum(len(sh["points"]) for sh in out) > MAX_DRAWING_POINTS:
        raise ValueError(f"a drawing has at most {MAX_DRAWING_POINTS} points in all its shapes")
    return out


def _drawing(c: dict, marks_dir: Path | None) -> dict:
    (t,) = _finite(c, "t")
    if t < 0:
        raise ValueError("t must be >= 0")
    image = c["image"]
    if not isinstance(image, str) or not MARK_ID_RE.match(image) or marks_dir is None \
            or not (marks_dir / f"{image}.jpg").is_file():
        raise ValueError(f"unknown image {image!r}: post the drawn frame to /api/marks first")
    return {"t": round(t, 3), "image": image, "shapes": _shapes(c["shapes"])}


def _range(c: dict) -> dict:
    t_start, t_end = _finite(c, "t_start", "t_end")
    if t_start < 0 or t_end <= t_start:
        raise ValueError("need 0 <= t_start < t_end")
    target = c.get("target")
    if target is not None and (not isinstance(target, str) or not TARGET_RE.match(target)):
        raise ValueError("target must be captions, headline, title, or segment:N, overlay:N or sfx:N")
    return {"t_start": round(t_start, 3), "t_end": round(t_end, 3), "target": target}


def _old(v: Any) -> bool:
    """An old value as the page read it: null, a bool, a finite number or a short string."""
    return v is None or isinstance(v, bool) or (_number(v)) or (isinstance(v, str) and len(v) <= MAX_VALUE)


def _changes(changes: Any, check) -> dict:
    """{key: [old, new]} with each new value checked by `check(key, new)`, which returns it cleaned."""
    if not isinstance(changes, dict) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ValueError(f"changes must map 1..{MAX_CHANGES} keys to [old, new]")
    out = {}
    for key, pair in changes.items():
        if not isinstance(pair, list) or len(pair) != 2 or not _old(pair[0]):
            raise ValueError(f"changes[{key!r}] must be [old, new]")
        new = check(key, pair[1])
        if new == pair[0] and isinstance(new, bool) == isinstance(pair[0], bool):
            raise ValueError(f"changes[{key!r}]: the new value is the old one")
        out[key] = [pair[0], new]
    return out


def _css_text(key: str, v: Any) -> str:
    """One CSS value: nothing that ends the declaration, opens a comment or loads a file."""
    if not isinstance(v, str) or not CSS_VALUE_RE.match(v.strip()) or "/*" in v or URL_RE.search(v):
        raise ValueError(f"{key} must be 1..200 characters of one CSS value (no ; {{ }} < > \\ or url())")
    return v.strip()


def _caption_value(key: str, v: Any) -> Any:
    node = job_files.schema("looks")["properties"]["captions"]["items"]["properties"]["values"]
    node = {**node, "properties": {**node["properties"],
                                   "max_chars": {"type": "integer", "minimum": 8, "maximum": 80}}}
    errors = job_files.check({key: v}, node, where="captions")
    if errors:
        raise ValueError(errors[0])
    return v


def _brand_value(key: str, v: Any) -> str:
    if key not in BRAND_KEYS:
        raise ValueError(f"brand has no {key!r}: change one of {', '.join(BRAND_KEYS)}")
    if not isinstance(v, str) or not COLOUR_RE.match(v):
        raise ValueError(f"brand {key} must be #RRGGBB")
    return v.upper()


def _style_value(key: str, v: Any) -> Any:
    if not isinstance(key, str) or not STYLE_KEY_RE.match(key):
        raise ValueError(f"a style has no {key!r} the panel edits")
    if key.startswith("palette."):
        if not isinstance(v, str) or not COLOUR_RE.match(v):
            raise ValueError(f"{key} must be #RRGGBB")
        return v.upper()
    if key in ("fonts.display", "fonts.body"):
        return _css_text(key, v)
    lo, hi = next(b for prefix, b in STYLE_NUMBERS.items() if key.startswith(prefix))
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{key} must be a number, not {type(v).__name__}")
    (n,) = _finite({key: v}, key)
    if not lo <= n <= hi or (key.startswith("fonts.weights.") and not isinstance(v, int)):
        raise ValueError(f"{key} must be {'a whole number ' if key.startswith('fonts.') else ''}within {lo}..{hi}")
    return v


def _style(c: dict, record: dict | None) -> dict:
    target = c["target"]
    m = STYLE_TARGET_RE.match(target) if isinstance(target, str) else None
    if not m:
        raise ValueError("target must be captions, brand or style:<name>")
    if record is None:
        raise ValueError("this render has no .edl.json, so there is nothing to restyle against")
    edl = record["edl"]
    if m["name"] is not None:
        if m["name"] not in {o.get("style") for o in edl.get("overlays") or [] if isinstance(o, dict)}:
            raise ValueError(f"no overlay in this render uses style {m['name']!r}")
        check = _style_value
    elif target == "captions":
        if not isinstance(edl.get("captions"), dict):
            raise ValueError("this render has no captions")
        check = _caption_value
    else:
        check = _brand_value
    row = {"target": target, "changes": _changes(c["changes"], check), "save_as": c.get("save_as")}
    if row["save_as"] is not None:
        if target == "captions":
            raise ValueError("save_as is for a brand or a style; caption values go into the EDL")
        if not isinstance(row["save_as"], str) or not SAVE_AS_RE.match(row["save_as"]):
            raise ValueError("save_as must be a file name of 1..63 letters, digits, - or _")
    return row


def _restyle_value(key: str, v: Any) -> str:
    if key not in RESTYLE_PROPS:
        raise ValueError(f"a restyle changes only {', '.join(RESTYLE_PROPS)}")
    return _css_text(key, v)


def _restyle(c: dict, record: dict | None) -> dict:
    (t,) = _finite(c, "t")
    if t < 0:
        raise ValueError("t must be >= 0")
    if record is None:
        raise ValueError("this render has no .edl.json, so its overlays are unknown")
    overlays = record["edl"].get("overlays") or []
    i = c["overlay"]
    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(overlays):
        raise ValueError(f"overlay must be the index of one of this render's {len(overlays)} overlays")
    if not isinstance(overlays[i], dict) or c["html"] != overlays[i].get("html"):
        raise ValueError(f"html must be overlay {i}'s page as the render's .edl.json names it")
    sel = c["selector"]
    if not isinstance(sel, str) or not 1 <= len(sel) <= MAX_SELECTOR or re.search(r"[\x00-\x1f\x7f]", sel):
        raise ValueError(f"selector must be 1..{MAX_SELECTOR} characters on one line")
    return {"t": round(t, 3), "overlay": i, "html": c["html"], "selector": sel,
            "changes": _changes(c["changes"], _restyle_value)}


def offered_names(outdir: Path) -> dict[str, set[str]]:
    """The names in a valid looks.json and music.json, by list (OFFERED_LISTS)."""
    files = {name: job_files.load(outdir, name) for name in ("looks", "music")}
    return {key: ({x["name"] for x in files[f]["data"].get(lst, [])} if files[f]["status"] == "ok" else set())
            for key, (f, lst) in OFFERED_LISTS.items()}


def validate_comment(c: Any, known: set[tuple[str, int]],
                     candidates: set[str] | frozenset[str] = frozenset(),
                     offered: dict[str, set[str]] | None = None,
                     marks_dir: Path | None = None,
                     records: dict[tuple[str, int], dict | None] | None = None) -> dict:
    """One row from the page → a mailbox row, or ValueError.

    `known` is the (video, version) pairs on disk; `candidates` the ids in a
    valid candidates.json; `offered` the names a look or music row may pick
    (offered_names); `marks_dir` where the images drawing rows name are saved;
    `records` each render's sidecar (None for a render without one), by
    (video, version), which style and restyle rows are checked against.
    Unknown kinds and fields are refused.
    """
    if not isinstance(c, dict):
        raise ValueError("each comment must be an object")
    kind = c.get("kind", "point")
    spec = ROW_KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise ValueError(f"unknown kind {kind!r}")
    allowed = {"kind", "text", *spec["fields"], *spec.get("optional", ()),
               *(("video", "version") if spec["review"] else ())}
    unknown = sorted(set(c) - allowed)
    if unknown:
        raise ValueError(f"{kind} rows have no field(s) {', '.join(map(repr, unknown))}")
    missing = [f for f in spec["fields"] if f not in c]
    if missing:
        raise ValueError(f"{kind} rows need {', '.join(missing)}")
    text = c.get("text", "")
    if not isinstance(text, str) or len(text) > MAX_TEXT or (spec["needs_text"] and not text.strip()):
        raise ValueError(f"text must be {1 if spec['needs_text'] else 0}..{MAX_TEXT} characters")
    row: dict[str, Any] = {"id": f"c-{uuid.uuid4().hex[:12]}", "kind": kind}
    if spec["review"]:
        video, version = c.get("video"), c.get("version")
        if not isinstance(video, str) or not isinstance(version, int) or (video, version) not in known:
            raise ValueError(f"unknown video {video!r} version {version!r}")
        row.update(video=video, version=version)
    if kind == "point":
        t, x, y = _finite(c, "t", "x", "y")
        if t < 0 or not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("t must be a finite number >= 0 and x, y within 0..1")
        row.update(t=round(t, 3), x=round(x, 4), y=round(y, 4))
    elif kind == "pick":
        row.update(_pick(c, candidates))
    elif kind == "panels":
        row.update(_panels(c))
    elif kind == "look":
        row.update(_look(c, offered or {}))
    elif kind == "music":
        row.update(_music(c, offered or {}))
    elif kind == "drawing":
        row.update(_drawing(c, marks_dir))
    elif kind == "range":
        row.update(_range(c))
    elif kind == "style":
        row.update(_style(c, (records or {}).get((row["video"], row["version"]))))
    elif kind == "restyle":
        row.update(_restyle(c, (records or {}).get((row["video"], row["version"]))))
    row.update(text=text.strip(), created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return row


# --- server -------------------------------------------------------------------------

class _Reject(Exception):
    def __init__(self, status: int, error: str, headers: dict | None = None):
        self.status, self.error, self.headers = status, error, headers or {}


class ReviewHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def make_handler(video_dir: Path, mailbox: Mailbox, token: str):
    cache = _JsonCache()

    def job() -> dict | None:
        got = job_files.load(video_dir, "job")
        return got["data"] if got["status"] == "ok" else None

    def videos() -> list[dict]:
        j = job()
        return list_videos(video_dir, {Path(j["source"]).resolve()} if j else frozenset())

    def sidecar(video: Any, version: int) -> dict:
        """The sidecar `pipeline.py render` wrote beside one listed render; _Reject 404 or 422 without one."""
        name = next((v["name"] for c in videos() if c["clip"] == video
                     for v in c["versions"] if v["version"] == version), None)
        if name is None:
            raise _Reject(404, f"no render {video!r} version {version}")
        path = video_dir / (Path(name).stem + EDL_SIDECAR)
        if not path.is_file():
            raise _Reject(404, f"{name} has no {path.name}: it was rendered before render wrote one")
        try:
            data = cache.get(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _Reject(422, f"cannot read {path.name}: {e}") from None
        if not isinstance(data, dict) or not isinstance(data.get("edl"), dict):
            raise _Reject(422, f"{path.name} is not a render sidecar")
        return data

    def record(video: Any, version: Any) -> dict | None:
        """sidecar(), or None for a render without a usable one."""
        try:
            return sidecar(video, version)
        except _Reject:
            return None

    def overlay_pages(data: dict) -> list[str]:
        """The absolute overlay page paths a sidecar's EDL names, in its order."""
        return [o["html"] for o in data["edl"].get("overlays") or []
                if isinstance(o, dict) and isinstance(o.get("html"), str) and Path(o["html"]).is_absolute()]

    def named_overlays() -> set[Path]:
        """Every overlay page the sidecars of the listed renders name, resolved."""
        out: set[Path] = set()
        for c in videos():
            for v in c["versions"]:
                data = record(c["clip"], v["version"])
                if data:
                    out.update(Path(p).resolve() for p in overlay_pages(data))
        return out

    class Handler(BaseHTTPRequestHandler):
        server_version = "video-edit-review"
        protocol_version = "HTTP/1.1"   # keep-alive: the browser seeks with many small Range requests
        sys_version = ""

        def log_message(self, format, *args):   # noqa: A002 — keep the token out of any log
            pass

        def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None,
                     csp: str = CSP) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()

        def _json(self, obj, status: int = 200, extra: dict | None = None) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _check_origin(self) -> None:
            origin = self.headers.get("Origin")
            if origin is not None and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
                raise _Reject(403, "forbidden")

        def _check_token(self, query: dict) -> None:
            given = self.headers.get("X-Review-Token") or query.get("t", [""])[0]
            if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
                raise _Reject(401, "unauthorized")

        def _check_page_key(self, route: str, query: dict) -> None:
            given, path = query.get("k", [""])[0], query.get("path", [""])[0]
            want = (overlay_key if route == "/media/overlay" else sample_key)(token, path)
            if not hmac.compare_digest(given.encode("utf-8"), want.encode("utf-8")):
                raise _Reject(401, "unauthorized")

        def do_GET(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def _dispatch(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                self._check_origin()
                if url.path in ("/media/sample", "/media/overlay") and "k" in query:
                    self._check_page_key(url.path, query)
                elif url.path.startswith(("/api/", "/media/")):
                    self._check_token(query)
                get = self.command in ("GET", "HEAD")
                if url.path == "/api/videos" and get:
                    return self._json({"videos": videos()})
                if url.path == "/api/looks" and get:
                    return self._json(self._looks())
                if url.path in ("/api/job", "/api/candidates", "/api/music") and get:
                    return self._json(job_files.load(video_dir, url.path[len("/api/"):]))
                if url.path == "/api/brands" and get:
                    return self._json({"brands": self._brands()})
                if url.path == "/api/sentences" and get:
                    return self._json({"sentences": self._sentences()})
                if url.path == "/api/transcript" and get:
                    return self._transcript(query)
                if url.path in ("/media/source", "/media/audio", "/media/sample", "/media/overlay") and get:
                    return self._named(url.path[len("/media/"):], query)
                if url.path == "/api/comments" and self.command == "POST":
                    return self._post_comments()
                if url.path == "/api/marks" and self.command == "POST":
                    return self._post_mark()
                if url.path == "/api/edl" and get:
                    return self._edl(query)
                if url.path == "/api/style" and get:
                    return self._style(query)
                if url.path == "/api/comments" and get:
                    clip, kind = query.get("video", [None])[0], query.get("kind", [None])[0]
                    return self._json({"comments": [
                        r for r in mailbox.read()
                        if (clip is None or r.get("video") == clip)
                        and (kind is None or r.get("kind", "point") == kind)]})
                if url.path.startswith("/media/") and get:
                    return self._media(unquote(url.path[len("/media/"):]))
                if self.command in ("GET", "HEAD"):
                    return self._static(url.path)
                raise _Reject(405, "method not allowed")
            except _Reject as r:
                # A refused POST's body may be unread (a check failed before the route read it),
                # so the connection must not carry another request.
                if self.command == "POST":
                    self.close_connection = True
                # A client refused mid-upload (a mark over the limit) may already be gone.
                try:
                    self._json({"error": r.error}, r.status, r.headers)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _static(self, route: str) -> None:
            name = "review.html" if route in ("/", "/index.html") else route.removeprefix("/static/")
            if route not in ("/", "/index.html") and not route.startswith("/static/"):
                raise _Reject(404, "not found")
            path = STATIC_DIR / name
            if "/" in name or name.startswith(".") or not path.is_file():
                raise _Reject(404, "not found")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            body = path.read_bytes()
            self._headers(200, ctype, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _looks(self) -> dict:
            """looks.json as /api/job serves the others, plus a key per sample page (sample_key)."""
            got = job_files.load(video_dir, "looks")
            if got["status"] == "ok":
                got["sample_keys"] = {s["sample_html"]: sample_key(token, s["sample_html"])
                                      for s in got["data"].get("styles", [])}
            return got

        def _brands(self) -> list[dict]:
            """Each brand looks.json offers, read with stages/brand.py; [] without a valid looks.json."""
            got = job_files.load(video_dir, "looks")
            if got["status"] != "ok":
                return []
            return [{"name": b["name"], "file": b["file"], **brand_summary(b["file"])}
                    for b in got["data"].get("brands", [])]

        def _job_file(self, key: str) -> Any:
            j = job()
            if j is None:
                raise _Reject(404, "no valid job.json in the job directory")
            try:
                return cache.get(Path(j[key]))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                raise _Reject(422, f"cannot read {key}: {e}") from None

        def _sentences(self) -> list[dict]:
            data = self._job_file("sentences")
            if not isinstance(data, list) or not all(
                    isinstance(s, dict) and _number(s.get("start")) and _number(s.get("end"))
                    and isinstance(s.get("text"), str) for s in data):
                raise _Reject(422, "sentences.json is not a list of {start, end, text}")
            return data

        def _words(self) -> list[dict]:
            data = self._job_file("transcript")
            words = data.get("words") if isinstance(data, dict) else None
            if not isinstance(words, list) or not all(
                    isinstance(w, dict) and _number(w.get("start")) and _number(w.get("end"))
                    and isinstance(w.get("text"), str) for w in words):
                raise _Reject(422, "transcript.json has no words list of {text, start, end}")
            return words

        def _transcript(self, query: dict) -> None:
            words = self._words()
            if "start" in query or "end" in query:
                try:
                    start, end = float(query["start"][0]), float(query["end"][0])
                except (KeyError, ValueError):
                    raise _Reject(400, "start and end must both be numbers") from None
                if not (math.isfinite(start) and math.isfinite(end)) or end < start \
                        or end - start > MAX_WORDS_WINDOW_S:
                    raise _Reject(400, f"need start <= end, at most {MAX_WORDS_WINDOW_S:g} s apart")
                return self._json({"words": words_between(words, start, end)})
            speakers = speaker_lines(words)
            label = query.get("speaker", [None])[0]
            if label is not None:
                speakers = [s for s in speakers if s["label"] == label]
            return self._json({"speakers": speakers})

        @staticmethod
        def _render(query: dict) -> tuple[str, int]:
            video = query.get("video", [""])[0]
            try:
                return video, int(query.get("version", [""])[0])
            except ValueError:
                raise _Reject(400, "version must be a number") from None

        def _edl(self, query: dict) -> None:
            """The sidecar of one render, with a key per overlay page it names; 404 for a render without one."""
            data = sidecar(*self._render(query))
            return self._json({"status": "ok", "data": data,
                               "overlay_keys": {p: overlay_key(token, p) for p in overlay_pages(data)}})

        def _style(self, query: dict) -> None:
            """What the style panel shows for one render: its caption values, brand colours and overlay styles."""
            video, version = self._render(query)
            edl = sidecar(video, version)["edl"]
            path = edl.get("brand") if isinstance(edl.get("brand"), str) else None
            names = dict.fromkeys(o["style"] for o in edl.get("overlays") or []
                                  if isinstance(o, dict) and isinstance(o.get("style"), str) and o["style"])
            out = edl.get("output") if isinstance(edl.get("output"), dict) else {}
            return self._json({
                "status": "ok", "video": video, "version": version,
                "output": {"width": out.get("width"), "height": out.get("height")},
                "captions": edl.get("captions") if isinstance(edl.get("captions"), dict) else None,
                "brand": {"file": path, **brand_summary(path)},
                "styles": [style_summary(n, video_dir) for n in names],
            })

        def _named(self, what: str, query: dict) -> None:
            """A file a validated input file names; anything else is 404."""
            allowed = named_overlays() if what == "overlay" else job_files.named_files(video_dir)[what]
            if what == "source":
                path = next(iter(allowed), None)
            else:
                given = query.get("path", [""])[0]
                path = Path(given).resolve() if given and Path(given).is_absolute() else None
                path = path if path in allowed else None
            if path is None or not path.is_file():
                raise _Reject(404, "not found")
            if what in ("sample", "overlay"):
                try:
                    html = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raise _Reject(422, f"the {what} page is not UTF-8 text") from None
                scripts = SAMPLE_SCRIPTS if what == "sample" else OVERLAY_SCRIPTS
                body = inject_scripts(html, [(STATIC_DIR / n).read_text(encoding="utf-8")
                                             for n in scripts]).encode("utf-8")
                self._headers(200, "text/html; charset=utf-8", len(body), csp=SAMPLE_CSP)
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            self._send_file(path, mimetypes.guess_type(path.name)[0]
                            or ("video/mp4" if what == "source" else "application/octet-stream"))

        def _media(self, name: str) -> None:
            """A render the list names; the source recording is /media/source, not this."""
            path = video_dir / name
            if "/" in name or name.startswith(".") or path.suffix.lower() not in VIDEO_EXTS or not path.is_file() \
                    or name not in {v["name"] for c in videos() for v in c["versions"]}:
                raise _Reject(404, "not found")
            self._send_file(path, mimetypes.guess_type(name)[0] or "video/mp4")

        def _send_file(self, path: Path, ctype: str) -> None:
            """The file, or the one byte range the Range header asks for."""
            size = path.stat().st_size
            try:
                rng = parse_range(self.headers.get("Range"), size)
            except RangeNotSatisfiable:
                raise _Reject(416, "range not satisfiable", {"Content-Range": f"bytes */{size}"}) from None
            first, last = rng if rng else (0, size - 1)
            extra = {"Accept-Ranges": "bytes"}
            if rng:
                extra["Content-Range"] = f"bytes {first}-{last}/{size}"
            self._headers(206 if rng else 200, ctype, last - first + 1, extra)
            if self.command == "HEAD":
                return
            with path.open("rb") as f:
                f.seek(first)
                left = last - first + 1
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _post_mark(self) -> None:
            """A drawn-on frame: image/jpeg, at most MAX_MARK_BYTES, starting FF D8 FF."""
            # A refused body is left unread, so the connection must not carry another request.
            self.close_connection = True
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "image/jpeg":
                raise _Reject(415, "a mark must be image/jpeg")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _Reject(400, "bad content length") from None
            if length <= 0 or length > MAX_MARK_BYTES:
                raise _Reject(413 if length > 0 else 400, f"a mark must be 1..{MAX_MARK_BYTES} bytes")
            data = self.rfile.read(length)
            if len(data) != length or not data.startswith(JPEG_MAGIC):
                raise _Reject(400, "the body is not a JPEG")
            self._json({"ok": True, "image": mailbox.save_mark(data)})

        def _post_comments(self) -> None:
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise _Reject(415, "content type must be application/json")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _Reject(400, "bad content length") from None
            if length <= 0 or length > MAX_BODY_BYTES:
                raise _Reject(413 if length > 0 else 400, "bad body size")
            try:
                data = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise _Reject(400, "invalid json") from None
            comments = data.get("comments") if isinstance(data, dict) else None
            if not isinstance(comments, list) or not comments:
                raise _Reject(400, "expected {comments: [...]}")
            known = {(c["clip"], v["version"]) for c in videos() for v in c["versions"]}
            cands = job_files.load(video_dir, "candidates")
            ids = {c["id"] for c in cands["data"]} if cands["status"] == "ok" else set()
            offered = offered_names(video_dir)
            # The sidecars style and restyle rows are checked against, read once per render.
            records = {(c["video"], c["version"]): None for c in comments
                       if isinstance(c, dict) and c.get("kind") in ("style", "restyle")
                       and isinstance(c.get("video"), str) and isinstance(c.get("version"), int)
                       and (c["video"], c["version"]) in known}
            for video, version in records:
                records[(video, version)] = record(video, version)
            try:
                rows = [validate_comment(c, known, ids, offered, mailbox.marks, records) for c in comments]
            except ValueError as e:
                raise _Reject(400, str(e)) from None
            mailbox.append(rows)
            self._json({"ok": True, "ids": [r["id"] for r in rows]})

    return Handler


def open_page_html(urls: list[str], token: str) -> str:
    """Redirects to the first URL; links every URL in case the first does not resolve."""
    targets = [escape(f"{u}?t={token}", quote=True) for u in urls]
    links = "".join(f"<li><a href=\"{t}\" rel=\"noreferrer\">{escape(u)}</a></li>\n"
                    for t, u in zip(targets, urls))
    return ("<!doctype html>\n<meta charset=\"utf-8\">\n<meta name=\"referrer\" content=\"no-referrer\">\n"
            f"<meta http-equiv=\"refresh\" content=\"0; url={targets[0]}\">\n<title>Opening video review…</title>\n"
            "<p>Opening the video review. If it does not load, try another address:</p>\n"
            f"<ul>\n{links}</ul>\n")


def serve(video_dir: Path, mailbox_dir: Path, host: str | None = None,
          port: int = 8765, span: int = 20) -> tuple[ReviewHTTPServer, str, list[str]]:
    """Bind and publish; returns (server, token, urls). The caller runs serve_forever().

    `video_dir` is the job's output directory: the renders, and the input files
    the session writes (stages/job_files.py), all live in it.
    """
    video_dir = video_dir.resolve()
    if not video_dir.is_dir():
        raise FileNotFoundError(f"job directory not found: {video_dir}")
    mailbox = Mailbox(mailbox_dir.resolve())
    mailbox.create()
    token = secrets.token_urlsafe(32)
    handler = make_handler(video_dir, mailbox, token)
    host = host or bind_host()
    httpd = None
    for candidate in range(port, port + span):
        try:
            httpd = ReviewHTTPServer((host, candidate), handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"no free port in {port}..{port + span - 1}")
    urls = display_urls(httpd.server_address[1])
    _write_private(mailbox.token_file, token + "\n")
    _write_private(mailbox.open_html, open_page_html(urls, token))
    return httpd, token, urls


def unpublish(mailbox_dir: Path) -> None:
    box = Mailbox(mailbox_dir)
    for p in (box.token_file, box.open_html):
        try:
            p.unlink()
        except OSError:
            pass
