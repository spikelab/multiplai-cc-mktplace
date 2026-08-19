"""Tests for state management — checkpoint, resume, phase transitions."""

import json
import os
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


class TestAtomicCheckpoint:
    """The checkpoint is the only crash-recovery record, so a half-written one
    is unrecoverable: temp file in the same directory + os.replace."""

    def _state(self, state_file):
        return BuildState(
            change_name="atomic", mode="scratch", tier="advanced",
            state_file=str(state_file),
        )

    def test_writes_through_a_same_directory_temp_then_replaces(self, tmp_path, monkeypatch):
        state_file = tmp_path / "sub" / "state.json"
        seen = {}
        real_replace = os.replace

        def spy_replace(src, dst):
            seen["src"] = Path(src)
            seen["dst"] = Path(dst)
            # The temp file must be a sibling of the target, or the rename is
            # a cross-filesystem move and stops being atomic.
            assert Path(src).parent == Path(dst).parent
            assert Path(src).exists()
            return real_replace(src, dst)

        monkeypatch.setattr("build_pipeline.state.os.replace", spy_replace)
        self._state(state_file).checkpoint(state_file)

        assert seen["dst"] == state_file
        assert seen["src"] != state_file
        assert json.loads(state_file.read_text())["change_name"] == "atomic"

    def test_serialization_failure_leaves_no_partial_and_no_temp(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        s = self._state(state_file)
        s.checkpoint(state_file)
        good = state_file.read_text()

        s.phase = BuildPhase.TDD_BUILD
        monkeypatch.setattr(
            BuildState, "model_dump_json",
            lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError):
            s.checkpoint(state_file)

        # Previous checkpoint intact, still loadable, and no scratch left behind.
        assert state_file.read_text() == good
        assert BuildState.load(state_file).phase == BuildPhase.INIT
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_crashed_write_never_leaves_a_truncated_checkpoint(self, tmp_path, monkeypatch):
        """os.replace dying mid-checkpoint is the crash we cannot recover from
        if the write were in place — the old checkpoint must still parse."""
        state_file = tmp_path / "state.json"
        s = self._state(state_file)
        s.checkpoint(state_file)

        s.phase = BuildPhase.PUBLISH
        monkeypatch.setattr(
            "build_pipeline.state.os.replace",
            lambda src, dst: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(OSError):
            s.checkpoint(state_file)

        assert BuildState.load(state_file).phase == BuildPhase.INIT
        assert list(tmp_path.glob("*.tmp")) == []


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


class TestPreCodebaseAnalysisCheckpointResume:
    """A .build-state.json written before BuildPhase.CODEBASE_ANALYSIS existed
    must still load, and the inserted phase must land where it runs — ahead of
    a checkpoint parked at RESEARCH, behind one already into spec generation.
    The stored phase is a name, not an ordinal, so the insertion cannot shift
    it; is_phase_complete compares enum *positions*, which is what has to be
    asserted."""

    FIXTURE = "build-state-pre-codebase-analysis.json"

    def test_fixture_predates_the_change(self):
        """Guard the guard: regenerating this fixture with current code would
        stop it testing backwards compatibility."""
        raw = (FIXTURES / self.FIXTURE).read_text()
        for token in ("codebase_analysis_path", "prototype_done", "explainers_done", "budget"):
            assert token not in raw, f"{self.FIXTURE} is no longer a pre-change fixture"

    def test_loads_and_resumes_with_codebase_analysis_still_ahead(self):
        s = BuildState.load(FIXTURES / self.FIXTURE)

        assert s.phase == BuildPhase.RESEARCH
        assert s.change_name == "legacy-research"
        # The field the new phase writes takes its default on an old checkpoint.
        assert s.spec_gen.codebase_analysis_path is None

        assert s.is_phase_complete(BuildPhase.BOOTSTRAP)
        assert s.is_phase_complete(BuildPhase.INTERVIEW_DONE)
        # The resume point is research, with the new phase after it rather
        # than silently skipped...
        assert not s.is_phase_complete(BuildPhase.RESEARCH)
        assert not s.is_phase_complete(BuildPhase.CODEBASE_ANALYSIS)
        assert not s.is_phase_complete(BuildPhase.SPEC_GENERATION)

    def test_codebase_analysis_is_ordered_between_research_and_spec_generation(self):
        order = list(BuildPhase)
        assert order.index(BuildPhase.RESEARCH) < order.index(BuildPhase.CODEBASE_ANALYSIS)
        assert order.index(BuildPhase.CODEBASE_ANALYSIS) < order.index(BuildPhase.SPEC_GENERATION)

    def test_a_checkpoint_past_research_does_not_rewind_for_analysis(self):
        """...and an old checkpoint already at spec generation is not sent
        back to analyze the codebase — its shaping is done."""
        s = BuildState.load(FIXTURES / self.FIXTURE)
        s.phase = BuildPhase.SPEC_GENERATION
        assert s.is_phase_complete(BuildPhase.CODEBASE_ANALYSIS)

    def test_roundtrip_rewrites_with_the_new_field(self, tmp_path):
        s = BuildState.load(FIXTURES / self.FIXTURE)
        out = tmp_path / "state.json"
        s.checkpoint(out)

        reloaded = BuildState.load(out)
        assert reloaded.phase == BuildPhase.RESEARCH
        assert reloaded.interview_summary == s.interview_summary
        assert json.loads(out.read_text())["spec_gen"]["codebase_analysis_path"] is None


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

    def test_pre_refactor_all_fixture_has_no_refactor_all_field(self):
        """Guard the guard: regenerating this fixture with current code would
        stop it testing backwards compatibility."""
        raw = (FIXTURES / "build-state-pre-refactor-all.json").read_text()
        assert "refactor_all" not in raw

    def test_pre_refactor_all_checkpoint_resumes_at_the_refactor_pass(self):
        """A checkpoint written before the whole-change refactor existed: every
        block is done, the final review has not run, and the new flag defaults
        to False — so the resume runs the pass rather than skipping it."""
        s = BuildState.load(FIXTURES / "build-state-pre-refactor-all.json")

        assert s.phase == BuildPhase.TDD_BUILD
        assert s.tdd.refactor_all_done is False
        assert s.tdd.final_review_done is False
        assert [b.status for b in s.tdd.blocks] == [BlockStatus.DONE, BlockStatus.DONE]
        assert s.tdd.current_block == len(s.tdd.blocks)
        # Fields added after the checkpoint was written take their defaults.
        assert all(b.refactor_commit is None for b in s.tdd.blocks)

    def test_refactor_all_done_survives_checkpoint_and_reload(self, tmp_path):
        s = BuildState.load(FIXTURES / "build-state-pre-refactor-all.json")
        out = tmp_path / "state.json"
        s.tdd.refactor_all_done = True
        s.checkpoint(out)

        assert BuildState.load(out).tdd.refactor_all_done is True
        assert json.loads(out.read_text())["tdd"]["refactor_all_done"] is True

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


class TestDocsUpdatePhaseAndImpact:
    """BuildPhase.DOCS_UPDATE sits between TDD_BUILD and RESPEC, and a
    checkpoint written before it existed still loads and resumes into it."""

    def test_docs_update_is_ordered_between_tdd_build_and_respec(self):
        order = list(BuildPhase)
        assert order.index(BuildPhase.TDD_BUILD) < order.index(BuildPhase.DOCS_UPDATE)
        assert order.index(BuildPhase.DOCS_UPDATE) < order.index(BuildPhase.RESPEC)

    def test_pre_docs_update_fixture_has_no_docs_fields(self):
        """Guard the guard: regenerating this fixture with the current code
        would stop it testing backwards compatibility."""
        raw = (FIXTURES / "build-state-pre-docs-update.json").read_text()
        assert "docs_update" not in raw
        assert "docs_impact" not in raw

    def test_legacy_checkpoint_loads_and_resumes_at_docs_update(self):
        s = BuildState.load(FIXTURES / "build-state-pre-docs-update.json")

        assert s.phase == BuildPhase.TDD_BUILD
        assert s.change_name == "legacy-change"
        # A field added after the checkpoint was written takes its default.
        assert s.docs_impact == []
        # The TDD build it recorded stays complete...
        assert s.is_phase_complete(BuildPhase.REVIEW)
        assert not s.is_phase_complete(BuildPhase.TDD_BUILD)
        # ...and the new phase is ahead of it rather than silently skipped.
        assert not s.is_phase_complete(BuildPhase.DOCS_UPDATE)
        assert not s.is_phase_complete(BuildPhase.RESPEC)

    def test_legacy_checkpoint_past_respec_does_not_go_back_for_docs(self):
        """A build already past RESPEC has its documentation window behind it;
        inserting a phase must not send it backwards."""
        s = BuildState.load(FIXTURES / "build-state-pre-docs-update.json")
        s.phase = BuildPhase.RESPEC
        assert s.is_phase_complete(BuildPhase.DOCS_UPDATE)

    def test_legacy_roundtrip_rewrites_with_the_new_field(self, tmp_path):
        s = BuildState.load(FIXTURES / "build-state-pre-docs-update.json")
        out = tmp_path / "state.json"
        s.checkpoint(out)

        assert json.loads(out.read_text())["docs_impact"] == []
        assert BuildState.load(out).phase == BuildPhase.TDD_BUILD

    def test_docs_impact_round_trips_through_a_checkpoint(self, tmp_path):
        path = tmp_path / ".build-state.json"
        s = BuildState(
            change_name="c", mode="only", tier="advanced",
            docs_impact=["README.md", "CHANGELOG.md"],
        )
        s.checkpoint(path)
        assert BuildState.load(path).docs_impact == ["README.md", "CHANGELOG.md"]


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


class TestPrePlanReviewCheckpointResume:
    """A .build-state.json written before BuildPhase.PLAN_REVIEW existed must
    still load, and the inserted phase must land where it runs — ahead of a
    checkpoint parked at DESIGN_AUDIT, behind one already past the prototype.
    The stored phase is a name, not an ordinal, so the insertion cannot shift
    it; is_phase_complete compares enum *positions*, which is what has to be
    asserted."""

    FIXTURE = "build-state-pre-plan-review.json"

    def test_fixture_predates_the_change(self):
        """Guard the guard: regenerating this fixture with current code would
        stop it testing backwards compatibility."""
        raw = (FIXTURES / self.FIXTURE).read_text()
        for token in ("plan_review", "prototype", "budget", "docs_impact"):
            assert token not in raw, f"{self.FIXTURE} is no longer a pre-change fixture"

    def test_loads_and_resumes_with_plan_review_still_ahead(self):
        s = BuildState.load(FIXTURES / self.FIXTURE)

        assert s.phase == BuildPhase.DESIGN_AUDIT
        assert s.change_name == "legacy-plan"
        # The two flags the new phase writes take their defaults on an old
        # checkpoint, so the phase runs once rather than being skipped.
        assert s.spec_gen.plan_review_done is False
        assert s.spec_gen.plan_review_regen_done is False
        # A field that predates it keeps the value it was written with.
        assert s.spec_gen.tasks_audit_done is True

        assert s.is_phase_complete(BuildPhase.SPEC_GENERATION)
        # The resume point is the design audit, with the new phase after it
        # rather than silently skipped...
        assert not s.is_phase_complete(BuildPhase.DESIGN_AUDIT)
        assert not s.is_phase_complete(BuildPhase.PLAN_REVIEW)
        assert not s.is_phase_complete(BuildPhase.PROTOTYPE)

    def test_plan_review_is_ordered_between_design_audit_and_prototype(self):
        order = list(BuildPhase)
        assert order.index(BuildPhase.DESIGN_AUDIT) < order.index(BuildPhase.PLAN_REVIEW)
        assert order.index(BuildPhase.PLAN_REVIEW) < order.index(BuildPhase.PROTOTYPE)

    def test_a_checkpoint_past_the_prototype_does_not_rewind_for_the_review(self):
        """...and an old checkpoint already at the review checkpoint is not
        sent back to review its plan — its planning is done."""
        s = BuildState.load(FIXTURES / "build-state-pre-prototype-tdd.json")
        assert s.phase == BuildPhase.REVIEW
        assert s.is_phase_complete(BuildPhase.PLAN_REVIEW)

    def test_roundtrip_rewrites_with_the_new_fields(self, tmp_path):
        """Loading an old checkpoint and re-checkpointing must not lose data."""
        s = BuildState.load(FIXTURES / self.FIXTURE)
        out = tmp_path / "state.json"
        s.checkpoint(out)

        reloaded = BuildState.load(out)
        assert reloaded.phase == BuildPhase.DESIGN_AUDIT
        assert reloaded.spec_gen.completed_artifacts == s.spec_gen.completed_artifacts
        written = json.loads(out.read_text())["spec_gen"]
        assert written["plan_review_done"] is False
        assert written["plan_review_regen_done"] is False
