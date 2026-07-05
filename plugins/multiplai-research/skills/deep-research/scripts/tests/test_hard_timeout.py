"""Tests for sdk.hard_timeout — the wedged-subprocess hang fix.

The bug: asyncio.wait_for, on timeout, cancels the inner coroutine AND awaits
the cancellation. If cleanup blocks (a wedged claude CLI subprocess whose
transport teardown never finishes), wait_for hangs forever. hard_timeout must
return on timeout regardless of whether the inner task can actually be cancelled.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from research_pipeline import sdk


@pytest.mark.asyncio
async def test_hard_timeout_returns_value_when_coro_completes():
    async def quick():
        await asyncio.sleep(0.01)
        return "done"

    assert await sdk.hard_timeout(quick(), 1.0) == "done"


@pytest.mark.asyncio
async def test_hard_timeout_propagates_inner_exception():
    async def boom():
        raise ValueError("inner failure")

    with pytest.raises(ValueError, match="inner failure"):
        await sdk.hard_timeout(boom(), 1.0)


@pytest.mark.asyncio
async def test_hard_timeout_raises_timeout_on_slow_coro():
    async def slow():
        await asyncio.sleep(10)

    with pytest.raises(asyncio.TimeoutError):
        await sdk.hard_timeout(slow(), 0.1)


@pytest.mark.asyncio
async def test_hard_timeout_returns_even_when_cancellation_is_blocked():
    """THE regression test. This coroutine refuses to die when cancelled —
    it swallows CancelledError and keeps sleeping, simulating a wedged
    subprocess whose teardown never completes. asyncio.wait_for would hang
    here for ~30s; hard_timeout must return within the timeout window.
    """
    started = asyncio.Event()

    async def uncancellable():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # Ignore the cancellation and keep blocking — the wedge.
            await asyncio.sleep(30)
            raise

    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await sdk.hard_timeout(uncancellable(), 0.1)
    elapsed = time.monotonic() - start

    # The whole point: we returned promptly, NOT after the inner 30s sleeps.
    assert elapsed < 2.0, f"hard_timeout blocked on cancellation ({elapsed:.1f}s)"
    assert started.is_set()
