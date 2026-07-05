"""Benchmark: Find the optimal concurrency level for Claude Agent SDK calls.

Tests concurrency levels 1, 3, 5, 10 against real Claude Max API using
WebSearch. Uses 60s timeout (not the 15s router default) to measure actual
completion time rather than artificial cutoffs.

Run from the scripts dir:
    python3 poc_concurrency_benchmark.py
"""

from __future__ import annotations

import asyncio
import statistics
import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from research_pipeline.env import load_env

load_env()

_CFG = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
_HOOK_SESSION_DIR = _CFG / "hook-sessions"
_HOOK_SESSION_DIR.mkdir(exist_ok=True)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)


TIMEOUT_S = 60.0  # generous — measure real latency, not timeout failures
QUERIES = [
    "Python httpx async client best practices 2026",
    "Django REST framework alternatives 2026",
    "FastAPI dependency injection patterns",
    "Python web scraping trafilatura readability",
    "asyncio semaphore concurrency limiting",
    "Claude API rate limits Max plan",
    "Pydantic v2 model validation performance",
    "Python subprocess management asyncio",
    "web search API free tier comparison 2026",
    "httpx vs aiohttp vs requests performance",
]


async def timed_search(query_text: str, index: int) -> dict:
    """Single WebSearch call with timing."""
    start = time.monotonic()
    stderr_lines: list[str] = []
    options = ClaudeAgentOptions(
        allowed_tools=["WebSearch"],
        max_turns=3,
        permission_mode="bypassPermissions",
        cwd=str(_HOOK_SESSION_DIR),
        setting_sources=[],
        stderr=lambda line: stderr_lines.append(line),
        extra_args={"setting-sources": "", "debug-to-stderr": None},
        env={"_HOOK_CHILD_SESSION": "1"},
    )
    chunks: list[str] = []
    error: str | None = None
    try:
        async for msg in query(
            prompt=f"Use WebSearch to search for: {query_text}\nReturn up to 3 results as a JSON array with url, title, snippet fields.",
            options=options,
        ):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
    except asyncio.CancelledError:
        error = "cancelled"
    except Exception as e:
        captured = "\n".join(stderr_lines[-2000:])
        error = f"{type(e).__name__}: {e}\nCLI stderr:\n{captured}" if captured else f"{type(e).__name__}: {e}"

    elapsed = time.monotonic() - start
    text = "".join(chunks).strip()
    success = error is None and len(text) > 10

    return {
        "index": index,
        "query": query_text[:40],
        "success": success,
        "elapsed": elapsed,
        "error": error,
        "result_chars": len(text),
    }


async def run_level(concurrency: int) -> dict:
    """Run `concurrency` queries concurrently, measure results."""
    queries_to_run = QUERIES[:concurrency]
    print(f"\n  Launching {concurrency} concurrent calls...", flush=True)

    tasks = [
        asyncio.wait_for(
            timed_search(q, i),
            timeout=TIMEOUT_S,
        )
        for i, q in enumerate(queries_to_run)
    ]

    start = time.monotonic()
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    wall_clock = time.monotonic() - start

    results = []
    for i, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            results.append({
                "index": i,
                "query": queries_to_run[i][:40] if i < len(queries_to_run) else "?",
                "success": False,
                "elapsed": TIMEOUT_S,
                "error": f"{type(r).__name__}: {r}",
                "result_chars": 0,
            })
        else:
            results.append(r)

    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    latencies = [r["elapsed"] for r in successes]

    level_result = {
        "concurrency": concurrency,
        "total": len(results),
        "successes": len(successes),
        "failures": len(failures),
        "wall_clock": wall_clock,
        "median_latency": statistics.median(latencies) if latencies else 0,
        "p95_latency": (
            sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else (latencies[0] if latencies else 0)
        ),
        "max_latency": max(latencies) if latencies else 0,
        "error_types": [r["error"] for r in failures if r["error"]],
    }

    # Print per-call details
    for r in sorted(results, key=lambda x: x["index"]):
        status = "OK" if r["success"] else "FAIL"
        err = f" [{r['error']}]" if r["error"] else ""
        print(f"    {r['index']+1:>2}: {status} {r['elapsed']:5.1f}s {r['query']}{err}")

    return level_result


async def main() -> int:
    print("=" * 60)
    print("SDK CONCURRENCY BENCHMARK")
    print("=" * 60)
    print(f"Timeout per call: {TIMEOUT_S}s")
    print(f"Queries available: {len(QUERIES)}")

    levels = [1, 3, 5, 10]
    all_results = []

    for level in levels:
        print(f"\n--- Concurrency level: {level} ---")
        result = await run_level(level)
        all_results.append(result)

        print(f"\n  Results: {result['successes']}/{result['total']} success, "
              f"wall={result['wall_clock']:.1f}s, "
              f"median={result['median_latency']:.1f}s, "
              f"p95={result['p95_latency']:.1f}s, "
              f"max={result['max_latency']:.1f}s")

        if result["failures"]:
            print(f"  Errors: {result['error_types']}")

        # Wait between levels to let rate limits reset
        if level != levels[-1]:
            wait = 15
            print(f"\n  Waiting {wait}s before next level...")
            await asyncio.sleep(wait)

    # Summary table
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"\n  {'N':>3} {'ok/total':>10} {'wall':>7} {'median':>7} {'p95':>7} {'max':>7}")
    for r in all_results:
        print(
            f"  {r['concurrency']:>3} "
            f"{r['successes']}/{r['total']:>8} "
            f"{r['wall_clock']:>6.1f}s "
            f"{r['median_latency']:>6.1f}s "
            f"{r['p95_latency']:>6.1f}s "
            f"{r['max_latency']:>6.1f}s"
        )

    # Recommendation
    print("\n" + "=" * 60)
    print("RECOMMENDATION")
    print("=" * 60)
    # Find the highest concurrency where all calls succeed
    best = 1
    for r in all_results:
        if r["successes"] == r["total"] and r["max_latency"] < 30:
            best = r["concurrency"]
    print(f"  Highest concurrency with 100% success and <30s max latency: {best}")
    print(f"  Suggested MAX_CONCURRENT_SDK_CALLS = {best}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
