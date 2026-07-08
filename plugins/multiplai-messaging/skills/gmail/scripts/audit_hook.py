#!/usr/bin/env python3
"""PreToolUse audit hook for the gmail skill.

Fires on every Bash tool call. If the command invokes gmail.py it appends one
line to an append-only audit log: timestamp, verb, and the salient detail
(search query, read id, or — for draft — the reply-to target plus recipient and
subject pulled from the --input JSON file). It NEVER blocks the call and NEVER
raises: any error is swallowed and the tool is allowed, so the audit hook can't
break Gmail access. Non-gmail Bash calls are ignored.

Registered as a plugin hook in ../../hooks/hooks.json (PreToolUse, matcher "Bash").
The log lives with the skill's other state, under the workspace's git-ignored
bucket: $WORKSPACE/.multiplai/data/skills/gmail/audit.log.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_MARKER = "gmail.py"
# Shell control tokens that end the gmail.py argument list.
CONTROL = {"|", "||", "&&", ";", "&", ">", ">>", "<", "2>", "2>>"}


def _skill_state_dir() -> Path:
    ws = os.environ.get("WORKSPACE")
    if ws and (Path(ws) / ".multiplai").is_dir():
        return Path(ws) / ".multiplai" / "data" / "skills" / "gmail"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "multiplai-messaging" / "gmail"


def _write(line: str) -> None:
    path = _skill_state_dir() / "audit.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # never let logging failure affect the tool call


def _gmail_args(tokens: list) -> list | None:
    """Return the tokens belonging to the first gmail.py invocation."""
    for i, tok in enumerate(tokens):
        if tok.endswith(SCRIPT_MARKER):
            args = []
            for t in tokens[i + 1:]:
                if t in CONTROL:
                    break
                args.append(t)
            return args
    return None


def _opt(args: list, name: str) -> str | None:
    """Value of --name x or --name=x from a flat arg list."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _draft_recipient_subject(args: list) -> tuple:
    """Best-effort (to, subject) for a draft, read from the --input JSON file."""
    input_file = _opt(args, "--input")
    if not input_file:
        return ("(payload via stdin — not captured)", "(payload via stdin)")
    try:
        with open(input_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return (str(payload.get("to", "") or "(none)"),
                str(payload.get("subject", "") or "(none)"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ("(unreadable --input)", "(unreadable --input)")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command", "")
    if SCRIPT_MARKER not in command:
        return 0

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    args = _gmail_args(tokens)
    if not args:
        return 0

    verb = args[0] if args else "?"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session = data.get("session_id", "?")

    if verb == "search":
        # First non-flag token after the verb is the query.
        query = next((a for a in args[1:] if not a.startswith("-")), "")
        detail = f"query={query!r}"
    elif verb == "read":
        mid = next((a for a in args[1:] if not a.startswith("-")), "")
        detail = f"message_id={mid}"
    elif verb == "draft":
        reply_to = _opt(args, "--reply-to") or "none"
        to, subject = _draft_recipient_subject(args)
        detail = f"reply_to={reply_to} to={to!r} subject={subject!r}"
    else:
        detail = f"args={' '.join(args[1:])}"

    _write(f"{ts}\tsession={session}\tverb={verb}\t{detail}")
    return 0


if __name__ == "__main__":
    # Always allow the tool; the hook is audit-only.
    sys.exit(main())
