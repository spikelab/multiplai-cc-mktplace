"""Sizing and chunk planning for dream's consolidation pass — pure code, no LLM.

Dream used to hand its whole backlog to one model call and hope it fit. Measured
from ``dream*.log``, it doesn't: emission is flat at 35–41 bytes of proposal per
second whatever the input size, and a proposal is ~45% of the learnings that
produced it, so

    wall clock ≈ learnings_bytes / 85

Against a per-attempt cap that is a fixed number of seconds, that makes failure
arithmetic rather than luck — 187 KB and 249 KB backlogs both burned two full
1800 s attempts and produced nothing. This module inverts the relation: the
chunk budget is *derived from* the timeout instead of hoped to fit under it, so
"will it finish?" has an answer before anything is spent.

Two properties carry the design.

**Chunk boundaries fall only between blocks.** A ``## Session Learnings`` record
is the indivisible unit. Splitting inside one would hand the model half a lesson
and half a provenance range.

**Line numbers are the originals.** :func:`render_chunk` reproduces the slice of
``dream.py::_read_all_learnings`` that covers a chunk's blocks — same
``### File: <name>`` headers, same ``N: `` prefixes carrying each line's ORIGINAL
1-indexed position in its source file, same ``\\n\\n---\\n\\n`` between files.
Renumbering per chunk would silently corrupt every ``**Source:** file:line``
line the model writes from it, and a corrupted citation is undetectable at
review time — it still *looks* like a citation.

Nothing here performs I/O or reads state; the caller supplies the observed
throughput (an EWMA it keeps in ``dream_state.yaml``) and the per-call timeout.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Sequence

from lib.learnings_ledger import Block

logger = logging.getLogger(__name__)

# 38 B/s of emitted proposal ÷ a 0.45 proposal-to-learnings ratio. Both numbers
# come from the log table in the plan, and both were flat across a 12x range of
# input sizes — which is why a single constant is honest here.
DEFAULT_THROUGHPUT_BYTES_PER_S = 85.0

# Below the floor the per-call overhead (subprocess spawn, system prompt, memory
# domain block) dominates the useful payload; above the ceiling a single chunk
# gets large enough that one failure costs too much work. The clamp matters more
# than its exact endpoints.
MIN_CHUNK_BYTES = 8_000
MAX_CHUNK_BYTES = 40_000

# Escalation ceiling for one oversized indivisible block. Matches the SDK's own
# hard per-attempt cap: raising past it buys nothing, the call dies anyway.
MAX_ESCALATED_TIMEOUT_S = 1800.0

# Aim a chunk at 40% of the timeout, not 100%. The estimate is a mean over noisy
# calls; a chunk sized to exactly fill the budget times out half the time, and a
# timeout costs the whole chunk plus a retry.
BUDGET_FRACTION = 0.4

_THROUGHPUT_ENV = "MULTIPLAI_DREAM_THROUGHPUT"

_FILE_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class Chunk:
    """One model call's worth of learnings blocks.

    ``timeout_s`` is per-chunk rather than global because of ``oversized``: a
    single block that cannot fit the normal budget gets a doubled timeout for
    its own call only (via ``query(timeout_s=…)``), leaving every sibling chunk
    on the standard one. Patching the module-level default instead would race
    under ``asyncio.gather``.
    """

    index: int
    blocks: tuple[Block, ...]
    n_bytes: int
    timeout_s: float
    oversized: bool = False


# ---------------------------------------------------------------------------
# Throughput and estimates
# ---------------------------------------------------------------------------

def resolve_throughput(observed: float | None = None) -> float:
    """Bytes of learnings consumable per second: env > observed > default.

    The env var is read at CALL time, never captured at import: dream's own
    tests and a human debugging a slow run both set it after this module is
    already loaded, and an import-time read would silently ignore them.

    A non-positive or unparseable value is ignored rather than fatal — a typo in
    a tuning knob must not stop the backlog from being consolidated.
    """
    raw = os.environ.get(_THROUGHPUT_ENV)
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("%s=%r is not a number — ignoring", _THROUGHPUT_ENV, raw)
        else:
            if value > 0:
                return value
            logger.warning("%s=%r is not positive — ignoring", _THROUGHPUT_ENV, raw)
    if observed is not None and observed > 0:
        return observed
    return DEFAULT_THROUGHPUT_BYTES_PER_S


def estimate_seconds(n_bytes: int, throughput: float | None = None) -> float:
    """Predicted wall clock for a call carrying *n_bytes* of learnings."""
    return max(0, n_bytes) / resolve_throughput(throughput)


def chunk_budget_bytes(timeout_s: float, throughput: float | None = None) -> int:
    """Largest chunk that should comfortably finish inside *timeout_s*.

    ``throughput × timeout × 0.4``, clamped to ``[MIN_CHUNK_BYTES,
    MAX_CHUNK_BYTES]``. At the 900 s timeout dream uses that is ~30 KB.
    """
    raw = resolve_throughput(throughput) * max(0.0, timeout_s) * BUDGET_FRACTION
    return int(min(MAX_CHUNK_BYTES, max(MIN_CHUNK_BYTES, raw)))


# ---------------------------------------------------------------------------
# Rendering — must match dream.py::_read_all_learnings byte for byte
# ---------------------------------------------------------------------------

def _file_header(file_name: str) -> str:
    return f"### File: {file_name}\n\n"


def _numbered(block: Block) -> str:
    """The block's lines, each prefixed with its ORIGINAL 1-indexed file line.

    ``_read_all_learnings`` numbers from 1 over the whole file; a block starting
    at line 42 therefore renders as ``42: …``. This is the function whose output
    the model's ``**Source:** file:line`` citations are read off.
    """
    return "\n".join(
        f"{n}: {line}"
        for n, line in enumerate(block.text.splitlines(), start=block.start_line)
    )


def _render_blocks(blocks: Sequence[Block]) -> str:
    """Render blocks grouped by consecutive source file.

    Blocks from the same file concatenate directly: the result is exactly the
    numbered lines of those blocks, nothing invented between them. The gap in
    the numbering *is* the signal that intervening lines (the ``---`` rule and
    its blank lines) were not included.
    """
    if not blocks:
        return ""
    parts: list[str] = []
    group: list[Block] = []

    def flush() -> None:
        if group:
            parts.append(_file_header(group[0].file) + "\n".join(_numbered(b) for b in group))

    for b in blocks:
        if group and group[-1].file != b.file:
            flush()
            group.clear()
        group.append(b)
    flush()
    return _FILE_SEPARATOR.join(parts)


def render_chunk(chunk: Chunk) -> str:
    """The user-message payload for one chunk's model call."""
    return _render_blocks(chunk.blocks)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _solo_bytes(block: Block) -> int:
    """Rendered size of *block* alone in a fresh chunk."""
    return len((_file_header(block.file) + _numbered(block)).encode("utf-8"))


def _append_cost(block: Block, current: list[Block]) -> int:
    """Bytes :func:`_render_blocks` grows by when *block* joins *current*.

    Exact by construction — the three cases mirror the three joins in
    :func:`_render_blocks` — so the running total never drifts from the
    rendered length.
    """
    body = len(_numbered(block).encode("utf-8"))
    if not current:
        return len(_file_header(block.file).encode("utf-8")) + body
    if current[-1].file == block.file:
        return len("\n") + body
    return (
        len(_FILE_SEPARATOR.encode("utf-8"))
        + len(_file_header(block.file).encode("utf-8"))
        + body
    )


def plan_chunks(
    blocks: Sequence[Block],
    timeout_s: float,
    throughput: float | None = None,
) -> list[Chunk]:
    """Pack *blocks* into chunks that each fit inside *timeout_s*.

    Greedy, order-preserving, and split only at block boundaries. A new chunk
    also starts whenever the source file changes: same-day learnings staying
    together is what lets one call dedup a lesson repeated across a day's
    sessions, and mixing files would scatter those repeats across calls where
    nothing can see them together.

    A single block larger than the whole budget is indivisible, so it gets a
    chunk of its own with ``oversized=True`` and a doubled (capped) timeout
    rather than being dropped or split.
    """
    budget = chunk_budget_bytes(timeout_s, throughput)
    escalated = min(2.0 * timeout_s, MAX_ESCALATED_TIMEOUT_S)

    chunks: list[Chunk] = []
    current: list[Block] = []
    current_bytes = 0

    def flush(*, oversized: bool = False) -> None:
        nonlocal current, current_bytes
        if not current:
            return
        packed = tuple(current)
        chunks.append(
            Chunk(
                index=len(chunks) + 1,
                blocks=packed,
                n_bytes=len(_render_blocks(packed).encode("utf-8")),
                timeout_s=escalated if oversized else float(timeout_s),
                oversized=oversized,
            )
        )
        current = []
        current_bytes = 0

    for block in blocks:
        if _solo_bytes(block) > budget:
            flush()
            current = [block]
            current_bytes = _solo_bytes(block)
            logger.warning(
                "Learnings block %s:%d is %d bytes, over the %d-byte chunk budget — "
                "own chunk, timeout raised to %.0fs",
                block.file, block.start_line, current_bytes, budget, escalated,
            )
            flush(oversized=True)
            continue

        cost = _append_cost(block, current)
        if current and (current[-1].file != block.file or current_bytes + cost > budget):
            flush()
            cost = _append_cost(block, current)
        current.append(block)
        current_bytes += cost

    flush()
    return chunks


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def estimate_wall_clock(
    chunks: Sequence[Chunk], concurrency: int, throughput: float | None = None
) -> float:
    """Predicted wall clock for running *chunks* ``concurrency``-at-a-time.

    A wave costs its **slowest** chunk, not the sum of its chunks — summing
    would report a serial run's cost for a parallel one and make the whole
    estimate useless for deciding whether to start.
    """
    n = max(1, int(concurrency))
    total = 0.0
    for start in range(0, len(chunks), n):
        wave = chunks[start:start + n]
        total += max(estimate_seconds(c.n_bytes, throughput) for c in wave)
    return total


def format_plan_line(
    *,
    new_bytes: int,
    total_bytes: int,
    chunks: Sequence[Chunk],
    concurrency: int,
    throughput: float,
) -> str:
    """The one line dream logs and prints before spending anything.

    Everything a human needs to decide "let it run" or "^C": how much is new
    against how much exists, how it was cut up, and how long that will take.
    """
    n = max(1, int(concurrency))
    waves = math.ceil(len(chunks) / n) if chunks else 0
    eta = estimate_wall_clock(chunks, n, throughput)
    oversized = sum(1 for c in chunks if c.oversized)
    note = f", {oversized} oversized" if oversized else ""
    return (
        f"Dream plan: {new_bytes:,} new bytes of {total_bytes:,} total · "
        f"{len(chunks)} chunk(s){note} · concurrency {n} · {waves} wave(s) · "
        f"~{throughput:.0f} B/s · est. {_fmt_duration(eta)}"
    )
