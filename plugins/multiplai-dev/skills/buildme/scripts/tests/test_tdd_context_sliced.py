"""`tdd.context: sliced` gives each TDD agent the design sections its block
cites, the requirements it satisfies, the rubric for the reviewer role only,
and only the memory files `memory_files.tdd` lists (plan item 65). `full`, the
default, is proved equal to main in `test_tdd_context_full.py`."""

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.models import BlockInfo
from build_pipeline.tdd_engine import (
    _sliced_design,
    _sliced_requirements,
    assemble_context,
)

from tests.test_tdd_context_full import DESIGN, setup_change

SLICED = "memory_files:\n  tdd: [testing.md]\ntdd:\n  context: sliced\n"


def _block(description="", satisfies=(), name="Block"):
    return BlockInfo(
        number=1, name=name, description=description, satisfies=list(satisfies)
    )


class TestConfig:
    def _config(self, tmp_path, yaml_text):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(yaml_text)
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        return config

    def test_full_by_default(self, tmp_path):
        assert BuildConfig().tdd_context == "full"
        assert self._config(tmp_path, "tdd:\n  test_command: x\n").tdd_context == (
            "full"
        )

    def test_sliced_and_the_tdd_memory_files_are_read(self, tmp_path):
        config = self._config(tmp_path, SLICED)
        assert config.tdd_context == "sliced"
        assert config.tdd_memory_files == ["testing.md"]

    @pytest.mark.parametrize("value", ["slice", "1", "true"])
    def test_anything_else_is_full_with_a_warning(self, tmp_path, value, caplog):
        config = self._config(tmp_path, f"tdd:\n  context: {value}\n")
        assert config.tdd_context == "full"
        assert "tdd.context" in caplog.text


class TestDesign:
    def test_keeps_the_title_and_the_cited_sections(self):
        text = _sliced_design(
            _block("Build it as the Arithmetic section says."), DESIGN
        )
        assert text.startswith("# Design\n\n## Arithmetic\n")
        assert "### Overflow" in text
        assert "## Audit Log" not in text
        assert "## Overview" not in text
        assert "## Global Constraints" not in text

    def test_a_cited_subheading_keeps_its_whole_section(self):
        text = _sliced_design(_block("Mind overflow."), DESIGN)
        assert "## Arithmetic" in text and "### Overflow" in text
        assert "## Audit Log" not in text

    def test_the_block_name_cites_too(self):
        text = _sliced_design(_block(name="Audit log"), DESIGN)
        assert "## Audit Log" in text and "## Arithmetic" not in text

    def test_no_citation_keeps_the_whole_design(self):
        assert _sliced_design(_block("Add numbers."), DESIGN) == DESIGN

    def test_a_part_of_a_word_is_not_a_citation(self):
        assert _sliced_design(_block("Arithmetical tricks."), DESIGN) == DESIGN


SOURCES = {
    "specs.md#arithmetic": (
        "## ADDED Requirements\n\n"
        "### Requirement: Adding\n\nadd it\n\n"
        "### Requirement: Subtracting\n\nsub it\n"
    ),
    "specs.md#audit-log": (
        "## ADDED Requirements\n\n### Requirement: Role changes are logged\n\nlog\n"
    ),
}


class TestRequirements:
    def test_a_named_capability_keeps_all_its_requirements(self):
        got = _sliced_requirements(_block(satisfies=["arithmetic"]), SOURCES)
        assert got == {"specs.md#arithmetic": SOURCES["specs.md#arithmetic"]}

    def test_named_requirements_keep_only_those(self):
        got = _sliced_requirements(_block(satisfies=["arithmetic — Adding"]), SOURCES)
        assert list(got) == ["specs.md#arithmetic"]
        assert "### Requirement: Adding" in got["specs.md#arithmetic"]
        assert "Subtracting" not in got["specs.md#arithmetic"]
        assert got["specs.md#arithmetic"].startswith("## ADDED Requirements")

    def test_a_requirement_named_without_its_capability(self):
        got = _sliced_requirements(
            _block(satisfies=["Role changes are logged"]), SOURCES
        )
        assert list(got) == ["specs.md#audit-log"]

    def test_requirements_files_are_matched_by_their_stem(self):
        sources = {"requirements/audit-log.md": "x", "requirements/billing.md": "y"}
        got = _sliced_requirements(_block(satisfies=["audit-log"]), sources)
        assert got == {"requirements/audit-log.md": "x"}

    def test_naming_nothing_keeps_every_source(self):
        assert _sliced_requirements(_block(satisfies=["other"]), SOURCES) == SOURCES
        assert _sliced_requirements(_block(), SOURCES) == SOURCES


class TestAssembleContext:
    @pytest.fixture
    def contexts(self, tmp_path, monkeypatch):
        config, state, _, block = setup_change(tmp_path, monkeypatch, SLICED)
        return {
            role: assemble_context(block, config, role, blocks=state.tdd.blocks)
            for role in ("test_writer", "implementer", "refactorer", "reviewer")
        }

    def test_the_design_is_cut_to_the_cited_section(self, contexts):
        for text in contexts.values():
            assert "## Arithmetic" in text
            assert "## Audit Log" not in text
            # Global Constraints still lead every bundle, once.
            assert "## Global Constraints (project-wide" in text
            assert "Python 3.11 floor." in text

    def test_the_requirements_are_cut_to_what_the_block_satisfies(self, contexts):
        text = contexts["implementer"]
        assert "## Requirements: specs.md#arithmetic" in text
        assert "### Requirement: Adding" in text
        assert "Subtracting" not in text
        assert "audit-log" not in text
        # The test writer gets the specs through its own slot, as with full.
        assert "## Requirements:" not in contexts["test_writer"]

    def test_the_rubric_goes_to_the_reviewer_only(self, contexts):
        assert "## Evaluation Rubric" in contexts["reviewer"]
        for role in ("test_writer", "implementer", "refactorer"):
            assert "## Evaluation Rubric" not in contexts[role]

    def test_only_the_tdd_memory_files(self, contexts):
        for text in contexts.values():
            assert "## Memory: testing.md" in text
            assert "technical-pref.md" not in text

    def test_no_tdd_memory_files_means_none(self, tmp_path, monkeypatch):
        config, state, _, block = setup_change(
            tmp_path, monkeypatch, "tdd:\n  context: sliced\n"
        )
        text = assemble_context(block, config, "implementer", blocks=state.tdd.blocks)
        assert "## Memory:" not in text

    def test_sliced_is_smaller_than_full(self, tmp_path, monkeypatch, contexts):
        other = tmp_path / "full"
        other.mkdir()
        config, state, _, block = setup_change(other, monkeypatch, "")
        full = assemble_context(block, config, "implementer", blocks=state.tdd.blocks)
        assert len(contexts["implementer"]) < len(full)
