"""Tests for state management — checkpoint, resume, phase transitions."""

import json
import pytest
from pathlib import Path

from build_pipeline.state import BuildState, TDDState, SpecGenState
from build_pipeline.models import BuildPhase, BlockInfo, BlockStatus


class TestBuildState:
    def test_create_default(self):
        s = BuildState(change_name="test", mode="scratch", tier="advanced")
        assert s.phase == BuildPhase.INIT
        assert not s.bootstrap_done

    def test_checkpoint_and_load(self, tmp_path):
        state_file = tmp_path / "state.json"
        s = BuildState(
            change_name="feat", mode="scratch", tier="advanced",
            state_file=str(state_file),
        )
        s.phase = BuildPhase.RESEARCH
        s.bootstrap_done = True
        s.checkpoint(state_file)

        assert state_file.exists()
        loaded = BuildState.load(state_file)
        assert loaded.phase == BuildPhase.RESEARCH
        assert loaded.bootstrap_done
        assert loaded.change_name == "feat"

    def test_advance_to(self, tmp_path):
        state_file = tmp_path / "state.json"
        s = BuildState(
            change_name="test", mode="scratch", tier="standard",
            state_file=str(state_file),
        )
        s.advance_to(BuildPhase.SPEC_GENERATION, state_file)
        assert s.phase == BuildPhase.SPEC_GENERATION
        assert state_file.exists()

    def test_cleanup(self, tmp_path):
        state_file = tmp_path / "state.json"
        s = BuildState(
            change_name="test", mode="scratch", tier="standard",
            state_file=str(state_file),
        )
        s.checkpoint(state_file)
        assert state_file.exists()
        s.cleanup(state_file)
        assert not state_file.exists()

    def test_is_phase_complete(self):
        s = BuildState(change_name="t", mode="scratch", tier="advanced")
        s.phase = BuildPhase.TDD_BUILD
        assert s.is_phase_complete(BuildPhase.RESEARCH)
        assert s.is_phase_complete(BuildPhase.SPEC_GENERATION)
        assert not s.is_phase_complete(BuildPhase.COMPLETE)


class TestTDDState:
    def test_block_tracking(self, tmp_path):
        state_file = tmp_path / "state.json"
        blocks = [
            BlockInfo(number=1, name="Infra", description="setup"),
            BlockInfo(number=2, name="Engine", description="build"),
        ]
        s = BuildState(
            change_name="test", mode="only", tier="advanced",
            state_file=str(state_file),
            tdd=TDDState(blocks=blocks),
        )
        assert s.current_block().name == "Infra"
        s.mark_block_status(0, BlockStatus.DONE, state_file)
        s.advance_block(state_file)
        assert s.current_block().name == "Engine"
        assert not s.all_blocks_done()

        s.mark_block_status(1, BlockStatus.DONE, state_file)
        assert s.all_blocks_done()

    def test_resume_mid_block(self, tmp_path):
        state_file = tmp_path / "state.json"
        blocks = [
            BlockInfo(number=1, name="A", description="a", status=BlockStatus.DONE),
            BlockInfo(number=2, name="B", description="b", status=BlockStatus.TESTING),
        ]
        s = BuildState(
            change_name="test", mode="only", tier="advanced",
            state_file=str(state_file),
            tdd=TDDState(blocks=blocks, current_block=1),
        )
        s.checkpoint(state_file)

        loaded = BuildState.load(state_file)
        assert loaded.tdd.current_block == 1
        assert loaded.tdd.blocks[1].status == BlockStatus.TESTING


FIXTURES = Path(__file__).parent / "fixtures"


class TestLegacyCheckpointResume:
    """Done-means #7: a .build-state.json written before BuildPhase.PROTOTYPE
    existed must still load and resume at the right phase. The stored phase is
    a name, not an ordinal, so inserting a phase cannot shift it — but
    is_phase_complete compares enum *positions*, so the resume point is what
    actually has to be asserted."""

    def test_pre_prototype_fixture_has_no_prototype_fields(self):
        """Guard the guard: if someone regenerates these fixtures with the
        current code they stop testing backwards compatibility."""
        for name in ("build-state-pre-prototype-design-audit.json",
                     "build-state-pre-prototype-tdd.json"):
            raw = (FIXTURES / name).read_text()
            assert "prototype" not in raw, f"{name} is no longer a pre-change fixture"

    def test_loads_and_resumes_at_design_audit(self):
        s = BuildState.load(FIXTURES / "build-state-pre-prototype-design-audit.json")

        assert s.phase == BuildPhase.DESIGN_AUDIT
        assert s.change_name == "legacy-change"
        # Fields added after the checkpoint was written take their defaults.
        assert s.spec_gen.prototype_done is False
        assert s.spec_gen.tasks_audit_done is True

        # Everything before the design audit stays complete...
        assert s.is_phase_complete(BuildPhase.BOOTSTRAP)
        assert s.is_phase_complete(BuildPhase.RESEARCH)
        assert s.is_phase_complete(BuildPhase.SPEC_GENERATION)
        # ...and the resume point is the design audit, with the new prototype
        # phase still ahead of it rather than silently skipped.
        assert not s.is_phase_complete(BuildPhase.DESIGN_AUDIT)
        assert not s.is_phase_complete(BuildPhase.PROTOTYPE)
        assert not s.is_phase_complete(BuildPhase.REVIEW)
        assert not s.is_phase_complete(BuildPhase.TDD_BUILD)

    def test_loads_and_resumes_at_tdd_build(self):
        s = BuildState.load(FIXTURES / "build-state-pre-prototype-tdd.json")

        assert s.phase == BuildPhase.REVIEW
        assert s.tdd.current_block == 1
        assert s.tdd.blocks[0].status == BlockStatus.DONE

        # A build already past the review checkpoint does not go back for a
        # prototype — its shaping is done.
        assert s.is_phase_complete(BuildPhase.DESIGN_AUDIT)
        assert s.is_phase_complete(BuildPhase.PROTOTYPE)
        assert not s.is_phase_complete(BuildPhase.REVIEW)
        assert not s.is_phase_complete(BuildPhase.TDD_BUILD)

    def test_roundtrip_rewrites_with_the_new_field(self, tmp_path):
        """Loading an old checkpoint and re-checkpointing must not lose data."""
        s = BuildState.load(FIXTURES / "build-state-pre-prototype-design-audit.json")
        out = tmp_path / "state.json"
        s.checkpoint(out)

        reloaded = BuildState.load(out)
        assert reloaded.phase == BuildPhase.DESIGN_AUDIT
        assert reloaded.spec_gen.completed_artifacts == s.spec_gen.completed_artifacts
        assert json.loads(out.read_text())["spec_gen"]["prototype_done"] is False
