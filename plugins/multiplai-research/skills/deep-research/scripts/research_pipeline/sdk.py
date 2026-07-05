"""Thin wrapper around claude_agent_sdk.query() for LLM reasoning nodes.

Each LLM node in the pipeline calls llm_call() with a focused prompt and
(optionally) a Pydantic model class for structured output validation. On
validation failure, the wrapper retries once with an error message appended
to the prompt indicating the expected format.

All SDK calls are wrapped in asyncio.wait_for with a configurable timeout
(default 10 minutes) to prevent indefinite hangs from rate-limit stalls,
slow model responses, or SDK subprocess issues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


T = TypeVar("T", bound=BaseModel)


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

# Max prompt size in bytes before triggering E2BIG workaround.
# The SDK passes prompts as CLI args; Linux ARG_MAX is typically 128KB
# but we stay conservative to leave room for other args/env.
MAX_PROMPT_BYTES = 80_000

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


async def _safe_query(*, prompt, options):
    """Wrap SDK query() to skip unknown message types (e.g. rate_limit_event).

    The SDK message parser raises MessageParseError for message types it doesn't
    recognize. With `debug-to-stderr` enabled the CLI emits additional internal
    message types that the bundled SDK parser doesn't know about — without this
    wrapper they crash the call. Mirrors buildme's _safe_query.
    """
    from claude_agent_sdk import query as raw_query

    gen = raw_query(prompt=prompt, options=options).__aiter__()
    while True:
        try:
            message = await gen.__anext__()
            yield message
        except StopAsyncIteration:
            break
        except Exception as e:  # noqa: BLE001
            if "Unknown message type" in str(e):
                log.debug("Skipping unknown SDK message type: %s", e)
                continue
            raise


def reset_sdk_concurrency_stats() -> None:
    """Reset concurrency tracking (call at pipeline start)."""
    global _sdk_active_calls, _sdk_peak_calls
    with _sdk_active_lock:
        _sdk_active_calls = 0
        _sdk_peak_calls = 0


class LLMCallError(Exception):
    """Raised when an LLM call fails beyond retry."""


class LLMCallTimeoutError(LLMCallError):
    """Raised when an LLM call exceeds its timeout."""


def _swallow_task_result(task: "asyncio.Task") -> None:
    """Retrieve and discard a task's result/exception so the event loop does
    not log 'Task exception was never retrieved' for abandoned tasks."""
    try:
        task.result()
    except BaseException:  # noqa: BLE001 — includes CancelledError; we discard all
        pass


async def hard_timeout(coro, timeout_s: float):
    """Run ``coro`` with a wall-clock timeout that ALWAYS returns control.

    Drop-in replacement for ``asyncio.wait_for`` with one critical difference:
    on timeout it cancels the task **fire-and-forget** and returns immediately,
    instead of awaiting the cancellation to complete.

    Why this matters: ``asyncio.wait_for`` awaits the cancellation it triggers.
    If the inner coroutine's cleanup blocks — e.g. the claude-agent-sdk CLI
    subprocess is wedged and its transport teardown never finishes — then the
    cancellation never completes and ``wait_for`` hangs forever (observed: a
    7-hour, ~0-CPU hang that defeated all three nested wait_for layers). This
    helper uses ``asyncio.wait``, which returns (done, pending) when the timeout
    elapses and does NOT await pending tasks, so a wedged subprocess can leak in
    the background but can never stall the pipeline.

    Raises ``asyncio.TimeoutError`` on timeout (same contract as wait_for).
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if task not in done:
        task.cancel()  # best-effort; do NOT await — cancellation may block
        task.add_done_callback(_swallow_task_result)
        raise asyncio.TimeoutError()
    return task.result()


async def llm_call(
    prompt: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = 1,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    call_timeout: float = DEFAULT_LLM_CALL_TIMEOUT_S,
    label: str = "llm",
) -> str:
    """Execute a single LLM reasoning call via claude_agent_sdk.query().

    Returns the assistant's text response. No tools are allowed by default —
    the pipeline provides all context as prompt text and parses structured
    output from the response.

    Args:
        label: Short identifier for this call (e.g., "synthesize", "fetch:example.com").
            Used in concurrency tracking and log messages to distinguish concurrent calls.

    Raises LLMCallTimeoutError if the call exceeds call_timeout seconds.
    Default timeout is 10 minutes — long enough for synthesis on large finding
    sets, short enough to prevent indefinite hangs from API stalls.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions  # type: ignore
        from claude_agent_sdk import AssistantMessage, TextBlock  # type: ignore
    except ImportError as e:
        raise LLMCallError(
            "claude-agent-sdk not installed. Run: pip install claude-agent-sdk"
        ) from e

    try:
        from claude_agent_sdk import ResultMessage  # type: ignore
    except ImportError:
        ResultMessage = None  # type: ignore[assignment,misc]

    # The SDK passes the prompt as a CLI argument to a subprocess. If the
    # prompt exceeds ~100KB, the OS rejects it with E2BIG. Instead of
    # truncating (which discards findings), we write the full prompt to a
    # temp file and tell Claude to read it via the Read tool. No data loss.
    import tempfile

    prompt_file: str | None = None
    effective_tools = list(allowed_tools or [])

    # Environment overrides for SDK subagents. The parent session may have
    # settings (e.g., large Read output tokens) that subagents don't inherit
    # since they're fresh CLI processes without the parent's settings.json.
    subagent_env: dict[str, str] = {}

    prompt_bytes = len(prompt.encode("utf-8"))
    log.info(
        "SDK call [%s] prompt=%d bytes tools=%s timeout=%.0fs",
        label, prompt_bytes, effective_tools or "none", call_timeout,
    )

    if prompt_bytes > MAX_PROMPT_BYTES:
        log.info(
            "Prompt too large for CLI arg (%d bytes), writing to temp file",
            prompt_bytes,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="research_prompt_", delete=False
        ) as f:
            f.write(prompt)
            prompt_file = f.name
        log.info("Prompt written to temp file: %s", prompt_file)

        # Replace the prompt with a directive to read the file.
        # IMPORTANT: The prompt must not encourage "thinking out loud" —
        # any text the agent emits before reading gets captured as output.
        prompt = (
            f"Read the file {prompt_file} using the Read tool. "
            f"It contains your complete instructions and data. "
            f"After reading, follow those instructions exactly and produce "
            f"the requested output. Do not describe what you are doing — "
            f"just read the file and produce the output directly."
        )
        # Grant Read tool access so the agent can read the prompt file
        if "Read" not in effective_tools:
            effective_tools.append("Read")
        # Agent needs at least 3 turns: read the file, then produce output.
        # With max_turns=1, the agent reads but has no turn left to respond.
        max_turns = max(max_turns, 3)
        # Ensure the subagent can read the full file without truncation.
        # Default Read limit is 2000 lines — insufficient for large prompts.
        subagent_env["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = "100000"

    # Use a dedicated session dir so SDK subagent sessions don't pollute
    # the user's session history, and disable settings.json hook loading
    # to prevent re-entry cascades.
    _config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    _session_dir = _config_dir / "hook-sessions"
    _session_dir.mkdir(exist_ok=True)

    # Capture stderr from the CLI subprocess so SDK errors are diagnosable.
    # The SDK hardcodes ProcessError stderr to "Check stderr output for details" —
    # without this callback the real error never surfaces. debug-to-stderr forces
    # the CLI to emit verbose output we can attach to exceptions on failure.
    stderr_lines: list[str] = []

    opts_kwargs: dict = dict(
        allowed_tools=effective_tools,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        system_prompt=system_prompt,
        model=model,
        env={**subagent_env, "_HOOK_CHILD_SESSION": "1"},  # Tell hooks to skip
        cwd=_session_dir,
        setting_sources=[],
        stderr=lambda line: stderr_lines.append(line),
        # Both setting_sources=[] AND extra_args={"setting-sources": ""} are
        # required when debug-to-stderr is on — without setting_sources=[]
        # the parent agent spawns runaway agent:builtin:Explore subagents
        # that loop until the CLI gives up and exits 1 (verified 2026-04-20).
        extra_args={"setting-sources": "", "debug-to-stderr": None},
    )
    if effort is not None:
        opts_kwargs["effort"] = effort
    options = ClaudeAgentOptions(**opts_kwargs)

    import time as _time

    chunks: list[str] = []
    call_usage = LLMCallUsage(num_calls=1)
    call_start = _time.monotonic()
    async with _get_semaphore():
        _track_call_start(label)
        call_ok = False
        try:
            async def _run_query() -> None:
                async for message in _safe_query(prompt=prompt, options=options):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)
                    elif ResultMessage is not None and isinstance(message, ResultMessage):
                        usage = getattr(message, "usage", None) or {}
                        call_usage.input_tokens = usage.get("input_tokens", 0) or 0
                        call_usage.output_tokens = usage.get("output_tokens", 0) or 0
                        call_usage.cache_creation_tokens = (
                            usage.get("cache_creation_input_tokens", 0) or 0
                        )
                        call_usage.cache_read_tokens = (
                            usage.get("cache_read_input_tokens", 0) or 0
                        )
                        call_usage.cost_usd = getattr(message, "total_cost_usd", 0.0) or 0.0

            try:
                # hard_timeout (not asyncio.wait_for): a wedged CLI subprocess
                # can make cancellation block indefinitely, which would hang
                # wait_for forever. hard_timeout returns on timeout regardless.
                await hard_timeout(_run_query(), call_timeout)
                call_ok = True
            except asyncio.TimeoutError:
                captured = "\n".join(stderr_lines[-2000:])
                log.error(
                    "FAIL llm_call [%s] reason=timeout after %.0fs\n--- CLI stderr (last 2000 lines) ---\n%s",
                    label, call_timeout, captured,
                )
                raise LLMCallTimeoutError(
                    f"LLM call [{label}] exceeded {call_timeout:.0f}s timeout\n"
                    f"--- CLI stderr ---\n{captured}"
                )
        except LLMCallTimeoutError:
            raise
        except Exception as e:  # noqa: BLE001
            captured = "\n".join(stderr_lines[-2000:])
            log.error(
                "FAIL llm_call [%s] error=%s\n--- CLI stderr (last 2000 lines) ---\n%s",
                label, e, captured,
            )
            raise LLMCallError(
                f"SDK query failed [{label}]: {e}\n--- CLI stderr ---\n{captured}"
            ) from e
        finally:
            elapsed = _time.monotonic() - call_start
            _track_call_end(label, elapsed=elapsed, ok=call_ok)
            # Record usage even on failure (partial tokens still billed)
            _record_usage(call_usage)
            # Clean up temp file if we created one
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

    return "".join(chunks).strip()


async def llm_call_structured(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    effort: str | None = None,
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


def extract_json(text: str) -> dict | list:
    """Extract a JSON object or array from a model response.

    Handles:
    - ```json ... ``` fenced code blocks
    - Plain JSON with surrounding prose
    - Multi-line JSON objects
    """
    if not text or not text.strip():
        raise ValueError("Empty response")

    # 1. Fenced code blocks
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        return json.loads(candidate)

    # 2. First complete JSON object/array via bracket balancing
    stripped = text.strip()
    start = None
    for i, ch in enumerate(stripped):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError("No JSON object/array found in response")

    # Track brackets to find matching close
    open_ch = stripped[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    end = None

    for i in range(start, len(stripped)):
        ch = stripped[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                end = i
                break

    if end is None:
        raise ValueError("Unbalanced JSON in response")

    return json.loads(stripped[start : end + 1])
