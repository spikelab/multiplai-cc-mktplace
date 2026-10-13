"""Stages: find → verify → merge → repeats → assess.

Each is `async def run_<stage>(state, ctx) -> state`. A stage returns at once
when `state.stage` is already past it, and skips items it already has a
result for, so a resumed run repeats no finished work.

Only `RepoTrustError` and `BudgetExceededError` escape a stage. Any other
agent failure is logged at ERROR and turned into the conservative outcome for
that item (no findings from that finder, `unverifiable`, an unmerged group).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from code_facts import bounded  # noqa: F401  (re-exported for the stages)

from ..config import ReviewConfig
from ..models import Citation, TargetInfo
from ..progress import ProgressWriter

log = logging.getLogger(__name__)


@dataclass
class RunContext:
    config: ReviewConfig
    snapshot: Path  # the head tree the agents read
    diff: str
    progress: ProgressWriter | None = None
    session_id: str = ""
    # `<out>/<repo>/<object>/`, where earlier rounds and the person's decisions are
    # read from (repeats, assess). None in tests that need neither.
    target_dir: Path | None = None
    # Stage-level counts for the `stage` activity event, filled by each stage.
    counts: dict[str, int] = field(default_factory=dict)
    gate_reasons: list[str] = field(default_factory=list)


def relative_path(path: str, target: TargetInfo, snapshot: Path) -> str:
    """A repo-relative path for whatever form the agent wrote."""
    p = path.strip()
    for root in (str(snapshot.resolve()), str(snapshot), target.repo_path):
        root = root.rstrip("/") + "/"
        if p.startswith(root):
            p = p[len(root):]
            break
    while p.startswith("./"):
        p = p[2:]
    return p


def fix_citation(citation: Citation | None, target: TargetInfo, snapshot: Path) -> Citation | None:
    if citation is None:
        return None
    return citation.model_copy(update={"path": relative_path(citation.path, target, snapshot)})
