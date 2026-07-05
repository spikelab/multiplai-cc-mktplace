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
