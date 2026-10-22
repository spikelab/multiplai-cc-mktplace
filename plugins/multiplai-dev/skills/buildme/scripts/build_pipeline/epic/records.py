"""The coordinator's records: one started and one result file, and a log, per
agent or subprocess it runs (plan-buildme-epic-mode-2026-10-12.md, item 14).

`agents/<NNN>-<kind>-<child>.started.json` is written before the process
starts (argv, `pr_round`, head sha, time), `<...>.result.json` after it ends
(exit code, markers, PR URL, `interrupted`, and `spend`: the USD on the
process's last `SPEND:<usd>:<tokens>` line, null without one, PR B2), and
`<...>.log` holds its stdout
and stderr. `NNN` is the next free number. A started record with no result
is an interrupted attempt. Both files are written through a temp file and
`os.replace`, so a kill leaves the old file or the new one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import _write_atomic

AGENTS_DIR = "agents"
KINDS = ("build", "review", "resume", "post", "fix", "merge", "merge-tests")
NAMED_MARKERS = ("WORKTREE", "BRANCH", "PR", "SPEND")
# The kind and child come from the started record, which the name must match:
# the name alone is ambiguous (`merge-tests-api` is `merge-tests` of `api` or
# `merge` of `tests-api`).
# `NNN` is at least three digits: a thousandth record is `1000-...`.
_NAME_RE = re.compile(r"^(\d{3,})-(.+)\.started\.json$")
# A marker line: `WORD:WORD...`, as buildme prints them (`SPLIT:REQUIRED:<path>`).
_MARKER_RE = re.compile(r"^[A-Z][A-Z_]*:[A-Z][A-Z_]*(?::|$)")
_NOT_MARKERS = ("PHASE:", "RESULT:", "BOARD:", "BUDGET:")


@dataclass
class Attempt:
    n: int
    kind: str
    child: str
    path: Path            # agents/<NNN>-<kind>-<child>, without a suffix
    started: dict
    result: dict | None

    @property
    def log(self) -> Path:
        return self.path.with_name(self.path.name + ".log")

    @property
    def result_path(self) -> Path:
        return self.path.with_name(self.path.name + ".result.json")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def attempts(epic_dir: Path) -> list[Attempt]:
    """Every attempt under `agents/`, in number order."""
    out = []
    root = Path(epic_dir) / AGENTS_DIR
    for p in sorted(root.glob("*.started.json")) if root.is_dir() else []:
        m = _NAME_RE.match(p.name)
        started = _read_json(p) if m else None
        if started is None:
            continue
        kind, child = started.get("kind"), started.get("child")
        if kind not in KINDS or not isinstance(child, str) or m.group(2) != f"{kind}-{child}":
            continue
        base = p.with_name(p.name[:-len(".started.json")])
        out.append(Attempt(int(m.group(1)), kind, child, base, started,
                           _read_json(base.with_name(base.name + ".result.json"))))
    return sorted(out, key=lambda a: a.n)     # by number: `1000-` sorts before `999-` by name


def start(epic_dir: Path, kind: str, child: str, argv: list[str], *,
          head_sha: str | None = None, pr_round: int | None = None) -> Attempt:
    """Write the started record (and an empty log) for the next free number."""
    root = Path(epic_dir) / AGENTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    n = max((a.n for a in attempts(epic_dir)), default=0) + 1
    base = root / f"{n:03d}-{kind}-{child}"
    started = {"kind": kind, "child": child, "argv": list(argv), "pr_round": pr_round,
               "head_sha": head_sha, "at": _now()}
    base.with_name(base.name + ".log").touch()
    _write_atomic(base.with_name(base.name + ".started.json"), json.dumps(started, indent=2) + "\n")
    return Attempt(n, kind, child, base, started, None)


def markers(log: Path) -> list[str]:
    """The marker lines a process printed, in order: `WORKTREE:`, `BRANCH:`,
    `PR:` and `SPEND:`, and any `WORD:WORD` line but the `PHASE:`, `RESULT:`,
    `BOARD:` and `BUDGET:` progress lines."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in (raw.strip() for raw in lines):
        named = line.split(":", 1)[0] in NAMED_MARKERS and ":" in line
        if named or (_MARKER_RE.match(line) and not line.startswith(_NOT_MARKERS)):
            out.append(line)
    return out


def marker(found: list[str], name: str) -> list[str]:
    """The values of every `<name>:<value>` marker."""
    return [m[len(name) + 1:] for m in found if m.startswith(name + ":")]


def spend_of(found: list[str]) -> float | None:
    """The USD of the last `SPEND:<usd>:<tokens>` marker; None without a readable one."""
    for value in reversed(marker(found, "SPEND")):
        try:
            return float(value.split(":", 1)[0])
        except ValueError:
            continue
    return None


def spend_incomplete(found: list[str]) -> bool:
    """True when the `SPEND:` marker `spend_of` reads ends `:incomplete`: a skill the
    process ran printed no spend, so the USD on it is a lower bound."""
    for value in reversed(marker(found, "SPEND")):
        parts = value.split(":")
        try:
            float(parts[0])
        except ValueError:
            continue
        return parts[2:3] == ["incomplete"]
    return False


def finish(attempt: Attempt, exit_code: int | None, *, interrupted: bool, extra: dict | None = None) -> dict:
    """Write the result record, markers, PR URL and spend read from the attempt's
    log, plus *extra* (a review's fields, item 22)."""
    found = markers(attempt.log)
    prs = marker(found, "PR")
    result = {"exit_code": exit_code, "markers": found, "pr": prs[-1] if prs else None,
              "interrupted": interrupted, "spend": spend_of(found), **(extra or {}), "at": _now()}
    _write_atomic(attempt.result_path, json.dumps(result, indent=2) + "\n")
    attempt.result = result
    return result


def wait_marker(found: list[str]) -> str | None:
    """The last marker that is not one of WORKTREE/BRANCH/PR/SPEND: what an
    exit 3 waits on (`SPLIT:REQUIRED:<path>`, `INTERVIEW:WAITING`, ...)."""
    others = [m for m in found if m.split(":", 1)[0] not in NAMED_MARKERS]
    return others[-1] if others else None
