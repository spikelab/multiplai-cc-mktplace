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
            "design", self._CONTEXT, config,
            reference_docs="### Reference: uv-python.md\nAlways use uv, never pip.",
        )
        assert "Always use uv, never pip." in prompt
        assert "(none available)" not in prompt

    def test_tasks_prompt_inlines_the_docs(self, tmp_path):
        config = _make_config(tmp_path, {"auth": "SCENARIO: x"})
        (config.change_dir / "proposal.md").write_text("proposal body")
        (config.change_dir / "design.md").write_text("design body")
        prompt = _build_prompt(
            "tasks", self._CONTEXT, config,
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
            asyncio.run(generate_artifact(
                "design", self._CONTEXT, config,
                reference_docs="### Reference: house.md\nHOUSE-RULE-MARKER",
            ))
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
    async def test_non_json_response_returns_empty(self, tmp_path):
        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text("not json at all")
            findings = await run_tasks_audit(change_dir, config)

        assert findings == []

    @pytest.mark.asyncio
    async def test_json_object_response_warns_and_returns_empty(self, tmp_path, caplog):
        """A JSON object (model wrapped the findings) must not pass silently
        as 'no findings' — it returns [] but leaves a warning in the log."""
        change_dir = self._change_dir(tmp_path, "## 1. Slice")
        config = MagicMock(model="test-model")

        with patch(
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm, caplog.at_level("WARNING"):
            mock_llm.return_value = _agent_text('{"findings": [{"category": "horizontal-decomposition"}]}')
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
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text('["Blocks 1-3 are layers", "re-slice them"]')
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
            "build_pipeline.llm_steps.spec_steps.agent_call", new_callable=AsyncMock
        ) as mock_llm:
            mock_llm.return_value = _agent_text(raw)
            findings = await run_tasks_audit(change_dir, config)

        assert len(findings) == 1
        assert findings[0]["category"] == "horizontal-decomposition"


class TestEveryBlockAddsBehaviour:
    """Epic e2e attempt 1, Defect 1: a block whose tests pass before it is
    built fails the TDD RED gate and the whole build. The rule is stated where
    blocks are written, and checked where they are audited."""

    def test_the_tasks_prompt_requires_behaviour_that_fails_first(self):
        assert "Every block MUST add behaviour" in TASKS_PROMPT
        assert "RED gate" in TASKS_PROMPT

    def test_the_tasks_prompt_forbids_confirm_and_verify_blocks(self):
        assert "Never write a block that only confirms, verifies" in TASKS_PROMPT

    @pytest.mark.asyncio
    async def test_the_shape_audit_asks_for_and_returns_no_new_behaviour(
        self, tmp_path,
    ):
        """The audit's prompt carries the rule with its severity and remedy,
        and a finding the model reports under it comes back to the caller,
        which turns every finding into the one rewrite."""
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
        section = prompt[prompt.index("## Blocks That Add No Behaviour"):]
        section = section[:section.index("\n## ", 1)]
        assert '(category "no-new-behaviour", severity "critical")' in section
        assert "tests would pass" in section
        assert "into the block that builds the behaviour" in section
        assert [f["category"] for f in findings] == ["no-new-behaviour"]


# --- PR 4 of the reverse-specs plan: spec generation reads specs/registry --------------


def _registry_config(tmp_path, req_files: dict[str, str], registry: dict[str, str]) -> BuildConfig:
    config = _make_config(tmp_path, req_files)
    (config.specs_dir / "registry").mkdir(parents=True, exist_ok=True)
    for name, text in registry.items():
        (config.specs_dir / "registry" / f"{name}.md").write_text(text)
    (config.change_dir / "proposal.md").write_text("proposal body")
    (config.change_dir / "design.md").write_text("design body")
    return config


def _registry_context(config, capability: str = "billing") -> dict:
    return {**TestBuildPromptGrounding._CONTEXT, "capability_name": capability,
            "registry": sorted(p.stem for p in (config.specs_dir / "registry").glob("*.md"))}


class TestRegistryInTheContext:
    def test_artifact_context_lists_the_registry_stems_sorted(self, tmp_path):
        from build_pipeline.change_manager import ChangeManager

        cm = ChangeManager(tmp_path / "specs")
        change = cm.create_change("feat")
        cm.registry_dir.mkdir(parents=True, exist_ok=True)
        for name in ("users", "auth", "billing"):
            (cm.registry_dir / f"{name}.md").write_text("x")
        (cm.registry_dir / "notes.txt").write_text("not a capability")
        assert cm.artifact_context(change, "requirements")["registry"] == ["auth", "billing", "users"]

    def test_an_empty_registry_is_an_empty_list(self, tmp_path):
        from build_pipeline.change_manager import ChangeManager

        cm = ChangeManager(tmp_path / "specs")
        assert cm.artifact_context(cm.create_change("feat"), "design")["registry"] == []


class TestRegistryInThePrompts:
    REGISTRY = {"billing": "### Requirement: BILLING-TODAY-MARKER\n",
                "auth": "### Requirement: AUTH-TODAY-MARKER\n",
                "users": "### Requirement: USERS-TODAY-MARKER\n"}

    def test_requirements_prompt_carries_the_same_name_file_and_the_modified_rule(self, tmp_path):
        config = _registry_config(tmp_path, {}, self.REGISTRY)
        prompt = _build_prompt("requirements", _registry_context(config), config)
        section = prompt.split("## Current behaviour (specs/registry)", 1)[1]
        assert "### billing (specs/registry/billing.md)" in section
        assert "BILLING-TODAY-MARKER" in section
        assert "AUTH-TODAY-MARKER" not in section and "USERS-TODAY-MARKER" not in section
        assert "Other capabilities in specs/registry: auth, users" in section
        for heading in ("## MODIFIED Requirements", "## REMOVED Requirements", "## ADDED Requirements"):
            assert heading in section

    def test_a_new_capability_still_gets_the_list_and_the_rule(self, tmp_path):
        config = _registry_config(tmp_path, {}, self.REGISTRY)
        prompt = _build_prompt("requirements", _registry_context(config, "exports"), config)
        section = prompt.split("## Current behaviour (specs/registry)", 1)[1]
        assert "Other capabilities in specs/registry: auth, billing, users" in section
        assert "TODAY-MARKER" not in section
        assert "## MODIFIED Requirements" in section

    @pytest.mark.parametrize("artifact", ["design", "tasks"])
    def test_design_and_tasks_carry_the_change_capabilities_files(self, artifact, tmp_path):
        config = _registry_config(tmp_path, {"billing": "R", "users": "R"}, self.REGISTRY)
        prompt = _build_prompt(artifact, _registry_context(config), config)
        section = prompt.split("## Current behaviour (specs/registry)", 1)[1]
        assert "BILLING-TODAY-MARKER" in section and "USERS-TODAY-MARKER" in section
        assert "AUTH-TODAY-MARKER" not in section
        assert "Other capabilities in specs/registry: auth" in section

    @pytest.mark.parametrize("artifact", ["requirements", "design", "tasks"])
    def test_every_prompt_is_unchanged_with_an_empty_registry(self, artifact, tmp_path):
        """The section is appended, never a template placeholder, so an empty
        registry gives today's prompt byte for byte."""
        from build_pipeline.prompts.spec_generation import SPEC_PROMPT

        config = _registry_config(tmp_path, {"billing": "R"}, {})
        ctx = {**TestBuildPromptGrounding._CONTEXT, "capability_name": "billing"}
        today = {
            "requirements": SPEC_PROMPT.format(
                project_context="PROJECT", proposal_content="proposal body", capability_name="billing",
                instruction="INSTRUCTION", template="TEMPLATE"),
            "design": DESIGN_PROMPT.format(
                project_context="PROJECT", proposal_content="proposal body",
                specs_content=_read_specs(config), codebase_analysis="(new project)",
                reference_docs="(none available)", audit_findings="(none — first pass)",
                instruction="INSTRUCTION", template="TEMPLATE"),
            "tasks": TASKS_PROMPT.format(
                project_context="PROJECT", proposal_content="proposal body",
                specs_content=_read_specs(config), design_content="design body",
                granularity=config.task_granularity, unknowns_content="(none detected)",
                reference_docs="(none available)", audit_findings="(none — first pass)",
                instruction="INSTRUCTION", template="TEMPLATE"),
        }[artifact]
        assert _build_prompt(artifact, {**ctx, "registry": []}, config) == today
        assert _build_prompt(artifact, ctx, config) == today

    def test_the_cap_drops_whole_files_from_the_end_and_names_them(self, tmp_path):
        from build_pipeline.llm_steps.spec_steps import REGISTRY_TEXT_LIMIT, _read_registry

        assert REGISTRY_TEXT_LIMIT == 60_000
        big = {name: f"### Requirement: {name.upper()}-START\n" + "x" * 25_000 + f"\n{name.upper()}-END\n"
               for name in ("aaa", "bbb", "ccc")}
        config = _registry_config(tmp_path, {}, big)
        text = _read_registry(config, _registry_context(config), ["aaa", "bbb", "ccc"])
        assert len(text) <= 60_000
        assert "AAA-END" in text and "BBB-END" in text
        assert "CCC-END" not in text
        assert "ccc (specs/registry/ccc.md; requirements: CCC-START)" in text.split("BBB-END", 1)[1]

    def test_read_registry_skips_names_with_no_file(self, tmp_path):
        from build_pipeline.llm_steps.spec_steps import _read_registry

        config = _registry_config(tmp_path, {}, self.REGISTRY)
        text = _read_registry(config, _registry_context(config), ["billing", "nope"])
        assert "### billing (specs/registry/billing.md)" in text
        assert "nope" not in text
        assert "auth, users" in text


class TestRegistryInTheProposal:
    """The proposal picks the capability names the later prompts look up in the
    registry, so it is told the registry's names (review round 1 of #345)."""

    def test_the_proposal_prompt_names_the_registry_capabilities(self, tmp_path):
        config = _registry_config(tmp_path, {}, TestRegistryInThePrompts.REGISTRY)
        prompt = _build_prompt("proposal", _registry_context(config), config)
        section = prompt.split("## Current behaviour (specs/registry)", 1)[1]
        assert "Capabilities in specs/registry: auth, billing, users" in section
        assert "### Modified Capabilities" in section and "### New Capabilities" in section
        assert "TODAY-MARKER" not in section

    def test_the_proposal_prompt_is_unchanged_with_an_empty_registry(self, tmp_path):
        from build_pipeline.prompts.spec_generation import PROPOSAL_PROMPT

        config = _registry_config(tmp_path, {}, {})
        ctx = TestBuildPromptGrounding._CONTEXT
        today = PROPOSAL_PROMPT.format(project_context="PROJECT", interview_summary="(none provided)",
                                       research="(no research conducted)", instruction="INSTRUCTION",
                                       template="TEMPLATE")
        assert _build_prompt("proposal", {**ctx, "registry": []}, config) == today
        assert _build_prompt("proposal", ctx, config) == today


def test_a_file_left_out_by_the_cap_still_names_its_requirements(tmp_path):
    """The requirements prompt tells the model to reuse the registry's requirement
    names; a same-name file over the cap keeps those names in the prompt."""
    from build_pipeline.llm_steps.spec_steps import _read_registry

    big = "### Requirement: Billing totals\n" + "x" * 70_000 + "\n### Requirement: Refunds\nBODY-END\n"
    config = _registry_config(tmp_path, {}, {"billing": big})
    text = _read_registry(config, _registry_context(config), ["billing"])
    assert len(text) <= 60_000 and "BODY-END" not in text
    assert "billing (specs/registry/billing.md; requirements: Billing totals, Refunds)" in text


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


def test_the_proposal_list_of_capabilities_is_capped_too(tmp_path):
    from build_pipeline.llm_steps.spec_steps import REGISTRY_TEXT_LIMIT

    config = _registry_config(tmp_path, {}, {f"capability-{i:05d}": "x\n" for i in range(5_000)})
    prompt = _build_prompt("proposal", _registry_context(config), config)
    section = prompt.split("## Current behaviour (specs/registry)", 1)[1]
    names = section.split("Capabilities in specs/registry: ", 1)[1]
    assert len(names) <= REGISTRY_TEXT_LIMIT + 1
    assert names.rstrip("\n").endswith(f"(cut at {REGISTRY_TEXT_LIMIT:,} characters)")
