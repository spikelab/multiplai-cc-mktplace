"""Shared fixtures: a real git repository in a temporary directory, with a
bare `origin` beside it, the command line run in-process, and a fake model
that answers each call by its label. No test makes a model call:
`pm_spec_pipeline.sdk.run_agent` is replaced. WORKSPACE is unset, so pm-spec's
worktree is `<tmp>/.worktrees/pm-spec-<branch>`, beside the test's repo and
never in the workspace of whoever runs the suite."""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pm_spec_pipeline import __main__ as cli
from pm_spec_pipeline import sdk

SKILL_DIR = Path(__file__).resolve().parents[2]
SCHEMA = SKILL_DIR / "references" / "tickets.schema.json"

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
}


class Killed(BaseException):
    """Stands in for a kill: a BaseException, so no `except Exception` in the code catches it."""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- the default answers ------------------------------------------------------------

CAPS = ("rate-limit", "retry-policy")

PRD = """\
# Throttling PRD

Priority: high

## Limit requests per client
Each client gets a fixed number of API requests per minute.

## Retry guidance
A refused request tells the client when to retry.
"""

PRD_SECTIONS = ("Limit requests per client", "Retry guidance")


def proposal(
    *,
    title="Request throttling",
    sources: list[str] | None = None,
    extra: str = "",
) -> str:
    """A proposal in the stage 1 format: no capabilities; `## Sources` lists PRD headings."""
    text = (
        f"# {title}\n\n## Why\n\nClients flood the API server.\n\n## What Changes\n\n"
        f"Throttle requests.{extra}\n\n## Impact\n\nThe `server` module.\n"
    )
    if sources:
        text += "\n## Sources\n\n" + "".join(f'- PRD "{s}"\n' for s in sources)
    return text


def requirements(cap: str, *, rule: str = "", scenario: str = "") -> str:
    """One capability's delta (the text under its `# Capability:` heading)."""
    name = cap.replace("-", " ").capitalize()
    return (
        f"## ADDED Requirements\n\n### Requirement: {name} holds\n"
        f"The system SHALL apply {rule or cap}.\n\n"
        f"#### Scenario: {scenario or name + ' applies'}\n"
        f"- **WHEN** a client sends a request\n- **THEN** {rule or cap} is applied\n"
    )


def specs(caps=CAPS, *, rule: str = "", rules: dict | None = None) -> str:
    """A `specs.md`: one `# Capability:` section per capability."""
    rules = rules or {}
    return "\n".join(
        f"# Capability: {c}\n\n" + requirements(c, rule=rules.get(c, rule))
        for c in caps
    )


def plan(
    title: str = "Throttle the API",
    done: tuple = ("A client over its limit is refused.",),
) -> str:
    return (
        f"# Plan: {title}\n\n> File: x.md · Date: 2026-10-23\n\n"
        f"**Objective:** {title}, so the server stays up.\n\n"
        "## Context\nThe server falls over.\n\n## Outcome\nIt does not.\n\n"
        "## Constraints / out of scope\n- None.\n\n## Done means\n"
        + "".join(f"{i}. {d}\n" for i, d in enumerate(done, 1))
    )


def component(cid: str, deps=(), ticket_ref: str = "", title: str = "") -> dict:
    return {
        "id": cid,
        "title": title or cid,
        "depends_on": list(deps),
        "ticket_ref": ticket_ref,
        "plan": plan(title or f"Build {cid}", (f"{cid} works.",)),
    }


USE_CASES = "# Use cases\n\n## Personas\n\n### operator\n- **Who they are:** runs the API server\n"

QUESTIONS = {
    "questions": ["How many requests per minute?", "Which clients are exempt?"]
}
ANSWERS = {
    "answers": [
        {
            "question": "How many requests per minute?",
            "answer": "60.",
            "grounded_in": "PRD: Limit requests",
        },
        {"question": "Which clients are exempt?", "answer": "None.", "grounded_in": ""},
    ]
}

SINGLE = {
    "reason": "One pull request carries it.",
    "components": [component("throttle", title="Throttle the API")],
}
TWO = {
    "reason": "Each is a pull request on its own.",
    "components": [component("api"), component("ui", deps=["api"])],
}
NO_FINDINGS = {"findings": [], "prior_assessments": []}

NOTES = "- mockup.html: the limits page\n\nQUESTIONS: none\nSTATUS: DONE\n"


def agent(notes=NOTES, *, artifact="mockup.html", outside: Path | None = None):
    """A prototype agent stand-in: writes its files where it was told to."""

    def handler(prompt, label, kw):
        root = Path(kw["cwd"])
        files = []
        if artifact:
            files.append(str(write(root / artifact, "<html>sample</html>\n")))
        if notes is not None:
            files.append(str(write(root / "NOTES.md", notes)))
        if outside is not None:
            files.append(str(write(outside, "oops\n")))
        return "done", files

    return handler


def default_proposal(prompt: str) -> str:
    """The proposal, citing the PRD when the prompt has one."""
    return proposal(sources=list(PRD_SECTIONS) if "## PRD (prd.md)" in prompt else None)


def rewrite_answer(text: str) -> str:
    """A file answer holding *text* between the delimiter lines: a rewrite's
    (`rewrite.parse_answer`) or a first-pass specs.md's (`artifacts.write_specs`)."""
    from pm_spec_pipeline.rewrite import BEGIN, END

    return f"{BEGIN}\n{text.rstrip(chr(10))}\n{END}\n"


def default_rewrite(prompt: str, label: str) -> str:
    target = label.rsplit(":", 1)[1]
    return rewrite_answer(specs(rule=f"the rewritten {target}"))


DEFAULTS = {
    "interview:questions": QUESTIONS,
    "interview:answers": ANSWERS,
    "components": SINGLE,
    "spec:proposal": lambda prompt, label, kw: default_proposal(prompt),
    "spec:use-cases": USE_CASES,
    "spec:specs": lambda prompt, label, kw: rewrite_answer(specs()),
    "consistency": NO_FINDINGS,
    "component_check": NO_FINDINGS,
    "prototype": agent(),
}


class FakeModel:
    """Answers `run_agent` by label: the longest handler key the label starts
    with (a rewrite, `<step>:rewrite:<target>`, falls back to `rewrite`).

    A handler is a value (a dict is sent as JSON), a list of values (one per
    call, the last repeated), or a callable `(prompt, label, kwargs)`. A
    handler that raises stands for a failing or killed call."""

    def __init__(self) -> None:
        self.handlers: dict = dict(DEFAULTS)
        self.calls: list[tuple[str, str, dict]] = []
        self.count: Counter = Counter()
        self.handlers["rewrite"] = lambda prompt, label, kw: default_rewrite(
            prompt, label
        )

    def __setitem__(self, key, value) -> None:
        self.handlers[key] = value

    def labels(self) -> list[str]:
        return [label for label, _, _ in self.calls]

    def prompts(self, prefix: str) -> list[str]:
        return [p for label, p, _ in self.calls if label.startswith(prefix)]

    def exactly(self, label: str) -> list[str]:
        """The prompts of the calls labelled exactly *label*."""
        return [p for name, p, _ in self.calls if name == label]

    def calls_to(self, prefix: str) -> int:
        return sum(1 for label in self.labels() if label.startswith(prefix))

    def _handler(self, label: str):
        if ":rewrite:" in label:
            for key in sorted(self.handlers, key=len, reverse=True):
                if label.startswith(key) and ":rewrite" in key:
                    return self.handlers[key]
            return self.handlers["rewrite"]
        for key in sorted(self.handlers, key=len, reverse=True):
            if label == key or label.startswith(key + ":"):
                return self.handlers[key]
        raise AssertionError(f"no fake answer for the call {label!r}")

    async def __call__(self, prompt, *, label="", **kwargs):
        self.calls.append((label, prompt, kwargs))
        n = self.count[label]
        self.count[label] += 1
        handler = self._handler(label)
        if isinstance(handler, list):
            handler = handler[min(n, len(handler) - 1)]
        value = handler(prompt, label, kwargs) if callable(handler) else handler
        files: list[str] = []
        if isinstance(value, tuple):
            value, files = value
        text = (
            json.dumps(value) if isinstance(value, (dict, list)) else str(value or "")
        )
        usage = SimpleNamespace(
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        return SimpleNamespace(
            text=text, turns=1, usage=usage, files_changed=files, stderr_tail=""
        )


@pytest.fixture(autouse=True)
def model(monkeypatch):
    """Every test runs trusted, with the fake model, and with no session model settings."""
    fake = FakeModel()
    monkeypatch.setattr(sdk, "run_agent", fake)
    monkeypatch.setenv(sdk.TRUST_ENV, "1")
    for key in (
        "MULTIPLAI_MODEL",
        "MULTIPLAI_EFFORT",
        "MULTIPLAI_SESSION_MODEL",
        "MULTIPLAI_SESSION_EFFORT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_EFFORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "multiplai_core.session.load_multiplai_conf", lambda: {"_sections": {}}
    )
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WORKSPACE", raising=False)
    return fake


def make_repo(path: Path) -> Path:
    """A checkout at *path*, on `main`, with `origin` (a bare repo beside it,
    `<parent>/origin.git`) holding `main`. pm-spec's worktrees go in
    `<parent>/.worktrees/`, so each repo needs a parent of its own."""
    origin = path.parent / "origin.git"
    path.mkdir(parents=True)
    git(path.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    git(path, "init", "-q", "-b", "main")
    write(path / "README.md", "# proj\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "-q", "origin", "main")
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    """The user's checkout (`make_repo`)."""
    return make_repo(tmp_path / "proj")


TICKET = "ENG-1"
BRANCH = f"{TICKET}-throttle"


def worktree(repo: Path, branch: str = "") -> Path:
    """pm-spec's worktree of *branch* for the checkout *repo* (WORKSPACE unset);
    with no *branch*, the one pm-spec worktree there (a test's own
    `--ticket-ref` names the branch), else `ENG-1-throttle`'s."""
    root = repo.parent / ".worktrees"
    if not branch:
        made = sorted(root.glob("pm-spec-*")) if root.is_dir() else []
        if len(made) == 1:
            return made[0]
        branch = BRANCH
    return root / ("pm-spec-" + branch.replace("/", "-"))


def origin(repo: Path) -> Path:
    return repo.parent / "origin.git"


def run(repo: Path, *argv: str) -> int:
    """The command line, in-process: `<command> <change> --project-dir <repo> ...`;
    `new` gets `--ticket-ref ENG-1` unless the test names one, as pm-spec
    refuses a change with no ticket."""
    command, change, *rest = argv
    if command == "new" and "--ticket-ref" not in rest:
        rest = [*rest, "--ticket-ref", TICKET]
    return cli.main([command, change, "--project-dir", str(repo), *rest])


def resume_to_end(
    repo: Path, name: str = "throttle", *args: str, limit: int = 4
) -> int:
    """`resume` through each review stop and the ticket wait (exit 3, each time
    the approval); the first other exit code."""
    for _ in range(limit):
        code = run(repo, "resume", name, *args)
        if code != 3:
            return code
    return code


def source_dir(repo: Path, name: str = "throttle") -> Path:
    """The change's dir in the user's checkout, which pm-spec copies into its
    worktree when the dir records no branch (`branch.adopt`)."""
    return repo / "specs" / "changes" / name


def change_dir(repo: Path, name: str = "throttle") -> Path:
    """The change's dir where pm-spec writes it: its worktree of `ENG-1-throttle`,
    the branch every change in these tests (a child's too) is on."""
    return worktree(repo) / "specs" / "changes" / name


def meta(cdir: Path) -> dict:
    return yaml.safe_load((cdir / ".change.yaml").read_text()) or {}


def state(cdir: Path) -> dict:
    return json.loads((cdir / ".pm-spec" / "state.json").read_text())


SNAPSHOT_SKIP = {".pm-spec"}


def snapshot(cdir: Path) -> dict[str, str]:
    """Every file of the change dir but the run folder, each `.change.yaml` without its timestamps."""
    out = {}
    for path in sorted(cdir.rglob("*")):
        rel = path.relative_to(cdir)
        if not path.is_file() or rel.parts[0] in SNAPSHOT_SKIP:
            continue
        if rel.name == ".change.yaml":
            data = {
                k: v
                for k, v in meta(path.parent).items()
                if k not in ("created", "spec_checked_at")
            }
            out[rel.as_posix()] = json.dumps(data, sort_keys=True)
        else:
            out[rel.as_posix()] = path.read_text()
    return out


def committed(repo: Path, cdir: Path) -> list[str]:
    """The paths HEAD's commit changed under the change dir, in pm-spec's worktree."""
    wt = worktree(repo)
    rel = cdir.relative_to(wt).as_posix()
    return sorted(
        git(wt, "show", "--name-only", "--format=", "HEAD", "--", rel).splitlines()
    )


def ready_line(out: str) -> str:
    lines = [line for line in out.splitlines() if line.startswith("SPEC:READY:")]
    assert lines, out
    return lines[-1]
