"""Stop notices in Slack: one message per (change, stop, docs revision), and
every later event about that stop as a reply in its thread.

The token is `SLACK_TOKEN` from the environment, the one the
`multiplai-messaging:slack` skill uses. Its value is read here only to hand
it to `slack_sdk.WebClient`; it is never printed, logged or written.

Without a token (or without `pipeline_viewer.slack.channel` in
`specs/config.yaml`) nothing is posted: the message is appended to
`<box>/slack-outbox.md` instead, and `notify` prints
`SLACK:SKIPPED:<reason>` and exits 0.

`<box>/notified.json` maps `<change>|<stop>|<revision>` to the thread
(`channel`, `ts`, `at`). It is written after the post succeeds, so a crash
between the two posts a second message on the next `notify` (row S11): one
duplicate in a channel costs less than a lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import slack_sdk
from viewer_kit.mailbox import atomic_write, utc_now

log = logging.getLogger(__name__)

NOTIFIED_FILE = "notified.json"
OUTBOX_FILE = "slack-outbox.md"
TOKEN_ENV = "SLACK_TOKEN"
_LOCK = threading.Lock()


def key(change: str, stop: int, revision: str | None) -> str:
    return f"{change}|{stop}|{revision or ''}"


class Slack:
    def __init__(self, box: Path, channel: str | None):
        self.box = Path(box)
        self.channel = channel

    # --- files -------------------------------------------------------------

    @property
    def notified_path(self) -> Path:
        return self.box / NOTIFIED_FILE

    def notified(self) -> dict:
        try:
            data = json.loads(self.notified_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, k: str, entry: dict) -> None:
        data = self.notified()
        data[k] = entry
        atomic_write(
            self.notified_path, json.dumps(data, indent=2, sort_keys=True) + "\n"
        )

    def _outbox(self, k: str, text: str, why: str) -> None:
        path = self.box / OUTBOX_FILE
        block = f"## {utc_now()} {k} (not posted: {why})\n\n{text.strip()}\n\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, block.encode("utf-8"))
        finally:
            os.close(fd)

    # --- posting -----------------------------------------------------------

    def _why_not(self) -> str | None:
        if not os.environ.get(TOKEN_ENV):
            return "no token"
        if not self.channel:
            return "no channel"
        return None

    def _post(self, text: str, thread_ts: str | None = None) -> str:
        client = slack_sdk.WebClient(token=os.environ[TOKEN_ENV])
        kwargs = {"channel": self.channel, "text": text, "unfurl_links": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = client.chat_postMessage(**kwargs)
        return str(resp["ts"])

    def notify(
        self, change: str, stop: int, revision: str | None, text: str
    ) -> tuple[str, str]:
        """Post the stop's message once per revision. Returns (outcome,
        detail): `posted`/ts, `already`/ts, `skipped`/reason, `failed`/error."""
        return self.post_once(key(change, stop, revision), text)

    def post_once(self, k: str, text: str) -> tuple[str, str]:
        """Post *text* once per key *k* (`notify`, and `post` for buildme's
        `epic watch`), with `notify`'s outcomes."""
        with _LOCK:
            entry = self.notified().get(k)
            if entry:
                return "already", str(entry.get("ts", ""))
            why = self._why_not()
            if why:
                self._outbox(k, text, why)
                return "skipped", why
            try:
                ts = self._post(text)
            except Exception as exc:  # noqa: BLE001 - any Slack failure is reported, never raised
                err = _error(exc)
                log.warning("slack post for %s failed: %s", k, err)
                self._outbox(k, text, err)
                return "failed", err
            self._save(k, {"channel": self.channel, "ts": ts, "at": utc_now()})
            return "posted", ts

    def reply(
        self, change: str, stop: int, revision: str | None, text: str
    ) -> tuple[str, str]:
        """Reply in the stop's thread; with no thread for this revision, the
        text goes to the outbox file."""
        k = key(change, stop, revision)
        entry = self.notified().get(k)
        why = self._why_not() or (None if entry else "no thread")
        if why:
            self._outbox(k, text, why)
            return "skipped", why
        try:
            ts = self._post(text, thread_ts=str(entry["ts"]))
        except Exception as exc:  # noqa: BLE001 - any Slack failure is reported, never raised
            err = _error(exc)
            log.warning("slack reply for %s failed: %s", k, err)
            self._outbox(k, text, err)
            return "failed", err
        return "posted", ts


def _error(exc: Exception) -> str:
    """A short reason that never carries the token: Slack's error code when
    the API answered, else the exception's class."""
    resp = getattr(exc, "response", None)
    try:
        code = resp.get("error") if resp is not None else None
    except Exception:  # noqa: BLE001
        code = None
    return str(code) if code else type(exc).__name__


def mention(name: str, slack_id: str | None) -> str:
    return f"<@{slack_id}>" if slack_id else name


def page_link(base_url: str | None, change: str, stop: int) -> str:
    if not base_url:
        return "(the page is not running)"
    return f"{base_url.rstrip('/')}/#change={change}&stop={stop}"


def stop_text(status, stop: int, people, base_url: str | None) -> str:
    """The first message of a stop's thread: the stop, who it waits on (each
    @-mentioned when people.json has their Slack id), and the page link."""
    s = status.stops[stop - 1]
    ids = {p.name: p.slack_id for p in people.values()}
    who = ", ".join(mention(n, ids.get(n)) for n in s.waiting_on) or "nobody listed"
    return (
        f"*{status.name}* is at stop {stop} ({s.label}) and waits on {who}.\n"
        f"Review it: {page_link(base_url, status.name, stop)}"
    )
