"""A local review page for rendered videos, and the mailbox it writes to.

The page plays every render in a directory. Clicking the picture pauses it
and records the time and the click position with a comment; "Send all" posts
the pending comments in one batch, appended to `comments.jsonl` in the
mailbox directory, which the session watches.

Standard library only, so the media test suite keeps running without the
workspace environment.

Security follows the review-viewer skill (multiplai-dev): a per-start token,
written only to `server.token` and `open.html` in the mailbox (both 0600,
deleted on exit) and never printed — the session's stdout is its transcript.
Every /api and /media request needs it (header `X-Review-Token`, or `?t=` for
media, which a <video> element cannot send headers for); a request whose
Origin is not this server is refused. In a container the server binds
0.0.0.0, because the browser is on the other side of a network boundary.

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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
VERSION_RE = re.compile(r"^(?P<clip>.+?)(?:\.v(?P<v>\d+))?$")
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT = 4000
CHUNK = 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; media-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


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

def list_videos(video_dir: Path) -> list[dict]:
    """[{clip, versions: [{version, name}]}], clips by name, versions ascending."""
    clips: dict[str, list[dict]] = {}
    for f in sorted(video_dir.iterdir()):
        if f.suffix.lower() not in VIDEO_EXTS or not f.is_file():
            continue
        m = VERSION_RE.match(f.stem)
        assert m
        clips.setdefault(m["clip"], []).append({"version": int(m["v"] or 1), "name": f.name})
    return [{"clip": c, "versions": sorted(v, key=lambda x: x["version"])} for c, v in sorted(clips.items())]


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


def validate_comment(c: Any, known: set[tuple[str, int]]) -> dict:
    """One comment from the page → a mailbox row, or ValueError."""
    if not isinstance(c, dict):
        raise ValueError("each comment must be an object")
    video, version = c.get("video"), c.get("version")
    if not isinstance(video, str) or not isinstance(version, int) or (video, version) not in known:
        raise ValueError(f"unknown video {video!r} version {version!r}")
    try:
        t, x, y = float(c["t"]), float(c["x"]), float(c["y"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("t, x and y must be numbers") from None
    # float() takes "nan" and "inf", and json.loads takes bare NaN and Infinity;
    # every comparison with NaN is false, so test finiteness first.
    if not all(map(math.isfinite, (t, x, y))) or t < 0 or not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError("t must be a finite number >= 0 and x, y within 0..1")
    text = c.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"text must be 1..{MAX_TEXT} characters")
    return {"id": f"c-{uuid.uuid4().hex[:12]}", "video": video, "version": version,
            "t": round(t, 3), "x": round(x, 4), "y": round(y, 4), "text": text.strip(),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


# --- server -------------------------------------------------------------------------

class _Reject(Exception):
    def __init__(self, status: int, error: str, headers: dict | None = None):
        self.status, self.error, self.headers = status, error, headers or {}


class ReviewHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def make_handler(video_dir: Path, mailbox: Mailbox, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "video-edit-review"
        protocol_version = "HTTP/1.1"   # keep-alive: the browser seeks with many small Range requests
        sys_version = ""

        def log_message(self, format, *args):   # noqa: A002 — keep the token out of any log
            pass

        def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
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
                if url.path == "/api/videos" and self.command in ("GET", "HEAD"):
                    return self._json({"videos": list_videos(video_dir)})
                if url.path == "/api/comments" and self.command == "POST":
                    return self._post_comments()
                if url.path == "/api/comments" and self.command in ("GET", "HEAD"):
                    clip = query.get("video", [None])[0]
                    return self._json({"comments": [r for r in mailbox.read()
                                                    if clip is None or r.get("video") == clip]})
                if url.path.startswith("/media/") and self.command in ("GET", "HEAD"):
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

        def _media(self, name: str) -> None:
            path = video_dir / name
            if "/" in name or name.startswith(".") or path.suffix.lower() not in VIDEO_EXTS or not path.is_file():
                raise _Reject(404, "not found")
            size = path.stat().st_size
            try:
                rng = parse_range(self.headers.get("Range"), size)
            except RangeNotSatisfiable:
                raise _Reject(416, "range not satisfiable", {"Content-Range": f"bytes */{size}"}) from None
            ctype = mimetypes.guess_type(name)[0] or "video/mp4"
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
            known = {(c["clip"], v["version"]) for c in list_videos(video_dir) for v in c["versions"]}
            try:
                rows = [validate_comment(c, known) for c in comments]
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
    """Bind and publish; returns (server, token, urls). The caller runs serve_forever()."""
    video_dir = video_dir.resolve()
    if not video_dir.is_dir():
        raise FileNotFoundError(f"video directory not found: {video_dir}")
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
