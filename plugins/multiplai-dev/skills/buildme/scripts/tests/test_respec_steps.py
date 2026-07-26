"""Tests for the respec loop — notes on disk as the build runs, and an
end-of-build proposal that never edits the specs it comments on."""

import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.respec_steps import (
    DELTA_SECTIONS,
    append_implementation_note,
    ensure_delta_sections,
    notes_path,
    run_respec_audit,
)
from build_pipeline.models import ImplementationNote

LLM_CALL = "build_pipeline.llm_steps.respec_steps.llm_call"

DELTA_RESPONSE = """\
## ADDED Requirements

### Requirement: Retry on transient upload failure
The system SHALL retry a failed upload twice before surfacing an error.

#### Scenario: Transient failure
- **WHEN** the storage client raises a timeout
- **THEN** the upload is retried twice before the error surfaces

_Motivated by:_ "the client raises on timeout, the design assumed a return
code" (block 1, implementer)

## MODIFIED Requirements

### Requirement: Upload a document

The system SHALL upload a document and return its id.

## REMOVED Requirements

_None proposed._
"""


def _make_config(tmp_path: Path, change_name: str = "test-change") -> BuildConfig:
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    config = BuildConfig(
        project_dir=project_dir,
        change_name=change_name,
        config_dir=tmp_path / "config",
    )
    config.specs_dir = project_dir / "specs"
    config.change_dir.mkdir(parents=True, exist_ok=True)
    return config


def _seed_specs(config: BuildConfig) -> dict[Path, str]:
    """Write requirements/*.md + design.md; return path → sha256 of each."""
    req_dir = config.change_dir / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    files = {
        req_dir / "upload.md": (
            "### Requirement: Upload a document\n"
            "The system SHALL upload a document.\n"
        ),
        req_dir / "auth.md": (
            "### Requirement: Authenticate\nThe system SHALL authenticate.\n"
        ),
        config.change_dir / "design.md": "# Design\nUploads are synchronous.\n",
    }
    for path, text in files.items():
        path.write_text(text)
    return {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }


def _note(**kw) -> ImplementationNote:
    base = dict(
        block_number=1,
        block_name="Uploader",
        role="implementer",
        surprises="The storage client raises on timeout; the design assumed a return code.",
        spec_impact="contradicts",
    )
    base.update(kw)
    return ImplementationNote(**base)


class TestAppendImplementationNote:
    def test_creates_file_with_header_and_note(self, tmp_path):
        config = _make_config(tmp_path)
        path = append_implementation_note(config.change_dir, _note())

        assert path == notes_path(config.change_dir)
        text = path.read_text()
        assert "# Implementation Notes" in text
        assert "## Block 1 — Uploader (implementer)" in text
        assert "SPEC_IMPACT: contradicts" in text
        assert "raises on timeout" in text

    def test_appends_without_clobbering_earlier_notes(self, tmp_path):
        config = _make_config(tmp_path)
        append_implementation_note(config.change_dir, _note())
        append_implementation_note(
            config.change_dir,
            _note(block_number=2, block_name="Auth", role="test_writer",
                  surprises="Token TTL was unspecified.", spec_impact="clarify"),
        )

        text = notes_path(config.change_dir).read_text()
        assert text.count("# Implementation Notes") == 1
        assert "## Block 1 — Uploader (implementer)" in text
        assert "## Block 2 — Auth (test_writer)" in text
        assert "Token TTL was unspecified." in text


class TestEnsureDeltaSections:
    def test_adds_missing_sections(self):
        out = ensure_delta_sections("## ADDED Requirements\n\n### Requirement: X\n")
        for section in DELTA_SECTIONS:
            assert section in out
        assert out.count("_None proposed._") == 2

    def test_keeps_existing_sections_once(self):
        out = ensure_delta_sections(DELTA_RESPONSE)
        for section in DELTA_SECTIONS:
            assert out.count(section) == 1


class TestRunRespecAudit:
    @pytest.mark.asyncio
    async def test_writes_respec_in_delta_format(self, tmp_path):
        config = _make_config(tmp_path)
        _seed_specs(config)
        append_implementation_note(config.change_dir, _note())

        with patch(LLM_CALL, new_callable=AsyncMock, return_value=DELTA_RESPONSE):
            path = await run_respec_audit(config, None)

        assert path == config.change_dir / "respec.md"
        text = path.read_text()
        for section in DELTA_SECTIONS:
            assert section in text
        assert "Retry on transient upload failure" in text
        assert "has been applied" in text  # propose-only banner

    @pytest.mark.asyncio
    async def test_never_modifies_requirements_or_design(self, tmp_path):
        """Criterion 9: the spec files are byte-identical before and after."""
        config = _make_config(tmp_path)
        before = _seed_specs(config)
        append_implementation_note(config.change_dir, _note())

        with patch(LLM_CALL, new_callable=AsyncMock, return_value=DELTA_RESPONSE):
            await run_respec_audit(config, None)

        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
        assert after == before

    @pytest.mark.asyncio
    async def test_notes_and_specs_are_in_the_prompt(self, tmp_path):
        config = _make_config(tmp_path)
        _seed_specs(config)
        append_implementation_note(config.change_dir, _note())

        with patch(LLM_CALL, new_callable=AsyncMock, return_value=DELTA_RESPONSE) as mock:
            await run_respec_audit(config, None)

        prompt = mock.call_args[0][0]
        assert "raises on timeout" in prompt
        assert "Requirement: Upload a document" in prompt
        assert "Uploads are synchronous." in prompt

    @pytest.mark.asyncio
    async def test_no_notes_writes_empty_proposal_without_calling_the_llm(self, tmp_path):
        config = _make_config(tmp_path)
        _seed_specs(config)

        with patch(LLM_CALL, new_callable=AsyncMock) as mock:
            path = await run_respec_audit(config, None)

        mock.assert_not_called()
        text = path.read_text()
        assert "no spec delta is proposed" in text
        for section in DELTA_SECTIONS:
            assert section in text

    @pytest.mark.asyncio
    async def test_llm_failure_is_non_fatal_and_leaves_specs_untouched(self, tmp_path):
        config = _make_config(tmp_path)
        before = _seed_specs(config)
        append_implementation_note(config.change_dir, _note())

        with patch(LLM_CALL, new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            path = await run_respec_audit(config, None)

        assert path is None
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
        assert after == before

    @pytest.mark.asyncio
    async def test_malformed_model_output_still_lands_in_delta_format(self, tmp_path):
        config = _make_config(tmp_path)
        _seed_specs(config)
        append_implementation_note(config.change_dir, _note())

        with patch(LLM_CALL, new_callable=AsyncMock, return_value="I think the spec is fine."):
            path = await run_respec_audit(config, None)

        text = path.read_text()
        for section in DELTA_SECTIONS:
            assert section in text


class TestArchiveCarriesTheLoopArtifacts:
    """3.5 — both artifacts travel into archive/ and neither reaches registry/."""

    def test_archive_moves_notes_and_respec_but_merges_only_requirements(self, tmp_path):
        config = _make_config(tmp_path)
        _seed_specs(config)
        append_implementation_note(config.change_dir, _note())
        (config.change_dir / "respec.md").write_text(
            "## ADDED Requirements\n\n### Requirement: Retry on timeout\n"
        )

        cm = ChangeManager(config.specs_dir)
        dest = cm.archive_change(config.change_dir)

        assert (dest / "implementation-notes.md").exists()
        assert (dest / "respec.md").exists()
        registry = config.specs_dir / "registry"
        assert (registry / "upload.md").exists()  # requirements DO merge
        assert not (registry / "respec.md").exists()
        assert not (registry / "implementation-notes.md").exists()
        # The proposed delta was not applied to the merged registry.
        assert "Retry on timeout" not in (registry / "upload.md").read_text()
