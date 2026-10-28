"""Stop 4: an engineer approves the implementation plan before TDD starts
(`orchestrator._plan_approval_gate`, `review.plan_approval`).

Chunk E of plan-multiplai-dev-pipeline-viewer-2026-10-28.md: items E1-E3 and
E6, and rows S8, S9 and S10 of its Stop and resume table (S12, the epic
child, is in test_epic_run.py). Each row's case is compared with a run that
never stopped where the row says.

The repos are real temp repos and the approval is a real commit, read back
with `git log`; every model call is a fake (`planning_fakes`).
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest
from build_pipeline import git_ops
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.models import BuildPhase
from build_pipeline.orchestrator import (
    PLAN_APPROVAL_DOC,
    PLAN_APPROVAL_FILE,
    _plan_approval_gate,
    run_orchestrator,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, PlanApproval, completed_state_path

from tests.planning_fakes import accepted_planning  # noqa: F401
from tests.test_git_ops import make_repo

CHANGE = "csv"


@pytest.fixture(autouse=True)
def _no_spec_extract(monkeypatch):
    from build_pipeline import orchestrator

    monkeypatch.setattr(
        orchestrator, "_run_spec_extract_step", lambda config, state, progress: None
    )


def _args(project: Path, **over) -> argparse.Namespace:
    base = {
        "change": CHANGE,
        "project_dir": str(project),
        "auto": True,
        "spec_only": True,
        "skip_research": True,
        "research_path": "",
        "session_id": "",
        "accept_size": False,
        "approve_plan": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


class Build:
    """A project repo with a change planned up to the plan check, built in
    place (spec-only) so every run ends at the gate or just after it."""

    def __init__(self, root: Path, *, gate: bool = True):
        self.repo = make_repo(root)
        self.gate = gate
        config = self.config()
        config.change_dir.mkdir(parents=True, exist_ok=True)
        state = BuildState(
            change_name=CHANGE,
            tier="advanced",
            phase=BuildPhase.PLAN_REVIEW,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())

    def config(self) -> BuildConfig:
        config = BuildConfig(
            project_dir=self.repo,
            change_name=CHANGE,
            auto=True,
            spec_only=True,
            skip_research=True,
            git=GitToggles(worktree=False),
        )
        config.specs_dir = self.repo / "specs"
        config.plan_approval = self.gate
        return config

    async def run(self, capsys, **over) -> tuple[int, str]:
        capsys.readouterr()
        rc = await run_orchestrator(self.config(), _args(self.repo, **over))
        return rc, capsys.readouterr().out

    @property
    def change_dir(self) -> Path:
        return self.config().change_dir

    def state(self) -> BuildState:
        path = self.config().state_file_path()
        if not path.exists():
            path = completed_state_path(path)
        return BuildState.load(path)

    def revision(self) -> str:
        return self.state().spec_gen.revision

    def git(self, *argv: str) -> str:
        return subprocess.run(
            ["git", *argv],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def approve(self, revision: str, who: str = "Ada Lovelace", key="Approved-by"):
        """The commit plan-approval.md tells the engineer to make (the page
        makes the same one)."""
        self.git(
            "commit",
            "--only",
            "--allow-empty",
            "-m",
            f"review({CHANGE}): approve the implementation plan",
            "--trailer",
            "Stop: 4",
            "--trailer",
            f"Revision: {revision}",
            "--trailer",
            f"{key}: {who}",
        )
        return self.git("rev-parse", "HEAD").strip()

    def approvals(self) -> list[str]:
        return [
            sha
            for sha, trailers in git_ops.commit_trailers(self.repo)
            if trailers.get("stop") == "4"
        ]


def _ended(out: str) -> list[str]:
    """The lines a wrapper acts on, in order."""
    return [
        line
        for line in out.splitlines()
        if line.startswith(("PHASE:", "RESULT:", "PLAN:", "ERROR:"))
    ]


# --- E1 ---------------------------------------------------------------------------


def test_config_plan_approval_is_off_unless_set_true(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    cases = {
        "": False,
        "review:\n  plan_approval: true\n": True,
        "review:\n  plan_approval: false\n": False,
        "review:\n  plan_approval: yes please\n": False,
    }
    for text, want in cases.items():
        (specs / "config.yaml").write_text("context: x\n" + text)
        config = BuildConfig(project_dir=tmp_path, change_name=CHANGE)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.plan_approval is want, text


def test_turning_the_gate_on_changes_no_plan_revision(tmp_path):
    """`review:` is cut out of the plan's hash: turning stop 4 on for a
    planned build asks for an approval of that plan, never a new plan."""
    from build_pipeline.spec_generator import _config_hashes

    specs = tmp_path / "specs"
    specs.mkdir()
    config = BuildConfig(project_dir=tmp_path, change_name=CHANGE)
    config.specs_dir = specs
    (specs / "config.yaml").write_text("context: x\n")
    before = _config_hashes(config)
    (specs / "config.yaml").write_text("context: x\nreview:\n  plan_approval: true\n")
    assert _config_hashes(config) == before


# --- E6 ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_off_changes_nothing(tmp_path, capsys):
    """Off, the build runs exactly as one without the key, and
    `--approve-plan` commits nothing."""
    off = Build(tmp_path / "off", gate=False)
    rc, out = await off.run(capsys, approve_plan=True)
    assert rc == 0
    assert "PLAN:WAITING" not in out
    assert not (off.change_dir / PLAN_APPROVAL_FILE).exists()
    assert off.approvals() == []
    assert off.state().plan_approval is None

    plain = Build(tmp_path / "plain", gate=False)
    rc2, out2 = await plain.run(capsys)
    assert rc2 == rc and _ended(out2) == _ended(out)


@pytest.mark.asyncio
async def test_gate_on_without_an_approval_stops_with_the_marker(tmp_path, capsys):
    b = Build(tmp_path / "p")
    rc, out = await b.run(capsys)
    path = b.change_dir / PLAN_APPROVAL_FILE
    assert rc == 3
    assert f"PLAN:WAITING:{path}" in out
    assert "RESULT:SUCCESS" not in out
    text = path.read_text()
    assert f"Plan revision: `{b.revision()}`" in text
    assert f"`{PLAN_APPROVAL_DOC}`: how the change will be implemented. Approve" in text
    assert "`tasks.md`: 1 block(s)" in text
    assert "- Outcome: passed" in text
    assert f"Revision: {b.revision()}" in text
    state = b.state()
    assert state.plan_approval is None
    assert state.phase == BuildPhase.PLAN_REVIEW


@pytest.mark.asyncio
async def test_approval_commit_for_the_current_revision_passes(tmp_path, capsys):
    """Row S8, approval present: the re-run passes, and ends as a run whose
    approval was on the branch before it started (never stopped)."""
    stopped = Build(tmp_path / "stopped")
    rc, _ = await stopped.run(capsys)
    assert rc == 3
    sha = stopped.approve(stopped.revision())
    rc, out = await stopped.run(capsys)
    assert rc == 0
    assert "PLAN:WAITING" not in out
    got = stopped.state().plan_approval
    assert got == PlanApproval(
        revision=stopped.revision(), by="Ada Lovelace", commit=sha
    )
    progress = (stopped.repo / "build-progress.md").read_text()
    assert "approved by Ada Lovelace" in progress

    never = Build(tmp_path / "never")
    # Plan the same change once without the gate, to know its revision.
    never.gate = False
    assert (await never.run(capsys))[0] == 0
    never.gate = True
    never_sha = never.approve(never.revision())
    rc2, out2 = await never.run(capsys)
    assert rc2 == rc and _ended(out2) == _ended(out)
    assert never.state().plan_approval == PlanApproval(
        revision=never.revision(), by="Ada Lovelace", commit=never_sha
    )
    assert never.revision() == stopped.revision()


@pytest.mark.asyncio
async def test_a_decided_by_commit_passes_too(tmp_path, capsys):
    b = Build(tmp_path / "p")
    assert (await b.run(capsys))[0] == 3
    sha = b.approve(b.revision(), who="agent", key="Decided-by")
    rc, _ = await b.run(capsys)
    assert rc == 0
    assert b.state().plan_approval == PlanApproval(
        revision=b.revision(), by="agent", commit=sha
    )


@pytest.mark.asyncio
async def test_s8_absent_stops_again_without_rewriting_the_request(tmp_path, capsys):
    """Row S8, approval absent: the marker again and exit 3, with
    plan-approval.md as the first stop left it; the same as the first stop."""
    b = Build(tmp_path / "p")
    rc, out = await b.run(capsys)
    path = b.change_dir / PLAN_APPROVAL_FILE
    path.write_text(path.read_text() + "\nA note the engineer added.\n")
    before = path.read_text()
    rc2, out2 = await b.run(capsys)
    assert (rc2, _ended(out2)) == (rc, _ended(out)) == (3, _ended(out))
    assert f"PLAN:WAITING:{path}" in out2
    assert path.read_text() == before
    assert b.state().plan_approval is None


@pytest.mark.asyncio
async def test_a_commit_for_an_older_revision_stops_again(tmp_path, capsys):
    b = Build(tmp_path / "p")
    assert (await b.run(capsys))[0] == 3
    old = b.revision()
    b.approve(old)
    (b.change_dir / PLAN_APPROVAL_DOC).write_text("# Design\n\nA second approach.\n")
    rc, out = await b.run(capsys)
    assert rc == 3 and "PLAN:WAITING:" in out
    assert b.revision() != old
    assert b.state().plan_approval is None
    text = (b.change_dir / PLAN_APPROVAL_FILE).read_text()
    assert f"Plan revision: `{b.revision()}`" in text


@pytest.mark.asyncio
async def test_s9_approved_then_edited_clears_and_stops_again(tmp_path, capsys):
    """Row S9: the saved approval names the old revision; the re-run clears
    it and stops, as a build that reached the edited plan unapproved does."""
    b = Build(tmp_path / "edited")
    assert (await b.run(capsys))[0] == 3
    b.approve(b.revision())
    assert (await b.run(capsys))[0] == 0
    saved = b.state().plan_approval
    assert saved is not None
    edit = "# Design\n\nA second approach.\n"
    (b.change_dir / PLAN_APPROVAL_DOC).write_text(edit)
    rc, out = await b.run(capsys)
    assert rc == 3
    assert b.state().plan_approval is None
    assert b.revision() != saved.revision
    request = (b.change_dir / PLAN_APPROVAL_FILE).read_text()

    never = Build(tmp_path / "never")
    never.gate = False
    assert (await never.run(capsys))[0] == 0
    (never.change_dir / PLAN_APPROVAL_DOC).write_text(edit)
    never.gate = True
    rc2, out2 = await never.run(capsys)
    assert (rc2, _ended(out2)) == (
        rc,
        [line.replace(str(b.repo), str(never.repo)) for line in _ended(out)],
    )
    assert never.revision() == b.revision()
    assert (never.change_dir / PLAN_APPROVAL_FILE).read_text() == request


@pytest.mark.asyncio
async def test_s10_killed_after_the_approval_commit(tmp_path, capsys, monkeypatch):
    """Row S10: `--approve-plan` committed, then the run died before the
    checkpoint. The re-run (no flag) finds the commit and saves the field;
    nothing else differs from a run that was never killed."""
    killed = Build(tmp_path / "killed")
    assert (await killed.run(capsys))[0] == 3
    real = BuildState.checkpoint

    def die_once_approved(self, path=None):
        if self.plan_approval is not None:
            raise KeyboardInterrupt
        return real(self, path)

    monkeypatch.setattr(BuildState, "checkpoint", die_once_approved)
    with pytest.raises(KeyboardInterrupt):
        await killed.run(capsys, approve_plan=True)
    monkeypatch.setattr(BuildState, "checkpoint", real)
    assert len(killed.approvals()) == 1
    assert killed.state().plan_approval is None
    rc, out = await killed.run(capsys)

    never = Build(tmp_path / "never")
    assert (await never.run(capsys))[0] == 3
    rc2, out2 = await never.run(capsys, approve_plan=True)
    assert (rc, _ended(out)) == (rc2, _ended(out2)) == (0, _ended(out2))
    for b in (killed, never):
        assert len(b.approvals()) == 1
    got, want = killed.state().plan_approval, never.state().plan_approval
    assert got.commit == killed.approvals()[0]
    assert (
        (got.revision, got.by)
        == (want.revision, want.by)
        == (
            never.revision(),
            "Test",
        )
    )


@pytest.mark.asyncio
async def test_approve_plan_commits_the_approval_and_nothing_else(tmp_path, capsys):
    """`--approve-plan`: one empty commit in git user.name's name with the
    three trailers; a file someone staged stays staged and out of it."""
    b = Build(tmp_path / "p")
    assert (await b.run(capsys))[0] == 3
    (b.repo / "notes.txt").write_text("staged by the engineer\n")
    b.git("add", "notes.txt")
    rc, out = await b.run(capsys, approve_plan=True)
    assert rc == 0 and "PLAN:WAITING" not in out
    trailers = b.git("log", "-1", "--format=%(trailers:only,unfold)")
    assert trailers.strip().splitlines() == [
        "Stop: 4",
        f"Revision: {b.revision()}",
        "Approved-by: Test",
    ]
    assert b.git("show", "--name-only", "--format=", "HEAD").strip() == ""
    assert "A  notes.txt" in b.git("status", "--short")
    assert b.state().plan_approval.commit == b.approvals()[0]

    # Given again, it finds the approval and makes no second commit.
    state_path = b.config().state_file_path()
    state = b.state()
    state.plan_approval = None
    state.checkpoint(state_path)
    completed_state_path(state_path).unlink(missing_ok=True)
    rc, _ = await b.run(capsys, approve_plan=True)
    assert rc == 0 and len(b.approvals()) == 1


@pytest.mark.asyncio
async def test_approve_plan_without_a_git_user_name_refuses(tmp_path, capsys):
    b = Build(tmp_path / "p")
    assert (await b.run(capsys))[0] == 3
    b.git("config", "--unset", "user.name")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(git_ops, "user_name", lambda repo: "")
        rc, out = await b.run(capsys, approve_plan=True)
    assert rc == 1
    assert "ERROR:--approve-plan needs git user.name" in out
    assert b.approvals() == []


def test_gate_unit_saved_approval_for_this_revision_skips_git(tmp_path, monkeypatch):
    """A saved approval at the current revision passes without reading git."""
    from build_pipeline.state import SpecGenState

    config = BuildConfig(project_dir=tmp_path, change_name=CHANGE)
    config.specs_dir = tmp_path / "specs"
    config.plan_approval = True
    state = BuildState(
        change_name=CHANGE,
        tier="advanced",
        spec_gen=SpecGenState(revision="r1"),
        plan_approval=PlanApproval(revision="r1", by="Ada", commit="c" * 40),
    )

    def no_git(repo):
        raise AssertionError("git read")

    monkeypatch.setattr(git_ops, "commit_trailers", no_git)
    path = tmp_path / "state.json"
    progress = ProgressWriter(tmp_path / "progress.md")
    assert _plan_approval_gate(config, state, path, progress, approve=False) is None


def test_cli_build_accepts_approve_plan():
    from build_pipeline.__main__ import build_parser

    args = build_parser().parse_args(["build", "--change", CHANGE, "--approve-plan"])
    assert args.approve_plan is True
    args = build_parser().parse_args(["build", "--change", CHANGE])
    assert args.approve_plan is False
