"""Regression tests: spec-generation steps ground prompts in requirements/.

These lock in the fix for the legacy specs/ readers. The only writer of
capability files is spec_generator._generate_requirements, which writes flat
change_dir/requirements/<capability>.md. Design/tasks/audit must read from
there — not the empty legacy change_dir/specs/*/spec.md layout.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.spec_steps import (
    _build_prompt,
    _read_specs,
    run_design_audit,
    run_tasks_audit,
)
from build_pipeline.models import AgentResult
from build_pipeline.prompts.spec_generation import DESIGN_PROMPT, TASKS_PROMPT


def _agent_text(text: str) -> AgentResult:
    """What a successful audit subagent hands back.

    The audits run as read-only subagents (`agent_call`), so their seam returns
    an AgentResult rather than a bare string. Their tool list is asserted in
    test_review_steps.py — here the result only has to carry the text.
    """
    return AgentResult(success=True, output=text)


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


class TestReferenceDocsInThePrompts:
    """The spec-generation system prompt forbids tools, so the reference docs
    have to travel inlined in the prompt or they do not reach the generator at
    all."""

    _CONTEXT = TestBuildPromptGrounding._CONTEXT

    def test_design_prompt_has_a_reference_docs_slot(self):
        assert "{reference_docs}" in DESIGN_PROMPT

    def test_tasks_prompt_has_a_reference_docs_slot(self):
        assert "{reference_docs}" in TASKS_PROMPT

    def test_design_prompt_inlines_the_docs(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: x"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        prompt = _build_prompt(
            "design",
            self._CONTEXT,
            config,
            reference_docs="### Reference: uv-python.md\nAlways use uv, never pip.",
        )
        assert "Always use uv, never pip." in prompt
        assert "(none available)" not in prompt

    def test_tasks_prompt_inlines_the_docs(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: x"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        (config.change_dir / "design.md").write_text("design body")
        prompt = _build_prompt(
            "tasks",
            self._CONTEXT,
            config,
            reference_docs="### Reference: uv-python.md\nAlways use uv, never pip.",
        )
        assert "Always use uv, never pip." in prompt

    def test_absent_docs_render_as_a_placeholder_not_an_empty_heading(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: x"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        prompt = _build_prompt("design", self._CONTEXT, config)
        assert "(none available)" in prompt

    def test_generate_artifact_passes_the_docs_through(self, tmp_path):
        """The parameter has to reach _build_prompt, not stop at the door."""
        import asyncio

        from build_pipeline.llm_steps.spec_steps import generate_artifact

        config = _make_config(tmp_path, {"auth": "SCENARIO: x"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        with patch(
            "build_pipeline.llm_steps.spec_steps.llm_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = "# design"
            asyncio.run(
                generate_artifact(
                    "design",
                    self._CONTEXT,
                    config,
                    reference_docs="### Reference: house.md\nHOUSE-RULE-MARKER",
                )
            )
        assert "HOUSE-RULE-MARKER" in mock_llm.call_args[0][0]


class TestDesignAudit:
    @pytest.mark.asyncio
    async def test_audit_prompt_includes_requirements(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: audit-req-marker"})
        change_dir = config.change_dir
        (change_dir / "proposal.md").write_text("proposal")
        (change_dir / "design.md").write_text("design")
        (change_dir / "tasks.md").write_text("## 1. Do the thing")

        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text("[]")
            await run_design_audit(change_dir, config)

        prompt = mock_llm.call_args[0][0]
        assert "SCENARIO: audit-req-marker" in prompt
        assert "(no specs)" not in prompt


class TestDesignAuditComponentScope:
    """A component is audited against its own plan.md, not the whole change.

    Real-model run, 2026-10-23: the audit of component `word-frequencies` read
    only the change's proposal (which promises `word_frequencies` and
    `top_words`), reported its sibling `top-words`' capability as missing
    (critical), and the epic stopped on spec-issues.md (exit 4)."""

    def _change(self, tmp_path) -> Path:
        root = tmp_path / "project" / "specs" / "changes" / "word-freq"
        root.mkdir(parents=True)
        (root / "proposal.md").write_text(
            "## Impact\nAdds word_frequencies and top_words.\n"
        )
        for comp, cap in (
            ("word-frequencies", "word-frequencies"),
            ("top-words", "top-words"),
        ):
            d = root / "components" / comp
            d.mkdir(parents=True)
            (d / "plan.md").write_text(f"# Plan: {comp}\nPLAN-MARKER-{comp}\n")
            (d / "specs.md").write_text(
                f"# Capability: {cap}\n\n## ADDED Requirements\n\n"
                "### Requirement: R\nThe system SHALL r.\n"
            )
            for name in ("design.md", "tasks.md"):
                (d / name).write_text("x")
        return root

    async def _prompt(self, change_dir: Path, tmp_path) -> str:
        config = BuildConfig(project_dir=tmp_path / "project", change_name="c")
        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text("[]")
            await run_design_audit(change_dir, config)
        return mock_llm.call_args[0][0]

    @pytest.mark.asyncio
    async def test_a_component_is_judged_against_its_plan(self, tmp_path):
        from build_pipeline.prompts.design_audit import (
            CAPABILITY_CHECK,
            COMPONENT_CAPABILITY_CHECK,
            COMPONENT_SCOPE,
        )

        root = self._change(tmp_path)
        prompt = await self._prompt(root / "components" / "word-frequencies", tmp_path)
        assert "PLAN-MARKER-word-frequencies" in prompt
        assert "PLAN-MARKER-top-words" not in prompt
        assert COMPONENT_SCOPE in prompt
        assert COMPONENT_CAPABILITY_CHECK in prompt
        assert CAPABILITY_CHECK not in prompt
        # Its own specs only: the sibling's capability is not shown as its.
        assert "### top-words" not in prompt

    @pytest.mark.asyncio
    async def test_a_change_with_its_plan_in_the_change_dir_keeps_the_proposal_check(
        self, tmp_path
    ):
        from build_pipeline.prompts.design_audit import (
            CAPABILITY_CHECK,
            COMPONENT_SCOPE,
        )

        root = tmp_path / "project" / "specs" / "changes" / "one"
        root.mkdir(parents=True)
        (root / "proposal.md").write_text("proposal")
        (root / "plan.md").write_text("PLAN-MARKER-one\n")
        (root / "specs.md").write_text(
            "# Capability: one\n\n## ADDED Requirements\n\n"
            "### Requirement: R\nThe system SHALL r.\n"
        )
        prompt = await self._prompt(root, tmp_path)
        assert "PLAN-MARKER-one" in prompt
        assert CAPABILITY_CHECK in prompt
        assert COMPONENT_SCOPE not in prompt


class TestSpecIssueLabels:
    """A requirement a design or its audit cannot follow is named as the
    change holds it: `specs.md#<capability>` with a `specs.md`, else
    `requirements/<capability>.md`."""

    def _component(self, tmp_path) -> BuildConfig:
        project = tmp_path / "project"
        root = project / "specs" / "changes" / "feat"
        component = root / "components" / "billing"
        component.mkdir(parents=True)
        (root / "proposal.md").write_text("proposal")
        (component / "plan.md").write_text("# Plan: billing\n")
        (component / "specs.md").write_text(
            "# Capability: billing\n\n## ADDED Requirements\n\n"
            "### Requirement: Invoices\nThe system SHALL list invoices.\n"
        )
        for name in ("design.md", "tasks.md"):
            (component / name).write_text("x")
        config = BuildConfig(project_dir=project, change_name="feat/billing")
        config.specs_dir = project / "specs"
        return config

    def _old(self, tmp_path) -> BuildConfig:
        config = _make_config(tmp_path, {"auth": "### Requirement: Log in\n"})
        for name in ("proposal.md", "design.md", "tasks.md"):
            (config.change_dir / name).write_text("x")
        return config

    def test_the_design_prompt_names_specs_md_sections(self, tmp_path):
        config = self._component(tmp_path)
        ctx = ChangeManager(config.specs_dir).artifact_context(
            config.change_dir, "design"
        )
        prompt = _build_prompt("design", ctx, config)
        assert "`SPEC_ISSUE: specs.md#<capability>: <requirement name>:" in prompt
        assert "requirements/<capability>.md" not in prompt

    def test_the_design_prompt_keeps_requirements_files_for_the_old_layout(
        self, tmp_path
    ):
        config = self._old(tmp_path)
        ctx = ChangeManager(config.specs_dir).artifact_context(
            config.change_dir, "design"
        )
        prompt = _build_prompt("design", ctx, config)
        assert (
            "`SPEC_ISSUE: requirements/<capability>.md: <requirement name>:" in prompt
        )
        assert "specs.md#" not in prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("layout", "label", "other"),
        [
            ("_component", "specs.md#<capability>", "requirements/<capability>.md"),
            ("_old", "requirements/<capability>.md", "specs.md#<capability>"),
        ],
    )
    async def test_the_design_audit_prompt_names_them_as_the_change_holds_them(
        self, tmp_path, layout, label, other
    ):
        config = getattr(self, layout)(tmp_path)
        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text("[]")
            await run_design_audit(config.change_dir, config)
        prompt = mock_llm.call_args[0][0]
        assert f"as `{label}: <requirement name>`" in prompt
        assert other not in prompt

    def test_a_specs_md_spec_issue_line_is_parsed(self):
        from build_pipeline.spec_generator import design_spec_issues

        line = "SPEC_ISSUE: specs.md#billing: Invoices: cannot page; allow it\n"
        assert [t for t, _ in design_spec_issues(line)] == [
            "Design: specs.md#billing: Invoices"
        ]


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
            "tasks",
            context,
            config,
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
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text("[]")
            findings = await run_tasks_audit(change_dir, config)

        prompt = mock_llm.call_args[0][0]
        assert "TASKS-MARKER" in prompt
        assert "DESIGN-MARKER" in prompt
        assert "vertical slice" in prompt
        assert findings == []

    @pytest.mark.asyncio
    async def test_prompt_includes_specs_md_capabilities(self, tmp_path):
        change_dir = self._change_dir(tmp_path, "## 1. TASKS-MARKER block")
        (change_dir / "specs.md").write_text(
            "# Capability: cap-a\n\n## ADDED Requirements\n\n"
            "### Requirement: Keep the form\nThe form keeps the text.\n\n"
            "#### Scenario: Wake from sleep\n- **WHEN** the laptop wakes\n"
            "- **THEN** the form still holds the text\n"
        )
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text("[]")
            await run_tasks_audit(change_dir, config)

        prompt = mock_llm.call_args[0][0]
        assert "### specs.md#cap-a" in prompt
        assert "Scenario: Wake from sleep" in prompt
        assert "(no requirements files)" not in prompt

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
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text(raw)
            findings = await run_tasks_audit(change_dir, config)

        assert len(findings) == 1
        assert findings[0]["category"] == "horizontal-decomposition"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '{"findings": []}',
            '["finding"]',
            "[{}]",
            '[{"severity": "unknown"}]',
        ],
    )
    async def test_unavailable_audit_evidence_blocks(self, tmp_path, audit, raw):
        from build_pipeline.sdk import LLMCallError

        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as call:
            call.return_value = _agent_text(raw)
            with pytest.raises(LLMCallError):
                await audit(change_dir, MagicMock(model="test-model"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
    async def test_failed_auditor_cannot_supply_passing_partial_output(
        self, tmp_path, audit
    ):
        from build_pipeline.models import AgentResult
        from build_pipeline.sdk import LLMCallError

        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as call:
            call.return_value = AgentResult(
                success=False, output="[]", error="timed out"
            )
            with pytest.raises(LLMCallError, match="timed out"):
                await audit(change_dir, MagicMock(model="test-model"))


class TestEveryBlockAddsBehaviour:
    """Epic e2e attempt 1, Defect 1: a block whose tests pass before it is
    built fails the TDD RED gate and the whole build. The rule is stated where
    blocks are written, and checked where they are audited."""

    def test_the_tasks_prompt_requires_behaviour_that_fails_first(self):
        assert "Every block MUST add behaviour" in TASKS_PROMPT
        assert "RED gate" in TASKS_PROMPT

    def test_the_tasks_prompt_puts_confirm_and_verify_criteria_in_the_building_block(
        self,
    ):
        assert "Never write a block" not in TASKS_PROMPT
        assert (
            "Put criteria that confirm, verify, pin or re-test what an earlier "
            "block delivers"
        ) in " ".join(TASKS_PROMPT.split())

    @pytest.mark.asyncio
    async def test_the_shape_audit_asks_for_and_returns_no_new_behaviour(
        self,
        tmp_path,
    ):
        """The audit's prompt carries the rule, its remedy and its severity
        (critical, as the plan review's scale says), and a finding the model
        reports under it comes back to the caller."""
        change_dir = tmp_path / "changes" / "feat"
        change_dir.mkdir(parents=True)
        (change_dir / "design.md").write_text("# Design")
        (change_dir / "tasks.md").write_text("## 1. Slice\n\n## 2. Pin it\n")
        raw = (
            '[{"category": "no-new-behaviour", "severity": "critical", '
            '"description": "Block 2 pins Block 1", "suggestion": "fold it in"}]'
        )

        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text(raw)
            findings = await run_tasks_audit(change_dir, config=MagicMock(model="m"))

        prompt = mock_llm.call_args[0][0]
        section = prompt[prompt.index("## Blocks That Add No Behaviour") :]
        section = section[: section.index("\n## ", 1)]
        assert '(category "no-new-behaviour")' in section
        assert "tests would pass before it is built is **critical**" in " ".join(
            section.split()
        )
        assert "tests would pass" in section
        assert "into the block that builds the behaviour" in section
        assert [f["category"] for f in findings] == ["no-new-behaviour"]


# --- PR 4 of the reverse-specs plan: spec generation reads specs/registry --------------


def _registry_config(
    tmp_path, req_files: dict[str, str], registry: dict[str, str]
) -> BuildConfig:
    config = _make_config(tmp_path, req_files)
    (config.specs_dir / "registry").mkdir(parents=True, exist_ok=True)
    for name, text in registry.items():
        (config.specs_dir / "registry" / f"{name}.md").write_text(text)
    (config.change_dir / "proposal.md").write_text("proposal body")
    (config.change_dir / "design.md").write_text("design body")
    return config


def _registry_context(config, capability: str = "billing") -> dict:
    return {
        **TestBuildPromptGrounding._CONTEXT,
        "capability_name": capability,
        "registry": sorted(
            p.stem for p in (config.specs_dir / "registry").glob("*.md")
        ),
    }


class TestRegistryInTheContext:
    def test_artifact_context_lists_the_registry_stems_sorted(self, tmp_path):
        from build_pipeline.change_manager import ChangeManager

        cm = ChangeManager(tmp_path / "specs")
        change = cm.create_change("feat")
        cm.registry_dir.mkdir(parents=True, exist_ok=True)
        for name in ("users", "auth", "billing"):
            (cm.registry_dir / f"{name}.md").write_text("x")
        (cm.registry_dir / "notes.txt").write_text("not a capability")
        assert cm.artifact_context(change, "design")["registry"] == [
            "auth",
            "billing",
            "users",
        ]

    def test_an_empty_registry_is_an_empty_list(self, tmp_path):
        from build_pipeline.change_manager import ChangeManager

        cm = ChangeManager(tmp_path / "specs")
        assert cm.artifact_context(cm.create_change("feat"), "design")["registry"] == []


class TestRegistryInThePrompts:
    REGISTRY = {
        "billing": "### Requirement: BILLING-TODAY-MARKER\n",
        "auth": "### Requirement: AUTH-TODAY-MARKER\n",
        "users": "### Requirement: USERS-TODAY-MARKER\n",
    }

    @pytest.mark.parametrize("artifact", ["design", "tasks"])
    def test_design_and_tasks_carry_the_change_capabilities_files(
        self, artifact, tmp_path
    ):
        config = _registry_config(
            tmp_path, {"billing": "R", "users": "R"}, self.REGISTRY
        )
        prompt = _build_prompt(artifact, _registry_context(config), config)
        section = prompt.split("## Current behaviour (specs/registry)", 1)[1]
        assert "BILLING-TODAY-MARKER" in section and "USERS-TODAY-MARKER" in section
        assert "AUTH-TODAY-MARKER" not in section
        assert "Other capabilities in specs/registry: auth" in section

    @pytest.mark.parametrize("artifact", ["design", "tasks"])
    def test_design_and_tasks_get_the_components_plan(self, artifact, tmp_path):
        project = tmp_path / "project"
        root = project / "specs" / "changes" / "feat"
        component = root / "components" / "billing"
        component.mkdir(parents=True)
        (root / "proposal.md").write_text("PROPOSAL-MARKER")
        (component / "plan.md").write_text(
            "# Plan: billing\n\n## Out of scope\n- OUT-OF-SCOPE-MARKER\n\n"
            "## Done means\n1. DONE-MEANS-MARKER\n"
        )
        (component / "specs.md").write_text(
            "# Capability: billing\n\n## ADDED Requirements\n\n"
            "### Requirement: Invoices\nSPEC-MARKER\n"
        )
        (component / "design.md").write_text("design body")
        config = BuildConfig(project_dir=project, change_name="feat/billing")
        config.specs_dir = project / "specs"
        assert config.change_dir == component
        ctx = ChangeManager(config.specs_dir).artifact_context(component, artifact)

        prompt = _build_prompt(artifact, ctx, config)

        plan = prompt.split("## Plan — what this builds", 1)[1].split("\n## Specs", 1)[
            0
        ]
        assert "OUT-OF-SCOPE-MARKER" in plan and "DONE-MEANS-MARKER" in plan
        assert "PROPOSAL-MARKER" in prompt and "SPEC-MARKER" in prompt
        assert (
            prompt.index("## Proposal")
            < prompt.index("## Plan — ")
            < prompt.index("## Specs")
        )

    @pytest.mark.parametrize("artifact", ["design", "tasks"])
    def test_every_prompt_is_unchanged_with_an_empty_registry(self, artifact, tmp_path):
        """The section is appended, never a template placeholder, so an empty
        registry gives today's prompt byte for byte."""
        config = _registry_config(tmp_path, {"billing": "R"}, {})
        ctx = {**TestBuildPromptGrounding._CONTEXT, "capability_name": "billing"}
        today = {
            "design": DESIGN_PROMPT.format(
                project_context="PROJECT",
                proposal_content="proposal body",
                plan_content="(no plan.md)",
                specs_content=_read_specs(config),
                spec_ref="requirements/<capability>.md",
                codebase_analysis="(new project)",
                research="(no research conducted)",
                reference_docs="(none available)",
                audit_findings="(none — first pass)",
                instruction="INSTRUCTION",
                template="TEMPLATE",
            ),
            "tasks": TASKS_PROMPT.format(
                project_context="PROJECT",
                proposal_content="proposal body",
                plan_content="(no plan.md)",
                specs_content=_read_specs(config),
                design_content="design body",
                use_cases_content="(not available)",
                granularity=config.task_granularity,
                unknowns_content="(none detected)",
                reference_docs="(none available)",
                audit_findings="(none — first pass)",
                instruction="INSTRUCTION",
                template="TEMPLATE",
            ),
        }[artifact]
        assert _build_prompt(artifact, {**ctx, "registry": []}, config) == today
        assert _build_prompt(artifact, ctx, config) == today

    def test_the_cap_drops_whole_files_from_the_end_and_names_them(self, tmp_path):
        from build_pipeline.llm_steps.spec_steps import (
            REGISTRY_TEXT_LIMIT,
            _read_registry,
        )

        assert REGISTRY_TEXT_LIMIT == 60_000
        big = {
            name: f"### Requirement: {name.upper()}-START\n"
            + "x" * 25_000
            + f"\n{name.upper()}-END\n"
            for name in ("aaa", "bbb", "ccc")
        }
        config = _registry_config(tmp_path, {}, big)
        text = _read_registry(config, _registry_context(config), ["aaa", "bbb", "ccc"])
        assert len(text) <= 60_000
        assert "AAA-END" in text and "BBB-END" in text
        assert "CCC-END" not in text
        assert (
            "ccc (specs/registry/ccc.md; requirements: CCC-START)"
            in text.split("BBB-END", 1)[1]
        )

    def test_read_registry_skips_names_with_no_file(self, tmp_path):
        from build_pipeline.llm_steps.spec_steps import _read_registry

        config = _registry_config(tmp_path, {}, self.REGISTRY)
        text = _read_registry(config, _registry_context(config), ["billing", "nope"])
        assert "### billing (specs/registry/billing.md)" in text
        assert "nope" not in text
        assert "auth, users" in text


def test_a_file_left_out_by_the_cap_still_names_its_requirements(tmp_path):
    """pm-spec's requirements reuse the registry's requirement names; a
    same-name file over the cap keeps those names in the design prompt."""
    from build_pipeline.llm_steps.spec_steps import _read_registry

    big = (
        "### Requirement: Billing totals\n"
        + "x" * 70_000
        + "\n### Requirement: Refunds\nBODY-END\n"
    )
    config = _registry_config(tmp_path, {}, {"billing": big})
    text = _read_registry(config, _registry_context(config), ["billing"])
    assert len(text) <= 60_000 and "BODY-END" not in text
    assert (
        "billing (specs/registry/billing.md; requirements: Billing totals, Refunds)"
        in text
    )


def test_the_cap_holds_when_the_names_alone_are_over_it(tmp_path):
    """Thousands of requirement names in a left-out file, and thousands of other
    capabilities: the text still stays within the cap, and says it was cut."""
    from build_pipeline.llm_steps.spec_steps import REGISTRY_TEXT_LIMIT, _read_registry

    big = "".join(f"### Requirement: Requirement number {i}\nx\n" for i in range(4_000))
    others = {f"capability-{i:05d}": "### Requirement: R\n" for i in range(3_000)}
    config = _registry_config(tmp_path, {}, {"billing": big, **others})
    text = _read_registry(config, _registry_context(config), ["billing"])
    assert len(text) <= REGISTRY_TEXT_LIMIT
    assert text.endswith(f"(cut at {REGISTRY_TEXT_LIMIT:,} characters)")


@pytest.mark.asyncio
@pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
async def test_audit_severity_is_canonical_for_blocking_consumers(tmp_path, audit):
    raw = '[{"category":"coverage","severity":"MAJOR","description":"missing behavior","suggestion":"add behavior"}]'
    with patch(
        "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
    ) as call:
        call.return_value = _agent_text(raw)
        findings = await audit(tmp_path, MagicMock(model="test-model"))
    assert findings[0]["severity"] == "major"


PRIOR = [
    {
        "finding_id": "DA-F1",
        "severity": "major",
        "category": "coverage",
        "text": "LISTED-MARKER gap",
    }
]
FINDING = '{"category":"coverage","severity":"critical","description":"new gap","suggestion":"fix it"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
async def test_prior_assessments_are_read_from_the_recheck_object(tmp_path, audit):
    raw = (
        '{"findings": [' + FINDING + '], "prior_assessments": '
        '[{"finding_id": "DA-F1", "status": "still_present", "evidence": "Block 2 still lacks it"}]}'
    )
    with patch(
        "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
    ) as call:
        call.return_value = _agent_text(raw)
        findings = await audit(tmp_path, MagicMock(model="test-model"), prior=PRIOR)
    assert [f["description"] for f in findings] == ["new gap"]
    assert [
        (a.finding_id, a.status, a.evidence) for a in findings.prior_assessments
    ] == [("DA-F1", "still_present", "Block 2 still lacks it")]
    prompt = call.await_args.args[0]
    assert "## Findings the rewrite was asked to fix" in prompt
    assert "DA-F1 [major] (coverage) LISTED-MARKER gap" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
async def test_prior_assessments_absent_from_a_bare_list_recheck(tmp_path, audit):
    with patch(
        "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
    ) as call:
        call.return_value = _agent_text("[" + FINDING + "]")
        findings = await audit(tmp_path, MagicMock(model="test-model"), prior=PRIOR)
    assert len(findings) == 1
    assert findings.prior_assessments == []


@pytest.mark.asyncio
@pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
async def test_prior_assessments_unknown_status_is_cannot_verify(tmp_path, audit):
    raw = '{"findings": [], "prior_assessments": [{"finding_id": "DA-F1", "status": "fixed, I think"}]}'
    with patch(
        "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
    ) as call:
        call.return_value = _agent_text(raw)
        findings = await audit(tmp_path, MagicMock(model="test-model"), prior=PRIOR)
    assert findings.prior_assessments[0].status == "cannot_verify"


@pytest.mark.asyncio
@pytest.mark.parametrize("audit", [run_design_audit, run_tasks_audit])
async def test_prior_assessments_section_left_out_of_a_first_audit(tmp_path, audit):
    with patch(
        "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
    ) as call:
        call.return_value = _agent_text("[]")
        findings = await audit(tmp_path, MagicMock(model="test-model"))
    assert findings.prior_assessments == []
    assert "Findings the rewrite was asked to fix" not in call.await_args.args[0]
