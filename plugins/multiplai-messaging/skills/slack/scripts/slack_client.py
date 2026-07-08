#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "slack_sdk>=3.27",
#   "multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.5.2",
# ]
# ///
"""
Slack reader — pulls channel history for the channels *you* (the token owner)
are in, caches everything to a local SQLite DB so it is never re-fetched, saves
attachments (PDFs etc.) to disk, and remembers a per-channel read-since marker.

Auth: a Slack **user token** (`xoxp-...`) from a custom *internal* app, exported
as `SLACK_TOKEN`. A user token reads every channel you are a member of, no bot
invites needed. Scopes required on the app:
  read:  channels:history, groups:history, im:history, mpim:history,
         channels:read, groups:read, users:read
  files: files:read   (to download attachments)

Run (deps auto-installed by uv):
    uv run slack_client.py                 # sync all member channels, incremental
    uv run slack_client.py channels        # list channels you're in + tracked state
    uv run slack_client.py status          # DB stats
    uv run slack_client.py sync --channels '#sales,#eng' --full
    uv run slack_client.py export --channel '#sales' --format md

Data (DB + assets) lives under the git-ignored runtime bucket
`$WORKSPACE/.multiplai/data/skills/slack` (or `~/.multiplai/...` standalone),
resolved via multiplai-core. `SLACK_DATA_DIR`/`--data-dir` override it (advanced:
the value is a filesystem path that must be valid in whatever context the script
runs — mind host-vs-container paths). The token is read from the environment
only; it is never written to disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
    ServerErrorRetryHandler,
)

from multiplai_core.log_utils import log_event, setup_logging
from multiplai_core.paths import get_paths

# Configured in main() via setup_logging("slack"); until then this is an
# unconfigured logger, so module-import-time use is a no-op (never crashes).
log = logging.getLogger("slack")

SCRIPT_DIR = Path(__file__).resolve().parent


def default_data_dir() -> Path:
    """Where the SQLite cache + downloaded assets live.

    Skill state lives under the git-ignored runtime data bucket resolved by
    ``multiplai_core.paths`` — ``$WORKSPACE/.multiplai/data/skills/slack`` when
    a workspace is configured, else ``~/.multiplai/data/skills/slack`` for a
    standalone install. ``skill_state_dir`` also drops a ``*`` ``.gitignore`` at
    the data-dir root, so message content and attachments are git-ignored by
    mechanism. An explicit ``--data-dir``/``SLACK_DATA_DIR`` overrides it.
    """
    return get_paths().skill_state_dir("slack")

# Channels the token owner is a member of, across all conversation kinds.
MEMBER_CHANNEL_TYPES = "public_channel,private_channel,mpim,im"

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    is_private     INTEGER,
    is_im          INTEGER,
    last_ts        TEXT,          -- read-since marker: highest top-level ts synced
    last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    channel_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    thread_ts    TEXT,
    user         TEXT,
    type         TEXT,
    subtype      TEXT,
    text         TEXT,
    reply_count  INTEGER,
    raw          TEXT,           -- full JSON of the message
    retrieved_at TEXT,
    PRIMARY KEY (channel_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, ts);
CREATE TABLE IF NOT EXISTS files (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT,
    message_ts    TEXT,
    name          TEXT,
    title         TEXT,
    mimetype      TEXT,
    filetype      TEXT,
    size          INTEGER,
    url_private   TEXT,
    permalink     TEXT,
    local_path    TEXT,          -- NULL if not downloaded (external/non-slack)
    downloaded_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    real_name  TEXT,
    updated_at TEXT
);
"""


class Store:
    """Thin SQLite wrapper. All reads are cached so the API is hit only for genuinely new data."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- channels --
    def upsert_channel(self, cid: str, name: str, is_private: bool, is_im: bool) -> None:
        self.db.execute(
            """INSERT INTO channels (id, name, is_private, is_im) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                   is_private=excluded.is_private, is_im=excluded.is_im""",
            (cid, name, int(is_private), int(is_im)),
        )
        self.db.commit()

    def get_last_ts(self, cid: str) -> str | None:
        row = self.db.execute("SELECT last_ts FROM channels WHERE id=?", (cid,)).fetchone()
        return row["last_ts"] if row and row["last_ts"] else None

    def set_marker(self, cid: str, last_ts: str | None) -> None:
        self.db.execute(
            "UPDATE channels SET last_ts=?, last_synced_at=? WHERE id=?",
            (last_ts, _now(), cid),
        )
        self.db.commit()

    # -- messages --
    def insert_message(self, cid: str, msg: dict) -> bool:
        """Insert one message; returns True if it was new (not already cached)."""
        cur = self.db.execute(
            """INSERT OR IGNORE INTO messages
               (channel_id, ts, thread_ts, user, type, subtype, text, reply_count, raw, retrieved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                cid,
                msg.get("ts"),
                msg.get("thread_ts"),
                msg.get("user") or msg.get("bot_id"),
                msg.get("type"),
                msg.get("subtype"),
                msg.get("text"),
                msg.get("reply_count"),
                json.dumps(msg, ensure_ascii=False),
                _now(),
            ),
        )
        return cur.rowcount > 0

    def file_local_path(self, file_id: str) -> str | None:
        row = self.db.execute(
            "SELECT local_path FROM files WHERE id=?", (file_id,)
        ).fetchone()
        return row["local_path"] if row and row["local_path"] else None

    def file_rows(self, channel_id: str | None = None) -> list[sqlite3.Row]:
        if channel_id:
            return self.db.execute(
                "SELECT * FROM files WHERE channel_id=?", (channel_id,)
            ).fetchall()
        return self.db.execute("SELECT * FROM files").fetchall()

    def set_file_path(self, file_id: str, path: str | None) -> None:
        self.db.execute(
            "UPDATE files SET local_path=?, downloaded_at=? WHERE id=?",
            (path, _now() if path else None, file_id),
        )
        self.db.commit()

    def channel_name(self, cid: str) -> str:
        row = self.db.execute("SELECT name FROM channels WHERE id=?", (cid,)).fetchone()
        return row["name"] if row and row["name"] else cid

    def record_file(self, f: dict, cid: str, message_ts: str, local_path: str | None) -> None:
        self.db.execute(
            """INSERT INTO files
               (id, channel_id, message_ts, name, title, mimetype, filetype, size,
                url_private, permalink, local_path, downloaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET local_path=COALESCE(excluded.local_path, files.local_path),
                   downloaded_at=COALESCE(excluded.downloaded_at, files.downloaded_at)""",
            (
                f.get("id"),
                cid,
                message_ts,
                f.get("name"),
                f.get("title"),
                f.get("mimetype"),
                f.get("filetype"),
                f.get("size"),
                f.get("url_private_download") or f.get("url_private"),
                f.get("permalink"),
                local_path,
                _now() if local_path else None,
            ),
        )
        self.db.commit()

    # -- users --
    def upsert_user(self, uid: str, name: str | None, real_name: str | None) -> None:
        self.db.execute(
            """INSERT INTO users (id, name, real_name, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                   real_name=excluded.real_name, updated_at=excluded.updated_at""",
            (uid, name, real_name, _now()),
        )
        self.db.commit()

    def users_empty(self) -> bool:
        return self.db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0

    def users_last_updated(self) -> datetime | None:
        row = self.db.execute("SELECT MAX(updated_at) u FROM users").fetchone()
        if not row or not row["u"]:
            return None
        try:
            return datetime.fromisoformat(row["u"])
        except ValueError:
            return None

    def user_label(self, uid: str | None) -> str | None:
        if not uid:
            return None
        row = self.db.execute("SELECT real_name, name FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return None
        return row["real_name"] or row["name"]

    def user_rows(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT id, name, real_name FROM users ORDER BY real_name COLLATE NOCASE"
        ).fetchall()

    def find_users(self, query: str) -> list[sqlite3.Row]:
        like = f"%{query}%"
        return self.db.execute(
            """SELECT id, name, real_name FROM users
               WHERE real_name LIKE ? OR name LIKE ?
               ORDER BY real_name COLLATE NOCASE""",
            (like, like),
        ).fetchall()

    def stats(self) -> dict:
        c = self.db.execute("SELECT COUNT(*) n FROM channels").fetchone()["n"]
        m = self.db.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
        f = self.db.execute(
            "SELECT COUNT(*) n, SUM(local_path IS NOT NULL) d FROM files"
        ).fetchone()
        return {"channels": c, "messages": m, "files": f["n"], "files_downloaded": f["d"] or 0}

    def channel_rows(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) AS msg_count
               FROM channels c ORDER BY c.name"""
        ).fetchall()

    def messages_for(self, cid: str, since: str | None) -> list[sqlite3.Row]:
        if since:
            return self.db.execute(
                "SELECT * FROM messages WHERE channel_id=? AND ts > ? ORDER BY ts",
                (cid, since),
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM messages WHERE channel_id=? ORDER BY ts", (cid,)
        ).fetchall()

    def search_local(
        self, query: str, cid: str | None, uid: str | None, limit: int
    ) -> list[sqlite3.Row]:
        """Full-text-ish search over the cached message bodies (offline, no API).

        Case-insensitive substring match on `text`, newest first. Optional
        channel / author filters. Only covers what has been synced locally."""
        clauses = ["m.text LIKE ? ESCAPE '\\'"]
        # Escape LIKE wildcards so a literal % or _ in the query isn't treated as a pattern.
        esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list = [f"%{esc}%"]
        if cid:
            clauses.append("m.channel_id = ?")
            params.append(cid)
        if uid:
            clauses.append("m.user = ?")
            params.append(uid)
        params.append(limit)
        return self.db.execute(
            f"""SELECT m.ts, m.channel_id, m.user, m.text, m.thread_ts, c.name AS chan
                FROM messages m LEFT JOIN channels c ON c.id = m.channel_id
                WHERE {' AND '.join(clauses)}
                ORDER BY m.ts DESC LIMIT ?""",
            params,
        ).fetchall()


# ---------------------------------------------------------------------------
# Slack client
# ---------------------------------------------------------------------------


class SlackReader:
    def __init__(self, token: str, store: Store, assets_dir: Path):
        # All three retry handlers wired: only ConnectionError is on by default,
        # so RateLimit (reads Retry-After) and ServerError are added explicitly.
        self.client = WebClient(
            token=token,
            retry_handlers=[
                ConnectionErrorRetryHandler(max_retry_count=3),
                RateLimitErrorRetryHandler(max_retry_count=5),
                ServerErrorRetryHandler(max_retry_count=3),
            ],
        )
        self.token = token
        self.store = store
        self.assets_dir = assets_dir
        self._name_to_id: dict[str, str] | None = None
        self._self_id: str | None = None

    # -- users --
    USER_CACHE_TTL = timedelta(hours=24)

    def sync_users(self, force: bool = False) -> int:
        """Cache the workspace user directory (id → real name). One paginated call.

        Refreshes when forced, when empty, or when the cache is older than the
        TTL (24h) — so the first command run each day re-pulls; later runs reuse.
        """
        if not force:
            last = self.store.users_last_updated()
            if last is not None and datetime.now(timezone.utc) - last < self.USER_CACHE_TTL:
                return 0
        count = 0
        cursor = None
        while True:
            resp = self.client.users_list(limit=200, cursor=cursor)
            for m in resp.get("members", []):
                prof = m.get("profile") or {}
                real = (
                    prof.get("display_name")
                    or prof.get("real_name")
                    or m.get("real_name")
                    or m.get("name")
                )
                self.store.upsert_user(m["id"], m.get("name"), real)
                count += 1
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return count

    def label_for(self, ch: dict) -> str:
        """Human label for a conversation: '#chan', 'dm:Real Name', or 'mpdm:...'."""
        if ch.get("is_im"):
            uid = ch.get("user")
            return "dm:" + (self.store.user_label(uid) or uid or ch["id"])
        if ch.get("name"):
            return ("#" if not ch.get("is_mpim") else "") + ch["name"]
        return ch["id"]

    # -- posting --
    def whoami(self) -> str:
        """This token's own user ID (cached)."""
        if self._self_id is None:
            self._self_id = self.client.auth_test()["user_id"]
        return self._self_id

    def resolve_target(self, target: str) -> str:
        """Resolve a send target to a channel/user ID that chat.postMessage accepts.

        Accepts: 'me'/'self', a '#channel', a raw C/D/G/U id, or a person's name
        (with or without a leading '@'). Posting to a user ID delivers to that DM.
        """
        t = target.strip()
        if t.lower() in ("me", "self"):
            return self.whoami()
        if re.fullmatch(r"[CDGU][A-Z0-9]+", t):
            return t
        if t.startswith("#"):
            ids = self.resolve_channels([t])
            if not ids:
                sys.exit(f"error: could not resolve channel {t!r}")
            return ids[0]
        # treat as a person's name → look up in the cached directory
        name_q = t[1:] if t.startswith("@") else t
        rows = self.store.find_users(name_q)
        if not rows:
            sys.exit(f"error: no user matches {name_q!r} (try: users --refresh)")
        if len(rows) > 1:
            listing = "\n".join(f"  {r['real_name']}  @{r['name']}  {r['id']}" for r in rows)
            sys.exit(
                f"error: {len(rows)} users match {name_q!r} — be more specific or pass the ID:\n{listing}"
            )
        return rows[0]["id"]

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> dict:
        return self.client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)

    def search_messages(self, query: str, count: int) -> list[dict]:
        """Server-side workspace search via search.messages (needs `search:read`).

        Covers the whole workspace, not just synced channels. Returns match
        dicts (each has channel, user/username, text, ts, permalink), newest
        first."""
        resp = self.client.search_messages(query=query, count=count, sort="timestamp")
        return ((resp.get("messages") or {}).get("matches")) or []

    def fetch_thread(self, cid: str, parent_ts: str, fetch_files: bool = True) -> list[dict]:
        """Fetch a whole thread (parent + replies), caching messages and files.

        Falls back to fetching the single message when `parent_ts` isn't a thread
        parent (e.g. a permalink to a standalone message or a bare reply)."""
        name = self.store.channel_name(cid)
        out: list[dict] = []
        cursor = None
        try:
            while True:
                resp = self.client.conversations_replies(
                    channel=cid, ts=parent_ts, limit=200, cursor=cursor
                )
                for m in resp.get("messages", []):
                    self.store.insert_message(cid, m)
                    if fetch_files:
                        self._handle_files(m, cid, name)
                    out.append(m)
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as e:
            if e.response.get("error") not in ("thread_not_found", "message_not_found"):
                raise
        if out:
            return out
        # Fallback: fetch just the one message at that exact ts.
        resp = self.client.conversations_history(
            channel=cid, latest=parent_ts, oldest=parent_ts, inclusive=True, limit=1
        )
        for m in resp.get("messages", []):
            self.store.insert_message(cid, m)
            if fetch_files:
                self._handle_files(m, cid, name)
            out.append(m)
        return out

    # -- discovery --
    def member_channels(self) -> list[dict]:
        """Every conversation the token owner belongs to (paginated)."""
        out: list[dict] = []
        cursor = None
        while True:
            resp = self.client.users_conversations(
                types=MEMBER_CHANNEL_TYPES,
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            out.extend(resp.get("channels", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return out

    def resolve_channels(self, tokens: list[str]) -> list[str]:
        """Map a mix of channel IDs and #names to IDs."""
        if self._name_to_id is None:
            self._name_to_id = {}
            for ch in self.member_channels():
                if ch.get("name"):
                    self._name_to_id["#" + ch["name"]] = ch["id"]
                    self._name_to_id[ch["name"]] = ch["id"]
        ids = []
        for t in tokens:
            t = t.strip()
            if not t:
                continue
            if t in self._name_to_id:
                ids.append(self._name_to_id[t])
            elif re.fullmatch(r"[CDG][A-Z0-9]+", t):  # looks like a channel/DM id
                ids.append(t)
            else:
                log.warning("could not resolve channel %r — skipping", t)
        return ids

    # -- sync --
    def sync_channel(
        self,
        cid: str,
        name: str,
        *,
        full: bool = False,
        limit: int | None = None,
        fetch_files: bool = True,
        fetch_threads: bool = True,
    ) -> dict:
        oldest = None if full else self.store.get_last_ts(cid)
        new_count = 0
        file_count = 0
        max_ts = oldest
        cursor = None
        stop = False
        completed = False

        while not stop:
            try:
                resp = self.client.conversations_history(
                    channel=cid,
                    oldest=oldest or "0",
                    limit=200,
                    cursor=cursor,
                )
            except SlackApiError as e:
                # Leave `completed` False so the marker is NOT advanced: the
                # unfetched window is retried next run (INSERT OR IGNORE dedups).
                log.error("history failed for %s (%s): %s", name, cid, e.response.get("error"))
                break

            for msg in resp.get("messages", []):
                ts = msg.get("ts")
                if ts and (max_ts is None or ts > max_ts):
                    max_ts = ts
                if self.store.insert_message(cid, msg):
                    new_count += 1
                if fetch_files:
                    file_count += self._handle_files(msg, cid, name)
                if fetch_threads and msg.get("reply_count") and msg.get("thread_ts") == ts:
                    nf = self._sync_thread(cid, name, ts, fetch_files)
                    file_count += nf
                if limit and new_count >= limit:
                    stop = True
                    break

            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if stop:
                break  # --limit reached mid-window: incomplete, don't advance marker
            if not cursor:
                completed = True  # pagination exhausted cleanly
                break

        # Advance the read-since marker ONLY on a clean, complete pass. On an
        # early exit (--limit or an API error) the old marker stands, so the
        # next incremental run refetches the window; the only cost is re-reading
        # already-stored rows (deduped by INSERT OR IGNORE at insert time).
        if completed:
            self.store.set_marker(cid, max_ts)
        else:
            log.warning(
                "%s (%s): sync incomplete — marker not advanced, %d new fetched, "
                "will refetch on next run", name, cid, new_count)
        return {
            "new": new_count,
            "files": file_count,
            "marker": max_ts if completed else oldest,
            "completed": completed,
        }

    def _sync_thread(self, cid: str, name: str, thread_ts: str, fetch_files: bool) -> int:
        """Fetch replies for one thread. Dedup is handled by INSERT OR IGNORE."""
        file_count = 0
        cursor = None
        while True:
            try:
                resp = self.client.conversations_replies(
                    channel=cid, ts=thread_ts, limit=200, cursor=cursor
                )
            except SlackApiError as e:
                log.warning("replies failed for %s ts=%s: %s", name, thread_ts, e.response.get("error"))
                break
            for msg in resp.get("messages", []):
                if msg.get("ts") == thread_ts:
                    continue  # parent already stored from history
                self.store.insert_message(cid, msg)
                if fetch_files:
                    file_count += self._handle_files(msg, cid, name)
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return file_count

    # -- files --
    def _handle_files(self, msg: dict, cid: str, channel_name: str) -> int:
        downloaded = 0
        for f in msg.get("files", []) or []:
            fid = f.get("id")
            if not fid:
                continue
            # Skip only if we have it AND the local copy still exists on disk —
            # a deleted file gets re-fetched next time the message is revisited.
            existing = self.store.file_local_path(fid)
            if existing and Path(existing).exists():
                continue
            local_path = None
            url = f.get("url_private_download") or f.get("url_private")
            is_external = f.get("is_external") or f.get("external_type")
            if url and not is_external:
                local_path = self._download_file(f, cid, channel_name, msg.get("ts"))
                if local_path:
                    downloaded += 1
            self.store.record_file(f, cid, msg.get("ts"), local_path)
        return downloaded

    def _download_file(self, f: dict, cid: str, channel_name: str, ts: str | None) -> str | None:
        url = f.get("url_private_download") or f.get("url_private")
        subdir = self.assets_dir / _safe(channel_name or cid)
        subdir.mkdir(parents=True, exist_ok=True)
        fname = f"{ts or f.get('id')}_{_safe(f.get('name') or f.get('id'))}"
        dest = subdir / fname
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=90) as resp, open(dest, "wb") as out:
                    shutil.copyfileobj(resp, out)
                # Slack serves an HTML login/error page (HTTP 200) when the token
                # can't actually access the file — catch that instead of saving a fake file.
                with open(dest, "rb") as fh:
                    head = fh.read(64).lstrip().lower()
                if head.startswith((b"<!doctype html", b"<html")):
                    dest.unlink(missing_ok=True)
                    log.error("  ✗ %s came back as an HTML page, not a file (check files:read / access)", fname)
                    return None
                log.info("  ↓ %s (%s)", dest.name, _human(f.get("size")))
                return str(dest)
            except (urllib.error.URLError, TimeoutError) as e:
                wait = 2 ** attempt
                log.warning("  download retry %d for %s: %s (waiting %ds)", attempt, fname, e, wait)
                time.sleep(wait)
        log.error("  ✗ failed to download %s", fname)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "unknown")[:120]


def _human(n) -> str:
    if not n:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}TB"


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts


def parse_permalink(url: str) -> tuple[str, str, str | None] | None:
    """Parse a Slack message permalink → (channel_id, ts, thread_ts|None).

    e.g. https://x.slack.com/archives/C0B0T384MT8/p1783347819836239
         → ('C0B0T384MT8', '1783347819.836239', None)
    A '?thread_ts=…' query param (present on reply links) becomes the thread parent.
    """
    m = re.search(r"/archives/([CDG][A-Z0-9]+)/p(\d+)", url)
    if not m:
        return None
    cid, digits = m.group(1), m.group(2)
    ts = f"{digits[:-6]}.{digits[-6:]}" if len(digits) > 6 else digits
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    thread_ts = params.get("thread_ts", [None])[0]
    return cid, ts, thread_ts


def get_token() -> str:
    token = os.environ.get("SLACK_TOKEN")
    if not token:
        sys.exit("error: set SLACK_TOKEN to your xoxp-... user token")
    if not token.startswith("xoxp-"):
        log.warning("token does not start with xoxp- ; a user token is expected for reading your channels")
    return token


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_sync(args, reader: SlackReader, store: Store) -> None:
    reader.sync_users()  # populate name cache on first run
    if args.channels:
        ids = reader.resolve_channels(args.channels.split(","))
        channels = [c for c in reader.member_channels() if c["id"] in ids]
    else:
        channels = reader.member_channels()

    log.info("syncing %d channel(s)%s", len(channels), " (full)" if args.full else " (incremental)")
    totals = {"new": 0, "files": 0}
    for ch in channels:
        cid = ch["id"]
        name = reader.label_for(ch)
        store.upsert_channel(cid, name, bool(ch.get("is_private")), bool(ch.get("is_im")))
        res = reader.sync_channel(
            cid, name,
            full=args.full,
            limit=args.limit,
            fetch_files=not args.no_files,
            fetch_threads=not args.no_threads,
        )
        if res["new"] or res["files"]:
            log.info("%-28s +%d msg, +%d files", name, res["new"], res["files"])
        totals["new"] += res["new"]
        totals["files"] += res["files"]
    log.info("done: %d new messages, %d files downloaded", totals["new"], totals["files"])


def cmd_channels(args, reader: SlackReader, store: Store) -> None:
    reader.sync_users(force=args.refresh_users)
    tracked = {r["id"]: r for r in store.channel_rows()}
    print(f"{'CHANNEL':<40} {'ID':<12} {'PRIV':<5} {'MSGS':>6} {'LAST SYNC':<17}")
    for ch in sorted(reader.member_channels(), key=lambda c: reader.label_for(c).lower()):
        cid = ch["id"]
        name = reader.label_for(ch)
        row = tracked.get(cid)
        msgs = row["msg_count"] if row else 0
        last = _fmt_ts(row["last_ts"]) if row and row["last_ts"] else "never"
        priv = "yes" if ch.get("is_private") or ch.get("is_im") else ""
        print(f"{name:<40} {cid:<12} {priv:<5} {msgs:>6} {last:<17}")


def cmd_users(args, reader: SlackReader, store: Store) -> None:
    n = reader.sync_users(force=args.refresh)
    if n:
        log.info("cached %d users", n)
    rows = store.user_rows()
    print(f"{'REAL NAME':<28} {'@HANDLE':<24} {'ID':<12}")
    for r in rows:
        print(f"{(r['real_name'] or ''):<28} {('@' + (r['name'] or '')):<24} {r['id']:<12}")


def cmd_send(args, reader: SlackReader, store: Store) -> None:
    reader.sync_users()  # ensure name lookups work
    channel = reader.resolve_target(args.to)
    resp = reader.post(channel, args.text, args.thread_ts)
    log.info("sent to %s (channel=%s, ts=%s)", args.to, resp.get("channel"), resp.get("ts"))
    # Audit the outward-facing action to the shared activity log (mirrors the
    # gmail draft audit) — closes the "gmail audited, slack send not" gap.
    log_event(
        "slack", "send", f"posted to {args.to}",
        channel_id=resp.get("channel"), ts=resp.get("ts"),
        threaded=bool(args.thread_ts))


def cmd_files(args, reader: SlackReader, store: Store) -> None:
    cid = None
    if args.channel:
        ids = reader.resolve_channels([args.channel])
        if not ids:
            sys.exit(f"error: could not resolve channel {args.channel!r}")
        cid = ids[0]
    rows = store.file_rows(cid)
    ok = present = failed = skipped = 0
    for r in rows:
        if r["local_path"] and Path(r["local_path"]).exists() and not args.redownload:
            present += 1
            continue
        url = r["url_private"]
        if not url:
            skipped += 1  # external / non-downloadable
            continue
        f = {"id": r["id"], "name": r["name"], "size": r["size"], "url_private_download": url}
        path = reader._download_file(f, r["channel_id"], store.channel_name(r["channel_id"]), r["message_ts"])
        store.set_file_path(r["id"], path)
        if path:
            ok += 1
        else:
            failed += 1
    log.info("files: %d downloaded, %d already present, %d skipped, %d failed", ok, present, skipped, failed)


def cmd_thread(args, reader: SlackReader, store: Store) -> None:
    parsed = parse_permalink(args.url)
    if not parsed:
        sys.exit("error: not a Slack message link (expected …/archives/<CID>/p<digits>)")
    cid, ts, thread_ts = parsed
    reader.sync_users()  # resolve author names
    parent = thread_ts or ts
    msgs = reader.fetch_thread(cid, parent, fetch_files=not args.no_files)
    if not msgs:
        sys.exit("error: message not found (are you a member of that channel?)")
    cname = store.channel_name(cid)
    header = f"# {cname} ({cid}) — {len(msgs)} message(s)"
    if len(msgs) > 1:
        header += f", thread parent {parent}"
    print(header + "\n")
    for m in msgs:
        who = store.user_label(m.get("user")) or m.get("user") or m.get("bot_id") or "?"
        print(f"[{_fmt_ts(m.get('ts'))}] {who}:")
        text = (m.get("text") or "").strip()
        for line in text.splitlines() or [""]:
            print(f"    {line}")
        for f in m.get("files", []) or []:
            print(f"    📎 {f.get('name')} ({_human(f.get('size'))})")
        print()


def cmd_status(args, reader: SlackReader, store: Store) -> None:
    s = store.stats()
    print("Slack local cache")
    print(f"  db:        {store.db_path}")
    print(f"  channels:  {s['channels']}")
    print(f"  messages:  {s['messages']}")
    print(f"  files:     {s['files']} ({s['files_downloaded']} downloaded locally)")


def cmd_export(args, reader: SlackReader, store: Store) -> None:
    ids = reader.resolve_channels([args.channel])
    if not ids:
        sys.exit(f"error: could not resolve channel {args.channel!r}")
    cid = ids[0]
    rows = store.messages_for(cid, args.since)
    if args.format == "json":
        out = [dict(r) for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"[{_fmt_ts(r['ts'])}] {r['user'] or '?'}: {r['text'] or ''}")
    log.info("exported %d messages from %s", len(rows), args.channel)


def _oneline(text: str | None, width: int = 200) -> str:
    """Collapse a message body to a single trimmed line for list output."""
    s = " ".join((text or "").split())
    return s if len(s) <= width else s[: width - 1] + "…"


def cmd_search(args, reader: SlackReader, store: Store) -> None:
    reader.sync_users()  # resolve author names in results

    if args.local:
        cid = None
        if args.channel:
            ids = reader.resolve_channels([args.channel])
            if not ids:
                sys.exit(f"error: could not resolve channel {args.channel!r}")
            cid = ids[0]
        uid = None
        if getattr(args, "from_"):
            rows = store.find_users(args.from_)
            if not rows:
                sys.exit(f"error: no cached user matches {args.from_!r} (try: users --refresh)")
            if len(rows) > 1:
                listing = "\n".join(f"  {r['real_name']}  @{r['name']}  {r['id']}" for r in rows)
                sys.exit(f"error: {len(rows)} users match {args.from_!r} — be more specific:\n{listing}")
            uid = rows[0]["id"]
        matches = store.search_local(args.query, cid, uid, args.limit)
        if not matches:
            print("no local matches (only synced messages are searched — try `sync` first, or drop --local)")
            return
        for r in matches:
            who = store.user_label(r["user"]) or r["user"] or "?"
            chan = ("#" + r["chan"]) if r["chan"] else r["channel_id"]
            print(f"[{_fmt_ts(r['ts'])}] {chan}  {who}: {_oneline(r['text'])}")
        log.info("%d local match(es)", len(matches))
        return

    # Server-side workspace search. --channel/--from become Slack search
    # modifiers (in:/from:), which Slack parses against #names and @handles.
    parts = [args.query]
    if args.channel:
        parts.append(f"in:{args.channel}")
    if args.from_:
        parts.append(f"from:{args.from_ if args.from_.startswith('@') else '@' + args.from_}")
    matches = reader.search_messages(" ".join(parts), count=args.limit)
    if not matches:
        print("no matches")
        return
    for m in matches:
        ch = m.get("channel") or {}
        chan = ("#" + ch["name"]) if ch.get("name") else (ch.get("id") or "?")
        who = store.user_label(m.get("user")) or m.get("username") or m.get("user") or "?"
        print(f"[{_fmt_ts(m.get('ts'))}] {chan}  {who}: {_oneline(m.get('text'))}")
        if m.get("permalink"):
            print(f"    {m['permalink']}")
    log.info("%d match(es)", len(matches))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Shared flags live on a parent parser so they're accepted at the top
    # level AND on the `sync` subparser — the latter matters because `sync`
    # is the implicit default command (re-parsed as `sync <argv>` in main()).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=os.environ.get("SLACK_DATA_DIR"), help="override data dir")

    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common])
    sub = p.add_subparsers(dest="cmd")

    sync = sub.add_parser("sync", parents=[common], help="fetch new messages for member channels (default command)")
    sync.add_argument("--channels", help="comma list of #names or IDs (default: all you're in)")
    sync.add_argument("--full", action="store_true", help="ignore read-since marker, refetch history")
    sync.add_argument("--limit", type=int, help="cap new messages per channel this run")
    sync.add_argument("--no-files", action="store_true", help="don't download attachments")
    sync.add_argument("--no-threads", action="store_true", help="don't fetch thread replies")

    chans = sub.add_parser("channels", help="list channels you're in + tracked state")
    chans.add_argument("--refresh-users", action="store_true", help="refresh the user-name cache")

    usr = sub.add_parser("users", help="list cached workspace users (id ↔ name)")
    usr.add_argument("--refresh", action="store_true", help="re-pull the directory from Slack")

    snd = sub.add_parser("send", help="post a message to a channel, a person, or yourself")
    snd.add_argument("--to", required=True, help="'me', '#channel', a person's name, or a C/D/U id")
    snd.add_argument("--text", required=True, help="message text (Slack mrkdwn)")
    snd.add_argument("--thread-ts", help="reply within a thread (parent message ts)")

    thr = sub.add_parser("thread", help="read a thread/message from a pasted Slack link")
    thr.add_argument("url", help="a Slack message permalink (…/archives/<CID>/p<digits>)")
    thr.add_argument("--no-files", action="store_true", help="don't download attachments")

    fls = sub.add_parser("files", help="re-download attachments (e.g. after deleting local copies)")
    fls.add_argument("--channel", help="limit to one #channel or id")
    fls.add_argument("--redownload", action="store_true", help="re-download even if the local copy exists")

    sub.add_parser("status", help="show local cache stats")

    exp = sub.add_parser("export", help="dump cached messages for a channel")
    exp.add_argument("--channel", required=True, help="#name or ID")
    exp.add_argument("--since", help="only messages with ts greater than this")
    exp.add_argument("--format", choices=["md", "json"], default="md")

    srch = sub.add_parser("search", help="search messages (whole workspace, or --local cache)")
    srch.add_argument("query", help="text to search for (server-side: Slack search syntax)")
    srch.add_argument("--local", action="store_true", help="search the local cache only (offline, synced messages)")
    srch.add_argument("--channel", help="limit to a #channel or ID (server-side: in:<channel>)")
    srch.add_argument("--from", dest="from_", metavar="WHO", help="limit to a sender (server-side: from:@handle)")
    srch.add_argument("--limit", type=int, default=20, help="max results (default 20)")
    return p


def main(argv: list[str]) -> None:
    setup_logging("slack")
    parser = build_parser()
    args = parser.parse_args(argv)

    # Default command is sync when none given. Re-parse with the verb
    # prepended so sync's own defaults (channels, full, limit, …) are set.
    # --data-dir lives on a shared parent parser attached to BOTH the top
    # level and the sync subparser, so `slack --data-dir X` (no subcommand)
    # re-parses to `sync --data-dir X` without the old crash.
    if args.cmd is None:
        args = parser.parse_args(["sync", *argv])

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = data_dir / "assets"
    store = Store(data_dir / "slack.db")
    reader = SlackReader(get_token(), store, assets_dir)

    try:
        {
            "sync": cmd_sync,
            "channels": cmd_channels,
            "users": cmd_users,
            "send": cmd_send,
            "thread": cmd_thread,
            "files": cmd_files,
            "status": cmd_status,
            "export": cmd_export,
            "search": cmd_search,
        }[args.cmd](args, reader, store)
    except SlackApiError as e:
        err = e.response.get("error")
        if err == "missing_scope":
            needed = e.response.get("needed")
            provided = e.response.get("provided")
            sys.exit(
                f"Slack API error: missing_scope\n"
                f"  needed:   {needed}\n"
                f"  provided: {provided}\n"
                f"  → add the 'needed' scope under User Token Scopes, reinstall the app, "
                f"and re-export the new xoxp- token."
            )
        sys.exit(f"Slack API error: {err}")
    finally:
        store.close()


if __name__ == "__main__":
    main(sys.argv[1:])
