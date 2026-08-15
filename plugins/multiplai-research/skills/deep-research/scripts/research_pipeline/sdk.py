"""Thin adapter over multiplai_core.run_agent() for LLM reasoning nodes.

Each LLM node in the pipeline calls llm_call() with a focused prompt and
(optionally) a Pydantic model class for structured output validation. On
validation failure, the wrapper retries once with an error message appended
to the prompt indicating the expected format.

The SDK machinery (isolation flags, hard timeout, stderr capture, big-prompt
tempfile fallback, unknown-message skip) lives in multiplai_core.agent_runner
— this module keeps only what is research-specific: the per-run usage/cost
accumulator, the concurrency semaphore + tracking, structured-output
validation, and the pipeline's LLMCallError taxonomy.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from multiplai_core.agent_runner import (
    MAX_PROMPT_BYTES,  # noqa: F401 — re-exported (E2BIG threshold lives in core now)
    AgentRunError,
    AgentRunResult,
    AgentRunTimeout,
    run_agent,
)
from multiplai_core.aio import hard_timeout, swallow_task_result as _swallow_task_result  # noqa: F401
from multiplai_core.text import extract_json  # noqa: F401

log = logging.getLogger(__name__)


T = TypeVar("T", bound=BaseModel)


# Every tool an SDK call could otherwise reach. Copied rather than imported
# from multiplai-core: this is the belt-and-braces layer, and it has to hold on
# an installed plugin that resolved a core release predating core's own
# fail-closed default. In the core version this repo currently resolves the
# list lives in `model_client._DISALLOWED_TOOLS`; core's `agent_runner`
# additionally exports it as `TOOL_UNIVERSE` and pairs it with the SDK's
# `tools` option, which is the real fix — see core's CHANGELOG. Once this repo
# re-locks onto that core, the deny-list here is the *second* layer under a
# base set that no longer depends on a list being complete.
#
# Why it exists: run_agent runs under permission_mode="bypassPermissions",
# where `allowed_tools` is an allow-list only — it adds tools, it never removes
# any. Every prompt this module sends carries fetched web-page text, i.e. text
# an attacker can author. Without an explicit deny-list, an instruction injected
# into a page reaches a Bash/Read/Write/WebFetch that is not merely available
# but pre-approved.
#
# Best-effort, not exhaustive — a name missing here is a tool left open, which
# is why it is not the only layer. Re-derive on a CLI bump, from the CLI's own
# generated schema list:
#
#   grep -oE '^export (type|interface) [A-Za-z]+Input' \
#     "$(dirname "$(readlink -f "$(command -v claude)")")/../sdk-tools.d.ts" \
#     | sed 's/.* //;s/Input$//' | sort -u
#
_TOOL_UNIVERSE = [
    # mutation / execution
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "NotebookEdit",
    "MultiEdit", "REPL", "Task", "Agent", "AskUserQuestion", "SlashCommand",
    "ExitPlanMode", "EnterPlanMode", "TodoWrite",
    # read / network / meta — closes the prompt-injection exfiltration chain
    "Read", "NotebookRead", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
    "ToolSearch", "Skill",
    # egress: each can carry text off the machine. Artifact publishes a page,
    # SendMessage hands it to another agent — exfiltration channels every bit
    # as much as WebFetch, and this pipeline's whole input is attacker-authored.
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
    """Deny everything the caller did not explicitly open.

    The pipeline opens exactly one tool at a time — `WebFetch` for the agent
    fetcher, `WebSearch` for the agent search provider — so the complement is
    the whole universe minus that one.

    `Read` is left available on an oversized prompt: run_agent then writes the
    prompt to a temp file and directs the agent to Read it, so denying Read
    would break every synthesis call over the E2BIG threshold. Same carve-out
    as buildme's `_deny_list`.

    The carve-out opens *read* on a prompt built from attacker-authored page
    text, so it is worth being precise about what it does and does not cost.
    It grants the ability to read local files; it does not grant a way to send
    what was read anywhere, because every egress tool (Bash, Write, WebFetch,
    Artifact, SendMessage, …) stays denied in both branches — the exfiltration
    half of the chain is what the deny-list is really holding, and the
    carve-out does not touch it. `test_sdk.py` pins that invariant so a future
    edit to this function cannot widen it by accident.
    """
    opened = set(allowed_tools or ())
    denied = [t for t in _TOOL_UNIVERSE if t not in opened]
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        denied = [t for t in denied if t != "Read"]
        log.warning(
            "prompt is %d bytes (> %d): Read left open for the tempfile "
            "fallback; all egress tools remain denied",
            len(prompt.encode("utf-8")),
            MAX_PROMPT_BYTES,
        )
    return denied


# ---------------------------------------------------------------------------
# Usage tracking — accumulates cost/token data across all SDK calls in a run
# ---------------------------------------------------------------------------


@dataclass
class LLMCallUsage:
    """Token and cost metrics for a single or accumulated set of SDK calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    num_calls: int = 0

    def accumulate(self, other: "LLMCallUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cost_usd += other.cost_usd
        self.num_calls += other.num_calls


_usage_lock = threading.Lock()
_accumulated_usage = LLMCallUsage()


def get_accumulated_usage() -> LLMCallUsage:
    """Return a snapshot of accumulated usage across all SDK calls."""
    with _usage_lock:
        return LLMCallUsage(
            input_tokens=_accumulated_usage.input_tokens,
            output_tokens=_accumulated_usage.output_tokens,
            cache_creation_tokens=_accumulated_usage.cache_creation_tokens,
            cache_read_tokens=_accumulated_usage.cache_read_tokens,
            cost_usd=_accumulated_usage.cost_usd,
            num_calls=_accumulated_usage.num_calls,
        )


def reset_accumulated_usage() -> None:
    """Reset the accumulated usage counter (call at pipeline start)."""
    global _accumulated_usage
    with _usage_lock:
        _accumulated_usage = LLMCallUsage()


def _record_usage(usage: LLMCallUsage) -> None:
    with _usage_lock:
        _accumulated_usage.accumulate(usage)

# Default per-call timeout: 10 minutes. Synthesis on large finding sets can
# take 3-5 minutes; REASSESS is lighter. Adjust per-node via call_timeout kwarg.
DEFAULT_LLM_CALL_TIMEOUT_S = 600.0

# Concurrency limit for SDK subprocess calls. Benchmarked: 10 concurrent calls
# succeed with ~28% wall-clock overhead vs sequential. Above 10, subprocess
# spawning pressure increases without proportional throughput gain.
# The semaphore gates all SDK calls regardless of caller (search, fetch, etc.).
MAX_CONCURRENT_SDK_CALLS = 10

_sdk_semaphore: asyncio.Semaphore | None = None
_sdk_active_calls: int = 0
_sdk_peak_calls: int = 0
_sdk_active_lock = threading.Lock()


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy semaphore init — must be created inside an event loop."""
    global _sdk_semaphore
    if _sdk_semaphore is None:
        _sdk_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SDK_CALLS)
    return _sdk_semaphore


def _track_call_start(label: str) -> None:
    """Increment active call counter and log concurrency."""
    global _sdk_active_calls, _sdk_peak_calls
    with _sdk_active_lock:
        _sdk_active_calls += 1
        if _sdk_active_calls > _sdk_peak_calls:
            _sdk_peak_calls = _sdk_active_calls
        active = _sdk_active_calls
        peak = _sdk_peak_calls
    log.debug("SDK call START [%s] active=%d peak=%d", label, active, peak)


def _track_call_end(label: str, *, elapsed: float, ok: bool) -> None:
    """Decrement active call counter and log outcome."""
    global _sdk_active_calls
    with _sdk_active_lock:
        _sdk_active_calls -= 1
        active = _sdk_active_calls
        peak = _sdk_peak_calls
    status = "OK" if ok else "FAIL"
    log.debug(
        "SDK call END   [%s] %s %.1fs active=%d peak=%d",
        label, status, elapsed, active, peak,
    )


def get_sdk_peak_concurrency() -> int:
    """Return the peak number of concurrent SDK calls observed."""
    with _sdk_active_lock:
        return _sdk_peak_calls


def reset_sdk_concurrency_stats() -> None:
    """Reset concurrency tracking (call at pipeline start)."""
    global _sdk_active_calls, _sdk_peak_calls
    with _sdk_active_lock:
        _sdk_active_calls = 0
        _sdk_peak_calls = 0


# One-time probe: can the resolved dependencies carry a thinking= kwarg all the
# way to the API? Two independent boundaries have to hold, and checking only
# the near one is how this turns into a crash:
#
#   1. multiplai-core's run_agent() must accept `thinking=`.
#   2. claude-agent-sdk's ClaudeAgentOptions must have a `thinking` field —
#      because core forwards the value into `ClaudeAgentOptions(**opts_kwargs)`
#      with no TypeError tolerance of its own. A core new enough to take the
#      kwarg paired with an SDK too old to have the field is a hard failure on
#      the first mechanical call, not a degradation.
#
# When either boundary fails the kwarg is dropped with a single warning, so the
# run degrades to today's behavior (thinking on) instead of raising.
_core_thinking_support: bool | None = None

_THINKING_UNSUPPORTED_FIX = (
    "Extended thinking stays on for every call, which costs ~15s each; "
    "nothing else is affected. Fix: update this skill's plugin to a version "
    "with current dependencies (reinstall it from the marketplace)."
)


def _sdk_options_accept_thinking() -> bool:
    """Does the installed claude-agent-sdk's ClaudeAgentOptions take thinking=?

    ``ClaudeAgentOptions`` is a dataclass, so its ``__init__`` signature is the
    field list. An unanswerable probe returns True deliberately: if the SDK
    cannot be imported or introspected at all, core's own import of it fails
    next with a message about the real problem, and a warning here claiming
    "your SDK is too old" would be a worse description of that.
    """
    try:
        import claude_agent_sdk

        params = inspect.signature(claude_agent_sdk.ClaudeAgentOptions).parameters
    except Exception as e:  # noqa: BLE001 — missing, moved, or unsignable
        log.debug("Could not introspect ClaudeAgentOptions (%s); assuming support", e)
        return True
    return "thinking" in params


def _core_supports_thinking() -> bool:
    global _core_thinking_support
    if _core_thinking_support is None:
        params = inspect.signature(run_agent).parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            # A **kwargs callable swallows anything (test fakes included), and
            # there is no declared forwarding path left to probe.
            _core_thinking_support = True
        elif "thinking" not in params:
            _core_thinking_support = False
            log.warning(
                "multiplai-core's run_agent() does not accept thinking=. %s",
                _THINKING_UNSUPPORTED_FIX,
            )
        elif not _sdk_options_accept_thinking():
            _core_thinking_support = False
            log.warning(
                "the installed claude-agent-sdk's ClaudeAgentOptions has no "
                "thinking field, and multiplai-core forwards the kwarg there "
                "unguarded. %s",
                _THINKING_UNSUPPORTED_FIX,
            )
        else:
            _core_thinking_support = True
    return _core_thinking_support


class LLMCallError(Exception):
    """Raised when an LLM call fails beyond retry."""


class LLMCallTimeoutError(LLMCallError):
    """Raised when an LLM call exceeds its timeout."""


# hard_timeout / swallow_task_result now live in multiplai_core.aio
# (imported at the top of this module).


async def llm_call(
    prompt: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    thinking: dict | None = None,
    max_turns: int = 1,
    max_attempts: int = 2,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
    label: str = "llm",
) -> str:
    """Execute a single LLM reasoning call via claude_agent_sdk.query().

    Returns the assistant's text response. No tools are allowed by default —
    the pipeline provides all context as prompt text and parses structured
    output from the response. Whatever is not in `allowed_tools` is explicitly
    *denied* (`_deny_list`), because these prompts carry attacker-authorable
    web-page text and an allow-list alone removes nothing under
    bypassPermissions.

    Args:
        thinking: Extended-thinking config forwarded to run_agent, e.g.
            {"type": "disabled"}. None (default) keeps the SDK default and
            does not forward the kwarg at all, so a dependency that cannot
            carry it is never handed it.
        max_attempts: Transient-error retries at the run_agent level (default 2:
            one retry). Callers with their own failover — the search router's
            provider chain, the fetcher's per-source error handling — pass 1.
        label: Short identifier for this call (e.g., "synthesize", "fetch:example.com").
            Used in concurrency tracking and log messages to distinguish concurrent calls.

    Raises LLMCallTimeoutError if the call exceeds call_timeout seconds.
    Default timeout is 10 minutes — long enough for synthesis on large finding
    sets, short enough to prevent indefinite hangs from API stalls.
    """
    log.info(
        "SDK call [%s] prompt=%d bytes tools=%s timeout=%.0fs",
        label, len(prompt.encode("utf-8")), allowed_tools or "none", call_timeout,
    )

    def _capture_usage(into: LLMCallUsage, result: AgentRunResult | None) -> None:
        if result is None:
            return
        into.input_tokens = result.usage.input_tokens
        into.output_tokens = result.usage.output_tokens
        into.cache_creation_tokens = result.usage.cache_creation_tokens
        into.cache_read_tokens = result.usage.cache_read_tokens
        into.cost_usd = result.usage.cost_usd

    # Forward thinking= only when set AND the resolved core *and* SDK can
    # carry it — never handing the kwarg to a dependency that would choke on
    # it keeps this a warning, not a TypeError.
    thinking_kwargs: dict = {}
    if thinking is not None and _core_supports_thinking():
        thinking_kwargs["thinking"] = thinking

    call_usage = LLMCallUsage(num_calls=1)
    call_start = time.monotonic()
    async with _get_semaphore():
        _track_call_start(label)
        call_ok = False
        try:
            result = await run_agent(
                prompt,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                disallowed_tools=_deny_list(prompt, allowed_tools),
                max_turns=max_turns,
                max_attempts=max_attempts,
                model=model,
                effort=effort,
                timeout_s=call_timeout,
                label=label,
                component="deep-research",
                **thinking_kwargs,
            )
            call_ok = True
            _capture_usage(call_usage, result)
            return result.text
        except AgentRunTimeout as e:
            _capture_usage(call_usage, e.partial)
            log.error(
                "FAIL llm_call [%s] reason=timeout after %.0fs\n--- CLI stderr ---\n%s",
                label, call_timeout, e.stderr_tail,
            )
            raise LLMCallTimeoutError(
                f"LLM call [{label}] exceeded {call_timeout:.0f}s timeout\n"
                f"--- CLI stderr ---\n{e.stderr_tail}"
            ) from e
        except AgentRunError as e:
            _capture_usage(call_usage, e.partial)
            log.error(
                "FAIL llm_call [%s] error=%s\n--- CLI stderr ---\n%s",
                label, e.reason, e.stderr_tail,
            )
            raise LLMCallError(
                f"SDK query failed [{label}]: {e.reason}\n"
                f"--- CLI stderr ---\n{e.stderr_tail}"
            ) from e
        finally:
            elapsed = time.monotonic() - call_start
            _track_call_end(label, elapsed=elapsed, ok=call_ok)
            # Record usage even on failure (partial tokens still billed)
            _record_usage(call_usage)


async def llm_call_structured(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    effort: str | None = None,
    thinking: dict | None = None,
    max_retries: int = 1,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
    label: str = "structured",
) -> T:
    """Execute an LLM call and parse the response into a Pydantic model.

    - Extracts the first JSON object from the response
    - Validates against the schema
    - On validation failure, retries once with an error message appended
    - Raises LLMCallTimeoutError if any attempt exceeds call_timeout seconds
    """
    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        raw = await llm_call(
            current_prompt,
            model=model,
            effort=effort,
            thinking=thinking,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            max_turns=3 if allowed_tools else 1,
            call_timeout=call_timeout,
            label=f"{label}:attempt{attempt}" if attempt > 0 else label,
        )
        try:
            payload = extract_json(raw)
            return schema.model_validate(payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = e
            log.warning(
                "Structured output validation failed (attempt %d/%d): %s",
                attempt + 1,
                max_retries + 1,
                e,
            )
            if attempt < max_retries:
                current_prompt = (
                    f"{prompt}\n\n"
                    f"---\n"
                    f"Previous response failed validation: {e}\n"
                    f"Return ONLY valid JSON matching this schema:\n"
                    f"{json.dumps(schema.model_json_schema(), indent=2)}\n"
                )

    raise LLMCallError(
        f"Structured output validation failed after {max_retries + 1} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


# extract_json now lives in multiplai_core.text (imported at top of module).
