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

import logging
from dataclasses import dataclass, field
from pathlib import Path

from code_facts import bounded, ws_path  # noqa: F401  (re-exported for the stages)

from ..config import WalkConfig
from ..models import Citation, RepoInfo
from ..progress import ProgressWriter

log = logging.getLogger(__name__)


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
