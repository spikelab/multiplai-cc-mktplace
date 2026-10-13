"""Helpers for the epic-mode stop-and-resume row tests.

Each row marked `Test: yes` in the "Stop and resume states" tables of
plan-buildme-epic-mode-2026-10-12.md runs a stopped run and a never-stopped
run in two temporary roots with the same fixtures and fakes, then compares
them ("How the row tests compare").

- Layout: each bare origin at `<root>/o/<name>.git`, so both runs' slugs are
  `o-<name>` and their epic paths match; `WORKSPACE=<root>/ws`; constant git
  identities and dates.
- Epic directory: `epic_snapshot` compares every file under it except `.lock`
  and the log, with the keys `at`, `since`, `pid`, `host` and `*_at` left out
  of JSON and the root path rewritten.
- Calls: only launches and writing calls are logged (`CallLog`), never
  read-only calls. In PR A the one launch is `epic_split`.
- A kill is a forked child exiting 137 at a named seam (`run_killed`), as
  `row_helpers.run_killed_at` does for git calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.epic.split import EpicChild, EpicSplitAnswer
from build_pipeline.models import BuildPhase
from build_pipeline.state import BuildState

DROP_KEYS = {"at", "since", "pid", "host"}          # and every key ending in "_at"
NOT_COMPARED = {".lock", "progress.log"}            # the lock and the log: never compared
NOT_COMPARED_DIRS = ("agents/", "runs/")            # compared as records_snapshot and last_summary

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Row Test", "GIT_AUTHOR_EMAIL": "row@example.invalid",
    "GIT_COMMITTER_NAME": "Row Test", "GIT_COMMITTER_EMAIL": "row@example.invalid",
    "GIT_AUTHOR_DATE": "2026-10-13T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-10-13T12:00:00+00:00",
}


def use_workspace(monkeypatch, root: Path) -> Path:
    """Point `get_paths().data_dir` at `<root>/ws/.multiplai/data` and pin git identities."""
    from multiplai_core import paths

    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSPACE", str(ws))
    for name in ("CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR", "CLAUDE_PLUGIN_OPTION_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    paths._reset_cache()
    return ws / ".multiplai" / "data"


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_project(root: Path, name: str = "r") -> Path:
    """A clone at `<root>/p` of a bare origin at `<root>/o/<name>.git`, with one commit."""
    origin = root / "o" / f"{name}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    project = root / "p"
    _git(root, "clone", "-q", str(origin), str(project))
    (project / "README.md").write_text("# r\n")
    _git(project, "add", "README.md")
    _git(project, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    _git(project, "push", "-q", "origin", "HEAD:main")
    return project


def move_origin(root: Path) -> str:
    """Push one more commit to origin's main from another clone; its sha."""
    other = root / "other"
    _git(root, "clone", "-q", str(root / "o" / "r.git"), str(other))
    (other / "y.txt").write_text("y\n")
    _git(other, "add", "y.txt")
    _git(other, "-c", "commit.gpgsign=false", "commit", "-qm", "moved")
    _git(other, "push", "-q", "origin", "HEAD:main")
    return _git(other, "rev-parse", "HEAD").strip()


def make_build(project: Path, change: str = "e", *, mode: str = "scratch",
               phase: BuildPhase = BuildPhase.RESEARCH) -> BuildConfig:
    """A build of *change* whose shaping is done up to *phase*, with a ticket
    in the change dir and no source files (so the codebase analysis makes no
    call). No worktree: the epic files live in the data directory, not in it."""
    from build_pipeline.change_manager import ChangeManager

    config = BuildConfig(
        project_dir=project, change_name=change, mode=mode, auto=True,
        spec_only=True, skip_research=True, git=GitToggles(worktree=False),
    )
    config.specs_dir = project / "specs"
    config.prototype_mode = "false"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    cm.create_change(change)
    (config.change_dir / "ticket.md").write_text("# Ticket #1\n\nBuild the epic.\n")
    state = BuildState(change_name=change, mode=mode, tier="advanced", phase=phase,
                       bootstrap_done=True, state_file=str(config.state_file_path()))
    state.checkpoint(config.state_file_path())
    return config


def args_for(config: BuildConfig, **over) -> Any:
    import argparse

    base = dict(mode=config.mode, change=config.change_name, project_dir=str(config.project_dir),
                auto=True, spec_only=True, skip_research=True, interview_summary="",
                research_path="", context_files=[], session_id="s",
                approve_split=False, no_epic_split=False)
    base.update(over)
    return argparse.Namespace(**base)


def run_build(config: BuildConfig, args, spec=None, *, coordinator: bool = False) -> int:
    """One `build` invocation, in this process. The spec generator is a fake
    (*spec*, default: one that fails), so a build that goes on past the split
    stops right there. Unless *coordinator*, an approved epic's coordinator
    (PR B1) is a stub that returns 0, so PR A's cases launch no child."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    from build_pipeline.orchestrator import run_orchestrator

    spec = spec or AsyncMock(return_value=1)
    with ExitStack() as stack:
        stack.enter_context(patch("build_pipeline.spec_generator.run_spec_generator", spec))
        if not coordinator:
            stack.enter_context(patch("build_pipeline.epic.coordinator.run_epic", AsyncMock(return_value=0)))
        return asyncio.run(run_orchestrator(config, args))


def load_state(config: BuildConfig) -> BuildState:
    return BuildState.load(config.state_file_path())


# --- the fake model call ------------------------------------------------------

PLAN = "# Plan: {title}\n\n**Objective** — {title}.\n\n## Work items\n\n1. Do it.\n\n## Done means\n\n1. It is done.\n"


def child(cid: str, deps: tuple[str, ...] = (), title: str | None = None) -> EpicChild:
    title = title or f"Child {cid}"
    return EpicChild(id=cid, title=title, depends_on=list(deps), plan=PLAN.format(title=title))


THREE = EpicSplitAnswer(single=False, reason="Three parts.",
                        children=[child("api"), child("ui"), child("docs", ("api", "ui"))])
CYCLE = EpicSplitAnswer(single=False, reason="Cycle.",
                        children=[child("a", ("b",)), child("b", ("a",))])
SINGLE = EpicSplitAnswer(single=True, reason="One change.")


class CallLog:
    """Every launch, one JSON line per call, in a file so a forked child's calls count."""

    def __init__(self, path: Path):
        self.path = path

    def add(self, kind: str, **fields) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"kind": kind, **fields}, sort_keys=True) + "\n")

    def calls(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines()]


class FakeSplit:
    """`epic.split.epic_split`, answering from its arguments: the first answer
    of an invocation is `first`, a retry (errors given) is `retry`. With
    `die_on_call=n`, the n-th call this process makes is launched and logged,
    then the process exits 137 as SIGKILL would."""

    def __init__(self, log: CallLog, first: EpicSplitAnswer, retry: EpicSplitAnswer | None = None):
        self.log, self.first, self.retry = log, first, retry or first
        self.die_on_call: int | None = None
        self.n = 0

    async def __call__(self, config, inputs, errors=(), *, call=None):
        self.n += 1
        self.log.add("epic_split", retry=bool(errors))
        if self.die_on_call == self.n:
            os._exit(137)
        return (self.retry if errors else self.first).model_copy(deep=True)


# --- stopping -----------------------------------------------------------------

def run_killed(fn: Callable[[], Any], setup: Callable[[], Any] = lambda: None) -> None:
    """Run fn() in a forked child that is expected to exit 137 at a seam *setup*
    installs (in the child only); assert it did."""
    pid = os.fork()
    if pid == 0:
        try:
            setup()
            fn()
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 137, "the child never reached the kill point"


def die_at(obj: Any, attr: str, *, after: bool = False) -> Callable[[], None]:
    """A setup for `run_killed`: replace obj.attr so its first call exits 137,
    before it runs or (after=True) once it has returned."""

    def setup() -> None:
        real = getattr(obj, attr)

        def dying(*a, **k):
            if not after:
                os._exit(137)
            out = real(*a, **k)
            os._exit(137)
            return out

        setattr(obj, attr, dying)

    return setup


# --- comparing ----------------------------------------------------------------

def _norm(v: Any, root: Path) -> Any:
    if isinstance(v, dict):
        return {k: _norm(x, root) for k, x in v.items()
                if k not in DROP_KEYS and not k.endswith("_at")}
    if isinstance(v, list):
        return [_norm(x, root) for x in v]
    return v.replace(str(root), "<root>") if isinstance(v, str) else v


def epic_snapshot(epic_dir: Path, root: Path) -> dict[str, Any] | None:
    """Every file of the epic directory, normalised; None when it does not exist."""
    if not epic_dir.exists():
        return None
    out: dict[str, Any] = {}
    for p in sorted(epic_dir.rglob("*")):
        rel = p.relative_to(epic_dir).as_posix()
        if not p.is_file() or rel in NOT_COMPARED or re.match(r".*\.tmp$", rel) \
                or rel.startswith(NOT_COMPARED_DIRS):
            continue
        text = p.read_text()
        if p.suffix == ".json":
            out[rel] = _norm(json.loads(text), root)
        else:
            out[rel] = text.replace(str(root), "<root>")
    return out


def state_summary(config: BuildConfig, root: Path) -> dict[str, Any]:
    """The checkpoint fields the epic step owns; None once a finished build removed it."""
    if not config.state_file_path().exists():
        return None
    s = load_state(config)
    return {"phase": s.phase, "epic_dir": (s.epic_dir or "").replace(str(root), "<root>"),
            "epic_single": s.epic_single}


# --- PR B1: the coordinator's fakes, seams and records ---------------------------

FAKE_BUILD = r'''#!{python}
"""A fake `python -m build_pipeline ... build`: the real markers, a fake PR."""
import json, os, pathlib, signal, subprocess, sys, time

argv = sys.argv[1:]
root = pathlib.Path(os.environ["EPIC_FAKE_ROOT"])


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


plan = opt("--plan-file")
cid = pathlib.Path(plan).stem
change = opt("--change")
counts = root / "attempts"
counts.mkdir(exist_ok=True)
n = len(list(counts.glob(cid + ".*"))) + 1
(counts / f"{{cid}}.{{n}}").write_text("")
with (root / "calls.log").open("a") as f:
    f.write(json.dumps({{"kind": "build", "argv": [a.replace(str(root), "<root>") for a in argv]}},
                       sort_keys=True) + "\n")
fx = json.loads((root / "fakes.json").read_text()) if (root / "fakes.json").exists() else {{}}
steps = fx.get(cid, [])
step = steps[n - 1] if n <= len(steps) else (steps[-1] if steps else {{}})

project = pathlib.Path(opt("--project-dir"))
top = git("rev-parse", "--show-toplevel", cwd=project).stdout.strip()
if git("status", "--porcelain", "--untracked-files=no", cwd=top).stdout.strip():
    print(f"ERROR:{{top}} has uncommitted changes in tracked files. Commit or stash them.",
          file=sys.stderr, flush=True)
    sys.exit(1)
wt = pathlib.Path(os.environ["WORKSPACE"]) / ".worktrees" / f"buildme-{{change}}"
branch = f"buildme/{{change}}"
if wt.exists():
    print(f"WORKTREE:{{wt}}", flush=True)
else:
    base = opt("--base-ref")
    git("worktree", "add", "-b", branch, str(wt), *([base] if base else []), cwd=top)
    print(f"WORKTREE:{{wt}}", flush=True)
    print(f"BRANCH:{{branch}}", flush=True)
    (root / "bases").mkdir(exist_ok=True)
    (root / "bases" / cid).write_text(git("rev-parse", "HEAD", cwd=wt).stdout.strip())
if step.get("grandchild"):      # a process in the build's group, as a real build's agents are
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (root / "grandchildren").mkdir(exist_ok=True)
    (root / "grandchildren" / f"{{cid}}.{{n}}").write_text(str(kid.pid))
if step.get("ignore_signals"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if step.get("block"):
    gate = root / "release" / cid
    while not gate.exists():
        time.sleep(0.02)
if step.get("marker"):
    print(step["marker"], flush=True)
    sys.exit(3)
if step.get("exit"):
    sys.exit(step["exit"])
if step.get("no_pr"):
    sys.exit(0)
url = f"https://example.invalid/o/r/pull/{{change}}"
prs_file = root / "prs.json"
prs = json.loads(prs_file.read_text()) if prs_file.exists() else {{}}
prs.setdefault(url, {{"branch": branch, "state": "OPEN"}})
(root / f"prs.{{os.getpid()}}.tmp").write_text(json.dumps(prs))
os.replace(root / f"prs.{{os.getpid()}}.tmp", prs_file)     # readers never see half a file
if step.get("die_before_pr_line"):
    os.kill(os.getpid(), signal.SIGKILL)
print(f"PR:{{url}}", flush=True)
'''

FAKE_GH = r'''#!{python}
"""A fake `gh`: reads only, from `<root>/prs.json`."""
import json, os, pathlib, sys

root = pathlib.Path(os.environ["EPIC_FAKE_ROOT"])
prs = json.loads((root / "prs.json").read_text()) if (root / "prs.json").exists() else {{}}
a = sys.argv[1:]
if (root / "gh-fails").exists():
    print("fake gh: offline", file=sys.stderr)
    sys.exit(1)
if a[:2] == ["pr", "view"]:
    pr = prs.get(a[2])
    if pr is None:
        print("no pull request found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"state": pr["state"]}}))
elif a[:2] == ["pr", "list"]:
    head = a[a.index("--head") + 1]
    print(json.dumps([{{"url": u}} for u, p in prs.items() if p["branch"] == head]))
else:
    print("fake gh: unexpected " + " ".join(a), file=sys.stderr)
    sys.exit(2)
'''


class Fakes:
    """The fake build and `gh` first on PATH, answering from `<root>/fakes.json`
    (per child, one step per attempt: `block`, `marker`, `exit`, `no_pr`,
    `die_before_pr_line`, `grandchild`, `ignore_signals`) and `<root>/prs.json`; launches go to
    `<root>/calls.log`. `<root>/gh-fails` makes every `gh` call fail; `ps_fails`
    puts a `ps` that exits 1 first on PATH."""

    def __init__(self, root: Path, monkeypatch, steps: dict[str, list[dict]] | None = None):
        import sys

        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        for name, text in (("python", FAKE_BUILD), ("gh", FAKE_GH)):
            path = self.bin / name
            path.write_text(text.format(python=sys.executable))
            path.chmod(0o755)
        (root / "release").mkdir(exist_ok=True)
        (root / "fakes.json").write_text(json.dumps(steps or {}))
        (root / "psfail").mkdir(exist_ok=True)
        (root / "psfail" / "ps").write_text("#!/bin/sh\necho 'ps: failed' >&2\nexit 1\n")
        (root / "psfail" / "ps").chmod(0o755)
        self.use(monkeypatch)

    def ps_fails(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([str(self.root / "psfail"), os.environ["PATH"]]))

    def grandchildren(self) -> list[int]:
        return [int(p.read_text()) for p in sorted((self.root / "grandchildren").glob("*"))]

    def use(self, monkeypatch) -> None:
        path = [p for p in os.environ["PATH"].split(os.pathsep)
                if not (Path(p).parent / "fakes.json").exists()]     # another root's fakes
        monkeypatch.setenv("PATH", os.pathsep.join([str(self.bin), *path]))
        monkeypatch.setenv("EPIC_FAKE_ROOT", str(self.root))

    def release(self, cid: str) -> None:
        (self.root / "release" / cid).write_text("")

    def prs(self) -> dict:
        p = self.root / "prs.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def set_pr(self, url: str, state: str) -> None:
        prs = self.prs()
        prs[url]["state"] = state
        (self.root / "prs.tmp").write_text(json.dumps(prs))
        os.replace(self.root / "prs.tmp", self.root / "prs.json")

    def attempts(self, cid: str) -> int:
        return len(list((self.root / "attempts").glob(cid + ".*")))


def pr_url(change: str) -> str:
    return f"https://example.invalid/o/r/pull/{change}"


class FakeClock:
    """The coordinator's sleep-and-clock seam: time moves only when it sleeps,
    with a short real pause so the fakes on PATH get to run."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0.01)


def approve(config: BuildConfig, answer: EpicSplitAnswer, log: CallLog) -> None:
    """Split *config*'s build with *answer* and approve it, without running the coordinator."""
    from unittest.mock import AsyncMock, patch

    from build_pipeline.epic import coordinator, lock
    from build_pipeline.epic import split as split_mod

    with patch.object(split_mod, "epic_split", FakeSplit(log, answer)):
        assert run_build(config, args_for(config)) == 3
    with patch.object(coordinator, "run_epic", AsyncMock(return_value=0)):
        assert run_build(config, args_for(config, approve_split=True)) == 0
    lock.release_all()


def run_coordinator(config: BuildConfig, **over) -> int:
    """One re-run of the approved build: the coordinator runs, with the fake
    clock. Like a new process, it takes the epic lock itself and drops it at
    the end, so a child build holding the lock would make it exit 2."""
    from unittest.mock import patch

    from build_pipeline.epic import coordinator, lock

    lock.release_all()
    try:
        with patch.object(coordinator, "CLOCK", FakeClock()):
            return run_build(config, args_for(config, **over), coordinator=True)
    finally:
        lock.release_all()


def run_signalled(fn: Callable[[], int], ready: Callable[[], bool], sig: int) -> int:
    """Run fn() in a forked child, send it *sig* once ready() is true, and
    return the child's exit code (fn's return value)."""
    import time

    pid = os.fork()
    if pid == 0:
        rc = 1
        try:
            rc = fn()
        finally:
            os._exit(rc)
    for _ in range(1000):
        if ready():
            break
        time.sleep(0.02)
    else:
        os.kill(pid, 9)
        raise AssertionError("never ready to signal")
    os.kill(pid, sig)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def alive(pid: int) -> bool:
    """True while *pid* runs (a zombie counts as gone)."""
    ps = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return bool(ps.stdout.strip()) and not ps.stdout.strip().startswith("Z")


def records_snapshot(epic_dir: Path, root: Path) -> dict[str, list[tuple]]:
    """Per child, in order: (kind, argv, exit_code, markers, interrupted) of each
    attempt; the numbering, times and log contents are left out."""
    from build_pipeline.epic import records

    out: dict[str, list[tuple]] = {}
    for a in records.attempts(epic_dir):
        r = a.result or {}
        out.setdefault(a.child, []).append((
            a.kind, [str(x).replace(str(root), "<root>") for x in a.started.get("argv", [])],
            r.get("exit_code", "no result"),
            [m.replace(str(root), "<root>") for m in r.get("markers", [])],
            r.get("interrupted")))
    return out


def last_summary(epic_dir: Path, root: Path) -> dict | None:
    runs = sorted((epic_dir / "runs").glob("*/summary.json"))
    return _norm(json.loads(runs[-1].read_text()), root) if runs else None


def commit_names(repos: list[Path]) -> dict[str, str]:
    """sha -> `<repo k> <branch> <i>` for each commit on each branch's first-parent
    line (repos in the order given, branches by name; the first name wins)."""
    names: dict[str, str] = {}
    for k, repo in enumerate(repos):
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
        for b in sorted(branches):
            for i, sha in enumerate(_git(repo, "rev-list", "--first-parent", "--reverse", b).split()):
                names.setdefault(sha, f"<repo {k}> {b} {i}")
    return names


def name_commits(value: Any, names: dict[str, str]) -> Any:
    """Replace each 7-40 character hex string that prefixes exactly one named commit."""
    if isinstance(value, dict):
        return {k: name_commits(v, names) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(name_commits(v, names) for v in value)
    if not isinstance(value, str):
        return value

    def sub(m: re.Match) -> str:
        hits = [n for sha, n in names.items() if sha.startswith(m.group(0))]
        return hits[0] if len(hits) == 1 else m.group(0)

    return re.sub(r"\b[0-9a-f]{7,40}\b", sub, value)
