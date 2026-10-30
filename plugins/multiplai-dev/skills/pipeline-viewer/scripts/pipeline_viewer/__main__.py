"""pipeline-viewer command line.

    python -m pipeline_viewer [--session-id ID] [--agent NAME] <command> ...

    serve     serve the page for one repo (--repo <abs>) until stopped
    invite    give a person a personal link (written to a 0600 file, never printed)
    reply     send the session's answer to a row from the page
    pending   print the inbox rows that have no final answer yet, fenced
    watch     print new inbox rows as they come, fenced (the session's Monitor)
    notify    post a stop's Slack message (once per docs revision)
    post      post a message from stdin once per --key (buildme's epic watch)
    record    record decided: the agent's decision at a stop after the timeout

Every command finds the box (the mailbox directory: inbox, outbox,
people.json, deadlines.json, notified.json, invites/) from `--box`, or from
`--repo` as `<data_dir>/pipeline-viewer/<repo name>-<hash of its path>/`.

stdout is a contract: `serve` prints its `url:` lines, `box:`, `monitor:`
and `pending:` lines, then nothing per request. No command prints a token.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import signal
import socket
import sys
from pathlib import Path

from multiplai_core.log_utils import log_event, setup_logging
from viewer_kit import netinfo
from viewer_kit.mailbox import (
    MAX_ROW_BYTES,
    Mailbox,
    MailboxError,
    utc_now,
    write_private,
)
from viewer_kit.people import PEOPLE_FILE, Person, add_person
from viewer_kit.request_checks import public_origin

from . import buildme, fenced, record, server, status
from . import config as config_mod
from . import questions as questions_mod
from .models import OutboxRow
from .places import list_changes
from .slack import Slack, stop_text

COMPONENT = "pipeline-viewer"
EXIT_USAGE = 2
EXIT_REFUSED = 3
PORT_START = 8790
PORT_SPAN = 20
MONITOR_TIMEOUT_MS = 1800000


def invocation_path(value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    pwd = os.environ.get("PWD")
    base = Path(pwd) if pwd and Path(pwd).is_dir() else Path.cwd()
    return (base / p).resolve()


def data_dir() -> Path:
    from multiplai_core.paths import get_paths

    return Path(get_paths().data_dir)


def default_box(repo: Path) -> Path:
    digest = hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:8]
    return data_dir() / "pipeline-viewer" / f"{repo.resolve().name}-{digest}"


def default_worktrees_root(repo: Path) -> Path:
    """buildme's and pm-spec's rule: `$WORKSPACE/.worktrees`, else
    `<repo>/../.worktrees`."""
    workspace = os.environ.get("WORKSPACE", "").strip()
    if workspace:
        return Path(workspace).expanduser() / ".worktrees"
    return repo.resolve().parent / ".worktrees"


def _box(args) -> Mailbox | None:
    if getattr(args, "box", None):
        return Mailbox(invocation_path(args.box))
    if getattr(args, "repo", None):
        return Mailbox(default_box(invocation_path(args.repo)))
    print("give --repo <path> or --box <dir>", file=sys.stderr)
    return None


def _server_record(box: Mailbox) -> dict | None:
    try:
        data = json.loads((box.dir / server.SERVER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _alive(rec: dict) -> bool:
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    port = rec.get("port")
    if not isinstance(port, int):
        return False
    try:
        with socket.create_connection((netinfo.probe_host(), port), timeout=1.0):
            return True
    except OSError:
        return False


def _repo_of(args, box: Mailbox) -> Path | None:
    if getattr(args, "repo", None):
        return invocation_path(args.repo)
    rec = _server_record(box) or {}
    return Path(rec["repo"]) if rec.get("repo") else None


# --- serve ------------------------------------------------------------------------


def _monitor_command(box: Mailbox) -> str:
    # `watch`, not a raw `tail -F`: rows carry text other people typed, and
    # the session must only ever see it fenced (fenced.py).
    command = json.dumps(
        f"{shlex.quote(sys.executable)} -m pipeline_viewer watch --box {shlex.quote(str(box.dir))}"
    )
    description = json.dumps("pipeline-viewer rows from the page")
    return f"Monitor(command={command}, description={description}, timeout_ms={MONITOR_TIMEOUT_MS})"


def cmd_serve(args) -> int:
    repo = invocation_path(args.repo)
    if not (repo / ".git").exists():
        print(f"{repo} is not a git checkout", file=sys.stderr)
        return EXIT_USAGE
    try:
        config_mod.load(repo)
    except config_mod.ConfigError as exc:
        print(f"specs/config.yaml: {exc}", file=sys.stderr)
        return EXIT_USAGE
    origin = None
    if args.public_url:
        try:
            origin = public_origin(args.public_url)
        except ValueError as exc:
            print(f"--public-url: {exc}", file=sys.stderr)
            return EXIT_USAGE
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    rec = _server_record(box)
    if rec and _alive(rec):
        print(
            f"pipeline-viewer already serving {rec.get('public_url') or rec.get('url')} "
            f"(pid {rec.get('pid')})",
            flush=True,
        )
        return EXIT_REFUSED
    box.create()
    viewer = server.Viewer(
        repo=repo,
        box=box,
        worktrees_root=invocation_path(args.worktrees_root)
        if args.worktrees_root
        else default_worktrees_root(repo),
        public_origin=origin,
        public_url=origin,
        agent=args.agent,
        session_id=args.session_id,
        epic_dir_for=buildme.epic_dir_for,
    )
    host = netinfo.bind_host()
    if server.bind(viewer, host, args.port, PORT_SPAN) is None:
        print(
            f"no free port between {args.port} and {args.port + PORT_SPAN - 1}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    urls = netinfo.display_urls(viewer.port)
    server.publish(viewer, urls[0][0] if urls else f"http://127.0.0.1:{viewer.port}/")
    log_event(
        COMPONENT,
        "start",
        f"pipeline-viewer started for {repo.name} on port {viewer.port}",
        session_id=args.session_id,
        port=viewer.port,
        host=host,
        public=bool(origin),
    )
    for url, why in urls:
        print(f"url: {url}  ({why}; open it through a personal link from invite)")
    if origin:
        print(f"public: {origin}/")
    print(f"box: {box.dir}")
    if not (box.dir / PEOPLE_FILE).is_file():
        print("people: none yet; run invite for each person")
    print(f"monitor: {_monitor_command(box)}")
    print(
        f"pending: python -m pipeline_viewer pending --box {shlex.quote(str(box.dir))}"
    )
    sys.stdout.flush()
    signal.signal(signal.SIGTERM, _raise_interrupt)
    server.run(viewer)
    return 0


def _raise_interrupt(_signum, _frame):
    raise KeyboardInterrupt


# --- invite -----------------------------------------------------------------------


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "person"


def cmd_invite(args) -> int:
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    base = args.url
    if not base:
        rec = _server_record(box) or {}
        base = rec.get("public_url") or rec.get("url")
    if not base:
        print(
            "no server is recorded for this box; start serve first or pass --url",
            file=sys.stderr,
        )
        return EXIT_USAGE
    box.create()
    try:
        person = Person(
            name=args.name, email=args.email, role=args.role, slack_id=args.slack_id
        )
    except ValueError as exc:
        print(f"invalid person: {exc}", file=sys.stderr)
        return EXIT_USAGE
    token = add_person(box.dir / PEOPLE_FILE, person)
    invites = box.dir / "invites"
    invites.mkdir(mode=0o700, exist_ok=True)
    os.chmod(invites, 0o700)
    path = invites / f"{_slug(person.name)}.url"
    write_private(path, f"{base.rstrip('/')}/?t={token}\n")
    print(f"invite: {path}")
    return 0


# --- reply / pending ----------------------------------------------------------------


def _chunks(text: str, limit: int) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[: limit // 4])
            line = line[limit // 4 :]
        if current and len((current + line).encode("utf-8")) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return [p for p in parts if p]


def cmd_reply(args) -> int:
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    if not any(r.get("id") == args.to for r in box.read_inbox()):
        print(f"no row {args.to!r} in {box.inbox}", file=sys.stderr)
        return EXIT_USAGE
    text = (
        invocation_path(args.file).read_text(encoding="utf-8")
        if args.file
        else sys.stdin.read()
    )
    if not text.strip():
        print("the answer is empty", file=sys.stderr)
        return EXIT_USAGE
    parts = _chunks(text, MAX_ROW_BYTES - 2048)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            box.append_outbox(
                OutboxRow(
                    reply_to=args.to,
                    ts=utc_now(),
                    text=part,
                    done=last and not args.more,
                    commit=args.commit if last else None,
                )
            )
    except MailboxError as exc:
        print(f"could not write the answer: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"{'partial' if args.more else 'final'} answer for {args.to} -> {box.outbox}")
    if not args.more:
        _record_answer(args, box)
    return 0


def _record_answer(args, box: Mailbox) -> None:
    """Put the question and its answer in the stop's review file, in git,
    with any earlier answer on the same change whose commit was refused
    (row S17). The answer is already in the outbox, so a refusal is only
    reported: `RECORD:<id>:<sha>`, `RECORD:SKIPPED:<why>`,
    `RECORD:REFUSED:<id>:<message>` (retried on the next write)."""
    row = next((r for r in box.read_inbox() if r.get("id") == args.to), {})
    if row.get("kind") != "ask":
        return
    if row.get("stop") is None:
        print("RECORD:SKIPPED:the question names no stop")
        return
    args.change = row.get("change")
    if _repo_of(args, box) is None:
        print("RECORD:SKIPPED:give --repo to record the answer in git")
        return
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        found = _status_for(args, box)
    if found is None:
        print(f"RECORD:SKIPPED:{err.getvalue().strip()}")
        return
    place, _cfg, _people, fresh = found
    qs = questions_mod.questions(box.read_inbox(), box.read_outbox(0)[0], place.name)
    agent = getattr(args, "agent", None) or "Claude"
    for qid, res, refused in record.record_answers(place, fresh, qs, agent):
        if refused:
            print(f"RECORD:REFUSED:{qid}:{refused}")
        else:
            print(f"RECORD:{qid}:{res.commit}")
            if res.unpushed:
                print(f"UNPUSHED:{res.push_error or 'push failed'}")


def cmd_pending(args) -> int:
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    if not box.dir.is_dir():
        print(f"no box at {box.dir}", file=sys.stderr)
        return EXIT_USAGE
    for row in box.pending():
        print(fenced.format_row(row))
    return 0


def cmd_watch(args) -> int:
    """Print each row appended to the inbox from now on, fenced, one JSON
    line per row, until stopped. The session's Monitor runs this."""
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    try:
        for row in fenced.follow(box.inbox):
            print(fenced.format_row(row), flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


# --- notify / record ----------------------------------------------------------------


def _status_for(args, box: Mailbox):
    """(viewer-like context) for a change: place, config, people, status fn."""
    repo = _repo_of(args, box)
    if repo is None:
        print(
            "give --repo <path> (no server is recorded for this box)", file=sys.stderr
        )
        return None
    from viewer_kit.people import load_people

    cfg = config_mod.load(repo)
    people = load_people(box.dir / PEOPLE_FILE)
    root = (
        invocation_path(args.worktrees_root)
        if args.worktrees_root
        else default_worktrees_root(repo)
    )
    place = list_changes(repo, root).get(args.change)
    if place is None:
        print(f"no change {args.change!r} in {repo} or its worktrees", file=sys.stderr)
        return None

    def fresh():
        return status.change_status(place, cfg, people, buildme.epic_dir_for)

    return place, cfg, people, fresh


def cmd_notify(args) -> int:
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    found = _status_for(args, box)
    if found is None:
        return EXIT_USAGE
    _place, cfg, people, fresh = found
    st = fresh()
    s = st.stops[args.stop - 1]
    if s.state not in ("waiting", "acknowledged"):
        print(f"SLACK:SKIPPED:stop {args.stop} is {s.state}")
        return 0
    box.create()
    rec = _server_record(box) or {}
    text = stop_text(st, args.stop, people, rec.get("public_url") or rec.get("url"))
    outcome, detail = Slack(box.dir, cfg.slack_channel).notify(
        st.name, args.stop, s.revision, text
    )
    print(f"SLACK:{outcome.upper()}:{detail}")
    return 1 if outcome == "failed" else 0


def cmd_post(args) -> int:
    """Post the message on stdin (or `--file`) once per `--change` and
    `--key`, to the channel and outbox `notify` uses: buildme's `epic watch`
    posts through it when the epic stops."""
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    repo = _repo_of(args, box)
    try:
        cfg = config_mod.load(repo) if repo is not None else None
    except config_mod.ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE
    text = (
        Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    )
    if not text.strip():
        print("ERROR: no message: give it on stdin or with --file", file=sys.stderr)
        return EXIT_USAGE
    box.create()
    outcome, detail = Slack(box.dir, cfg.slack_channel if cfg else None).post_once(
        f"{args.change}|post|{args.key}", text
    )
    print(f"SLACK:{outcome.upper()}:{detail}")
    return 1 if outcome == "failed" else 0


def cmd_record_decided(args) -> int:
    box = _box(args)
    if box is None:
        return EXIT_USAGE
    found = _status_for(args, box)
    if found is None:
        return EXIT_USAGE
    place, cfg, _people, fresh = found
    try:
        res = record.decided(place, fresh, args.stop, args.reason)
    except record.Refused as ref:
        print(ref.message, file=sys.stderr)
        return EXIT_REFUSED
    revision = fresh().stops[args.stop - 1].revision
    if not res.already:
        box.create()
        Slack(box.dir, cfg.slack_channel).reply(
            place.name,
            args.stop,
            revision,
            f"The agent decided stop {args.stop}: {args.reason}",
        )
    print(f"DECIDED:{res.commit}{' (already recorded)' if res.already else ''}")
    if res.unpushed:
        print(f"UNPUSHED:{res.push_error or 'push failed'}")
    return 0


# --- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--session-id", default=argparse.SUPPRESS, help="Claude Code session id"
    )
    common.add_argument(
        "--agent", default=argparse.SUPPRESS, help="who answers (default: Claude)"
    )
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--repo", help="the repository (absolute path)")
    where.add_argument(
        "--box", help="the box directory, instead of the one --repo implies"
    )
    where.add_argument(
        "--worktrees-root",
        help="where worktrees live (default: $WORKSPACE/.worktrees, else beside the repo)",
    )

    ap = argparse.ArgumentParser(
        prog="pipeline_viewer",
        parents=[common],
        description="A shared page for following and answering pm-spec and buildme.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser(
        "serve", parents=[common, where], help="serve the page for one repo"
    )
    s.add_argument(
        "--public-url",
        help="the ngrok (or other) origin people open, e.g. https://x.ngrok.app",
    )
    s.add_argument(
        "--port",
        type=int,
        default=PORT_START,
        help=f"first port to try (default {PORT_START})",
    )
    s.set_defaults(func=cmd_serve)

    i = sub.add_parser(
        "invite", parents=[common, where], help="give a person a personal link"
    )
    i.add_argument("--name", required=True)
    i.add_argument("--email", required=True)
    i.add_argument("--role", required=True, choices=["pm", "engineer"])
    i.add_argument("--slack-id", help="their Slack member id, for @-mentions")
    i.add_argument(
        "--url", help="the base URL of the link (default: the running server's)"
    )
    i.set_defaults(func=cmd_invite)

    r = sub.add_parser(
        "reply", parents=[common, where], help="answer a row from the page"
    )
    r.add_argument("--to", required=True, help="the row id")
    r.add_argument("--file", help="the answer (markdown); default: stdin")
    r.add_argument("--more", action="store_true", help="a partial answer; more follows")
    r.add_argument(
        "--commit", help="the commit the answer made, when it edited a document"
    )
    r.set_defaults(func=cmd_reply)

    p = sub.add_parser(
        "pending", parents=[common, where], help="rows with no final answer"
    )
    p.set_defaults(func=cmd_pending)

    w = sub.add_parser(
        "watch",
        parents=[common, where],
        help="print new rows from the page as they come, fenced (for Monitor)",
    )
    w.set_defaults(func=cmd_watch)

    n = sub.add_parser(
        "notify", parents=[common, where], help="post a stop's Slack message"
    )
    n.add_argument("--change", required=True)
    n.add_argument("--stop", required=True, type=int, choices=[1, 2, 3, 4])
    n.set_defaults(func=cmd_notify)

    po = sub.add_parser(
        "post",
        parents=[common, where],
        help="post a message (stdin or --file) once per --change and --key",
    )
    po.add_argument("--change", required=True)
    po.add_argument("--key", required=True, help="the same key never posts twice")
    po.add_argument("--file", help="the message (default: stdin)")
    po.set_defaults(func=cmd_post)

    rec = sub.add_parser("record", parents=[common], help="record a decision")
    rsub = rec.add_subparsers(dest="what", required=True, metavar="what")
    d = rsub.add_parser(
        "decided", parents=[common, where], help="the agent decided a stop"
    )
    d.add_argument("--change", required=True)
    d.add_argument("--stop", required=True, type=int, choices=[1, 2, 3, 4])
    d.add_argument("--reason", required=True, help='e.g. "no reply in 480 minutes"')
    d.set_defaults(func=cmd_record_decided)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.session_id = getattr(args, "session_id", "")
    if args.session_id in ("", "{session_id}"):
        args.session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    args.agent = getattr(args, "agent", "Claude")
    setup_logging(COMPONENT, session_id=args.session_id, packages=("pipeline_viewer",))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
