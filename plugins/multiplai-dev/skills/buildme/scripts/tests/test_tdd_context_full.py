"""`tdd.context: full` (the default) leaves a block's TDD run as it was before
the setting existed: every prompt, every context bundle and every commit of a
block run equal the ones recorded at main 845ff594, in
`fixtures/tdd-context-full-golden.json`, on a change whose design has several
sections, whose specs have two capabilities, and which has a rubric, a memory
file and a reference doc: everything `tdd.context: sliced` would cut.

Agents are faked at `agent_call`, so the prompts compared are the full text
each agent would get; git and the files are real. The reference docs are
fixed here, because which docs resolve depends on the machine.

Regenerate the golden only from a commit whose behaviour with `full` is the one
to keep: `TDD_CONTEXT_GOLDEN=write pytest tests/test_tdd_context_full.py`.
"""

import asyncio
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

GOLDEN = Path(__file__).parent / "fixtures" / "tdd-context-full-golden.json"
RED = (1, "FAILED tests/test_new.py::test_add - AssertionError: not implemented")
NEW_TEST = "def test_add():\n    from module import add\n    assert add(2, 3) == 5\n"
ROLES = ("test_writer", "implementer", "refactorer", "reviewer")

DESIGN = """\
# Design

## Global Constraints
- Python 3.11 floor.

## Overview
The change adds arithmetic and an audit log.

## Arithmetic
add() is pure and lives in module.py.

### Overflow
Integers do not overflow in Python.

## Audit Log
Every role change writes one line to audit.log.
"""

SPECS = """\
# Capability: arithmetic

## ADDED Requirements

### Requirement: Adding

#### Scenario: Adding two numbers
- **WHEN** add is called with 2 and 3
- **THEN** it returns 5

### Requirement: Subtracting

#### Scenario: Subtracting two numbers
- **WHEN** sub is called with 5 and 3
- **THEN** it returns 2

# Capability: audit-log

## ADDED Requirements

### Requirement: Role changes are logged

#### Scenario: A role change
- **WHEN** a manager changes a role
- **THEN** audit.log gets one line
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


def setup_change(tmp_path, monkeypatch, config_yaml: str = ""):
    """A git project with a change, its config and one block that cites the
    Arithmetic section and satisfies one requirement of one capability."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    project = tmp_path / "project"
    change = project / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (change / "implementation-plan.md").write_text(DESIGN)
    (change / "specs.md").write_text(SPECS)
    (change / "rubric.md").write_text("# Rubric\n\n- Correctness: 1-5\n")
    (project / "specs" / "config.yaml").write_text(config_yaml)
    (project / "module.py").write_text(
        "def add(a, b):\n    raise NotImplementedError\n"
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_old.py").write_text(
        "def test_old():\n    assert 1 == 1\n"
    )
    memory = tmp_path / "config" / "memory"
    memory.mkdir(parents=True)
    (memory / "technical-pref.md").write_text("# Technical prefs\n\nUse uv.\n")
    (memory / "testing.md").write_text("# Testing\n\nUse pytest.\n")
    reference = tmp_path / "reference" / "python.md"
    reference.parent.mkdir()
    reference.write_text("# Python\n\nType hints everywhere.\n")
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")

    config = BuildConfig(project_dir=project, config_dir=tmp_path / "config")
    config.specs_dir = project / "specs"
    config._load_specs_config()
    config.change_name = "test"
    config.tier = "advanced"
    config.test_command = "pytest"
    config.project_description = "Arithmetic service"
    monkeypatch.setattr(BuildConfig, "stack_reference_docs", lambda self: [reference])
    block = BlockInfo(
        number=1,
        name="Add",
        description="Add two numbers, as the Arithmetic section says.",
        satisfies=["arithmetic — Adding"],
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


def record_block_run(tmp_path, config, state, progress, block) -> dict:
    """Run one block with fake agents; every prompt, each role's context
    bundle and the commit subjects, with the temporary directory as <TMP>."""
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
        for role in ROLES
    }
    with (
        patch("build_pipeline.llm_steps.tdd_steps.agent_call", side_effect=agent),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED),
    ):
        ok = asyncio.run(run_block_tdd(block, config, state, progress))
    commits = _git(config.project_dir, "log", "--format=%s", "main").splitlines()
    record = {"ok": ok, "contexts": contexts, "prompts": prompts, "commits": commits}
    return json.loads(json.dumps(record).replace(str(tmp_path), "<TMP>"))


@pytest.mark.parametrize(
    "config_yaml",
    [
        "",
        "tdd:\n  context: full\n",
        # `memory_files.tdd` is read only with `sliced`.
        "memory_files:\n  tdd: [testing.md]\ntdd:\n  context: full\n",
    ],
    ids=["unset", "full", "full-with-tdd-memory"],
)
def test_full_context_matches_main(tmp_path, monkeypatch, config_yaml):
    config, state, progress, block = setup_change(tmp_path, monkeypatch, config_yaml)
    record = record_block_run(tmp_path, config, state, progress, block)
    if os.environ.get("TDD_CONTEXT_GOLDEN") == "write" and not config_yaml:
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
