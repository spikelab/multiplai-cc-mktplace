"""Regression tests: spec-generation steps ground prompts in requirements/.

These lock in the fix for the legacy specs/ readers. The only writer of
capability files is spec_generator._generate_requirements, which writes flat
change_dir/requirements/<capability>.md. Design/tasks/audit must read from
there — not the empty legacy change_dir/specs/*/spec.md layout.
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.spec_steps import (
    _build_prompt,
    _read_specs,
    run_design_audit,
    run_tasks_audit,
)
from build_pipeline.prompts.spec_generation import TASKS_PROMPT


def _make_config(tmp_path, req_files: dict[str, str]) -> BuildConfig:
    project_dir = tmp_path / "project"
    specs_root = project_dir / "specs"
    change_dir = specs_root / "changes" / "feat"
    (change_dir / "requirements").mkdir(parents=True)
    for name, text in req_files.items():
        (change_dir / "requirements" / f"{name}.md").write_text(text)
    config = BuildConfig(project_dir=project_dir, change_name="feat")
    config.specs_dir = specs_root
    return config


class TestReadSpecs:
    def test_reads_requirement_files(self, tmp_path):
        config = _make_config(
            tmp_path,
            {
                "auth": "Requirement: user can log in with email.",
                "billing": "Requirement: user can view invoices.",
            },
        )
        out = _read_specs(config)
        assert out != "(no specs yet)"
        assert "user can log in with email" in out
        assert "user can view invoices" in out
        # Capability name is the file stem (mirrors tdd_engine.assemble_context)
        assert "### auth" in out
        assert "### billing" in out

    def test_empty_when_no_requirements(self, tmp_path):
        project_dir = tmp_path / "project"
        specs_root = project_dir / "specs"
        (specs_root / "changes" / "feat").mkdir(parents=True)
        config = BuildConfig(project_dir=project_dir, change_name="feat")
        config.specs_dir = specs_root
        assert _read_specs(config) == "(no specs yet)"


class TestBuildPromptGrounding:
    """The design and tasks prompts must carry the requirement text."""

    _CONTEXT = {
        "template": "TEMPLATE",
        "instruction": "INSTRUCTION",
        "context": "PROJECT",
        "dependencies": {"proposal": "proposal.md", "design": "design.md"},
    }

    def test_design_prompt_includes_requirements(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: login-flow-marker"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        prompt = _build_prompt("design", self._CONTEXT, config)
        assert "SCENARIO: login-flow-marker" in prompt
        assert "(no specs yet)" not in prompt

    def test_tasks_prompt_includes_requirements(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: login-flow-marker"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        (config.change_dir / "design.md").write_text("design body")
        prompt = _build_prompt("tasks", self._CONTEXT, config)
        assert "SCENARIO: login-flow-marker" in prompt
        assert "(no specs yet)" not in prompt


class TestDesignAudit:
    @pytest.mark.asyncio
    async def test_audit_prompt_includes_requirements(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: audit-req-marker"})
        change_dir = config.change_dir
        (change_dir / "proposal.md").write_text("proposal")
        (change_dir / "design.md").write_text("design")
        (change_dir / "tasks.md").write_text("## 1. Do the thing")

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = "[]"
            await run_design_audit(change_dir, config)

        prompt = mock_llm.call_args[0][0]
        assert "SCENARIO: audit-req-marker" in prompt
        assert "(no specs)" not in prompt


class TestTasksPromptVerticalSlices:
    """B4: the tasks prompt must demand vertical slices, not layer-per-block."""

    def test_demands_vertical_slices(self):
        assert "vertical slice" in TASKS_PROMPT

    def test_forbids_layer_per_block(self):
        assert "Layer-per-block decomposition is FORBIDDEN" in TASKS_PROMPT

    def test_final_integration_block_removed(self):
        assert "Final block should be a wiring/integration block" not in TASKS_PROMPT
        assert "a final integration block is a smell" in TASKS_PROMPT

    def test_keeps_satisfies_and_dependency_ordering(self):
        assert '"Satisfies:" line MUST reference specific spec files' in TASKS_PROMPT
        assert "Order blocks by dependency" in TASKS_PROMPT

    def test_first_pass_has_no_findings_placeholder(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "req"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        (config.change_dir / "design.md").write_text("design body")
        context = {
            "template": "TEMPLATE",
            "instruction": "INSTRUCTION",
            "context": "PROJECT",
            "dependencies": {"proposal": "proposal.md", "design": "design.md"},
        }
        prompt = _build_prompt("tasks", context, config)
        assert "(none — first pass)" in prompt

    def test_regeneration_pass_injects_findings(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "req"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        (config.change_dir / "design.md").write_text("design body")
        context = {
            "template": "TEMPLATE",
            "instruction": "INSTRUCTION",
            "context": "PROJECT",
            "dependencies": {"proposal": "proposal.md", "design": "design.md"},
        }
        prompt = _build_prompt(
            "tasks", context, config,
            audit_findings="LAYERING-FINDING-MARKER",
        )
        assert "LAYERING-FINDING-MARKER" in prompt
        assert "(none — first pass)" not in prompt


class TestTasksAudit:
    """B4: adversarial shape audit over the generated tasks.md."""

    def _change_dir(self, tmp_path, tasks_text: str) -> Path:
        change_dir = tmp_path / "changes" / "feat"
        change_dir.mkdir(parents=True)
        (change_dir / "design.md").write_text("DESIGN-MARKER")
        (change_dir / "tasks.md").write_text(tasks_text)
        return change_dir

    @pytest.mark.asyncio
    async def test_prompt_includes_tasks_and_design(self, tmp_path):
        change_dir = self._change_dir(tmp_path, "## 1. TASKS-MARKER block")
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = "[]"
            findings = await run_tasks_audit(change_dir, config)

        prompt = mock_llm.call_args[0][0]
        assert "TASKS-MARKER" in prompt
        assert "DESIGN-MARKER" in prompt
        assert "vertical slice" in prompt
        assert findings == []

    @pytest.mark.asyncio
    async def test_returns_layering_findings(self, tmp_path):
        change_dir = self._change_dir(
            tmp_path, "## 1. Database schema\n## 2. API layer\n## 3. Wiring"
        )
        config = MagicMock(model="test-model")
        raw = (
            '[{"category": "horizontal-decomposition", "severity": "critical", '
            '"description": "blocks are layers", "suggestion": "re-slice"}]'
        )

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = raw
            findings = await run_tasks_audit(change_dir, config)

        assert len(findings) == 1
        assert findings[0]["category"] == "horizontal-decomposition"

    @pytest.mark.asyncio
    async def test_non_json_response_returns_empty(self, tmp_path):
        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = "not json at all"
            findings = await run_tasks_audit(change_dir, config)

        assert findings == []

    @pytest.mark.asyncio
    async def test_json_object_response_warns_and_returns_empty(self, tmp_path, caplog):
        """A JSON object (model wrapped the findings) must not pass silently
        as 'no findings' — it returns [] but leaves a warning in the log."""
        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm, caplog.at_level("WARNING"):
            mock_llm.return_value = '{"findings": [{"category": "horizontal-decomposition"}]}'
            findings = await run_tasks_audit(change_dir, config)

        assert findings == []
        assert any(
            "instead of a list" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_non_dict_list_items_are_filtered(self, tmp_path):
        """A JSON list of strings must not leak out — downstream calls .get()."""
        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = '["Blocks 1-3 are layers", "re-slice them"]'
            findings = await run_tasks_audit(change_dir, config)

        assert findings == []

    @pytest.mark.asyncio
    async def test_mixed_list_keeps_only_dict_findings(self, tmp_path):
        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        config = MagicMock(model="test-model")
        raw = (
            '["stray string", {"category": "horizontal-decomposition", '
            '"severity": "critical", "description": "layers", "suggestion": "fix"}]'
        )

        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = raw
            findings = await run_tasks_audit(change_dir, config)

        assert len(findings) == 1
        assert findings[0]["category"] == "horizontal-decomposition"
