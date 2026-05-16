"""Model client abstraction for multiplai plugin.

Provides a Protocol-based interface with two implementations:
- AgentSDKClient: uses claude_agent_sdk from the host runtime (zero-config)
- AnthropicAPIClient: uses the anthropic PyPI package with an API key

The create_client() factory tries Agent SDK first, falls back to API key.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 4096
_STDERR_TAIL_LINES = 2000


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
            extra_args={"setting-sources": "", "debug-to-stderr": None},
            stderr=lambda line: stderr_lines.append(line),
        )

        chunks: list[str] = []
        try:
            async for message in self._sdk.query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
        except SDKQueryError:
            raise
        except Exception as e:
            tail = "\n".join(stderr_lines[-_STDERR_TAIL_LINES:])
            raise SDKQueryError(
                f"claude_agent_sdk.query() failed: {e}",
                stderr_tail=tail,
            ) from e

        return ModelResponse(content="".join(chunks).strip())


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
        return ModelResponse(content=response.content[0].text)


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
