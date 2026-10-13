"""Shared fixtures: real git repositories in temporary directories, a fixed clock,
a stand-in for `gh pr view`, and a fake model call. No test calls `gh` or a model."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from code_facts import ExploreOutput
from multiplai_core import budget as core_budget
from multiplai_core.budget import BudgetExceededError

from spec_extract_pipeline import __main__ as cli
from spec_extract_pipeline import sdk
from spec_extract_pipeline import target as target_mod

FIXED = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
SCRIPTS_DIR = Path(__file__).resolve().parent.parent

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000",
}


class Killed(BaseException):
    """Stands in for a kill: a BaseException, so no `except Exception` in the code catches it."""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, **_GIT_ENV), check=True)
    return proc.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(path: Path, origin: str | None = "https://github.com/acme/widgets.git") -> Path:
    """A repository with one commit holding a few Python files, and `origin` set (not fetched)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write(path / "src" / "billing" / "charge.py", "def charge(amount):\n    return amount\n")
    write(path / "src" / "a" / "one.py", "def one():\n    return 1\n")
    write(path / "src_a" / "two.py", "def two():\n    return 2\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def commit_file(repo: Path, rel: str, text: str, message: str = "change") -> str:
    write(repo / rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run(*argv: str | Path, now: datetime = FIXED) -> int:
    return cli.main([str(a) for a in argv], now=lambda: now)


def read_state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text(encoding="utf-8"))


def fake_state(run_dir: Path, **target) -> None:
    """A `state.json` another run wrote, holding only the `target` fields given."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps({"target": target, "steps_done": []}), encoding="utf-8")


def outputs(run_dir: Path) -> dict[str, bytes]:
    """Every file under *run_dir* a resumed run must reproduce byte for byte: all but
    `state.json`, `.state-*.tmp`, `snapshot/` and `source/.git/`."""
    out = {}
    for p in sorted(run_dir.rglob("*")):
        rel = p.relative_to(run_dir).as_posix()
        if not p.is_file() or rel == "state.json" or rel.startswith(("snapshot/", "source/.git/")):
            continue
        if "/" not in rel and rel.startswith(".state-") and rel.endswith(".tmp"):
            continue
        out[rel] = p.read_bytes()
    return out


def everything(root: Path) -> dict[str, bytes | None]:
    """Every path under *root*, files with their bytes: what "writes nothing" compares."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def source_head(run_dir: Path) -> str | None:
    src = run_dir / "source"
    return git(src, "rev-parse", "HEAD") if (src / ".git").is_dir() else None


@pytest.fixture
def pr_view(monkeypatch):
    """Replace `gh pr view`: set `pr_view.info[n] = {"headRefOid": …, "baseRefOid": …}`."""
    class View:
        info: dict[int, dict] = {}
        calls: list[int] = []

    view = View()
    view.info, view.calls = {}, []

    def fake(repo, number):
        view.calls.append(number)
        return dict(view.info[number])

    monkeypatch.setattr(target_mod, "_pr_view", fake)
    return view


# --- the fake model call --------------------------------------------------------------

_LISTED = re.compile(r"^- (\S.*) \((\d+) lines\)$", re.M)


class FakeCall:
    """Stands in for `sdk.agent_call_structured`, the `CallFn` explore calls.

    It answers an explore prompt with one fact and one file purpose per listed
    file, each citing the file's first non-blank line, read from the snapshot
    (`cwd`), so the gate keeps them. A unit is named by its first listed file.
    Each call records `cost` dollars into the run's ledger, as the real call
    does as it returns. `fail[file] = n` makes the unit's next n calls raise
    `CallFailed` (after recording spend); `kill[file] = n` raises `Killed`
    (before recording, like a process killed mid-call); `over[file] = n` raises
    `BudgetExceededError`, as the real call's own budget check does.
    """

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.fail: Counter[str] = Counter()
        self.kill: Counter[str] = Counter()
        self.over: Counter[str] = Counter()
        self.cost = 1.0
        self.kwargs: list[dict] = []

    async def __call__(self, prompt, schema, *, budget_label="", cwd=None, **kwargs):
        files = [m.group(1) for m in _LISTED.finditer(prompt)]
        unit = files[0]
        self.calls[unit] += 1
        self.kwargs.append(dict(kwargs, cwd=cwd, budget_label=budget_label))
        if self.kill[unit]:
            self.kill[unit] -= 1
            raise Killed(f"killed during the call for {unit}")
        if self.over[unit]:
            self.over[unit] -= 1
            raise BudgetExceededError("circuit breaker stopped the run (fake)")
        core_budget.record(SimpleNamespace(cost_usd=self.cost, input_tokens=100, output_tokens=10),
                           label=budget_label)
        if self.fail[unit]:
            self.fail[unit] -= 1
            raise sdk.CallFailed(f"fake failure for {unit}")
        return schema.model_validate(answer_for(files, Path(cwd)))


def answer_for(files: list[str], snapshot: Path) -> dict:
    facts, purposes = [], []
    for f in files:
        lines = (snapshot / f).read_text(encoding="utf-8").splitlines()
        n = next((i for i, line in enumerate(lines, 1) if line.strip()), None)
        if n is None:
            continue
        cite = {"path": f, "line_start": n, "line_end": n, "quote": lines[n - 1].strip()}
        facts.append({"claim": f"{f} starts with {lines[n - 1].strip()!r}", "citations": [cite]})
        purposes.append({"path": f, "purpose": f"holds {Path(f).stem}", "citation": cite})
    return ExploreOutput.model_validate({"facts": facts, "files": purposes}).model_dump()


@pytest.fixture(autouse=True)
def fake_call(monkeypatch):
    """Every test runs trusted, with the fake call, and with no model key from multiplai.conf."""
    fake = FakeCall()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    monkeypatch.setattr(sdk, "model_for", lambda step: None)
    monkeypatch.setenv(sdk.TRUST_ENV, "1")
    return fake
