"""One case per row S1-S14 of the Stop and resume states table in
plan-buildme-planning-checks-2026-10-27.md. Each case stops the plan check at
the row's point (or applies its perturbation), resumes, and compares the
result with a run that never stopped: the plan files, the change-dir commits
and the saved `steps.plan_review`, except where the row says otherwise. S15
adds no behaviour (no lock), so it has no case.

A stop is a `Killed` (a BaseException) raised at the row's point; the stage's
own `except BaseException` records what a SIGTERM leaves on disk."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import budget
from build_pipeline import spec_generator as owner
from build_pipeline.llm_steps import plan_review_steps
from build_pipeline.models import (
    PlanEdit,
    PlanEdits,
    PlanReviewFinding,
    PlanReviewResult,
    PriorAssessment,
)
from build_pipeline.spec_generator import PlanBlocked, PlanRepairRejected
from build_pipeline.state import BuildState, SpecGenState

from tests.test_plan_review import _stage_setup

STEP = "plan_review"
REWRITES = [
    "## 1. Behavior\n- [ ] do it properly\n",
    "## 1. Behavior\n- [ ] do it right\n",
]
PLAN_FILES = ("implementation-plan.md", "tasks.md", "unknowns.md", "rubric.md")


class Killed(BaseException):
    """Stands in for the process stopping at the point a row names."""


def _gap(claim):
    return PlanReviewFinding(
        category="coverage",
        severity="critical",
        file_path="tasks.md",
        claim=claim,
        reason="fix it",
    )


def _recheck(**status):
    return PlanReviewResult(
        prior_assessments=[
            PriorAssessment(finding_id=i, status=s) for i, s in status.items()
        ]
    )


FIRST = PlanReviewResult(findings=[_gap("ONE"), _gap("TWO")])
RECHECK_1 = _recheck(**{"PR-F1": "resolved", "PR-F2": "still_present"})
RECHECK_2 = _recheck(**{"PR-F2": "resolved"})
# The run that never stops: a check, two rewrites, two rechecks.
CHECKS = [FIRST, RECHECK_1, RECHECK_2]


@pytest.fixture(autouse=True)
def rubric_writer():
    with patch(
        "build_pipeline.spec_generator.generate_rubric",
        AsyncMock(return_value="# Current rubric"),
    ):
        yield


class Plan:
    """A change dir, its checkpoint, and queues of what the check and the
    edit call answer, shared by every run of the stage on it."""

    def __init__(self, tmp_path, checks=CHECKS, writes=REWRITES):
        self.config, state, self.state_path = _stage_setup(tmp_path)
        (self.config.specs_dir / "config.yaml").write_text("project: x\n")
        owner.reconcile_plan(self.config, state)
        state.checkpoint(self.state_path)
        self.checks = list(checks)
        self.writes = list(writes)
        self.check_calls = 0
        self.edit_calls = 0
        self.commits: list[str] = []

    async def _check(self, *a, **kw):
        self.check_calls += 1
        item = self.checks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def _edit(self, change_dir, config, findings):
        self.edit_calls += 1
        item = self.writes.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, PlanEdits):
            return item
        old = (change_dir / "tasks.md").read_text()
        return PlanEdits(edits=[PlanEdit(file="tasks.md", old_str=old, new_str=item)])

    def state(self) -> BuildState:
        return BuildState.load(self.state_path)

    async def run(self):
        """One run of the stage on the saved checkpoint; returns the
        exception it ended with, or None."""
        state = self.state()
        with (
            patch.object(plan_review_steps, "run_plan_review", self._check),
            patch.object(plan_review_steps, "run_plan_edit", self._edit),
            patch.object(
                owner,
                "_commit_change_dir",
                lambda config, change_dir, message: self.commits.append(message),
            ),
        ):
            try:
                await plan_review_steps.run_plan_review_stage(
                    self.config.change_dir, self.config, state, self.state_path
                )
            except BaseException as error:  # noqa: BLE001 - a stop is the point
                return error
        return None

    def outcome(self):
        """What a resumed run is compared on."""
        step = self.state().spec_gen.steps[STEP]
        files = {
            p.name: p.read_bytes()
            for p in sorted(self.config.change_dir.iterdir())
            if p.name in PLAN_FILES or p.name.startswith("plan-edits-")
        }
        return {
            "files": files,
            "commits": list(self.commits),
            "step": (
                step.outcome,
                step.repair_attempts,
                step.repair_findings,
                step.blocking_counts,
                step.blocked_revision,
            ),
        }


async def _reference(tmp_path):
    plan = Plan(tmp_path / "reference")
    assert await plan.run() is None
    return plan


async def _stopped_then_resumed(tmp_path, checks, writes=REWRITES, between=None):
    plan = Plan(tmp_path / "stopped", checks, writes)
    assert isinstance(await plan.run(), Killed)
    if between:
        between(plan)
    error = await plan.run()
    return plan, error


# --- S1, S2: before admission --------------------------------------------------


@pytest.mark.asyncio
async def test_S1_killed_during_the_first_check_checks_again(tmp_path):
    reference = await _reference(tmp_path)
    plan = Plan(tmp_path / "stopped", [Killed(), *CHECKS])
    assert isinstance(await plan.run(), Killed)
    step = plan.state().spec_gen.steps[STEP]
    assert (step.outcome, step.repair_attempts, step.blocked_revision) == (
        "blocked",
        0,
        None,
    )
    assert await plan.run() is None
    assert plan.outcome() == reference.outcome()
    assert plan.check_calls == reference.check_calls + 1


@pytest.mark.asyncio
async def test_S2_killed_before_the_admission_checks_again(tmp_path, monkeypatch):
    reference = await _reference(tmp_path)
    admit = SpecGenState.admit_repair
    calls = []

    def die_once(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise Killed()
        return admit(self, *a, **kw)

    monkeypatch.setattr(SpecGenState, "admit_repair", die_once)
    plan, error = await _stopped_then_resumed(tmp_path, [FIRST, *CHECKS])
    assert error is None
    assert plan.outcome() == reference.outcome()
    assert plan.check_calls == reference.check_calls + 1


# --- S3, S14: after admission, before the edit returns -------------------------


@pytest.mark.asyncio
async def test_S3_an_interrupted_rewrite_counts_and_the_next_is_admitted(tmp_path):
    """Attempt 1 is admitted and stopped; the resume checks again and admits
    attempt 2, the last one, so it ends at the rewrite limit."""
    plan, error = await _stopped_then_resumed(
        tmp_path, [FIRST, FIRST, RECHECK_1], [Killed(), REWRITES[0]]
    )
    assert isinstance(error, PlanBlocked)
    assert "2 of planning.max_rewrites 2 rewrites used" in str(error)
    step = plan.state().spec_gen.steps[STEP]
    assert step.repair_attempts == 2
    assert step.blocking_counts == [2, 2]
    assert not (plan.config.change_dir / "plan-edits-1.diff").exists()
    assert (plan.config.change_dir / "plan-edits-2.diff").exists()


@pytest.mark.asyncio
async def test_S14_stopped_twice_at_the_admission_blocks_at_the_limit(tmp_path):
    plan = Plan(tmp_path, [FIRST, FIRST, FIRST], [Killed(), Killed()])
    assert isinstance(await plan.run(), Killed)
    assert isinstance(await plan.run(), Killed)
    assert plan.state().spec_gen.steps[STEP].repair_attempts == 2
    before = {n: (plan.config.change_dir / n).read_bytes() for n in PLAN_FILES[:2]}
    error = await plan.run()
    assert isinstance(error, PlanBlocked)
    assert "2 of planning.max_rewrites 2 rewrites used" in str(error)
    assert plan.edit_calls == 2
    assert {
        n: (plan.config.change_dir / n).read_bytes() for n in PLAN_FILES[:2]
    } == before


# --- S4: an edit misses --------------------------------------------------------


@pytest.mark.asyncio
async def test_S4_a_missed_edit_refuses_until_the_plan_is_edited(tmp_path):
    miss = PlanEdits(edits=[PlanEdit(file="tasks.md", old_str="absent", new_str="x")])
    plan = Plan(tmp_path, [FIRST, FIRST, RECHECK_1, RECHECK_2], [miss, *REWRITES])
    assert isinstance(await plan.run(), PlanRepairRejected)
    calls = (plan.check_calls, plan.edit_calls)
    assert isinstance(await plan.run(), PlanBlocked)
    assert (plan.check_calls, plan.edit_calls) == calls
    design = plan.config.change_dir / "implementation-plan.md"
    design.write_text(design.read_text() + "Edited by the user.\n")
    assert await plan.run() is None
    step = plan.state().spec_gen.steps[STEP]
    assert step.outcome == "passed" and step.repair_attempts == 2


# --- S5: publication -----------------------------------------------------------


def _die_at_live_tasks(plan, monkeypatch):
    original = owner.atomic_write
    live = plan.config.change_dir / "tasks.md"
    fired = []

    def write(path, content):
        if path == live and not fired:
            fired.append(1)
            raise Killed()
        original(path, content)

    monkeypatch.setattr(owner, "atomic_write", write)


@pytest.mark.asyncio
async def test_S5_a_publication_stopped_part_way_is_finished_then_rechecked(
    tmp_path, monkeypatch
):
    reference = await _reference(tmp_path)
    plan = Plan(tmp_path / "stopped")
    _die_at_live_tasks(plan, monkeypatch)
    assert isinstance(await plan.run(), Killed)
    journal = json.loads(
        (plan.config.change_dir / ".plan-publication.json").read_text()
    )
    assert "plan-edits-1.diff" in journal
    assert await plan.run() is None
    assert not (plan.config.change_dir / ".plan-publication.json").exists()
    assert plan.outcome()["files"] == reference.outcome()["files"]
    assert plan.outcome()["step"] == reference.outcome()["step"]
    assert plan.edit_calls == reference.edit_calls


@pytest.mark.asyncio
async def test_S5_and_S12_an_edit_inside_a_stopped_publication_refuses(
    tmp_path, monkeypatch
):
    plan = Plan(tmp_path)
    _die_at_live_tasks(plan, monkeypatch)
    assert isinstance(await plan.run(), Killed)
    (plan.config.change_dir / "tasks.md").write_text("## 1. Mine\n- [ ] keep\n")
    error = await plan.run()
    assert isinstance(error, RuntimeError)
    assert "Planning publication interrupted and tasks.md changed" in str(error)


# --- S6: the recheck -----------------------------------------------------------


@pytest.mark.asyncio
async def test_S6_killed_during_the_recheck_rechecks_without_a_new_rewrite(tmp_path):
    reference = await _reference(tmp_path)
    plan, error = await _stopped_then_resumed(
        tmp_path, [FIRST, Killed(), RECHECK_1, RECHECK_2]
    )
    assert error is None
    assert plan.outcome() == reference.outcome()
    assert plan.edit_calls == reference.edit_calls


# --- S7: the count did not go down ---------------------------------------------


@pytest.mark.asyncio
async def test_S7_a_count_that_did_not_go_down_refuses_at_no_cost(tmp_path):
    plan = Plan(
        tmp_path,
        [FIRST, _recheck(**{"PR-F1": "still_present", "PR-F2": "still_present"})],
    )
    error = await plan.run()
    assert "rewrite 1 left 2 blocking findings (was 2)" in str(error)
    saved = plan.outcome()
    calls = (plan.check_calls, plan.edit_calls)
    assert isinstance(await plan.run(), PlanBlocked)
    assert (plan.check_calls, plan.edit_calls) == calls
    assert plan.outcome() == saved


# --- S8: the spend cap ---------------------------------------------------------


@pytest.mark.asyncio
async def test_S8_the_spend_cap_refuses_until_it_is_raised(tmp_path, monkeypatch):
    reference = await _reference(tmp_path)
    plan = Plan(tmp_path / "stopped", [FIRST, *CHECKS])
    plan.config.planning_max_usd = 1.0
    monkeypatch.setattr(budget.get_budget(), "usd_by_label", {"plan_review": 2.0})
    error = await plan.run()
    assert "planning spend $2.00 reached planning.max_usd $1.00" in str(error)
    assert isinstance(await plan.run(), PlanBlocked)
    assert plan.edit_calls == 0
    path = plan.config.specs_dir / "config.yaml"
    path.write_text(path.read_text() + "planning:\n  max_usd: 60\n")
    plan.config.planning_max_usd = 60.0
    stale = list(plan.state().spec_gen.stale_artifacts)
    assert await plan.run() is None
    assert plan.outcome()["files"] == reference.outcome()["files"]
    assert plan.outcome()["commits"] == reference.outcome()["commits"]
    assert set(plan.state().spec_gen.stale_artifacts) <= set(stale)


# --- S9, S10, S11: checkpoints of older code -----------------------------------


@pytest.mark.asyncio
async def test_S9_a_step_blocked_under_the_old_rule_gets_one_fresh_check(tmp_path):
    reference = await _reference(tmp_path)
    plan = Plan(tmp_path / "stopped")
    state = plan.state()
    state.spec_gen.record_step(
        STEP,
        "blocked",
        "old block",
        blocking_findings="- old finding",
        blocked_revision=state.spec_gen.step_revision(STEP),
    )
    step = state.spec_gen.steps[STEP]
    step.rules_version, step.repair_attempts = 0, 1
    step.blocking_counts = [3]
    state.checkpoint(plan.state_path)
    assert await plan.run() is None
    assert plan.outcome() == reference.outcome()


def _old_audit_steps(plan, outcome):
    raw = json.loads(plan.state_path.read_text())
    raw["spec_gen"]["steps"]["tasks_audit"] = {"revision": "x", "outcome": "passed"}
    raw["spec_gen"]["steps"]["design_audit"] = {"revision": "x", "outcome": outcome}
    plan.state_path.write_text(json.dumps(raw))


@pytest.mark.asyncio
async def test_S10_old_audit_steps_are_dropped_and_a_passed_check_costs_nothing(
    tmp_path,
):
    reference = await _reference(tmp_path)
    _old_audit_steps(reference, "blocked")
    before = reference.check_calls
    assert await reference.run() is None
    assert reference.check_calls == before
    assert (
        set(reference.state().spec_gen.steps) & {"tasks_audit", "design_audit"} == set()
    )


@pytest.mark.asyncio
async def test_S11_an_old_journal_is_finished_then_the_check_runs_fresh(
    tmp_path, monkeypatch
):
    plan = Plan(tmp_path, [FIRST, PlanReviewResult(findings=[])])
    _die_at_live_tasks(plan, monkeypatch)
    assert isinstance(await plan.run(), Killed)
    path = plan.config.change_dir / ".plan-publication.json"
    journal = json.loads(path.read_text())
    journal.pop("plan-edits-1.diff")
    path.write_text(json.dumps(journal))
    raw = json.loads(plan.state_path.read_text())
    raw["spec_gen"]["steps"].pop(STEP)
    plan.state_path.write_text(json.dumps(raw))
    _old_audit_steps(plan, "blocked")
    assert await plan.run() is None
    assert not path.exists()
    assert (plan.config.change_dir / "tasks.md").read_text() == REWRITES[0]
    state = plan.state()
    assert set(state.spec_gen.steps) & {"tasks_audit", "design_audit"} == set()
    assert state.spec_gen.step_passed(STEP)
    assert plan.check_calls == 2


# --- S12, S13: perturbations ---------------------------------------------------


@pytest.mark.asyncio
async def test_S12_an_edit_after_a_block_gets_a_fresh_count(tmp_path):
    plan = Plan(
        tmp_path,
        [
            FIRST,
            _recheck(**{"PR-F1": "still_present", "PR-F2": "still_present"}),
            *CHECKS,
        ],
    )
    assert isinstance(await plan.run(), PlanBlocked)
    plan.writes = ["## 1. Behavior\n- [ ] do it again\n", REWRITES[1]]
    design = plan.config.change_dir / "implementation-plan.md"
    design.write_text(design.read_text() + "Edited by the user.\n")
    assert await plan.run() is None
    step = plan.state().spec_gen.steps[STEP]
    assert (step.outcome, step.repair_attempts, step.blocking_counts) == (
        "passed",
        2,
        [2, 1],
    )


def _git(cwd, *argv):
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_S13_a_moved_origin_changes_nothing(tmp_path):
    reference = await _reference(tmp_path)
    plan = Plan(tmp_path / "stopped", [FIRST, Killed(), RECHECK_1, RECHECK_2])
    repo = plan.config.project_dir
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "one",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert isinstance(await plan.run(), Killed)
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "two",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert await plan.run() is None
    assert plan.outcome() == reference.outcome()
