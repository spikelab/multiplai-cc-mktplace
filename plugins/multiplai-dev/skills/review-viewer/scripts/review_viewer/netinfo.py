"""Where to bind, and which URLs to print so the browser can reach the server.

Container detection follows rule 2 of docs/degradation-contract.md: the
MULTIPLAI_CONTAINER flag first, then /.dockerenv. Never `uname` — "not a Mac"
does not mean "a container".
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

DOCKERENV = Path("/.dockerenv")


def detect_container() -> bool:
    flag = os.environ.get("MULTIPLAI_CONTAINER", "")
    if flag == "1":
        return True
    if flag == "0":
        return False
    return DOCKERENV.exists()


def bind_host() -> str:
    override = os.environ.get("REVIEW_VIEWER_HOST")
    if override:
        return override
    # In a container the browser is on the other side of a network boundary,
    # so loopback would be unreachable. The token still guards every /api call.
    return "0.0.0.0" if detect_container() else "127.0.0.1"


def probe_host() -> str:
    """Where this machine's own tools reach the server: the bound address when
    REVIEW_VIEWER_HOST names one interface, loopback otherwise."""
    override = os.environ.get("REVIEW_VIEWER_HOST")
    if override and override not in ("0.0.0.0", "::", ""):
        return override
    return "127.0.0.1"


def _first_address() -> str | None:
    try:
        out = subprocess.run(["hostname", "-I"], shell=False, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split()
    return parts[0] if parts else None


def display_urls(port: int) -> list[tuple[str, str]]:
    """(url, label) pairs in the order they should be tried."""
    urls: list[tuple[str, str]] = []
    override = os.environ.get("REVIEW_VIEWER_URL_HOST")
    if override:
        base = override if "://" in override else f"http://{override}"
        urls.append((f"{base.rstrip('/')}:{port}/", "REVIEW_VIEWER_URL_HOST"))
    if detect_container():
        urls.append((f"http://{socket.gethostname()}.orb.local:{port}/", "OrbStack"))
        addr = _first_address()
        if addr:
            urls.append((f"http://{addr}:{port}/",
                         "container IP; with Docker Desktop, publish the port instead"))
    elif not override:
        urls.append((f"http://127.0.0.1:{port}/", "local"))
    return urls
