"""The size gate parks a build only on the plan check's `oversized-plan` finding.

plan-buildme-split-gate-model-verdict-2026-10-31.md, items 4(a)-(g) and rows
S1-S4. The plan check's model call is a fake (`run_plan_review`); the stage
that saves its answer and the gate that reads it are real.

`plan-split-probe-tasks.md` is the dolcecleaning-users plan that parked on
2026-10-30: user management is the feature, so the auth keywords matched 7 of
its 9 blocks, and its `Consumes:` lines start with `Block N:`.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.change_manager import ARTIFACT_DAG, ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps import plan_review_steps
from build_pipeline.models import (
    BuildPhase,
    PlanReviewCut,
    PlanReviewFinding,
    PlanReviewResult,
)
from build_pipeline.orchestrator import _run_size_gate
from build_pipeline.plan_split import partition_blocks
from build_pipeline.progress import ProgressWriter
from build_pipeline.spec_generator import reconcile_plan
from build_pipeline.state import BuildState, SpecGenState
from build_pipeline.tdd_engine import parse_blocks

from tests.spec_fixtures import write_specs

FIXTURES = Path(__file__).parent / "fixtures"
PROBE = FIXTURES / "plan-split-probe-tasks.md"
MIGRATION = FIXTURES / "plan-split-migration-tasks.md"


def _oversized(severity: str) -> PlanReviewFinding:
    return PlanReviewFinding(
        category="oversized-plan",
        severity=severity,
        location="Block 1",
        claim="The migration should land on its own branch.",
        proposed_cut=[
            PlanReviewCut(ticket="reports-table-migration", blocks=[1]),
            PlanReviewCut(ticket="settings-colour-picker", blocks=[2]),
        ],
    )


def _setup(tmp_path: Path, tasks: Path):
    config = BuildConfig(
        project_dir=tmp_path, change_name="sized", auto=True, spec_only=True
    )
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    cm.create_change("sized")
    root = config.change_dir
    write_specs(root)
    for name, text in {
        "design": "# Design",
        "unknowns": "# Unknowns",
        "rubric": "# Rubric",
    }.items():
        (root / ARTIFACT_DAG[name]["generates"]).write_text(text)
    shutil.copy(tasks, root / "tasks.md")
    state_path = config.state_file_path()
    state = BuildState(
        change_name="sized",
        tier="advanced",
        phase=BuildPhase.PLAN_REVIEW,
        state_file=str(state_path),
        spec_gen=SpecGenState(),
    )
    reconcile_plan(config, state, regenerated=set(ARTIFACT_DAG))
    state.checkpoint(state_path)
    return config, state, state_path


async def _review(config, state, state_path, findings):
    """Run the real plan check stage with the model's answer *findings*."""
    call = AsyncMock(return_value=PlanReviewResult(findings=findings))
    with (
        patch.object(plan_review_steps, "run_plan_review", call),
        patch.object(plan_review_steps, "_scan_findings", lambda change_dir: []),
    ):
        await plan_review_steps.run_plan_review_stage(
            config.change_dir, config, state, state_path
        )
    return call


def _gate(config, state, state_path, *, accept_size=False):
    progress = ProgressWriter(config.progress_file_path())
    return _run_size_gate(config, state, state_path, progress, accept_size=accept_size)


def _parked(config, rc, out) -> bool:
    proposal = config.change_dir / "split-proposal.md"
    if rc is None:
        assert "SPLIT:REQUIRED" not in out
        assert not proposal.exists()
        return False
    assert rc == 3
    assert "SPLIT:REQUIRED:" in out
    assert proposal.exists()
    return True


# --- 4(a)-(e): what the gate decides ------------------------------------------


@pytest.mark.asyncio
async def test_size_gate_lets_the_probe_plan_through_when_the_reviewer_finds_no_oversized_plan(
    tmp_path, capsys
):
    """4(a): the words in the blocks no longer park a plan the reviewer passed."""
    config, state, state_path = _setup(tmp_path, PROBE)
    await _review(config, state, state_path, [])
    rc = _gate(config, state, state_path)
    assert not _parked(config, rc, capsys.readouterr().out)
    assert state.split_required is False


@pytest.mark.asyncio
async def test_size_gate_parks_on_a_major_oversized_plan_with_the_reviewers_cut(
    tmp_path, capsys
):
    """4(b)."""
    config, state, state_path = _setup(tmp_path, MIGRATION)
    await _review(config, state, state_path, [_oversized("major")])
    rc = _gate(config, state, state_path)
    assert _parked(config, rc, capsys.readouterr().out)
    proposal = (config.change_dir / "split-proposal.md").read_text()
    assert "reports-table-migration" in proposal
    assert "settings-colour-picker" in proposal
    assert BuildState.load(state_path).split_required is True


@pytest.mark.asyncio
async def test_size_gate_parks_on_a_critical_oversized_plan(tmp_path, capsys):
    config, state, state_path = _setup(tmp_path, PROBE)
    await _review(config, state, state_path, [_oversized("critical")])
    rc = _gate(config, state, state_path)
    assert _parked(config, rc, capsys.readouterr().out)


@pytest.mark.asyncio
async def test_size_gate_does_not_park_on_a_minor_oversized_plan(tmp_path, capsys):
    """4(c): the migration fixture parks on the code's verdict; a minor finding
    from the reviewer does not park it. (`note` is not a severity the finding
    model accepts: it is rejected and the call retried.)"""
    config, state, state_path = _setup(tmp_path, MIGRATION)
    await _review(config, state, state_path, [_oversized("minor")])
    rc = _gate(config, state, state_path)
    assert not _parked(config, rc, capsys.readouterr().out)


def test_size_gate_falls_back_to_the_code_verdict_with_no_saved_review(
    tmp_path, capsys, caplog
):
    """4(d): no plan check recorded (it never ran, or its call failed and
    left the step blocked): the code's verdict decides, and the log says so."""
    config, state, state_path = _setup(tmp_path, MIGRATION)
    with caplog.at_level(logging.INFO, logger="build_pipeline.orchestrator"):
        rc = _gate(config, state, state_path)
    assert _parked(config, rc, capsys.readouterr().out)
    assert "SIZE_GATE verdict=code-fallback" in caplog.text


@pytest.mark.asyncio
async def test_size_gate_falls_back_when_the_review_call_failed(
    tmp_path, capsys, caplog
):
    """4(d): a failed model call blocks the step; it saves no verdict."""
    config, state, state_path = _setup(tmp_path, MIGRATION)
    call = AsyncMock(side_effect=RuntimeError("model unavailable"))
    with (
        patch.object(plan_review_steps, "run_plan_review", call),
        pytest.raises(RuntimeError),
    ):
        await plan_review_steps.run_plan_review_stage(
            config.change_dir, config, state, state_path
        )
    with caplog.at_level(logging.INFO, logger="build_pipeline.orchestrator"):
        rc = _gate(config, state, state_path)
    assert _parked(config, rc, capsys.readouterr().out)
    assert "SIZE_GATE verdict=code-fallback" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [False, True])
async def test_size_gate_accepted_size_skips_a_major_oversized_plan(
    tmp_path, capsys, saved
):
    """4(e): `--accept-size`, and an acceptance saved by an earlier run."""
    config, state, state_path = _setup(tmp_path, MIGRATION)
    await _review(config, state, state_path, [_oversized("major")])
    state.size_accepted = saved
    rc = _gate(config, state, state_path, accept_size=not saved)
    assert not _parked(config, rc, capsys.readouterr().out)
    assert BuildState.load(state_path).size_accepted is True


# --- 4(f): the grouping ---------------------------------------------------------


def test_partition_joins_blocks_whose_consumes_name_another_blocks_produces():
    """Block 2 consumes block 1's `_provider_account`; block 3 consumes
    block 2's `TeamClient` and `AccountTeam`. Each `Consumes:` line starts with
    `Block N:`, and block 1's `Produces:` line goes on past the signature."""
    split = partition_blocks(parse_blocks(PROBE))
    together = [g.block_numbers for g in split.groups if 1 in g.block_numbers]
    assert len(together) == 1
    assert {1, 2, 3} <= set(together[0])


def test_partition_puts_the_whole_probe_plan_in_one_group():
    split = partition_blocks(parse_blocks(PROBE))
    assert [g.block_numbers for g in split.groups] == [list(range(1, 10))]


def test_partition_does_not_join_two_endpoints_by_their_path(tmp_path):
    """`POST api/a` and `POST api/b` define no identifier; `api` is a path."""
    text = (
        "## 1. First endpoint\n"
        "Serve the first endpoint.\n"
        "- [ ] 1.1 Do it\n"
        "Interfaces:\n"
        "- Produces: `POST api/first` → `{}`\n"
        "\n"
        "## 2. Second endpoint\n"
        "Serve the second endpoint.\n"
        "- [ ] 2.1 Do it\n"
        "Interfaces:\n"
        "- Produces: `POST api/second` → `{}`\n"
    )
    path = tmp_path / "tasks.md"
    path.write_text(text)
    split = partition_blocks(parse_blocks(path))
    assert [g.block_numbers for g in split.groups] == [[1], [2]]


# --- Stop and resume rows S1-S4 -------------------------------------------------


@pytest.mark.asyncio
async def test_s1_resume_after_the_review_uses_the_saved_finding(tmp_path, capsys):
    """S1, and the new field's round trip: a resume loads the checkpoint,
    skips the plan check (no model call) and parks on the saved finding."""
    config, state, state_path = _setup(tmp_path, PROBE)
    await _review(config, state, state_path, [_oversized("major")])

    resumed = BuildState.load(state_path)
    saved = resumed.spec_gen.steps["plan_review"].size_verdict
    assert saved is not None
    assert saved.oversized == _oversized("major")
    call = await _review(config, resumed, state_path, [])
    call.assert_not_called()
    rc = _gate(config, resumed, state_path)
    assert _parked(config, rc, capsys.readouterr().out)
    assert (
        "reports-table-migration"
        in (config.change_dir / "split-proposal.md").read_text()
    )


@pytest.mark.asyncio
async def test_s1_saved_review_with_no_finding_round_trips(tmp_path):
    config, state, state_path = _setup(tmp_path, PROBE)
    await _review(config, state, state_path, [])
    resumed = BuildState.load(state_path)
    saved = resumed.spec_gen.steps["plan_review"].size_verdict
    assert saved is not None and saved.oversized is None
    assert _gate(config, resumed, state_path) is None


@pytest.mark.asyncio
async def test_s2_a_parked_build_parks_again_without_a_model_call(tmp_path, capsys):
    config, state, state_path = _setup(tmp_path, MIGRATION)
    await _review(config, state, state_path, [_oversized("major")])
    assert _gate(config, state, state_path) == 3
    capsys.readouterr()

    resumed = BuildState.load(state_path)
    call = await _review(config, resumed, state_path, [])
    call.assert_not_called()
    rc = _gate(config, resumed, state_path)
    assert _parked(config, rc, capsys.readouterr().out)


@pytest.mark.asyncio
async def test_s3_checkpoint_without_the_field_uses_the_code_verdict(
    tmp_path, capsys, caplog
):
    """S3: a checkpoint written by a version without `size_verdict` loads,
    and the gate falls back to the code's verdict."""
    config, state, state_path = _setup(tmp_path, MIGRATION)
    await _review(config, state, state_path, [])
    raw = json.loads(state_path.read_text())
    del raw["spec_gen"]["steps"]["plan_review"]["size_verdict"]
    state_path.write_text(json.dumps(raw))

    old = BuildState.load(state_path)
    assert old.spec_gen.step_passed("plan_review")
    assert old.spec_gen.steps["plan_review"].size_verdict is None
    with caplog.at_level(logging.INFO, logger="build_pipeline.orchestrator"):
        rc = _gate(config, old, state_path)
    assert _parked(config, rc, capsys.readouterr().out)
    assert "SIZE_GATE verdict=code-fallback" in caplog.text


@pytest.mark.asyncio
async def test_s4_an_edited_plan_is_reviewed_again_and_the_gate_reads_the_new_answer(
    tmp_path, capsys
):
    config, state, state_path = _setup(tmp_path, MIGRATION)
    await _review(config, state, state_path, [])
    assert _gate(config, state, state_path) is None

    tasks = config.change_dir / "tasks.md"
    tasks.write_text(tasks.read_text() + "\n- [ ] 2.2 Remember the colour\n")
    call = await _review(config, state, state_path, [_oversized("major")])
    call.assert_called_once()
    rc = _gate(config, state, state_path)
    assert _parked(config, rc, capsys.readouterr().out)
