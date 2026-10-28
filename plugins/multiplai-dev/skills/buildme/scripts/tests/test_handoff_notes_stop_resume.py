"""Stop and resume rows S1-S8 of plan-buildme-carry-block-notes-2026-10-27.md
for `BlockInfo.handoff_notes`.

Each case puts the checkpoint where the row's stop leaves it (written with
`BuildState.checkpoint` and read back with `BuildState.load`, as a new process
would), resumes, and compares `handoff_notes` and the block's status with a
run that never stopped. A stop here is the checkpoint on disk, not a killed
process: the note is saved only by `state.checkpoint`, so the file is the
whole of what a killed run leaves of it. S9 and S10 are unchanged by this
plan and stay with their own suites.
"""

from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import git_ops, models
from build_pipeline.config import BuildConfig
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    GateResult,
    HandoffNote,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    HANDOFF_HEADING,
    _record_handoff_note,
    assemble_context,
    run_block_tdd,
)

RED = (1, "FAILED tests/test_x.py::test_a - AssertionError: not implemented")


def _report(text: str) -> str:
    return f"NOTES_FOR_LATER_BLOCKS: {text}\nSTATUS: DONE\nSURPRISES: none\n"


@pytest.fixture(autouse=True)
def _agents_without_files(monkeypatch):
    """The doubles return reports and write nothing, as in
    test_tdd_engine.py's `_unit_agent_evidence`."""
    monkeypatch.setattr(git_ops, "commit_tree", lambda *a, **kw: None)
    monkeypatch.setattr(
        "build_pipeline.tdd_engine._list_block_test_files",
        lambda *a: ["tests/test_x.py"],
    )
    monkeypatch.setattr(
        "build_pipeline.tdd_engine._snapshot_test_files",
        lambda *a: {"tests/test_x.py": "unchanged"},
    )
    monkeypatch.setattr(
        "build_pipeline.tdd_engine.integration_gate",
        lambda *a, **kw: GateResult(passed=True, reason="unit suite"),
    )
    with (
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED),
        patch(
            "build_pipeline.tdd_engine._audit_test_quality",
            new_callable=AsyncMock,
            return_value=models.TestQualityAudit(passed=True),
        ),
        patch(
            "build_pipeline.tdd_engine.run_refactorer",
            new_callable=AsyncMock,
            return_value=AgentResult(success=True, output="nothing to refactor"),
        ),
    ):
        yield


def _setup(tmp_path, carry=True):
    project = tmp_path / "project"
    change = project / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (change / "implementation-plan.md").write_text("# Design")
    config = BuildConfig(
        project_dir=project,
        change_name="test",
        tier="advanced",
        test_command="pytest -xvs",
        config_dir=tmp_path / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
        carry_block_notes=carry,
    )
    config.specs_dir = project / "specs"
    state = BuildState(
        change_name="test",
        tier="advanced",
        tdd=TDDState(
            blocks=[
                BlockInfo(number=1, name="One", description="first"),
                BlockInfo(number=2, name="Two", description="second"),
            ]
        ),
    )
    progress = ProgressWriter(tmp_path / "progress.md")
    progress.initialize("test", "advanced", 2)
    return config, state, progress


async def _run_block(config, state, progress, idx, impl_text):
    state.tdd.current_block = idx
    with (
        patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=AgentResult(success=True, output="Tests written"),
        ),
        patch(
            "build_pipeline.tdd_engine.run_implementer",
            new_callable=AsyncMock,
            return_value=AgentResult(success=True, output=_report(impl_text)),
        ) as impl,
    ):
        ok = await run_block_tdd(state.tdd.blocks[idx], config, state, progress)
    return ok, impl


def _reload(config) -> BuildState:
    return BuildState.load(config.state_file_path())


def _notes(state):
    return [
        [(n.role, n.round, n.text) for n in b.handoff_notes] for b in state.tdd.blocks
    ]


async def _never_stopped(tmp_path):
    config, state, progress = _setup(tmp_path / "clean")
    assert (await _run_block(config, state, progress, 0, "ids are strings"))[0]
    return _notes(state), state.tdd.blocks[0].status


@pytest.mark.asyncio
async def test_s1_note_checkpointed_then_the_implementer_runs_again(tmp_path):
    """S1: stopped after the note's checkpoint, before the impl commit. The
    re-run implementer's note replaces the saved one; no duplicate."""
    expected = await _never_stopped(tmp_path)
    config, state, progress = _setup(tmp_path / "s1")
    block = state.tdd.blocks[0]
    _record_handoff_note(block, config, state, "implementer", 0, _report("stale guess"))
    resumed = _reload(config)
    resumed.tdd.blocks[0].status = BlockStatus.PENDING
    assert (await _run_block(config, resumed, progress, 0, "ids are strings"))[0]
    assert (_notes(resumed), resumed.tdd.blocks[0].status) == expected


@pytest.mark.asyncio
async def test_s2_stopped_before_the_note_was_checkpointed(tmp_path):
    """S2: the implementer returned, the checkpoint was never written. The
    file holds no note; the re-run writes a fresh one."""
    expected = await _never_stopped(tmp_path)
    config, state, progress = _setup(tmp_path / "s2")
    state.checkpoint(config.state_file_path())
    resumed = _reload(config)
    assert _notes(resumed) == [[], []]
    assert (await _run_block(config, resumed, progress, 0, "ids are strings"))[0]
    assert (_notes(resumed), resumed.tdd.blocks[0].status) == expected


def test_s3_review_fix_note_re_run_replaces_its_round(tmp_path):
    """S3: a review fix's note was checkpointed before its commit; the re-run
    fix of the same round replaces it, and the implementer's note stays."""
    config, state, _ = _setup(tmp_path)
    block = state.tdd.blocks[0]
    _record_handoff_note(block, config, state, "implementer", 0, _report("a"))
    _record_handoff_note(block, config, state, "review-fix", 1, _report("first try"))
    resumed = _reload(config)
    rblock = resumed.tdd.blocks[0]
    _record_handoff_note(
        rblock, config, resumed, "review-fix", 1, _report("second try")
    )
    assert _notes(_reload(config))[0] == [
        ("implementer", 0, "a"),
        ("review-fix", 1, "second try"),
    ]


def test_s4_the_same_stop_twice_keeps_one_note_per_key(tmp_path):
    """S4: stopped twice at the same point: still one entry per (role, round)."""
    config, state, _ = _setup(tmp_path)
    for _ in range(3):
        state = _reload(config) if config.state_file_path().exists() else state
        _record_handoff_note(
            state.tdd.blocks[0], config, state, "implementer", 0, _report("x")
        )
    assert _notes(_reload(config))[0] == [("implementer", 0, "x")]


def test_s5_a_checkpoint_from_before_the_field(tmp_path):
    """S5: an old checkpoint loads with no notes; a later block gets notes only
    from blocks built after the upgrade."""
    import json

    config, state, _ = _setup(tmp_path)
    state.checkpoint(config.state_file_path())
    data = json.loads(config.state_file_path().read_text())
    for b in data["tdd"]["blocks"]:
        b.pop("handoff_notes", None)
    config.state_file_path().write_text(json.dumps(data))
    resumed = _reload(config)
    assert _notes(resumed) == [[], []]
    blocks = resumed.tdd.blocks
    assert HANDOFF_HEADING not in assemble_context(
        blocks[1], config, "implementer", blocks=blocks
    )


@pytest.mark.asyncio
async def test_s6_flag_turned_off_between_stop_and_resume(tmp_path):
    """S6: on -> off. Saved notes stay in state, reach no context, and the
    implementer is not asked."""
    config, state, progress = _setup(tmp_path)
    assert (await _run_block(config, state, progress, 0, "ids are strings"))[0]
    config.carry_block_notes = False
    resumed = _reload(config)
    blocks = resumed.tdd.blocks
    assert HANDOFF_HEADING not in assemble_context(
        blocks[1], config, "implementer", blocks=blocks
    )
    ok, impl = await _run_block(config, resumed, progress, 1, "unused")
    assert ok
    assert impl.await_args.kwargs["carry_notes"] is False
    assert _notes(resumed) == [[("implementer", 0, "ids are strings")], []]


@pytest.mark.asyncio
async def test_s7_flag_turned_on_between_stop_and_resume(tmp_path):
    """S7: off -> on. Block 1 was built without notes; block 2 is asked, and a
    block 3 would see only block 2's."""
    config, state, progress = _setup(tmp_path, carry=False)
    assert (await _run_block(config, state, progress, 0, "unused"))[0]
    config.carry_block_notes = True
    resumed = _reload(config)
    ok, impl = await _run_block(config, resumed, progress, 1, "money is cents")
    assert ok
    assert impl.await_args.kwargs["carry_notes"] is True
    assert _notes(resumed) == [[], [("implementer", 0, "money is cents")]]
    third = BlockInfo(number=3, name="Three", description="third")
    blocks = [*resumed.tdd.blocks, third]
    section = assemble_context(third, config, "implementer", blocks=blocks).split(
        HANDOFF_HEADING, 1
    )[1]
    assert "Block 2: Two" in section
    assert "Block 1: One" not in section


def test_s8_user_edits_between_stop_and_resume(tmp_path):
    """S8: nothing extra is done; the section tells agents to check the notes
    against the code."""
    config, state, _ = _setup(tmp_path)
    state.tdd.blocks[0].handoff_notes = [
        HandoffNote(role="implementer", round=0, text="Repo.get raises NotFound")
    ]
    blocks = state.tdd.blocks
    context = assemble_context(blocks[1], config, "implementer", blocks=blocks)
    assert "check them against the code before relying on them" in context
    assert "Repo.get raises NotFound" in context
