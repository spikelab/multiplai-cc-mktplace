"""review-viewer command line.

    python -m review_viewer [--session-id ID] [--agent NAME] <command> ...

    serve          start (or reuse) the viewer for findings files or a diff
    reply          send the session's answer to a question from the page
    list           show the live viewers this user can reach
    stop           stop one viewer (--box) or all of them (--all)
    validate       check findings files against the v1 contract
    export-schema  write the v1 JSON Schema

stdout is a contract: `serve` prints the `open:` line, the reference URLs, the
mailboxes and the Monitor command, then nothing per request. Diagnostics go to
the logger, never to print().
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from multiplai_core.log_utils import log_event, setup_logging
from pydantic import ValidationError

from . import netinfo, registry, server
from .gitdata import GitError, diff_findings, diff_target
from .mailbox import MAX_ROW_BYTES, Mailbox, MailboxError, utc_now
from .models import SCHEMA_PATH, FindingsFile, OutboxRow, load_findings, schema_text

COMPONENT = "review-viewer"
EXIT_USAGE = 2
EXIT_OTHER_SESSION = 3
MONITOR_TIMEOUT_MS = 1800000


def invocation_path(value: str) -> Path:
    """Resolve a user-supplied path against the directory the user ran from.

    `uv run --directory` changes the working directory to this package but
    leaves $PWD at the caller's directory, so relative paths are resolved
    against $PWD when it names a real directory.
    """
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    pwd = os.environ.get("PWD")
    base = Path(pwd) if pwd and Path(pwd).is_dir() else Path.cwd()
    return (base / p).resolve()


def output_root() -> Path:
    """Workspace INBOX/ if there is one, else the directory the user ran from."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        marker = Path(cfg) / ".workspace"
        try:
            root = Path(marker.read_text(encoding="utf-8").strip()).expanduser()
        except OSError:
            root = None
        if root and (root / "INBOX").is_dir():
            return root / "INBOX"
    return invocation_path(".")


# --- serve --------------------------------------------------------------------


def _load_targets(args) -> list[tuple[FindingsFile, Path]] | int:
    loaded: list[tuple[FindingsFile, Path]] = []
    if args.repo or args.range:
        if not (args.repo and args.range) or args.findings:
            print("plain-diff mode takes --repo <path> --range <base>..<head> and no findings files",
                  file=sys.stderr)
            return EXIT_USAGE
        try:
            target = diff_target(invocation_path(args.repo), args.range)
        except (GitError, ValueError) as exc:
            print(f"cannot read the diff: {exc}", file=sys.stderr)
            return EXIT_USAGE
        box = output_root() / "review-viewer" / target.slug / "viewer"
        loaded.append((diff_findings(target), box))
        return loaded
    if not args.findings:
        print("give one or more findings.json files, or --repo <path> --range <base>..<head>",
              file=sys.stderr)
        return EXIT_USAGE
    for raw in args.findings:
        path = invocation_path(raw)
        try:
            ff = load_findings(path)
        except OSError as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except ValidationError as exc:
            print(f"{path} is not a valid findings.json v1:\n{exc}", file=sys.stderr)
            return EXIT_USAGE
        if args.repo_root:
            ff.target.repo_path = str(invocation_path(args.repo_root))
        loaded.append((ff, path.parent / "viewer"))
    slugs = [ff.target.slug for ff, _ in loaded]
    if len(set(slugs)) != len(slugs):
        print(f"target slugs must be unique across files; got {slugs}", file=sys.stderr)
        return EXIT_USAGE
    boxes = [b.resolve() for _, b in loaded]
    if len(set(boxes)) != len(boxes):
        print("two findings files share one directory, so they would share one mailbox; "
              "move one of them", file=sys.stderr)
        return EXIT_USAGE
    return loaded


def _monitor_command(boxes: list[Path], label: str) -> str:
    files = " ".join(str(b / "inbox.jsonl") for b in boxes)
    return (f'Monitor(command="tail -n 0 -q -F {files}", '
            f'description="review-viewer questions and decisions for {label}", '
            f"timeout_ms={MONITOR_TIMEOUT_MS})")


def _print_contract(open_html: Path, urls: list[tuple[str, str]], boxes: list[Path],
                    label: str) -> None:
    print(f"open: file://{open_html}")
    for url, why in urls:
        print(f"url: {url}  ({why}; reference only, needs the link above to authorise)")
    for box in boxes:
        print(f"mailbox: {box}")
    print(f"monitor: {_monitor_command(boxes, label)}", flush=True)


def cmd_serve(args) -> int:
    loaded = _load_targets(args)
    if isinstance(loaded, int):
        return loaded
    boxes = [b.resolve() for _, b in loaded]
    label = ", ".join(ff.target.label for ff, _ in loaded)

    for box in boxes:
        live = registry.existing(box, args.port, registry.PORT_SPAN)
        if not live:
            continue
        owner = live.get("session_id") or ""
        if owner != args.session_id:
            print(f"a viewer for {box} is already running, owned by another session "
                  f"({owner[:8] or 'none'}): {registry.describe(live)}\n"
                  f"to take it over: python -m review_viewer stop --box {box}", flush=True)
            return EXIT_OTHER_SESSION
        served = set(live.get("mailboxes", []))
        missing = [b for b in boxes if str(b) not in served]
        if missing:
            print(f"a viewer is already running for {box} but not for {missing[0]}; "
                  f"stop it first: python -m review_viewer stop --box {box}", file=sys.stderr)
            return EXIT_USAGE
        log_event(COMPONENT, "reuse", f"viewer already running for {', '.join(live['targets'])}; "
                  "reused it", session_id=args.session_id, port=live["port"],
                  owner_session=owner[:8])
        print("viewer already running for this session; reusing it", flush=True)
        _print_contract(box / "open.html", netinfo.display_urls(live["port"]), boxes, label)
        return 0

    viewer = server.build_viewer(loaded, agent=args.agent, session_id=args.session_id,
                                 idle_minutes=args.idle)
    host = netinfo.bind_host()
    last = args.port + registry.PORT_SPAN - 1
    if server.bind(viewer, host, args.port, registry.PORT_SPAN) is None:
        print(f"no free port between {args.port} and {last}. Old viewers may still be "
              "running: python -m review_viewer stop --all", file=sys.stderr)
        return EXIT_USAGE
    urls = netinfo.display_urls(viewer.port)
    server.publish(viewer, urls)
    container = netinfo.detect_container()
    log_event(COMPONENT, "start",
              f"viewer started for {', '.join(viewer.targets)} on port {viewer.port}",
              session_id=args.session_id, port=viewer.port, targets=list(viewer.targets),
              host=host, container=container)
    _print_contract(boxes[0] / "open.html", urls, boxes, label)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    server.run(viewer)
    return 0


def _raise_interrupt(_signum, _frame):
    raise KeyboardInterrupt


# --- reply --------------------------------------------------------------------


def _chunks(text: str, limit: int) -> list[str]:
    """Split an answer so every outbox row stays under the row size limit."""
    def size(s: str) -> int:
        return len(OutboxRow(reply_to="x" * 40, ts=utc_now(), text=s, done=False)
                   .model_dump_json().encode("utf-8"))

    if size(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while size(line) > limit:  # one enormous line: hard-split it
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:4000])
            line = line[4000:]
        if current and size(current + line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return [p for p in parts if p]


def cmd_reply(args) -> int:
    box = Mailbox(invocation_path(args.box))
    if not box.dir.is_dir():
        print(f"no mailbox at {box.dir} (it is the viewer/ directory the server printed)",
              file=sys.stderr)
        return EXIT_USAGE
    question = next((r for r in box.read_inbox() if r.get("id") == args.to), None)
    if question is None:
        print(f"no question {args.to!r} in {box.inbox}; check the id and the --box path",
              file=sys.stderr)
        return EXIT_USAGE
    if args.file:
        text = invocation_path(args.file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("the answer is empty", file=sys.stderr)
        return EXIT_USAGE
    parts = _chunks(text, MAX_ROW_BYTES - 1024)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            box.append_outbox(OutboxRow(reply_to=args.to, ts=utc_now(), text=part,
                                        done=last and not args.more))
    except MailboxError as exc:
        print(f"could not write the answer: {exc}", file=sys.stderr)
        return EXIT_USAGE
    done = not args.more
    log_event(COMPONENT, "reply", f"reply to {args.to} ({'final' if done else 'partial'})",
              session_id=args.session_id, target=question.get("target"), reply_to=args.to,
              chars=len(text), done=done)
    print(f"{'final answer' if done else 'partial answer'} for {args.to} -> {box.outbox}")
    return 0


# --- list / stop ----------------------------------------------------------------


def cmd_list(args) -> int:
    live = registry.scan(args.port, args.span)
    if not live:
        print(f"no live review-viewer found on ports {args.port}-{args.port + args.span - 1}")
        return 0
    print(f"{len(live)} live viewer(s):")
    for who in live:
        print("  " + registry.describe(who))
        for box in who.get("mailboxes", []):
            print(f"    mailbox: {box}")
    return 0


def cmd_stop(args) -> int:
    if args.box:
        box = invocation_path(args.box).resolve()
        live = [w for w in registry.scan(args.port, args.span, [box])
                if str(box) in w.get("mailboxes", [])]
        if not live and not registry.read_token(box):
            print(f"no viewer running for {box} (or its token file is not readable)")
            return 0
    elif args.all:
        live = registry.scan(args.port, args.span)
    else:
        print("stop needs --box <mailbox> or --all", file=sys.stderr)
        return EXIT_USAGE
    if not live:
        print("nothing to stop")
        return 0
    failed = 0
    for who in live:
        token = next((registry.read_token(Path(b)) for b in who.get("mailboxes", [])
                      if registry.read_token(Path(b))), None)
        if token and registry.shutdown(who["port"], token):
            print("stopped: " + registry.describe(who))
        else:
            failed += 1
            print("NOT stopped: " + registry.describe(who))
    return 1 if failed else 0


# --- validate / export-schema -------------------------------------------------------


def cmd_validate(args) -> int:
    failed = False
    for raw in args.files:
        path = invocation_path(raw)
        try:
            ff = load_findings(path)
        except (OSError, ValidationError) as exc:
            failed = True
            print(f"FAIL {path}\n{exc}")
            continue
        print(f"OK {path} ({len(ff.findings)} findings)")
    return 1 if failed else 0


def cmd_export_schema(args) -> int:
    out = invocation_path(args.out) if args.out else SCHEMA_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(schema_text(), encoding="utf-8")
    print(f"wrote {out}")
    return 0


# --- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session-id", default=argparse.SUPPRESS,
                        help="Claude Code session id, for ownership and log correlation")
    common.add_argument("--agent", default=argparse.SUPPRESS,
                        help="who answers, shown in the page header (default: Claude)")

    ap = argparse.ArgumentParser(
        prog="review_viewer", parents=[common],
        description="Local web viewer for code-review findings; questions from the "
                    "page reach the Claude Code session that started it.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("serve", parents=[common], help="start or reuse the viewer")
    s.add_argument("findings", nargs="*", help="findings.json files (v1)")
    s.add_argument("--repo", help="plain-diff mode: the repository")
    s.add_argument("--range", help="plain-diff mode: <base>..<head>")
    s.add_argument("--repo-root", help="read git from here instead of target.repo_path")
    s.add_argument("--port", type=int, default=registry.PORT_START,
                   help=f"first port to try (default {registry.PORT_START}; 20 are tried)")
    s.add_argument("--idle", type=float, default=30.0,
                   help="minutes without a request before the server exits (0 = never)")
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("reply", parents=[common], help="answer a question from the page")
    r.add_argument("--box", required=True, help="the mailbox directory the server printed")
    r.add_argument("--to", required=True, help="the question id")
    r.add_argument("--more", action="store_true",
                   help="a partial answer: the page keeps waiting for the rest")
    r.add_argument("--file", help="read the markdown answer from this file (default: stdin)")
    r.set_defaults(func=cmd_reply)

    ls = sub.add_parser("list", parents=[common], help="list live viewers")
    ls.add_argument("--port", type=int, default=registry.PORT_START)
    ls.add_argument("--span", type=int, default=registry.PORT_SPAN)
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("stop", parents=[common], help="stop a viewer")
    st.add_argument("--box", help="the mailbox of the viewer to stop")
    st.add_argument("--all", action="store_true", help="stop every live viewer")
    st.add_argument("--port", type=int, default=registry.PORT_START)
    st.add_argument("--span", type=int, default=registry.PORT_SPAN)
    st.set_defaults(func=cmd_stop)

    v = sub.add_parser("validate", parents=[common], help="validate findings.json files")
    v.add_argument("files", nargs="+")
    v.set_defaults(func=cmd_validate)

    e = sub.add_parser("export-schema", parents=[common], help="write the v1 JSON Schema")
    e.add_argument("--out", help=f"output path (default: {SCHEMA_PATH.name} in schema/)")
    e.set_defaults(func=cmd_export_schema)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.session_id = getattr(args, "session_id", "")
    args.agent = getattr(args, "agent", "Claude")
    setup_logging(COMPONENT, session_id=args.session_id,
                  propagate_loggers=("review_viewer", "multiplai_core"))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
