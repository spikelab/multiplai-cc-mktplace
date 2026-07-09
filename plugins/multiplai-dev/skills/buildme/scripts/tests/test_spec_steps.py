"""Regression tests: spec-generation steps ground prompts in requirements/.

These lock in the fix for the legacy specs/ readers. The only writer of
capability files is spec_generator._generate_requirements, which writes flat
change_dir/requirements/<capability>.md. Design/tasks/audit must read from
there — not the empty legacy change_dir/specs/*/spec.md layout.
"""

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.spec_steps import (
    _build_prompt,
    _read_specs,
    run_design_audit,
)


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
