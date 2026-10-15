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


def make_project(root: Path, name: str = "r", dest: str = "p") -> Path:
    """A clone at `<root>/<dest>` of a bare origin at `<root>/o/<name>.git`, with
    one commit, its own (`# <name>`), so two repos never share a sha. The fakes
    find the clone of a PR's repo in `<root>/checkouts.json` (PR F)."""
    origin = root / "o" / f"{name}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    project = root / dest
    _git(root, "clone", "-q", str(origin), str(project))
    checkouts = root / "checkouts.json"
    known = json.loads(checkouts.read_text()) if checkouts.exists() else {}
    checkouts.write_text(json.dumps({**known, name: dest}))
    (project / "README.md").write_text(f"# {name}\n")
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
if "findings" in argv:      # `python -m build_pipeline findings ...`: the fake fixer, same pid
    os.execv(sys.executable, [sys.executable, str(pathlib.Path(__file__).with_name("fake_findings.py")), *argv])


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
if "spend" in step:     # as the real build: SPEND on every exit path but a kill
    import atexit
    atexit.register(lambda: print(f"SPEND:{{step['spend']}}:0", flush=True))
for _sig in (signal.SIGTERM, signal.SIGHUP):    # as the real build: these unwind, as Ctrl-C does
    signal.signal(_sig, lambda signum, _frame: sys.exit(128 + signum))

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
    if step.get("commit"):      # PR E: the child's own change, pushed as a real build pushes it
        for rel, text in step["commit"].items():
            (wt / rel).write_text(text)
        git("add", "--", *step["commit"], cwd=wt)
        git("-c", "commit.gpgsign=false", "commit", "-qm", f"child {{cid}}", cwd=wt)
        git("push", "-q", "-u", "origin", branch, cwd=wt)
if step.get("grandchild"):      # a process in the build's group, as a real build's agents are
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (root / "grandchildren").mkdir(exist_ok=True)
    (root / f"grandchild.{{cid}}.{{n}}.tmp").write_text(str(kid.pid))
    os.replace(root / f"grandchild.{{cid}}.{{n}}.tmp",     # a poller never reads an empty pid file
               root / "grandchildren" / f"{{cid}}.{{n}}")
if step.get("ignore_signals"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if step.get("block"):
    (root / "blocked").mkdir(exist_ok=True)     # past its commit and push: a kill now leaves them whole
    (root / "blocked" / f"{{cid}}.{{n}}").write_text("")
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
repo = pathlib.Path(git("remote", "get-url", "origin", cwd=top).stdout.strip()).stem   # PR F: `r` or another
url = f"https://example.invalid/o/{{repo}}/pull/{{step.get('pr_number', change)}}"
prs_file = root / "prs.json"
import fcntl
with (root / "prs.lock").open("a") as held:     # two builds ending at once both keep their PR
    fcntl.flock(held, fcntl.LOCK_EX)
    prs = json.loads(prs_file.read_text()) if prs_file.exists() else {{}}
    prs.setdefault(url, {{"branch": branch, "state": "OPEN", **step.get("pr", {{}})}})
    (root / f"prs.{{os.getpid()}}.tmp").write_text(json.dumps(prs))
    os.replace(root / f"prs.{{os.getpid()}}.tmp", prs_file)     # readers never see half a file
if step.get("die_before_pr_line"):
    os.kill(os.getpid(), signal.SIGKILL)
print(f"PR:{{url}}", flush=True)
'''

FAKE_GH = r'''#!{python}
"""A fake `gh`, from `<root>/prs.json`: reads, and (PR E2) `pr checks` and
`pr merge`. A PR's `checks` is one step per `pr checks --required` call (the
JSON list of checks, each required unless `"required": false`, which a
`pr checks` without `--required` reads too; a string: gh fails with it; no
`checks`, or `[]`: none at all). Its `merges` is one step per `pr merge`
call: `refuse`, refused with that stderr; `queue`, exits 0 and only queues
the PR; `lost`, merges and exits 1; `offline`, refused, then every later
`gh` call fails. `pr merge` is logged to `<root>/merges.log`; it is refused
when `--match-head-commit` is not origin's branch, or while origin's branch
lacks origin's main (`BEHIND`, as a branch rule requiring it to be up to
date), and otherwise merges the branch into origin's main from a scratch
clone and marks the PR `MERGED`. A PR's `lag`: after origin's branch
moves, the next `lag` `pr view` and `pr checks` reads still show its
previous head, whose checks are `old_checks` (none when absent), and `pr
merge` compares `--match-head-commit` with that head, as GitHub did on
spikelab/github-tests for about 1.5 s after a push. A PR's pinned `head`
gives way to origin's branch once that branch moves on top of it."""
import hashlib, json, os, pathlib, subprocess, sys


def number(change):
    return int(hashlib.sha1(change.encode()).hexdigest()[:4], 16) % 9000 + 1000


def at(origin, *a):
    return subprocess.run(["{git}", "--git-dir", str(origin), *a], capture_output=True, text=True)


def behind(origin, pr):
    pushed = at(origin, "rev-parse", "-q", "--verify", "refs/heads/" + pr["branch"]).stdout.strip()
    return bool(pushed) and at(origin, "merge-base", "--is-ancestor", "refs/heads/main", pushed).returncode != 0


def shown(url, pr, pushed, consume=True):
    """The head GitHub shows for the PR: *pushed*, or with `lag` its previous one."""
    if not pr.get("lag") or not pushed:
        return pushed
    f = root / f"shown-{{number(url.rsplit('/', 1)[1])}}.json"
    s = json.loads(f.read_text()) if f.exists() else {{"head": pushed, "next": None, "left": 0}}
    if pushed not in (s["head"], s["next"]):
        s["next"], s["left"] = pushed, pr["lag"]
    if consume and s["next"] == pushed:
        if s["left"] > 0:
            s["left"] -= 1
        else:
            s["head"], s["next"] = pushed, None
    f.write_text(json.dumps(s))
    return s["head"]


def nth(url, kind, steps, advance=True):
    counts = root / f"{{kind}}-attempts"
    counts.mkdir(exist_ok=True)
    key = str(number(url.rsplit("/", 1)[1]))
    k = len(list(counts.glob(key + ".*"))) + advance
    if advance:
        (counts / f"{{key}}.{{k}}").write_text("")
    return steps[min(max(k, 1), len(steps)) - 1] if steps else None


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
    origin = root / "o" / (a[2].split("/")[-3] + ".git")      # the PR's repo (PR F), `r` unless another
    change = a[2].rsplit("/", 1)[1]
    pushed = subprocess.run(["{git}", "--git-dir", str(origin), "rev-parse", "-q", "--verify",
                             "refs/heads/" + pr["branch"]], capture_output=True, text=True).stdout.strip()
    checkouts = json.loads((root / "checkouts.json").read_text()) if (root / "checkouts.json").exists() else {{}}
    pin = pr.get("head")
    if pin and pushed and pin != pushed and at(origin, "merge-base", "--is-ancestor", pin, pushed).returncode == 0:
        pin = None       # a push to the PR's branch on top of a pinned head moves it, as on GitHub (PR E2)
    head = pin or pushed or subprocess.run(       # origin's branch once pushed (PR E)
        ["git", "rev-parse", "refs/heads/" + pr["branch"]],
        cwd=root / checkouts.get(a[2].split("/")[-3], "p"),   # in the PR's repo's clone (PR F)
        capture_output=True, text=True).stdout.strip()
    head = shown(a[2], pr, head) if pushed and not pin else head
    print(json.dumps({{"state": pr["state"], "number": number(change), "headRefOid": head,
                      "mergeable": pr.get("mergeable", "MERGEABLE"),
                      "mergeStateStatus": pr.get("merge_state") or ("BEHIND" if behind(origin, pr) else "CLEAN")}}))
elif a[:2] in (["pr", "checks"], ["pr", "merge"]):
    url, pr = a[2], prs.get(a[2])
    if pr is None:
        print("no pull request found", file=sys.stderr)
        sys.exit(1)
    origin = root / "o" / (url.split("/")[-3] + ".git")
    if a[1] == "checks":
        required = "--required" in a
        pushed = at(origin, "rev-parse", "-q", "--verify", "refs/heads/" + pr["branch"]).stdout.strip()
        stale = shown(url, pr, pushed) != pushed
        every = (pr.get("old_checks", []) if stale
                 else nth(url, "checks", pr.get("checks", []), advance=required) or [])
        if isinstance(every, str):
            print(every, file=sys.stderr)
            sys.exit(1)
        checks = [x for x in every if x.get("required", True) or not required]
        if not checks:      # gh 2.102's words: no checks at all, or none of them required
            which = "no required checks" if every else "no checks"
            print(f"{{which}} reported on the '{{pr['branch']}}' branch", file=sys.stderr)
            sys.exit(1)
        print(json.dumps([{{"name": x["name"], "bucket": x["bucket"]}} for x in checks]))
        buckets = {{x["bucket"] for x in checks}}
        sys.exit(8 if "pending" in buckets else 1 if buckets & {{"fail", "cancel"}} else 0)
    with (root / "merges.log").open("a") as f:
        f.write(json.dumps({{"kind": "gh-merge", "argv": a}}, sort_keys=True) + "\n")
    step = nth(url, "merge", pr.get("merges", [])) or {{}}
    match = a[a.index("--match-head-commit") + 1] if "--match-head-commit" in a else None
    pushed = at(origin, "rev-parse", "-q", "--verify", "refs/heads/" + pr["branch"]).stdout.strip()
    refusal = (step.get("refuse") or ("GraphQL: Head branch was modified. Review and try the merge again. "
                                      "(mergePullRequest)" if match and match != shown(url, pr, pushed, False) else None)
               or ("X Pull request is not mergeable: the head branch is not up to date with the base branch."
                   if behind(origin, pr) else None))
    if refusal:
        print(refusal, file=sys.stderr)
        if step.get("offline"):
            (root / "gh-fails").write_text("")
        sys.exit(1)
    if step.get("queue"):
        print(f"! Pull request #{{number(url.rsplit('/', 1)[1])}} will be added to the merge queue")
        sys.exit(0)
    merger = root / ("merger-" + origin.stem)
    if not merger.exists():
        subprocess.run(["{git}", "clone", "-q", str(origin), str(merger)], check=True, capture_output=True)
    for cmd in (["fetch", "-q", "origin"], ["checkout", "-q", "-B", "main", "origin/main"],
                ["-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "-m",
                 f"Merge pull request #{{number(url.rsplit('/', 1)[1])}} from {{pr['branch']}}",
                 "origin/" + pr["branch"]], ["push", "-q", "origin", "main"]):
        subprocess.run(["{git}", *cmd], cwd=merger, check=True, capture_output=True)
    import fcntl
    with (root / "prs.lock").open("a") as held:     # as the fake build writes it
        fcntl.flock(held, fcntl.LOCK_EX)
        prs = json.loads((root / "prs.json").read_text())
        prs[url]["state"] = "MERGED"
        (root / f"prs.{{os.getpid()}}.tmp").write_text(json.dumps(prs))
        os.replace(root / f"prs.{{os.getpid()}}.tmp", root / "prs.json")
    if step.get("lost"):
        print("error connecting to api.github.com", file=sys.stderr)
        sys.exit(1)
    print(f"Merged pull request #{{number(url.rsplit('/', 1)[1])}}")
elif a[:2] == ["pr", "list"]:
    head = a[a.index("--head") + 1]
    print(json.dumps([{{"url": u}} for u, p in prs.items() if p["branch"] == head]))
elif a[:2] in (["issue", "list"], ["issue", "create"]):     # the final fix's issue, in `<root>/issues.json`
    store = root / "issues.json"
    issues = json.loads(store.read_text()) if store.exists() else []
    if a[1] == "list":      # `--search '"<title>" in:title'`: issues whose title holds the phrase
        phrase = a[a.index("--search") + 1].rsplit(" in:title", 1)[0].strip('"')
        print(json.dumps([{{k: i[k] for k in ("number", "title", "url")}} for i in issues if phrase in i["title"]]))
    else:
        url = f"https://example.invalid/o/r/issues/{{len(issues) + 1}}"
        issues.append({{"number": len(issues) + 1, "title": a[a.index("--title") + 1],
                       "body": a[a.index("--body") + 1], "url": url}})
        store.write_text(json.dumps(issues))
        print(url)
else:
    print("fake gh: unexpected " + " ".join(a), file=sys.stderr)
    sys.exit(2)
'''


FAKE_UV = r'''#!{python}
"""A fake `uv run --directory <review scripts> python -m review_pipeline
review|resume ...`: the review skill's ledger lines, run lock, findings.json
and `findings: <path>` line, answering from `<root>/reviews.json` (per PR
number, one step per `review` launch; `resume` uses the key `resume`)."""
import hashlib, json, os, pathlib, signal, subprocess, sys, time

argv = sys.argv[1:]
root = pathlib.Path(os.environ["EPIC_FAKE_ROOT"])
cmd = argv[argv.index("review_pipeline") + 1]
if cmd == "post":       # PR D2: logged apart from the reviews; steps under the key `post`
    with (root / "posts.log").open("a") as f:
        f.write(json.dumps({{"kind": "post", "cwd": os.getcwd().replace(str(root), "<root>"),
                            "argv": [a.replace(str(root), "<root>") for a in argv]}}, sort_keys=True) + "\n")
    fx = json.loads((root / "reviews.json").read_text()) if (root / "reviews.json").exists() else {{}}
    counts = root / "post-attempts"
    counts.mkdir(exist_ok=True)
    k = len(list(counts.glob("post.*"))) + 1
    (counts / f"post.{{k}}").write_text("")
    steps = fx.get("post", [])
    step = steps[k - 1] if k <= len(steps) else (steps[-1] if steps else {{}})
    decisions = pathlib.Path(argv[argv.index("--decisions") + 1])
    (root / "posted").mkdir(exist_ok=True)
    (root / "posted" / f"post.{{k}}.json").write_text(decisions.read_text())
    if step.get("block"):
        gate = root / "release" / f"post-{{k}}"
        while not gate.exists():
            time.sleep(0.02)
    if step.get("die"):
        os.kill(os.getpid(), signal.SIGKILL)
    if step.get("exit"):
        print(step.get("stderr", "fake post: failed"), file=sys.stderr, flush=True)
        sys.exit(step["exit"])
    print("posted", flush=True)
    sys.exit(0)
with (root / "reviews.log").open("a") as f:
    f.write(json.dumps({{"kind": cmd, "argv": [a.replace(str(root), "<root>") for a in argv]}},
                       sort_keys=True) + "\n")


def number(change):
    return int(hashlib.sha1(change.encode()).hexdigest()[:4], 16) % 9000 + 1000


data = pathlib.Path(os.environ["WORKSPACE"]) / ".multiplai" / "data" / "reviews"
ledger = data / "ledger.jsonl"
if cmd == "review":
    n = int(argv[argv.index("--pr") + 1])
else:
    target = pathlib.Path(argv[argv.index("resume") + 1])
    n = int(target.name[len("pr-"):])
prs = json.loads((root / "prs.json").read_text())
url, pr = next((u, p) for u, p in prs.items() if number(u.rsplit("/", 1)[1]) == n)
repo = url.split("/")[-3]           # the PR's repo (PR F), `r` unless another
if cmd == "review":
    target = data / f"o-{{repo}}" / f"pr-{{n}}"
pushed = subprocess.run(["{git}", "--git-dir", str(root / "o" / f"{{repo}}.git"), "rev-parse", "-q", "--verify",
                         "refs/heads/" + pr["branch"]], capture_output=True, text=True).stdout.strip()
checkouts = json.loads((root / "checkouts.json").read_text()) if (root / "checkouts.json").exists() else {{}}
head = pr.get("head") or pushed or subprocess.run(       # the head `gh` reports (PR E), in the PR's repo (PR F)
    ["git", "rev-parse", "refs/heads/" + pr["branch"]], cwd=root / checkouts.get(repo, "p"),
    capture_output=True, text=True).stdout.strip()
fx = json.loads((root / "reviews.json").read_text()) if (root / "reviews.json").exists() else {{}}
counts = root / "review-attempts"
counts.mkdir(exist_ok=True)
key = f"{{n}}.{{cmd}}"
k = len(list(counts.glob(key + ".*"))) + 1
(counts / f"{{key}}.{{k}}").write_text("")
steps = fx.get("resume" if cmd == "resume" else str(n), [])
step = steps[k - 1] if k <= len(steps) else (steps[-1] if steps else {{}})
if step.get("exit"):
    print(step.get("stderr", "fake review: failed"), file=sys.stderr, flush=True)
    sys.exit(step["exit"])
target.mkdir(parents=True, exist_ok=True)
# The review skill's run lock (review_pipeline/runlock.py): an flock on
# `<out>/<repo>/.locks/<object>.lock`, held for the process's life; the file
# keeps the holder's pid.
import fcntl
(target.parent / ".locks").mkdir(exist_ok=True)
lock = target.parent / ".locks" / f"{{target.name}}.lock"
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print(f"ERROR: a review of {{target}} is running now (pid {{lock.read_text()}}); wait for it",
          file=sys.stderr, flush=True)
    sys.exit(2)
os.ftruncate(fd, 0)
os.pwrite(fd, str(os.getpid()).encode(), 0)
lines = [json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
last = [x["event"] for x in lines if x.get("target_dir") == str(target) and x.get("head_sha") == head]
state = target / "review-state.json"
if cmd == "review" and last and last[-1] == "started" and state.exists() \
        and json.loads(state.read_text()).get("head") == head:
    print(f"STOPPED: interrupted run found; continue with: python -m review_pipeline resume {{target}}\n"
          "To start over instead, pass --restart.", file=sys.stderr, flush=True)
    sys.exit(4)
slug = f"o-{{repo}}--pr-{{n}}"
data.mkdir(parents=True, exist_ok=True)


def append(entry):
    with ledger.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


if cmd == "review":
    state.write_text(json.dumps({{"head": head}}))
    append({{"event": "started", "slug": slug, "target_dir": str(target), "head_sha": head}})
if step.get("block"):
    gate = root / "release" / f"review-{{n}}"
    while not gate.exists():
        time.sleep(0.02)
if step.get("busy"):        # told another review runs now, after its `started` line
    print(f"ERROR: a review of {{target}} is running now (pid 0); wait for it", file=sys.stderr, flush=True)
    sys.exit(2)
if step.get("die"):
    os.kill(os.getpid(), signal.SIGKILL)
(target / "findings.json").write_text(json.dumps({{"mode": None, "findings": step.get("findings", [])}}))
append({{"event": "finished", "slug": slug, "target_dir": str(target), "head_sha": head,
        "base_sha": "b" * 40, "pr": n, "round": "01", "stopped": step.get("stopped"),
        "run": {{"cost_usd": step.get("cost", 1.5)}}}})
print(f"findings: {{target / 'findings.json'}}", flush=True)
'''


FAKE_FINDINGS = r'''#!{python}
"""A fake `python -m build_pipeline findings --pr <n> --change <c> --project-dir <P> ...`
(PR D2), answering from `<root>/fixes.json`: per PR number, one step per launch.
`block` waits for `<root>/release/fix-<n>`; `die` is a SIGKILL; `exit` prints
`stdout` (a `FINDINGS:FAILED:` line) and exits with it; each of `rounds` is one
fix round: an empty commit on the PR branch, one more round in the change's
`.findings-state.json` (its steps all `finished`), and the fixer's own review
of that head in the review ledger with the round's `findings` (none under
`--no-review`; with `push`, the commit is pushed to the branch's upstream). A round with
`stop` is SIGKILLed part-way: `fetch` right after the round is added (no commit,
no review), `review` after its review is in the ledger but before the round
records it as finished. A launch that finds the last round unfinished
finishes it with its first entry of `rounds`, as `findings` resumes a round.
After its rounds it prints `FINDINGS:DONE:<rounds>`, or with `max_rounds`
`FINDINGS:MAX_ROUNDS:<rounds>`, or with `budget` `FINDINGS:BUDGET:<rounds>:<budget>`
(its `--max-usd` reached), as `findings` ends. A PR with no entry in
`fixes.json` has no fixer under test: it exits 0 printing no `FINDINGS:` end
line, so the coordinator neither parks nor fixes it again (PR C's tests).
`SPEND:<spend>:0` on every exit but a kill."""
import atexit, json, os, pathlib, signal, subprocess, sys, time

argv = sys.argv[1:]
root = pathlib.Path(os.environ["EPIC_FAKE_ROOT"])


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


with (root / "calls.log").open("a") as f:
    f.write(json.dumps({{"kind": "findings", "cwd": os.getcwd().replace(str(root), "<root>"),
                        "argv": [a.replace(str(root), "<root>") for a in argv]}}, sort_keys=True) + "\n")
n, change, project = int(opt("--pr")), opt("--change"), pathlib.Path(opt("--project-dir"))
fx = json.loads((root / "fixes.json").read_text()) if (root / "fixes.json").exists() else {{}}
counts = root / "fix-attempts"
counts.mkdir(exist_ok=True)
k = len(list(counts.glob(f"{{n}}.*"))) + 1
(counts / f"{{n}}.{{k}}").write_text("")
steps = fx.get(str(n), [])
step = steps[k - 1] if k <= len(steps) else (steps[-1] if steps else {{}})
atexit.register(lambda: print(f"SPEND:{{step.get('spend', 0.25)}}:0", flush=True))
for _sig in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, lambda signum, _frame: sys.exit(128 + signum))
if str(n) not in fx:
    sys.exit(0)
if step.get("block"):
    gate = root / "release" / f"fix-{{n}}"
    while not gate.exists():
        time.sleep(0.02)
if step.get("die"):
    os.kill(os.getpid(), signal.SIGKILL)
if step.get("exit"):
    print(step.get("stdout", ""), flush=True)
    sys.exit(step["exit"])
state = project / "specs" / "changes" / change / ".findings-state.json"
doc = json.loads(state.read_text()) if state.exists() else {{"findings": {{"rounds": []}}}}
reviews = pathlib.Path(os.environ["WORKSPACE"]) / ".multiplai" / "data" / "reviews"
import hashlib
prs = json.loads((root / "prs.json").read_text()) if (root / "prs.json").exists() else {{}}
repo = next((u for u in prs if int(hashlib.sha1(u.rsplit("/", 1)[1].encode()).hexdigest()[:4], 16) % 9000 + 1000 == n),
            "o/r/pull/0").split("/")[-3]      # the PR's repo (PR F), `r` unless another
target = reviews / f"o-{{repo}}" / f"pr-{{n}}"
ALL = ["fetch", "triage", "spec", "code", "publish", "review"]
rounds = doc["findings"]["rounds"]
todo = list(step.get("rounds", []))
resume = bool(rounds) and "review" not in rounds[-1].get("finished", ALL) and bool(todo)


def save():
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(doc))


for i, r in enumerate(todo):
    if i == 0 and resume:
        rnd = rounds[-1]
    else:
        rnd = {{"number": len(rounds) + 1, "finished": ["fetch"]}}
        rounds.append(rnd)
        save()
        if r.get("stop") == "fetch":
            os.kill(os.getpid(), signal.SIGKILL)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m",
                    f"fix round {{rnd['number']}}"], cwd=project, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True,
                          check=True).stdout.strip()
    if step.get("push"):        # as `findings` publishes: the round's commit on the PR's branch
        subprocess.run(["git", "push", "-q"], cwd=project, check=True, capture_output=True)
    rnd["finished"] = ALL[:-1]
    save()
    if "--no-review" not in argv:       # the epic's final fix runs no review of its round
        target.mkdir(parents=True, exist_ok=True)
        (target / "findings.json").write_text(json.dumps({{"mode": None, "findings": r.get("findings", [])}}))
        with (reviews / "ledger.jsonl").open("a") as f:
            for entry in ({{"event": "started", "slug": f"o-{{repo}}--pr-{{n}}", "target_dir": str(target),
                           "head_sha": head}},
                          {{"event": "finished", "slug": f"o-{{repo}}--pr-{{n}}", "target_dir": str(target),
                           "head_sha": head, "base_sha": "b" * 40, "pr": n, "round": "01", "stopped": None,
                           "run": {{"cost_usd": r.get("cost", 1.5)}}}}):
                f.write(json.dumps(entry, sort_keys=True) + "\n")
    if r.get("stop") == "review":
        os.kill(os.getpid(), signal.SIGKILL)
    rnd["finished"] = ALL
    save()
    print(f"FINDINGS:ROUND_DONE:{{rnd['number']}}", flush=True)
if step.get("budget"):
    print(f"FINDINGS:BUDGET:{{len(doc['findings']['rounds'])}}:{{step['budget']}}", flush=True)
    sys.exit(0)
print(f"FINDINGS:{{'MAX_ROUNDS' if step.get('max_rounds') else 'DONE'}}:{{len(doc['findings']['rounds'])}}",
      flush=True)
'''


FAKE_TESTS = r'''#!{python}
"""A fake test command (PR E's `merge-tests`), answering from `<root>/tests.json`:
one step per launch (`exit`; `block`, waiting for `<root>/release/tests-<k>`; `die`)."""
import json, os, pathlib, signal, sys, time

root = pathlib.Path(os.environ["EPIC_FAKE_ROOT"])
with (root / "calls.log").open("a") as f:
    f.write(json.dumps({{"kind": "merge-tests", "cwd": os.getcwd().replace(str(root), "<root>"),
                        "trusted": os.environ.get("BUILDME_TRUST_REPO")}}, sort_keys=True) + "\n")
counts = root / "test-attempts"
counts.mkdir(exist_ok=True)
k = len(list(counts.iterdir())) + 1
(counts / str(k)).write_text("")
steps = json.loads((root / "tests.json").read_text()) if (root / "tests.json").exists() else []
step = steps[k - 1] if k <= len(steps) else (steps[-1] if steps else {{}})
while step.get("block") and not (root / "release" / f"tests-{{k}}").exists():
    time.sleep(0.02)
if step.get("die"):
    os.kill(os.getpid(), signal.SIGKILL)
print("fake tests ran", flush=True)
sys.exit(step.get("exit", 0))
'''

# A `git` that logs only pushes (PR E's writing calls) to `<root>/pushes.log`,
# apart from `calls.log`, whose readers expect launches only; then runs the real one.
GIT_SHIM = """#!/bin/sh
if [ "$1" = push ]; then printf '{{"kind": "git-push", "argv": "%s"}}\\n' "$*" >> "$EPIC_FAKE_ROOT/pushes.log"; fi
exec {git} "$@"
"""


def real_git() -> str:
    """The real `git`, skipping any fake root's shim on PATH."""
    import shutil

    path = [p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p).parent / "fakes.json").exists()]
    return shutil.which("git", path=os.pathsep.join(path))


class Fakes:
    """The fake build and `gh` first on PATH, answering from `<root>/fakes.json`
    (per child, one step per attempt: `block`, `marker`, `exit`, `no_pr`,
    `die_before_pr_line`, `grandchild`, `ignore_signals`, `spend`, `pr_number`, and
    `pr`: fields the new PR starts with, such as `mergeable`) and `<root>/prs.json`; launches go to
    `<root>/calls.log`. `<root>/gh-fails` makes every `gh` call fail; `ps_fails`
    puts a `ps` that exits 1 first on PATH. The fake review (`uv`, PR C)
    answers from `<root>/reviews.json` (per PR number, one step per `review`
    launch: `block`, `die`, `exit` with `stderr`, `busy` (exit 2, another review
    runs now, its `started` line left), `findings`, `cost`, `stopped`
    (the `finished` line's value); `resume`
    has its own key) and logs its launches to `<root>/reviews.log`; by
    default each review is clean. PR D2: `review_pipeline post` answers from
    the key `post` of `<root>/reviews.json` (`block`, waiting for
    `<root>/release/post-<k>` on its k-th launch; `die`; `exit` with `stderr`),
    keeps a copy of each decisions file under `<root>/posted/` and logs to
    `<root>/posts.log`; the fixer (`FAKE_FINDINGS`) answers from
    `<root>/fixes.json` and logs to `<root>/calls.log` as kind `findings`."""

    def __init__(self, root: Path, monkeypatch, steps: dict[str, list[dict]] | None = None,
                 reviews: dict[str, list[dict]] | None = None, fixes: dict[str, list[dict]] | None = None):
        import sys

        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        (root / "reviews.json").write_text(json.dumps(reviews or {}))
        (root / "fixes.json").write_text(json.dumps(fixes or {}))
        for name, text in (("python", FAKE_BUILD), ("gh", FAKE_GH), ("uv", FAKE_UV),
                           ("fake_findings.py", FAKE_FINDINGS), ("fake-tests", FAKE_TESTS), ("git", GIT_SHIM)):
            path = self.bin / name
            path.write_text(text.format(python=sys.executable, git=real_git()))
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

    def _update_prs(self, url: str, **fields) -> None:
        """Change one PR in `prs.json` under the lock the fake build takes."""
        import fcntl

        with (self.root / "prs.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            prs = self.prs()
            prs[url].update(fields)
            (self.root / "prs.tmp").write_text(json.dumps(prs))
            os.replace(self.root / "prs.tmp", self.root / "prs.json")

    def set_pr(self, url: str, state: str) -> None:
        self._update_prs(url, state=state)

    def attempts(self, cid: str) -> int:
        return len(list((self.root / "attempts").glob(cid + ".*")))

    def release_review(self, change: str) -> None:
        (self.root / "release" / f"review-{pr_number(change)}").write_text("")

    def set_head(self, change: str, sha: str) -> None:
        self._update_prs(pr_url(change), head=sha)

    def review_lock(self, change: str = "e-a") -> Path:
        """The fake review's run lock for *change*'s PR, where the review skill keeps it."""
        reviews = self.root / "ws" / ".multiplai" / "data" / "reviews" / "o-r"
        return reviews / ".locks" / f"pr-{pr_number(change)}.lock"

    def review_launches(self) -> list[dict]:
        p = self.root / "reviews.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def post_launches(self) -> list[dict]:
        p = self.root / "posts.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def merges(self) -> list[dict]:
        """Every `gh pr merge` (PR E2)."""
        p = self.root / "merges.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def fix_launches(self) -> list[dict]:
        return [x for x in CallLog(self.root / "calls.log").calls() if x["kind"] == "findings"]

    def release_fix(self, change: str) -> None:
        (self.root / "release" / f"fix-{pr_number(change)}").write_text("")


def pr_url(change: str) -> str:
    return f"https://example.invalid/o/r/pull/{change}"


def pr_number(change: str) -> int:
    """The fake PR's number, as the fake `gh` and review give it."""
    import hashlib

    return int(hashlib.sha1(change.encode()).hexdigest()[:4], 16) % 9000 + 1000


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


_OUT: list[str] = []      # what the last `run_coordinator` printed


class _Tee:
    def __init__(self, out):
        self.out, self.text = out, []

    def write(self, s: str) -> int:
        self.text.append(s)
        return self.out.write(s)

    def flush(self) -> None:
        self.out.flush()


def run_coordinator(config: BuildConfig, **over) -> int:
    """One re-run of the approved build: the coordinator runs, with the fake
    clock. Like a new process, it takes the epic lock itself and drops it at
    the end, so a child build holding the lock would make it exit 2. What it
    prints is kept for `merge_waiting`."""
    import contextlib
    import sys
    from unittest.mock import patch

    from build_pipeline.epic import coordinator, lock

    lock.release_all()
    tee = _Tee(sys.stdout)
    try:
        with patch.object(coordinator, "CLOCK", FakeClock()), contextlib.redirect_stdout(tee):
            return run_build(config, args_for(config, **over), coordinator=True)
    finally:
        lock.release_all()
        _OUT[:] = "".join(tee.text).splitlines()


def merge_waiting() -> list[str] | None:
    """The PRs the last `run_coordinator` named on `EPIC:MERGE_WAITING:`; None without that line."""
    found = [line for line in _OUT if line.startswith("EPIC:MERGE_WAITING:")]
    return found[-1][len("EPIC:MERGE_WAITING:"):].split(",") if found else None


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
