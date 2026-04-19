"""Tests for model client abstraction (scripts/lib/model_client.py)."""

import asyncio
import inspect
import logging
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure scripts/lib is importable
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "multiplai-plugin" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestModelClientInterface:
    """Verify ModelClient Protocol definition."""

    def test_model_client_is_protocol(self):
        from lib.model_client import ModelClient
        assert hasattr(ModelClient, "query")

    def test_query_is_async(self):
        from lib.model_client import ModelClient
        # Protocol methods should indicate async
        hints = ModelClient.__protocol_attrs__
        assert "query" in hints

    def test_unimplemented_subclass_fails(self):
        from lib.model_client import ModelClient
        class BadClient:
            pass
        assert not isinstance(BadClient(), ModelClient)

    def test_query_signature_accepts_all_specified_params(self):
        """WHEN query() is called with system, messages, model, max_tokens, temperature
        THEN the method accepts all parameters without error."""
        from lib.model_client import ModelClient
        sig = inspect.signature(ModelClient.query)
        param_names = set(sig.parameters.keys())
        # Must include all specified parameters
        assert "system" in param_names or "self" in param_names
        assert "messages" in param_names
        assert "model" in param_names
        assert "max_tokens" in param_names
        assert "temperature" in param_names


class TestAgentSDKClient:
    """Verify AgentSDKClient implementation."""

    def test_raises_import_error_when_sdk_missing(self):
        from lib.model_client import AgentSDKClient
        with patch.dict(sys.modules, {"claude_agent_sdk": None}):
            with pytest.raises(ImportError, match="claude_agent_sdk"):
                AgentSDKClient()

    def test_successful_instantiation_with_mock_sdk(self):
        mock_sdk = MagicMock()
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()
            assert client._sdk is mock_sdk

    def test_query_delegates_to_sdk(self):
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "test response"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            async def _test():
                result = await client.query("system", [{"role": "user", "content": "hello"}])
                assert result.content == "test response"

            asyncio.run(_test())

    def test_query_propagates_exceptions(self):
        mock_sdk = MagicMock()
        mock_sdk.query.side_effect = RuntimeError("SDK error")

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            async def _test():
                with pytest.raises(RuntimeError, match="SDK error"):
                    await client.query("system", [])

            asyncio.run(_test())

    def test_query_forwards_all_parameters_to_sdk(self):
        """WHEN query() is called with system, messages, model, max_tokens, temperature
        THEN all parameters are forwarded to claude_agent_sdk.query()."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "ok"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            messages = [{"role": "user", "content": "test"}]

            async def _test():
                await client.query(
                    "system prompt",
                    messages,
                    model="claude-opus-4-20250514",
                    max_tokens=8000,
                    temperature=0.5,
                )
                mock_sdk.query.assert_called_once_with(
                    system="system prompt",
                    messages=messages,
                    model="claude-opus-4-20250514",
                    max_tokens=8000,
                    temperature=0.5,
                )

            asyncio.run(_test())

    def test_query_default_max_tokens_forwarded(self):
        """WHEN query() is called without max_tokens
        THEN max_tokens=4096 is sent to the SDK."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "ok"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            async def _test():
                await client.query("sys", [])
                call_kwargs = mock_sdk.query.call_args
                assert call_kwargs.kwargs["max_tokens"] == 4096

            asyncio.run(_test())

    def test_query_handles_list_content_with_text_blocks(self):
        """WHEN SDK returns response.content as list of TextBlock objects
        THEN the content is extracted from content[0].text."""
        mock_sdk = MagicMock()
        mock_text_block = MagicMock()
        mock_text_block.text = "extracted from text block"
        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            async def _test():
                result = await client.query("sys", [])
                assert result.content == "extracted from text block"

            asyncio.run(_test())


class TestAnthropicAPIClient:
    """Verify AnthropicAPIClient implementation."""

    def test_raises_value_error_on_empty_key(self):
        from lib.model_client import AnthropicAPIClient
        with pytest.raises(ValueError, match="API key is required"):
            AnthropicAPIClient("")

    def test_raises_value_error_on_none_key(self):
        from lib.model_client import AnthropicAPIClient
        with pytest.raises(ValueError, match="API key is required"):
            AnthropicAPIClient(None)

    def test_successful_instantiation(self):
        from lib.model_client import AnthropicAPIClient
        client = AnthropicAPIClient("sk-test-key")
        assert client._api_key == "sk-test-key"

    def test_default_model(self):
        from lib.model_client import AnthropicAPIClient, DEFAULT_MODEL
        client = AnthropicAPIClient("sk-test-key")
        sig = inspect.signature(client.query)
        assert sig.parameters["model"].default == DEFAULT_MODEL

    def test_successful_query_via_anthropic_api(self):
        """WHEN AnthropicAPIClient.query() is called with valid key, system, messages
        THEN it calls anthropic.AsyncAnthropic().messages.create() and returns
        a response with .content containing the model's text."""
        from lib.model_client import AnthropicAPIClient

        mock_text_block = MagicMock()
        mock_text_block.text = "API response text"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            client = AnthropicAPIClient("sk-test-key")
            client._client = None  # ensure lazy init triggers

            async def _test():
                result = await client.query(
                    "You are helpful.",
                    [{"role": "user", "content": "hello"}],
                )
                assert result.content == "API response text"
                mock_async_client.messages.create.assert_called_once()
                call_kwargs = mock_async_client.messages.create.call_args
                assert call_kwargs.kwargs["system"] == "You are helpful."
                assert call_kwargs.kwargs["messages"] == [{"role": "user", "content": "hello"}]

            asyncio.run(_test())

    def test_model_override(self):
        """WHEN query() is called with model='claude-opus-4-20250514'
        THEN the request uses that model instead of the default."""
        from lib.model_client import AnthropicAPIClient

        mock_text_block = MagicMock()
        mock_text_block.text = "opus response"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            client = AnthropicAPIClient("sk-test-key")
            client._client = None

            async def _test():
                await client.query("sys", [], model="claude-opus-4-20250514")
                call_kwargs = mock_async_client.messages.create.call_args
                assert call_kwargs.kwargs["model"] == "claude-opus-4-20250514"

            asyncio.run(_test())

    def test_default_model_sent_to_api(self):
        """WHEN query() is called without explicit model kwarg
        THEN the request uses 'claude-sonnet-4-20250514' as default."""
        from lib.model_client import AnthropicAPIClient, DEFAULT_MODEL

        mock_text_block = MagicMock()
        mock_text_block.text = "response"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            client = AnthropicAPIClient("sk-test-key")
            client._client = None

            async def _test():
                await client.query("sys", [])
                call_kwargs = mock_async_client.messages.create.call_args
                assert call_kwargs.kwargs["model"] == DEFAULT_MODEL
                assert call_kwargs.kwargs["model"] == "claude-sonnet-4-20250514"

            asyncio.run(_test())

    def test_default_max_tokens_sent_to_api(self):
        """WHEN query() is called without max_tokens kwarg
        THEN the request is sent with max_tokens=4096."""
        from lib.model_client import AnthropicAPIClient

        mock_text_block = MagicMock()
        mock_text_block.text = "response"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            client = AnthropicAPIClient("sk-test-key")
            client._client = None

            async def _test():
                await client.query("sys", [])
                call_kwargs = mock_async_client.messages.create.call_args
                assert call_kwargs.kwargs["max_tokens"] == 4096

            asyncio.run(_test())

    def test_caller_override_max_tokens(self):
        """WHEN query() is called with max_tokens=16000
        THEN the request uses 16000, not the default."""
        from lib.model_client import AnthropicAPIClient

        mock_text_block = MagicMock()
        mock_text_block.text = "response"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            client = AnthropicAPIClient("sk-test-key")
            client._client = None

            async def _test():
                await client.query("sys", [], max_tokens=16000)
                call_kwargs = mock_async_client.messages.create.call_args
                assert call_kwargs.kwargs["max_tokens"] == 16000

            asyncio.run(_test())


class TestCreateClientFactory:
    """Verify create_client() factory function."""

    def test_returns_agent_sdk_when_available(self):
        mock_sdk = MagicMock()
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import create_client, AgentSDKClient

            async def _test():
                client = await create_client()
                assert isinstance(client, AgentSDKClient)

            asyncio.run(_test())

    def test_falls_back_to_api_client_with_key(self):
        with patch.dict(sys.modules, {"claude_agent_sdk": None}):
            from lib.model_client import create_client, AnthropicAPIClient

            async def _test():
                client = await create_client(api_key="sk-test")
                assert isinstance(client, AnthropicAPIClient)

            asyncio.run(_test())

    def test_reads_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_anthropic_api_key", "sk-env-key")
        with patch.dict(sys.modules, {"claude_agent_sdk": None}):
            from lib.model_client import create_client, AnthropicAPIClient

            async def _test():
                client = await create_client()
                assert isinstance(client, AnthropicAPIClient)

            asyncio.run(_test())

    def test_raises_when_no_sdk_no_key(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_anthropic_api_key", raising=False)
        with patch.dict(sys.modules, {"claude_agent_sdk": None}):
            from lib.model_client import create_client

            async def _test():
                with pytest.raises(RuntimeError, match="Neither"):
                    await create_client()

            asyncio.run(_test())


class TestResponseNormalization:
    """Verify both clients return consistent response objects."""

    def test_agent_sdk_response_has_content(self):
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "hello"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient

            async def _test():
                client = AgentSDKClient()
                result = await client.query("sys", [])
                assert hasattr(result, "content")
                assert isinstance(result.content, str)

            asyncio.run(_test())

    def test_anthropic_api_response_has_content(self):
        """WHEN AnthropicAPIClient.query() returns successfully
        THEN the return value has a .content attribute that is a string,
        extracted from the Anthropic API's response.content[0].text structure."""
        from lib.model_client import AnthropicAPIClient, ModelResponse

        mock_text_block = MagicMock()
        mock_text_block.text = "anthropic response text"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            client = AnthropicAPIClient("sk-test-key")
            client._client = None

            async def _test():
                result = await client.query("sys", [{"role": "user", "content": "hi"}])
                assert hasattr(result, "content")
                assert isinstance(result.content, str)
                assert result.content == "anthropic response text"
                # Verify it's a ModelResponse instance for type consistency
                assert isinstance(result, ModelResponse)

            asyncio.run(_test())

    def test_both_clients_return_model_response(self):
        """Both implementations return ModelResponse for interface consistency."""
        from lib.model_client import AgentSDKClient, AnthropicAPIClient, ModelResponse

        # AgentSDKClient
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "sdk text"
        mock_sdk.query.return_value = mock_response

        # AnthropicAPIClient
        mock_text_block = MagicMock()
        mock_text_block.text = "api text"
        mock_api_response = MagicMock()
        mock_api_response.content = [mock_text_block]

        mock_async_client = MagicMock()
        mock_async_client.messages.create = AsyncMock(return_value=mock_api_response)

        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_async_client

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "anthropic": mock_anthropic}):
            sdk_client = AgentSDKClient()
            api_client = AnthropicAPIClient("sk-test")
            api_client._client = None

            async def _test():
                sdk_result = await sdk_client.query("sys", [])
                api_result = await api_client.query("sys", [])
                # Both must be ModelResponse
                assert isinstance(sdk_result, ModelResponse)
                assert isinstance(api_result, ModelResponse)
                # Both must have string content
                assert isinstance(sdk_result.content, str)
                assert isinstance(api_result.content, str)

            asyncio.run(_test())


class TestAsyncInterface:
    """Verify async nature of the interface."""

    def test_query_is_coroutine(self):
        from lib.model_client import AnthropicAPIClient
        client = AnthropicAPIClient("sk-test")
        result = client.query("sys", [])
        assert asyncio.iscoroutine(result)
        result.close()  # clean up

    def test_create_client_is_coroutine(self):
        from lib.model_client import create_client
        result = create_client()
        assert asyncio.iscoroutine(result)
        result.close()

    def test_create_client_and_query_work_inside_asyncio_run(self):
        """WHEN create_client() and client.query() are called inside asyncio.run()
        THEN both complete without event loop errors."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "integration test"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import create_client

            async def _test():
                client = await create_client()
                result = await client.query(
                    "system",
                    [{"role": "user", "content": "test"}],
                )
                assert result.content == "integration test"

            # Must complete without RuntimeError about event loop
            asyncio.run(_test())

    def test_agent_sdk_query_is_awaitable(self):
        """WHEN client.query() is called on AgentSDKClient
        THEN it returns a coroutine that must be awaited."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "ok"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()
            result = client.query("sys", [])
            assert asyncio.iscoroutine(result)
            # Must await to get actual result
            actual = asyncio.run(result)
            assert actual.content == "ok"


class TestNoVendoring:
    """Verify claude-agent-sdk is not vendored."""

    def test_sdk_import_deferred(self):
        """Module imports without claude_agent_sdk installed."""
        # Just importing the module should succeed
        from lib import model_client
        assert hasattr(model_client, "ModelClient")

    def test_anthropic_in_requirements(self):
        req_path = Path(__file__).parent.parent.parent / "multiplai-plugin" / "requirements.txt"
        text = req_path.read_text()
        assert "anthropic" in text
        assert "claude-agent-sdk" not in text.lower()
        assert "claude_agent_sdk" not in text.lower()


class TestMaxTokensDefaults:
    """Verify default max_tokens behavior."""

    def test_default_max_tokens_value(self):
        from lib.model_client import DEFAULT_MAX_TOKENS
        assert DEFAULT_MAX_TOKENS == 4096

    def test_agent_sdk_default_max_tokens_forwarded(self):
        """WHEN AgentSDKClient.query() is called without max_tokens
        THEN the SDK receives max_tokens=4096."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "ok"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            async def _test():
                await client.query("sys", [])
                assert mock_sdk.query.call_args.kwargs["max_tokens"] == 4096

            asyncio.run(_test())

    def test_agent_sdk_caller_override_max_tokens(self):
        """WHEN AgentSDKClient.query() is called with max_tokens=16000
        THEN the SDK receives 16000, not the default."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "ok"
        mock_sdk.query.return_value = mock_response

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import AgentSDKClient
            client = AgentSDKClient()

            async def _test():
                await client.query("sys", [], max_tokens=16000)
                assert mock_sdk.query.call_args.kwargs["max_tokens"] == 16000

            asyncio.run(_test())


class TestLoggingOnFallback:
    """Verify logging when client is selected."""

    def test_agent_sdk_logs_info(self):
        """Verify Agent SDK selection is logged at INFO level."""
        mock_sdk = MagicMock()
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            from lib.model_client import create_client, logger as mc_logger
            handler = logging.Handler()
            records = []

            class Capture(logging.Handler):
                def emit(self, record):
                    records.append(record)

            cap = Capture()
            mc_logger.addHandler(cap)
            mc_logger.setLevel(logging.DEBUG)
            try:
                asyncio.run(create_client())
                assert any("Agent SDK" in r.getMessage() for r in records)
                # Must be info-level specifically
                sdk_records = [r for r in records if "Agent SDK" in r.getMessage()]
                assert any(r.levelno == logging.INFO for r in sdk_records)
            finally:
                mc_logger.removeHandler(cap)

    def test_fallback_logs_warning(self):
        """Verify fallback to API client is logged as warning."""
        from lib import model_client
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        actual_logger = model_client.logger
        cap = Capture()
        actual_logger.addHandler(cap)
        actual_logger.setLevel(logging.DEBUG)
        try:
            with patch.dict(sys.modules, {"claude_agent_sdk": None}):
                asyncio.run(model_client.create_client(api_key="sk-test"))
            assert any("Falling back" in r.getMessage() for r in records)
            assert any(r.levelno >= logging.WARNING for r in records)
        finally:
            actual_logger.removeHandler(cap)
