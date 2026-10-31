"""`tdd.spec_tests` (item 62 of plan-factory-merged-2026-10-29.md): with it on,
a test writer gets the block's scenarios marked security or permission and the
public interfaces they touch, not the design, before the block's own test
writer and the implementer. With it off nothing changes: that is
`test_spec_tests_off.py`.

Agents are faked at `agent_call`; git and the files are real.
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.models import BlockInfo
from build_pipeline.sdk import AgentResult
from build_pipeline.spec_tests import block_marked_scenarios
from build_pipeline.tdd_engine import run_block_tdd

from tests.test_spec_tests_off import RED, _gates, _setup  # noqa: F401

SPEC_TEST = (
    "import pytest\nfrom module import set_role\n\n"
    "def test_cleaner_cannot_set_role():\n"
    "    with pytest.raises(PermissionError):\n"
    "        set_role('cleaner', 'manager')\n"
)
NEW_TEST = "def test_add():\n    from module import add\n    assert add(2, 3) == 5\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _config_from_yaml(tmp_path, yaml_text):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "config.yaml").write_text(yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = specs
    config._load_specs_config()
    return config


class TestConfig:
    def test_off_by_default(self, tmp_path):
        assert BuildConfig().spec_tests is False
        assert (
            _config_from_yaml(tmp_path, "tdd:\n  test_command: pytest\n").spec_tests
            is False
        )

    def test_true_turns_it_on(self, tmp_path):
        assert _config_from_yaml(tmp_path, "tdd:\n  spec_tests: true\n").spec_tests

    @pytest.mark.parametrize("value", ['"yes"', "1", "on-ish"])
    def test_anything_but_a_yaml_boolean_keeps_it_off(self, tmp_path, value):
        config = _config_from_yaml(tmp_path, f"tdd:\n  spec_tests: {value}\n")
        assert config.spec_tests is False


class TestMarkedScenarios:
    def _change(self, tmp_path, specs):
        change = tmp_path / "change"
        change.mkdir()
        (change / "specs.md").write_text(specs)
        return change

    SPECS = """\
# Capability: account-roles

### Requirement: Managers see their own company only (permission)

The system SHALL scope every manager query to the manager's company.

#### Scenario: A manager asks for another company's staff
- **WHEN** a manager requests another company's staff
- **THEN** the request is refused with 403

#### Scenario: A manager asks for their own staff
- **WHEN** a manager requests their own staff
- **THEN** the list is returned

### Requirement: Totals

#### Scenario: Adding two numbers
- **WHEN** add is called with 2 and 3
- **THEN** it returns 5

#### Scenario: A cleaner cannot change roles [security]
- **WHEN** a cleaner calls set_role
- **THEN** it raises PermissionError

# Capability: billing

### Requirement: Invoices

#### Scenario: Only admins void an invoice @Permission
- **WHEN** a manager voids an invoice
- **THEN** it is refused
"""

    def test_a_block_gets_the_marked_scenarios_of_the_capabilities_it_satisfies(
        self, tmp_path
    ):
        change = self._change(tmp_path, self.SPECS)
        block = BlockInfo(
            number=1, name="b", description="", satisfies=["account-roles"]
        )
        text = block_marked_scenarios(block, change)
        assert text.startswith("## Capability: account-roles")
        # a marked requirement marks all its scenarios, with its statement
        assert "The system SHALL scope every manager query" in text
        assert "A manager asks for another company's staff" in text
        assert "A manager asks for their own staff" in text
        # a marked scenario under an unmarked requirement
        assert "### Requirement: Totals" in text
        assert "A cleaner cannot change roles [security]" in text
        assert "Adding two numbers" not in text
        assert "billing" not in text

    def test_a_named_requirement_narrows_the_capability(self, tmp_path):
        change = self._change(tmp_path, self.SPECS)
        block = BlockInfo(
            number=1, name="b", description="", satisfies=["account-roles: Totals"]
        )
        text = block_marked_scenarios(block, change)
        assert "A cleaner cannot change roles" in text
        assert "Managers see their own company only" not in text

    SHARED_NAME_SPECS = """\
# Capability: account-roles

### Requirement: Audit

#### Scenario: Role changes are logged [security]
- **WHEN** a role changes
- **THEN** it is logged

### Requirement: Managers see their own company only (permission)

#### Scenario: A manager asks for another company's staff
- **WHEN** a manager requests another company's staff
- **THEN** the request is refused with 403

# Capability: billing

### Requirement: Audit

#### Scenario: Voided invoices are logged [security]
- **WHEN** an invoice is voided
- **THEN** it is logged
"""

    def test_an_entry_about_another_capability_does_not_narrow_this_one(self, tmp_path):
        change = self._change(tmp_path, self.SHARED_NAME_SPECS)
        block = BlockInfo(
            number=1,
            name="b",
            description="",
            satisfies=["account-roles", "billing: Audit"],
        )
        text = block_marked_scenarios(block, change)
        assert "A manager asks for another company's staff" in text
        assert "Role changes are logged" in text
        assert "Voided invoices are logged" in text

    def test_an_entry_about_another_capability_adds_nothing_to_this_one(self, tmp_path):
        change = self._change(tmp_path, self.SHARED_NAME_SPECS)
        block = BlockInfo(
            number=1, name="b", description="", satisfies=["billing: Audit"]
        )
        text = block_marked_scenarios(block, change)
        assert "Voided invoices are logged" in text
        assert "account-roles" not in text

    def test_marks_match_in_any_case_and_form(self, tmp_path):
        change = self._change(tmp_path, self.SPECS)
        block = BlockInfo(number=1, name="b", description="", satisfies=["billing"])
        assert "Only admins void an invoice @Permission" in block_marked_scenarios(
            block, change
        )

    def test_no_marked_scenario_or_no_satisfies_gives_nothing(self, tmp_path):
        change = self._change(tmp_path, self.SPECS.replace("[security]", ""))
        assert (
            block_marked_scenarios(
                BlockInfo(
                    number=1,
                    name="b",
                    description="",
                    satisfies=["account-roles: Totals"],
                ),
                change,
            )
            == ""
        )
        assert (
            block_marked_scenarios(
                BlockInfo(number=1, name="b", description=""), change
            )
            == ""
        )


def _agents(config, calls, spec_result=None):
    async def agent(prompt, **kw):
        label = kw.get("budget_label", "")
        calls.append((label, prompt))
        project = config.project_dir
        if label == "spec_test_writer":
            (project / "tests" / "test_roles_spec.py").write_text(SPEC_TEST)
            if spec_result is not None:
                return spec_result
        elif label == "test_writer":
            (project / "tests" / "test_new.py").write_text(NEW_TEST)
        elif label == "implementer":
            (project / "module.py").write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def set_role(who, role):\n    raise PermissionError(who)\n"
            )
        return AgentResult(success=True, output="STATUS: DONE")

    return agent


def _run(config, state, progress, block, calls, spec_result=None):
    with (
        patch(
            "build_pipeline.llm_steps.tdd_steps.agent_call",
            side_effect=_agents(config, calls, spec_result),
        ),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED),
    ):
        return asyncio.run(run_block_tdd(block, config, state, progress))


class TestOn:
    def test_the_spec_test_writer_gets_the_marked_scenarios_and_interfaces_not_the_design(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = _setup(tmp_path, monkeypatch)
        config.spec_tests = True
        calls: list = []
        assert _run(config, state, progress, block, calls) is True

        labels = [label for label, _ in calls]
        assert labels[:3] == ["spec_test_writer", "test_writer", "implementer"]
        spec_prompt = calls[0][1]
        assert "A cleaner cannot change roles [security]" in spec_prompt
        assert "A manager asks for another company's staff" in spec_prompt
        assert "Adding two numbers" not in spec_prompt
        assert "- Produces: add(a: int, b: int) -> int" in spec_prompt
        assert "Keep role checks in auth.py" not in spec_prompt
        assert "## Design Document" not in spec_prompt
        assert "You are not given the design on purpose" in spec_prompt

        # the block's own test writer still gets the design, and the files
        test_prompt = calls[1][1]
        assert "Keep role checks in auth.py" in test_prompt
        assert "## Tests already written from the specs alone" in test_prompt
        assert "- tests/test_roles_spec.py" in test_prompt

        # one test commit holds both writers' tests, before the implementation
        assert set(
            _git(
                config.project_dir,
                "show",
                "--name-only",
                "--format=",
                block.test_commit,
            ).split()
        ) == {"tests/test_new.py", "tests/test_roles_spec.py"}
        assert _git(config.project_dir, "log", "--format=%s", "main").splitlines() == [
            "impl(block-1): Add",
            "test(block-1): Add",
            "init",
        ]
        assert "tests/test_roles_spec.py" in block.test_file_hashes
        assert [n.role for n in block.notes] == []

    def test_a_block_with_no_marked_scenario_runs_no_spec_test_writer(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = _setup(tmp_path, monkeypatch)
        config.spec_tests = True
        block.satisfies = []
        calls: list = []
        assert _run(config, state, progress, block, calls) is True
        assert "spec_test_writer" not in [label for label, _ in calls]
        assert "Tests already written" not in calls[0][1]

    def test_a_failed_spec_test_writer_stops_the_block_as_resumable(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = _setup(tmp_path, monkeypatch)
        config.spec_tests = True
        calls: list = []
        crashed = AgentResult(success=False, error="CLI exited 1")
        assert _run(config, state, progress, block, calls, crashed) is False
        assert [label for label, _ in calls] == ["spec_test_writer"]
        assert block.resumable is True and block.test_commit is None

    def test_a_spec_test_writer_asking_for_context_stops_the_block(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = _setup(tmp_path, monkeypatch)
        config.spec_tests = True
        calls: list = []
        asked = AgentResult(success=True, output="STATUS: NEEDS_CONTEXT\nwho is admin?")
        assert _run(config, state, progress, block, calls, asked) is False
        assert [label for label, _ in calls] == ["spec_test_writer"]
