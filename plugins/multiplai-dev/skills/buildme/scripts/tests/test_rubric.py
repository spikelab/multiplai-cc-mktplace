"""Tests for rubric — change type detection and rubric generation."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from build_pipeline.rubric import detect_change_type, generate_rubric


class TestDetectChangeType:
    def _make_change(self, tmp_path, proposal_text="", design_text=""):
        change_dir = tmp_path / "change"
        change_dir.mkdir()
        if proposal_text:
            (change_dir / "proposal.md").write_text(proposal_text)
        if design_text:
            (change_dir / "design.md").write_text(design_text)
        return change_dir

    def test_backend_from_api_keywords(self, tmp_path):
        change_dir = self._make_change(
            tmp_path,
            proposal_text=(
                "Add REST API endpoints for user management. "
                "Database migration for new schema. "
                "Backend server handles authentication. "
                "API endpoint returns JSON. "
                "Database model stores user data."
            ),
        )
        assert detect_change_type(change_dir) == "backend"

    def test_frontend_from_ui_keywords(self, tmp_path):
        change_dir = self._make_change(
            tmp_path,
            proposal_text=(
                "Build React component library with Tailwind CSS. "
                "Page layout with form elements and button components. "
                "Frontend browser UI rendering. "
                "HTML template with stylesheet."
            ),
        )
        assert detect_change_type(change_dir) == "frontend"

    def test_fullstack_from_mixed_keywords(self, tmp_path):
        change_dir = self._make_change(
            tmp_path,
            proposal_text=(
                "Build a React frontend with component library. "
                "Form handling, page routing, button components, UI elements. "
                "REST API backend with database. "
                "Server endpoints return JSON, model schema with migration. "
                "Backend authentication for API access."
            ),
        )
        assert detect_change_type(change_dir) == "fullstack"

    def test_infra_from_devops_keywords(self, tmp_path):
        change_dir = self._make_change(
            tmp_path,
            proposal_text=(
                "Terraform infrastructure for Kubernetes deployment. "
                "Docker containers with CI/CD pipeline. "
                "Monitoring and logging infrastructure. "
                "Helm charts for k8s deploy."
            ),
        )
        assert detect_change_type(change_dir) == "infra"

    def test_empty_change_defaults_to_backend(self, tmp_path):
        change_dir = tmp_path / "empty-change"
        change_dir.mkdir()
        assert detect_change_type(change_dir) == "backend"

    def test_reads_requirements_too(self, tmp_path):
        """Requirement text (not just proposal/design) feeds change-type detection.

        Regression for the legacy specs/ reader: with a neutral proposal, the
        frontend keywords living only in requirements/*.md must be what tips
        detection to frontend — proving _gather_text reads requirements/.
        """
        change_dir = tmp_path / "change"
        change_dir.mkdir()
        (change_dir / "proposal.md").write_text("Some generic proposal")
        req_dir = change_dir / "requirements"
        req_dir.mkdir(parents=True)
        (req_dir / "ui.md").write_text(
            "React component with Tailwind CSS and page layout. "
            "Frontend browser UI with form and button components. "
            "HTML stylesheet renders the DOM."
        )
        assert detect_change_type(change_dir) == "frontend"

    def test_minor_frontend_presence_stays_backend(self, tmp_path):
        """A single frontend keyword shouldn't flip to frontend/fullstack."""
        change_dir = self._make_change(
            tmp_path,
            proposal_text=(
                "REST API endpoint for data processing. "
                "Database migration and model changes. "
                "Backend queue worker for async jobs. "
                "One small UI notification."
            ),
        )
        result = detect_change_type(change_dir)
        assert result == "backend"


class TestRubricHasCoreDimensions:
    """The generated rubric must always include the 3 core dimensions."""

    @pytest.mark.asyncio
    async def test_rubric_generation_called_with_change_type(self, tmp_path):
        change_dir = tmp_path / "change"
        change_dir.mkdir()
        (change_dir / "proposal.md").write_text("REST API with database models and endpoint")
        (change_dir / "tasks.md").write_text("## 1. Setup\nCreate module")

        config = MagicMock()
        config.model = "test-model"

        rubric_content = (
            "# Evaluation Rubric\n"
            "## Code Architecture (weight: 2)\n"
            "| Score | Criteria |\n|---|---|\n| 5 | Excellent |\n| 3 | OK |\n| 1 | Bad |\n\n"
            "## Test Quality (weight: 1)\n"
            "| Score | Criteria |\n|---|---|\n| 5 | Excellent |\n| 3 | OK |\n| 1 | Bad |\n\n"
            "## Spec Compliance (weight: 3)\n"
            "| Score | Criteria |\n|---|---|\n| 5 | Excellent |\n| 3 | OK |\n| 1 | Bad |\n\n"
            "## API Design (weight: 2)\n"
            "| Score | Criteria |\n|---|---|\n| 5 | Excellent |\n| 3 | OK |\n| 1 | Bad |\n"
        )

        with patch("build_pipeline.rubric.llm_call", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = rubric_content
            result = await generate_rubric(change_dir, config)

            assert "Code Architecture" in result
            assert "Test Quality" in result
            assert "Spec Compliance" in result
            # Should have been called once
            mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rubric_prompt_includes_change_type(self, tmp_path):
        change_dir = tmp_path / "change"
        change_dir.mkdir()
        (change_dir / "proposal.md").write_text(
            "React component with Tailwind CSS and page layout. "
            "Frontend browser UI with form and button components."
        )
        (change_dir / "tasks.md").write_text("## 1. Build UI")

        config = MagicMock()
        config.model = "test-model"

        with patch("build_pipeline.rubric.llm_call", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "# Rubric\nContent"
            await generate_rubric(change_dir, config)

            # Check that the prompt included "frontend"
            call_args = mock_llm.call_args
            prompt = call_args[0][0]
            assert "frontend" in prompt

    @pytest.mark.asyncio
    async def test_rubric_prompt_includes_requirement_summaries(self, tmp_path):
        """Requirement text reaches the rubric prompt (regression for _gather_spec_summaries)."""
        change_dir = tmp_path / "change"
        change_dir.mkdir()
        (change_dir / "proposal.md").write_text("Some proposal")
        (change_dir / "tasks.md").write_text("## 1. Setup")
        req_dir = change_dir / "requirements"
        req_dir.mkdir(parents=True)
        (req_dir / "auth.md").write_text("Requirement: rubric-req-marker for login flow")

        config = MagicMock()
        config.model = "test-model"

        with patch("build_pipeline.rubric.llm_call", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "# Rubric\nContent"
            await generate_rubric(change_dir, config)

        prompt = mock_llm.call_args[0][0]
        assert "rubric-req-marker" in prompt
        assert "### auth" in prompt
        assert "(no specs)" not in prompt
