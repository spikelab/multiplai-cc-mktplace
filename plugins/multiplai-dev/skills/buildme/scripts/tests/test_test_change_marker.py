"""Every agent that may change a test after the RED gate is told the marker.

`gates.unchanged_tests_gate` accepts a test-file change only when the agent's
report carries `gates.TEST_CHANGE_MARKER` followed by a reason. An agent that
is never told the marker cannot use it, and its correct fix to a wrong test
fails the whole block (live e2e attempt 3, Defect 2). These tests render each
such prompt and check that it names the gate's marker exactly and says where
to write it, so the prompts and the gate cannot drift apart.

The refactor prompts are deliberately absent: those windows have no escape
hatch (`test_tdd_engine.py::test_refactor_prompts_forbid_touching_tests`).
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline.config import BuildConfig
from build_pipeline.gates import (
    TEST_CHANGE_MARKER,
    parse_implementation_note,
    parse_searched,
    parse_test_change_claims,
    unchanged_tests_gate,
)
from build_pipeline.llm_steps.tdd_steps import run_implementer, run_integration_fix
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    GateResult,
    ReviewResult,
    ReviewScore,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import _review_fix_description, _run_integration_and_review


async def _rendered(step, *args, **kwargs) -> str:
    """The prompt *step* hands to the model, with the model call stubbed."""
    ok = AgentResult(success=True, output="done")
    with patch("build_pipeline.llm_steps.tdd_steps.agent_call",
               new_callable=AsyncMock, return_value=ok) as call:
        await step(*args, **kwargs)
    return call.call_args.args[0]


async def _implementer_prompt(style: str) -> str:
    # The review-fix and fix-on-pass agents are this same implementer
    # (`tdd_engine` calls `run_implementer` for both), so these two renders
    # cover every implementer window.
    return await _rendered(run_implementer, "Block", "desc", "FAILED test_a",
                           "ctx", "pytest", prompt_style=style)


async def _integration_fix_prompt(escalate: bool) -> str:
    return await _rendered(run_integration_fix, "FAILED test_a", "pytest", "ctx",
                           escalate=escalate)


PROMPTS = {
    "implementer clean": lambda: _implementer_prompt("clean"),
    "implementer minimum": lambda: _implementer_prompt("minimum"),
    "integration fix": lambda: _integration_fix_prompt(False),
    "integration fix escalated": lambda: _integration_fix_prompt(True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROMPTS))
async def test_prompt_names_the_gate_marker(name):
    prompt = await PROMPTS[name]()
    assert TEST_CHANGE_MARKER in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROMPTS))
async def test_prompt_says_to_write_it_in_the_report_on_its_own_line(name):
    prompt = await PROMPTS[name]()
    assert f"{TEST_CHANGE_MARKER} <reason>" in prompt
    assert "final report" in prompt
    assert "own line" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROMPTS))
async def test_the_line_the_prompt_asks_for_is_the_line_the_gate_accepts(name):
    """Fill in the prompt's own example line and hand it to the gate's parser."""
    prompt = await PROMPTS[name]()
    example = next(line for line in prompt.splitlines()
                   if line.strip().startswith(f"{TEST_CHANGE_MARKER} <reason>"))
    filled = re.sub(r"<reason>.*", "the assertion contradicts the spec", example.strip())
    assert parse_test_change_claims(f"STATUS: DONE\n{filled}\n") == [
        "the assertion contradicts the spec"]


# --- The integration fixer's declaration reaches the implement window ---

TEST_FILE = "tests/test_a.py"


@pytest.fixture
def setup(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    specs_root = project_dir / "specs"
    (specs_root / "changes" / "test").mkdir(parents=True)
    config = BuildConfig(
        project_dir=project_dir, change_name="test", tier="advanced",
        test_command="pytest -xvs", config_dir=tmp_path / "config",
        core_memory_files=[], stack_memory_files=[], additional_memory_files=[],
    )
    config.specs_dir = specs_root
    block = BlockInfo(number=1, name="Block1", description="d",
                      status=BlockStatus.IMPLEMENTING)
    block.implementer_report = "STATUS: DONE\nSURPRISES: none"
    block.test_file_hashes = {TEST_FILE: "at-red"}
    state = BuildState(
        change_name="test", mode="only", tier="advanced",
        state_file=str(tmp_path / "state.json"),
        tdd=TDDState(blocks=[block]),
    )
    progress = ProgressWriter(tmp_path / "progress.md")
    progress.initialize("test", "only", "advanced", 1)
    return config, state, progress, block


async def _run_with_integration_fix(config, state, progress, block, fix_output):
    """Integration fails once, the fixer changes the test file, then it passes."""
    gates = iter([
        GateResult(passed=False, reason="failing", metadata={"stdout": "FAILED"}),
        GateResult(passed=True, reason="all green", metadata={"stdout": "3 passed"}),
    ])
    fix = AgentResult(success=True, output=fix_output)
    review = ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")])
    with patch("build_pipeline.tdd_engine.integration_gate",
               side_effect=lambda *a, **k: next(gates)), \
         patch("build_pipeline.tdd_engine.run_integration_fix",
               new_callable=AsyncMock, return_value=fix), \
         patch("build_pipeline.tdd_engine._snapshot_test_files",
               return_value={TEST_FILE: "changed-by-fixer"}), \
         patch("build_pipeline.tdd_engine._run_quality_review",
               new_callable=AsyncMock, return_value=review):
        return await _run_integration_and_review(block, config, state, progress)


@pytest.mark.asyncio
async def test_integration_fixer_declared_test_change_passes_and_reaches_reviewer(setup):
    """The prompt tells the integration fixer to declare a test change in its
    report, so the implement window must read that report."""
    config, state, progress, block = setup
    result = await _run_with_integration_fix(
        config, state, progress, block,
        f"fixed it\n{TEST_CHANGE_MARKER} the fixture path was wrong\n",
    )
    assert result is True
    assert block.status == BlockStatus.DONE
    # The claim is what the reviewer is handed to verify.
    assert block.test_change_claims == ["the fixture path was wrong"]


@pytest.mark.asyncio
async def test_long_fixer_reports_do_not_cut_the_implementers_declaration(setup):
    """The implementer declared its change; a fixer's long report must not
    push that declaration out of what the implement window's gate reads."""
    config, state, progress, block = setup
    block.implementer_report = f"STATUS: DONE\n{TEST_CHANGE_MARKER} wrong import path"
    result = await _run_with_integration_fix(
        config, state, progress, block, "x" * 10_000,
    )
    assert result is True
    assert block.test_change_claims == ["wrong import path"]


@pytest.mark.asyncio
async def test_integration_fixer_silent_test_change_still_fails(setup):
    config, state, progress, block = setup
    result = await _run_with_integration_fix(
        config, state, progress, block, "fixed it by editing the test\n",
    )
    assert result is False
    assert block.status == BlockStatus.FAILED
    assert block.test_change_claims == []


@pytest.mark.asyncio
async def test_integration_fixer_echoing_the_template_still_fails(setup):
    """The prompt shows `TEST CHANGE REQUIRED: <reason>`; an agent that echoes
    it has declared nothing, so its test change is still silent."""
    config, state, progress, block = setup
    result = await _run_with_integration_fix(
        config, state, progress, block, f"fixed it\n{TEST_CHANGE_MARKER} <reason>\n",
    )
    assert result is False
    assert block.status == BlockStatus.FAILED


# --- Only a real declaration counts ---
#
# Now that every such prompt shows the marker, an agent can echo the template
# or mention the marker in prose. Neither is a declaration: the gate must
# still fail the silent test change (hashing is the enforcement).

NOT_DECLARATIONS = {
    "echoed template": f"STATUS: DONE\n{TEST_CHANGE_MARKER} <reason>\n",
    "echoed fenced template": (
        f"STATUS: DONE\nSPEC_IMPACT: none\n```\n{TEST_CHANGE_MARKER} <reason>\n```\n"),
    "none": f"STATUS: DONE\n{TEST_CHANGE_MARKER} none\n",
    "empty": f"STATUS: DONE\n{TEST_CHANGE_MARKER}\n",
    "prose mention": (
        f"I changed no test, so no {TEST_CHANGE_MARKER} line is needed.\n"),
    "negated, lower case": "No test change required: all edits were in src/.\n",
    "inline code mention": (
        f"I changed no tests, so no `{TEST_CHANGE_MARKER} <reason>` line applies.\n"),
}


@pytest.mark.parametrize("name", sorted(NOT_DECLARATIONS))
def test_a_mention_or_echo_is_not_a_declaration(name):
    report = NOT_DECLARATIONS[name]
    assert parse_test_change_claims(report) == []
    gate = unchanged_tests_gate({TEST_FILE: "a"}, {TEST_FILE: "b"}, report)
    assert not gate.passed
    assert gate.action == "restore_tests"


@pytest.mark.parametrize("line", [
    f"{TEST_CHANGE_MARKER} tests/test_a.py asserted the wrong default",
    f"- {TEST_CHANGE_MARKER} tests/test_a.py asserted the wrong default",
    f"  **{TEST_CHANGE_MARKER}** tests/test_a.py asserted the wrong default",
    f"{TEST_CHANGE_MARKER.lower()} tests/test_a.py asserted the wrong default",
])
def test_a_line_that_starts_with_the_marker_is_a_declaration(line):
    assert parse_test_change_claims(f"STATUS: DONE\n{line}\n") == [
        "tests/test_a.py asserted the wrong default"]


# --- The declaration ends a report without joining the slot before it ---

DECLARATION = f"{TEST_CHANGE_MARKER} tests/test_a.py asserted the wrong default"


def test_a_declaration_after_searched_is_not_a_searched_entry():
    report = (f"STATUS: DONE\nSPEC_IMPACT: none\n\nSEARCHED:\n"
              f"- finding 1: checked parse() and both branches\n\n{DECLARATION}\n")
    assert parse_searched(report) == ["finding 1: checked parse() and both branches"]
    assert parse_test_change_claims(report) == [
        "tests/test_a.py asserted the wrong default"]


def test_a_declaration_after_surprises_is_not_part_of_the_note():
    report = (f"STATUS: DONE\nSURPRISES: the spec named the wrong module\n"
              f"SPEC_IMPACT: clarify\n{DECLARATION}\n")
    note = parse_implementation_note(report, block_number=1, block_name="B",
                                     role="implementer")
    assert note.surprises == "the spec named the wrong module"
    report = f"STATUS: DONE\nSURPRISES: the spec named the wrong module\n{DECLARATION}\n"
    note = parse_implementation_note(report, block_number=1, block_name="B",
                                     role="implementer")
    assert note.surprises == "the spec named the wrong module"


@pytest.mark.asyncio
async def test_review_fix_prompt_orders_searched_before_the_declaration():
    """The review-fix agent gets the implementer prompt with the self-check
    appended, which asks for a `SEARCHED:` section at the end. The declaration
    must say it goes after that section, not contradict it."""
    prompt = await _rendered(run_implementer, "Block", _review_fix_description("fix x"),
                             "FAILED test_a", "ctx", "pytest", prompt_style="clean")
    assert "SEARCHED:" in prompt
    assert TEST_CHANGE_MARKER in prompt
    assert "`SEARCHED:` section included" in prompt


@pytest.mark.asyncio
async def test_a_failed_fixers_declaration_still_counts(setup):
    """A fixer that times out leaves its edits on disk. When it changed a test
    and declared it, a later fixer that turns the suite green does not have to
    declare that change again: the gate reads every fixer's report."""
    config, state, progress, block = setup
    gates = iter([
        GateResult(passed=False, reason="failing", metadata={"stdout": "FAILED"}),
        GateResult(passed=True, reason="all green", metadata={"stdout": "3 passed"}),
    ])
    fixes = [
        AgentResult(success=False,
                    output=f"partial\n{TEST_CHANGE_MARKER} the fixture path was wrong\n"),
        AgentResult(success=True, output="fixed the import\n"),
    ]
    review = ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")])
    with patch("build_pipeline.tdd_engine.integration_gate",
               side_effect=lambda *a, **k: next(gates)), \
         patch("build_pipeline.tdd_engine.run_integration_fix",
               new_callable=AsyncMock, side_effect=fixes) as fix_call, \
         patch("build_pipeline.tdd_engine._snapshot_test_files",
               return_value={TEST_FILE: "changed-by-fixer"}), \
         patch("build_pipeline.tdd_engine._run_quality_review",
               new_callable=AsyncMock, return_value=review):
        result = await _run_integration_and_review(block, config, state, progress)
    assert fix_call.await_count == 2
    assert result is True
    assert block.test_change_claims == ["the fixture path was wrong"]
