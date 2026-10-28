"""The two deadlines of a waiting stop (1-4), checked on every status refresh
and once a minute by the server.

- Past `ack_by` (`ack_timeout_minutes`) with nobody acknowledging: a reminder
  in the stop's Slack thread.
- Past `decide_at` (`decide_timeout_minutes`) with nobody acknowledging or
  approving: a `timeout` row in the inbox. The session decides the open
  questions, runs `record decided`, then resumes. An acknowledgement holds
  the wait: `status` sets no `decide_at` for an acknowledged stop.

Each deadline fires once per `(change, stop, revision)`, recorded in
`<box>/deadlines.json` right after it fires. A deadline that passed while
the server was down fires once at the next check (row S1). A stop reached
again at a new revision has a new key and fires afresh; the old record stays
(row S7).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from viewer_kit.mailbox import Mailbox, atomic_write, new_question_id, utc_now

from .config import ViewerConfig
from .models import ChangeStatus, InboxRow
from .slack import Slack, key
from .status import parse_utc

log = logging.getLogger(__name__)

DEADLINES_FILE = "deadlines.json"
_LOCK = threading.Lock()


def load(box: Path) -> dict:
    try:
        data = json.loads((Path(box) / DEADLINES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(box: Path, data: dict) -> None:
    atomic_write(
        Path(box) / DEADLINES_FILE, json.dumps(data, indent=2, sort_keys=True) + "\n"
    )


def check(
    statuses: Iterable[ChangeStatus],
    cfg: ViewerConfig,
    mailbox: Mailbox,
    slack: Slack | None,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Fire every due deadline; returns (kind, key) for each one fired."""
    now = now or datetime.now(UTC)
    fired: list[tuple[str, str]] = []
    with _LOCK:
        data = load(mailbox.dir)
        for status in statuses:
            for s in status.stops[:4]:
                if s.state != "waiting":
                    continue
                k = key(status.name, s.n, s.revision)
                entry = data.setdefault(k, {})
                minutes = cfg.stop(s.n)
                ack_by, decide_at = parse_utc(s.ack_by), parse_utc(s.decide_at)
                if ack_by and now >= ack_by and "reminded" not in entry:
                    text = (
                        f"Reminder: nobody has acknowledged stop {s.n} ({s.label}) of "
                        f"*{status.name}* in {minutes.ack_timeout_minutes} minutes. "
                        f"Waiting on: {', '.join(s.waiting_on) or 'nobody listed'}."
                    )
                    outcome = (
                        slack.reply(status.name, s.n, s.revision, text)[0]
                        if slack
                        else "none"
                    )
                    entry["reminded"] = utc_now()
                    entry["reminder_outcome"] = outcome
                    _save(mailbox.dir, data)
                    fired.append(("reminder", k))
                if decide_at and now >= decide_at and "timeout" not in entry:
                    row = InboxRow(
                        id=new_question_id(),
                        ts=utc_now(),
                        kind="timeout",
                        change=status.name,
                        stop=s.n,
                        revision=s.revision,
                        text=(
                            f"No reply in {minutes.decide_timeout_minutes} minutes at stop "
                            f"{s.n} ({s.label}). Decide its open questions, run `record "
                            f"decided`, then resume."
                        ),
                    )
                    mailbox.append_inbox(row)
                    entry["timeout"] = row.id
                    entry["timeout_at"] = row.ts
                    _save(mailbox.dir, data)
                    fired.append(("timeout", k))
                if not entry:
                    del data[k]
    for kind, k in fired:
        log.info("deadline %s fired for %s", kind, k)
    return fired
