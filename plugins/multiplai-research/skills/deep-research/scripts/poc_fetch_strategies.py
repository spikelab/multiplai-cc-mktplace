"""PoC: Compare three fetch-and-extract strategies for ClaudeAgentFetcher.

We need to pick the most rate-limit-efficient way to combine WebFetch + finding
extraction in the deep-research pipeline. The key variable is how many SDK
calls (subprocess spawns) we need per source and how many tokens they burn.

STRATEGIES

A) Verbatim pass-through → separate extract call (2 SDK calls)
   Call 1: agent uses WebFetch asking for "full content verbatim" and
           returns whatever comes back.
   Call 2: pure reasoning — agent sees the content from call 1 and extracts
           findings as JSON.

B) Markdown pass-through → separate extract call (2 SDK calls)
   Call 1: agent uses WebFetch asking for "clean markdown" and returns it.
   Call 2: same as A call 2.

C) Combined fetch + extract (1 SDK call)
   One call: agent uses WebFetch with query-directed extraction and returns
   Finding JSON directly. The agent sees page content inside its own context
   and generates the findings in the same turn sequence.

MEASUREMENTS (per strategy)
  - Input tokens (cumulative across all calls)
  - Output tokens (cumulative)
  - total_cost_usd (reported by SDK)
  - Wall-clock elapsed (sequential)
  - Quality: do the findings capture the key facts from the source?
  - Num turns (how many model invocations the agent used internally)

PLUS: rate limit reconnaissance — fire N lightweight calls in a tight loop
to see if/when Claude Max rate limits kick in.

TEST URL
  https://trafilatura.readthedocs.io/en/latest/evaluation.html — a real
  article directly relevant to the research query. Stable, well-known, rich
  with comparative data. A real-ish pipeline use case, not a synthetic one.

TEST QUERY
  "How does trafilatura compare to other HTML extraction libraries, and
  what makes it suitable for LLM pipelines?"

Run from the scripts dir:
    python3 poc_fetch_strategies.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from research_pipeline.env import load_env  # noqa: E402

load_env()

_CFG = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
_HOOK_SESSION_DIR = _CFG / "hook-sessions"
_HOOK_SESSION_DIR.mkdir(exist_ok=True)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)


TEST_URL = "https://trafilatura.readthedocs.io/en/latest/evaluation.html"
TEST_QUERY = (
    "How does trafilatura compare to other HTML extraction libraries, and "
    "what makes it suitable for LLM pipelines?"
)

FINDING_SCHEMA_INSTRUCTION = """Return a JSON array of findings. Each finding \
has shape:
  {"fact": "one-sentence claim", "quote": "direct quote from the source or null"}
Limit to 5 most relevant findings. Return ONLY the JSON array in a fenced \
code block, no prose."""


# ---------------------------------------------------------------------------
# SDK call with metrics
# ---------------------------------------------------------------------------


@dataclass
class CallMetrics:
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    elapsed_s: float = 0.0
    num_turns: int = 0
    error: str | None = None


async def sdk_call(
    prompt: str,
    *,
    allowed_tools: list[str],
    max_turns: int = 3,
) -> CallMetrics:
    """Single SDK query, capture text + usage metrics."""
    start = time.monotonic()
    chunks: list[str] = []
    metrics = CallMetrics()
    stderr_lines: list[str] = []

    options = ClaudeAgentOptions(
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        cwd=str(_HOOK_SESSION_DIR),
        setting_sources=[],
        stderr=lambda line: stderr_lines.append(line),
        extra_args={"setting-sources": "", "debug-to-stderr": None},
        env={"_HOOK_CHILD_SESSION": "1"},
    )

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                usage = getattr(msg, "usage", None) or {}
                metrics.input_tokens = usage.get("input_tokens", 0) or 0
                metrics.output_tokens = usage.get("output_tokens", 0) or 0
                metrics.cache_creation_tokens = (
                    usage.get("cache_creation_input_tokens", 0) or 0
                )
                metrics.cache_read_tokens = (
                    usage.get("cache_read_input_tokens", 0) or 0
                )
                metrics.total_cost_usd = getattr(msg, "total_cost_usd", 0.0) or 0.0
                metrics.num_turns = getattr(msg, "num_turns", 0) or 0
    except Exception as e:  # noqa: BLE001
        captured = "\n".join(stderr_lines[-2000:])
        metrics.error = (
            f"{type(e).__name__}: {e}\nCLI stderr:\n{captured}"
            if captured
            else f"{type(e).__name__}: {e}"
        )

    metrics.text = "".join(chunks).strip()
    metrics.elapsed_s = time.monotonic() - start
    return metrics


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@dataclass
class StrategyResult:
    name: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    elapsed_s: float = 0.0
    num_turns: int = 0
    findings_text: str = ""
    intermediate_chars: int = 0  # chars returned by intermediate step
    notes: str = ""
    error: str | None = None

    def accumulate(self, m: CallMetrics) -> None:
        self.calls += 1
        self.input_tokens += m.input_tokens
        self.output_tokens += m.output_tokens
        self.cache_creation_tokens += m.cache_creation_tokens
        self.cache_read_tokens += m.cache_read_tokens
        self.total_cost_usd += m.total_cost_usd
        self.elapsed_s += m.elapsed_s
        self.num_turns += m.num_turns
        if m.error and not self.error:
            self.error = m.error


async def strategy_a_verbatim(url: str, query_text: str) -> StrategyResult:
    """Ask WebFetch for content verbatim, then run a separate extract call."""
    result = StrategyResult(name="A_verbatim_then_extract")

    prompt1 = (
        f"Use the WebFetch tool on this URL: {url}\n\n"
        f"Pass WebFetch this prompt: 'Return the full page content verbatim. "
        f"Do not summarize, do not skip sections, do not add commentary. "
        f"Preserve the original text as completely as possible.'\n\n"
        f"After WebFetch returns, output the content it returned verbatim "
        f"inside a fenced code block. Do not add any commentary of your own."
    )
    call1 = await sdk_call(prompt1, allowed_tools=["WebFetch"], max_turns=3)
    result.accumulate(call1)

    # Extract the fenced block if present; else use the raw text
    content = _extract_fenced_block(call1.text) or call1.text
    result.intermediate_chars = len(content)

    if call1.error:
        result.error = f"call1: {call1.error}"
        return result

    prompt2 = (
        f"Below is the content of a web page. Extract findings relevant to:\n\n"
        f"QUERY: {query_text}\n\n"
        f"CONTENT:\n{content[:15000]}\n\n"
        f"{FINDING_SCHEMA_INSTRUCTION}"
    )
    call2 = await sdk_call(prompt2, allowed_tools=[], max_turns=1)
    result.accumulate(call2)
    result.findings_text = call2.text
    result.notes = f"verbatim returned {result.intermediate_chars} chars"
    if call2.error and not result.error:
        result.error = f"call2: {call2.error}"
    return result


async def strategy_b_markdown(url: str, query_text: str) -> StrategyResult:
    """Ask WebFetch for clean markdown, then run a separate extract call."""
    result = StrategyResult(name="B_markdown_then_extract")

    prompt1 = (
        f"Use the WebFetch tool on this URL: {url}\n\n"
        f"Pass WebFetch this prompt: 'Extract the main article content as "
        f"clean markdown. Preserve headings, lists, tables, and code blocks. "
        f"Strip navigation, ads, footers, cookie banners, and boilerplate. "
        f"Return only the article markdown.'\n\n"
        f"After WebFetch returns, output the markdown it returned inside a "
        f"fenced code block. Do not add commentary."
    )
    call1 = await sdk_call(prompt1, allowed_tools=["WebFetch"], max_turns=3)
    result.accumulate(call1)

    markdown = _extract_fenced_block(call1.text) or call1.text
    result.intermediate_chars = len(markdown)

    if call1.error:
        result.error = f"call1: {call1.error}"
        return result

    prompt2 = (
        f"Below is the content of a web page. Extract findings relevant to:\n\n"
        f"QUERY: {query_text}\n\n"
        f"CONTENT:\n{markdown[:15000]}\n\n"
        f"{FINDING_SCHEMA_INSTRUCTION}"
    )
    call2 = await sdk_call(prompt2, allowed_tools=[], max_turns=1)
    result.accumulate(call2)
    result.findings_text = call2.text
    result.notes = f"markdown returned {result.intermediate_chars} chars"
    if call2.error and not result.error:
        result.error = f"call2: {call2.error}"
    return result


async def strategy_c_combined(url: str, query_text: str) -> StrategyResult:
    """Single SDK call: WebFetch + directly extract findings."""
    result = StrategyResult(name="C_combined_fetch_and_extract")

    prompt = (
        f"Use the WebFetch tool on this URL: {url}\n\n"
        f"Pass WebFetch a prompt telling it to return the article content "
        f"relevant to this query:\n\nQUERY: {query_text}\n\n"
        f"After WebFetch returns the content, extract findings from it and "
        f"return them in the required format.\n\n"
        f"{FINDING_SCHEMA_INSTRUCTION}"
    )
    call = await sdk_call(prompt, allowed_tools=["WebFetch"], max_turns=3)
    result.accumulate(call)
    result.findings_text = call.text
    result.notes = f"single call, {call.num_turns} agent turns"
    if call.error:
        result.error = call.error
    return result


def _extract_fenced_block(text: str) -> str | None:
    """Extract content from a markdown fenced code block, if present."""
    match = re.search(r"```(?:[a-zA-Z]+)?\s*\n(.*?)\n```", text, re.DOTALL)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Rate limit reconnaissance
# ---------------------------------------------------------------------------


async def rate_limit_probe(n: int = 10) -> dict:
    """Fire N lightweight SDK calls in sequence to see if rate limits trip."""
    print(f"\n{'=' * 60}")
    print(f"RATE LIMIT PROBE: {n} sequential lightweight calls")
    print("=" * 60)
    outcomes = []
    start_total = time.monotonic()
    for i in range(n):
        start = time.monotonic()
        m = await sdk_call(
            f"Respond with exactly: PROBE_{i}",
            allowed_tools=[],
            max_turns=1,
        )
        elapsed = time.monotonic() - start
        status = "ERR" if m.error else "OK"
        print(
            f"  {i+1:>2}: {status}  {elapsed:5.1f}s  "
            f"in={m.input_tokens:>5} out={m.output_tokens:>3} "
            f"cost=${m.total_cost_usd:.4f}"
        )
        if m.error:
            print(f"       {m.error}")
        outcomes.append({"ok": not m.error, "elapsed": elapsed, "error": m.error})
    total_elapsed = time.monotonic() - start_total
    errors = sum(1 for o in outcomes if not o["ok"])
    print(
        f"\n  Total: {n} calls in {total_elapsed:.1f}s "
        f"({n / total_elapsed:.2f} calls/sec)  Errors: {errors}/{n}"
    )
    return {
        "total": n,
        "errors": errors,
        "elapsed_total": total_elapsed,
        "outcomes": outcomes,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_strategy(r: StrategyResult) -> None:
    print(f"\n  {r.name}")
    print(f"    calls:               {r.calls}")
    print(f"    input tokens:        {r.input_tokens:>10,}")
    print(f"    output tokens:       {r.output_tokens:>10,}")
    if r.cache_creation_tokens:
        print(f"    cache creation:      {r.cache_creation_tokens:>10,}")
    if r.cache_read_tokens:
        print(f"    cache read:          {r.cache_read_tokens:>10,}")
    print(f"    total cost (USD):    ${r.total_cost_usd:.4f}")
    print(f"    elapsed:             {r.elapsed_s:.1f}s")
    print(f"    agent turns:         {r.num_turns}")
    if r.intermediate_chars:
        print(f"    intermediate chars:  {r.intermediate_chars:,}")
    print(f"    notes:               {r.notes}")
    if r.error:
        print(f"    ERROR: {r.error}")


def extrapolate(r: StrategyResult, n_sources: int = 60) -> dict:
    return {
        "name": r.name,
        "total_calls": r.calls * n_sources,
        "total_input": r.input_tokens * n_sources,
        "total_output": r.output_tokens * n_sources,
        "total_cost": r.total_cost_usd * n_sources,
        "sequential_minutes": (r.elapsed_s * n_sources) / 60,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    print("=" * 60)
    print("FETCH STRATEGY COMPARISON PoC")
    print("=" * 60)
    print(f"URL:   {TEST_URL}")
    print(f"QUERY: {TEST_QUERY}")

    print("\n--- Running Strategy A (verbatim → extract) ---")
    result_a = await strategy_a_verbatim(TEST_URL, TEST_QUERY)
    print_strategy(result_a)

    print("\n--- Running Strategy B (markdown → extract) ---")
    result_b = await strategy_b_markdown(TEST_URL, TEST_QUERY)
    print_strategy(result_b)

    print("\n--- Running Strategy C (combined) ---")
    result_c = await strategy_c_combined(TEST_URL, TEST_QUERY)
    print_strategy(result_c)

    # Quality comparison — show findings from each
    print("\n" + "=" * 60)
    print("FINDINGS QUALITY COMPARISON")
    print("=" * 60)
    for label, r in [("A", result_a), ("B", result_b), ("C", result_c)]:
        print(f"\n--- {label}: {r.name} ---")
        if r.error:
            print(f"ERROR: {r.error}")
            continue
        text = r.findings_text[:1500]
        print(text)
        # Count findings
        try:
            fenced = _extract_fenced_block(r.findings_text)
            parsed = json.loads(fenced or r.findings_text)
            print(f"\n    [{len(parsed)} findings parsed successfully]")
        except Exception as e:  # noqa: BLE001
            print(f"\n    [JSON parse failed: {e}]")

    # Extrapolation to 60-source thorough preset
    print("\n" + "=" * 60)
    print("EXTRAPOLATED TO THOROUGH PRESET (60 sources, sequential)")
    print("=" * 60)
    print(f"\n  {'strategy':<32} {'calls':>6} {'in_tok':>10} {'out_tok':>10} {'cost':>8} {'time':>8}")
    for r in [result_a, result_b, result_c]:
        if r.error:
            continue
        e = extrapolate(r)
        print(
            f"  {e['name']:<32} {e['total_calls']:>6} "
            f"{e['total_input']:>10,} {e['total_output']:>10,} "
            f"${e['total_cost']:>6.2f} {e['sequential_minutes']:>6.1f}m"
        )

    # Rate limit probe
    await rate_limit_probe(n=10)

    print("\n" + "=" * 60)
    print("DONE — review numbers above to pick a strategy")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
