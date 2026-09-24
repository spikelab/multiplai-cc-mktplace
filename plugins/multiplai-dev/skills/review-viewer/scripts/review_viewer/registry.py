"""Find the viewers already running: one server per mailbox, never two.

A leftover `server.json` proves nothing — a killed process never deletes it,
and its port may now belong to someone else. So every claim is checked by
asking the recorded port who it is (`/api/whoami`), authenticated with the
token from the same mailbox's `server.token`.

A token is only ever sent to the port its own mailbox records. Probing a port
range with every known token would hand each token to whatever else happens to
listen there.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import netinfo

log = logging.getLogger(__name__)

PORT_START = 8765
PORT_SPAN = 20
PROBE_TIMEOUTS = (1.5, 4.0)


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
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        if not pid_alive(_int(entry.stem)):
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        boxes.extend(Path(b) for b in data.get("mailboxes", []))
    return list(dict.fromkeys(boxes))


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_token(box: Path) -> str | None:
    try:
        return (box / "server.token").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_record(box: Path) -> dict | None:
    try:
        data = json.loads((box / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _request(port: int, route: str, token: str, *, body: dict | None = None,
             timeout: float = PROBE_TIMEOUTS[0]) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(f"http://{netinfo.probe_host()}:{port}{route}", data=data,
                  method="POST" if data is not None else "GET",
                  headers={"X-Review-Token": token, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def probe(port: int, token: str) -> dict | None:
    """What the review-viewer on `port` says about itself, if it accepts
    `token`. A slow server gets a second, longer try."""
    for timeout in PROBE_TIMEOUTS:
        who = _request(port, "/api/whoami", token, timeout=timeout)
        if who and "targets" in who and "mailboxes" in who:
            who["port"] = port
            return who
    return None


def _probe_box(box: Path) -> dict | None:
    token = read_token(box)
    record = read_record(box)
    port = _int(record.get("port")) if record else None
    if not token or not port:
        return None
    who = probe(port, token)
    if who and str(box.resolve()) in who.get("mailboxes", []):
        return who
    return None


def scan(boxes: list[Path] | None = None) -> list[dict]:
    """Every live viewer serving one of `boxes` (default: every registered
    mailbox), found by probing each mailbox's own recorded port."""
    if boxes is None:
        boxes = registered_boxes()
    if not boxes:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(boxes))) as pool:
        found = list(pool.map(_probe_box, boxes))
    seen: dict[int, dict] = {}
    for who in found:
        if who:
            seen[who["port"]] = who
    return [seen[p] for p in sorted(seen)]


def existing(box: Path) -> dict | None:
    """The live server for this mailbox, if there is one.

    Returns `{"unresponsive": True, "pid", "port"}` when the recorded process
    is still alive but did not answer: its files are left alone, because
    deleting them would orphan a running server. Files whose process is gone
    are deleted, so the next reader does not trust them either.
    """
    box = box.resolve()
    who = _probe_box(box)
    if who:
        return who
    record = read_record(box)
    if record and pid_alive(_int(record.get("pid"))):
        return {"unresponsive": True, "pid": record.get("pid"), "port": record.get("port")}
    for leftover in ("server.json", "server.token", "open.html"):
        try:
            (box / leftover).unlink()
        except OSError:
            pass
    return None


def shutdown(who: dict, token: str, reason: str = "stop") -> bool:
    return _request(who["port"], "/api/shutdown", token, body={"reason": reason},
                    timeout=4.0) is not None


def describe(who: dict) -> str:
    sid = (who.get("session_id") or "")[:8] or "no session"
    targets = ", ".join(who.get("targets", [])) or "?"
    return (f"port {who.get('port')}  targets: {targets}  agent: {who.get('agent', '?')}"
            f"  session: {sid}  pid: {who.get('pid', '?')}  since {who.get('started', '?')}")
