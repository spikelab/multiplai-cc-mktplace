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


def run_build(config: BuildConfig, args, spec=None) -> int:
    """One `build` invocation, in this process. The spec generator is a fake
    (*spec*, default: one that fails), so a build that goes on past the split
    stops right there."""
    from unittest.mock import AsyncMock, patch

    from build_pipeline.orchestrator import run_orchestrator

    spec = spec or AsyncMock(return_value=1)
    with patch("build_pipeline.spec_generator.run_spec_generator", spec):
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
        if not p.is_file() or rel in NOT_COMPARED or re.match(r".*\.tmp$", rel):
            continue
        text = p.read_text()
        if p.suffix == ".json":
            out[rel] = _norm(json.loads(text), root)
        else:
            out[rel] = text.replace(str(root), "<root>")
    return out


def state_summary(config: BuildConfig, root: Path) -> dict[str, Any]:
    """The checkpoint fields the epic step owns."""
    s = load_state(config)
    return {"phase": s.phase, "epic_dir": (s.epic_dir or "").replace(str(root), "<root>")}
