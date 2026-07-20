"""Tests for change_manager — the specs/ directory manager."""

import pytest

from build_pipeline.change_manager import (
    ChangeManager,
    ARTIFACT_DAG,
    extract_global_constraints,
)
from build_pipeline.models import ArtifactStatus


@pytest.fixture
def specs_dir(tmp_path):
    """Create a minimal specs/ directory structure."""
    d = tmp_path / "specs"
    d.mkdir()
    (d / "changes").mkdir()
    (d / "registry").mkdir()
    (d / "archive").mkdir()
    return d


@pytest.fixture
def cm(specs_dir):
    return ChangeManager(specs_dir)


class TestCreateChange:
    def test_creates_directory_and_metadata(self, cm, specs_dir):
        path = cm.create_change("my-feature")
        assert path.exists()
        assert (path / ".change.yaml").exists()
        meta = (path / ".change.yaml").read_text()
        assert "spec-driven" in meta

    def test_normalizes_name(self, cm):
        path = cm.create_change("My Feature!")
        assert path.name == "my-feature"

    def test_idempotent(self, cm):
        p1 = cm.create_change("test")
        p2 = cm.create_change("test")
        assert p1 == p2

    def test_underscores_to_hyphens(self, cm):
        path = cm.create_change("my_feature_name")
        assert path.name == "my-feature-name"


class TestArtifactStatus:
    def test_empty_change(self, cm, specs_dir):
        change = cm.create_change("test")
        status = cm.artifact_status(change)
        assert status["proposal"] == ArtifactStatus.READY
        assert status["requirements"] == ArtifactStatus.BLOCKED
        assert status["design"] == ArtifactStatus.BLOCKED
        assert status["tasks"] == ArtifactStatus.BLOCKED

    def test_proposal_done_unlocks_requirements_and_design(self, cm, specs_dir):
        change = cm.create_change("test")
        (change / "proposal.md").write_text("# Proposal\n")
        status = cm.artifact_status(change)
        assert status["proposal"] == ArtifactStatus.DONE
        assert status["requirements"] == ArtifactStatus.READY
        assert status["design"] == ArtifactStatus.READY
        assert status["tasks"] == ArtifactStatus.BLOCKED

    def test_all_done(self, cm, specs_dir):
        change = cm.create_change("test")
        (change / "proposal.md").write_text("x")
        (change / "design.md").write_text("x")
        (change / "requirements").mkdir(exist_ok=True)
        (change / "requirements" / "cap1.md").write_text("x")
        (change / "tasks.md").write_text("x")
        (change / "rubric.md").write_text("x")
        status = cm.artifact_status(change)
        assert all(s == ArtifactStatus.DONE for s in status.values())

    def test_tasks_blocked_without_requirements(self, cm, specs_dir):
        change = cm.create_change("test")
        (change / "proposal.md").write_text("x")
        (change / "design.md").write_text("x")
        status = cm.artifact_status(change)
        assert status["tasks"] == ArtifactStatus.BLOCKED

    def test_tasks_ready_with_requirements_and_design(self, cm, specs_dir):
        change = cm.create_change("test")
        (change / "proposal.md").write_text("x")
        (change / "design.md").write_text("x")
        (change / "requirements").mkdir(exist_ok=True)
        (change / "requirements" / "cap1.md").write_text("x")
        status = cm.artifact_status(change)
        assert status["tasks"] == ArtifactStatus.READY


class TestReadyArtifacts:
    def test_initial_state(self, cm):
        change = cm.create_change("test")
        ready = cm.ready_artifacts(change)
        assert ready == ["proposal"]

    def test_after_proposal(self, cm):
        change = cm.create_change("test")
        (change / "proposal.md").write_text("x")
        ready = cm.ready_artifacts(change)
        assert "requirements" in ready
        assert "design" in ready


class TestChangeStatus:
    def test_returns_change_status_object(self, cm):
        change = cm.create_change("feat")
        cs = cm.change_status(change)
        assert cs.change_name == "feat"
        assert len(cs.artifacts) == len(ARTIFACT_DAG)
        assert not cs.is_complete


class TestArtifactContext:
    def test_returns_template_and_instruction(self, cm):
        change = cm.create_change("test")
        ctx = cm.artifact_context(change, "proposal")
        assert "## Why" in ctx["template"]
        assert "WHY" in ctx["instruction"]
        assert ctx["output_path"] == "proposal.md"

    def test_includes_dependency_paths(self, cm):
        change = cm.create_change("test")
        (change / "proposal.md").write_text("x")
        ctx = cm.artifact_context(change, "design")
        assert "proposal" in ctx["dependencies"]

    def test_loads_project_context(self, cm, specs_dir):
        config = specs_dir / "config.yaml"
        config.write_text("schema: spec-driven\ncontext: |\n  Project: TestProject\n")
        change = cm.create_change("test")
        ctx = cm.artifact_context(change, "proposal")
        assert "TestProject" in ctx["context"]


class TestListChanges:
    def test_empty(self, cm):
        assert cm.list_changes() == []

    def test_lists_active_changes(self, cm):
        cm.create_change("feat-a")
        cm.create_change("feat-b")
        changes = cm.list_changes()
        assert len(changes) == 2
        names = [c["name"] for c in changes]
        assert "feat-a" in names
        assert "feat-b" in names


class TestArchiveChange:
    def test_moves_to_archive(self, cm):
        change = cm.create_change("done")
        (change / "proposal.md").write_text("x")
        dest = cm.archive_change(change, merge_specs=False)
        assert dest.exists()
        assert not change.exists()
        assert "done" in dest.name

    def test_merges_delta_requirements(self, cm, specs_dir):
        change = cm.create_change("done")
        (change / "proposal.md").write_text("x")
        # Create delta requirement
        (change / "requirements").mkdir(parents=True)
        (change / "requirements" / "auth.md").write_text(
            "## ADDED Requirements\n\n"
            "### Requirement: User can login\nThe system SHALL allow login.\n\n"
            "#### Scenario: Valid credentials\n- **WHEN** valid creds\n- **THEN** login succeeds\n"
        )

        cm.archive_change(change, merge_specs=True)
        main_file = specs_dir / "registry" / "auth.md"
        assert main_file.exists()
        assert "User can login" in main_file.read_text()


class TestDeltaRequirementMerging:
    def test_added_requirements_append(self, cm, specs_dir):
        # Existing main registry file
        registry = specs_dir / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / "data.md").write_text(
            "### Requirement: Existing\nThe system SHALL exist.\n\n"
            "#### Scenario: It exists\n- **WHEN** checked\n- **THEN** it exists\n"
        )
        # Delta with ADDED
        change = cm.create_change("add-export")
        (change / "requirements").mkdir(parents=True)
        (change / "requirements" / "data.md").write_text(
            "## ADDED Requirements\n\n"
            "### Requirement: Export data\nThe system SHALL export.\n\n"
            "#### Scenario: CSV export\n- **WHEN** export\n- **THEN** CSV\n"
        )
        cm._merge_delta_requirements(change)
        merged = (registry / "data.md").read_text()
        assert "Existing" in merged
        assert "Export data" in merged

    def test_removed_requirements_delete(self, cm, specs_dir):
        registry = specs_dir / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / "old.md").write_text(
            "### Requirement: Keep me\nStay.\n\n"
            "#### Scenario: Stays\n- **WHEN** x\n- **THEN** y\n\n"
            "### Requirement: Remove me\nGo away.\n\n"
            "#### Scenario: Goes\n- **WHEN** x\n- **THEN** y\n"
        )
        change = cm.create_change("cleanup")
        (change / "requirements").mkdir(parents=True)
        (change / "requirements" / "old.md").write_text(
            "## REMOVED Requirements\n\n"
            "### Requirement: Remove me\n**Reason**: Obsolete\n"
        )
        cm._merge_delta_requirements(change)
        merged = (registry / "old.md").read_text()
        assert "Keep me" in merged
        assert "Remove me" not in merged


class TestInitSpecs:
    def test_creates_directory_structure(self, tmp_path):
        specs = tmp_path / "specs"
        cm = ChangeManager(specs)
        cm.init_specs()
        assert (specs / "changes").is_dir()
        assert (specs / "registry").is_dir()
        assert (specs / "archive").is_dir()


class TestExtractGlobalConstraints:
    """design.md's `## Global Constraints` body is threaded into agent prompts."""

    def test_extracts_section_body_verbatim(self):
        design = (
            "# Design\n\n## Decisions\nUse X.\n\n"
            "## Global Constraints\n"
            "- Python >= 3.11\n"
            "- Queue name is exactly `dolce-jobs-v2`\n\n"
            "## Risks\nNone.\n"
        )
        out = extract_global_constraints(design)
        assert out == "- Python >= 3.11\n- Queue name is exactly `dolce-jobs-v2`"

    def test_extracts_trailing_section(self):
        design = "# Design\n\n## Global Constraints\n- Only rule.\n"
        assert extract_global_constraints(design) == "- Only rule."

    def test_returns_empty_when_absent(self):
        assert extract_global_constraints("# Design\n\n## Decisions\nNothing.\n") == ""

    def test_stops_at_next_h2(self):
        design = "## Global Constraints\n- A\n\n## Other\n- B\n"
        out = extract_global_constraints(design)
        assert "- A" in out
        assert "- B" not in out
