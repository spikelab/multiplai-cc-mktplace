"""TDD LLM step functions — spawn agents for test writing, implementation, refactoring.

Each function assembles a prompt from templates + context and delegates to sdk.agent_call().
Tool allowlists and timeouts are configured per agent type.
"""

from __future__ import annotations

import logging

from ..models import AgentResult
from ..prompts.test_writing import TEST_WRITER_PROMPT
from ..prompts.implementation import (
    IMPLEMENTER_PROMPT_CLEAN,
    IMPLEMENTER_PROMPT_MINIMUM,
    REFACTOR_PROMPT,
)
from ..sdk import agent_call

log = logging.getLogger(__name__)

# Tool allowlists per agent type (from design.md)
TEST_WRITER_TOOLS = ["Read", "Write", "Bash", "Glob", "Grep"]
IMPLEMENTER_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
REFACTORER_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Timeouts and turn limits per agent type
TEST_WRITER_MAX_TURNS = 30
TEST_WRITER_TIMEOUT = 20 * 60  # 20 min

IMPLEMENTER_MAX_TURNS = 50
IMPLEMENTER_TIMEOUT = 30 * 60  # 30 min

REFACTORER_MAX_TURNS = 30
REFACTORER_TIMEOUT = 15 * 60  # 15 min


async def run_test_writer(
    block_name: str,
    block_description: str,
    specs: str,
    context_bundle: str,
    test_command: str,
    *,
    model: str | None = None,
    cwd: str | None = None,
) -> AgentResult:
    """Spawn a test-writing agent for a block.

    The agent writes failing tests that define expected behavior per the specs.
    """
    prompt = TEST_WRITER_PROMPT.format(
        block_name=block_name,
        block_description=block_description,
        specs=specs,
        context_bundle=context_bundle,
        test_command=test_command,
    )
    log.info("Spawning test writer for block: %s", block_name)
    return await agent_call(
        prompt,
        allowed_tools=TEST_WRITER_TOOLS,
        model=model,
        max_turns=TEST_WRITER_MAX_TURNS,
        cwd=cwd,
        call_timeout=TEST_WRITER_TIMEOUT,
    )


async def run_implementer(
    block_name: str,
    block_description: str,
    failing_tests: str,
    context_bundle: str,
    test_command: str,
    *,
    prompt_style: str = "clean",
    model: str | None = None,
    cwd: str | None = None,
) -> AgentResult:
    """Spawn an implementation agent for a block.

    Uses CLEAN prompt for advanced tier, MINIMUM for standard tier.
    """
    template = IMPLEMENTER_PROMPT_CLEAN if prompt_style == "clean" else IMPLEMENTER_PROMPT_MINIMUM
    prompt = template.format(
        block_name=block_name,
        block_description=block_description,
        failing_tests=failing_tests,
        context_bundle=context_bundle,
        test_command=test_command,
    )
    log.info("Spawning implementer (%s) for block: %s", prompt_style, block_name)
    return await agent_call(
        prompt,
        allowed_tools=IMPLEMENTER_TOOLS,
        model=model,
        max_turns=IMPLEMENTER_MAX_TURNS,
        cwd=cwd,
        call_timeout=IMPLEMENTER_TIMEOUT,
    )


async def run_refactorer(
    block_name: str,
    block_description: str,
    context_bundle: str,
    test_command: str,
    *,
    model: str | None = None,
    cwd: str | None = None,
) -> AgentResult:
    """Spawn a refactoring agent (standard tier only).

    Cleans up implementation code without breaking tests.
    """
    prompt = REFACTOR_PROMPT.format(
        block_name=block_name,
        block_description=block_description,
        context_bundle=context_bundle,
        test_command=test_command,
    )
    log.info("Spawning refactorer for block: %s", block_name)
    return await agent_call(
        prompt,
        allowed_tools=REFACTORER_TOOLS,
        model=model,
        max_turns=REFACTORER_MAX_TURNS,
        cwd=cwd,
        call_timeout=REFACTORER_TIMEOUT,
    )


async def run_integration_fix(
    failure_output: str,
    test_command: str,
    context_bundle: str,
    *,
    model: str | None = None,
    cwd: str | None = None,
) -> AgentResult:
    """Spawn an agent to fix broken integration tests.

    Gets the failure output and must make the full test suite pass again.
    """
    prompt = f"""\
You are a fix agent. The integration test suite is failing after a block was implemented.
Your job is to fix the failures without breaking other tests.

## Failure Output
{failure_output}

## Context
{context_bundle}

## Test Command
{test_command}

## Rules
1. Diagnose the root cause from the failure output.
2. Fix the minimum code needed to make tests pass.
3. Don't modify tests unless they have a genuine bug.
4. Run the full test suite after fixing to verify.

## Output
Report what you fixed and confirm all tests pass.
"""
    log.info("Spawning integration fix agent")
    return await agent_call(
        prompt,
        allowed_tools=IMPLEMENTER_TOOLS,
        model=model,
        max_turns=IMPLEMENTER_MAX_TURNS,
        cwd=cwd,
        call_timeout=IMPLEMENTER_TIMEOUT,
    )
