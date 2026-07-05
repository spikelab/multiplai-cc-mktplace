"""LLM step functions for code and security review.

Each function calls llm_call_structured() or llm_call() to produce
ReviewResult objects for gate evaluation.
"""

from __future__ import annotations

import logging

from ..models import AgentResult, ReviewResult
from ..prompts.review import CODE_REVIEW_PROMPT, SECURITY_REVIEW_PROMPT
from ..sdk import llm_call, llm_call_structured, agent_call, LLMCallError

log = logging.getLogger(__name__)


async def run_code_review(
    diff: str,
    rubric: str,
    config,
    *,
    spec_context: str = "",
) -> ReviewResult:
    """Run code review against rubric dimensions.

    Args:
        diff: The git diff to review
        rubric: The rubric.md content
        config: BuildConfig for model selection
        spec_context: Relevant spec scenarios for compliance checking

    Returns:
        ReviewResult with scores and issues.
    """
    prompt = CODE_REVIEW_PROMPT.format(
        diff=diff,
        rubric=rubric,
        spec_context=spec_context or "(no spec context provided)",
    )

    log.info("Running code review (%d bytes diff)", len(diff))
    result = await llm_call_structured(
        prompt,
        ReviewResult,
        model=config.model,
        max_retries=1,
    )
    log.info(
        "Code review: weighted_avg=%.1f passed=%s issues=%d",
        result.weighted_average,
        result.passed,
        len(result.issues),
    )
    return result


async def run_security_review(
    diff: str,
    rubric: str,
    config,
) -> ReviewResult:
    """Run security-focused review of code changes.

    Args:
        diff: The git diff to review
        rubric: The rubric.md content (for context)
        config: BuildConfig for model selection

    Returns:
        ReviewResult with security-focused scores and issues.
    """
    prompt = SECURITY_REVIEW_PROMPT.format(
        diff=diff,
        rubric=rubric,
    )

    log.info("Running security review (%d bytes diff)", len(diff))
    result = await llm_call_structured(
        prompt,
        ReviewResult,
        model=config.model,
        max_retries=1,
    )
    log.info(
        "Security review: weighted_avg=%.1f issues=%d",
        result.weighted_average,
        len(result.issues),
    )
    return result


async def run_review_fix(
    issues: list[dict],
    diff: str,
    config,
) -> AgentResult:
    """Spawn an agent to fix review issues.

    Args:
        issues: List of issue dicts from ReviewResult
        diff: The original diff for context
        config: BuildConfig for model and project dir

    Returns:
        AgentResult from the fix agent.
    """
    issues_text = "\n".join(
        f"- [{i.get('severity', 'Unknown')}] {i.get('dimension', '')}: {i.get('description', '')}"
        + (f" ({i.get('file_path', '')}:{i.get('line', '')})" if i.get('file_path') else "")
        for i in issues
    )

    prompt = (
        "Fix the following review issues in the codebase.\n\n"
        f"## Issues to Fix\n{issues_text}\n\n"
        f"## Original Diff Context\n```\n{diff[:5000]}\n```\n\n"
        "Fix each issue. Run tests after fixing to ensure nothing breaks.\n"
        f"Test command: {config.test_command or 'pytest -xvs'}\n"
        f"Project dir: {config.project_dir}\n"
    )

    log.info("Spawning review fix agent for %d issues", len(issues))
    result = await agent_call(
        prompt,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        model=config.model,
        max_turns=20,
        cwd=str(config.project_dir),
    )
    log.info("Review fix agent: success=%s turns=%d", result.success, result.turns_used)
    return result
