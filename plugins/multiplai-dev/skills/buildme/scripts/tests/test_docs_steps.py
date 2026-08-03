"""Tests for the documentation phase step — prompt inputs, tools, non-fatality."""

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.docs_steps import DOCS_TOOLS, run_docs_update
from build_pipeline.models import AgentResult
from build_pipeline.state import BuildState

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
)


def make_config(tmp_path, change_name="docs-change"):
    config = BuildConfig(project_dir=tmp_path, change_name=change_name)
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    cm.create_change(change_name)
    return config


def make_state():
    return BuildState(change_name="docs-change", mode="only", tier="advanced")


class TestRunDocsUpdate:
    @pytest.mark.asyncio
    async def test_reports_the_files_the_agent_wrote(self, tmp_path):
        config = make_config(tmp_path)
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
        agent = AsyncMock(return_value=AgentResult(
            success=True,
            output="DOCS_IMPACT: README.md, CHANGELOG.md\nSTATUS: DONE\n",
        ))
        with patch("build_pipeline.llm_steps.docs_steps.agent_call", agent), \
             patch("build_pipeline.tdd_engine._capture_full_build_diff",
                   return_value=DIFF):
            files, gate = await run_docs_update(config, make_state())

        assert files == ["README.md", "CHANGELOG.md"]
        assert gate.passed is True
        assert gate.action is None

    @pytest.mark.asyncio
    async def test_agent_gets_the_documented_tools_budget_label_and_cwd(self, tmp_path):
        config = make_config(tmp_path)
        agent = AsyncMock(return_value=AgentResult(success=True, output="DOCS_IMPACT: none\n"))
        with patch("build_pipeline.llm_steps.docs_steps.agent_call", agent), \
             patch("build_pipeline.tdd_engine._capture_full_build_diff", return_value=""):
            await run_docs_update(config, make_state())

        kwargs = agent.await_args.kwargs
        assert kwargs["allowed_tools"] == DOCS_TOOLS
        assert kwargs["allowed_tools"] == ["Read", "Write", "Edit", "Glob", "Grep"]
        assert kwargs["budget_label"] == "docs_update"
        assert kwargs["effort"] == config.spec_effort
        assert kwargs["cwd"] == str(config.project_dir)
        # Skill / SlashCommand are never granted to a pipeline agent.
        assert "Skill" not in kwargs["allowed_tools"]
        assert "SlashCommand" not in kwargs["allowed_tools"]

    @pytest.mark.asyncio
    async def test_prompt_carries_the_diff_and_the_implementation_notes(self, tmp_path):
        config = make_config(tmp_path)
        (config.change_dir / "implementation-notes.md").write_text(
            "## Block 1 — Uploader (implementer)\n- SPEC_IMPACT: clarify\n"
        )
        agent = AsyncMock(return_value=AgentResult(success=True, output="DOCS_IMPACT: none\n"))
        with patch("build_pipeline.llm_steps.docs_steps.agent_call", agent), \
             patch("build_pipeline.tdd_engine._capture_full_build_diff",
                   return_value=DIFF):
            await run_docs_update(config, make_state())

        prompt = agent.await_args.args[0]
        assert "src/app.py" in prompt
        assert "SPEC_IMPACT: clarify" in prompt
        assert "DOCS_IMPACT:" in prompt, "the REQUIRED report slot must be asked for"
        assert str(config.project_dir) in prompt

    @pytest.mark.asyncio
    async def test_missing_notes_are_stated_not_faked(self, tmp_path):
        config = make_config(tmp_path)
        agent = AsyncMock(return_value=AgentResult(success=True, output="DOCS_IMPACT: none\n"))
        with patch("build_pipeline.llm_steps.docs_steps.agent_call", agent), \
             patch("build_pipeline.tdd_engine._capture_full_build_diff", return_value=""):
            await run_docs_update(config, make_state())

        assert "no implementation notes were recorded" in agent.await_args.args[0]

    @pytest.mark.asyncio
    async def test_llm_failure_is_non_fatal_and_still_reaches_the_gate(self, tmp_path):
        """The code is already built — a docs failure must not raise, and the
        deterministic warning is exactly what should fire in that case."""
        config = make_config(tmp_path)
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
        with patch("build_pipeline.llm_steps.docs_steps.agent_call",
                   AsyncMock(side_effect=RuntimeError("model down"))), \
             patch("build_pipeline.tdd_engine._capture_full_build_diff",
                   return_value=DIFF):
            files, gate = await run_docs_update(config, make_state())

        assert files == []
        assert gate.passed is True
        assert gate.action == "docs_may_be_stale"

    @pytest.mark.asyncio
    async def test_failed_agent_result_is_non_fatal(self, tmp_path):
        config = make_config(tmp_path)
        with patch("build_pipeline.llm_steps.docs_steps.agent_call",
                   AsyncMock(return_value=AgentResult(
                       success=False, output="", error="timed out"))), \
             patch("build_pipeline.tdd_engine._capture_full_build_diff", return_value=""):
            files, gate = await run_docs_update(config, make_state())

        assert files == []
        assert gate.passed is True

    @pytest.mark.asyncio
    async def test_no_state_means_no_diff_capture(self, tmp_path):
        config = make_config(tmp_path)
        agent = AsyncMock(return_value=AgentResult(success=True, output="DOCS_IMPACT: none\n"))
        with patch("build_pipeline.llm_steps.docs_steps.agent_call", agent):
            files, gate = await run_docs_update(config, None)

        assert files == []
        assert "no diff could be captured" in agent.await_args.args[0]
