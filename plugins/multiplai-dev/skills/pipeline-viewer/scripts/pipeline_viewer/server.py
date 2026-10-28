"""The HTTP server between the page, the repo and the Claude Code session.

It never calls a model. It serves the page (`static/`), answers the
read-only routes from `status.py` and git, writes decision commits through
`record.py`, and carries messages: each ask and write appends a row to
`<box>/inbox.jsonl`, which the session watches; the session's answers in
`outbox.jsonl` reach the page through `/api/poll`. `API.md` is the contract.

Every `/api/*` request names a person: `X-Review-Token` must hold a token in
`<box>/people.json` (`viewer_kit.request_checks.check_person`). The file is
re-read when it changes, so `invite` needs no restart. No token is printed,
logged or written anywhere but people.json and the invite files.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from multiplai_core.log_utils import log_event
from viewer_kit.mailbox import (
    Mailbox,
    MailboxError,
    new_question_id,
    utc_now,
    write_private,
)
from viewer_kit.people import PEOPLE_FILE, PeopleError, Person, load_people
from viewer_kit.request_checks import Reject, check_origin, check_person, read_json_body

from . import config as config_mod
from . import deadlines, gitio, record, status
from .models import ChangeStatus, InboxRow
from .places import Place, list_changes
from .slack import Slack

log = logging.getLogger(__name__)

COMPONENT = "pipeline-viewer"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT = 8000
MAX_SECTION = 200
MAX_TITLE = 120
MAX_DOC_BYTES = 1024 * 1024
MAX_DIFF_BYTES = 1024 * 1024
DEADLINE_INTERVAL_S = 60.0
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SERVER_FILE = "server.json"
CSP = (
    "default-src 'none'; script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' https://cdnjs.cloudflare.com; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class PipelineHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


@dataclass
class Viewer:
    repo: Path
    box: Mailbox
    worktrees_root: Path | None = None
    public_origin: str | None = None
    public_url: str | None = None
    agent: str = "Claude"
    session_id: str = ""
    epic_dir_for: Callable[[Path, str], Path] | None = None
    started: str = field(default_factory=utc_now)
    httpd: PipelineHTTPServer | None = None
    stopped: threading.Event = field(default_factory=threading.Event)
    _people: dict[str, Person] = field(default_factory=dict)
    _people_mtime: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    @property
    def people_path(self) -> Path:
        return self.box.dir / PEOPLE_FILE

    def people(self) -> dict[str, Person]:
        try:
            mtime = self.people_path.stat().st_mtime_ns
        except OSError:
            mtime = None
        with self._lock:
            if mtime != self._people_mtime:
                try:
                    self._people = load_people(self.people_path)
                except PeopleError as exc:
                    log.error(
                        "people.json unreadable, keeping the last good one: %s", exc
                    )
                self._people_mtime = mtime
            return dict(self._people)

    def config(self) -> config_mod.ViewerConfig:
        return config_mod.load(self.repo)

    def slack(self, cfg: config_mod.ViewerConfig) -> Slack:
        return Slack(self.box.dir, cfg.slack_channel)

    def places(self) -> dict[str, Place]:
        return list_changes(self.repo, self.worktrees_root)

    def status_of(self, place: Place, cfg=None, people=None) -> ChangeStatus:
        return status.change_status(
            place, cfg or self.config(), people or self.people(), self.epic_dir_for
        )

    def statuses(self) -> list[ChangeStatus]:
        cfg, people = self.config(), self.people()
        return [self.status_of(p, cfg, people) for p in self.places().values()]

    def run_deadlines(self, statuses: list[ChangeStatus] | None = None) -> None:
        cfg = self.config()
        try:
            deadlines.check(
                statuses if statuses is not None else self.statuses(),
                cfg,
                self.box,
                self.slack(cfg),
            )
        except Exception:
            log.exception("deadline check failed")


def _int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise Reject(400, f"{name} must be a number {lo}-{hi}")
    return value


def _text(body: dict, name: str, limit: int, required: bool = True) -> str | None:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Reject(400, f"{name} must be 1-{limit} characters")
    return value


def _inside(base: Path, rel: str) -> Path:
    """*base*/*rel*, resolved; 403 when it leaves *base* (symlinks included)."""
    if (
        not rel
        or "\x00" in rel
        or rel.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", rel)
    ):
        raise Reject(403, "outside the change")
    root = base.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise Reject(403, "outside the change")
    return target


def make_handler(viewer: Viewer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pipeline-viewer"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.client_address[0], fmt % args)

        def _send(self, status_code: int, body: bytes, ctype: str) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status_code: int = 200) -> None:
            self._send(
                status_code,
                json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            route = url.path
            try:
                check_origin(self.headers, viewer.public_origin)
                if route.startswith("/api/"):
                    person = check_person(self.headers, viewer.people())
                    body = (
                        read_json_body(self.headers, self.rfile, MAX_BODY_BYTES)
                        if method == "POST"
                        else {}
                    )
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    return self._api(method, route, q, body, person)
                if method == "GET":
                    return self._static(route)
                raise Reject(405, "method not allowed")
            except Reject as rej:
                if rej.log_reason:
                    log.warning("refused %s %s: %s", rej.status, route, rej.log_reason)
                self._json({"error": rej.error}, rej.status)
            except record.Refused as ref:
                self._json({"error": ref.message}, ref.status)
            except Exception:
                log.exception("unhandled error on %s %s", method, route)
                try:
                    self._json({"error": "internal"}, 500)
                except OSError:
                    pass

        def _static(self, route: str) -> None:
            name = "index.html" if route in ("/", "/index.html") else None
            if route.startswith("/static/"):
                name = route[len("/static/") :]
            if not name or "/" in name or name.startswith("."):
                raise Reject(404, "not found")
            path = STATIC_DIR / name
            if not path.is_file():
                raise Reject(404, "not found")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        # --- routes -----------------------------------------------------------

        def _place(self, name) -> Place:
            if not isinstance(name, str) or not name:
                raise Reject(400, "name is required")
            place = viewer.places().get(name)
            if place is None:
                raise Reject(404, f"unknown change {name}")
            return place

        def _api(
            self, method: str, route: str, q: dict, body: dict, person: Person
        ) -> None:
            gets = {
                "/api/whoami": lambda: self._whoami(person),
                "/api/changes": self._changes,
                "/api/change": lambda: self._json(
                    viewer.status_of(self._place(q.get("name"))).model_dump(mode="json")
                ),
                "/api/doc": lambda: self._doc(q),
                "/api/diff": lambda: self._diff(q),
                "/api/poll": lambda: self._poll(q),
            }
            posts = {
                "/api/ask": lambda: self._ask(body, person),
                "/api/comment": lambda: self._write("comment", body, person),
                "/api/ack": lambda: self._write("ack", body, person),
                "/api/approve": lambda: self._write("approve", body, person),
                "/api/rework": lambda: self._write("rework", body, person),
                "/api/push": lambda: self._push(body),
            }
            table = gets if method == "GET" else posts
            other = posts if method == "GET" else gets
            if route in table:
                return table[route]()
            if route in other:
                raise Reject(405, "method not allowed")
            raise Reject(404, "not found")

        def _whoami(self, person: Person) -> None:
            self._json(
                {
                    "person": person.model_dump(mode="json"),
                    "agent": viewer.agent,
                    "repo": viewer.repo.name,
                    "started": viewer.started,
                    "public_url": viewer.public_url,
                }
            )

        def _changes(self) -> None:
            statuses = viewer.statuses()
            viewer.run_deadlines(statuses)
            self._json(
                {
                    "changes": [s.model_dump(mode="json") for s in statuses],
                    "generated_at": utc_now(),
                }
            )

        def _doc(self, q: dict) -> None:
            place = self._place(q.get("name"))
            rel = q.get("path") or ""
            dirs = [place.read_dir()] + [c.change_dir(place.name) for c in place.copies]
            for d in dict.fromkeys(dirs):
                target = _inside(d, rel)
                if target.is_file():
                    size = target.stat().st_size
                    if size > MAX_DOC_BYTES:
                        raise Reject(413, "document too large")
                    text = target.read_bytes().decode("utf-8", errors="replace")
                    return self._json(
                        {"name": place.name, "path": rel, "text": text, "bytes": size}
                    )
            raise Reject(404, f"no {rel} in {place.name}")

        def _diff(self, q: dict) -> None:
            place = self._place(q.get("name"))
            commit = q.get("commit") or ""
            if not SHA_RE.match(commit):
                raise Reject(400, "commit must be 7-40 hex characters")
            build, _ = status.build_state(place)
            b = place.build()
            base = (build or {}).get("base_sha")
            if b is None or not base:
                raise Reject(404, f"no build for {place.name}")
            cwd = b.checkout
            full = gitio.ok(
                cwd, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"
            )
            if not full:
                raise Reject(404, "unknown commit")
            on_branch = (
                full != gitio.ok(cwd, "rev-parse", f"{base}^{{commit}}")
                and gitio.ok(cwd, "merge-base", "--is-ancestor", base, full) is not None
                and gitio.ok(cwd, "merge-base", "--is-ancestor", full, "HEAD")
                is not None
            )
            if not on_branch:
                raise Reject(403, "outside the change")
            meta = gitio.run(
                cwd, "show", "-s", "--format=%H%x1f%an%x1f%cI%x1f%s", full
            ).stdout
            sha, author, date, subject = (
                meta.strip().split("\x1f") + ["", "", "", ""]
            )[:4]
            names = gitio.run(
                cwd, "show", "--format=", "--name-status", "--no-renames", full
            ).stdout.splitlines()
            files = [
                {"path": line.split("\t", 1)[1], "status": line.split("\t", 1)[0]}
                for line in names
                if "\t" in line
            ]
            diff = gitio.run(
                cwd,
                "show",
                "--format=",
                "--patch",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                full,
            ).stdout
            raw = diff.encode("utf-8")
            truncated = len(raw) > MAX_DIFF_BYTES
            if truncated:
                diff = raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
            self._json(
                {
                    "commit": sha,
                    "subject": subject,
                    "author": author,
                    "date": status.iso_utc(date) or date,
                    "files": files,
                    "diff": diff,
                    "truncated": truncated,
                }
            )

        def _poll(self, q: dict) -> None:
            try:
                since = int(q.get("since", "0"))
            except ValueError:
                since = 0
            rows, n = viewer.box.read_outbox(since)
            self._json({"answers": rows, "n": n})

        def _row(
            self,
            kind,
            name,
            person,
            *,
            stop=None,
            doc=None,
            section=None,
            text="",
            revision=None,
            commit=None,
        ) -> InboxRow:
            row = InboxRow(
                id=new_question_id(),
                ts=utc_now(),
                kind=kind,
                change=name,
                stop=stop,
                doc=doc,
                section=section,
                text=text,
                revision=revision,
                commit=commit,
                author=person,
            )
            try:
                viewer.box.append_inbox(row)
            except MailboxError as exc:
                raise Reject(413, str(exc)) from None
            log_event(
                COMPONENT,
                kind,
                f"{kind} {row.id} on {name}" + (f" stop {stop}" if stop else ""),
                session_id=viewer.session_id,
                change=name,
                stop=stop,
                chars=len(text),
            )
            return row

        def _ask(self, body: dict, person: Person) -> None:
            place = self._place(body.get("name"))
            stop = body.get("stop")
            if stop is not None:
                stop = _int(stop, "stop", 1, 5)
            doc = _text(body, "doc", 500, required=False)
            section = _text(body, "section", MAX_SECTION, required=False)
            text = _text(body, "text", MAX_TEXT)
            row = self._row(
                "ask",
                place.name,
                person,
                stop=stop,
                doc=doc,
                section=section,
                text=text,
            )
            self._json({"id": row.id})

        def _write(self, kind: str, body: dict, person: Person) -> None:
            place = self._place(body.get("name"))
            n = _int(body.get("stop"), "stop", 1, 5)
            cfg = viewer.config()
            roles = cfg.stop(n).roles

            def fresh() -> ChangeStatus:
                return viewer.status_of(place, cfg)

            doc = section = None
            text = ""
            if kind == "comment":
                doc = _text(body, "doc", 500)
                section = _text(body, "section", MAX_SECTION)
                text = _text(body, "text", MAX_TEXT)
                res = record.comment(place, fresh, person, n, doc, section, text)
            elif kind == "ack":
                res = record.acknowledge(place, fresh, person, n, roles)
            elif kind == "approve":
                res = record.approve(place, fresh, person, n, roles)
            else:
                text = _text(body, "text", MAX_TEXT)
                title = _text(body, "title", MAX_TITLE, required=False)
                title = title or f"Rework requested at stop {n} by {person.name}"
                res = record.rework(place, fresh, person, n, roles, title, text)
            revision = fresh().stops[n - 1].revision
            row = self._row(
                kind,
                place.name,
                person,
                stop=n,
                doc=doc,
                section=section,
                text=text,
                revision=revision,
                commit=res.commit,
            )
            if kind in ("ack", "approve", "rework") and not res.already:
                said = {
                    "ack": f"{person.name} is reviewing stop {n}.",
                    "approve": f"{person.name} approved stop {n}.",
                    "rework": f"{person.name} asked for rework at stop {n}.",
                }[kind]
                viewer.slack(cfg).reply(place.name, n, revision, said)
            self._json({"id": row.id, **res.to_json()})

        def _push(self, body: dict) -> None:
            place = self._place(body.get("name"))
            res = record.push(place)
            self._json(
                {
                    "pushed": res.pushed,
                    "unpushed": res.unpushed,
                    "push_error": res.push_error,
                }
            )

    return Handler


def bind(
    viewer: Viewer, host: str, first_port: int, span: int
) -> PipelineHTTPServer | None:
    handler = make_handler(viewer)
    for port in range(first_port, first_port + span):
        try:
            viewer.httpd = PipelineHTTPServer((host, port), handler)
            return viewer.httpd
        except OSError:
            continue
    return None


def server_record(viewer: Viewer, url: str) -> dict:
    return {
        "url": url,
        "public_url": viewer.public_url,
        "port": viewer.port,
        "pid": os.getpid(),
        "session_id": viewer.session_id,
        "started": viewer.started,
        "repo": str(viewer.repo),
    }


def publish(viewer: Viewer, url: str) -> None:
    write_private(
        viewer.box.dir / SERVER_FILE,
        json.dumps(server_record(viewer, url), indent=2) + "\n",
    )


def deadline_loop(viewer: Viewer) -> None:
    """Check deadlines at start (row S1: a deadline missed while down fires
    once now) and then every minute."""
    while True:
        viewer.run_deadlines()
        if viewer.stopped.wait(DEADLINE_INTERVAL_S):
            return


def run(viewer: Viewer) -> None:
    assert viewer.httpd is not None
    threading.Thread(target=deadline_loop, args=(viewer,), daemon=True).start()
    try:
        viewer.httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log_event(
            COMPONENT,
            "stop",
            "viewer stopped by a signal",
            session_id=viewer.session_id,
        )
    finally:
        viewer.stopped.set()
        viewer.httpd.server_close()
        try:
            (viewer.box.dir / SERVER_FILE).unlink()
        except OSError:
            pass
        log.info("stopped")
