"""Find the viewers already running: one server per mailbox, never two.

A leftover `server.json` proves nothing — a killed process never deletes it,
and its port may now belong to someone else. So every claim is checked by
asking whoever answers on that port who it is (`/api/whoami`), authenticated
with the token from the mailbox's `server.token`.

Ports are probed in parallel. Probed one by one, nineteen idle ports that let
the connection time out instead of refusing it took a measured 18 s.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

PORT_START = 8765
PORT_SPAN = 20
PROBE_TIMEOUT = 1.5


def state_dir() -> Path:
    """Per-user index of live mailboxes, so `list` and `stop --all` know where
    to read tokens from. Holds mailbox paths only — never a token."""
    override = os.environ.get("REVIEW_VIEWER_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "review-viewer"


def register(pid: int, boxes: list[Path]) -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    entry = d / f"{pid}.json"
    entry.write_text(json.dumps({"mailboxes": [str(b) for b in boxes]}), encoding="utf-8")
    return entry


def unregister(pid: int) -> None:
    try:
        (state_dir() / f"{pid}.json").unlink()
    except OSError:
        pass


def registered_boxes() -> list[Path]:
    boxes: list[Path] = []
    d = state_dir()
    if not d.is_dir():
        return boxes
    for entry in sorted(d.glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for b in data.get("mailboxes", []):
            path = Path(b)
            if (path / "server.token").exists():
                boxes.append(path)
    if not boxes:
        return boxes
    return list(dict.fromkeys(boxes))


def read_token(box: Path) -> str | None:
    try:
        return (box / "server.token").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _request(port: int, route: str, token: str, *, method: str = "GET",
             timeout: float = PROBE_TIMEOUT) -> dict | None:
    data = b"{}" if method == "POST" else None
    req = Request(f"http://127.0.0.1:{port}{route}", data=data, method=method,
                  headers={"X-Review-Token": token, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def probe(port: int, token: str, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """What a review-viewer on `port` says about itself, if it accepts `token`."""
    who = _request(port, "/api/whoami", token, timeout=timeout)
    if not who or "targets" not in who or "mailboxes" not in who:
        return None
    who["port"] = port
    return who


def scan(first: int = PORT_START, span: int = PORT_SPAN,
         boxes: list[Path] | None = None) -> list[dict]:
    """Every live viewer in the port range whose token we can read.

    A token is readable only from a mailbox on this machine, so `boxes` lists
    the mailboxes to try (default: every registered one). Each port is probed
    with each token, in parallel.
    """
    if boxes is None:
        boxes = registered_boxes()
    tokens = list(dict.fromkeys(t for t in (read_token(b) for b in boxes) if t))
    if not tokens:
        return []
    jobs = [(p, t) for p in range(first, first + span) for t in tokens]
    with ThreadPoolExecutor(max_workers=min(64, len(jobs))) as pool:
        found = list(pool.map(lambda job: probe(*job), jobs))
    seen: dict[int, dict] = {}
    for who in found:
        if who:
            seen[who["port"]] = who
    return [seen[p] for p in sorted(seen)]


def existing(box: Path, first: int = PORT_START, span: int = PORT_SPAN) -> dict | None:
    """The live server for this mailbox, if there is one.

    `server.json` names the port to try first; a range scan covers a server
    whose file was lost. A `server.json` no live server confirms is deleted,
    so the next reader does not trust it either.
    """
    box = box.resolve()
    token = read_token(box)
    record = box / "server.json"
    if token and record.exists():
        try:
            port = int(json.loads(record.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError):
            port = None
        who = probe(port, token) if port else None
        if who and str(box) in who.get("mailboxes", []):
            return who
    if token:
        for who in scan(first, span, [box]):
            if str(box) in who.get("mailboxes", []):
                return who
    # Nothing confirmed it: the files are left over from a dead server.
    for leftover in ("server.json", "server.token", "open.html"):
        try:
            (box / leftover).unlink()
        except OSError:
            pass
    return None


def shutdown(port: int, token: str) -> bool:
    return _request(port, "/api/shutdown", token, method="POST", timeout=3) is not None


def describe(who: dict) -> str:
    sid = (who.get("session_id") or "")[:8] or "no session"
    targets = ", ".join(who.get("targets", [])) or "?"
    return (f"port {who.get('port')}  targets: {targets}  agent: {who.get('agent', '?')}"
            f"  session: {sid}  pid: {who.get('pid', '?')}  since {who.get('started', '?')}")
