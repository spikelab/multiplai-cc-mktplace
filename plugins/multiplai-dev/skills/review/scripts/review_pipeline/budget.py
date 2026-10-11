"""Per-target token/cost ledger with a circuit breaker.

The ledger is `multiplai_core.budget.SpendLedger`: recording never raises,
only `check()` can stop a run, and a resumed run restores what was already
spent. A batch reviews several targets concurrently in one process, and the
ceiling is per target, so the current ledger lives in core's `ContextVar`:
each target's asyncio task records against its own ledger.

This module supplies review's default ceiling and the names the rest of the
pipeline calls (`start`, `get_budget`, `record(label=)`, `check(stage=)`).
"""

from __future__ import annotations

from dataclasses import dataclass

from multiplai_core import budget as _core
from multiplai_core.budget import (  # noqa: F401 — re-exported for the pipeline and its tests
    TOKEN_FIELDS,
    WARN_FRACTION,
    BudgetExceededError,
    SpendLedger,
    empty_stage,
)

# Per target. Past reviews spent $1-11 for up to 18 changed files, and $60 for
# 52; the finders cost about $0.40-0.50 per changed file on the larger ones.
DEFAULT_MAX_USD = 50.0


@dataclass
class ReviewBudget(SpendLedger):
    max_usd: float | None = DEFAULT_MAX_USD
    run_name = "Review"


def get_budget() -> ReviewBudget:
    """The ledger of the target this task is reviewing (created on first use)."""
    return _core.get(ledger_cls=ReviewBudget)  # type: ignore[return-value]


def start(max_usd: float | None = DEFAULT_MAX_USD, state: dict | None = None) -> ReviewBudget:
    """Begin a target's ledger in the current context, restoring *state* if given."""
    return _core.start(max_usd, state, ledger_cls=ReviewBudget)  # type: ignore[return-value]


def record(usage, *, label: str = "") -> None:
    get_budget().record(usage, label=label)


def check(*, stage: str = "") -> None:
    get_budget().check(stage)
