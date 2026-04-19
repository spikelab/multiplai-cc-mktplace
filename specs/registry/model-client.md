## ADDED Requirements

### Requirement: ModelClient abstract interface
The `ModelClient` class defines an abstract interface with an async `query()` method that accepts a system prompt (str), a list of messages (list[dict]), and optional kwargs (model, max_tokens, temperature), and returns a response object with a `content` attribute containing the LLM's text output.

#### Scenario: Interface contract enforcement
- **WHEN** a class subclasses `ModelClient` without implementing `query()`
- **THEN** instantiating that class raises `TypeError` indicating the abstract method is not implemented

#### Scenario: query method signature
- **WHEN** `query()` is called with `system`, `messages`, and optional `model`, `max_tokens`, `temperature` kwargs
- **THEN** the method accepts all parameters without error and forwards them to the underlying provider

---

### Requirement: AgentSDKClient implementation
`AgentSDKClient` implements `ModelClient` using `claude_agent_sdk.query()` from the host runtime. It requires no API key — authentication is handled via Claude Code's OAuth session.

#### Scenario: Successful query via Agent SDK
- **WHEN** `AgentSDKClient.query()` is called with a system prompt and messages, and `claude_agent_sdk` is available in the runtime
- **THEN** it delegates to `claude_agent_sdk.query()` with the provided parameters and returns the response

#### Scenario: Agent SDK not installed
- **WHEN** `AgentSDKClient` is instantiated but `claude_agent_sdk` is not importable
- **THEN** instantiation raises `ImportError` with a message indicating the Agent SDK is unavailable

#### Scenario: Agent SDK query failure
- **WHEN** `claude_agent_sdk.query()` raises an exception during a call
- **THEN** `AgentSDKClient.query()` propagates the exception without swallowing it

---

### Requirement: AnthropicAPIClient implementation
`AnthropicAPIClient` implements `ModelClient` using the `anthropic` PyPI package with an explicit API key. It serves as the fallback when the Agent SDK is unavailable.

#### Scenario: Successful query via Anthropic API
- **WHEN** `AnthropicAPIClient.query()` is called with a valid API key, system prompt, and messages
- **THEN** it calls `anthropic.AsyncAnthropic(api_key=...).messages.create()` with the provided parameters and returns a response whose `content` attribute contains the model's text output

#### Scenario: Missing API key
- **WHEN** `AnthropicAPIClient` is instantiated with `api_key=None` or an empty string
- **THEN** instantiation raises `ValueError` with a message indicating an API key is required for the Anthropic fallback client

#### Scenario: Default model
- **WHEN** `AnthropicAPIClient.query()` is called without an explicit `model` kwarg
- **THEN** the request uses `"claude-sonnet-4-20250514"` as the default model

#### Scenario: Model override
- **WHEN** `AnthropicAPIClient.query()` is called with `model="claude-opus-4-20250514"`
- **THEN** the request uses `"claude-opus-4-20250514"` instead of the default

---

### Requirement: create_client() factory function
`create_client()` is an async factory that returns a ready-to-use `ModelClient` instance. It tries `AgentSDKClient` first (zero-config, OAuth) and falls back to `AnthropicAPIClient` if the Agent SDK is not available.

#### Scenario: Agent SDK available — returns AgentSDKClient
- **WHEN** `create_client()` is called and `claude_agent_sdk` is importable
- **THEN** it returns an instance of `AgentSDKClient`

#### Scenario: Agent SDK unavailable, API key provided — returns AnthropicAPIClient
- **WHEN** `create_client()` is called, `claude_agent_sdk` is not importable, and an `api_key` argument is provided
- **THEN** it returns an instance of `AnthropicAPIClient` initialized with the given API key

#### Scenario: Agent SDK unavailable, API key from plugin config
- **WHEN** `create_client()` is called, `claude_agent_sdk` is not importable, no `api_key` argument is passed, and `CLAUDE_PLUGIN_OPTION_anthropic_api_key` environment variable is set
- **THEN** it returns an `AnthropicAPIClient` initialized with the API key from the environment variable

#### Scenario: No SDK and no API key — raises error
- **WHEN** `create_client()` is called, `claude_agent_sdk` is not importable, no `api_key` argument is provided, and no API key environment variable is set
- **THEN** it raises `RuntimeError` with a message explaining that neither the Agent SDK nor an API key is available

---

### Requirement: Response normalization
Both client implementations return a response object with a consistent interface so callers do not need to know which backend is in use.

#### Scenario: AgentSDKClient response has content attribute
- **WHEN** `AgentSDKClient.query()` returns successfully
- **THEN** the return value has a `.content` attribute that is a string containing the model's text response

#### Scenario: AnthropicAPIClient response has content attribute
- **WHEN** `AnthropicAPIClient.query()` returns successfully
- **THEN** the return value has a `.content` attribute that is a string containing the model's text response, extracted from the Anthropic API's `response.content[0].text` structure

---

### Requirement: Async-native interface
All `ModelClient` methods are async, consistent with the project's asyncio-based architecture.

#### Scenario: query is awaitable
- **WHEN** `client.query()` is called on any `ModelClient` implementation
- **THEN** it returns a coroutine that must be awaited

#### Scenario: Works inside asyncio.run
- **WHEN** `create_client()` and `client.query()` are called inside `asyncio.run()`
- **THEN** both complete without event loop errors

---

### Requirement: No vendoring of claude-agent-sdk
The `claude-agent-sdk` package is imported at runtime from the Claude Code host environment. It is not listed in the plugin's `requirements.txt` or `pyproject.toml` dependencies.

#### Scenario: SDK import is deferred
- **WHEN** the `model_client` module is imported in an environment without `claude_agent_sdk` installed
- **THEN** the module imports successfully — `ImportError` is only raised when `AgentSDKClient` is instantiated or when `create_client()` attempts the Agent SDK path

#### Scenario: anthropic is a declared dependency
- **WHEN** the plugin's dependency list is inspected
- **THEN** `anthropic>=0.40.0` is listed and `claude-agent-sdk` is not

---

### Requirement: Timeout and max_tokens defaults
Clients apply sensible defaults for `max_tokens` when the caller does not specify one, preventing unbounded responses.

#### Scenario: Default max_tokens applied
- **WHEN** `query()` is called without a `max_tokens` kwarg
- **THEN** the request is sent with `max_tokens=4096`

#### Scenario: Caller override of max_tokens
- **WHEN** `query()` is called with `max_tokens=16000`
- **THEN** the request uses `16000`, not the default

---

### Requirement: Logging on fallback
The `create_client()` factory logs which client was selected so operators can diagnose configuration issues.

#### Scenario: Agent SDK selected — info log
- **WHEN** `create_client()` successfully creates an `AgentSDKClient`
- **THEN** an info-level log message is emitted indicating Agent SDK client was selected

#### Scenario: Fallback to Anthropic API — warning log
- **WHEN** `create_client()` falls back to `AnthropicAPIClient`
- **THEN** a warning-level log message is emitted indicating fallback to API key authentication