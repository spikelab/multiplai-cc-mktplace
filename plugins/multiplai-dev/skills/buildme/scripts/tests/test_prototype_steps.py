"""Tests for the prototype-first stage — agent run, write boundary, feedback pass."""

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.prototype_steps import (
    PROTOTYPE_TOOLS,
    apply_prototype_findings,
    primary_prototype_artifact,
    prototype_findings_text,
    read_prototype_notes,
    run_prototype,
)
from build_pipeline.models import AgentResult

NOTES_CLEAN = (
    "PROVES: the report fits on one page with four columns\n"
    "DISPROVES: none\n"
    "OPEN_QUESTIONS: none\n"
    "STATUS: DONE\n"
)
NOTES_WITH_DISPROVES = (
    "PROVES: the report fits on one page\n"
    "DISPROVES: the per-row total column cannot be computed without the tax rate\n"
    "OPEN_QUESTIONS: none\n"
    "STATUS: DONE_WITH_CONCERNS\n"
)
NOTES_WITH_QUESTIONS = (
    "PROVES: the CLI transcript shape\n"
    "DISPROVES: none\n"
    "OPEN_QUESTIONS: should --json be the default output mode?\n"
    "STATUS: DONE\n"
)


def make_config(tmp_path, change_name="proto-change"):
    config = BuildConfig(project_dir=tmp_path, change_name=change_name)
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    cm.create_change(change_name)
    (config.change_dir / "proposal.md").write_text("## Why\nUsers need a weekly report.")
    (config.change_dir / "design.md").write_text("## Decisions\nRender as HTML.")
    (config.change_dir / "tasks.md").write_text("## 1. Block\n- [ ] 1.1 do it")
    return config


def agent_writing(config, notes=NOTES_CLEAN, artifact="mockup.html", files_changed=None):
    """An agent_call stand-in that writes into the prototype directory."""

    async def _call(prompt, **kwargs):
        proto = config.prototype_dir
        proto.mkdir(parents=True, exist_ok=True)
        if artifact:
            (proto / artifact).write_text("<html><body>report</body></html>")
        (proto / "NOTES.md").write_text(notes)
        return AgentResult(
            success=True,
            output="done",
            files_changed=files_changed if files_changed is not None
            else [str(proto / "NOTES.md")],
        )

    return AsyncMock(side_effect=_call)


class TestRunPrototype:
    @pytest.mark.asyncio
    async def test_produces_artifact_and_passes_gate(self, tmp_path):
        config = make_config(tmp_path)
        mock_agent = agent_writing(config)
        with patch("build_pipeline.llm_steps.prototype_steps.agent_call", mock_agent):
            result = await run_prototype(config)

        assert result.passed, result.reason
        assert mock_agent.await_count == 1
        kwargs = mock_agent.await_args.kwargs
        assert kwargs["allowed_tools"] == PROTOTYPE_TOOLS
        assert kwargs["cwd"] == str(config.prototype_dir)
        assert (config.prototype_dir / "mockup.html").exists()

    @pytest.mark.asyncio
    async def test_retries_once_then_fails_the_phase(self, tmp_path):
        """An agent that produces nothing gets exactly one retry, then stops."""
        config = make_config(tmp_path)
        mock_agent = AsyncMock(
            return_value=AgentResult(success=True, output="I could not draw it")
        )
        with patch("build_pipeline.llm_steps.prototype_steps.agent_call", mock_agent):
            result = await run_prototype(config)

        assert not result.passed
        assert mock_agent.await_count == 2

    @pytest.mark.asyncio
    async def test_writes_outside_prototype_dir_are_rejected(self, tmp_path):
        """The write boundary is enforced in code, not just stated in the prompt."""
        config = make_config(tmp_path)
        stray = tmp_path / "src" / "app.py"
        mock_agent = agent_writing(config, files_changed=[str(stray)])
        with patch("build_pipeline.llm_steps.prototype_steps.agent_call", mock_agent):
            result = await run_prototype(config)

        assert not result.passed
        assert result.action == "prototype_write_boundary"
        assert str(stray) in result.reason
        # No retry after a boundary violation — it is not a quality problem.
        assert mock_agent.await_count == 1

    @pytest.mark.asyncio
    async def test_relative_paths_inside_the_dir_are_allowed(self, tmp_path):
        config = make_config(tmp_path)
        mock_agent = agent_writing(config, files_changed=["mockup.html", "NOTES.md"])
        with patch("build_pipeline.llm_steps.prototype_steps.agent_call", mock_agent):
            result = await run_prototype(config)
        assert result.passed, result.reason


class TestPrototypeFeedback:
    @pytest.mark.asyncio
    async def test_disproves_triggers_exactly_one_regeneration_each(self, tmp_path):
        """Done-means #6: non-empty DISPROVES regenerates design.md and tasks.md
        once — one call per artifact, no re-audit loop."""
        config = make_config(tmp_path)
        config.prototype_dir.mkdir(parents=True, exist_ok=True)
        (config.prototype_dir / "NOTES.md").write_text(NOTES_WITH_DISPROVES)
        (config.prototype_dir / "sample-output.md").write_text("| a | b |")

        mock_gen = AsyncMock(return_value="# regenerated\n")
        with patch("build_pipeline.llm_steps.spec_steps.generate_artifact", mock_gen):
            regenerated = await apply_prototype_findings(config)

        assert regenerated == 2
        assert mock_gen.await_count == 2
        assert [c.args[0] for c in mock_gen.await_args_list] == ["design", "tasks"]
        # The notes reach the regeneration as audit findings.
        findings = mock_gen.await_args_list[0].kwargs["audit_findings"]
        assert "tax rate" in findings
        assert config.design_path.read_text() == "# regenerated\n"
        assert config.tasks_path.read_text() == "# regenerated\n"

    @pytest.mark.asyncio
    async def test_open_questions_alone_trigger_regeneration(self, tmp_path):
        config = make_config(tmp_path)
        config.prototype_dir.mkdir(parents=True, exist_ok=True)
        (config.prototype_dir / "NOTES.md").write_text(NOTES_WITH_QUESTIONS)

        mock_gen = AsyncMock(return_value="# regenerated\n")
        with patch("build_pipeline.llm_steps.spec_steps.generate_artifact", mock_gen):
            regenerated = await apply_prototype_findings(config)

        assert regenerated == 2
        assert mock_gen.await_count == 2

    @pytest.mark.asyncio
    async def test_clean_notes_regenerate_nothing(self, tmp_path):
        config = make_config(tmp_path)
        config.prototype_dir.mkdir(parents=True, exist_ok=True)
        (config.prototype_dir / "NOTES.md").write_text(NOTES_CLEAN)
        before = config.design_path.read_text()

        mock_gen = AsyncMock(return_value="# regenerated\n")
        with patch("build_pipeline.llm_steps.spec_steps.generate_artifact", mock_gen):
            regenerated = await apply_prototype_findings(config)

        assert regenerated == 0
        assert mock_gen.await_count == 0
        assert config.design_path.read_text() == before

    @pytest.mark.asyncio
    async def test_regeneration_failure_is_non_fatal(self, tmp_path):
        config = make_config(tmp_path)
        config.prototype_dir.mkdir(parents=True, exist_ok=True)
        (config.prototype_dir / "NOTES.md").write_text(NOTES_WITH_DISPROVES)
        before = config.design_path.read_text()

        mock_gen = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch("build_pipeline.llm_steps.spec_steps.generate_artifact", mock_gen):
            regenerated = await apply_prototype_findings(config)

        assert regenerated == 0
        assert config.design_path.read_text() == before

    def test_findings_text_is_empty_for_clean_notes(self, tmp_path):
        proto = tmp_path / "prototype"
        proto.mkdir()
        (proto / "NOTES.md").write_text(NOTES_CLEAN)
        assert prototype_findings_text(read_prototype_notes(proto)) == ""

    def test_findings_text_carries_disproves_and_proves(self, tmp_path):
        proto = tmp_path / "prototype"
        proto.mkdir()
        (proto / "NOTES.md").write_text(NOTES_WITH_DISPROVES)
        text = prototype_findings_text(read_prototype_notes(proto))
        assert "tax rate" in text
        assert "one page" in text


class TestPrimaryPrototypeArtifact:
    def test_prefers_html(self, tmp_path):
        proto = tmp_path / "prototype"
        proto.mkdir()
        (proto / "NOTES.md").write_text("x")
        (proto / "data.json").write_text("{}")
        (proto / "mockup.html").write_text("<html>")
        assert primary_prototype_artifact(proto).name == "mockup.html"

    def test_falls_back_to_any_artifact(self, tmp_path):
        proto = tmp_path / "prototype"
        proto.mkdir()
        (proto / "NOTES.md").write_text("x")
        (proto / "sample-output.csv").write_text("a,b")
        assert primary_prototype_artifact(proto).name == "sample-output.csv"

    def test_none_when_only_notes(self, tmp_path):
        proto = tmp_path / "prototype"
        proto.mkdir()
        (proto / "NOTES.md").write_text("x")
        assert primary_prototype_artifact(proto) is None

    def test_none_when_missing(self, tmp_path):
        assert primary_prototype_artifact(tmp_path / "nope") is None
