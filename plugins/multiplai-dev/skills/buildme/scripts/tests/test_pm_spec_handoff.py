"""End to end: pm-spec writes and readies the specs, buildme builds from them.

pm-spec runs as its own command line in its own environment
(`uv run --package pm-spec-pipeline`, through `pm_spec_driver.py`, its model
replaced); buildme runs in-process with its model-calling steps replaced, up
to the start of TDD. Editing one requirement after the handoff makes the next
build refuse with `SPEC:NOT_READY`. buildme never imports `pm_spec_pipeline`:
the change dir and `multiplai_core.specs` are all the two share.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from multiplai_core.specs import is_ready

from build_pipeline.orchestrator import run_orchestrator
from tests.test_git_ops import make_repo
from tests.test_ready_specs import CHANGE, Models, _args, _config

pytestmark = pytest.mark.no_handoff

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parents[4]
DRIVER = Path(__file__).with_name("pm_spec_driver.py")
PRD = "# Return a value\n\n## Return a value\nThe app returns a value when asked.\n"


class ReachedTdd(BaseException):
    """Raised by the TDD stand-in: the build got to TDD with ready specs."""


def _pm_spec(sandbox: Path, repo: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "CLAUDE", "MULTIPLAI_", "BUILDME_", "PM_SPEC_", "VIRTUAL_ENV"))
           and k != "WORKSPACE"}
    for name in ("claude-config", "conf", "data"):
        (sandbox / name).mkdir(exist_ok=True)
    env.update(CLAUDE_CONFIG_DIR=str(sandbox / "claude-config"),
               CLAUDE_MULTIPLAI_HOME=str(sandbox / "conf"),
               CLAUDE_PLUGIN_OPTION_DATA_DIR=str(sandbox / "data"),
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    argv = [shutil.which("uv") or "uv", "run", "--project", str(REPO_ROOT), "--package", "pm-spec-pipeline",
            "python", str(DRIVER), *argv]
    return subprocess.run(argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=600)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv runs pm-spec in its own environment")
@pytest.mark.asyncio
async def test_pm_spec_ready_specs_build_to_tdd_and_an_edit_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    from build_pipeline import orchestrator
    monkeypatch.setattr(orchestrator, "_run_spec_extract_step", lambda config, state, progress: None)
    repo = make_repo(tmp_path / "proj")
    prd = tmp_path / "prd.md"
    prd.write_text(PRD)

    # pm-spec: the specs, checked, marked ready and committed.
    proc = _pm_spec(tmp_path, repo, "new", CHANGE, "--project-dir", str(repo), "--prd", str(prd),
                    "--auto", "--trust-repo", "--no-prototype", "--no-epic-split")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    change_dir = repo / "specs" / "changes" / CHANGE
    assert f"SPEC:READY:{change_dir}:" in proc.stdout
    assert is_ready(change_dir)[0]
    assert subprocess.run(["git", "status", "--porcelain", "--", "specs"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout == ""

    # buildme: plans from them in its worktree and starts TDD.
    reached = {}

    async def tdd(config, args, standalone=True):
        reached["change_dir"] = config.change_dir
        raise ReachedTdd

    models = Models()
    p = models.patches()
    with p[0], p[1], p[2], p[3], p[4], patch("build_pipeline.tdd_engine.run_tdd_engine", tdd):
        with pytest.raises(ReachedTdd):
            await run_orchestrator(_config(repo, spec_only=False), _args(repo, spec_only=False))
    built = reached["change_dir"]
    assert built == tmp_path / "ws" / ".worktrees" / f"buildme-{CHANGE}" / "specs" / "changes" / CHANGE
    for name in ("proposal.md", "use-cases.md", "requirements/fixture.md"):
        assert (built / name).read_bytes() == (change_dir / name).read_bytes(), name
    assert (built / "design.md").is_file() and (built / "tasks.md").is_file()
    generated = {c.args[0] for c in models.generate.await_args_list}
    assert generated == {"design", "tasks"}, generated

    # One requirement edited after the handoff: the next build refuses.
    requirement = built / "requirements" / "fixture.md"
    requirement.write_text(requirement.read_text() + "\nThe value SHALL be 2.\n")
    capsys.readouterr()
    models = Models()
    p = models.patches()
    with p[0], p[1], p[2], p[3], p[4], patch("build_pipeline.tdd_engine.run_tdd_engine", tdd):
        rc = await run_orchestrator(_config(repo, spec_only=False), _args(repo, spec_only=False))
    out = capsys.readouterr().out
    assert rc == 2
    assert "SPEC:NOT_READY:the specs changed after they were marked ready" in out
    assert f"Run /multiplai-pm:pm-spec {CHANGE} first" in out
    models.generate.assert_not_awaited()

