"""The HTTP server between the page and the Claude Code session.

It never calls a model. It serves the page, answers read-only questions about
the review, and carries messages: questions and decisions from the page go to
`inbox.jsonl`, which the session watches; answers the session writes to
`outbox.jsonl` go back to the page through `/api/poll`.

Every `/api/*` request needs the per-start token in `X-Review-Token`. The token
is written only to `server.token` and `open.html` (both 0600, deleted on exit)
and never printed or logged: the session's stdout is its transcript.

The full walkthrough page is served at `/full-walkthrough/<page_key>/<slug>`, a
navigation that cannot send the token header. `page_key` is a second secret per
start, held only in memory and handed to the page by the token-checked
`/api/targets/<slug>/full-walkthrough`; it is never written, printed or logged.
The page gets its own CSP, `PAGE_CSP`, whose `sandbox` gives it an opaque origin.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, NamedTuple
from urllib.parse import parse_qs, urlparse

from code_facts.paths import PathsResult
from multiplai_core.log_utils import log_event
from pydantic import ValidationError

from . import export_zip, registry
from .gitdata import (
    DEFINITION_NAME_RE,
    REPO_FILES_MAX,
    GitError,
    PathNotInReview,
    TargetError,
    allowed_paths,
    definitions,
    file_view,
    pr_status,
    repo_files,
)
from .mailbox import Mailbox, MailboxError, new_question_id, utc_now, write_private
from .models import (
    SHARE_RECIPIENT_RE,
    SHARE_TEXT_MAX,
    Anchor,
    ChecksFile,
    FindingsFile,
    InboxRow,
    Summary,
    findings_digest,
    load_checks,
    load_findings,
)
from .stats import PR_BADGE_IDS, pr_badges
from .summary import Served, full_walkthrough, served_path, summary_path

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COMPONENT = "review-viewer"
MAX_QUESTION_CHARS = 8000
MAX_BODY_BYTES = 64 * 1024
SHARE_RECIPIENT = re.compile(SHARE_RECIPIENT_RE)
REJECT_LOG_INTERVAL = 60.0
# However many pages ask, GitHub is asked about one PR at most this often.
PR_REFRESH_S = 60.0
STOP_MESSAGES = {
    "stop": "viewer stopped by stop --box",
    "findings_changed": "viewer restarted because a findings file changed",
    "share_changed": "viewer restarted to offer Slack (serve --share slack)",
}

CSP = (
    "default-src 'none'; script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' https://cdnjs.cloudflare.com; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


# The codebase-walkthrough page's template: the https origins it loads scripts
# and styles from are the ones PAGE_CSP allows.
WALKTHROUGH_TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "codebase-walkthrough"
    / "scripts"
    / "templates"
    / "walkthrough.html"
)
# A design system's fonts.css, inlined into the page, loads from Google Fonts:
# the stylesheet from the first origin, the font files from the second.
FONT_STYLE_ORIGINS = ("https://fonts.googleapis.com",)
FONT_FILE_ORIGINS = ("https://fonts.gstatic.com",)
_HTTPS_ORIGIN = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?")


def template_origins(path: Path = WALKTHROUGH_TEMPLATE) -> tuple[str, ...]:
    """The https origins *path* names, sorted; () with one warning when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("cannot read the walkthrough template %s: %s", path, exc)
        return ()
    return tuple(sorted({m.group(0).lower() for m in _HTTPS_ORIGIN.finditer(text)}))


def page_csp(origins: tuple[str, ...]) -> str:
    """The CSP of the full walkthrough page: a sandbox (an opaque origin, so the
    page cannot call /api), inline script and style for the template, and only
    the template's origins plus Google Fonts for what the page loads."""
    o = " ".join(origins)
    styles = " ".join((*origins, *FONT_STYLE_ORIGINS))
    fonts = " ".join((*origins, *FONT_FILE_ORIGINS))
    return (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
        f"default-src 'none'; script-src 'unsafe-inline' {o}; "
        f"style-src 'unsafe-inline' {styles}; img-src data: {o}; font-src {fonts}; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ).replace("  ", " ")


PAGE_CSP = page_csp(template_origins())
FULL_WALKTHROUGH_PREFIX = "/full-walkthrough/"


class ViewerHTTPServer(ThreadingHTTPServer):
    """Refuses a port that is already in use.

    `ThreadingHTTPServer` sets SO_REUSEADDR, and on Windows that flag lets a
    second process bind the *same* port: two servers then split requests at
    random, and one review's questions land in another's mailbox. Here the
    bind must fail so the search moves on to the next port.
    """

    allow_reuse_address = False
    daemon_threads = True


@dataclass
class TargetState:
    findings: FindingsFile
    mailbox: Mailbox
    allowed: set[str]
    pr: dict | None = None
    notice: str | None = None
    stats: dict | None = None
    # The review's checks.json, when one sits beside its findings.json and
    # describes the same commits; None hides the page's Checked tab.
    checks: ChecksFile | None = None
    # The review's paths.json (code_facts' paths.v1), when one sits beside its
    # findings.json, names the review's exact head sha and validates; None
    # hides the page's Paths tab.
    paths: PathsResult | None = None
    # The PR this review is of: `pr["number"]` when serve resolved a PR, else
    # the `target.pr` the review recorded in review-state.json. None: not a PR.
    pr_number: int | None = None
    # Every file git lists at head (gitdata.repo_files), for the page's
    # "Show all files in the repo"; [] when git could not list them.
    repo_files: list[str] = field(default_factory=list)
    # The findings.json in the review's directory, and its mtime in ns when last read. `/api/poll`
    # re-reads it when the mtime changes (`reload_findings`), so a page already open sees what
    # review's `answers` wrote. None: the findings were built in memory, or no file is there.
    findings_path: Path | None = None
    findings_mtime: int = 0
    _reload_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # `serve` fetched the PR just before the server started.
    _pr_checked: float = field(default_factory=time.monotonic, repr=False)
    _pr_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def slug(self) -> str:
        return self.findings.target.slug

    def refresh_pr(self) -> dict:
        """Ask GitHub again for the PR's checks, mergeable state and review
        decision (at most once per PR_REFRESH_S), and rebuild the PR badges.
        A failed call keeps the last answer."""
        with self._pr_lock:
            if self.pr and time.monotonic() - self._pr_checked >= PR_REFRESH_S:
                self._pr_checked = time.monotonic()
                try:
                    self.pr.update(
                        pr_status(
                            self.findings.target.repo_path,
                            self.pr["number"],
                            self.pr.get("url") or "",
                        )
                    )
                except TargetError as exc:
                    log.warning("cannot refresh PR #%s: %s", self.pr["number"], exc)
                else:
                    if self.stats is not None:
                        self.stats["badges"] = [
                            b
                            for b in self.stats.get("badges", [])
                            if b.get("id") not in PR_BADGE_IDS
                        ] + [b.to_dict() for b in pr_badges(self.pr)]
            return {"pr": self.pr, "stats": self.stats}

    def reload_findings(self) -> int:
        """Re-read `findings_path` when its mtime changed; return the mtime of the findings served.

        A file that no longer validates, or names another target or head, is
        logged and not served; the mtime is still recorded, so it is not read
        again until it changes. The repository path serve chose is kept.
        """
        if self.findings_path is None:
            return self.findings_mtime
        with self._reload_lock:
            try:
                mtime = self.findings_path.stat().st_mtime_ns
            except OSError:
                return self.findings_mtime
            if mtime == self.findings_mtime:
                return mtime
            self.findings_mtime = mtime
            try:
                ff = load_findings(self.findings_path)
            except (OSError, ValueError) as exc:
                log.warning("findings for %s no longer read: %s", self.slug, exc)
                return mtime
            old = self.findings.target
            if (ff.target.slug, ff.target.head_sha) != (old.slug, old.head_sha):
                log.warning(
                    "%s now names %s at %s; not served",
                    self.findings_path,
                    ff.target.slug,
                    ff.target.head_sha[:12],
                )
                return mtime
            ff.target.repo_path = old.repo_path
            extra = _path_files(self.paths) if self.paths is not None else ()
            self.allowed = allowed_paths(ff.target, ff, self.repo_files, extra=extra)
            self.checks = checks_beside(ff, self.findings_path.parent)
            self.findings = ff
            write_private(
                served_path(self.mailbox.dir),
                Served(ff, self.pr, self.notice, self.stats).to_json(),
            )
            log.info("re-read %s", self.findings_path)
            return mtime

    def summary(self) -> Summary | None:
        """The summary `summary put` last wrote, or None. It was checked when
        it was put; a file that no longer parses is treated as absent rather
        than served half-read."""
        path = summary_path(self.mailbox.dir)
        try:
            return Summary.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("summary for %s does not parse: %s", self.slug, exc)
            return None

    def full_walkthrough(self) -> dict:
        """summary.full_walkthrough for this target, read from disk on every call."""
        return full_walkthrough(
            self.mailbox.dir,
            Served(self.findings, self.pr, self.notice, self.stats),
            self.pr_number,
        )


@dataclass
class Viewer:
    targets: dict[str, TargetState]
    token: str
    agent: str
    session_id: str
    idle_minutes: float
    started: str = field(default_factory=utc_now)
    last_seen: float = field(default_factory=time.monotonic)
    httpd: ViewerHTTPServer | None = None
    stop_reason: str | None = None
    stopped: threading.Event = field(default_factory=threading.Event)
    # `serve --share slack`: the session that started the viewer has the
    # multiplai-messaging:slack skill, so the page may ask it to send to Slack.
    share_slack: bool = False
    # The full walkthrough route's secret (see the module docstring). Never
    # written, printed or logged; `redact` removes it from anything logged.
    page_key: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    _rejects: dict[int, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    def whoami(self) -> dict:
        return {
            "agent": self.agent,
            "session_id": self.session_id,
            "pid": os.getpid(),
            "started": self.started,
            "targets": list(self.targets),
            "mailboxes": [str(t.mailbox.dir.resolve()) for t in self.targets.values()],
            "digests": {
                str(t.mailbox.dir.resolve()): findings_digest(t.findings)
                for t in self.targets.values()
            },
            "share_slack": self.share_slack,
        }

    def redact(self, text: str) -> str:
        """*text* with the page key replaced by `<key>`."""
        return text.replace(self.page_key, "<key>")

    def note_reject(self, status: int, route: str, reason: str) -> None:
        """Log a refused request, at most once a minute per status code, so a
        scanner hitting the port cannot flood activity.log."""
        route = self.redact(route)
        if route.startswith(FULL_WALKTHROUGH_PREFIX):
            route = FULL_WALKTHROUGH_PREFIX + "<key>/<slug>"
        now = time.monotonic()
        with self._lock:
            last = self._rejects.get(status)
            if last is not None and now - last < REJECT_LOG_INTERVAL:
                return
            self._rejects[status] = now
        log.warning("refused %s %s: %s", status, route, reason)
        log_event(
            COMPONENT,
            "rejected_request",
            f"refused request: {reason}",
            session_id=self.session_id,
            level="WARNING",
            status=status,
            route=route,
        )


def checks_beside(ff: FindingsFile, review_dir: Path) -> ChecksFile | None:
    """`<review_dir>/checks.json` when it exists, validates against checks.v1 and
    names the same commits as *ff*. A missing file is normal (a review written
    before multiplai-dev 0.26); an invalid or mismatched one logs a warning."""
    path = review_dir / "checks.json"
    if not path.is_file():
        return None
    try:
        checks = load_checks(path)
    except (OSError, ValidationError) as exc:
        log.warning(
            "ignoring %s: not a valid checks.json v1: %s",
            path,
            str(exc).splitlines()[0],
        )
        return None
    t, c = ff.target, checks.target
    if (c.base_sha, c.head_sha) != (t.base_sha, t.head_sha):
        log.warning(
            "ignoring %s: it describes %s..%s, not the reviewed commits",
            path,
            c.base_sha[:10],
            c.head_sha[:10],
        )
        return None
    return checks


class PathsStatus(NamedTuple):
    """What `inspect_paths` made of `<review_dir>/paths.json`. `result` is set
    only when `status` is accepted; `detail` says why a file was not."""

    status: Literal["accepted", "stale", "invalid", "absent"]
    result: PathsResult | None
    detail: str


def inspect_paths(review_dir: Path, head_sha: str | None) -> PathsStatus:
    """Judge `<review_dir>/paths.json`, stopping at the first failure: absent;
    unreadable, not JSON, or no string `commit` (invalid); a `commit` that is
    not exactly the review's full `head_sha` (stale: no prefix, case or
    base_sha match); not paths.v1 (invalid, with the first error only)."""
    path = review_dir / "paths.json"
    if not path.is_file():
        return PathsStatus("absent", None, "")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return PathsStatus("invalid", None, str(exc))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return PathsStatus("invalid", None, f"parse failure: {exc}")
    commit = data.get("commit") if isinstance(data, dict) else None
    if not isinstance(commit, str):
        return PathsStatus("invalid", None, "no commit")
    if not head_sha or commit != head_sha:
        return PathsStatus("stale", None, f"commit={commit} head_sha={head_sha}")
    try:
        result = PathsResult.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "(top level)"
        return PathsStatus("invalid", None, f"{where}: {first['msg']}")
    return PathsStatus("accepted", result, "")


def paths_beside(review_dir: Path, head_sha: str | None) -> PathsResult | None:
    """The accepted `<review_dir>/paths.json`, else None. A stale or invalid
    file logs one warning; an absent one (an older review, or a tree review
    without --paths) logs nothing. Never raises for a bad file."""
    status, result, detail = inspect_paths(review_dir, head_sha)
    if status in ("stale", "invalid"):
        log.warning(
            "paths.json ignored (%s): %s %s", status, review_dir / "paths.json", detail
        )
    return result


def paths_status_line(status: PathsStatus, path: Path) -> str:
    """The `paths:` line of `summary status`: the verdict the viewer loads
    paths.json by (its Paths tab)."""
    if status.status == "accepted":
        return f"paths: accepted {len(status.result.paths)} ({path})"
    if status.status == "stale":
        commit = status.detail.removeprefix("commit=").rsplit(" head_sha=", 1)[0]
        return f"paths: stale {commit} ({path})"
    if status.status == "invalid":
        return f"paths: invalid ({path})"
    return "paths: none"


def _path_files(result: PathsResult) -> set[str]:
    """The files a page link on a path can open: each path's entry, handler
    (when it has one) and hop files. Not `also_invoked_by` locations, test
    files or patch sites, which the page shows as text."""
    files: set[str] = set()
    for p in result.paths:
        files.add(p.entry.location.path)
        if p.entry.handler is not None:
            files.add(p.entry.handler.path)
        files.update(h.path for h in p.hops)
    return files


def _path_ranges(result: PathsResult, path: str) -> list[tuple[int, int]]:
    """The lines of *path* a path's links point at (entry, handler, hops),
    sorted, for `file_view` to keep in view in a large file."""
    ranges: set[tuple[int, int]] = set()
    for p in result.paths:
        if p.entry.location.path == path:
            ranges.add((p.entry.location.line, p.entry.location.line))
        if p.entry.handler is not None and p.entry.handler.path == path:
            ranges.add((p.entry.handler.line, p.entry.handler.line))
        ranges.update((h.line_start, h.line_end) for h in p.hops if h.path == path)
    return sorted(ranges)


def summarise(state: TargetState) -> dict:
    counts: dict[str, dict[str, int]] = {"severity": {}, "status": {}}
    for f in state.findings.findings:
        counts["severity"][f.severity] = counts["severity"].get(f.severity, 0) + 1
        counts["status"][f.status] = counts["status"].get(f.status, 0) + 1
    t = state.findings.target
    run = state.findings.run.model_dump(mode="json") if state.findings.run else None
    return {"slug": t.slug, "label": t.label, "counts": counts, "run": run}


class _Reject(Exception):
    def __init__(self, status: int, error: str, log_reason: str | None = None):
        super().__init__(error)
        self.status, self.error, self.log_reason = status, error, log_reason


def make_handler(viewer: Viewer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "review-viewer"
        sys_version = ""

        def log_message(self, fmt, *args):  # stdlib access log → DEBUG only
            log.debug("%s %s", self.client_address[0], viewer.redact(fmt % args))

        # --- responses --------------------------------------------------------

        def _send(self, status: int, body: bytes, ctype: str, csp: str = CSP) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(
                status,
                json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        # --- checks -----------------------------------------------------------

        def _check_origin(self) -> None:
            origin = self.headers.get("Origin")
            if origin is None:
                return
            host = self.headers.get("Host", "")
            if urlparse(origin).netloc.lower() != host.lower():
                raise _Reject(403, "forbidden", "foreign origin")

        def _check_token(self) -> None:
            given = self.headers.get("X-Review-Token", "")
            if not hmac.compare_digest(
                given.encode("utf-8"), viewer.token.encode("utf-8")
            ):
                raise _Reject(401, "unauthorized", "bad token")

        def _body(self) -> dict:
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise _Reject(
                    415, "content type must be application/json", "wrong content type"
                )
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _Reject(400, "bad content length")
            if length < 0:
                raise _Reject(400, "bad content length")
            if length > MAX_BODY_BYTES:
                raise _Reject(413, "body too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise _Reject(400, "invalid json")
            if not isinstance(data, dict):
                raise _Reject(400, "expected a json object")
            return data

        def _target(self, slug: str | None) -> TargetState:
            state = viewer.targets.get(slug or "")
            if state is None:
                raise _Reject(404, "unknown target")
            return state

        # --- dispatch ---------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            route = url.path
            try:
                self._check_origin()
                if route.startswith("/api/"):
                    self._check_token()
                    # whoami is how `list`, `serve` and `stop` probe the server;
                    # counting it as activity would keep a server with no open
                    # page alive for as long as the session keeps checking.
                    if route != "/api/whoami":
                        viewer.last_seen = time.monotonic()
                    body = self._body() if method == "POST" else {}
                    return self._api(method, route, parse_qs(url.query), body)
                if method == "GET" and route.startswith(FULL_WALKTHROUGH_PREFIX):
                    return self._full_walkthrough_page(route)
                if method == "GET":
                    return self._static(route)
                raise _Reject(405, "method not allowed")
            except _Reject as rej:
                if rej.log_reason:
                    viewer.note_reject(rej.status, route, rej.log_reason)
                self._json({"error": rej.error}, rej.status)
            except Exception:
                log.error(
                    "unhandled error on %s %s",
                    method,
                    viewer.redact(route),
                    exc_info=True,
                )
                try:
                    self._json({"error": "internal"}, 500)
                except OSError:
                    pass

        def _full_walkthrough_page(self, route: str) -> None:
            """The full walkthrough page, when the key, the slug and the state are right; else 404.
            Not activity: it does not reset the idle timer."""
            key, _, slug = route[len(FULL_WALKTHROUGH_PREFIX) :].partition("/")
            state = viewer.targets.get(slug)
            if state is None or not hmac.compare_digest(
                key.encode("utf-8"), viewer.page_key.encode("utf-8")
            ):
                raise _Reject(404, "not found")
            fw = state.full_walkthrough()
            if fw["state"] != "ready":
                raise _Reject(404, "not found")
            try:
                body = Path(fw["page"]).read_bytes()
            except OSError:
                raise _Reject(404, "not found")
            self._send(200, body, "text/html; charset=utf-8", PAGE_CSP)

        def _static(self, route: str) -> None:
            name = "index.html" if route in ("/", "/index.html") else None
            if route.startswith("/static/"):
                name = route[len("/static/") :]
            if not name or "/" in name or name.startswith("."):
                raise _Reject(404, "not found")
            path = STATIC_DIR / name
            if not path.is_file():
                raise _Reject(404, "not found")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        def _api(self, method: str, route: str, query: dict, body: dict) -> None:
            q = {k: v[0] for k, v in query.items()}
            if method == "GET":
                if route == "/api/whoami":
                    return self._json(viewer.whoami())
                if route == "/api/alive":
                    return self._json({"ok": True})
                if route == "/api/targets":
                    return self._json(
                        {"targets": [summarise(s) for s in viewer.targets.values()]}
                    )
                if route == "/api/poll":
                    state = self._target(q.get("target"))
                    try:
                        since = int(q.get("since", "0"))
                    except ValueError:
                        since = 0
                    rows, n = state.mailbox.read_outbox(since)
                    # The page loads the target again when this differs from what it last saw.
                    mtime = str(state.reload_findings())
                    return self._json(
                        {"answers": rows, "n": n, "findings_mtime": mtime}
                    )
                if route.startswith("/api/targets/"):
                    rest = route[len("/api/targets/") :]
                    if rest.endswith("/file"):
                        state = self._target(rest[: -len("/file")])
                        return self._file(state, q.get("path", ""))
                    if rest.endswith("/definitions"):
                        state = self._target(rest[: -len("/definitions")])
                        return self._definitions(state, q.get("name", ""))
                    if rest.endswith("/export"):
                        return self._export(self._target(rest[: -len("/export")]))
                    if rest.endswith("/pr"):
                        return self._json(
                            self._target(rest[: -len("/pr")]).refresh_pr()
                        )
                    if rest.endswith("/summary"):
                        state = self._target(rest[: -len("/summary")])
                        sm = state.summary()
                        if sm is None:
                            raise _Reject(404, "no summary yet")
                        return self._json(sm.model_dump(mode="json"))
                    if rest.endswith("/full-walkthrough"):
                        state = self._target(rest[: -len("/full-walkthrough")])
                        fw = state.full_walkthrough()
                        return self._json(
                            {
                                "state": fw["state"],
                                "reason": fw["reason"],
                                "kind": fw["kind"],
                                "what": fw["what"],
                                "commit": fw["commit"],
                                "request_id": fw["request_id"],
                                "url": f"{FULL_WALKTHROUGH_PREFIX}{viewer.page_key}/{state.slug}"
                                if fw["state"] == "ready"
                                else None,
                            }
                        )
                    state = self._target(rest)
                    mtime = str(state.reload_findings())
                    return self._json(
                        {
                            "findings": state.findings.model_dump(mode="json"),
                            "findings_mtime": mtime,
                            "files": state.findings.target.files_changed,
                            "repo_files": state.repo_files[:REPO_FILES_MAX],
                            "repo_files_total": len(state.repo_files),
                            "decisions": state.mailbox.read_decisions(),
                            "viewed": state.mailbox.read_viewed(),
                            "questions": [
                                r
                                for r in state.mailbox.read_inbox()
                                if r.get("kind") in ("question", "share")
                            ],
                            "share": {
                                "github": state.pr_number is not None,
                                "pr": state.pr_number,
                                "slack": viewer.share_slack,
                            },
                            "pr": state.pr,
                            # Export zip needs a saved review (its findings.json on disk).
                            "export": (
                                state.mailbox.dir.parent / "findings.json"
                            ).is_file(),
                            "notice": state.notice,
                            "stats": state.stats,
                            "checks": state.checks.model_dump(mode="json")
                            if state.checks
                            else None,
                            "paths": state.paths.model_dump(mode="json")
                            if state.paths is not None
                            else None,
                        }
                    )
            if method == "POST":
                if route == "/api/ask":
                    return self._ask(body)
                if route == "/api/decision":
                    return self._decision(body)
                if route == "/api/viewed":
                    return self._viewed(body)
                if route == "/api/share":
                    return self._share(body)
                if route == "/api/shutdown":
                    reason = body.get("reason")
                    reason = reason if reason in STOP_MESSAGES else "stop"
                    viewer.stop_reason = reason
                    self._json({"ok": True})
                    log_event(
                        COMPONENT,
                        "stop",
                        STOP_MESSAGES[reason],
                        session_id=viewer.session_id,
                        reason=reason,
                    )
                    threading.Thread(target=viewer.httpd.shutdown, daemon=True).start()
                    return
            raise _Reject(404, "not found")

        def _export(self, state: TargetState) -> None:
            """The review folder and its sessions' transcripts as a zip, only when gitleaks finds nothing."""
            review_dir = state.mailbox.dir.parent
            if not (review_dir / "findings.json").is_file():
                raise _Reject(404, "no saved review to export")
            work = Path(tempfile.mkdtemp(prefix="review-export-"))
            try:
                try:
                    zipped = export_zip.build(
                        review_dir,
                        work,
                        producer=state.findings.producer,
                        ledger=export_zip.default_ledger(),
                    )
                except export_zip.GitleaksMissing as exc:
                    raise _Reject(503, str(exc))
                except export_zip.GitleaksFailed as exc:
                    log.warning("export of %s: %s", state.slug, exc)
                    raise _Reject(503, f"the secret scan failed: {exc}")
                except export_zip.Leaks as exc:
                    log.warning(
                        "export of %s refused: gitleaks reported %d finding(s)",
                        state.slug,
                        len(exc.hits),
                    )
                    return self._json(
                        {"error": "gitleaks found possible secrets", "leaks": exc.hits},
                        409,
                    )
                name = export_zip.zip_name(state.slug, state.findings.target.head_sha)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(zipped.stat().st_size))
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{name}"'
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", CSP)
                self.end_headers()
                with zipped.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
            finally:
                shutil.rmtree(work, ignore_errors=True)

        def _file(self, state: TargetState, path: str) -> None:
            try:
                hops = (
                    _path_ranges(state.paths, path) if state.paths is not None else []
                )
                view = file_view(
                    state.findings.target,
                    path,
                    state.findings,
                    state.allowed,
                    extra_ranges=hops,
                )
            except PathNotInReview:
                raise _Reject(404, "path is not part of this review")
            except GitError as exc:
                log.warning("file view failed for %s: %s", path, exc)
                raise _Reject(
                    422, "git could not read this file at the reviewed commits"
                )
            self._json(view.to_dict())

        def _definitions(self, state: TargetState, name: str) -> None:
            if not DEFINITION_NAME_RE.match(name):
                raise _Reject(
                    400,
                    "name must be an identifier: a letter, _ or $, then up to 99 of those or digits",
                )
            try:
                hits = definitions(state.findings.target, name, state.allowed)
            except GitError as exc:
                log.warning("definitions search failed: %s", exc)
                raise _Reject(422, "git could not search the reviewed commit")
            self._json({"name": name, "hits": hits})

        def _ask(self, body: dict) -> None:
            state = self._target(body.get("target"))
            text = body.get("text")
            if not isinstance(text, str) or not (
                1 <= len(text.strip()) and len(text) <= MAX_QUESTION_CHARS
            ):
                raise _Reject(400, f"text must be 1-{MAX_QUESTION_CHARS} characters")
            finding_id = body.get("finding_id")
            if finding_id is not None and not any(
                f.id == finding_id for f in state.findings.findings
            ):
                raise _Reject(404, "unknown finding")
            try:
                anchor = (
                    Anchor.model_validate(body["anchor"])
                    if body.get("anchor")
                    else None
                )
            except ValidationError:
                raise _Reject(400, "invalid anchor")
            explain = body.get("explain") is True
            if explain and anchor is None:
                raise _Reject(400, "explain needs the block's anchor")
            wants_page = body.get("full_walkthrough") is True
            if wants_page:
                if finding_id is not None or anchor is not None or explain:
                    raise _Reject(
                        400,
                        "full_walkthrough goes with no finding_id, anchor or explain",
                    )
                fw = state.full_walkthrough()
                if fw["state"] == "unavailable":
                    raise _Reject(403, fw["reason"])
                if fw["state"] == "ready":
                    raise _Reject(409, "the full walkthrough already exists")
                if fw["state"] == "requested":
                    raise _Reject(
                        409,
                        f"a full walkthrough is already requested ({fw['request_id']})",
                    )
                text = (
                    f"Write the full walkthrough of {fw['what']} at {fw['commit'][:12]}"
                )
            row = InboxRow(
                id=new_question_id(),
                ts=utc_now(),
                target=state.slug,
                kind="question",
                finding_id=finding_id,
                anchor=anchor,
                text=text,
                explain=explain,
                full_walkthrough=wants_page,
            )
            state.mailbox.append_inbox(row)
            about = (
                "the full walkthrough"
                if wants_page
                else f"block {anchor.path}:{anchor.line_start}"
                if explain
                else f"finding {finding_id}"
                if finding_id
                else (f"{anchor.path}:{anchor.line_start}" if anchor else "the review")
            )
            log_event(
                COMPONENT,
                "question",
                f"question {row.id} on {about}",
                session_id=viewer.session_id,
                target=state.slug,
                finding_id=finding_id,
                chars=len(text),
            )
            self._json({"id": row.id})

        def _decision(self, body: dict) -> None:
            state = self._target(body.get("target"))
            finding_id = body.get("finding_id")
            if not any(f.id == finding_id for f in state.findings.findings):
                raise _Reject(404, "unknown finding")
            decision = body.get("decision")
            if decision not in ("accept", "reject", "defer"):
                raise _Reject(400, "decision must be accept, reject or defer")
            note = body.get("note") or ""
            if not isinstance(note, str) or len(note) > MAX_QUESTION_CHARS:
                raise _Reject(400, "invalid note")
            entry = state.mailbox.write_decision(finding_id, decision, note)
            row = InboxRow(
                id=new_question_id(),
                ts=entry.ts,
                target=state.slug,
                kind="decision",
                finding_id=finding_id,
                text=note,
                decision=decision,
            )
            state.mailbox.append_inbox(row)
            past = {"accept": "accepted", "reject": "rejected", "defer": "deferred"}[
                decision
            ]
            log_event(
                COMPONENT,
                "decision",
                f"finding {finding_id} {past}",
                session_id=viewer.session_id,
                target=state.slug,
                finding_id=finding_id,
                decision=decision,
            )
            self._json({"id": row.id, "decision": entry.model_dump(mode="json")})

        def _share(self, body: dict) -> None:
            """The page's Send button: ask the session to post a finding to the
            PR or to Slack. Nothing is posted here; the row goes to the inbox."""
            state = self._target(body.get("target"))
            finding_id = body.get("finding_id")
            finding = next(
                (f for f in state.findings.findings if f.id == finding_id), None
            )
            if finding is None:
                raise _Reject(404, "unknown finding")
            to, where, text = body.get("to"), body.get("where"), body.get("text")
            if to not in ("github", "slack"):
                raise _Reject(400, "to must be github or slack")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > SHARE_TEXT_MAX
            ):
                raise _Reject(400, f"text must be 1-{SHARE_TEXT_MAX} characters")
            if to == "github":
                if state.pr_number is None:
                    raise _Reject(403, "this review is not of a PR")
                if where not in ("pr", "line"):
                    raise _Reject(400, "where must be pr or line")
                if (
                    where == "line"
                    and finding.file not in state.findings.target.files_changed
                ):
                    raise _Reject(400, "a line comment needs a file the PR changes")
            else:
                if not viewer.share_slack:
                    raise _Reject(
                        403, "the Slack skill is not installed in this session"
                    )
                if not isinstance(where, str) or not SHARE_RECIPIENT.match(
                    where.strip()
                ):
                    raise _Reject(400, "name one person or #channel, on one line")
                where = where.strip()
            row = InboxRow(
                id=new_question_id(),
                ts=utc_now(),
                target=state.slug,
                kind="share",
                finding_id=finding_id,
                text=text,
                to=to,
                where=where,
            )
            try:
                state.mailbox.append_inbox(row)
            except MailboxError:
                raise _Reject(400, "the text is too large to send")
            dest = (
                f"PR #{state.pr_number}" + (" line" if where == "line" else "")
                if to == "github"
                else "Slack"
            )
            log_event(
                COMPONENT,
                "share",
                f"share {row.id}: finding {finding_id} to {dest}",
                session_id=viewer.session_id,
                target=state.slug,
                finding_id=finding_id,
                to=to,
                chars=len(text),
            )
            self._json({"id": row.id})

        def _viewed(self, body: dict) -> None:
            state = self._target(body.get("target"))
            path = body.get("path")
            if (
                not isinstance(path, str)
                or path not in state.findings.target.files_changed
            ):
                raise _Reject(404, "path is not a changed file")
            viewed = body.get("viewed")
            if not isinstance(viewed, bool):
                raise _Reject(400, "viewed must be true or false")
            self._json({"viewed": state.mailbox.set_viewed(path, viewed)})

    return Handler


def build_viewer(
    findings: list[tuple[FindingsFile, Path]],
    *,
    agent: str,
    session_id: str,
    idle_minutes: float,
    meta: dict[str, dict] | None = None,
    share_slack: bool = False,
) -> Viewer:
    """`meta` maps a target slug to `{"pr", "notice", "stats"}`: PR metadata
    for the page header, the note that a review exists for other commits, and
    the measured badges (`stats.change_stats`). None goes into findings.json."""
    targets: dict[str, TargetState] = {}
    for ff, box in findings:
        mailbox = Mailbox(box)
        mailbox.create()
        extra = (meta or {}).get(ff.target.slug, {})
        try:
            listed = repo_files(ff.target)
        except GitError as exc:
            log.warning("cannot list the files at %s: %s", ff.target.head_sha[:12], exc)
            listed = []
        paths = paths_beside(Path(box).parent, ff.target.head_sha)
        allowed = allowed_paths(
            ff.target, ff, listed, extra=_path_files(paths) if paths is not None else ()
        )
        targets[ff.target.slug] = TargetState(
            ff,
            mailbox,
            allowed,
            pr=extra.get("pr"),
            notice=extra.get("notice"),
            stats=extra.get("stats"),
            checks=checks_beside(ff, Path(box).parent),
            paths=paths,
            repo_files=listed,
            pr_number=pr_number_of(extra, Path(box).parent),
        )
        on_disk = Path(box).parent / "findings.json"
        if on_disk.is_file():
            state = targets[ff.target.slug]
            state.findings_path = on_disk
            state.findings_mtime = on_disk.stat().st_mtime_ns
    return Viewer(
        targets=targets,
        token=secrets.token_urlsafe(32),
        agent=agent,
        session_id=session_id,
        idle_minutes=idle_minutes,
        share_slack=share_slack,
    )


def pr_number_of(meta: dict, review_dir: Path) -> int | None:
    """The PR a target is of: the one serve resolved, else the `target.pr` the
    review skill recorded in review-state.json beside findings.json."""
    pr = meta.get("pr")
    if isinstance(pr, dict) and isinstance(pr.get("number"), int):
        return pr["number"]
    try:
        n = json.loads((review_dir / "review-state.json").read_text(encoding="utf-8"))[
            "target"
        ]["pr"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


def bind(
    viewer: Viewer, host: str, first_port: int, span: int
) -> ViewerHTTPServer | None:
    handler = make_handler(viewer)
    for port in range(first_port, first_port + span):
        try:
            viewer.httpd = ViewerHTTPServer((host, port), handler)
            return viewer.httpd
        except OSError:
            continue
    return None


def open_page_html(url: str, token: str) -> str:
    target = escape(f"{url}?t={token}", quote=True)
    return (
        '<!doctype html>\n<meta charset="utf-8">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f'<meta http-equiv="refresh" content="0; url={target}">\n'
        "<title>Opening review viewer…</title>\n"
        f'<p><a href="{target}" rel="noreferrer">Open the review viewer</a></p>\n'
    )


def publish(viewer: Viewer, urls: list[tuple[str, str]]) -> None:
    """Write the token file, the tokenised redirect page and the server record
    into every mailbox, and create an empty inbox so `tail -F` has a file."""
    record = {
        "url_path_only": "/",
        "port": viewer.port,
        "pid": os.getpid(),
        "session_id": viewer.session_id,
        "started": viewer.started,
        "targets": list(viewer.targets),
    }
    for state in viewer.targets.values():
        box = state.mailbox
        write_private(box.token_file, viewer.token + "\n")
        write_private(box.open_html, open_page_html(urls[0][0], viewer.token))
        write_private(box.server_json, json.dumps(record, indent=2) + "\n")
        # What `summary put` checks against. Kept after exit: no secret.
        write_private(
            served_path(box.dir),
            Served(state.findings, state.pr, state.notice, state.stats).to_json(),
        )
    registry.register(
        os.getpid(), [s.mailbox.dir.resolve() for s in viewer.targets.values()]
    )


def unpublish(viewer: Viewer) -> None:
    for state in viewer.targets.values():
        for path in (
            state.mailbox.token_file,
            state.mailbox.open_html,
            state.mailbox.server_json,
        ):
            try:
                path.unlink()
            except OSError:
                pass
    registry.unregister(os.getpid())


def watchdog(viewer: Viewer) -> None:
    """Stop the server when no page has been open for `idle_minutes`.

    The page sends a heartbeat (`/api/alive`) every 10 s while it is open, so
    reading for half an hour without clicking keeps it alive, and closing the
    tab stops it. A browser cannot reliably announce that a tab closed, so the
    silence is the signal.
    """
    idle_s = viewer.idle_minutes * 60
    step = max(0.05, min(30.0, idle_s / 4))
    while not viewer.stopped.wait(step):
        quiet = time.monotonic() - viewer.last_seen
        if quiet >= idle_s:
            viewer.stop_reason = "idle"
            minutes = viewer.idle_minutes
            span = f"{minutes:g} min"
            log.info("no request for %.0f s; stopping", quiet)
            log_event(
                COMPONENT,
                "idle_stop",
                f"viewer stopped after {span} with no open page",
                session_id=viewer.session_id,
                idle_minutes=minutes,
            )
            viewer.httpd.shutdown()
            return


def run(viewer: Viewer) -> None:
    """Serve until shutdown, idle timeout or a signal; always clean up."""
    assert viewer.httpd is not None
    if viewer.idle_minutes > 0:
        threading.Thread(target=watchdog, args=(viewer,), daemon=True).start()
    try:
        viewer.httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        viewer.stop_reason = viewer.stop_reason or "signal"
        log_event(
            COMPONENT,
            "stop",
            "viewer stopped by a signal",
            session_id=viewer.session_id,
            reason="signal",
        )
    finally:
        viewer.stopped.set()
        viewer.httpd.server_close()
        unpublish(viewer)
        log.info("stopped (%s)", viewer.stop_reason or "unknown")
