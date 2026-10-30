"""`tdd.spec_tests` off (the default) leaves a block's TDD run as it was before
the setting existed: every prompt, every context bundle and every commit of a
block run equal the ones recorded at main 845ff594, in
`fixtures/spec-tests-off-golden.json`, on a change whose specs mark scenarios
security and permission.

Agents are faked at `agent_call`, so the prompts compared are the full text
each agent would get; git and the files are real.

Regenerate the golden only from a commit whose behaviour with the setting off
is the one to keep: `SPEC_TESTS_GOLDEN=write pytest tests/test_spec_tests_off.py`.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.models import BlockInfo
from build_pipeline.progress import ProgressWriter
from build_pipeline.sdk import AgentResult
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import assemble_context, run_block_tdd

GOLDEN = Path(__file__).parent / "fixtures" / "spec-tests-off-golden.json"
RED = (1, "FAILED tests/test_new.py::test_add - AssertionError: not implemented")
NEW_TEST = "def test_add():\n    from module import add\n    assert add(2, 3) == 5\n"

SPECS = """\
# Capability: account-roles

### Requirement: Managers see their own company only (permission)

The system SHALL scope every manager query to the manager's company.

#### Scenario: A manager asks for another company's staff
- **WHEN** a manager requests the staff list of another company
- **THEN** the request is refused with 403

### Requirement: Totals

#### Scenario: Adding two numbers
- **WHEN** add is called with 2 and 3
- **THEN** it returns 5

#### Scenario: A cleaner cannot change roles [security]
- **WHEN** a cleaner calls set_role
- **THEN** it raises PermissionError
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _gates():
    from build_pipeline.models import GateResult, TestQualityAudit

    with (
        patch(
            "build_pipeline.tdd_engine.integration_gate",
            return_value=GateResult(passed=True, reason="fixture checks pass"),
        ),
        patch(
            "build_pipeline.tdd_engine._audit_test_quality",
            new_callable=AsyncMock,
            return_value=TestQualityAudit(passed=True),
        ),
    ):
        yield


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    project = tmp_path / "project"
    change = project / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (change / "implementation-plan.md").write_text(
        "# Design\n\nKeep role checks in auth.py; add() is pure.\n"
    )
    (change / "specs.md").write_text(SPECS)
    (project / "module.py").write_text(
        "def add(a, b):\n    raise NotImplementedError\n"
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_old.py").write_text(
        "def test_old():\n    assert 1 == 1\n"
    )
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")

    config = BuildConfig(
        project_dir=project,
        change_name="test",
        tier="advanced",
        test_command="pytest",
        config_dir=tmp_path / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = project / "specs"
    block = BlockInfo(
        number=1,
        name="Add",
        description="Add two numbers; cleaners cannot change roles.",
        satisfies=["account-roles"],
        produces=["add(a: int, b: int) -> int"],
    )
    state = BuildState(
        change_name="test",
        tier="advanced",
        state_file=str(tmp_path / "state.json"),
        tdd=TDDState(blocks=[block]),
    )
    progress = ProgressWriter(tmp_path / "progress.md")
    progress.initialize("test", "advanced", 1)
    return config, state, progress, block


def _record(tmp_path, config, state, progress, block) -> dict:
    """Run one block with fake agents; every prompt, the context bundles and
    the commit subjects, with the temporary directory written as <TMP>."""
    prompts: list[dict] = []

    async def agent(prompt, **kw):
        label = kw.get("budget_label", "")
        prompts.append({"label": label, "prompt": prompt})
        if label == "test_writer":
            (config.project_dir / "tests" / "test_new.py").write_text(NEW_TEST)
        elif label == "implementer":
            (config.project_dir / "module.py").write_text(
                "def add(a, b):\n    return a + b\n"
            )
        return AgentResult(success=True, output="STATUS: DONE")

    contexts = {
        role: assemble_context(block, config, role, blocks=state.tdd.blocks)
        for role in ("test_writer", "implementer", "refactorer")
    }
    with (
        patch("build_pipeline.llm_steps.tdd_steps.agent_call", side_effect=agent),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED),
    ):
        ok = asyncio_run(run_block_tdd(block, config, state, progress))
    commits = _git(config.project_dir, "log", "--format=%s", "main").splitlines()
    record = {
        "ok": ok,
        "contexts": contexts,
        "prompts": prompts,
        "commits": commits,
    }
    return json.loads(json.dumps(record).replace(str(tmp_path), "<TMP>"))


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_spec_tests_off_by_default_matches_main(tmp_path, monkeypatch):
    config, state, progress, block = _setup(tmp_path, monkeypatch)
    assert getattr(config, "spec_tests", False) is False
    record = _record(tmp_path, config, state, progress, block)
    if os.environ.get("SPEC_TESTS_GOLDEN") == "write":
        GOLDEN.write_text(json.dumps(record, indent=2) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert record["ok"] is True
    assert [p["label"] for p in record["prompts"]] == [
        p["label"] for p in golden["prompts"]
    ]
    for got, want in zip(record["prompts"], golden["prompts"]):
        assert got["prompt"] == want["prompt"], got["label"]
    assert record["contexts"] == golden["contexts"]
    assert record["commits"] == golden["commits"]
    assert record == golden
