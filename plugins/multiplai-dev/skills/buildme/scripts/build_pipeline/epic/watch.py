"""`build_pipeline epic watch`: run an epic again until it is done or a
person is needed (plan-factory-merged-2026-10-29.md, item 68).

Each round runs the epic command (`build --change <epic> --auto`, in its
own session, its output appended to `<epic dir>/watch-epic.log`), then reads
its exit code, its output, the logs of the children's attempts that round
started, and `epic status` (`status.epic_status`). Then it does one of:

- **stop, done** (exit 0): the epic exited 0 and every child is merged;
- **stop, a person is needed** (exit 3): the epic printed a marker that waits
  on a person (`PERSON_MARKERS`: a manual merge, the spend cap, a feature
  branch conflict, a spec refusal, a split), or `epic status` shows a child
  parked, or waiting on a plan approval or on another child's specs;
- **wait and run again** after a network failure (`NETWORK`), an agent
  timeout (exit 4 with no `SPEC:ISSUES:`) or a hit Claude usage limit. For
  the usage limit it waits until the reset time the error names (one minute
  past it); with none, and for the other two, it waits 5 minutes, then twice
  as long each time in a row, up to 60;
- **stop, another failure** (exit 1): anything else. The loop never runs the
  epic again for a failure it cannot name.

On a stop it posts one Slack message through the pipeline-viewer's
`post` command (the `Slack` class `notify` uses: `SLACK_TOKEN`, the
channel in `specs/config.yaml`, `slack-outbox.md` without either), keyed by
what it stopped on, so running the loop again on the same state posts
nothing new.

Before each run it waits while another process holds the epic's lock
(`status.epic_live`) and while a child's build or fixer from an earlier run
is alive, found by pid and command line as the coordinator's `_recover`
does (`status.child_status` runs `coordinator._argv_pid` and `fix_pid`): a
child can outlive its coordinator. It never starts a second epic (row S49).

Every decision is a line in `<epic dir>/watch.log`. A usage wait is written
as `WAIT reason=usage until=<UTC time>` before the wait starts, so a loop
killed while it waits and started again reads the time back and waits until
then (row S50).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import git_ops
from ..change_manager import normalize_change_name
from . import coordinator, records, status, store

WATCH_LOG = "watch.log"
EPIC_OUTPUT = "watch-epic.log"
POLL = 60.0  # seconds between looks at a running epic or child
BACKOFF_FIRST = 300.0  # the first wait after a passing fault, 5 minutes
BACKOFF_MAX = 3600.0  # the longest, 60 minutes
RESET_MARGIN = 60.0  # seconds past a usage reset before running again (judgment)

EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_PERSON = 3

# What an epic prints when it waits on a person (`__main__` exit codes).
PERSON_MARKERS = (
    coordinator.MARKER_MERGE_WAITING,
    coordinator.MARKER_BUDGET,
    "FEATURE_BRANCH:CONFLICT",
    "SPEC:NOT_READY",
    "SPEC:ISSUES",
    "SPLIT:REQUIRED",
    "RESEARCH:NEEDED",
)
# Words a failed network read leaves in a log: the resolver (`EAI_AGAIN`, the
# 10-28 outage), Node's socket errors, git's and curl's, gh's and the Claude
# CLI's. A list, not a proof: a failure worded otherwise stops the loop.
NETWORK = re.compile(
    r"EAI_AGAIN|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENETUNREACH"
    r"|Could not resolve host|Temporary failure in name resolution"
    r"|Name or service not known|nodename nor servname provided"
    r"|Network is unreachable|Connection reset by peer|Connection timed out"
    r"|Failed to connect to|error connecting to|API Error: Connection error",
    re.IGNORECASE,
)
# The Claude CLI's limit text anywhere in a line (multiplai-core's
# `UsageLimitError` reads it at the start of an answer; here it is inside a
# log line): "You've hit your session limit · resets 9:40am (UTC)".
USAGE = re.compile(
    r"you(?:'|’)ve hit your [^\n·]{0,60}?\s*(?:limit|budget)\b"
    r"(?:\s*·\s*resets\s+(?P<resets>[^()\n·]*(?:\((?!exit code)[^()\n]*\))?))?",
    re.IGNORECASE,
)
_RESET = re.compile(
    r"^(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)"
    r"\s*(?:\((?P<tz>[^)]+)\))?\s*$",
    re.IGNORECASE,
)
_MONTHS = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]
_LOCKED = "is running now ("  # `lock.EpicLocked`, printed by `phase` with exit 2
_TIMEOUT_EXIT = 4  # `tdd_engine.EXIT_AGENT_TIMEOUT`, shared with SPEC:ISSUES


class Clock:
    """The watch's one clock and sleep: tests pass a fake."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


@dataclass
class Outcome:
    """What one run of the epic leads to."""

    stop: bool
    why: str
    code: int = EXIT_FAILED  # the watch's exit code, when it stops
    until: datetime | None = None  # when it runs again, when it waits
    reason: str = ""  # `usage` for a usage wait, `backoff` for a doubling one


# --- reading a run -----------------------------------------------------------------


def usage_reset(text: str) -> tuple[bool, str]:
    """Whether *text* holds a hit usage limit, and the reset it names ("")."""
    found = list(USAGE.finditer(text))
    if not found:
        return False, ""
    return True, (found[-1].group("resets") or "").strip()


def reset_time(resets: str, now: datetime) -> datetime | None:
    """The next moment after *now* that the CLI's reset text names
    ("9:40am (UTC)", "5pm", "Oct 19, 4am (Europe/Rome)"), in UTC. A time with
    no zone is UTC. None when the text is not one of those shapes."""
    m = _RESET.match(resets.strip())
    if not m:
        return None
    try:
        zone = ZoneInfo(m.group("tz").strip()) if m.group("tz") else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m.group("h")) % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    if hour > 23 or minute > 59:
        return None
    local = now.astimezone(zone)
    if m.group("mon"):
        mon = m.group("mon").lower()[:3]
        if mon not in _MONTHS:
            return None
        try:
            at = local.replace(
                month=_MONTHS.index(mon) + 1,
                day=int(m.group("day")),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if at <= local - timedelta(days=180):  # "Jan 2" read in December
            at = at.replace(year=at.year + 1)
    else:
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
    return at.astimezone(UTC)


def _person(text: str, rows: list[dict]) -> list[str]:
    """Why the epic waits on a person: its markers, then each child parked,
    or waiting on a plan approval or on another child's specs."""
    why = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(PERSON_MARKERS) and line not in why:
            why.append(line)
    for row in rows:
        blocker = row.get("blocker") or ""
        if row.get("state") == "parked" or (
            row.get("state") == "waiting"
            and blocker.startswith((coordinator.PLAN_WAITING, coordinator.WAITING_ON))
        ):
            why.append(f"{row.get('child')} {row.get('state')}: {blocker}")
    return why


def _timed_out(code: int, text: str, new: list[records.Attempt]) -> bool:
    """An agent timeout: exit 4 with no `SPEC:ISSUES:` marker, from the epic or
    from a child's build this run started (`Coordinator._classify` tells the
    two apart the same way)."""
    if code == _TIMEOUT_EXIT and "SPEC:ISSUES:" not in text:
        return True
    return any(
        a.kind == "build"
        and (a.result or {}).get("exit_code") == _TIMEOUT_EXIT
        and not any(
            m.startswith("SPEC:ISSUES:") for m in (a.result or {}).get("markers", [])
        )
        for a in new
    )


def decide(
    code: int,
    text: str,
    rows: list[dict],
    new: list[records.Attempt],
    logs: str,
    now: datetime,
    backoff: float,
) -> Outcome:
    """The outcome of a run that exited *code* and printed *text*, with
    `epic status` *rows* after it, the attempts *new* it started and *logs*,
    their logs' text. *backoff*: the next doubling wait."""
    if code == 0 and rows and all(r.get("state") == "merged" for r in rows):
        return Outcome(True, "the epic is done: every child is merged", EXIT_DONE)
    if code == 2 and _LOCKED in text:
        return Outcome(False, "another process runs the epic", until=now)
    both = f"{text}\n{logs}"
    hit, resets = usage_reset(both)
    if hit:
        at = reset_time(resets, now) if resets else None
        if at is not None:
            return Outcome(
                False,
                f"the Claude usage limit was hit; it resets {resets}",
                until=at + timedelta(seconds=RESET_MARGIN),
                reason="usage",
            )
        return Outcome(
            False,
            "the Claude usage limit was hit"
            + (f"; cannot read its reset time {resets!r}" if resets else ""),
            until=now + timedelta(seconds=backoff),
            reason="usage-backoff",
        )
    net = NETWORK.search(both)
    if net:
        return Outcome(
            False,
            f"a network failure ({net.group(0)})",
            until=now + timedelta(seconds=backoff),
            reason="backoff",
        )
    if _timed_out(code, text, new):
        return Outcome(
            False,
            "an agent timed out (exit 4)",
            until=now + timedelta(seconds=backoff),
            reason="backoff",
        )
    person = _person(text, rows)
    if person:
        return Outcome(True, "a person is needed: " + "; ".join(person), EXIT_PERSON)
    last = [
        ln.strip()
        for ln in text.splitlines()
        if records._MARKER_RE.match(ln.strip())
        and not ln.strip().startswith(records._NOT_MARKERS)
        and not ln.strip().startswith(("WATCH:", "SPEND:"))
    ][-3:]
    return Outcome(
        True,
        f"the epic exited {code}, which this loop does not run again on"
        + (f"; last markers: {', '.join(last)}" if last else ""),
        EXIT_FAILED,
    )


# --- the watch log -----------------------------------------------------------------


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class WatchLog:
    def __init__(self, path: Path, clock: Clock):
        self.path, self.clock = Path(path), clock

    def write(self, text: str) -> None:
        line = f"{_stamp(self.clock.now())} {text}\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
        print(f"WATCH: {text}", flush=True)

    def usage_wait(self) -> datetime | None:
        """The `until` of the last line when it is a usage wait (row S50):
        a loop stopped during that wait left it last."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        if not lines:
            return None
        m = re.search(r" WAIT reason=usage until=(\S+)", lines[-1])
        return _parse(m.group(1)) if m else None


# --- running -----------------------------------------------------------------------


def epic_argv(change: str, project: Path, *, session_id: str, trust: bool) -> list:
    """The epic command of the plan's Paths: `build --change <epic> --auto`."""
    argv = [sys.executable, "-m", "build_pipeline"]
    if session_id:
        argv += ["--session-id", session_id]
    argv += ["build", "--change", change, "--project-dir", str(project), "--auto"]
    return argv + (["--trust-repo"] if trust else [])


def run_command(argv: list[str], output: Path) -> int:
    """Run the epic in its own session, its output appended to *output*: a
    watch stopped with Ctrl-C or killed leaves the epic running (row S49)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("ab") as out:
        return subprocess.run(
            argv,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(coordinator.SCRIPTS_DIR),
            start_new_session=True,
            check=False,
        ).returncode


def pipeline_viewer_dir() -> Path:
    """The pipeline-viewer skill's scripts, beside buildme in the plugin."""
    return coordinator.SCRIPTS_DIR.parents[1] / "pipeline-viewer" / "scripts"


def slack_post(repo: Path, change: str, key: str, text: str) -> str:
    """Post *text* once per *key* through `pipeline_viewer post`; returns its
    `SLACK:<outcome>:<detail>` line, or why it could not run."""
    argv = [
        "uv",
        "run",
        "-q",
        "--directory",
        str(pipeline_viewer_dir()),
        "python",
        "-m",
        "pipeline_viewer",
        "post",
        "--repo",
        str(repo),
        "--change",
        change,
        "--key",
        key,
    ]
    try:
        res = subprocess.run(
            argv, input=text, capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"SLACK:FAILED:{type(e).__name__}"
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("SLACK:")]
    if lines:
        return lines[-1]
    return f"SLACK:FAILED:exit {res.returncode} {res.stderr.strip()[-200:]}".rstrip()


@dataclass
class Watch:
    """One epic's loop; every outside effect is a field, so tests pass fakes."""

    change: str
    epic_dir: Path
    run_epic: Callable[[Path], int]  # (output file) -> exit code
    read_status: Callable[[], list[dict]]
    notify: Callable[[str, str], str]  # (key, text) -> SLACK:... line
    clock: Clock
    epic_live: Callable[[Path], dict | None] = status.epic_live

    def __post_init__(self) -> None:
        self.log = WatchLog(self.epic_dir / WATCH_LOG, self.clock)
        self.output = self.epic_dir / EPIC_OUTPUT

    def _rows(self) -> list[dict]:
        try:
            return self.read_status()
        except status.StatusError:  # no epic yet: the first run splits it
            return []

    def _busy(self) -> str | None:
        """Why the epic cannot be run now: a live lock holder, or a child of an
        earlier run that is alive, or one `ps` cannot answer for."""
        holder = self.epic_live(self.epic_dir)
        if holder is not None:
            return f"the epic runs now (pid {holder.get('pid')}); waiting for it"
        for row in self._rows():
            process = row.get("process") or {}
            if process.get("alive"):
                return (
                    f"{row.get('child')}'s {process.get('kind')} from an earlier run "
                    "is alive; waiting for it"
                )
            if row.get("state") == "unknown" and "cannot tell whether its" in (
                row.get("blocker") or ""
            ):
                return f"{row.get('child')}: {row.get('blocker')}; waiting"
        return None

    def _wait_until(self, until: datetime) -> None:
        while True:
            left = (until - self.clock.now()).total_seconds()
            if left <= 0:
                return
            self.clock.sleep(min(left, POLL))

    def _wait_free(self) -> None:
        said = None
        while (why := self._busy()) is not None:
            if why != said:
                self.log.write(f"BUSY {why}")
                said = why
            self.clock.sleep(POLL)

    def _new_attempts(self, before: int) -> list[records.Attempt]:
        return [a for a in records.attempts(self.epic_dir) if a.n > before]

    def run(self) -> int:
        resume = self.log.usage_wait()
        if resume is not None and resume > self.clock.now():
            self.log.write(f"RESUME waiting for the usage reset until {_stamp(resume)}")
            self._wait_until(resume)
        backoff = BACKOFF_FIRST
        while True:
            self._wait_free()
            tries = records.attempts(self.epic_dir)
            before = max((a.n for a in tries), default=0)
            try:
                start = self.output.stat().st_size
            except OSError:
                start = 0
            self.log.write(f"RUN the epic {self.change}")
            code = self.run_epic(self.output)
            try:
                with self.output.open("rb") as f:
                    f.seek(start)
                    text = f.read().decode("utf-8", errors="replace")
            except OSError:
                text = ""
            new = self._new_attempts(before)
            logs = "\n".join(_read(a.log) for a in new)
            rows = self._rows()
            out = decide(code, text, rows, new, logs, self.clock.now(), backoff)
            self.log.write(f"EXIT {code}")
            if out.stop:
                self.log.write(f"STOP exit={out.code} why={out.why}")
                key = hashlib.sha256(out.why.encode()).hexdigest()[:16]
                posted = self.notify(key, _message(self.change, out))
                self.log.write(f"NOTIFY {posted}")
                return out.code
            if out.reason in ("backoff", "usage-backoff"):
                backoff = min(backoff * 2, BACKOFF_MAX)
            else:
                backoff = BACKOFF_FIRST
            if out.until is not None and out.until > self.clock.now():
                tag = "usage" if out.reason == "usage" else out.reason or "lock"
                self.log.write(
                    f"WAIT reason={tag} until={_stamp(out.until)} why={out.why}"
                )
                self._wait_until(out.until)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _message(change: str, out: Outcome) -> str:
    head = {
        EXIT_DONE: "is done",
        EXIT_PERSON: "needs a person",
    }.get(out.code, "stopped")
    return (
        f"*{change}*: the epic {head}. {out.why}\n"
        f"See each child with `python -m build_pipeline epic status --change {change}`."
    )


def run_cli(args) -> int:
    """`epic watch`: run the epic until it is done or a person is needed."""
    project = Path(args.project_dir).absolute()
    repo = git_ops.repo_root(project) or project
    change = normalize_change_name(args.change)
    epic_dir = store.epic_dir_for(repo, change)
    viewer_repo = Path(args.viewer_repo).absolute() if args.viewer_repo else repo
    argv = epic_argv(
        args.change,
        project,
        session_id=getattr(args, "session_id", "") or "",
        trust=bool(getattr(args, "trust_repo", False)),
    )
    return Watch(
        change=change,
        epic_dir=epic_dir,
        run_epic=lambda output: run_command(argv, output),
        read_status=lambda: status.epic_status(project, args.change),
        notify=lambda key, text: slack_post(viewer_repo, change, key, text),
        clock=Clock(),
    ).run()
