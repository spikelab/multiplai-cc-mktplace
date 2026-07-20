# Research Pipeline — Development Notes

This directory contains the Python pipeline that backs the `deep-research` skill. It's invoked by `../SKILL.md` as a subprocess:

```bash
uv run --directory <this-dir> python -m research_pipeline --query "..." [options]
```

## Architecture

- **Code drives orchestration** (`pipeline.py`), LLM handles reasoning at specific nodes.
- **Dependencies** are declared in `pyproject.toml` (this dir); `uv run --directory` resolves them into an ephemeral env — no shared root venv, no PYTHONPATH.
- **Stateless LLM calls** via `sdk.llm_call()` — no `ClaudeSDKClient`, no conversations, no tools.
- **Strict timeouts** everywhere network touches the wire. `asyncio.wait_for` is mandatory, not optional.

## Module Map

| Module | Purpose |
|---|---|
| `pipeline.py` | Orchestrator — sequences stages, invokes gates, handles recovery and resume |
| `config.py` | `ResearchConfig` + preset definitions (micro/quick/standard/thorough) + per-node `models`/`efforts` tier maps |
| `models.py` | Pydantic models for every structured type flowing through the pipeline |
| `state.py` | `ResearchState` with per-source granularity + JSON checkpointing |
| `gates.py` | Query diversity, min sources, coverage, reassess — pure functions |
| `search_router.py` | Multi-API routing with quota tracking and circuit breaker |
| `fetcher.py` | httpx + trafilatura with hard timeouts, retry, batch isolation |
| `sdk.py` | Adapter over `multiplai_core.run_agent()` — usage tracking, semaphore, structured output validation |
| `progress.py` | Human-readable progress file writer (tail-able) |
| `research_types.py` | Loads `../references/research-types.md` for type-specific guidance |
| `eval.py` | Quality scoring harness + dataset builder from existing research outputs |
| `nodes/` | Per-stage node implementations (plan, search, triage, read, reassess, verify, quality_check, synthesize, challenge) |
| `prompts/` | Focused prompt templates for each LLM node |

## Running Tests

```bash
uv run --directory <this-dir> --extra dev python -m pytest tests/ -q
```

All tests use stubs/mocks — no real API calls. Run in milliseconds.

## Provider Architecture

**Claude Agent is the default.** The pipeline uses `claude_agent_sdk.query(allowed_tools=["WebSearch"])` and `["WebFetch"]` as the primary provider, backed by the user's Claude Max subscription ($200/mo flat rate, unlimited tools). External APIs are fallback only.

**Three key classes:**
- `ClaudeAgentSearchProvider` (in `search_router.py`) — implements `SearchProvider` protocol
- `ClaudeAgentFetcher` (in `claude_agent_fetcher.py`) — implements `FetcherProtocol`, uses Strategy C (combined fetch + extract in one SDK call)
- `HttpxFetcher` (in `claude_agent_fetcher.py`) — wraps existing `fetcher.py` free functions for the external-API path

**Config flags:**
- `ResearchConfig.prefer_claude_tools` (default True) — `--no-claude-tools` to disable
- `ResearchConfig.allow_paid_fallback` (default False) — `--allow-paid-fallback` to enable (only unlocks Serper beyond free tier; Tavily/Exa/Brave are capped at free tier always)

**Model/effort tiers:** every LLM call site reads `config.models[node]` and
`config.efforts[node]`. Search, triage, extract, and verify run the parse-tier
model (sonnet) at `effort="low"` — they're mechanical formatting/parsing work.
The Claude Agent search provider gets its pin via
`build_default_router(model=..., effort=...)`. `--model`/`--effort` override
all nodes uniformly.

**Retry policy:** `sdk.llm_call` defaults to `max_attempts=2` (one transient
retry inside `run_agent`). The fetcher and the Claude Agent search provider
pass `max_attempts=1` — the router's provider failover and the per-source
FAILED handling are their retry layer; stacking SDK retries on top would
double worst-case latency.

Full provider comparison: `references/search-engines.md`

## Provider Cost Tiers

**Free tier (used automatically, no flag needed):**
- Claude Agent — unlimited (Claude Max subscription)
- Tavily — 1,000/mo free (capped, never used beyond free tier)
- Exa — 1,000/mo free (capped, never used beyond free tier)
- Brave — 1,000/mo free (capped, never used beyond free tier)
- Serper — 50K one-time free credits

**Paid overflow (`--allow-paid-fallback` required):**
- Serper — $1/1K queries (cheapest). Only provider allowed beyond free tier.
- Tavily, Exa, Brave are **never** used beyond their free quota, even with `--allow-paid-fallback` ($5/1K is too expensive vs Serper's $1/1K).

## Adding a New Search Provider

1. Implement the `SearchProvider` protocol in `search_router.py`:
   - `name: str`
   - `monthly_limit: int | None`
   - `one_time_limit: int | None`
   - `async def search(query, max_results) -> list[SearchResult]`
2. Add its API key env var check to `pipeline.OPTIONAL_API_KEYS`.
3. Add it to `build_default_router()`.
4. Include its name in `RouterConfig.keyword_priority` or `semantic_priority`.
5. Write tests in `tests/test_search_router.py` using the `StubProvider` pattern.

## Adding a New LLM Node

1. Create a prompt template in `prompts/<node>.py`.
2. Create the node function in `nodes/<node>.py` that calls `sdk.llm_call_structured()` (for JSON output) or `sdk.llm_call()` (for markdown output).
3. Define a response Pydantic model at the top of the node file.
4. Wire it into `pipeline._run_main_stages()` at the right stage.
5. Add a stage value to `state.Stage` if it needs its own checkpoint.

## Common Pitfalls

**Pyright false positives on relative imports.** The `..config` imports in `nodes/` can confuse Pyright depending on how the package root is resolved. The `pyrightconfig.json` silences these — runtime is fine (verified by the tests).

**Don't bypass the SearchRouter.** Direct `tavily-python` or `exa-py` calls skip quota tracking and the circuit breaker.

**Never return `None` from a fetch.** Use `FetchResult` with a typed `FetchError`. Silent Nones break per-source tracking.

**Don't add retry loops inside nodes.** Retry belongs at the fetcher level (transient errors) or the SDK wrapper level (LLM validation). Nodes should be idempotent and fail loudly.

**State transitions must go through `state.advance_to()`.** Don't set `state.stage` directly — `advance_to()` also checkpoints to disk.

**The reassess cycle is sequential ON PURPOSE.** `_run_reassess_cycle` runs
refinement, then verification — verification's verdict node must see the
findings refinement added, and both legs mutate `state.sources`/`state.findings`
(concurrent mutation corrupts per-source tracking). Don't "optimize" it back to
`asyncio.gather` (there's a test asserting it's gone). Each leg fails loudly
into `state.refinement_error`/`verification_error`, which `_format_reassessment`
surfaces to the synthesis prompt.

**Don't persist full page content on state.** `mark_source_extracted` truncates
`extracted_content` to a 2000-char debug excerpt — the checkpoint is rewritten
after every source, and full content makes it grow to tens of MB on a thorough
run. Findings carry the signal; anything needing full content must consume it
in-flight, not from the checkpoint.

## Debugging a Failing Pipeline Run

1. Check the progress file: `tail -f {output}/{slug}-{date}-progress.md`
2. Read the state file: `{output}/{slug}-{date}-state.json`
3. Re-run — the pipeline will resume from the last checkpointed stage (and from per-source granularity within READ).
4. If the pipeline is hung on a fetch, check `total_fetches` in state.json vs `max_total_fetches` in the preset. The fetcher has 15s per-request and 30s per-batch hard caps — anything beyond that is a bug in the fetcher, not a slow URL.
