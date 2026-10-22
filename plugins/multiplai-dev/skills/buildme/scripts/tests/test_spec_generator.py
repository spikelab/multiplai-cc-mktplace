"""Tests for spec_generator — artifact pipeline orchestration."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from build_pipeline.change_manager import ARTIFACT_DAG, INPUTS, ChangeManager
from build_pipeline.change_manager import expected_capabilities as _extract_capabilities
from build_pipeline.models import ArtifactStatus, BuildPhase
from build_pipeline.spec_generator import (
    _generate_all_artifacts,
    _generate_single_artifact,
    _load_or_create_state,
    design_audit_findings_text,
    run_design_audit_stage,
    run_spec_generator,
    scan_placeholders,
)
from build_pipeline.state import BuildState, SpecGenState


def _resolved(*ids):
    """A recheck's answer that finds nothing new and calls each listed finding resolved."""
    from build_pipeline.llm_steps.spec_steps import AuditFindings
    from build_pipeline.models import PriorAssessment

    return AuditFindings(
        [], [PriorAssessment(finding_id=i, status="resolved") for i in ids]
    )


# --- Capability extraction ---


class TestExtractCapabilities:
    def test_extracts_backtick_names(self):
        text = """\
## Capabilities

### New Capabilities
- `user-auth`: Handle user authentication
- `data-export`: Export data to CSV
"""
        caps = _extract_capabilities(text)
        assert caps == ["user-auth", "data-export"]

    def test_empty_proposal(self):
        assert _extract_capabilities("") == []

    def test_no_capabilities_section(self):
        text = "## Why\nSome motivation\n## What Changes\nSome changes\n"
        assert _extract_capabilities(text) == []

    def test_ignores_non_backtick_items(self):
        text = "- regular item\n- `valid-cap`: desc\n- another item\n"
        caps = _extract_capabilities(text)
        assert caps == ["valid-cap"]

    def test_impact_dependencies_are_not_capabilities(self):
        """The proposal template asks for every dependency name in backticks
        under `## Impact`. Harvesting the whole file turned those into
        capabilities and wrote a requirements file for each."""
        text = """\
## Capabilities

### New Capabilities
- `password-reset`
- `rate-limiting`

## Impact

- `django-ses` for email sending
- `redis` for the rate-limit counters
"""
        assert _extract_capabilities(text) == ["password-reset", "rate-limiting"]

    def test_modified_capabilities_are_still_harvested(self):
        text = """\
## Capabilities

### New Capabilities
- `password-reset`

### Modified Capabilities
- `user-auth`

## Impact
- `django-ses`
"""
        assert _extract_capabilities(text) == ["password-reset", "user-auth"]

    def test_impact_is_skipped_even_without_a_capabilities_heading(self):
        text = "- `password-reset`\n\n## Impact\n- `django-ses`\n"
        assert _extract_capabilities(text) == ["password-reset"]


# --- State loading ---


class TestLoadOrCreateState:
    def test_creates_new_state(self, tmp_path):
        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = (
            tmp_path / "changes" / "test" / ".build-state.json"
        )
        config.change_name = "test"
        config.tier = "standard"

        state = _load_or_create_state(config)
        assert state.change_name == "test"
        assert state.phase == BuildPhase.SPEC_GENERATION
        assert state.spec_gen is not None
        assert state.spec_gen.completed_artifacts == []

    def test_resumes_existing_state(self, tmp_path):
        state_path = tmp_path / ".build-state.json"
        existing = BuildState(
            change_name="test",
            tier="standard",
            state_file=str(state_path),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(completed_artifacts=["design"]),
        )
        state_path.write_text(existing.model_dump_json(indent=2))

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = state_path
        config.change_name = "test"
        config.tier = "standard"

        state = _load_or_create_state(config)
        assert state.spec_gen.completed_artifacts == ["design"]


# --- Dependency ordering ---


class TestArtifactDependencyOrder:
    """buildme's DAG holds its own planning artifacts; pm-spec's documents are
    required inputs (`INPUTS`), checked and never generated."""

    def test_the_dag_holds_only_buildmes_artifacts(self):
        assert list(ARTIFACT_DAG) == ["design", "unknowns", "tasks", "rubric"]
        assert set(INPUTS) == {"proposal", "use-cases", "requirements"}
        assert not set(INPUTS) & set(ARTIFACT_DAG)

    def test_design_requires_the_proposal_and_requirements(self):
        assert set(ARTIFACT_DAG["design"]["requires"]) == {"proposal", "requirements"}

    def test_tasks_requires_requirements_design_and_use_cases(self):
        """The task breakdown is what delivers a use case, so it is written
        with the use cases already on disk."""
        reqs = ARTIFACT_DAG["tasks"]["requires"]
        assert {"requirements", "design", "use-cases", "unknowns"} <= set(reqs)

    def test_rubric_requires_tasks(self):
        assert "tasks" in ARTIFACT_DAG["rubric"]["requires"]

    def test_every_node_is_declared_after_the_nodes_it_requires(self):
        """`artifact_status` resolves each node against the statuses computed
        so far, so a node declared before its dependency reads that dependency
        as absent and reports BLOCKED forever. An input counts as declared."""
        seen: list[str] = list(INPUTS)
        for aid, spec in ARTIFACT_DAG.items():
            missing = [dep for dep in spec["requires"] if dep not in seen]
            assert not missing, f"{aid} is declared before {missing}"
            seen.append(aid)

    def test_full_ordering(self):
        """design -> unknowns -> tasks -> rubric."""
        order, done = [], set(INPUTS)
        for _ in range(10):
            for aid, spec in ARTIFACT_DAG.items():
                if aid not in done and all(dep in done for dep in spec["requires"]):
                    order.append(aid)
                    done.add(aid)
        assert order == ["design", "unknowns", "tasks", "rubric"]


class TestRequiredInputs:
    """Item 19: the proposal, use cases and requirements are checked, never
    generated."""

    def _config(self, tmp_path):
        from build_pipeline.config import BuildConfig

        config = BuildConfig(project_dir=tmp_path, change_name="c")
        config.specs_dir = tmp_path / "specs"
        config.change_dir.mkdir(parents=True)
        return config

    def test_without_inputs_nothing_is_ready(self, tmp_path):
        config = self._config(tmp_path)
        status = ChangeManager(config.specs_dir).artifact_status(config.change_dir)
        assert set(status.values()) == {ArtifactStatus.BLOCKED}

    @pytest.mark.parametrize(
        "missing", ["proposal.md", "use-cases.md", "requirements/fixture.md"]
    )
    def test_missing_inputs_names_each_absent_input(self, tmp_path, missing):
        from tests.spec_fixtures import write_specs

        config = self._config(tmp_path)
        write_specs(config.change_dir)
        (config.change_dir / missing).unlink()
        names = ChangeManager(config.specs_dir).missing_inputs(config.change_dir)
        expected = (
            "requirements/*.md" if missing.startswith("requirements") else missing
        )
        # The proposal names the capabilities, so without it the requirements
        # cannot be checked either.
        assert expected in names
        assert set(names) <= {expected, "requirements/*.md"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing", ["proposal.md", "use-cases.md", "requirements/fixture.md"]
    )
    async def test_generation_refuses_and_writes_nothing(self, tmp_path, missing):
        from tests.spec_fixtures import write_specs

        config = self._config(tmp_path)
        write_specs(config.change_dir)
        (config.change_dir / missing).unlink()
        before = sorted(
            p.relative_to(config.change_dir) for p in config.change_dir.rglob("*")
        )
        state = BuildState(
            change_name="c",
            tier="advanced",
            spec_gen=SpecGenState(),
            state_file=str(config.state_file_path()),
        )
        llm = AsyncMock(side_effect=AssertionError("no model call"))
        with (
            patch("build_pipeline.llm_steps.spec_steps.llm_call", llm),
            pytest.raises(
                RuntimeError, match="Required inputs missing.*pm-spec c first"
            ),
        ):
            await _generate_all_artifacts(
                ChangeManager(config.specs_dir), config.change_dir, config, state
            )
        assert (
            sorted(
                p.relative_to(config.change_dir) for p in config.change_dir.rglob("*")
            )
            == before
        )
        llm.assert_not_awaited()

    @pytest.mark.parametrize("artifact", ["proposal", "requirements", "use-cases"])
    def test_an_input_is_not_an_artifact_buildme_generates(self, artifact):
        from build_pipeline.llm_steps.spec_steps import _build_prompt

        with pytest.raises(ValueError, match="Unknown artifact type"):
            _build_prompt(artifact, {}, None)


# --- Resume behavior ---


class TestResumeSkipsExisting:
    @pytest.fixture
    def change_setup(self, tmp_path):
        """Set up a change directory with some artifacts already present."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")

        # pm-spec's inputs, and a design already written.
        from tests.spec_fixtures import write_specs

        write_specs(change_dir, capabilities=("cap-a",))
        (change_dir / "design.md").write_text("# Design\n")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.explainers_active = True
        config.project_description = ""
        config.model = "test-model"
        config.tier = "standard"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(completed_artifacts=["design"]),
        )

        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_skips_completed_artifacts(self, change_setup):
        cm, change_dir, config, state = change_setup

        mock_content = "## ADDED Requirements\n### Requirement: Works\nRequired.\n#### Scenario: Works\n- **WHEN** called\n- **THEN** succeeds\n## 1. Implement behavior\nImplement the capability."

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                AsyncMock(return_value=[]),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.sdk.llm_call", new_callable=AsyncMock) as mock_llm,
            patch(
                "build_pipeline.spec_generator.generate_rubric", new_callable=AsyncMock
            ) as mock_rubric,
        ):
            mock_gen.return_value = mock_content
            mock_llm.return_value = "## Personas\n\n## Use cases\n"
            mock_rubric.return_value = "# Rubric\nContent"

            await _generate_all_artifacts(cm, change_dir, config, state)

            # design should NOT have been generated (it is on disk and
            # recorded), and no input is ever generated.
            generated = [call[0][0] for call in mock_gen.call_args_list]
            assert "design" not in generated
            assert not set(generated) & {"proposal", "use-cases", "requirements"}
            assert "tasks" in generated


# --- Tasks-audit resume durability (completion recorded in state, not file existence) ---


class TestTasksAuditResumeDurability:
    """The audit runs after tasks.md is written, so a crash mid-audit leaves
    the artifact DONE by file existence. Completion must be read from
    checkpoint state: a resume with tasks_audit_done=False re-runs the audit;
    tasks_audit_done=True skips it."""

    def _all_done_setup(self, tmp_path, audit_done: bool):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        # Every artifact already on disk → DAG loop sees all DONE and never
        # re-enters _generate_single_artifact.
        (change_dir / "proposal.md").write_text(
            "# Proposal\n## Capabilities\n- `cap-a`\n"
        )
        (change_dir / "use-cases.md").write_text("## Personas\n\n## Use cases\n")
        (change_dir / "design.md").write_text("# Design")
        (change_dir / "unknowns.md").write_text(
            "# Unknowns\n\nNo dependencies new to this project."
        )
        (change_dir / "tasks.md").write_text("## 1. Vertical slice A")
        (change_dir / "rubric.md").write_text("# Rubric")
        req_dir = change_dir / "requirements"
        req_dir.mkdir(exist_ok=True)
        (req_dir / "cap-a.md").write_text(
            "### Requirement: Works\nRequired.\n#### Scenario: Works\n- **WHEN** called\n- **THEN** succeeds\n"
        )

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.explainers_active = True
        config.project_description = ""
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(
                completed_artifacts=[
                    "proposal",
                    "use-cases",
                    "requirements",
                    "design",
                    "unknowns",
                    "tasks",
                    "rubric",
                ],
                tasks_audit_done=audit_done,
                explainers_done=True,
            ),
        )
        from build_pipeline.spec_generator import reconcile_plan

        reconcile_plan(config, state)
        state.spec_gen.record_step("tasks_audit", "passed" if audit_done else "blocked")
        state.spec_gen.record_step("explainers", "passed")
        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_resume_reruns_audit_when_not_recorded_complete(self, tmp_path):
        """Crash mid-audit: tasks.md exists but tasks_audit_done=False —
        resume re-runs the audit and records + checkpoints completion."""
        cm, change_dir, config, state = self._all_done_setup(tmp_path, audit_done=False)

        with patch(
            "build_pipeline.spec_generator._audit_tasks_shape", new_callable=AsyncMock
        ) as mock_audit:
            await _generate_all_artifacts(cm, change_dir, config, state)

        assert mock_audit.await_count == 1
        assert state.spec_gen.step_passed("tasks_audit") is True
        # Completion is checkpointed so the NEXT resume skips it.
        saved = BuildState.model_validate_json(
            (change_dir / ".build-state.json").read_text()
        )
        assert saved.spec_gen.step_passed("tasks_audit") is True

    @pytest.mark.asyncio
    async def test_resume_skips_audit_when_recorded_complete(self, tmp_path):
        cm, change_dir, config, state = self._all_done_setup(tmp_path, audit_done=True)

        with patch(
            "build_pipeline.spec_generator._audit_tasks_shape", new_callable=AsyncMock
        ) as mock_audit:
            await _generate_all_artifacts(cm, change_dir, config, state)

        assert mock_audit.await_count == 0

    @pytest.mark.asyncio
    async def test_old_checkpoint_without_flag_defaults_to_rerun(self, tmp_path):
        """A pre-upgrade checkpoint (no tasks_audit_done key) deserializes to
        False → idempotent re-audit on resume, never a silent skip."""
        cm, change_dir, config, state = self._all_done_setup(tmp_path, audit_done=False)
        raw = state.model_dump()
        raw["spec_gen"].pop("tasks_audit_done", None)
        old_state = BuildState.model_validate(raw)
        assert old_state.spec_gen.step_passed("tasks_audit") is False

    @pytest.mark.asyncio
    async def test_fresh_generation_records_audit_done(self, tmp_path):
        """The normal in-line audit in _generate_single_artifact records
        completion in state."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        (change_dir / "proposal.md").write_text(
            "# Proposal\n## Capabilities\n- `cap-a`\n"
        )
        (change_dir / "design.md").write_text("# Design")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.explainers_active = True
        config.project_description = ""
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(),
        )

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.return_value = "## 1. Vertical slice A"
            mock_audit.return_value = []
            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert state.spec_gen.step_passed("tasks_audit") is True


# --- Tasks shape audit (B4: vertical slices) ---

_LAYERING_FINDING = {
    "category": "horizontal-decomposition",
    "severity": "critical",
    "description": "Blocks 1-3 are schema/API/UI layers",
    "suggestion": "Re-slice by behavior",
}


class TestTasksShapeAudit:
    """Generated tasks.md is audited for horizontal decomposition; findings
    force exactly one regeneration pass with the findings injected."""

    @pytest.fixture
    def tasks_setup(self, tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        (change_dir / "proposal.md").write_text(
            "# Proposal\n## Capabilities\n- `cap-a`\n"
        )
        (change_dir / "design.md").write_text("# Design")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.explainers_active = True
        config.project_description = ""
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(),
        )
        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_regenerates_tasks_once_when_audit_reports_layering(
        self, tasks_setup
    ):
        cm, change_dir, config, state = tasks_setup

        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements/cap-a.md").write_text(
            "### Requirement: Slice\n#### Scenario: Works\nWHEN used\nTHEN returns a result\n"
        )
        config.pipeline_branch = None
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = ["## 1. Schema layer", "## 1. Vertical slice A"]
            mock_audit.side_effect = [[_LAYERING_FINDING], _resolved("TA-F1")]

            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        # Exactly one repair followed by a required recheck
        assert mock_gen.call_count == 2
        assert mock_audit.await_count == 2

        # The regeneration prompt carries the audit findings
        regen_kwargs = mock_gen.call_args_list[1].kwargs
        assert "Blocks 1-3 are schema/API/UI layers" in regen_kwargs["audit_findings"]
        assert "Re-slice by behavior" in regen_kwargs["audit_findings"]

        # The regenerated content is what lands on disk
        assert (change_dir / "tasks.md").read_text() == "## 1. Vertical slice A"
        assert "tasks" in state.spec_gen.completed_artifacts

    @pytest.mark.asyncio
    async def test_no_regeneration_when_audit_clean(self, tasks_setup):
        cm, change_dir, config, state = tasks_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.return_value = "## 1. Vertical slice A"
            mock_audit.return_value = []

            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert mock_gen.call_count == 1
        assert (change_dir / "tasks.md").read_text() == "## 1. Vertical slice A"

    @pytest.mark.asyncio
    async def test_audit_failure_blocks_completion(self, tasks_setup):
        cm, change_dir, config, state = tasks_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.return_value = "## 1. Vertical slice A"
            mock_audit.side_effect = RuntimeError("LLM down")

            with pytest.raises(RuntimeError, match="LLM down"):
                await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        # First-pass tasks.md stands; no regeneration attempted
        assert mock_gen.call_count == 1
        assert (change_dir / "tasks.md").read_text() == "## 1. Vertical slice A"
        assert "tasks" not in state.spec_gen.completed_artifacts
        assert not state.spec_gen.step_passed("tasks_audit")

    @pytest.mark.asyncio
    async def test_regeneration_failure_blocks_completion(self, tasks_setup):
        cm, change_dir, config, state = tasks_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = ["## 1. Schema layer", RuntimeError("LLM down")]
            mock_audit.return_value = [_LAYERING_FINDING]

            with pytest.raises(RuntimeError, match="LLM down"):
                await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        # The failed repair remains incomplete
        assert mock_gen.call_count == 2
        # The first draft is preserved for recovery, without completion evidence
        assert (change_dir / "tasks.md").read_text() == "## 1. Schema layer"
        assert "tasks" not in state.spec_gen.completed_artifacts
        assert not state.spec_gen.step_passed("tasks_audit")

    @pytest.mark.asyncio
    async def test_non_tasks_artifacts_are_not_audited(self, tasks_setup):
        cm, change_dir, config, state = tasks_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.return_value = "# Design content"

            await _generate_single_artifact(cm, change_dir, "design", config, state)

        mock_audit.assert_not_awaited()


class TestScanPlaceholders:
    """Deterministic placeholder scan over generated tasks (Item 6)."""

    def test_flags_tbd_and_todo(self):
        findings = scan_placeholders(
            "## 1. Block\nSchema: TBD\nWire it up.\n- [ ] TODO: name it\n"
        )
        assert len(findings) == 2
        assert all(f["category"] == "placeholder" for f in findings)
        assert "line 2" in findings[0]["description"]
        assert "line 4" in findings[1]["description"]

    def test_flags_deferred_phrases(self):
        text = "Add appropriate error handling here.\nUse a structure similar to block 2.\n"
        findings = scan_placeholders(text)
        assert len(findings) == 2

    def test_clean_text_yields_no_findings(self):
        text = "## 1. Token Issuer\nProduces issue_token(user_id: str) -> str.\n"
        assert scan_placeholders(text) == []

    def test_does_not_flag_substrings_of_words(self):
        """`TBD`/`TODO` match as words only — not inside identifiers."""
        assert scan_placeholders("call TODOS_LIST and tbdx_helper()\n") == []

    @pytest.mark.asyncio
    async def test_placeholder_scan_forces_regeneration_with_clean_model_audit(
        self, tmp_path
    ):
        """A clean model audit cannot overrule deterministic placeholder findings."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("ph")
        (change_dir / "proposal.md").write_text(
            "# Proposal\n## Capabilities\n- `cap-a`\n"
        )
        (change_dir / "design.md").write_text("# Design")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "ph"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="ph",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(),
        )

        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements/cap-a.md").write_text(
            "### Requirement: Slice\n#### Scenario: Works\nWHEN used\nTHEN returns a result\n"
        )
        config.pipeline_branch = None
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = [
                "## 1. Block\nError handling: TBD\n",
                "## 1. Block\nConcrete.\n",
            ]
            mock_audit.return_value = []

            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        # Scan findings alone drove the one regeneration pass
        assert mock_gen.call_count == 2
        assert (change_dir / "tasks.md").read_text() == "## 1. Block\nConcrete.\n"
        regen_findings = mock_gen.call_args_list[1].kwargs["audit_findings"]
        assert "Placeholder text on line 2" in regen_findings


# --- Design audit feedback loop (gaps rewrite the specs) ---

_CRITICAL_GAP = {
    "category": "spec-task-alignment",
    "severity": "critical",
    "description": "Scenario 'rejects an expired token' has no task block",
    "suggestion": "Add a block that implements the rejection path",
}
_MAJOR_GAP = {
    "category": "over-engineering",
    "severity": "major",
    "description": "TokenStoreFactory has one implementation and no requirement asks for a second",
    "suggestion": "Call the concrete store directly",
}
_MINOR_GAP = {
    "category": "granularity",
    "severity": "minor",
    "description": "Block 2's title could name the behavior more precisely",
    "suggestion": "Rename it",
}


class TestDesignAuditFeedback:
    """The design audit used to log its gaps and march on. Critical/major gaps
    now drive exactly ONE regeneration pass of design.md + tasks.md, followed
    by ONE report-only re-audit — the same one-pass-no-loop discipline as the
    prototype feedback and the tasks-shape audit."""

    @pytest.fixture
    def audit_setup(self, tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        (change_dir / "proposal.md").write_text(
            "# Proposal\n## Capabilities\n- `cap-a`\n"
        )
        (change_dir / "design.md").write_text("# Design v1")
        (change_dir / "unknowns.md").write_text(
            "# Unknowns\n\nNo dependencies new to this project."
        )
        (change_dir / "tasks.md").write_text("## 1. Block v1")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.DESIGN_AUDIT,
            spec_gen=SpecGenState(),
        )
        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements" / "cap-a.md").write_text(
            "### Requirement: A\n#### Scenario: A\nWHEN invoked\nTHEN returns A\n"
        )
        (change_dir / "use-cases.md").write_text("# Use cases\nInvoke A")
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric v2"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                AsyncMock(return_value=[]),
            ),
        ):
            yield change_dir, config, state

    @pytest.mark.asyncio
    async def test_critical_gaps_regenerate_design_and_tasks_once(self, audit_setup):
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage") as mock_commit,
        ):
            # First pass finds the gap; the report-only re-check finds nothing.
            mock_audit.side_effect = [[_CRITICAL_GAP, _MINOR_GAP], _resolved("DA-F1")]
            mock_gen.side_effect = ["# Design v2", "## 1. Block v2"]

            gaps = await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        # Exactly one regeneration call per artifact...
        assert mock_gen.await_count == 2
        regen_ids = [c.args[0] for c in mock_gen.call_args_list]
        assert regen_ids == ["design", "tasks"]  # design first: tasks reads it back
        for call in mock_gen.call_args_list:
            assert call.kwargs["audit_findings"].strip()
            assert "rejects an expired token" in call.kwargs["audit_findings"]

        # ...and exactly one report-only re-audit after it (2 audits total).
        assert mock_audit.await_count == 2

        # The regenerated content is what lands on disk, and it is committed.
        assert (change_dir / "design.md").read_text() == "# Design v2"
        assert (change_dir / "tasks.md").read_text() == "## 1. Block v2"
        assert mock_commit.call_count == 1
        assert "design audit" in mock_commit.call_args.args[1]

        # The first-pass gaps are returned for the caller's log.
        assert gaps == []  # the accepted revision has no blocking gaps

        # The pass is recorded AND checkpointed, so a resume never repeats it.
        assert state.spec_gen.repair_attempts("design_audit") == 1
        saved = BuildState.model_validate_json(
            (change_dir / ".build-state.json").read_text()
        )
        assert saved.spec_gen.repair_attempts("design_audit") == 1

    @pytest.mark.asyncio
    async def test_major_gaps_also_regenerate(self, audit_setup):
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage"),
        ):
            mock_audit.side_effect = [[_MAJOR_GAP], _resolved("DA-F1")]
            mock_gen.side_effect = ["# Design v2", "## 1. Block v2"]

            await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        assert mock_gen.await_count == 2
        assert (
            "TokenStoreFactory" in mock_gen.call_args_list[0].kwargs["audit_findings"]
        )

    @pytest.mark.asyncio
    async def test_no_gaps_means_no_regeneration_and_no_recheck(self, audit_setup):
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage") as mock_commit,
        ):
            mock_audit.return_value = []

            gaps = await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        assert gaps == []
        mock_gen.assert_not_awaited()
        assert mock_audit.await_count == 1
        mock_commit.assert_not_called()
        assert (change_dir / "design.md").read_text() == "# Design v1"
        assert state.spec_gen.repair_attempts("design_audit") == 0

    @pytest.mark.asyncio
    async def test_minor_gaps_alone_do_not_regenerate(self, audit_setup):
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
        ):
            mock_audit.return_value = [_MINOR_GAP]

            gaps = await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        assert gaps == [_MINOR_GAP]
        mock_gen.assert_not_awaited()
        assert mock_audit.await_count == 1

    @pytest.mark.asyncio
    async def test_resume_after_regeneration_is_report_only(self, audit_setup):
        """Crash between the regeneration checkpoint and the phase advance: the
        documents already absorbed the critique, so the resume audits and
        reports but never regenerates a second time."""
        change_dir, config, state = audit_setup
        state.spec_gen.admit_repair("design_audit")

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage") as mock_commit,
        ):
            mock_audit.return_value = [_CRITICAL_GAP]
            with pytest.raises(RuntimeError):
                await run_design_audit_stage(
                    change_dir,
                    config,
                    state,
                    config.state_file_path(),
                )

        assert not state.spec_gen.step_passed("design_audit")
        mock_gen.assert_not_awaited()
        assert mock_audit.await_count == 1  # report only, no re-check either
        mock_commit.assert_not_called()
        assert (change_dir / "design.md").read_text() == "# Design v1"

    @pytest.mark.asyncio
    async def test_old_checkpoint_without_flag_defaults_to_not_done(self, audit_setup):
        """A pre-upgrade checkpoint has no design_audit_regen_done key — it
        must still validate, and default to False (the pass has not run)."""
        _, _, state = audit_setup
        assert SpecGenState().repair_attempts("design_audit") == 0
        raw = state.model_dump()
        raw["spec_gen"].pop("design_audit_regen_done", None)
        old_state = BuildState.model_validate(raw)
        assert old_state.spec_gen.repair_attempts("design_audit") == 0

    @pytest.mark.asyncio
    async def test_audit_failure_is_non_fatal(self, audit_setup):
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
        ):
            mock_audit.side_effect = RuntimeError("LLM down")
            with pytest.raises(RuntimeError):
                await run_design_audit_stage(
                    change_dir,
                    config,
                    state,
                    config.state_file_path(),
                )

        assert not state.spec_gen.step_passed("design_audit")
        mock_gen.assert_not_awaited()
        assert state.spec_gen.repair_attempts("design_audit") == 0

    @pytest.mark.asyncio
    async def test_regeneration_failure_is_non_fatal_and_spends_the_pass(
        self, audit_setup
    ):
        """A failed regeneration leaves the first pass on disk and still burns
        the single pass — retrying later would be the loop this design forbids."""
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage") as mock_commit,
        ):
            mock_audit.side_effect = [[_CRITICAL_GAP], []]
            mock_gen.side_effect = RuntimeError("LLM down")
            with pytest.raises(RuntimeError):
                await run_design_audit_stage(
                    change_dir,
                    config,
                    state,
                    config.state_file_path(),
                )

        assert mock_gen.await_count == 1  # abort staging at the first failed writer
        assert (change_dir / "design.md").read_text() == "# Design v1"
        assert (change_dir / "tasks.md").read_text() == "## 1. Block v1"
        mock_commit.assert_not_called()
        assert mock_audit.await_count == 1  # nothing changed → nothing to re-check
        assert state.spec_gen.repair_attempts("design_audit") == 1

    @pytest.mark.asyncio
    async def test_recheck_reports_remaining_gaps_without_a_second_pass(
        self, audit_setup
    ):
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage"),
        ):
            # The critique did NOT land — the re-check still sees a critical gap.
            mock_audit.side_effect = [[_CRITICAL_GAP], [_CRITICAL_GAP]]
            mock_gen.side_effect = ["# Design v2", "## 1. Block v2"]
            with pytest.raises(RuntimeError):
                await run_design_audit_stage(
                    change_dir,
                    config,
                    state,
                    config.state_file_path(),
                )

        assert mock_audit.await_count == 2
        assert mock_gen.await_count == 2  # no third generation pass

    @pytest.mark.asyncio
    async def test_two_call_sites_regenerate_at_most_once(self, audit_setup):
        """A full build reaches this stage twice — once inside
        run_spec_generator, once in the orchestrator's DESIGN_AUDIT phase. The
        checkpointed flag is what keeps that from being two regeneration
        passes."""
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch("build_pipeline.git_ops.commit_stage"),
        ):
            mock_audit.side_effect = [[_CRITICAL_GAP], _resolved("DA-F1")]
            mock_gen.side_effect = ["# Design v2", "## 1. Block v2"]

            await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )
            # Second call site, same build, state carried across.
            await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        assert mock_gen.await_count == 2  # design + tasks, once — not four calls
        # ...and the second arrival costs no audit call either: the stage is
        # recorded as spent, so it returns before reaching the LLM.
        assert mock_audit.await_count == 2  # audit + re-check, from call one only

    @pytest.mark.asyncio
    async def test_second_call_site_costs_nothing_when_nothing_was_actionable(
        self,
        audit_setup,
    ):
        """The regeneration flag alone cannot carry this case: a clean audit
        never regenerates, so without design_audit_done the second call site
        would re-run a four-artifact audit to rediscover there is nothing to
        do."""
        change_dir, config, state = audit_setup

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_design_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
        ):
            mock_audit.return_value = []

            await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )
            assert state.spec_gen.step_passed("design_audit") is True
            gaps = await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        assert gaps == []
        assert mock_audit.await_count == 1  # not two
        mock_gen.assert_not_awaited()
        assert state.spec_gen.repair_attempts("design_audit") == 0

    @pytest.mark.asyncio
    async def test_completion_survives_a_reload(self, audit_setup):
        """The orchestrator reloads the state from disk between the two call
        sites, so the flag has to be on the checkpoint, not just in memory."""
        change_dir, config, state = audit_setup
        state_path = config.state_file_path()

        with patch(
            "build_pipeline.llm_steps.spec_steps.run_design_audit",
            new_callable=AsyncMock,
        ) as mock_audit:
            mock_audit.return_value = []
            await run_design_audit_stage(change_dir, config, state, state_path)

            reloaded = BuildState.load(state_path)
            assert reloaded.spec_gen.step_passed("design_audit") is True
            await run_design_audit_stage(change_dir, config, reloaded, state_path)

        assert mock_audit.await_count == 1

    @pytest.mark.asyncio
    async def test_a_failed_audit_is_not_recorded_as_done(self, audit_setup):
        """An LLM failure is a reason to try again — unlike a completed audit."""
        change_dir, config, state = audit_setup

        with patch(
            "build_pipeline.llm_steps.spec_steps.run_design_audit",
            new_callable=AsyncMock,
        ) as mock_audit:
            mock_audit.side_effect = [RuntimeError("LLM down"), []]
            with pytest.raises(RuntimeError):
                await run_design_audit_stage(
                    change_dir,
                    config,
                    state,
                    config.state_file_path(),
                )
            assert state.spec_gen.step_passed("design_audit") is False
            await run_design_audit_stage(
                change_dir,
                config,
                state,
                config.state_file_path(),
            )

        assert mock_audit.await_count == 2  # the retry really happened
        assert state.spec_gen.step_passed("design_audit") is True

    def test_old_checkpoint_without_done_flag_defaults_to_not_done(self, audit_setup):
        _, _, state = audit_setup
        assert SpecGenState().step_passed("design_audit") is False
        raw = state.model_dump()
        raw["spec_gen"].pop("design_audit_done", None)
        assert (
            BuildState.model_validate(raw).spec_gen.step_passed("design_audit") is False
        )

    def test_findings_text_carries_severity_category_and_fix(self):
        text = design_audit_findings_text([_CRITICAL_GAP, _MINOR_GAP])
        assert "[critical]" in text
        assert "(spec-task-alignment)" in text
        assert "Fix: Add a block that implements the rejection path" in text
        # Minor gaps are reported, not regenerated against.
        assert "Block 2's title" not in text

    def test_findings_text_empty_when_nothing_actionable(self):
        assert design_audit_findings_text([]) == ""
        assert design_audit_findings_text([_MINOR_GAP]) == ""
        assert design_audit_findings_text(["not a dict"]) == ""


# --- Run spec generator (integration-ish) ---


class TestRunSpecGenerator:
    @staticmethod
    def _config(tmp_path, *, ready=True):
        from tests.spec_fixtures import ready_specs, write_specs

        specs_dir = tmp_path / "specs"
        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.accept_unknown_spend = False
        config.task_granularity = "checkboxes"
        config.change_name = "test"
        config.specs_dir = specs_dir
        config.tier = "standard"
        config.model = "test-model"
        config.state_file_path.return_value = tmp_path / ".build-state.json"
        config.change_dir = specs_dir / "changes" / "test"
        if ready:
            ready_specs(config.change_dir)
        else:
            write_specs(config.change_dir)
        return config

    @pytest.mark.asyncio
    async def test_returns_0_on_success(self, tmp_path):
        config = self._config(tmp_path)
        with (
            patch(
                "build_pipeline.spec_generator._generate_all_artifacts",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.spec_generator._run_audit", new_callable=AsyncMock
            ) as mock_audit,
        ):
            mock_audit.return_value = []
            exit_code = await run_spec_generator(config)
            assert exit_code == 0
            mock_gen.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_1_on_failure(self, tmp_path):
        config = self._config(tmp_path)
        with patch(
            "build_pipeline.spec_generator._generate_all_artifacts",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.side_effect = RuntimeError("boom")
            exit_code = await run_spec_generator(config)
            assert exit_code == 1

    @pytest.mark.asyncio
    async def test_standalone_refuses_specs_that_are_not_ready(self, tmp_path, capsys):
        config = self._config(tmp_path, ready=False)
        with patch(
            "build_pipeline.spec_generator._generate_all_artifacts",
            new_callable=AsyncMock,
        ) as mock_gen:
            exit_code = await run_spec_generator(config)
        out = capsys.readouterr().out
        assert exit_code == 2
        assert "SPEC:NOT_READY:no .change.yaml" in out
        assert "Run /multiplai-pm:pm-spec test first" in out
        mock_gen.assert_not_awaited()
        assert not (tmp_path / ".build-state.json").exists()

    @pytest.mark.asyncio
    async def test_standalone_writes_spec_issues_and_exits_4(self, tmp_path, capsys):
        from build_pipeline.readiness import SpecIssuesFound
        from multiplai_core.specs import read_change_meta

        config = self._config(tmp_path)
        found = SpecIssuesFound(
            [("Design: requirements/fixture.md: Behavior", "cannot be built")],
            step="design",
        )
        with patch(
            "build_pipeline.spec_generator._generate_all_artifacts",
            AsyncMock(side_effect=found),
        ):
            exit_code = await run_spec_generator(config)
        issues = config.change_dir / "spec-issues.md"
        assert exit_code == 4
        assert f"SPEC:ISSUES:{issues}" in capsys.readouterr().out
        assert "## Design: requirements/fixture.md: Behavior" in issues.read_text()
        assert read_change_meta(config.change_dir)["spec_issues"] == "spec-issues.md"

    @pytest.mark.asyncio
    async def test_a_sub_phase_raises_spec_issues_for_the_orchestrator(self, tmp_path):
        from build_pipeline.readiness import SpecIssuesFound

        config = self._config(tmp_path)
        found = SpecIssuesFound([("t", "b")], step="design")
        with (
            patch(
                "build_pipeline.spec_generator._generate_all_artifacts",
                AsyncMock(side_effect=found),
            ),
            pytest.raises(SpecIssuesFound),
        ):
            await run_spec_generator(config, standalone=False)
        assert not (config.change_dir / "spec-issues.md").exists()


# --- Unknowns / explainer artifact (B1) ---------------------------------------


class TestGenerateUnknowns:
    """`unknowns.md` is always written — the DAG's `tasks` step depends on it,
    so a skip or a clean scan must still leave a readable record on disk."""

    def _setup(self, tmp_path, *, impact: str = "", explainers_active: bool = True):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["httpx"]\n'
        )
        (change_dir / "proposal.md").write_text("## Impact\n" + impact + "\n")
        (change_dir / "design.md").write_text("## Decisions\nNone.\n")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = project_dir
        config.project_description = ""
        config.model = "test-model"
        config.explainers_active = explainers_active
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(),
        )
        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_explainer_budget_denial_stops_before_regeneration(self, tmp_path):
        from build_pipeline.budget import BudgetExceededError
        from build_pipeline.spec_generator import _generate_unknowns

        cm, change_dir, config, state = self._setup(
            tmp_path, impact="Adds `polars` and `duckdb`."
        )
        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_explainer",
                AsyncMock(side_effect=[BudgetExceededError("cap"), "partial"]),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact", AsyncMock()
            ) as generate,
        ):
            with pytest.raises(BudgetExceededError):
                await _generate_unknowns(cm, change_dir, config, state)
        generate.assert_not_awaited()
        assert not state.spec_gen.step_passed("explainers")

    @pytest.mark.asyncio
    async def test_one_explainer_call_per_detected_dependency(self, tmp_path):
        from build_pipeline.spec_generator import _generate_unknowns

        cm, change_dir, config, state = self._setup(
            tmp_path, impact="Adds `polars` and `duckdb`."
        )
        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_explainer",
                new_callable=AsyncMock,
            ) as mock_explain,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
        ):
            mock_explain.side_effect = lambda dep, cfg, usage_context="": (
                f"## {dep.name}\n\n### Edge cases & failure modes\n- empty input errors\n\n"
                f"### Assumptions we are making\n- version 1.x is stable\n"
            )
            mock_gen.return_value = "regenerated"
            await _generate_unknowns(cm, change_dir, config, state)

        assert mock_explain.await_count == 2
        # A complete first pass needs no regeneration.
        assert mock_gen.await_count == 0
        text = (change_dir / "unknowns.md").read_text()
        assert "## duckdb" in text and "## polars" in text
        assert state.spec_gen.step_passed("explainers") is True

    @pytest.mark.asyncio
    async def test_explainer_receives_design_decisions(self, tmp_path):
        """usage_context carries the design's Decisions section, not the
        placeholder — the explainer researches the edge cases of how THIS
        project will use the dependency."""
        from build_pipeline.spec_generator import _generate_unknowns

        cm, change_dir, config, state = self._setup(tmp_path, impact="Adds `polars`.")
        (change_dir / "design.md").write_text(
            "## Decisions\nStream frames lazily; never collect() whole files.\n"
        )
        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_explainer",
                new_callable=AsyncMock,
            ) as mock_explain,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
        ):
            mock_explain.return_value = (
                "## polars\n\n### Edge cases & failure modes\n- x\n\n"
                "### Assumptions we are making\n- y\n"
            )
            mock_gen.return_value = "regenerated"
            await _generate_unknowns(cm, change_dir, config, state)

        assert mock_explain.call_args.kwargs["usage_context"] == (
            "Stream frames lazily; never collect() whole files."
        )

    @pytest.mark.asyncio
    async def test_no_new_dependencies_records_the_absence(self, tmp_path):
        from build_pipeline.spec_generator import (
            NO_NEW_DEPENDENCIES_LINE,
            _generate_unknowns,
        )

        cm, change_dir, config, state = self._setup(
            tmp_path, impact="Uses `httpx` and `pathlib` only."
        )
        with patch(
            "build_pipeline.llm_steps.spec_steps.run_explainer", new_callable=AsyncMock
        ) as mock_explain:
            await _generate_unknowns(cm, change_dir, config, state)

        assert mock_explain.await_count == 0
        assert NO_NEW_DEPENDENCIES_LINE in (change_dir / "unknowns.md").read_text()
        assert state.spec_gen.step_passed("explainers") is True

    @pytest.mark.asyncio
    async def test_skip_explainers_still_writes_the_artifact(self, tmp_path):
        from build_pipeline.spec_generator import (
            EXPLAINERS_SKIPPED_LINE,
            _generate_unknowns,
        )

        cm, change_dir, config, state = self._setup(
            tmp_path, impact="Adds `polars`.", explainers_active=False
        )
        with patch(
            "build_pipeline.llm_steps.spec_steps.run_explainer", new_callable=AsyncMock
        ) as mock_explain:
            await _generate_unknowns(cm, change_dir, config, state)

        assert mock_explain.await_count == 0
        assert EXPLAINERS_SKIPPED_LINE in (change_dir / "unknowns.md").read_text()

    @pytest.mark.asyncio
    async def test_failed_explainer_leaves_a_visible_hole(self, tmp_path):
        """A failed explainer call writes an empty-section stub so the gate
        sees the hole — it is never silently dropped."""
        from build_pipeline.spec_generator import _generate_unknowns

        cm, change_dir, config, state = self._setup(tmp_path, impact="Adds `polars`.")
        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_explainer",
                new_callable=AsyncMock,
            ) as mock_explain,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
        ):
            mock_explain.side_effect = RuntimeError("no network")
            mock_gen.return_value = "## polars\n\n### Edge cases & failure modes\n- x\n\n### Assumptions we are making\n- y\n"
            await _generate_unknowns(cm, change_dir, config, state)

        # The gate saw an incomplete section and spent its one regeneration pass.
        assert mock_gen.await_count == 1


class TestUnknownsGateResumeDurability:
    """Mirrors the tasks-audit durability contract: the gate runs after
    unknowns.md is written, so completion is read from checkpoint state."""

    def _setup(self, tmp_path, explainers_done: bool):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (change_dir / "proposal.md").write_text(
            "## Capabilities\n- `cap-a`\n## Impact\nAdds `polars`.\n"
        )
        (change_dir / "use-cases.md").write_text("## Personas\n\n## Use cases\n")
        (change_dir / "design.md").write_text("## Decisions\nNone.\n")
        (change_dir / "unknowns.md").write_text("# Unknowns\n\n## polars\n")
        (change_dir / "tasks.md").write_text("## 1. Slice")
        (change_dir / "rubric.md").write_text("# Rubric")
        req_dir = change_dir / "requirements"
        req_dir.mkdir(exist_ok=True)
        (req_dir / "cap-a.md").write_text(
            "### Requirement: Works\nRequired.\n#### Scenario: Works\n- **WHEN** called\n- **THEN** succeeds\n"
        )

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = project_dir
        config.project_description = ""
        config.model = "test-model"
        config.explainers_active = True
        config.task_granularity = "checkboxes"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(
                completed_artifacts=[
                    "proposal",
                    "use-cases",
                    "requirements",
                    "design",
                    "unknowns",
                    "tasks",
                    "rubric",
                ],
                tasks_audit_done=True,
                explainers_done=explainers_done,
            ),
        )
        from build_pipeline.spec_generator import reconcile_plan

        reconcile_plan(config, state)
        state.spec_gen.record_step("tasks_audit", "passed")
        state.spec_gen.record_step(
            "explainers", "passed" if explainers_done else "blocked"
        )
        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_resume_reruns_gate_when_not_recorded_complete(self, tmp_path):
        cm, change_dir, config, state = self._setup(tmp_path, explainers_done=False)
        with patch(
            "build_pipeline.spec_generator._audit_unknowns", new_callable=AsyncMock
        ) as mock_audit:
            await _generate_all_artifacts(cm, change_dir, config, state)

        assert mock_audit.await_count == 1
        assert state.spec_gen.step_passed("explainers") is True
        saved = BuildState.model_validate_json(
            (change_dir / ".build-state.json").read_text()
        )
        assert saved.spec_gen.step_passed("explainers") is True

    @pytest.mark.asyncio
    async def test_resume_skips_gate_when_recorded_complete(self, tmp_path):
        cm, change_dir, config, state = self._setup(tmp_path, explainers_done=True)
        with patch(
            "build_pipeline.spec_generator._audit_unknowns", new_callable=AsyncMock
        ) as mock_audit:
            await _generate_all_artifacts(cm, change_dir, config, state)

        assert mock_audit.await_count == 0

    @pytest.mark.asyncio
    async def test_resume_honors_skip_explainers(self, tmp_path):
        """--skip-explainers on a resumed build must not run detection or the
        gate's LLM regeneration — that is what the flag exists to prevent."""
        cm, change_dir, config, state = self._setup(tmp_path, explainers_done=False)
        config.explainers_active = False
        with (
            patch(
                "build_pipeline.spec_generator._audit_unknowns", new_callable=AsyncMock
            ) as mock_audit,
            patch(
                "build_pipeline.spec_generator.detect_new_dependencies"
            ) as mock_detect,
        ):
            await _generate_all_artifacts(cm, change_dir, config, state)

        assert mock_audit.await_count == 0
        mock_detect.assert_not_called()
        assert state.spec_gen.step_passed("explainers") is True

    @pytest.mark.asyncio
    async def test_resume_with_skip_explainers_writes_the_marker_when_missing(
        self,
        tmp_path,
    ):
        """A crash before unknowns.md existed + --skip-explainers on the
        re-run: the skip-marker document is written so the DAG's `tasks`
        dependency stays satisfiable."""
        from build_pipeline.spec_generator import EXPLAINERS_SKIPPED_LINE

        cm, change_dir, config, state = self._setup(tmp_path, explainers_done=False)
        config.explainers_active = False
        (change_dir / "unknowns.md").unlink()
        with (
            patch(
                "build_pipeline.spec_generator._audit_unknowns", new_callable=AsyncMock
            ) as mock_audit,
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                AsyncMock(return_value="## 1. Slice\nImplement A"),
            ),
            patch("build_pipeline.spec_generator._audit_tasks_shape", AsyncMock()),
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
        ):
            await _generate_all_artifacts(cm, change_dir, config, state)

        assert mock_audit.await_count == 0
        assert EXPLAINERS_SKIPPED_LINE in (change_dir / "unknowns.md").read_text()
        assert state.spec_gen.step_passed("explainers") is True

    def test_old_checkpoint_without_flag_defaults_to_rerun(self, tmp_path):
        cm, change_dir, config, state = self._setup(tmp_path, explainers_done=False)
        raw = state.model_dump()
        raw["spec_gen"].pop("explainers_done", None)
        assert (
            BuildState.model_validate(raw).spec_gen.step_passed("explainers") is False
        )


class TestUnknownsThreadedIntoTasks:
    @pytest.mark.asyncio
    async def test_tasks_context_carries_the_unknowns_text(self, tmp_path):
        """Edge cases only become tests if the block that touches the
        dependency names them — so the task breakdown must see them."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        (change_dir / "proposal.md").write_text("## Impact\nNone.\n")
        (change_dir / "design.md").write_text("## Decisions\nNone.\n")
        (change_dir / "unknowns.md").write_text(
            "# Unknowns\n\n## polars\n\n### Edge cases & failure modes\n"
            "- empty frame raises SchemaError\n"
        )
        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements" / "cap-a.md").write_text(
            "### Requirement: Works\nRequired.\n#### Scenario: Works\n- **WHEN** called\n- **THEN** succeeds\n"
        )

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.project_description = ""
        config.model = "test-model"
        config.task_granularity = "checkboxes"
        config.explainers_active = True
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(),
        )

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.spec_generator._audit_tasks_shape",
                new_callable=AsyncMock,
            ),
        ):
            mock_gen.return_value = "## 1. Slice"
            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        context = mock_gen.await_args.args[1]
        assert "empty frame raises SchemaError" in context["unknowns_content"]


class TestCodebaseAnalysisReachesTheDesign:
    """The CODEBASE_ANALYSIS phase stores a *path* on the checkpoint; the
    design prompt needs the report's text. `read_codebase_analysis` is the
    seam, and `_generate_single_artifact` must use it — passing the path
    string would send the design a filename to reason about."""

    def _state(self, path_value) -> BuildState:
        return BuildState(
            change_name="c",
            tier="standard",
            spec_gen=SpecGenState(codebase_analysis_path=path_value),
        )

    def test_reads_the_report_text(self, tmp_path):
        from build_pipeline.spec_generator import read_codebase_analysis

        report = tmp_path / "codebase-analysis.md"
        report.write_text("## Architecture\nMODULE-MAP-MARKER")
        assert "MODULE-MAP-MARKER" in read_codebase_analysis(self._state(str(report)))

    def test_absent_path_is_empty_so_the_prompt_says_new_project(self):
        from build_pipeline.spec_generator import read_codebase_analysis

        assert read_codebase_analysis(self._state(None)) == ""
        assert (
            read_codebase_analysis(BuildState(change_name="c", tier="standard")) == ""
        )

    def test_unresolvable_path_falls_back_to_the_stored_string(self, tmp_path, caplog):
        """Older checkpoints (and an archived change dir) may hold a path that
        no longer resolves — dropping the value silently would be worse."""
        from build_pipeline.spec_generator import read_codebase_analysis

        with caplog.at_level("WARNING"):
            out = read_codebase_analysis(self._state(str(tmp_path / "gone.md")))
        assert out.endswith("gone.md")
        assert any("not a readable file" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_design_generation_gets_the_text_and_the_reference_docs(
        self, tmp_path
    ):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("test-feature")
        (change_dir / "proposal.md").write_text("## Impact\nNone.\n")
        report = change_dir / "codebase-analysis.md"
        report.write_text("## Architecture\nMODULE-MAP-MARKER")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "test-feature"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.project_dir = tmp_path
        config.model = "test-model"
        config.reference_docs_text.return_value = (
            "### Reference: house.md\nHOUSE-MARKER"
        )
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="test-feature",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(codebase_analysis_path=str(report)),
        )

        with patch(
            "build_pipeline.llm_steps.spec_steps.generate_artifact",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.return_value = "# design"
            await _generate_single_artifact(cm, change_dir, "design", config, state)

        kwargs = mock_gen.await_args.kwargs
        assert "MODULE-MAP-MARKER" in kwargs["codebase_analysis"]
        assert "HOUSE-MARKER" in kwargs["reference_docs"]


# --- Blocks that add no behaviour (epic e2e attempt 1, Defect 1) ---
#
# Child 2 of the 2026-10-14 live epic wrote blocks 2 and 3 as "Confirm…" and
# "Verify…" with `Produces: (none)`: block 1 had already built what they
# test, so their tests passed before any implementation and the RED gate failed
# the whole build. The fixture is that tasks.md, verbatim.

from build_pipeline.spec_generator import scan_no_behaviour_blocks

_E2E_TASKS = Path(__file__).parent / "fixtures" / "tasks-no-behaviour-blocks.md"


def _tasks_file(tmp_path, text: str) -> Path:
    path = tmp_path / "tasks.md"
    path.write_text(text)
    return path


class TestScanNoBehaviourBlocks:
    def test_flags_the_confirm_and_verify_blocks_from_the_live_epic(self):
        findings = scan_no_behaviour_blocks(_E2E_TASKS)

        assert [f["block"] for f in findings] == [2, 3]
        assert all(f["category"] == "no-new-behaviour" for f in findings)
        assert all(f["severity"] == "critical" for f in findings)
        assert "Line-count and empty-input edge cases" in findings[0]["description"]
        assert "Module conventions" in findings[1]["description"]

    def test_the_finding_says_where_the_criteria_go(self):
        finding = scan_no_behaviour_blocks(_E2E_TASKS)[0]

        assert "RED gate" in finding["description"]
        # Block 2 consumes Block 1, so Block 1 is where its tests belong.
        assert "Block 1" in finding["suggestion"]

    def test_a_block_that_admits_an_earlier_block_satisfies_it_is_flagged(
        self,
        tmp_path,
    ):
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Parse dates\n\nAdd `parse(s: str) -> date`.\n\n"
                "Interfaces:\n- Produces: `parse(s: str) -> date`\n\n"
                "Satisfies: dates — Parse\n\n"
                "## 2. Leap years\n\nAdd tests for 29 February. These are already "
                "implemented by Block 1.\n\n"
                "Interfaces:\n- Produces: (none)\n- Consumes: Block 1: `parse(s: str) -> date`\n\n"
                "Satisfies: dates — Leap\n"
            ),
        )

        assert [f["block"] for f in scan_no_behaviour_blocks(path)] == [2]

    @pytest.mark.parametrize("says", ["Covered", "Satisfied", "Delivered"])
    def test_a_sentence_that_opens_with_covered_by_block_n_is_flagged(
        self,
        tmp_path,
        says,
    ):
        """The rule's second form on its own: no pronoun names the block, and
        the block names no file."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\nAdd `count(s: str) -> int`.\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                f"## 2. Tabs\n\nAdd tests for tabs. {says} by Block 1.\n\n"
                "Interfaces:\n- Produces: (none)\n"
            ),
        )

        findings = scan_no_behaviour_blocks(path)

        assert [f["block"] for f in findings] == [2]
        assert "an earlier block already satisfies it" in findings[0]["description"]

    def test_a_behaviour_block_with_no_produces_is_not_flagged(self, tmp_path):
        """`Produces: (none)` alone is legitimate: a last slice nobody calls."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Print the summary from the CLI\n\nAdd a `--summary` flag "
                "that prints `summarize(stdin)` and exits 0.\n\n"
                "Interfaces:\n- Produces: (none)\n\nSatisfies: cli — Summary\n"
            ),
        )

        assert scan_no_behaviour_blocks(path) == []

    def test_a_verify_block_that_produces_something_is_not_flagged(self, tmp_path):
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Signature check\n\nVerify each upload's HMAC and reject a "
                "mismatch with 401.\n\n"
                "Interfaces:\n- Produces: `check_signature(body: bytes, sig: str) -> bool`\n\n"
                "Satisfies: upload — Signature\n"
            ),
        )

        assert scan_no_behaviour_blocks(path) == []

    def test_a_checkbox_block_whose_prose_says_block_1_satisfies_it_is_flagged(
        self,
        tmp_path,
    ):
        """Checkbox format (every standard-tier build): `parse_blocks` keeps
        only the checkbox lines, so the scan must read the prose too, and a
        numbered item such as `2.1 Confirm …` must not hide anything."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\n- [ ] 1.1 Add `count(s: str) -> int`\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                "## 2. Line-count edge cases\n\nThis slice is satisfied by Block 1.\n\n"
                "- [ ] 2.1 Confirm `len(text.splitlines())` handles blank lines\n"
                '- [ ] 2.2 Add tests asserting `count("") == 0`\n\n'
                "Interfaces:\n- Produces: (none)\n"
                "- Consumes: Block 1: `count(s: str) -> int`\n"
            ),
        )

        assert [f["block"] for f in scan_no_behaviour_blocks(path)] == [2]

    def test_a_block_that_says_an_earlier_block_already_handles_it_is_flagged(
        self,
        tmp_path,
    ):
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\nAdd `count(s: str) -> int` to `src/words.py`.\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                "## 2. Tabs\n\nBlock 1's implementation already handles these; "
                "no code change. Add tests for tabs.\n\n"
                "Interfaces:\n- Produces: (none)\n"
            ),
        )

        assert [f["block"] for f in scan_no_behaviour_blocks(path)] == [2]

    @pytest.mark.parametrize(
        "says",
        [
            "no code change is needed",
            "there is no code change in this block",
            "there are no new code changes",
        ],
    )
    def test_a_block_that_says_it_needs_no_code_is_flagged(self, tmp_path, says):
        """The no-code rule on its own: no earlier block is named, and no file."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\nAdd `count(s: str) -> int`.\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                f"## 2. Tabs\n\nTabs already split like spaces, so {says}. "
                "Pin it with a test for tabs.\n\n"
                "Interfaces:\n- Produces: (none)\n"
            ),
        )

        findings = scan_no_behaviour_blocks(path)

        assert [f["block"] for f in findings] == [2]
        assert "it says it needs no code" in findings[0]["description"]

    def test_a_block_that_names_only_test_files_is_flagged(self, tmp_path):
        """The block the live plan review saw before the prototype rewrite:
        it adds a test file and names no code to write, so there is nothing
        for its tests to wait for."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Summarize\n\nCreate `src/textkit/summary.py` with "
                "`summarize(text: str) -> str`.\n\n"
                "Interfaces:\n- Produces: `summarize(text: str) -> str`\n\n"
                "## 2. `tests/test_summary.py` scenario coverage\n\n"
                "Add `tests/test_summary.py` following the `tests/test_words.py` "
                'style. Assert every scenario verbatim: `summarize("hello") == '
                '"1 words, 1 lines"`, and that `textkit.summary.summarize` is '
                "reachable.\n\n"
                "Interfaces:\n- Produces: (none)\n"
                "- Consumes: Block 1: `summarize(text: str) -> str`\n"
            ),
        )

        assert [f["block"] for f in scan_no_behaviour_blocks(path)] == [2]

    def test_a_checkbox_block_that_names_only_test_files_is_flagged(self, tmp_path):
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\n- [ ] 1.1 Add `count` to `src/words.py`\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                "## 2. Whitespace\n\n- [ ] 2.1 Add tests for tabs to `tests/test_words.py`\n\n"
                "Interfaces:\n- Produces: (none)\n"
            ),
        )

        assert [f["block"] for f in scan_no_behaviour_blocks(path)] == [2]

    @pytest.mark.parametrize(
        "block",
        [
            # Every step tests or checks: none writes code ("Make sure" and
            # "Make no change" are checks, not code).
            "## 2. Tabs\n\n- [ ] 2.1 Write failing tests for tabs in "
            "`tests/test_words.py`\n- [ ] 2.2 Confirm `count` splits on tabs\n"
            "- [ ] 2.3 Make no change to `src/words.py`\n"
            "- [ ] 2.4 Make sure the suite passes\n\n",
            # Only its heading says it is test work.
            "## 2. Tests for tabs\n\nCover tabs in `tests/test_words.py`.\n\n",
            # Only its heading names a file, and that file is a test file.
            "## 2. Tab cases in `tests/test_words.py`\n\nCover tabs and runs "
            "of spaces.\n\n",
            # A step whose verb changes code, and whose object is a test file.
            "## 2. Tests for tabs\n\n- [ ] 2.1 Update `tests/test_words.py` with "
            "tab cases\n\n",
            # An "add" step whose object is a case, with no test word.
            "## 2. Tests for tabs\n\n- [ ] 2.1 Cover tabs in `tests/test_words.py`"
            "\n- [ ] 2.2 Add a case for an empty string\n\n",
            # "Change nothing" is not a change.
            "## 2. Tabs\n\n- [ ] 2.1 Write failing tests in `tests/test_words.py`"
            "\n- [ ] 2.2 Change nothing else\n\n",
            # An "add" step whose test file has no standalone test word.
            "## 2. Tabs\n\n- [ ] 2.1 Add `__tests__/words.js` covering tabs\n\n",
            # ... named after the first few words of the step.
            "## 2. Tests for tabs\n\n- [ ] 2.1 Add coverage of tabs and spaces "
            "to `__tests__/words.js`\n\n",
            # A step whose verb changes code, and whose object is a test, named
            # with no file.
            "## 2. Tests for tabs\n\n- [ ] 2.1 Cover tabs in `tests/test_words.py`"
            "\n- [ ] 2.2 Update the fixtures for tabs\n\n",
            "## 2. Tests for tabs\n\n- [ ] 2.1 Cover tabs in `tests/test_words.py`"
            "\n- [ ] 2.2 Fix the flaky test\n\n",
        ],
    )
    def test_a_test_block_whose_steps_write_no_code_is_flagged(
        self,
        tmp_path,
        block,
    ):
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\n- [ ] 1.1 Add `count` to `src/words.py`\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                + block
                + "Interfaces:\n- Produces: (none)\n"
            ),
        )

        assert [f["block"] for f in scan_no_behaviour_blocks(path)] == [2]

    @pytest.mark.parametrize(
        "block",
        [
            # A domain "Verify": new behaviour, a 401 that fails before it is built.
            "## 2. Verify webhook signatures\n\nVerify each incoming webhook's "
            "HMAC-SHA256 header against the shared secret and reject a mismatch "
            "with 401 before the body is stored.\n\n",
            # Same, naming its source file and its test file.
            "## 2. Verify webhook signatures\n\nVerify the `X-Signature` header in "
            "`src/app/webhooks.py` and reject a mismatch with 401. Tests go in "
            "`tests/test_webhooks.py`.\n\n",
            # A domain "Confirm", whose "unchanged" is about the user's file.
            "## 2. Confirm before overwriting an existing file\n\nWhen the target "
            'exists, prompt "Overwrite? [y/N]" and on N leave the existing file '
            "unchanged.\n\n",
            # A leaf block that uses something an earlier block built.
            "## 2. CLI word count\n\nAdd a `wc` CLI command using the tokenizer "
            "already built in Block 1, printing the count and exiting 0.\n\n",
            # It names one file it leaves alone, and one it changes.
            "## 2. JSON output\n\nAdd `--json` to `src/app/cli.py`; leave "
            "`src/app/core.py` unchanged. Tests in `tests/test_cli.py`.\n\n",
            # A domain "Verify" that names only its test file: the block need not
            # name its source file (TASKS_PROMPT does not ask it to).
            "## 2. Verify webhook signatures\n\nVerify each incoming webhook's "
            "HMAC signature and reject a mismatch with 401. Tests in "
            "`tests/test_webhook.py`.\n\n",
            "## 2. Summary flag\n\nAdd a `--summary` flag that prints the word "
            "count and exits 0. Tests in `tests/test_cli.py`.\n\n",
            # A side concern another block satisfies, with and without the file
            # this block changes.
            "## 2. Rate limits\n\nAdd a token-bucket limiter to "
            "`src/api/limits.py`. Authentication is already handled by Block 1's "
            "middleware.\n\n",
            "## 2. Rate limits\n\nAdd a token-bucket limiter that answers 429. "
            "Authentication is already handled by Block 1's middleware.\n\n",
            # "No code change" scoped to another part of the code.
            "## 2. Rate limits\n\nAdd a token-bucket limiter to "
            "`src/api/limits.py`, with no code change to the core.\n\n",
            "## 2. Rate limits\n\nAdd a token-bucket limiter that answers 429, "
            "with no code change to the core.\n\n",
            "## 2. Rate limits\n\nAdd a token-bucket limiter that answers 429, "
            "with no new code changes to the core.\n\n",
            "## 2. Rate limits\n\nAdd a token-bucket limiter that answers 429, "
            "with no new code needed in the core.\n\n",
            "## 2. Rate limits\n\nAdd a token-bucket limiter that answers 429, "
            "with no production code required outside the module.\n\n",
            # Test-first leaf blocks that name only their test file: the tests
            # come first, and a later step writes the code.
            "## 2. Webhook signatures\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_webhook.py`: a bad HMAC returns 401\n- [ ] 2.2 Implement "
            "HMAC-SHA256 verification of the signature header in the upload "
            "handler\n\n",
            "## 2. Webhook signatures\n\nWrite tests for HMAC rejection in "
            "`tests/test_webhook.py`, then implement signature checking in the "
            "upload handler.\n\n",
            "## 2. Tab splitting\n\n- [ ] 2.1 Write failing tests for tab "
            "splitting in `tests/test_words.py`\n- [ ] 2.2 Make count split on "
            "tabs\n\n",
            # The code step names the tests only as its purpose.
            "## 2. Webhook signatures\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_webhook.py`\n- [ ] 2.2 Add HMAC verification to the "
            "upload handler so the tests pass\n\n",
            # A code step whose object is a case of the behaviour, not a test.
            "## 2. Empty input\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_words.py`\n- [ ] 2.2 Handle the empty-string case in "
            "`count`\n\n",
            # The code step is to make the failing tests pass.
            "## 2. Tab splitting\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_words.py`\n- [ ] 2.2 Make the failing tests pass\n\n",
            "## 2. Tab splitting\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_words.py`\n- [ ] 2.2 Fix the failing tests\n\n",
            # An "add" step whose object only starts with the word "case".
            "## 2. Case-insensitive search\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_search.py`\n- [ ] 2.2 Add case-insensitive matching\n\n",
            "## 2. Snake case\n\n- [ ] 2.1 Write failing tests in "
            "`tests/test_names.py`\n- [ ] 2.2 Add snake case conversion\n\n",
            # A feature whose heading opens with a test word.
            "## 2. Coverage report subcommand\n\nAdd a `report` subcommand that "
            "prints line coverage. Tests in `tests/test_cli.py`.\n\n",
            # It names only a file it leaves alone, and adds tests and the code.
            "## 2. Reject empty input\n\nMake summarize raise ValueError on an "
            "empty string. Leave `pyproject.toml` unchanged. Add tests for the "
            "empty case.\n\n",
        ],
    )
    def test_a_leaf_block_that_adds_behaviour_is_not_flagged(self, tmp_path, block):
        """`Produces: (none)` only says nothing later calls the block, and
        "Verify"/"Confirm" are ordinary verbs for new behaviour."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Upload endpoint\n\nAdd `upload(body: bytes) -> Response`.\n\n"
                "Interfaces:\n- Produces: `upload(body: bytes) -> Response`\n\n"
                + block
                + "Interfaces:\n- Produces: (none)\n"
                "- Consumes: Block 1: `upload(body: bytes) -> Response`\n"
            ),
        )

        assert scan_no_behaviour_blocks(path) == []

    def test_the_suggestion_lets_a_block_that_adds_behaviour_say_so(self, tmp_path):
        """A false positive must cost one rewrite, never the block: the rewrite
        is told to keep a block that does add behaviour and name its code."""
        path = _tasks_file(
            tmp_path,
            (
                "## 1. Count words\n\n- [ ] 1.1 Add `count` to `src/words.py`\n\n"
                "Interfaces:\n- Produces: `count(s: str) -> int`\n\n"
                "## 2. Whitespace\n\n- [ ] 2.1 Add tests for tabs to `tests/test_words.py`\n\n"
                "Interfaces:\n- Produces: (none)\n"
            ),
        )

        finding = scan_no_behaviour_blocks(path)[0]

        assert "the earlier block that builds it" in finding["suggestion"]
        assert "name the code" in finding["suggestion"]
        assert "names no code to write" in finding["description"]

    def test_a_missing_tasks_file_yields_no_findings(self, tmp_path):
        assert scan_no_behaviour_blocks(tmp_path / "absent.md") == []


class TestTasksShapeAuditRejectsNoBehaviourBlocks:
    """The scan's findings join the shape audit's ONE regeneration pass, like
    the placeholder scan's, even when the LLM audit finds nothing."""

    @staticmethod
    def _setup(tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        cm = ChangeManager(specs_dir)
        cm.init_specs()
        change_dir = cm.create_change("nb")
        (change_dir / "proposal.md").write_text(
            "# Proposal\n## Capabilities\n- `cap-a`\n"
        )
        (change_dir / "design.md").write_text("# Design")

        config = MagicMock()
        config.reference_docs_text.return_value = ""
        config.budget_max_tokens = None
        config.budget_max_usd = None
        config.task_granularity = "checkboxes"
        config.change_name = "nb"
        config.specs_dir = specs_dir
        config.change_dir = change_dir
        config.model = "test-model"
        config.task_granularity = "blocks"
        config.state_file_path.return_value = change_dir / ".build-state.json"

        state = BuildState(
            change_name="nb",
            tier="standard",
            state_file=str(change_dir / ".build-state.json"),
            phase=BuildPhase.SPEC_GENERATION,
            spec_gen=SpecGenState(),
        )
        return cm, change_dir, config, state

    @pytest.mark.asyncio
    async def test_a_no_behaviour_block_forces_the_regeneration(self, tmp_path):
        cm, change_dir, config, state = self._setup(tmp_path)
        rewritten = "## 1. Summarize\n\nBuilds it, edge cases included.\n"

        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements/cap-a.md").write_text(
            "### Requirement: Slice\n#### Scenario: Works\nWHEN used\nTHEN returns a result\n"
        )
        config.pipeline_branch = None
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = [_E2E_TASKS.read_text(), rewritten]
            mock_audit.return_value = []

            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert mock_gen.call_count == 2
        assert (change_dir / "tasks.md").read_text() == rewritten
        regen_findings = mock_gen.call_args_list[1].kwargs["audit_findings"]
        assert "Block 2" in regen_findings
        assert "Block 3" in regen_findings
        assert state.spec_gen.step_passed("tasks_audit") is True

    @pytest.mark.asyncio
    async def test_the_model_audits_no_new_behaviour_finding_reaches_the_regeneration(
        self,
        tmp_path,
    ):
        """A block the deterministic scan cannot see, reported by the model
        audit under the new category, still drives the one rewrite."""
        cm, change_dir, config, state = self._setup(tmp_path)
        first = (
            "## 1. Summarize\n\nCreate `src/summary.py`.\n\n"
            "Interfaces:\n- Produces: `summarize(text: str) -> str`\n\n"
            "## 2. Pin plurals\n\nPin the plural nouns.\n\n"
            "Interfaces:\n- Produces: (none)\n"
        )
        rewritten = "## 1. Summarize\n\nBuilds it, plurals included.\n"
        model_finding = {
            "category": "no-new-behaviour",
            "severity": "critical",
            "description": "Block 2 only pins what Block 1 builds",
            "suggestion": "Move its criteria into Block 1",
        }

        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements/cap-a.md").write_text(
            "### Requirement: Slice\n#### Scenario: Works\nWHEN used\nTHEN returns a result\n"
        )
        config.pipeline_branch = None
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = [first, rewritten]
            mock_audit.side_effect = [[model_finding], _resolved("TA-F1")]

            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert mock_gen.call_count == 2
        regen_findings = mock_gen.call_args_list[1].kwargs["audit_findings"]
        assert "Block 2 only pins what Block 1 builds" in regen_findings
        assert (change_dir / "tasks.md").read_text() == rewritten

    @staticmethod
    def _minor_model_case(tmp_path):
        """A plan the deterministic scan passes, and a model finding under the
        no-new-behaviour category that the model rated minor."""
        cm, change_dir, config, state = (
            TestTasksShapeAuditRejectsNoBehaviourBlocks._setup(tmp_path)
        )
        first = (
            "## 1. Summarize\n\nCreate `src/summary.py`.\n\n"
            "Interfaces:\n- Produces: `summarize(text: str) -> str`\n\n"
            "## 2. Pin plurals\n\nPin the plural nouns.\n\n"
            "Interfaces:\n- Produces: (none)\n"
        )
        finding = {
            "category": "no-new-behaviour",
            "severity": "minor",
            "description": "Block 2 only pins what Block 1 builds",
            "suggestion": "Move its criteria into Block 1",
        }
        (change_dir / "requirements").mkdir(exist_ok=True)
        (change_dir / "requirements/cap-a.md").write_text(
            "### Requirement: Slice\n#### Scenario: Works\nWHEN used\nTHEN returns a result\n"
        )
        config.pipeline_branch = None
        return cm, change_dir, config, state, first, finding

    @pytest.mark.asyncio
    async def test_a_minor_no_new_behaviour_finding_still_drives_the_rewrite(
        self, tmp_path
    ):
        """The audit prompt leaves this category's severity to the model, so a
        block it rates minor is still a block whose tests cannot fail first."""
        cm, change_dir, config, state, first, finding = self._minor_model_case(tmp_path)
        rewritten = "## 1. Summarize\n\nBuilds it, plurals included.\n"
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = [first, rewritten]
            mock_audit.side_effect = [[finding], _resolved("TA-F1")]

            await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert mock_gen.call_count == 2
        assert (
            "Block 2 only pins what Block 1 builds"
            in mock_gen.call_args_list[1].kwargs["audit_findings"]
        )
        assert (change_dir / "tasks.md").read_text() == rewritten

    @pytest.mark.asyncio
    async def test_a_minor_no_new_behaviour_finding_on_the_rewrite_blocks_it(
        self, tmp_path
    ):
        cm, change_dir, config, state, first, finding = self._minor_model_case(tmp_path)
        with (
            patch(
                "build_pipeline.spec_generator.generate_rubric",
                AsyncMock(return_value="# Rubric"),
            ),
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = [first, first]
            mock_audit.side_effect = [[finding], [finding]]

            with pytest.raises(ValueError, match="blocking shape findings"):
                await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert (change_dir / "tasks.md").read_text() == first
        assert not state.spec_gen.step_passed("tasks_audit")

    @pytest.mark.asyncio
    async def test_a_block_left_after_the_rewrite_blocks_completion(
        self,
        tmp_path,
        capsys,
    ):
        """One rewrite, then the plan stands, as every audit here: but a
        block the scan still flags is named, so the RED-gate failure that
        follows is not a surprise."""
        cm, change_dir, config, state = self._setup(tmp_path)

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.generate_artifact",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_gen.side_effect = [_E2E_TASKS.read_text(), _E2E_TASKS.read_text()]
            mock_audit.return_value = []

            with pytest.raises(ValueError, match="no behavior"):
                await _generate_single_artifact(cm, change_dir, "tasks", config, state)

        assert mock_gen.call_count == 2
        assert not state.spec_gen.step_passed("tasks_audit")
        assert state.spec_gen.repair_attempts("tasks_audit")


def test_plan_rewrite_invalidates_review_and_rubric(tmp_path):
    from build_pipeline.config import BuildConfig
    from build_pipeline.spec_generator import reconcile_plan
    from build_pipeline.state import BuildState, SpecGenState, StepOutcome

    config = BuildConfig(project_dir=tmp_path, change_name="revision")
    config.specs_dir = tmp_path / "specs"
    config.change_dir.mkdir(parents=True)
    (config.change_dir / "tasks.md").write_text(
        "## 1. Original\nImplement original behavior\n"
    )
    (config.change_dir / "design.md").write_text("Original design")
    (config.change_dir / "rubric.md").write_text("Old rubric")
    state = BuildState(change_name="revision", tier="standard", spec_gen=SpecGenState())
    reconcile_plan(config, state)
    state.spec_gen.steps["plan_review"] = StepOutcome(
        revision=state.spec_gen.revision, outcome="passed"
    )
    (config.change_dir / "tasks.md").write_text(
        "## 1. Changed\nImplement changed behavior\n"
    )
    reconcile_plan(config, state)
    assert not state.spec_gen.step_passed("plan_review")
    assert "rubric" in state.spec_gen.stale_artifacts
