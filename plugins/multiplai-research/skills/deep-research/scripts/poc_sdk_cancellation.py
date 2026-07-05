"""PoC: Verify asyncio.wait_for can cancel a hung Claude SDK query.

This is a one-off validation script (not part of the test suite) that answers:

  "Can we reliably kill a Claude Code SDK query that's blocked on WebFetch?"

If the answer is YES, we can build ClaudeAgentSearchProvider and
ClaudeAgentFetcher that use the SDK instead of external HTTP APIs,
leveraging the user's Claude Max subscription for unlimited web operations.

Three scenarios:

1. NORMAL — SDK query with WebFetch to a fast URL. Should complete
   successfully and return real content.

2. HANG — SDK query with WebFetch to a URL that blocks for 300 seconds.
   We wrap the call in asyncio.wait_for(timeout=8). Expected outcome:
   Python coroutine raises TimeoutError at ~8s.

3. RECOVERY — Normal SDK query after a timeout. Verifies that whatever
   cleanup (or lack thereof) happened in scenario 2 didn't leave the
   system in a broken state.

Run from the scripts dir:
    python3 poc_sdk_cancellation.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
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
    TextBlock,
    query,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def sdk_call(prompt: str, *, allowed_tools: list[str], max_turns: int = 3) -> str:
    """Single SDK query. Returns concatenated assistant text."""
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
    chunks: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
    except Exception as e:
        captured = "\n".join(stderr_lines[-2000:])
        if captured:
            raise RuntimeError(f"{type(e).__name__}: {e}\nCLI stderr:\n{captured}") from e
        raise
    return "".join(chunks).strip()


def count_child_processes() -> int:
    """Count Claude Code child processes of THIS python process."""
    try:
        result = subprocess.check_output(
            ["pgrep", "-P", str(__import__("os").getpid())],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return len([line for line in result.split() if line.strip()])
    except subprocess.CalledProcessError:
        return 0


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_normal() -> dict:
    """Normal fetch — should complete and return content."""
    print("\n=== SCENARIO 1: Normal WebFetch ===")
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            sdk_call(
                "Use WebFetch on https://httpbin.org/json to retrieve the JSON. "
                "Return exactly the value of the 'title' field in slideshow, "
                "nothing else.",
                allowed_tools=["WebFetch"],
                max_turns=3,
            ),
            timeout=90.0,
        )
        elapsed = time.monotonic() - start
        print(f"  elapsed: {elapsed:.1f}s")
        print(f"  response (first 200 chars): {result[:200]!r}")
        passed = len(result) > 0
        return {"passed": passed, "elapsed": elapsed, "response": result[:200]}
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - start
        print(f"  FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
        return {"passed": False, "elapsed": elapsed, "error": repr(e)}


async def scenario_hang() -> dict:
    """Hang test — should be cancelled by wait_for."""
    print("\n=== SCENARIO 2: Hanging WebFetch (8s timeout) ===")
    procs_before = count_child_processes()
    print(f"  child processes before: {procs_before}")

    start = time.monotonic()
    outcome: dict = {"elapsed": None, "exception": None}
    try:
        await asyncio.wait_for(
            sdk_call(
                "Use WebFetch on https://httpbin.org/delay/300. "
                "Return whatever the response body is.",
                allowed_tools=["WebFetch"],
                max_turns=3,
            ),
            timeout=8.0,
        )
        outcome["elapsed"] = time.monotonic() - start
        outcome["exception"] = "none (UNEXPECTED — call returned)"
        print(f"  UNEXPECTED: call returned after {outcome['elapsed']:.1f}s")
    except asyncio.TimeoutError:
        outcome["elapsed"] = time.monotonic() - start
        outcome["exception"] = "TimeoutError"
        print(f"  TimeoutError raised after {outcome['elapsed']:.1f}s (expected ~8s)")
    except Exception as e:  # noqa: BLE001
        outcome["elapsed"] = time.monotonic() - start
        outcome["exception"] = f"{type(e).__name__}: {e}"
        print(f"  OTHER exception after {outcome['elapsed']:.1f}s: {type(e).__name__}: {e}")

    # Give cleanup a moment
    await asyncio.sleep(3)
    procs_after = count_child_processes()
    print(f"  child processes 3s after timeout: {procs_after}")

    leaked = max(0, procs_after - procs_before)
    outcome["procs_leaked"] = leaked
    outcome["passed"] = (
        outcome["exception"] == "TimeoutError"
        and outcome["elapsed"] is not None
        and outcome["elapsed"] < 15.0  # should be ~8s, give some slack
    )
    return outcome


async def scenario_recovery() -> dict:
    """Recovery — normal call after a timeout should still work."""
    print("\n=== SCENARIO 3: Recovery after timeout ===")
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            sdk_call(
                "Respond with exactly the phrase: RECOVERY_OK",
                allowed_tools=[],
                max_turns=1,
            ),
            timeout=60.0,
        )
        elapsed = time.monotonic() - start
        print(f"  elapsed: {elapsed:.1f}s")
        print(f"  response: {result[:200]!r}")
        passed = "RECOVERY_OK" in result or len(result) > 0
        return {"passed": passed, "elapsed": elapsed, "response": result[:200]}
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - start
        print(f"  FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
        return {"passed": False, "elapsed": elapsed, "error": repr(e)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    print("=" * 60)
    print("SDK CANCELLATION PoC")
    print("=" * 60)
    print(f"python PID: {__import__('os').getpid()}")
    print(f"initial child processes: {count_child_processes()}")

    results = {
        "normal": await scenario_normal(),
        "hang": await scenario_hang(),
        "recovery": await scenario_recovery(),
    }

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    for name, r in results.items():
        status = "PASS" if r.get("passed") else "FAIL"
        print(f"  {name}: {status}")
        if not r.get("passed"):
            for k, v in r.items():
                if k != "passed":
                    print(f"    {k}: {v}")

    all_pass = all(r.get("passed") for r in results.values())
    leaked = results["hang"].get("procs_leaked", 0)

    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    if all_pass and leaked == 0:
        print("GREEN — asyncio.wait_for reliably cancels hung SDK queries.")
        print("  Build ClaudeAgentSearchProvider / ClaudeAgentFetcher.")
    elif all_pass and leaked > 0:
        print("YELLOW — cancellation works but leaks subprocess(es).")
        print(f"  Leaked {leaked} child process(es). Investigate cleanup.")
        print("  Still viable but needs subprocess tracking.")
    else:
        print("RED — cancellation path has problems.")
        print("  DO NOT build Claude-backed providers without fixing this.")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
