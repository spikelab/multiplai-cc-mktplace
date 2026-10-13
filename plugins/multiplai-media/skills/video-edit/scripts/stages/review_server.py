"""A local page for a whole video-edit job, and the mailbox it writes to.

The server is pointed at the job's output directory. The page shows what the
session wrote there (job.json, candidates.json, looks.json, music.json; see
stages/job_files.py) and plays every render in it. Each choice the user makes
(a comment on a render, picked clips, speaker boxes) is posted as a mailbox
row, appended to `comments.jsonl` in the mailbox directory, which the session
watches. The server never writes a job file.

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
the directory (`/media/<name>`). Any other path is refused.

Video versions: `clip-01.mp4` is version 1, `clip-01.v2.mp4` version 2, and so
on. The page polls the list, so a re-render saved as the next version appears
without a restart.
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

from stages import job_files

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
VERSION_RE = re.compile(r"^(?P<clip>.+?)(?:\.v(?P<v>\d+))?$")
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT = 4000
CHUNK = 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; media-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
# A sample page is served on its own, never with the page's script rights: the
# `sandbox` directive gives it an opaque origin even when opened directly.
SAMPLE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
              "form-action 'none'; frame-ancestors 'none'; sandbox")
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
# (`video` and `version`), its own fields (all required), and whether `text`
# may be empty. A row with no `kind` is a `point` row: that is the shape the
# page posted before rows had kinds.
ROW_KINDS: dict[str, dict] = {
    "point": {"review": True, "fields": ("t", "x", "y"), "needs_text": True},
    "pick": {"review": False, "fields": ("candidate", "decision", "start", "end"), "needs_text": False},
    "panels": {"review": False, "fields": ("panels", "speakers"), "needs_text": False},
}
MAX_PANELS = 8
MAX_NAME = 40


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


def validate_comment(c: Any, known: set[tuple[str, int]],
                     candidates: set[str] | frozenset[str] = frozenset()) -> dict:
    """One row from the page → a mailbox row, or ValueError.

    `known` is the (video, version) pairs on disk; `candidates` the ids in a
    valid candidates.json. Unknown kinds and fields are refused.
    """
    if not isinstance(c, dict):
        raise ValueError("each comment must be an object")
    kind = c.get("kind", "point")
    spec = ROW_KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise ValueError(f"unknown kind {kind!r}")
    allowed = {"kind", "text", *spec["fields"], *(("video", "version") if spec["review"] else ())}
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
                if url.path.startswith(("/api/", "/media/")):
                    self._check_token(query)
                get = self.command in ("GET", "HEAD")
                if url.path == "/api/videos" and get:
                    return self._json({"videos": videos()})
                if url.path in ("/api/job", "/api/candidates", "/api/looks", "/api/music") and get:
                    return self._json(job_files.load(video_dir, url.path[len("/api/"):]))
                if url.path == "/api/sentences" and get:
                    return self._json({"sentences": self._sentences()})
                if url.path == "/api/transcript" and get:
                    return self._transcript(query)
                if url.path in ("/media/source", "/media/audio", "/media/sample") and get:
                    return self._named(url.path[len("/media/"):], query)
                if url.path == "/api/comments" and self.command == "POST":
                    return self._post_comments()
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
                self._json({"error": r.error}, r.status, r.headers)
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

        def _named(self, what: str, query: dict) -> None:
            """A file a validated input file names; anything else is 404."""
            allowed = job_files.named_files(video_dir)[what]
            if what == "source":
                path = next(iter(allowed), None)
            else:
                given = query.get("path", [""])[0]
                path = Path(given).resolve() if given and Path(given).is_absolute() else None
                path = path if path in allowed else None
            if path is None or not path.is_file():
                raise _Reject(404, "not found")
            if what == "sample":
                body = path.read_bytes()
                self._headers(200, "text/html; charset=utf-8", len(body), csp=SAMPLE_CSP)
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            self._send_file(path, mimetypes.guess_type(path.name)[0]
                            or ("video/mp4" if what == "source" else "application/octet-stream"))

        def _media(self, name: str) -> None:
            path = video_dir / name
            if "/" in name or name.startswith(".") or path.suffix.lower() not in VIDEO_EXTS or not path.is_file():
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
            try:
                rows = [validate_comment(c, known, ids) for c in comments]
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
