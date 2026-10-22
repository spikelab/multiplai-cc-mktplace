"""Tests for change_manager — the specs/ directory manager."""

from pathlib import Path

import pytest
from build_pipeline.change_manager import (
    ARTIFACT_DAG,
    ChangeManager,
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

    def test_repairs_dir_missing_metadata(self, cm, specs_dir):
        """A pre-existing dir without .change.yaml (junk from an older
        pipeline) must not suppress the metadata — retry writes it."""
        junk = specs_dir / "changes" / "retried"
        junk.mkdir(parents=True)
        (junk / ".board.json").write_text("{}")
        path = cm.create_change("retried")
        assert path == junk
        assert (path / ".change.yaml").exists()
        assert "spec-driven" in (path / ".change.yaml").read_text()
        assert (path / ".board.json").exists(), "existing files must survive the repair"


class TestNormalizeChangeName:
    def test_empty_input_stays_empty(self):
        from build_pipeline.change_manager import normalize_change_name
        assert normalize_change_name("") == ""

    def test_traversal_is_stripped(self):
        from build_pipeline.change_manager import normalize_change_name
        assert normalize_change_name("../../etc") == "etc"

    def test_refuses_name_that_normalizes_to_nothing(self):
        """'!!!' → '' would collapse change_dir to specs/changes and the
        branch to 'buildme/', dying late with a raw git error."""
        from build_pipeline.change_manager import normalize_change_name
        with pytest.raises(ValueError) as exc:
            normalize_change_name("!!!")
        assert "no usable characters" in str(exc.value)

    def test_cli_refuses_empty_normalized_change_early(self, tmp_path, capsys):
        """The refusal happens at the CLI entry point, before any phase runs."""
        from build_pipeline.__main__ import main
        rc = main(["build", "--change", "!!!", "--project-dir", str(tmp_path)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "ERROR:" in err and "no usable characters" in err
        assert list(tmp_path.iterdir()) == [], "nothing may be created behind the refusal"


class TestArchivedChangeDirs:
    def test_anchored_match_ignores_suffix_collisions(self, specs_dir):
        """`foo` must not match `2026-07-26-bar-foo` — a loose `*-foo` glob
        would cross two changes' archives."""
        from build_pipeline.change_manager import archived_change_dirs
        (specs_dir / "archive" / "2026-07-26-bar-foo").mkdir(parents=True)
        (specs_dir / "archive" / "2026-07-20-foo").mkdir()
        (specs_dir / "archive" / "2026-07-25-foo").mkdir()
        (specs_dir / "archive" / "not-a-date-foo").mkdir()
        assert archived_change_dirs(specs_dir, "foo") == [
            specs_dir / "archive" / "2026-07-20-foo",
            specs_dir / "archive" / "2026-07-25-foo",
        ]
        assert archived_change_dirs(specs_dir, "bar-foo") == [
            specs_dir / "archive" / "2026-07-26-bar-foo",
        ]

    def test_no_archive_root_or_empty_name(self, tmp_path):
        from build_pipeline.change_manager import archived_change_dirs
        assert archived_change_dirs(tmp_path / "specs", "foo") == []
        assert archived_change_dirs(tmp_path / "specs", "") == []


def _inputs(change, *, use_cases=True, requirements=True):
    """pm-spec's documents: the proposal, and the use cases and requirements
    unless told otherwise."""
    (change / "proposal.md").write_text("## Capabilities\n- `cap1`\n")
    if use_cases:
        (change / "use-cases.md").write_text("x")
    if requirements:
        (change / "requirements").mkdir(exist_ok=True)
        (change / "requirements" / "cap1.md").write_text("### Requirement: A\n#### Scenario: A\nWHEN invoked\nTHEN returns A\n")


class TestArtifactStatus:
    def test_empty_change_blocks_everything(self, cm, specs_dir):
        """With no inputs nothing buildme writes can start."""
        change = cm.create_change("test")
        status = cm.artifact_status(change)
        assert set(status) == {"design", "unknowns", "tasks", "rubric"}
        assert set(status.values()) == {ArtifactStatus.BLOCKED}

    def test_the_inputs_unlock_design(self, cm, specs_dir):
        change = cm.create_change("test")
        _inputs(change)
        status = cm.artifact_status(change)
        assert status["design"] == ArtifactStatus.READY
        assert status["unknowns"] == ArtifactStatus.BLOCKED
        assert status["tasks"] == ArtifactStatus.BLOCKED

    def test_design_blocked_without_requirements(self, cm, specs_dir):
        change = cm.create_change("test")
        _inputs(change, requirements=False)
        assert cm.artifact_status(change)["design"] == ArtifactStatus.BLOCKED

    def test_all_done(self, cm, specs_dir):
        change = cm.create_change("test")
        _inputs(change)
        for name in ("design", "unknowns", "tasks", "rubric"):
            (change / f"{name}.md").write_text("x")
        status = cm.artifact_status(change)
        assert all(s == ArtifactStatus.DONE for s in status.values())

    def test_tasks_blocked_without_unknowns(self, cm, specs_dir):
        """The explainer gate is a hard DAG dependency: tasks cannot be written
        before the edge cases of anything new are on disk."""
        change = cm.create_change("test")
        _inputs(change)
        (change / "design.md").write_text("x")
        status = cm.artifact_status(change)
        assert status["unknowns"] == ArtifactStatus.READY
        assert status["tasks"] == ArtifactStatus.BLOCKED

    def test_tasks_ready_with_requirements_design_unknowns_and_use_cases(
        self, cm, specs_dir,
    ):
        change = cm.create_change("test")
        _inputs(change)
        (change / "design.md").write_text("x")
        (change / "unknowns.md").write_text("x")
        status = cm.artifact_status(change)
        assert status["tasks"] == ArtifactStatus.READY

    def test_tasks_blocked_without_use_cases(self, cm, specs_dir):
        """A use case is delivered by a block or by nothing at all, so the
        breakdown is not written until the use cases are on disk."""
        change = cm.create_change("test")
        _inputs(change, use_cases=False)
        (change / "design.md").write_text("x")
        (change / "unknowns.md").write_text("x")
        status = cm.artifact_status(change)
        assert status["tasks"] == ArtifactStatus.BLOCKED

    def test_missing_inputs(self, cm, specs_dir):
        change = cm.create_change("test")
        assert cm.missing_inputs(change) == ["proposal.md", "use-cases.md", "requirements/*.md"]
        _inputs(change, use_cases=False)
        assert cm.missing_inputs(change) == ["use-cases.md"]
        (change / "use-cases.md").write_text("x")
        assert cm.missing_inputs(change) == []


class TestReadyArtifacts:
    def test_initial_state(self, cm):
        change = cm.create_change("test")
        assert cm.ready_artifacts(change) == []

    def test_after_the_inputs(self, cm):
        change = cm.create_change("test")
        _inputs(change)
        assert cm.ready_artifacts(change) == ["design"]


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
        ctx = cm.artifact_context(change, "design")
        assert "## Decisions" in ctx["template"]
        assert ctx["instruction"]
        assert ctx["output_path"] == "design.md"

    def test_includes_dependency_paths(self, cm):
        change = cm.create_change("test")
        _inputs(change)
        ctx = cm.artifact_context(change, "design")
        assert ctx["dependencies"]["proposal"] == "proposal.md"
        assert ctx["dependencies"]["requirements"] == "requirements/cap1.md"
        assert cm.artifact_context(change, "tasks")["dependencies"]["use-cases"] == "use-cases.md"

    @pytest.mark.parametrize("name", ["proposal", "use-cases", "requirements"])
    def test_buildme_has_no_template_for_pm_specs_documents(self, cm, name):
        change = cm.create_change("test")
        ctx = cm.artifact_context(change, name)
        assert ctx["template"] == "" and ctx["instruction"] == "" and ctx["output_path"] == ""

    def test_design_drops_goals_non_goals_and_keeps_its_other_sections(self, cm):
        change = cm.create_change("test")
        ctx = cm.artifact_context(change, "design")
        template = ctx["template"]
        assert "Goals / Non-Goals" not in template
        assert "## Context" in template
        assert "## Decisions" in template
        assert "## Risks / Trade-offs" in template

    def test_loads_project_context(self, cm, specs_dir):
        config = specs_dir / "config.yaml"
        config.write_text("schema: spec-driven\ncontext: |\n  Project: TestProject\n")
        change = cm.create_change("test")
        ctx = cm.artifact_context(change, "design")
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
        (change / "proposal.md").write_text("## Capabilities\n- `cap1`\n")
        dest = cm.archive_change(change, merge_specs=False)
        assert dest.exists()
        assert not change.exists()
        assert "done" in dest.name

    def test_merges_delta_requirements(self, cm, specs_dir):
        change = cm.create_change("done")
        (change / "proposal.md").write_text("## Capabilities\n- `cap1`\n")
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


# --- spec-extract's registry files (spec-extract plan, PR 3a item 18) -----------------
# spec-extract writes registry files with extra lines under each scenario (CITES,
# EVIDENCE) and an HTML comment on the first line. Both readers must still work on them.

SPEC_EXTRACT_SAMPLE = (Path(__file__).resolve().parents[3] / "spec-extract" / "scripts" / "tests"
                       / "fixtures" / "golden" / "registry" / "sample.md")


def test_merges_delta_into_spec_extract_registry_file(cm, specs_dir):
    (specs_dir / "registry" / "sample.md").write_text(SPEC_EXTRACT_SAMPLE.read_text())
    change = cm.create_change("calc-change")
    (change / "requirements").mkdir(exist_ok=True)
    (change / "requirements" / "sample.md").write_text(
        "## MODIFIED Requirements\n\n"
        "### Requirement: Adds two numbers\nadd SHALL return the sum as an int.\n\n"
        "#### Scenario: adding ints\n- **WHEN** add(1, 2) is called\n- **THEN** it returns the int 3\n\n"
        "## REMOVED Requirements\n\n### Requirement: Subtracts two numbers\n")
    cm.archive_change(change)
    merged = (specs_dir / "registry" / "sample.md").read_text()
    assert merged.startswith("<!-- spec-extract widgets/tree@")
    assert "add SHALL return the sum as an int." in merged
    assert "add SHALL return the sum of its arguments." not in merged
    assert "Subtracts two numbers" not in merged and "sub(3, 1)" not in merged
    assert merged.count("### Requirement:") == 1


def test_requirements_need_every_proposed_capability(tmp_path):
    from build_pipeline.change_manager import ChangeManager
    from build_pipeline.models import ArtifactStatus
    cm = ChangeManager(tmp_path / "specs")
    change = cm.create_change("two-caps")
    (change / "proposal.md").write_text("## Capabilities\n- `one`\n- `two`\n")
    (change / "use-cases.md").write_text("x")
    (change / "requirements").mkdir()
    (change / "requirements" / "one.md").write_text(
        "## ADDED Requirements\n### Requirement: One\nRequired.\n#### Scenario: Works\n- **WHEN** called\n- **THEN** succeeds\n")
    (change / "design.md").write_text("x")
    (change / "unknowns.md").write_text("x")
    (change / "tasks.md").write_text("## 1. Both\nImplement both capabilities")
    assert cm.missing_inputs(change) == ["requirements/*.md"]
    status = cm.artifact_status(change)
    assert status["design"] == ArtifactStatus.BLOCKED
    assert status["tasks"] == ArtifactStatus.BLOCKED


def test_requirements_validate_every_requirement_and_allow_removal_deltas():
    from build_pipeline.change_manager import valid_requirement_text
    valid = "### Requirement: Works\n#### Scenario: Normal\nWHEN called\nTHEN succeeds\n"
    assert valid_requirement_text(valid)
    assert not valid_requirement_text(valid + "### Requirement: Missing scenarios\n")
    assert not valid_requirement_text(valid + "#### Scenario: Incomplete\nWHEN called\n")
    assert valid_requirement_text("## REMOVED Requirements\n### Requirement: Old behavior\n")
    assert not valid_requirement_text("## ADDED Requirements\n### Requirement: <!-- name -->\n")
