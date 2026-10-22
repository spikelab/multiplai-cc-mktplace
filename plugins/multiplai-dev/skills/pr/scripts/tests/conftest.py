"""Shared fixtures. No test calls `gh` or a model: both seams fail loudly unless a test stubs them."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pr_pipeline import fill, github


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def make_repo(path: Path, branch: str = "feat/x") -> Path:
    """A repo with one commit on `main` and one on *branch* (checked out): 2 files, +3 -1."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("one\ntwo\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    git(path, "checkout", "-q", "-b", branch)
    (path / "a.txt").write_text("one\nTWO\n")
    (path / "b.txt").write_text("x\ny\n")
    git(path, "add", "a.txt", "b.txt")
    git(path, "commit", "-q", "-m", "change")
    return path


def make_bare_origin(repo: Path, origin: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True
    )
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return origin


def make_change(
    repo: Path,
    name: str = "feat",
    *,
    why: str = "Because the queue drops jobs.",
    tasks: str
    | None = "## 1. First block\n\nDo it.\n\n## 2. Second block\n\nDo more.\n",
    state: dict | None = None,
    artifacts: tuple[str, ...] = (),
    ticket: str | None = None,
    archive: str | None = None,
) -> Path:
    d = (
        repo
        / "specs"
        / ("archive" if archive else "changes")
        / (f"{archive}-{name}" if archive else name)
    )
    d.mkdir(parents=True, exist_ok=True)
    (d / "proposal.md").write_text(f"# P\n\n## Why\n\n{why}\n\n## Impact\n\nnone\n")
    if tasks is not None:
        (d / "tasks.md").write_text(tasks)
    if state is not None:
        (d / ".build-state.json").write_text(json.dumps(state))
    for a in artifacts:
        (d / a).parent.mkdir(parents=True, exist_ok=True)
        (d / a).write_text("artifact\n")
    if ticket is not None:
        (d / "ticket.md").write_text(ticket)
    return d


class GhStub:
    """Records every `gh` argv and answers from a list of (predicate, Result-args) rules."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.bodies: dict[str, str] = {}
        self.rules: list[tuple] = []

    def on(
        self, prefix: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
    ):
        self.rules.append((prefix, returncode, stdout, stderr))
        return self

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        for flag in ("--body-file",):
            if flag in argv:
                path = Path(argv[argv.index(flag) + 1])
                if path.is_file():
                    self.bodies[" ".join(argv[:3])] = path.read_text()
        for prefix, code, out, err in self.rules:
            if argv[: len(prefix)] == prefix:
                return github.Result(list(argv), code, out, err)
        return github.Result(list(argv), 1, "", f"unstubbed gh call: {argv}")

    def writes(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[:3]
            in (["gh", "pr", "create"], ["gh", "pr", "edit"], ["gh", "pr", "comment"])
        ]


@pytest.fixture(autouse=True)
def no_real_calls(monkeypatch):
    def gh(argv, cwd=None, timeout=None):
        raise AssertionError(f"a test called gh without stubbing it: {argv}")

    async def model(prompt):
        raise AssertionError("a test called the model without stubbing it")

    monkeypatch.setattr(github, "_run_gh", gh)
    monkeypatch.setattr(fill, "_run_model", model)


@pytest.fixture
def gh_stub(monkeypatch):
    stub = GhStub()
    monkeypatch.setattr(github, "_run_gh", stub)
    return stub


@pytest.fixture
def model_stub(monkeypatch):
    """Answers each model call with the next entry of `.answers` (a dict, dumped to JSON, or a str)."""

    class Model:
        def __init__(self):
            self.answers: list = []
            self.prompts: list[str] = []

        async def __call__(self, prompt):
            self.prompts.append(prompt)
            a = self.answers.pop(0)
            return a if isinstance(a, str) else json.dumps(a)

    m = Model()
    monkeypatch.setattr(fill, "_run_model", m)
    return m


# multiplai-core 0.21: a step with no configured model or effort runs on
# MULTIPLAI_MODEL / MULTIPLAI_EFFORT, else the Claude Code session's own
# (multiplai_core.session). Run inside a session, the developer's values would
# set a test's model, so clear them and hide the developer's multiplai.conf
# from that lookup. A test that wants one sets it with monkeypatch.
_SESSION_DEFAULT_VARS = (
    "MULTIPLAI_MODEL",
    "MULTIPLAI_EFFORT",
    "MULTIPLAI_SESSION_MODEL",
    "MULTIPLAI_SESSION_EFFORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
)


@pytest.fixture(autouse=True)
def _no_session_defaults(monkeypatch):
    for key in _SESSION_DEFAULT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "multiplai_core.session.load_multiplai_conf", lambda: {"_sections": {}}
    )
