"""Model client abstraction for multiplai plugin.

Provides a Protocol-based interface with two implementations:
- AgentSDKClient: uses claude_agent_sdk from the host runtime (zero-config)
- AnthropicAPIClient: uses the anthropic PyPI package with an API key

The create_client() factory tries Agent SDK first, falls back to API key.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 4096
_STDERR_TAIL_LINES = 2000

# The bundled CLI intermittently exits 1 (verified recurring: diary
# 2026-04-19/28304b42, 2026-04-29/8bcd0f1c). One bounded retry turns a flaky
# failure into a transparent recovery for unattended pipelines like dream.
_SDK_MAX_ATTEMPTS = 2
_SDK_RETRY_BACKOFF_S = 1.5


@dataclass(frozen=True)
class ModelResponse:
    """Normalized response from any model client."""
    content: str


class SDKQueryError(RuntimeError):
    """Raised when ``claude_agent_sdk.query()`` fails.

    ``stderr_tail`` holds the last captured CLI stderr lines — useful
    for surfacing rate-limit, auth, or CLI crash details that would
    otherwise be silently dropped.
    """

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


def _messages_to_prompt(messages: list[dict]) -> str:
    """Flatten the ModelClient messages list into a single prompt string.

    ``claude_agent_sdk.query()`` takes a ``prompt`` string rather than a
    messages list. Plugin callers invoke single-turn user queries, so we
    concatenate every user message. Non-user roles are ignored (the
    system prompt is passed separately via ``ClaudeAgentOptions``).
    """
    user_parts = [m["content"] for m in messages if m.get("role") == "user"]
    return "\n\n".join(user_parts)


def _hook_session_dir() -> Path:
    """cwd for no-tool SDK calls — prevents project settings.json pickup."""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    d = cfg / "hook-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _safe_query(sdk, *, prompt, options):
    """Wrap ``claude_agent_sdk.query()`` to skip unknown message types.

    The SDK message parser raises for message types it doesn't recognize.
    With ``debug-to-stderr`` enabled (which this client sets), the CLI emits
    additional internal message types the bundled SDK parser doesn't know
    about — without this wrapper they crash the call with a generic
    ``Command failed with exit code 1``. Mirrors deep-research/sdk.py and
    buildme's ``_safe_query``. This guard is mandatory whenever
    ``debug-to-stderr`` is on.
    """
    gen = sdk.query(prompt=prompt, options=options).__aiter__()
    while True:
        try:
            message = await gen.__anext__()
            yield message
        except StopAsyncIteration:
            break
        except Exception as e:  # noqa: BLE001
            if "Unknown message type" in str(e):
                logger.debug("Skipping unknown SDK message type: %s", e)
                continue
            raise


@runtime_checkable
class ModelClient(Protocol):
    """Abstract interface for LLM clients."""

    async def query(
        self,
        system: str,
        messages: list[dict],
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 1.0,
    ) -> ModelResponse: ...


class AgentSDKClient:
    """Uses ``claude_agent_sdk.query()`` from the Claude Code host runtime.

    ``claude_agent_sdk.query()`` is an async generator that takes a
    ``prompt`` string and a ``ClaudeAgentOptions`` bundle and yields
    ``AssistantMessage`` objects. This client adapts the ``ModelClient``
    interface (system + messages list) to that shape for single-turn
    queries, captures CLI stderr so SDK-internal failures (rate limits,
    auth, CLI crashes) surface to the caller rather than being silently
    dropped, and attaches the captured tail to any raised exception.

    Requires running inside Claude Code where the host injects
    ``claude_agent_sdk`` into the plugin's Python environment.
    ``max_tokens`` and ``temperature`` parameters are accepted for
    interface parity but are not forwarded — the SDK uses session
    defaults.
    """

    def __init__(self) -> None:
        try:
            import claude_agent_sdk
            self._sdk = claude_agent_sdk
        except ImportError:
            raise ImportError(
                "claude_agent_sdk is not available in the current runtime. "
                "This client requires running inside Claude Code."
            )

    async def query(
        self,
        system: str,
        messages: list[dict],
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 1.0,
    ) -> ModelResponse:
        """Send a single-turn query via the Agent SDK and return normalized text."""
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
        )

        prompt = _messages_to_prompt(messages)

        last_exc: Exception | None = None
        last_tail = ""
        for attempt in range(_SDK_MAX_ATTEMPTS):
            stderr_lines: list[str] = []
            options = ClaudeAgentOptions(
                allowed_tools=[],
                max_turns=1,
                permission_mode="bypassPermissions",
                system_prompt=system,
                model=model,
                env={"_HOOK_CHILD_SESSION": "1"},
                cwd=str(_hook_session_dir()),
                setting_sources=[],
                # strict-mcp-config isolates this subprocess from
                # account-level MCP integrations (claude.ai Gmail/Drive/
                # Calendar/etc). Without it the bundled CLI discovers those
                # OAuth integrations and tries to authenticate them in a
                # non-interactive subprocess, collapsing with exit 1 and no
                # usable stderr. Verified root cause 2026-05-19 against
                # mcp-needs-auth-cache.json; see anthropics/
                # claude-agent-sdk-python issues + PLANS doc.
                extra_args={
                    "setting-sources": "",
                    "debug-to-stderr": None,
                    "strict-mcp-config": None,
                },
                stderr=lambda line, _s=stderr_lines: _s.append(line),
            )

            chunks: list[str] = []
            try:
                async for message in _safe_query(
                    self._sdk, prompt=prompt, options=options
                ):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)
                return ModelResponse(content="".join(chunks).strip())
            except Exception as e:  # noqa: BLE001
                last_exc = e
                last_tail = "\n".join(stderr_lines[-_STDERR_TAIL_LINES:])
                if attempt + 1 < _SDK_MAX_ATTEMPTS:
                    logger.warning(
                        "claude_agent_sdk.query() failed (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        attempt + 1,
                        _SDK_MAX_ATTEMPTS,
                        _SDK_RETRY_BACKOFF_S,
                        e,
                    )
                    await asyncio.sleep(_SDK_RETRY_BACKOFF_S)

        raise SDKQueryError(
            f"claude_agent_sdk.query() failed after {_SDK_MAX_ATTEMPTS} "
            f"attempts: {last_exc}",
            stderr_tail=last_tail,
        ) from last_exc


class AnthropicAPIClient:
    """Uses the anthropic PyPI package with an explicit API key.

    The underlying ``AsyncAnthropic`` client is created lazily on the
    first call to :meth:`query`, so the ``anthropic`` package need not
    be importable at instantiation time.
    """

    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise ValueError(
                "An API key is required for the Anthropic fallback client. "
                "Set CLAUDE_PLUGIN_OPTION_anthropic_api_key or pass api_key directly."
            )
        self._api_key = api_key
        self._client = None  # lazily created on first query

    def _ensure_client(self):
        """Lazily initialize the AsyncAnthropic client on first use."""
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def query(
        self,
        system: str,
        messages: list[dict],
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 1.0,
    ) -> ModelResponse:
        """Send a query via the Anthropic API and return a normalized response."""
        client = self._ensure_client()
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
        )
        # Empty content list (tool-only turn, refusal, non-text stop) would
        # otherwise IndexError and convert a recoverable empty reply into a
        # total extraction/routing failure.
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        return ModelResponse(content=text)


def detect_client_type() -> str:
    """Detect which model client backend will be used.

    Returns a human-readable string indicating the selected client type.
    This is a synchronous check suitable for logging at session start.
    """
    try:
        import claude_agent_sdk  # noqa: F401
        return "AgentSDKClient"
    except ImportError:
        key = os.environ.get("CLAUDE_PLUGIN_OPTION_anthropic_api_key", "")
        if key:
            return "AnthropicAPIClient"
        return "none (no SDK or API key)"


async def create_client(*, api_key: str | None = None) -> ModelClient:
    """Create a model client. Tries Agent SDK first, falls back to API key.

    Args:
        api_key: Optional API key override. If not provided, reads from
                 CLAUDE_PLUGIN_OPTION_anthropic_api_key env var.

    Returns:
        A ModelClient instance.

    Raises:
        RuntimeError: If neither Agent SDK nor API key is available.
    """
    try:
        client = AgentSDKClient()
        logger.info("Model client: Agent SDK selected (zero-config)")
        return client
    except ImportError:
        pass

    # Fall back to API key
    key = api_key or os.environ.get("CLAUDE_PLUGIN_OPTION_anthropic_api_key", "")
    if not key:
        raise RuntimeError(
            "Neither the Agent SDK nor an API key is available. "
            "Install claude_agent_sdk or set CLAUDE_PLUGIN_OPTION_anthropic_api_key."
        )

    logger.warning("Model client: Falling back to Anthropic API key authentication")
    return AnthropicAPIClient(key)
