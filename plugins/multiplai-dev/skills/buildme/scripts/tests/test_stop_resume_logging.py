"""Stop and resume rows S1–S11 of plan-logging-and-session-models-2026-10-16.md.

Each run is one `build` process (`stop_resume_driver.py`): the real
orchestrator, with the model-calling phases replaced by fakes that record a
fixed spend, so a stop is a real SIGKILL or SIGTERM. A case compares the
progress file, the saved spend and the completed checkpoint of a run that was
stopped and resumed with those of a run that never stopped. They match except
for the RESUMED / INTERRUPTED / BUDGET sections and the spend of work the row
says is redone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from build_pipeline.models import BlockStatus, BuildPhase
from build_pipeline.state import BuildState
from tests.stop_resume_driver import AUDIT_USD, BLOCK_USD, BLOCKS, SPEC_USD
from tests.test_git_ops import make_bare_origin, make_repo

DRIVER = Path(__file__).with_name("stop_resume_driver.py")
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FULL_USD = SPEC_USD + AUDIT_USD + BLOCKS * BLOCK_USD
COMPLETED = ".build-state.complete.json"

_TS = re.compile(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] ?")
# Sections a stop and a resume add, and the spend summary, whose figures differ.
_RUN_SECTIONS = ("RESUMED", "INTERRUPTED", "BUDGET")


class Sandbox:
    """A source repo with an origin, a workspace, and a clean environment."""

    def __init__(self, root: Path):
        self.root = root
        self.repo = make_repo(root / "proj")
        make_bare_origin(self.repo, root / "origin.git")
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.repo, check=True)
        self.ws = root / "ws"
        for name in ("home", "claude-config", "conf", "data"):
            (root / name).mkdir()

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PYTEST", "CLAUDE", "MULTIPLAI_", "BUILDME_"))
               and k not in ("WORKSPACE", "HOME")}
        env.update(
            HOME=str(self.root / "home"),
            CLAUDE_CONFIG_DIR=str(self.root / "claude-config"),
            CLAUDE_MULTIPLAI_HOME=str(self.root / "conf"),
            CLAUDE_PLUGIN_OPTION_DATA_DIR=str(self.root / "data"),
            WORKSPACE=str(self.ws),
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        return env

    def _spec(self, project: Path | None, spec: dict) -> Path:
        spec = {"project_dir": str(project or self.repo), "change": "feat", **spec}
        path = self.root / f"spec-{time.monotonic_ns()}.json"
        path.write_text(json.dumps(spec))
        return path

    def run(self, project: Path | None = None, **spec) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(DRIVER), str(self._spec(project, spec))],
                              cwd=SCRIPTS_DIR, env=self.env(), capture_output=True,
                              text=True, timeout=180)

    def start(self, **spec) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(DRIVER), str(self._spec(None, spec))],
                                cwd=SCRIPTS_DIR, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def worktree(self, n: int = 1) -> Path:
        return self.ws / ".worktrees" / ("buildme-feat" if n == 1 else f"buildme-feat-{n}")

    def progress(self, n: int = 1) -> str:
        return (self.worktree(n) / "build-progress.md").read_text()

    def live_state(self, n: int = 1) -> BuildState:
        return BuildState.load(self.worktree(n) / "specs" / "changes" / "feat" / ".build-state.json")

    def completed(self, n: int = 1) -> BuildState:
        found = list((self.worktree(n) / "specs").rglob(COMPLETED))
        assert len(found) == 1, found
        return BuildState.load(found[0])


def _ok(proc) -> None:
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def _killed(proc) -> None:
    assert proc.returncode == -9, (proc.returncode, proc.stderr[-2000:])


def _kept_lines(text: str, root: Path) -> list[str]:
    """The progress file's lines after its header, without timestamps or the
    sandbox path, leaving out the sections a stop, a resume or the spend
    summary add."""
    lines, keep = [], False
    for raw in text.replace(str(root), "<root>").splitlines():
        line = _TS.sub("", raw).rstrip()
        line = re.sub(r"spec-extract at [0-9a-f]{12}", "spec-extract at <revision>", line)
        if line.startswith("## "):
            keep = not line[3:].startswith(_RUN_SECTIONS)
        if keep and line:
            lines.append(line)
    return lines


def _is_subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


def assert_same_progress(stopped: Sandbox, full: Sandbox, n: int = 1) -> None:
    """Every line of the run that never stopped is in the resumed one, in
    order, and the resumed one has no line that run does not have. The header
    (title, mode, tier, model and effort) is the same."""
    a, b = full.progress(), stopped.progress(n)
    assert a.split("\n## ", 1)[0] == b.split("\n## ", 1)[0]
    kept_a, kept_b = _kept_lines(a, full.root), _kept_lines(b, stopped.root)
    assert _is_subsequence(kept_a, kept_b), (kept_a, kept_b)
    assert set(kept_b) <= set(kept_a), set(kept_b) - set(kept_a)


def _usd(state: BuildState) -> float:
    return round(state.budget["cost_usd"], 6)


def _snapshot(*roots: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for root in roots for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def full(tmp_path):
    """A run that never stopped."""
    box = Sandbox(tmp_path / "full")
    _ok(box.run())
    return box


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path / "box")


def test_full_run_keeps_progress_spend_and_the_completed_checkpoint(full):
    """Done means 6: the completed checkpoint and the progress file stay, and
    the progress file has a BUDGET section with USD per step label."""
    done = full.completed()
    assert done.phase == BuildPhase.COMPLETE and _usd(done) == FULL_USD
    progress = full.progress()
    assert progress.count("# Build Progress: feat") == 1
    assert "Model: CLI default | Effort: CLI default" in progress
    budget = progress.rsplit("] BUDGET\n", 1)[1]
    for label, usd in (("spec_generation", SPEC_USD), ("design_audit", AUDIT_USD),
                       ("implementer", BLOCKS * BLOCK_USD)):
        assert f"{label}=" in budget and f"/${usd:.2f}" in budget
    assert "COMPLETE\nPublished: https://github.com/acme/proj/pull/7" in progress


def test_S1_killed_between_spec_phases_and_tdd(box, full):
    _killed(box.run(stop_at="tdd-start", stop_how="kill"))
    saved = box.live_state()
    assert saved.phase == BuildPhase.REVIEW and _usd(saved) == SPEC_USD + AUDIT_USD
    before = box.progress()

    _ok(box.run())
    assert box.progress().startswith(before)
    assert box.progress().count("] RESUMED\n") == 1
    assert_same_progress(box, full)
    assert _usd(box.completed()) == _usd(full.completed()) == FULL_USD


def test_S2_killed_mid_block_after_its_checkpoint(box, full):
    _killed(box.run(stop_at="block-1-mid", stop_how="kill", checkpoint_mid=True))
    saved = box.live_state()
    assert saved.tdd.blocks[0].status == BlockStatus.IMPLEMENTING
    assert _usd(saved) == SPEC_USD + AUDIT_USD + BLOCK_USD

    _ok(box.run())
    assert box.progress().count("] RESUMED\n") == 1
    assert_same_progress(box, full)
    # The block is redone from its saved commits: its spend counts twice.
    assert _usd(box.completed()) == FULL_USD + BLOCK_USD


def test_S3_sigterm_mid_block_writes_the_stop_and_an_up_to_date_checkpoint(box, full):
    proc = box.run(stop_at="block-1-mid", stop_how="SIGTERM")
    assert proc.returncode == 143, proc.stderr[-2000:]
    assert "] INTERRUPTED by SIGTERM\n" in box.progress()
    saved = box.live_state()
    assert saved.phase != BuildPhase.FAILED
    # The block's spend was never checkpointed by the block; the stop saved it.
    assert _usd(saved) == SPEC_USD + AUDIT_USD + BLOCK_USD

    _ok(box.run())
    assert_same_progress(box, full)
    assert _usd(box.completed()) == FULL_USD + BLOCK_USD


def test_S4_rerun_of_a_published_build_asks_and_writes_nothing(full):
    roots = (full.repo, full.ws / ".worktrees")
    before = _snapshot(*roots)

    proc = full.run()
    assert proc.returncode == 3, proc.stderr[-2000:]
    path = full.worktree() / "specs" / "archive"
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BUILD:COMPLETE:"))
    assert Path(line.split(":", 2)[2]).parent.parent == path
    assert "branch buildme/feat" in proc.stderr and "https://github.com/acme/proj/pull/7" in proc.stderr
    assert "ERROR" not in proc.stderr
    assert _snapshot(*roots) == before


@pytest.mark.parametrize("force_again", [False, True])
def test_S5_force_builds_anew_on_the_next_branch_and_resumes_there(full, force_again):
    first = _snapshot(full.worktree())

    _killed(full.run(force=True, stop_at="tdd-start", stop_how="kill"))
    assert full.worktree(2).is_dir()
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=full.worktree(2),
                            capture_output=True, text=True).stdout.strip()
    assert branch == "buildme/feat-2"

    # The stopped forced build resumes: its .build-state.json wins over the
    # published build's completed one, with or without --force.
    _ok(full.run(force=force_again))
    assert not full.worktree(3).exists()
    assert full.completed(2).phase == BuildPhase.COMPLETE
    assert full.progress(2).count("] RESUMED\n") == 1
    assert _snapshot(full.worktree()) == first


def test_S6_spec_only_keeps_its_checkpoint_and_the_mode_only_handoff_runs(box):
    proc = box.run(spec_only=True)
    _ok(proc)
    assert "RESULT:SUCCESS:spec-only" in proc.stdout
    done = BuildState.load(box.worktree() / "specs" / "changes" / "feat" / COMPLETED)
    assert done.phase == BuildPhase.PLAN_REVIEW
    assert done.result.kind == "spec-only"
    specs_progress = box.progress()

    proc = box.run(project=box.worktree(), mode="only")
    _ok(proc)
    assert not any(ln.startswith("BUILD:COMPLETE:") for ln in proc.stdout.splitlines())
    after = box.progress()
    assert after.startswith(specs_progress)
    resumed = after[len(specs_progress):]
    assert "] RESUMED\nTier: advanced" in resumed and "Block 2/2: block-2 — done" in resumed


def test_S7_a_standalone_tdd_completion_does_not_stop_the_next_build(box):
    """`tdd`'s own success keeps the checkpoint at TDD_BUILD
    (test_tdd_engine.py::test_standalone_run_keeps_its_checkpoint_as_completed);
    such a file is not a published build."""
    change_dir = box.repo / "specs" / "changes" / "feat"
    tdd_done = BuildState(change_name="feat", mode="only", tier="standard",
                          phase=BuildPhase.TDD_BUILD, state_file=str(change_dir / ".build-state.json"))
    tdd_done.checkpoint()
    tdd_done.complete()

    proc = box.run()
    _ok(proc)
    assert not any(ln.startswith("BUILD:COMPLETE:") for ln in proc.stdout.splitlines())
    assert box.completed().phase == BuildPhase.COMPLETE


def test_S8_files_edited_between_stop_and_resume(box, full):
    _killed(box.run(stop_at="tdd-start", stop_how="kill"))
    (box.worktree() / "README.md").write_text("edited by the user\n")
    (box.worktree() / "notes.txt").write_text("the user's own file\n")

    proc = box.run()
    proceeded = proc.returncode == 0
    assert not proceeded
    assert "Uncommitted build inputs remain" in proc.stderr
    assert "RESULT:SUCCESS" not in proc.stdout
    assert (box.worktree() / "notes.txt").read_text() == "the user's own file\n"
    if proceeded:
        assert_same_progress(box, full)


def test_S9_origin_moved_between_stop_and_resume(box, full):
    _killed(box.run(stop_at="tdd-start", stop_how="kill"))
    (box.repo / "LATER.md").write_text("later\n")
    subprocess.run(["git", "add", "LATER.md"], cwd=box.repo, check=True)
    subprocess.run(["git", "commit", "-qm", "later on main"], cwd=box.repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=box.repo, check=True)

    _ok(box.run())
    log = subprocess.run(["git", "log", "--format=%s"], cwd=box.worktree(),
                         capture_output=True, text=True).stdout
    assert "later on main" not in log            # no fetch, no rebase
    assert_same_progress(box, full)
    assert _usd(box.completed()) == FULL_USD


def test_S10_stopped_twice_keeps_both_resumes_and_all_spend(box, full):
    _killed(box.run(stop_at="tdd-start", stop_how="kill"))
    _killed(box.run(stop_at="block-2-mid", stop_how="kill", checkpoint_mid=True))
    _ok(box.run())
    assert box.progress().count("] RESUMED\n") == 2
    assert_same_progress(box, full)
    assert _usd(box.completed()) == FULL_USD + BLOCK_USD


def test_S11_resume_while_the_stopped_process_still_runs(box):
    hold = box.root / "hold"
    hold.mkdir()
    first = box.start(stop_at="block-1-mid", stop_how="hold", hold_dir=str(hold),
                      tag_pid=True, checkpoint_mid=True)
    try:
        deadline = time.monotonic() + 120
        while not (hold / "held").exists():
            assert first.poll() is None, first.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "the first run never reached its hold"
            time.sleep(0.05)
        first_pid = (hold / "held").read_text()

        second = box.run(tag_pid=True)
        _ok(second)
    finally:
        (hold / "release").write_text("")
        first.wait(timeout=60)
    progress = box.progress()
    pids = re.findall(r"pid \(block-\d\): (\d+)", progress)
    assert first_pid in pids and any(p != first_pid for p in pids)
    assert progress.count("# Build Progress: feat") == 1
    assert progress.count("] RESUMED\n") == 1
