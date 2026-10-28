"""End to end: pm-spec writes and readies the specs on the change's feature
branch and pushes it, buildme builds from that branch.

pm-spec runs as its own command line in its own environment
(`uv run --package pm-spec-pipeline`, through `pm_spec_driver.py`, its model
replaced); buildme runs in-process with its model-calling steps replaced, up
to the start of TDD. Each test has a real bare origin. Editing the
specs on the feature branch after the handoff makes the next build
refuse with `SPEC:NOT_READY`. buildme never imports `pm_spec_pipeline`: the
change dir on the branch and `multiplai_core.specs` are all the two share.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from build_pipeline.orchestrator import run_orchestrator
from multiplai_core.specs import is_ready

from tests.spec_fixtures import ready_specs
from tests.test_git_ops import make_bare_origin, make_repo
from tests.test_ready_specs import CHANGE, Models, _args, _config

pytestmark = pytest.mark.no_handoff

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parents[4]
DRIVER = Path(__file__).with_name("pm_spec_driver.py")
PRD = "# Return a value\n\n## Return a value\nThe app returns a value when asked.\n"


class ReachedTdd(BaseException):
    """Raised by the TDD stand-in: the build got to TDD with ready specs."""


def _pm_spec(sandbox: Path, repo: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("PYTEST", "CLAUDE", "MULTIPLAI_", "BUILDME_", "PM_SPEC_", "VIRTUAL_ENV")
        )
        and k != "WORKSPACE"
    }
    for name in ("claude-config", "conf", "data"):
        (sandbox / name).mkdir(exist_ok=True)
    env.update(
        CLAUDE_CONFIG_DIR=str(sandbox / "claude-config"),
        CLAUDE_MULTIPLAI_HOME=str(sandbox / "conf"),
        CLAUDE_PLUGIN_OPTION_DATA_DIR=str(sandbox / "data"),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    argv = [
        shutil.which("uv") or "uv",
        "run",
        "--project",
        str(REPO_ROOT),
        "--package",
        "pm-spec-pipeline",
        "python",
        str(DRIVER),
        *argv,
    ]
    return subprocess.run(
        argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=600
    )


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _setup(tmp_path: Path, monkeypatch) -> Path:
    """The user's checkout, on main, with a bare origin holding main."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    from build_pipeline import orchestrator

    monkeypatch.setattr(
        orchestrator, "_run_spec_extract_step", lambda config, state, progress: None
    )
    repo = make_repo(tmp_path / "proj")
    make_bare_origin(repo, tmp_path / "origin.git")
    _git(repo, "push", "-q", "origin", "main")
    return repo


async def _build_to_tdd(repo: Path, models: Models) -> tuple[int | None, dict]:
    """buildme in *repo*, up to the start of TDD: its exit code (None when
    it reached TDD) and what the TDD stand-in saw."""
    reached = {}

    async def tdd(config, args, standalone=True):
        reached["change_dir"] = config.change_dir
        raise ReachedTdd

    p = models.patches()
    with (
        p[0],
        p[1],
        p[2],
        patch("build_pipeline.tdd_engine.run_tdd_engine", tdd),
    ):
        try:
            rc = await run_orchestrator(
                _config(repo, spec_only=False), _args(repo, spec_only=False)
            )
        except ReachedTdd:
            rc = None
    return rc, reached


needs_uv = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv runs pm-spec in its own environment"
)


@needs_uv
@pytest.mark.asyncio
async def test_pm_spec_ready_specs_build_to_tdd_and_an_edit_refuses(
    tmp_path, monkeypatch, capsys
):
    """T9."""
    repo = _setup(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    prd = tmp_path / "prd.md"
    prd.write_text(PRD)

    # pm-spec: the specs, through its three review stops and the ticket
    # wait (each a push, exit 3, and a resume as the approval), then
    # checked, marked ready, committed for <TICKET>-<change> in its own
    # worktree, and pushed.
    common = ["--project-dir", str(repo), "--trust-repo"]
    proc = _pm_spec(
        tmp_path,
        repo,
        "new",
        CHANGE,
        *common,
        "--prd",
        str(prd),
        "--interview-summary",
        "Return a value.",
        "--no-prototype",
        "--no-epic-split",
        "--ticket-ref",
        "ENG-1",
    )
    for marker in ("SPEC:REVIEW:proposal", "SPEC:REVIEW:components", "SPEC:TICKETS:"):
        assert proc.returncode == 3, proc.stdout[-3000:] + proc.stderr[-3000:]
        assert marker in proc.stdout, proc.stdout[-3000:]
        proc = _pm_spec(tmp_path, repo, "resume", CHANGE, *common)
    assert proc.returncode == 3, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "SPEC:REVIEW:specs" in proc.stdout, proc.stdout[-3000:]
    proc = _pm_spec(tmp_path, repo, "resume", CHANGE, *common)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    branch = f"ENG-1-{CHANGE}"
    spec_tree = tmp_path / ".worktrees" / f"pm-spec-{branch}"
    change_dir = spec_tree / "specs" / "changes" / CHANGE
    assert f"SPEC:READY:{change_dir}:" in proc.stdout
    assert f"SPEC:BRANCH:{branch}" in proc.stdout
    assert is_ready(change_dir)[0]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (repo / "specs").exists(), "the user's checkout is not written"
    assert _git(tmp_path / "origin.git", "rev-parse", branch) == _git(
        spec_tree, "rev-parse", "HEAD"
    )

    # buildme: finds the specs on origin's <TICKET>-<change>, checks that
    # branch out in its worktree, plans from them and starts TDD.
    models = Models()
    rc, reached = await _build_to_tdd(repo, models)
    assert rc is None, capsys.readouterr().out
    wt = tmp_path / "ws" / ".worktrees" / f"buildme-{CHANGE}"
    built = reached["change_dir"]
    assert built == wt / "specs" / "changes" / CHANGE
    assert _git(wt, "merge-base", "--is-ancestor", f"origin/{branch}", "HEAD") == ""
    assert _git(wt, "branch", "--show-current") == branch
    for name in ("proposal.md", "use-cases.md", "plan.md", "specs.md"):
        assert (built / name).read_bytes() == (change_dir / name).read_bytes(), name
    assert (built / "implementation-plan.md").is_file() and (
        built / "tasks.md"
    ).is_file()
    generated = {c.args[0] for c in models.generate.await_args_list}
    assert generated == {"design", "tasks"}, generated

    # The specs edited on the feature branch after the handoff: the next
    # build, from a teammate's fresh clone, refuses.
    requirement = change_dir / "specs.md"
    requirement.write_text(requirement.read_text() + "\nThe value SHALL be 2.\n")
    _git(spec_tree, "commit", "-qam", "edit a requirement")
    _git(spec_tree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")  # detached
    other = tmp_path / "teammate"
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws2"))
    capsys.readouterr()
    models = Models()
    rc, _ = await _build_to_tdd(other, models)
    out = capsys.readouterr().out
    assert rc == 2
    assert (
        f"SPEC:NOT_READY:specs/changes/{CHANGE} at origin/{branch}: the specs changed "
        "after they were marked ready" in out
    )
    assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out
    assert not (tmp_path / "ws2").exists(), "no worktree"
    models.generate.assert_not_awaited()


@needs_uv
@pytest.mark.asyncio
async def test_an_old_handoff_is_refused_until_pm_spec_resume_moves_it(
    tmp_path, monkeypatch, capsys
):
    """T8: specs a pm-spec from before the feature branch handed off (ready
    and committed on the user's main, no feature branch) are refused with
    `pm-spec resume <name>`; that resume moves them onto `<TICKET>-<name>` and
    pushes it, and the build then runs."""
    repo = _setup(tmp_path, monkeypatch)
    old = repo / "specs" / "changes" / CHANGE
    ready_specs(old, repo)

    models = Models()
    rc, _ = await _build_to_tdd(repo, models)
    out = capsys.readouterr().out
    assert rc == 2
    assert f"Run /multiplai-pm:pm-spec resume {CHANGE} first" in out
    models.generate.assert_not_awaited()

    proc = _pm_spec(
        tmp_path,
        repo,
        "resume",
        CHANGE,
        "--project-dir",
        str(repo),
        "--trust-repo",
        "--ticket-ref",
        "ENG-1",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert f"SPEC:BRANCH:ENG-1-{CHANGE}" in proc.stdout
    assert old.is_dir(), "the original is never deleted"

    rc, reached = await _build_to_tdd(repo, Models())
    assert rc is None, capsys.readouterr().out
    assert (reached["change_dir"] / "implementation-plan.md").is_file()
