"""Agent stages: explore → docs → trace → write.

Each is `async def run_<stage>(state, ctx) -> state`. A stage returns at once
when `state.stage` is already past it, and skips tasks whose answer is already
in the state, so a resumed run repeats no paid work. Each answer is stored in
the state as it returns, so the checkpoint saved at a budget stop keeps it.

Only `RepoTrustError` and `BudgetExceededError` escape a stage. Any other
agent failure is logged, recorded in `state.errors`, and leaves that task's
output empty; the coverage table then shows what is missing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, TypeVar

from ..config import WalkConfig
from ..models import Citation, RepoInfo
from ..progress import ProgressWriter

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class RunContext:
    config: WalkConfig
    run_dir: Path
    snap_root: Path            # <run>/snap: one directory per repo key; every agent's cwd
    repos: dict[str, RepoInfo]
    progress: ProgressWriter | None = None
    session_id: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    gate_reasons: list[str] = field(default_factory=list)


async def bounded(items: Iterable[T], fn: Callable[[T], Awaitable[R]], limit: int) -> list[R]:
    """Run fn over items, at most *limit* at a time, results in input order.

    A TaskGroup, so the first exception that escapes `fn` (trust, budget)
    cancels the rest instead of leaving them spending in the background.
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def guarded(item: T) -> R:
        async with sem:
            return await fn(item)

    items = list(items)
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(guarded(item)) for item in items]
    except* Exception as group:
        raise group.exceptions[0] from None
    return [t.result() for t in tasks]


def ws_path(path: str, snap_root: Path) -> str:
    """A workspace-relative path for whatever form the agent wrote."""
    p = path.strip().strip("`")
    for root in (str(snap_root.resolve()), str(snap_root)):
        root = root.rstrip("/") + "/"
        if p.startswith(root):
            p = p[len(root):]
            break
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def fix_citation(c: Citation, snap_root: Path) -> Citation:
    return c.model_copy(update={"path": ws_path(c.path, snap_root)})


def context_lines(snap_root: Path, path: str, line: int, around: int = 3) -> str:
    """Lines line-around..line+around of a snapshot file, numbered, for a prompt."""
    try:
        lines = (snap_root / path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    lo, hi = max(1, line - around), min(len(lines), line + around)
    return "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(lo, hi + 1))
