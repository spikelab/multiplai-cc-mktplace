"""Model client abstraction for multiplai plugin.

Provides a Protocol-based interface with two implementations:
- AgentSDKClient: uses claude_agent_sdk from the host runtime (zero-config)
- AnthropicAPIClient: uses the anthropic PyPI package with an API key

The create_client() factory tries Agent SDK first, falls back to API key.
"""

import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 4096


@dataclass(frozen=True)
class ModelResponse:
    """Normalized response from any model client."""
    content: str


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
    """Uses claude_agent_sdk.query() from the Claude Code host runtime.

    Requires running inside Claude Code where the host injects
    ``claude_agent_sdk`` into the plugin's Python environment.
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
        """Send a query via the Agent SDK and return a normalized response."""
        response = self._sdk.query(
            system=system,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return ModelResponse(content=self._extract_text(response))

    @staticmethod
    def _extract_text(response: object) -> str:
        """Extract text content from an Agent SDK response.

        The SDK may return content as a list of TextBlock objects,
        a plain string attribute, or a raw object.
        """
        if hasattr(response, "content"):
            if isinstance(response.content, list):
                return response.content[0].text
            return str(response.content)
        return str(response)


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
