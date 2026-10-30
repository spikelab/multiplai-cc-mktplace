"""The board outbox: each child's column moves reach its Plane card
(plan-factory-merged-2026-10-29.md, item 70; gaps G4 and G19).

With `epic.board: plane`, every `BOARD:<slug>:<Column>` line a child's build
printed into its record's log is saved to `<epic dir>/board-outbox.jsonl`
before it is sent, and then sent through the plane skill as
`plane.py update <ticket> --state <Column>`. The ticket is the child's own
`ticket_refs.epic` in its `.change.yaml` on `origin/<feature branch>`, which
pm-spec wrote; a child with none is not sent. With `epic.board: off` (the
default) nothing is saved or sent.

The file is append-only, one JSON object per line, never rewritten:
- `transition`: a `BOARD:` line, keyed `<NNN>:<k>` (the record's number and
  the line's place among that log's `BOARD:` lines), so reading a log again
  never adds it twice;
- `sending`, written just before the call;
- `sent`, after the call succeeded, or after a read of the card found it
  already in that column (`checked: true`);
- `failed`, with the error.

A transition with no `sent` line is sent again by the next drain, in file
order; a failure holds back that child's later transitions until it is sent,
so a card never moves backwards. One that was tried before (`sending` or
`failed`) is read first (`plane.py get <ticket> --json`): a card already in
its column is marked sent with no second update (row S52). Nothing here
launches a build or writes a record. A line cut short by a kill is skipped
on read, and the next append starts on a new line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import records

log = logging.getLogger(__name__)

OUTBOX = "board-outbox.jsonl"
PLANE_TIMEOUT = 120  # seconds one plane.py call gets (judgment)
NO_SCRIPT = (
    "the plane skill's plane.py was not found: install the multiplai-pm plugin, "
    "or set epic.plane_script in specs/config.yaml to its path"
)


@dataclass
class Transition:
    key: str
    child: str
    column: str
    sent: bool = False
    tried: bool = False
    error: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def read(epic_dir: Path) -> list[Transition]:
    """Every transition in the outbox, in file order, with what became of it."""
    try:
        text = (Path(epic_dir) / OUTBOX).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    by_key: dict[str, Transition] = {}
    for raw in text.splitlines():
        try:
            entry = json.loads(raw) if raw.strip() else None
        except ValueError:
            continue  # a line cut short by a kill
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        key, event = entry["key"], entry.get("event")
        if event == "transition":
            child, column = entry.get("child"), entry.get("column")
            if isinstance(child, str) and isinstance(column, str):
                by_key.setdefault(key, Transition(key, child, column))
            continue
        t = by_key.get(key)
        if t is None:
            continue
        if event == "sent":
            t.sent = True
        elif event in ("sending", "failed"):
            t.tried = True
            if event == "failed":
                t.error = str(entry.get("error") or "")
    return list(by_key.values())


def append(epic_dir: Path, entries: list[dict]) -> None:
    """Add *entries* to the outbox, each on its own line, synced to disk."""
    if not entries:
        return
    path = Path(epic_dir) / OUTBOX
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        if f.tell() and not _ends_with_newline(path):
            f.write(b"\n")  # a kill cut the last line short
        for entry in entries:
            f.write((json.dumps({**entry, "at": _now()}) + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def board_lines(log_path: Path, *, finished: bool) -> list[str]:
    """The column of each `BOARD:<slug>:<Column>` line in a record's log, in
    order. While the process runs, a last line with no newline yet is left
    for the next read."""
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return []
    if not finished:
        text = text[: text.rfind("\n") + 1]
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("BOARD:"):
            continue
        parts = line.split(":", 2)  # the column holds spaces, never the slug a `:`
        if len(parts) == 3 and parts[2].strip():
            out.append(parts[2].strip())
    return out


def collect(epic_dir: Path, tries: list[records.Attempt]) -> int:
    """Save each `BOARD:` line of the `build` records' logs that the outbox
    does not hold yet. Returns how many were added."""
    known = {t.key for t in read(epic_dir)}
    new = []
    for a in tries:
        if a.kind != "build":
            continue
        for k, column in enumerate(board_lines(a.log, finished=a.result is not None)):
            key = f"{a.n}:{k}"
            if key not in known:
                new.append(
                    {
                        "event": "transition",
                        "key": key,
                        "child": a.child,
                        "column": column,
                    }
                )
    append(epic_dir, new)
    return len(new)


def unsent(epic_dir: Path) -> dict[str, list[str]]:
    """Per child, the columns saved and not sent yet, in order."""
    out: dict[str, list[str]] = {}
    for t in read(epic_dir):
        if not t.sent:
            out.setdefault(t.child, []).append(t.column)
    return out


class PlaneBoard:
    """The plane skill's `plane.py`, run as a subprocess with this Python
    (it needs only the standard library) and the environment buildme has,
    where the skill reads `PLANE_API_TOKEN`, `PLANE_WORKSPACE` and
    `PLANE_ALLOWED_PROJECTS`."""

    def __init__(self, script: Path | None):
        self.script = script

    def _run(self, *args: str) -> tuple[str | None, str]:
        """(error or None, stdout)."""
        if self.script is None or not self.script.is_file():
            return NO_SCRIPT, ""
        try:
            proc = subprocess.run(
                [sys.executable, str(self.script), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=PLANE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"plane.py {args[0]} did not finish: {e}", ""
        if proc.returncode != 0:
            said = (proc.stderr or proc.stdout).strip()[-300:]
            return f"plane.py {args[0]} exited {proc.returncode}: {said}", ""
        return None, proc.stdout

    def move(self, ref: str, column: str) -> str | None:
        """Set the card's state to *column*; the error, or None."""
        error, _ = self._run("update", ref, "--state", column)
        return error

    def column_of(self, ref: str) -> tuple[str | None, str | None]:
        """(the card's state name, None), or (None, the error). A card whose
        state has no name (not expanded) reads as (None, None): its column is
        unknown, so the move is sent."""
        error, out = self._run("get", ref, "--json")
        if error:
            return None, error
        try:
            state = json.loads(out).get("state")
        except (ValueError, AttributeError):
            return None, "plane.py get printed no JSON issue"
        name = state.get("name") if isinstance(state, dict) else None
        return (name if isinstance(name, str) else None), None


def drain(
    epic_dir: Path,
    ticket_of: Callable[[str], str | None],
    board: PlaneBoard,
) -> None:
    """Send every transition not marked sent, in file order (row S52)."""
    held: set[str] = set()  # children whose earlier transition is not sent
    for t in read(epic_dir):
        if t.sent or t.child in held:
            continue
        held.add(t.child)  # until this one is sent
        ref = ticket_of(t.child)
        if not ref:
            log.info(
                "EPIC child %s: no ticket ref in its .change.yaml; its card is not moved",
                t.child,
            )
            continue
        if t.tried:
            state, error = board.column_of(ref)
            if error:
                append(epic_dir, [{"event": "failed", "key": t.key, "error": error}])
                log.warning(
                    "EPIC child %s: cannot read card %s: %s", t.child, ref, error
                )
                continue
            if _same(state, t.column):
                append(epic_dir, [{"event": "sent", "key": t.key, "checked": True}])
                held.discard(t.child)
                continue
        append(epic_dir, [{"event": "sending", "key": t.key}])
        error = board.move(ref, t.column)
        if error:
            append(epic_dir, [{"event": "failed", "key": t.key, "error": error}])
            log.warning(
                "EPIC child %s: moving card %s to %s failed: %s",
                t.child,
                ref,
                t.column,
                error,
            )
            continue
        append(epic_dir, [{"event": "sent", "key": t.key}])
        held.discard(t.child)
        log.info("EPIC child %s: card %s moved to %s", t.child, ref, t.column)


def _same(state: str | None, column: str) -> bool:
    return (state or "").strip().casefold() == column.strip().casefold()


_VERSION = re.compile(r"^\d+(\.\d+)*$")


def plane_script(configured: str | None) -> Path | None:
    """`epic.plane_script` when set; else the plane skill beside this plugin:
    `<plugins>/multiplai-pm/skills/plane/scripts/plane.py` in a checkout of
    the marketplace, or the newest `<cache>/multiplai-pm/<version>/...` in an
    installed copy. None when there is none."""
    if configured:
        return Path(configured).expanduser()
    tail = Path("skills") / "plane" / "scripts" / "plane.py"
    plugin = Path(__file__).resolve().parents[5]  # multiplai-dev, or its <version>
    found = plugin.parent / "multiplai-pm" / tail
    if found.is_file():
        return found
    installed = plugin.parent.parent / "multiplai-pm"
    versions = (
        [p for p in installed.iterdir() if _VERSION.match(p.name)]
        if installed.is_dir()
        else []
    )
    for v in sorted(
        versions, key=lambda p: tuple(int(x) for x in p.name.split(".")), reverse=True
    ):
        if (v / tail).is_file():
            return v / tail
    return None
