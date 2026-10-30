"""`planning.analysis` (plan item 64): the design prompt gets only its component's facts.

Off (`full`, the default) the design prompt carries the whole codebase
analysis, byte for byte as before the setting existed. On (`component`) it
carries the facts about the files the component's plan.md and specs name, every
convention, and no "Not analysed" list.
"""

from build_pipeline.change_manager import ChangeManager
from build_pipeline.code_analysis import (
    NO_COMPONENT_FACTS,
    component_facts,
    no_commit_report,
)
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.spec_steps import _build_prompt
from build_pipeline.spec_generator import read_codebase_analysis
from build_pipeline.state import BuildState, SpecGenState

REPORT = """\
Every `path:line` below was re-read at commit `abc123`. 1 claim(s) whose citations were not found there were dropped.

## Architecture

- users.py: the login model. ([`app/users.py:1-9`](../../app/users.py#L1))
- billing.py: invoices. ([`app/billing.py:3`](../../app/billing.py#L3))
- The session reads the role from both. ([`app/session.py:4`](../../app/session.py#L4), [`app/users.py:12`](../../app/users.py#L12))

## Patterns

- Term **Utente**: a login. ([`app/users.py:2`](../../app/users.py#L2))
- Gotcha in billing. ([`app/billing.py:8`](../../app/billing.py#L8))

## Dependencies

- Django 4.2. ([`requirements.txt:1`](../../requirements.txt#L1))

## Conventions

- Tests use pytest-django. ([`app/tests/test_x.py:1`](../../app/tests/test_x.py#L1))
- Models live in models.py. ([`app/billing.py:1`](../../app/billing.py#L1))

## Not analysed

Left out by `codebase_analysis_max_units` (1 file(s)):
- `app/huge.py`
Read, but no fact about them passed the check:
- `app/other.py`
"""

PLAN = "# Plan\n\n- Change `app/users.py` so a login belongs to an account.\n"
SPECS = (
    "# Capability: accounts\n\n## Requirement: Account\nThe session uses users.py.\n"
)

CONTEXT = {
    "template": "TEMPLATE",
    "instruction": "INSTRUCTION",
    "context": "PROJECT",
    "dependencies": {"proposal": "proposal.md"},
}


def _setup(tmp_path, yaml_text: str = ""):
    project = tmp_path / "project"
    specs = project / "specs"
    cm = ChangeManager(specs)
    cm.init_specs()
    change_dir = cm.create_change("feat")
    (change_dir / "proposal.md").write_text("proposal body")
    (change_dir / "plan.md").write_text(PLAN)
    (change_dir / "specs.md").write_text(SPECS)
    report = change_dir / "codebase-analysis.md"
    report.write_text(REPORT)
    (specs / "config.yaml").write_text(yaml_text)
    config = BuildConfig(project_dir=project, change_name="feat")
    config.specs_dir = specs
    config._load_specs_config()
    state = BuildState(
        change_name="feat",
        tier="standard",
        spec_gen=SpecGenState(codebase_analysis_path=str(report)),
    )
    return config, state, report


class TestConfig:
    def test_defaults_to_full(self, tmp_path):
        config, _, _ = _setup(tmp_path)
        assert config.planning_analysis == "full"
        assert BuildConfig().planning_analysis == "full"

    def test_component_loads_from_the_planning_block(self, tmp_path):
        config, _, _ = _setup(tmp_path, "planning:\n  analysis: component\n")
        assert config.planning_analysis == "component"

    def test_an_unknown_value_warns_and_keeps_full(self, tmp_path, caplog):
        config, _, _ = _setup(tmp_path, "planning:\n  analysis: sliced\n")
        assert config.planning_analysis == "full"
        assert "planning.analysis" in caplog.text


class TestOff:
    def test_design_prompt_is_byte_identical_to_main(self, tmp_path):
        """Main passed the report file's text straight into the design prompt."""
        config, state, report = _setup(tmp_path)
        main_prompt = _build_prompt(
            "design", CONTEXT, config, codebase_analysis=report.read_text()
        )
        prompt = _build_prompt(
            "design",
            CONTEXT,
            config,
            codebase_analysis=read_codebase_analysis(state, config),
        )
        assert prompt.encode() == main_prompt.encode()
        assert read_codebase_analysis(state, config) == read_codebase_analysis(state)
        assert "## Not analysed" in prompt


class TestOn:
    def test_keeps_named_files_facts_and_every_convention(self, tmp_path):
        config, state, _ = _setup(tmp_path, "planning:\n  analysis: component\n")
        text = read_codebase_analysis(state, config)

        assert "users.py: the login model." in text
        assert "The session reads the role from both." in text
        assert "Term **Utente**" in text
        assert "billing.py: invoices." not in text
        assert "Gotcha in billing." not in text
        assert "Django 4.2." not in text
        assert "Tests use pytest-django." in text
        assert "Models live in models.py." in text
        assert "re-read at commit `abc123`" in text
        assert "(5 of 8)" in text

    def test_the_not_analysed_list_never_enters_the_design_prompt(self, tmp_path):
        config, state, _ = _setup(tmp_path, "planning:\n  analysis: component\n")
        prompt = _build_prompt(
            "design",
            CONTEXT,
            config,
            codebase_analysis=read_codebase_analysis(state, config),
        )
        assert "Not analysed" not in prompt
        assert "app/huge.py" not in prompt
        assert "app/other.py" not in prompt

    def test_a_section_with_no_named_fact_says_so(self):
        out = component_facts(REPORT, "nothing named here")
        assert out.count(NO_COMPONENT_FACTS) == 3
        assert "Tests use pytest-django." in out

    def test_a_report_without_the_analysis_headings_is_unchanged(self, tmp_path):
        """The no-commit report says the project is not new; it is passed on whole."""
        report = no_commit_report(tmp_path)
        assert component_facts(report, PLAN) == report
