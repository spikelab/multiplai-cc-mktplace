"""Tests for spec_generator — artifact pipeline orchestration."""

import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from build_pipeline.spec_generator import (
    _extract_capabilities,
    _generate_all_artifacts,
    _load_or_create_state,
    run_spec_generator,
)
from build_pipeline.change_manager import ChangeManager, ARTIFACT_DAG
from build_pipeline.models import ArtifactStatus, BuildPhase
from build_pipeline.state import BuildState, SpecGenState


# --- Capability extraction ---

class TestExtractCapabilities:
    def test_extracts_backtick_names(self):
        text = """\
## Capabilities

### New Capabilities
- `user-auth`: Handle user authentication
- `data-export`: Export data to CSV
"""
        caps = _extract_capabilities(text)
        assert caps == ["user-auth", "data-export"]

    def test_empty_proposal(self):
        assert _extract_capabilities("") == []

    def test_no_capabilities_section(self):
        text = "## Why\nSome motivation\n## What Changes\nSome changes\n"
        assert _extract_capabilities(text) == []

    def test_ignores_non_backtick_items(self):
        text = "- regular item\n- `valid-cap`: desc\n- another item\n"
        caps = _extract_capabilities(text)
        assert caps == ["valid-cap"]


# --- State loading ---

class TestLoadOrCreateState:
    def test_creates_new_state(self, tmp_path):
        config = MagicMock()
        config.state_file_path.return_value = tmp_path / "changes" / "test" / ".build-state.json"
        config.change_name = "test"
        config.mode = "scratch"
        config.tier = "standard"

        state = _load_or_create_state(config)
        assert state.change_name == "test"
        assert state.phase == BuildPhase.SPEC_GENERATION
        assert state.spec_gen is not None
        assert state.spec_gen.completed_artifacts == []

    def test_resumes_existing_state(self, tmp_path):
        state_path = tmp_path / ".build-state.json"
        existing = BuildState(
            change_name="test",
            mode="scratch",
            tier="standard",
            state_file=str(state_path),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(completed_artifacts=["proposal"]),
        )
        state_path.write_text(existing.model_dump_json(indent=2))

        config = MagicMock()
        config.state_file_path.return_value = state_path
        config.change_name = "test"
        config.mode = "scratch"
        config.tier = "standard"

        state = _load_or_create_state(config)
        assert state.spec_gen.completed_artifacts == ["proposal"]


# --- Dependency ordering ---

class TestArtifactDependencyOrder:
    """Verify that artifact DAG enforces correct dependency order."""

    def test_proposal_has_no_deps(self):
        assert ARTIFACT_DAG["proposal"]["requires"] == []

    def test_requirements_requires_proposal(self):
        assert "proposal" in ARTIFACT_DAG["requirements"]["requires"]

    def test_design_requires_proposal(self):
        assert "proposal" in ARTIFACT_DAG["design"]["requires"]

    def test_tasks_requires_requirements_and_design(self):
        reqs = ARTIFACT_DAG["tasks"]["requires"]
        assert "requirements" in reqs
        assert "design" in reqs

    def test_rubric_requires_tasks(self):
        assert "tasks" in ARTIFACT_DAG["rubric"]["requires"]

    def test_full_ordering(self):
        """Proposal -> requirements+design -> tasks -> rubric."""
        _ = ChangeManager(Path("/fake"))
        # Simulate walking the DAG
        order = []
        done = set()

        # Iterate until all resolved
        for _ in range(10):
            for aid, spec in ARTIFACT_DAG.items():
                if aid in done:
                    continue
                if all(dep in done for dep in spec["requires"]):
                    order.append(aid)
                    done.add(aid)

        assert order.index("proposal") < order.index("requirements")
        assert order.index("proposal") < order.index("design")
        assert order.index("requirements") < order.index("tasks")
        assert order.index("design") < order.index("tasks")
        assert order.index("tasks") < order.index("rubric")


# --- Resume behavior ---

class TestResumeSkipsExisting:
    @pytest.fixture
    def change_setup(self, tmp_path):
        """Set up a change directory with some artifacts already present."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")

        # Create proposal as already existing
        (change_dir / "proposal.md").write_text("# Proposal\n## Capabilities\n### New\n- `cap-a`: desc\n")

        config = MagicMock()
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.model = "test-model"
        config.tier = "standard"
        config.mode = "scratch"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            mode="scratch",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(completed_artifacts=["proposal"]),
        )

        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_skips_completed_artifacts(self, change_setup):
        cm, change_dir, config, state = change_setup

        mock_content = "# Generated Content\nSome content here."

        with patch("build_pipeline.llm_steps.spec_steps.generate_artifact", new_callable=AsyncMock) as mock_gen, \
             patch("build_pipeline.spec_generator.generate_rubric", new_callable=AsyncMock) as mock_rubric:
            mock_gen.return_value = mock_content
            mock_rubric.return_value = "# Rubric\nContent"

            await _generate_all_artifacts(cm, change_dir, config, state)

            # proposal should NOT have been generated (it's in completed_artifacts)
            for call in mock_gen.call_args_list:
                assert call[0][0] != "proposal", "Should not regenerate proposal"


# --- Run spec generator (integration-ish) ---

class TestRunSpecGenerator:
    @pytest.mark.asyncio
    async def test_returns_0_on_success(self, tmp_path):
        specs_dir = tmp_path / "specs"

        config = MagicMock()
        config.change_name = "test"
        config.specs_dir = specs_dir
        config.mode = "scratch"
        config.tier = "standard"
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = tmp_path / ".build-state.json"
        config.change_dir = specs_dir / "changes" / "test"

        with patch("build_pipeline.spec_generator._generate_all_artifacts", new_callable=AsyncMock) as mock_gen, \
             patch("build_pipeline.spec_generator._run_audit", new_callable=AsyncMock) as mock_audit:
            mock_audit.return_value = []
            exit_code = await run_spec_generator(config)
            assert exit_code == 0
            mock_gen.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_1_on_failure(self, tmp_path):
        specs_dir = tmp_path / "specs"

        config = MagicMock()
        config.change_name = "test"
        config.specs_dir = specs_dir
        config.mode = "scratch"
        config.tier = "standard"
        config.model = "test-model"
        config.state_file_path.return_value = tmp_path / ".build-state.json"
        config.change_dir = specs_dir / "changes" / "test"

        with patch("build_pipeline.spec_generator._generate_all_artifacts", new_callable=AsyncMock) as mock_gen:
            mock_gen.side_effect = RuntimeError("boom")
            exit_code = await run_spec_generator(config)
            assert exit_code == 1
