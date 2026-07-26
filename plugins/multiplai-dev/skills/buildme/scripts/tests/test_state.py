"""Tests for state management — checkpoint, resume, phase transitions."""

import json
import pytest
from pathlib import Path

from build_pipeline import budget
from build_pipeline.state import BuildState, TDDState, SpecGenState
from build_pipeline.models import BuildPhase, BlockInfo, BlockStatus, ImplementationNote


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


class TestBudgetSnapshot:
    """Spend rides along with the state file, so resume cannot reset it."""

    class _Usage:
        input_tokens, output_tokens = 300, 100
        cache_read_tokens = cache_creation_tokens = 0
        cost_usd = 1.25

    def test_checkpoint_snapshots_the_live_budget(self, tmp_path):
        budget.reset()
        budget.record(self._Usage(), label="review")
        state = BuildState(change_name="c", mode="scratch", tier="standard")
        path = tmp_path / "state.json"
        state.checkpoint(path)
        budget.reset()

        reloaded = BuildState.load(path)
        assert reloaded.budget["total_tokens"] == 400
        assert reloaded.budget["by_label"] == {"review": 400}

    def test_absent_budget_defaults_to_empty(self):
        budget.reset()
        assert BuildState(change_name="c", mode="scratch", tier="standard").budget == {}
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


class TestRespecPhaseAndNotes:
    """BuildPhase.RESPEC sits between TDD_BUILD and COMPLETE, and the notes an
    agent reported survive a checkpoint/resume round trip."""

    def test_respec_is_ordered_after_tdd_build_and_before_complete(self):
        order = list(BuildPhase)
        assert order.index(BuildPhase.TDD_BUILD) < order.index(BuildPhase.RESPEC)
        assert order.index(BuildPhase.RESPEC) < order.index(BuildPhase.COMPLETE)

    def test_tdd_build_does_not_count_respec_as_complete(self):
        s = BuildState(change_name="t", mode="only", tier="advanced")
        s.phase = BuildPhase.TDD_BUILD
        assert s.is_phase_complete(BuildPhase.REVIEW)
        assert not s.is_phase_complete(BuildPhase.RESPEC)

    def test_completed_build_skips_respec_on_resume(self):
        s = BuildState(change_name="t", mode="only", tier="advanced")
        s.phase = BuildPhase.COMPLETE
        assert s.is_phase_complete(BuildPhase.RESPEC)

    def test_pre_respec_checkpoint_still_loads_and_resumes(self, tmp_path):
        """A .build-state.json written before BuildPhase.RESPEC existed (no
        `notes` on blocks, phase from the old enum) loads and resumes."""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "change_name": "legacy",
            "mode": "scratch",
            "tier": "advanced",
            "phase": "tdd_build",
            "bootstrap_done": True,
            "state_file": str(state_file),
            "tdd": {
                "blocks": [{
                    "number": 1, "name": "A", "description": "a", "status": "done",
                }],
                "current_block": 0,
            },
        }))

        loaded = BuildState.load(state_file)
        assert loaded.phase == BuildPhase.TDD_BUILD
        assert loaded.tdd.blocks[0].notes == []
        assert loaded.is_phase_complete(BuildPhase.REVIEW)
        assert not loaded.is_phase_complete(BuildPhase.RESPEC)

    def test_block_notes_survive_checkpoint_and_reload(self, tmp_path):
        state_file = tmp_path / "state.json"
        block = BlockInfo(number=2, name="Uploader", description="u")
        block.notes.append(ImplementationNote(
            block_number=2, block_name="Uploader", role="implementer",
            surprises="The client raises on timeout.", spec_impact="contradicts",
        ))
        s = BuildState(
            change_name="t", mode="only", tier="advanced",
            state_file=str(state_file), tdd=TDDState(blocks=[block]),
        )
        s.checkpoint(state_file)

        loaded = BuildState.load(state_file)
        note = loaded.tdd.blocks[0].notes[0]
        assert note.spec_impact == "contradicts"
        assert note.contradicts
        assert note.role == "implementer"
        assert "raises on timeout" in note.surprises


# --- Git lifecycle state (work item 4) -----------------------------------

class TestGitLifecycleState:
    def test_new_fields_default_to_none(self):
        from build_pipeline.state import BuildState

        state = BuildState(change_name="c", mode="scratch", tier="advanced")
        assert state.worktree_path is None
        assert state.branch is None
        assert state.source_repo is None
        assert state.pr_url is None

    def test_fields_round_trip_through_a_checkpoint(self, tmp_path):
        from build_pipeline.state import BuildState

        path = tmp_path / ".build-state.json"
        state = BuildState(
            change_name="c", mode="scratch", tier="advanced",
            worktree_path="/ws/.worktrees/buildme-c",
            branch="buildme/c",
            source_repo="/proj",
            pr_url="https://github.com/o/r/pull/1",
        )
        state.checkpoint(path)
        reloaded = BuildState.load(path)
        assert reloaded.worktree_path == "/ws/.worktrees/buildme-c"
        assert reloaded.branch == "buildme/c"
        assert reloaded.source_repo == "/proj"
        assert reloaded.pr_url == "https://github.com/o/r/pull/1"

    def test_pre_publish_checkpoint_still_loads_and_resumes(self, tmp_path):
        """A .build-state.json written before BuildPhase.PUBLISH existed (and
        before the git fields) must load and resume at the right phase."""
        import json
        from build_pipeline.models import BuildPhase
        from build_pipeline.state import BuildState

        legacy = {
            "change_name": "legacy",
            "mode": "scratch",
            "tier": "advanced",
            "phase": "tdd_build",
            "bootstrap_done": True,
            "interview_summary": "old run",
            "research_path": None,
            "spec_gen": {"completed_artifacts": ["proposal"], "research_path": None,
                         "codebase_analysis_path": None},
            "tdd": None,
            "state_file": "",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        path = tmp_path / ".build-state.json"
        path.write_text(json.dumps(legacy))

        state = BuildState.load(path)
        assert state.phase == BuildPhase.TDD_BUILD
        assert state.worktree_path is None and state.branch is None
        # Everything before TDD_BUILD is complete; TDD_BUILD itself is not.
        assert state.is_phase_complete(BuildPhase.REVIEW)
        assert not state.is_phase_complete(BuildPhase.TDD_BUILD)
        assert not state.is_phase_complete(BuildPhase.PUBLISH)

    def test_publish_sits_immediately_before_complete(self):
        from build_pipeline.models import BuildPhase

        order = [p.value for p in BuildPhase]
        assert order.index("tdd_build") < order.index("publish")
        assert order.index("publish") + 1 == order.index("complete")
