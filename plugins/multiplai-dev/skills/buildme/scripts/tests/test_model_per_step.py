"""A model and an effort per step: `models:` / `efforts:` in specs/config.yaml,
BuildConfig.model_for and BuildConfig.effort_for.

Groups: with no `models:` key every step runs on today's model; with no
config at all every step follows MULTIPLAI_MODEL / MULTIPLAI_EFFORT, then the
session; each chain's precedence; a `pi:` spec reaching `run_agent`
unchanged; the tier; and every model-bearing call site mapped to a step,
passing that step's effort.
"""

from __future__ import annotations

import argparse
import ast
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.config import (
    MODEL_STAGES,
    STEP_STAGES,
    BuildConfig,
    detect_tier,
)
from multiplai_core.env import CURRENT_MODEL

PIPELINE = Path(__file__).resolve().parent.parent / "build_pipeline"
PI_SPEC = "pi:deepseek/deepseek-v4-flash@fireworks"


@pytest.fixture
def no_conf(tmp_path):
    """An empty multiplai.conf, and no buildme, MULTIPLAI_* or session env vars."""
    home = tmp_path / "multiplai-home"
    home.mkdir()
    (home / "multiplai.conf").write_text("")
    with patch.dict(os.environ, {"CLAUDE_MULTIPLAI_HOME": str(home)}, clear=True):
        yield


SESSION = {
    "MULTIPLAI_SESSION_MODEL": "claude-test-session",
    "MULTIPLAI_SESSION_EFFORT": "low",
}


def _config(tmp_path, yaml_text: str = "", **fields) -> BuildConfig:
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    if yaml_text:
        (specs / "config.yaml").write_text(yaml_text)
    config = BuildConfig(project_dir=tmp_path, **fields)
    config.specs_dir = specs
    config._load_specs_config()
    return config


# What each step's call site passed as `model=` before model_for existed.
TODAY = {step: (lambda c: c.model) for step in STEP_STAGES}
TODAY.update(
    {
        "research": lambda c: c.spec_model,
        "review": lambda c: c.review_model or c.model,
        "plan_review": lambda c: c.plan_review_model or c.review_model,
        "integration_fix_escalated": lambda c: c.review_model or c.model,
        # New in 0.89.0, on the review stage like integration_fix_escalated.
        "review_fix_escalated": lambda c: c.review_model or c.model,
        "findings_triage": lambda c: c.review_model or c.model,
    }
)


class TestNoConfigEquivalence:
    def test_the_table_covers_every_step(self):
        assert set(TODAY) == set(STEP_STAGES)

    @pytest.mark.parametrize("model", [None, "claude-opus-4-6"])
    def test_every_step_runs_on_todays_model(self, tmp_path, no_conf, model):
        config = _config(tmp_path, **({"model": model} if model else {}))
        assert config.model_specs == {}
        for step in STEP_STAGES:
            assert config.model_for(step) == TODAY[step](config), step

    def test_a_dated_model_is_not_repinned(self, tmp_path, no_conf):
        """Links 3 and 5 are returned as loaded; resolving them again would
        turn claude-opus-4-6 into the current opus ID."""
        config = _config(
            tmp_path, model="claude-opus-4-6", review_model="claude-sonnet-4-6"
        )
        assert config.model_for("implementer") == "claude-opus-4-6"
        assert config.model_for("review") == "claude-sonnet-4-6"


class TestSessionDefaults:
    """Done means 7: with nothing configured every step follows the session;
    MULTIPLAI_MODEL moves every step's model; `models:` / `efforts:` move only
    the steps they name."""

    def test_there_are_24_steps(self):
        assert len(STEP_STAGES) == 24

    def test_every_step_follows_the_session(self, tmp_path, no_conf):
        with patch.dict(os.environ, SESSION):
            config = _config(tmp_path)
            for step in STEP_STAGES:
                assert (config.model_for(step), config.effort_for(step)) == (
                    "claude-test-session",
                    "low",
                ), step

    def test_multiplai_model_moves_every_step_but_not_the_effort(
        self, tmp_path, no_conf
    ):
        with patch.dict(
            os.environ, {**SESSION, "MULTIPLAI_MODEL": "claude-test-default"}
        ):
            config = _config(tmp_path)
            for step in STEP_STAGES:
                assert (config.model_for(step), config.effort_for(step)) == (
                    "claude-test-default",
                    "low",
                ), step

    def test_multiplai_effort_moves_every_step(self, tmp_path, no_conf):
        with patch.dict(os.environ, {**SESSION, "MULTIPLAI_EFFORT": "high"}):
            config = _config(tmp_path)
            assert {config.effort_for(step) for step in STEP_STAGES} == {"high"}

    @pytest.mark.parametrize("env", [{}, {"MULTIPLAI_MODEL": "claude-test-default"}])
    def test_models_implementer_moves_only_the_implementer(
        self, tmp_path, no_conf, env
    ):
        with patch.dict(os.environ, {**SESSION, **env}):
            config = _config(tmp_path, "models:\n  implementer: sonnet\n")
            rest = env.get("MULTIPLAI_MODEL", "claude-test-session")
            for step in STEP_STAGES:
                want = CURRENT_MODEL["sonnet"] if step == "implementer" else rest
                assert config.model_for(step) == want, step

    def test_efforts_review_moves_only_the_review_stage(self, tmp_path, no_conf):
        with patch.dict(os.environ, SESSION):
            config = _config(tmp_path, "efforts:\n  review: high\n")
            for step, stage in STEP_STAGES.items():
                assert config.effort_for(step) == (
                    "high" if stage == "review" else "low"
                ), step

    def test_nothing_anywhere_leaves_the_cli_to_choose(self, tmp_path, no_conf):
        config = _config(tmp_path)
        for step in STEP_STAGES:
            assert (config.model_for(step), config.effort_for(step)) == (None, None), (
                step
            )


class TestEffortPrecedence:
    """efforts.<step> → efforts.<stage> → [buildme.<stage>] EFFORT= →
    efforts.default → [buildme] EFFORT= → MULTIPLAI_EFFORT → the session."""

    def test_step_beats_stage(self, tmp_path, no_conf):
        config = _config(tmp_path, "efforts:\n  implementer: max\n  agent: low\n")
        assert config.effort_for("implementer") == "max"
        assert config.effort_for("refactor") == "low"

    def test_stage_beats_the_per_stage_field(self, tmp_path, no_conf):
        config = _config(tmp_path, "efforts:\n  review: low\n", review_effort="high")
        assert config.effort_for("review") == "low"

    def test_the_per_stage_field_beats_default(self, tmp_path, no_conf):
        config = _config(tmp_path, "efforts:\n  default: low\n", agent_effort="high")
        assert config.effort_for("implementer") == "high"
        assert config.effort_for("review") == "low"

    def test_default_beats_the_pipeline_effort(self, tmp_path, no_conf):
        config = _config(tmp_path, "efforts:\n  default: low\n", effort="high")
        assert config.effort_for("rubric") == "low"

    def test_the_pipeline_effort_beats_multiplai_effort(self, tmp_path, no_conf):
        with patch.dict(os.environ, {"MULTIPLAI_EFFORT": "low"}):
            config = _config(tmp_path, effort="high")
            assert {config.effort_for(step) for step in STEP_STAGES} == {"high"}

    def test_a_copied_field_is_not_a_choice(self, tmp_path, no_conf):
        """`effort` is copied into spec/agent/review_effort; the copy must not
        beat `efforts.default`."""
        config = _config(tmp_path, "efforts:\n  default: low\n", effort="high")
        assert config.agent_effort == "high"
        assert config.effort_for("implementer") == "low"

    def test_plan_review_falls_back_to_review_effort(self, tmp_path, no_conf):
        config = _config(tmp_path, review_effort="high")
        assert config.effort_for("plan_review") == "high"
        config = _config(tmp_path, review_effort="high", plan_review_effort="low")
        assert config.effort_for("plan_review") == "low"

    def test_the_conf_stage_section_sets_the_stage(self, tmp_path, no_conf):
        conf = Path(os.environ["CLAUDE_MULTIPLAI_HOME"]) / "multiplai.conf"
        conf.write_text("[buildme.agent]\nEFFORT=max\n")
        config = _config(tmp_path)
        assert config.effort_for("implementer") == "max"
        assert config.effort_for("rubric") is None

    def test_an_unknown_key_or_effort_is_dropped_with_a_warning(
        self, tmp_path, no_conf, caplog
    ):
        with caplog.at_level(logging.WARNING):
            config = _config(
                tmp_path, "efforts:\n  implementor: high\n  review: turbo\n"
            )
        assert config.effort_specs == {}
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "efforts.implementor" in text and "efforts.review" in text

    def test_an_unknown_step_is_a_programming_error(self, tmp_path, no_conf):
        with pytest.raises(KeyError):
            _config(tmp_path).effort_for("nope")


class TestPrecedence:
    YAML = "models:\n  test_writer: opus\n  agent: fable\n  default: sonnet\n"

    def test_step_beats_stage(self, tmp_path, no_conf):
        config = _config(
            tmp_path, self.YAML, model="model-x", agent_model="claude-haiku-4-5"
        )
        assert config.model_for("test_writer") == CURRENT_MODEL["opus"]

    def test_stage_beats_the_per_stage_field(self, tmp_path, no_conf):
        config = _config(
            tmp_path, self.YAML, model="model-x", agent_model="claude-haiku-4-5"
        )
        assert config.model_for("implementer") == CURRENT_MODEL["fable"]

    def test_the_per_stage_field_beats_default(self, tmp_path, no_conf):
        config = _config(
            tmp_path,
            "models:\n  default: sonnet\n",
            model="model-x",
            agent_model="claude-haiku-4-5",
        )
        assert config.model_for("implementer") == "claude-haiku-4-5"

    def test_default_beats_model(self, tmp_path, no_conf):
        config = _config(tmp_path, "models:\n  default: sonnet\n", model="model-x")
        assert config.model_for("implementer") == CURRENT_MODEL["sonnet"]

    def test_model_comes_last(self, tmp_path, no_conf):
        config = _config(tmp_path, model="model-x")
        assert config.model_for("implementer") == "model-x"

    def test_a_copied_field_is_not_a_choice(self, tmp_path, no_conf):
        """agent_model copied from model must not beat models.default."""
        config = _config(tmp_path, "models:\n  default: sonnet\n", model="model-x")
        assert config.agent_model == "model-x"
        assert config.model_for("test_writer") == CURRENT_MODEL["sonnet"]

    def test_plan_review_falls_back_to_review_model_before_default(
        self, tmp_path, no_conf
    ):
        config = _config(
            tmp_path,
            "models:\n  default: sonnet\n",
            model="model-x",
            review_model="claude-haiku-4-5",
        )
        assert config.model_for("plan_review") == "claude-haiku-4-5"

    def test_review_model_moves_the_review_stage_but_not_adjudication(
        self, tmp_path, no_conf
    ):
        config = _config(tmp_path, model="model-x", review_model="claude-haiku-4-5")
        for step in (
            "review",
            "plan_review",
            "test_quality_audit",
            "integration_fix_escalated",
            "review_fix_escalated",
        ):
            assert config.model_for(step) == "claude-haiku-4-5", step
        assert config.model_for("adjudication") == "model-x"

    def test_a_claude_spec_is_not_capped(self, tmp_path, no_conf):
        """MULTIPLAI_MODEL is a default, not a cap (multiplai-core 0.21)."""
        with patch.dict(os.environ, {"MULTIPLAI_MODEL": CURRENT_MODEL["opus"]}):
            config = _config(tmp_path, "models:\n  review: claude:fable\n")
            assert config.model_for("review") == CURRENT_MODEL["fable"]

    def test_an_unknown_step_is_a_programming_error(self, tmp_path, no_conf):
        with pytest.raises(KeyError):
            _config(tmp_path).model_for("nonexistent")

    def test_an_unknown_key_is_dropped_with_a_warning(self, tmp_path, no_conf, caplog):
        with caplog.at_level(logging.WARNING):
            config = _config(tmp_path, "models:\n  test_writr: opus\n")
        assert config.model_specs == {}
        assert any("models.test_writr" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("table", ["models", "efforts"])
    @pytest.mark.parametrize("step", ["design_audit", "tasks_audit"])
    def test_a_retired_audit_key_has_no_effect_and_names_plan_review(
        self, tmp_path, no_conf, caplog, table, step
    ):
        """One plan check replaced the design and tasks audits, so their
        `models:` and `efforts:` keys do nothing; the warning says which key
        applies now."""
        value = "opus" if table == "models" else "high"
        with caplog.at_level(logging.WARNING):
            config = _config(tmp_path, f"{table}:\n  {step}: {value}\n")
        assert config.model_specs == {}
        assert config.effort_specs == {}
        [message] = [
            r.getMessage()
            for r in caplog.records
            if f"{table}.{step}" in r.getMessage()
        ]
        assert "no longer has any effect" in message
        assert f"{table}.plan_review" in message

    def test_every_documented_spec_form_is_kept(self, tmp_path, no_conf):
        forms = [
            "haiku",
            "sonnet",
            "opus",
            "fable",
            "claude:opus",
            "anthropic:claude-opus-4-8",
            PI_SPEC,
        ]
        for form in forms:
            config = _config(tmp_path, f"models:\n  default: '{form}'\n")
            assert config.model_specs == {"default": form}, form


class TestConfModel:
    """`conf_model` keeps a Pi model instead of dropping it."""

    def test_a_pi_model_in_a_step_section_is_kept(self, tmp_path, no_conf):
        from build_pipeline.config import conf_model

        conf = Path(os.environ["CLAUDE_MULTIPLAI_HOME"]) / "multiplai.conf"
        conf.write_text(f"[buildme.agent]\nMODEL={PI_SPEC}\n")
        assert conf_model("buildme.agent") == PI_SPEC
        assert _config(tmp_path).model_for("implementer") == PI_SPEC

    def test_a_provider_with_no_runner_is_still_dropped(
        self, tmp_path, no_conf, caplog
    ):
        from build_pipeline.config import conf_model

        conf = Path(os.environ["CLAUDE_MULTIPLAI_HOME"]) / "multiplai.conf"
        conf.write_text("[buildme.agent]\nMODEL=openai:gpt-5\n")
        with caplog.at_level(logging.WARNING):
            assert conf_model("buildme.agent") is None
        assert any("no runner" in r.getMessage() for r in caplog.records)


class TestPassThrough:
    @pytest.mark.asyncio
    async def test_a_pi_spec_reaches_run_agent_unchanged(
        self, tmp_path, no_conf, monkeypatch
    ):
        from build_pipeline.llm_steps.tdd_steps import run_test_writer

        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        config = _config(tmp_path, f"models:\n  test_writer: '{PI_SPEC}'\n")
        run = AsyncMock()
        run.return_value.text = "STATUS: DONE"
        run.return_value.turns = 1
        run.return_value.files_changed = []
        with patch("build_pipeline.sdk.run_agent", run):
            await run_test_writer(
                "b",
                "d",
                "specs",
                "ctx",
                "pytest",
                model=config.model_for("test_writer"),
                cwd=str(tmp_path),
            )
        assert run.await_args.kwargs["model"] == PI_SPEC

    @pytest.mark.asyncio
    async def test_a_pi_spec_reaches_run_agent_on_a_toolless_call(
        self, tmp_path, no_conf
    ):
        from build_pipeline.sdk import llm_call

        config = _config(tmp_path, f"models:\n  spec: '{PI_SPEC}'\n")
        run = AsyncMock()
        run.return_value.text = "answer"
        run.return_value.turns = 1
        run.return_value.files_changed = []
        with patch("build_pipeline.sdk.run_agent", run):
            await llm_call("p", model=config.model_for("rubric"), budget_label="rubric")
        assert run.await_args.kwargs["model"] == PI_SPEC


class TestTier:
    @pytest.mark.parametrize("model", ["claude-fable-5-1", "fable"])
    def test_fable_is_advanced(self, model, no_conf):
        with patch.dict(os.environ, {"MULTIPLAI_MODEL": model}):
            assert detect_tier()[0] == "advanced"

    def test_a_non_anthropic_default_is_standard_and_says_why(self, caplog):
        with patch.dict(os.environ, {}, clear=True), caplog.at_level(logging.INFO):
            tier, name = detect_tier(PI_SPEC)
        assert (tier, name) == ("standard", PI_SPEC)
        assert any("not a Claude model" in r.getMessage() for r in caplog.records)

    def test_buildme_tier_pins_a_non_anthropic_default(self):
        with patch.dict(os.environ, {"BUILDME_TIER": "advanced"}, clear=True):
            assert detect_tier(PI_SPEC, configured_tier="standard")[0] == "advanced"

    def test_config_yaml_tier_pins_a_non_anthropic_default(self, tmp_path, no_conf):
        (tmp_path / "specs").mkdir()
        (tmp_path / "specs" / "config.yaml").write_text(
            f"tier: advanced\nmodels:\n  default: '{PI_SPEC}'\n"
        )
        args = argparse.Namespace(project_dir=str(tmp_path), change="feat")
        config = BuildConfig.from_cli_args(args)
        assert (config.tier, config.model_name) == ("advanced", PI_SPEC)

    def test_tier_does_not_override_a_claude_default(self, tmp_path, no_conf):
        with patch.dict(os.environ, {"MULTIPLAI_MODEL": "claude-sonnet-5"}):
            assert detect_tier(None, configured_tier="advanced")[0] == "standard"


# Every call that hands a model to the SDK, directly or through a step
# function: (file, enclosing function, callee) → (the `model=` source, the
# step(s) that source names). Sites whose model is a parameter or a local
# name the step that fills it.
CALL_SITES = {
    ("findings/code.py", "run_self_check", "agent_call"): (
        "config.model_for('findings_self_check')",
        ("findings_self_check",),
    ),
    ("epic/merge.py", "run_merge_agent", "agent_call"): (
        "config.model_for('merge')",
        ("merge",),
    ),
    ("findings/triage.py", "triage_comment", "call"): (
        "config.model_for('findings_triage')",
        ("findings_triage",),
    ),
    ("llm_steps/docs_steps.py", "run_docs_update", "agent_call"): (
        "config.model_for('docs_update')",
        ("docs_update",),
    ),
    ("llm_steps/plan_review_steps.py", "run_plan_edit", "agent_call_structured"): (
        "config.model_for('plan_rewrite')",
        ("plan_rewrite",),
    ),
    ("llm_steps/plan_review_steps.py", "run_plan_review", "agent_call_structured"): (
        "model",
        ("plan_review",),
    ),  # model = config.model_for("plan_review")
    ("llm_steps/research_steps.py", "decide_research", "call"): (
        "config.model_for('research')",
        ("research",),
    ),
    ("llm_steps/respec_steps.py", "run_respec_audit", "llm_call"): (
        "config.model_for('respec')",
        ("respec",),
    ),
    ("llm_steps/review_steps.py", "run_code_review", "agent_call_structured"): (
        "m",
        ("review",),
    ),  # _panel_models → config.model_for("review")
    ("llm_steps/review_steps.py", "run_review_fix", "agent_call"): (
        "config.model_for('review_fix')",
        ("review_fix",),
    ),
    ("llm_steps/spec_steps.py", "generate_artifact", "agent_call"): (
        "config.model_for('spec_artifact')",
        ("spec_artifact",),
    ),  # design and tasks
    ("llm_steps/spec_steps.py", "generate_artifact", "llm_call"): (
        "config.model_for('spec_artifact')",
        ("spec_artifact",),
    ),  # unknowns
    ("llm_steps/spec_steps.py", "run_explainer", "llm_call"): (
        "config.model_for('explainer')",
        ("explainer",),
    ),
    ("llm_steps/spec_steps.py", "_codebase_analysis_call", "agent_call_structured"): (
        "config.model_for('codebase_analysis')",
        ("codebase_analysis",),
    ),
    ("llm_steps/tdd_steps.py", "run_test_writer", "agent_call"): (
        "model",
        ("test_writer",),
    ),
    ("llm_steps/tdd_steps.py", "run_implementer", "agent_call"): (
        "model",
        ("implementer", "review_fix"),
    ),
    ("llm_steps/tdd_steps.py", "run_refactorer", "agent_call"): (
        "model",
        ("refactor",),
    ),
    ("llm_steps/tdd_steps.py", "run_refactor_all", "agent_call"): (
        "model",
        ("refactor_all",),
    ),
    ("llm_steps/tdd_steps.py", "run_mutation_test_writer", "agent_call"): (
        "model",
        ("mutation_test_writer",),
    ),
    ("llm_steps/tdd_steps.py", "run_integration_fix", "agent_call"): (
        "model",
        ("integration_fix", "integration_fix_escalated"),
    ),
    ("rubric.py", "generate_rubric", "llm_call"): (
        "config.model_for('rubric')",
        ("rubric",),
    ),
    ("tdd_engine.py", "_audit_test_quality", "llm_call_structured"): (
        "config.model_for('test_quality_audit')",
        ("test_quality_audit",),
    ),
    ("tdd_engine.py", "_adjudicate_review_findings", "llm_call_structured"): (
        "config.model_for('adjudication')",
        ("adjudication",),
    ),
    ("tdd_engine.py", "_enforce_test_quality", "run_test_writer"): (
        "config.model_for('test_writer')",
        ("test_writer",),
    ),
    ("tdd_engine.py", "_enforce_red_gate", "run_test_writer"): (
        "config.model_for('test_writer')",
        ("test_writer",),
    ),
    ("tdd_engine.py", "_write_block_tests", "run_test_writer"): (
        "config.model_for('test_writer')",
        ("test_writer",),
    ),
    ("tdd_engine.py", "run_block_tdd", "run_implementer"): (
        "config.model_for('implementer')",
        ("implementer",),
    ),
    ("tdd_engine.py", "_refactor_block", "run_refactorer"): (
        "config.model_for('refactor')",
        ("refactor",),
    ),
    ("tdd_engine.py", "_run_integration_and_review", "run_integration_fix"): (
        "fix_model",
        ("integration_fix", "integration_fix_escalated"),
    ),
    ("tdd_engine.py", "_run_end_suite", "run_integration_fix"): (
        "fix_model",
        ("integration_fix", "integration_fix_escalated"),
    ),
    ("tdd_engine.py", "_run_integration_and_review", "run_implementer"): (
        "fix_model",
        ("review_fix", "review_fix_escalated"),
    ),
    ("tdd_engine.py", "_run_fix_on_pass", "run_implementer"): (
        "config.model_for('review_fix')",
        ("review_fix",),
    ),
    ("tdd_engine.py", "_run_refactor_all", "run_refactor_all"): (
        "config.model_for('refactor_all')",
        ("refactor_all",),
    ),
    ("tdd_engine.py", "_run_mutation_round", "run_mutation_test_writer"): (
        "config.model_for('mutation_test_writer')",
        ("mutation_test_writer",),
    ),
}

SDK_CALLS = {"llm_call", "agent_call", "llm_call_structured", "agent_call_structured"}


def _model_call_sites(kwarg: str = "model") -> dict[tuple[str, str, str], str | None]:
    """Every call to an SDK function, or passing `model=` or `budget_label=`,
    and the source of its *kwarg* argument."""
    found: dict[tuple[str, str, str], str | None] = {}

    def walk(node, path: str, func: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call):
                callee = getattr(child.func, "id", None) or getattr(
                    child.func, "attr", None
                )
                kwargs = {k.arg: k.value for k in child.keywords}
                if callee in SDK_CALLS or "model" in kwargs or "budget_label" in kwargs:
                    key = (path, name, callee)
                    assert key not in found, f"two sites share {key}; extend the key"
                    found[key] = ast.unparse(kwargs[kwarg]) if kwarg in kwargs else None
            walk(child, path, name)

    for file in sorted(PIPELINE.rglob("*.py")):
        if file.name == "sdk.py":
            continue
        walk(ast.parse(file.read_text()), file.relative_to(PIPELINE).as_posix(), None)
    return found


class TestCallSiteCoverage:
    def test_every_call_site_is_in_the_table(self):
        found = _model_call_sites()
        assert set(found) == set(CALL_SITES)
        for key, source in found.items():
            assert source == CALL_SITES[key][0], key

    def test_a_literal_model_for_names_exactly_its_step(self):
        for key, (source, steps) in CALL_SITES.items():
            if source and source.startswith("config.model_for('"):
                assert (
                    source == f"config.model_for('{steps[0]}')" and len(steps) == 1
                ), key

    def test_every_step_is_reached_and_every_name_is_a_step(self):
        named = {step for _, steps in CALL_SITES.values() for step in steps}
        assert named == set(STEP_STAGES)

    def test_every_stage_is_known(self):
        assert set(STEP_STAGES.values()) == set(MODEL_STAGES)

    def test_every_call_site_passes_its_steps_effort(self):
        """A site asking `model_for(x)` asks `effort_for(x)`; a step function
        that takes `model` takes `effort` too."""
        other = {
            "model": "effort",
            "fix_model": "config.effort_for(fix_step)",
            "m": "config.effort_for('review')",
            None: None,
        }
        for key, effort in _model_call_sites("effort").items():
            model = CALL_SITES[key][0]
            want = (
                model.replace("model_for", "effort_for")
                if model and model.startswith("config.model_for(")
                else other[model]
            )
            assert effort == want, key
