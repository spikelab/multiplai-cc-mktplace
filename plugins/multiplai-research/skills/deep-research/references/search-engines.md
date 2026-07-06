# Search Engine Providers — Ranking, Comparison, Usage Guide

**Last Updated:** 2026-04-06

## Provider Overview

The deep-research pipeline routes search queries and content fetching through a `SearchRouter` that supports multiple providers with quota tracking, circuit breaker, and strict timeouts. Providers are ranked by priority — the router tries them in order and falls back on failure.

### Default Priority (with Claude Max subscription)

| Priority | Provider | Strategy | Why |
|---|---|---|---|
| **1 (default)** | Claude Agent | keyword + semantic | Zero marginal cost on Max ($200/mo flat). Uses Claude Code's built-in WebSearch/WebFetch. Hard-cancellable via asyncio.wait_for. |
| 2 (fallback) | Brave | keyword | 1,000 queries/month free (requires CC). Capped at free tier. |
| 3 (fallback) | Tavily | keyword | AI-optimized search with clean LLM-ready snippets. 1,000 credits/month free (no CC). Capped at free tier. |
| 4 (fallback) | Exa | semantic | Neural/semantic search with category-aware retrieval. 1,000 requests/month free (no CC). Capped at free tier. |
| 5 (fallback) | Serper.dev | keyword (Google SERP) | 50K one-time free credits (no CC). Only provider allowed beyond free tier ($1/1K — cheapest paid). |
| 6 (fallback) | You.com | keyword | $100 one-time free credit (~20K searches, no CC). |

### Priority without Claude Max (`--no-claude-tools`)

| Priority | Provider | Notes |
|---|---|---|
| 1 | Brave | Primary keyword search (if configured) |
| 2 | Tavily | Primary keyword search |
| 3 | Exa | Primary semantic search |
| 4 | Serper.dev | Google SERP quality, only paid overflow provider |
| 5 | You.com | Overflow capacity |

## Provider Details

### Claude Agent (default)

**How it works:** The pipeline calls `claude_agent_sdk.query(allowed_tools=["WebSearch"])` or `["WebFetch"]` for each search/fetch. Each call spawns a Claude Code subprocess that has access to the same WebSearch/WebFetch tools available in interactive Claude Code sessions.

**Strategy C (combined fetch + extract):** For content fetching, a single SDK call does both: the agent fetches the URL via WebFetch and extracts structured findings (JSON with facts, quotes, confidence) in the same turn. This is the most token-efficient approach — PoC-validated:

| Metric | Strategy C (combined) | Two-call alternative |
|---|---|---|
| SDK calls per source | 1 | 2 |
| Output tokens | 594 | 897-939 |
| Elapsed per source | 16.9s | 20.1-20.2s |
| Quality | 5 findings, well-cited | 5 findings, well-cited |

**Timeout enforcement:** Every SDK call is wrapped in `asyncio.wait_for(timeout=15)`. PoC validated that this cleanly cancels hung WebFetch calls — zero subprocess leaks, 2.9s recovery. This is the fix for the hung-WebFetch failure mode that plagued the old prompt-driven deep-research skill.

**Cost:** Zero marginal cost on Claude Max ($200/mo flat rate). Each call costs ~$0.01-0.05 at API pricing (not applicable to Max).

**Quota:** Unlimited (monthly_limit=None, one_time_limit=None). Relies on circuit breaker for failure handling — 3 consecutive failures triggers a 5-minute cooldown.

**Rate limits:** Not publicly documented for Max. PoC sustained 0.30 calls/sec for 33 seconds without hitting limits. Conservative batching (3-wide parallelism) is recommended.

**Fallback behavior:** When Claude Agent circuit breaker trips, the pipeline raises `PaidFallbackError` unless `--allow-paid-fallback` was set. This prevents silent spending on external APIs.

### Tavily

**What it is:** AI-optimized web search API. Purpose-built for LLM pipelines — returns clean, LLM-ready snippets without boilerplate.

**Free tier:** 1,000 API credits/month. No credit card required. Basic Search = 1 credit, Advanced Search = 2 credits. Effective: 500-1,000 searches/month.

**API endpoints:** Search, Extract (batch URL content), Crawl (recursive site traversal), Map (URL discovery), Research (multi-step synthesis).

**Python SDK:** `tavily-python` (official, supports async via `AsyncTavilyClient`).

**Paid tier:** $0.008/credit pay-as-you-go. Project plan $30/mo for 4,000 credits.

**Best for:** Keyword research queries, fact-finding, news monitoring.

**Source:** Verified from tavily.com/pricing on 2026-04-05.

### Exa

**What it is:** Neural/semantic search engine with category-aware entity retrieval. Understands queries conceptually — finds matches by meaning, not just keywords.

**Free tier:** 1,000 requests/month. No credit card required.

**Key differentiator — category search:**
- `category="company"` → searches company websites + LinkedIn profiles directly
- `category="people"` → searches LinkedIn + biographical sources
- `category="research paper"` → arxiv, academic publications
- `category="financial report"` → SEC filings, earnings data
- Also: `news`, `personal site`, `pdf`, `github`, `tweet`, `movie`, `song`

**Content modes:** `text` (full page), `highlights` (10x fewer tokens — recommended for agents), `summary` (LLM-generated).

**Python SDK:** `exa-py` (official, supports `search_and_contents()` for combined search + content retrieval).

**Paid tier:** Search $7/1K, Deep Search $12/1K, Contents $1/1K pages, Answer $5/1K.

**Best for:** Conceptual/semantic queries, company/people research (SDR agents), finding entities that don't match keyword vocabulary.

**Future use case:** An SDR (sales development representative) agent would use Exa's `category="company"` and `category="people"` directly — not through the deep-research pipeline. Exa's category-aware retrieval is uniquely suited for structured entity research.

**Source:** Verified from exa.ai/pricing and exa.ai/docs on 2026-04-05.

### Serper.dev

**What it is:** Google SERP results via API. Returns the same results you'd see on Google, structured as JSON.

**Free tier:** 50K one-time free credits (no credit card required). Cheapest paid tier at $1/1K — 5x cheaper than Tavily/Brave.

**NOT the same as SerpAPI.com** — different company, different pricing, different API. SerpAPI.com offers 250/month free but is 10x more expensive after the free tier.

**Python SDK:** None official. Use httpx REST calls to `google.serper.dev/search`.

**Paid tier:** $50 for 50K queries (valid 6 months). $1/1K at volume — cheapest Google SERP access.

**Best for:** Google-quality results when other providers return irrelevant content.

**Source:** Verified from serper.dev homepage on 2026-04-05.

### You.com

**What it is:** Web search API with full page contents returned alongside search results (saves a separate fetch step).

**Free tier:** $100 one-time credit. At $5/1K searches = ~20,000 searches. No credit card required.

**Python SDK:** None official. REST API via httpx.

**Paid tier:** Search $5/1K calls, Contents $1/1K pages, Research $6.50/1K calls.

**Best for:** Burst capacity when primary providers are exhausted.

**Source:** Verified from you.com/platform/upgrade on 2026-04-05.

### Brave Search

**Free tier:** $5/month in automatic credits ≈ 1,000 queries/month. Credit card required.

**Endpoint:** `GET https://api.search.brave.com/res/v1/web/search?q=<query>`. Auth via `X-Subscription-Token` header.

**Paid tier:** $5/1K queries — same as Tavily. Capped at free tier in the pipeline (Serper is 5x cheaper for paid overflow).

**Env var:** `BRAVE_API_KEY`

## When to Use Which Provider

| Use case | Recommended provider | Why |
|---|---|---|
| General deep research (daily use) | Claude Agent (default) | Zero cost on Max, unlimited, hard timeouts |
| Company/people research (SDR) | Exa with `category="company"` / `"people"` | Purpose-built entity retrieval |
| Google-specific results | Serper.dev | Actual Google SERP |
| Semantic/conceptual queries | Exa or Claude Agent | Neural matching, not just keywords |
| Burst capacity (many runs) | You.com (one-time credits) | 20K searches for free |
| Paid overflow (beyond free tiers) | Serper.dev | Cheapest at $1/1K (5x cheaper than Tavily/Brave) |
| No Claude subscription | Tavily + Exa (external only) | Both free, no CC |

## Configuration

```bash
# Default: Claude Agent primary, no external keys needed
python -m research_pipeline --query "..." --preset standard

# Force external APIs only
python -m research_pipeline --query "..." --no-claude-tools

# Allow paid fallback if Claude Agent fails
python -m research_pipeline --query "..." --allow-paid-fallback

# External API keys (optional — add to .env at project root)
TAVILY_API_KEY="tvly-..."
EXA_API_KEY="..."
SERPER_API_KEY="..."
YOU_API_KEY="..."
```

## PoC Validation Data

The provider ranking above was validated with a proof-of-concept covering SDK
cancellation, strategy comparison, and rate-limit probing.
