"""The mailbox: JSONL files beside the review that carry messages both ways.

    inbox.jsonl     server writes  — questions and decisions from the page
    outbox.jsonl    `reply` writes — the session's answers
    decisions.json  server writes  — latest decision per finding (atomic replace)
    server.json     server writes  — who is serving this mailbox (never the token)

Only one process ever writes each file, and each row is appended with a single
`write()` on an `O_APPEND` descriptor, so no locking is needed. Readers skip
lines that fail to parse: a reader can race a writer mid-line.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Decision, InboxRow, OutboxRow

log = logging.getLogger(__name__)

MAX_ROW_BYTES = 64 * 1024


class MailboxError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_question_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"q-{stamp}-{secrets.token_hex(2)}"


def _append(path: Path, row: dict) -> None:
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_ROW_BYTES:
        raise MailboxError(f"row is {len(data)} bytes; the limit is {MAX_ROW_BYTES}")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


class Mailbox:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self._decisions_lock = threading.Lock()

    @property
    def inbox(self) -> Path:
        return self.dir / "inbox.jsonl"

    @property
    def outbox(self) -> Path:
        return self.dir / "outbox.jsonl"

    @property
    def decisions(self) -> Path:
        return self.dir / "decisions.json"

    @property
    def server_json(self) -> Path:
        return self.dir / "server.json"

    @property
    def token_file(self) -> Path:
        return self.dir / "server.token"

    @property
    def open_html(self) -> Path:
        return self.dir / "open.html"

    def create(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def append_inbox(self, row: InboxRow) -> None:
        _append(self.inbox, row.model_dump(mode="json"))

    def append_outbox(self, row: OutboxRow) -> None:
        _append(self.outbox, row.model_dump(mode="json"))

    def read_inbox(self) -> list[dict]:
        return read_rows(self.inbox)

    def read_outbox(self, since: int = 0) -> tuple[list[dict], int]:
        """Rows after the first `since`, and the total row count."""
        rows = read_rows(self.outbox)
        since = max(0, since)
        return rows[since:], len(rows)

    def read_decisions(self) -> dict[str, dict]:
        try:
            data = json.loads(self.decisions.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_decision(self, finding_id: str, decision: str, note: str = "") -> Decision:
        entry = Decision(decision=decision, note=note, ts=utc_now())
        with self._decisions_lock:
            data = self.read_decisions()
            data[finding_id] = entry.model_dump(mode="json")
            atomic_write(self.decisions, json.dumps(data, indent=2, sort_keys=True) + "\n")
        return entry


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write via a temp file in the same directory, then `os.replace`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_private(path: Path, text: str) -> None:
    """Create or replace a file readable only by its owner (0600)."""
    atomic_write(path, text, mode=0o600)
