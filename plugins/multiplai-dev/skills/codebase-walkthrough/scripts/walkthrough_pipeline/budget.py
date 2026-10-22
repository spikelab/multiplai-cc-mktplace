"""Per-walk token/cost ledger with a circuit breaker.

The ledger is `multiplai_core.budget.SpendLedger`: recording never raises,
only `check()` can stop a run, and a resumed run restores what was already
spent. The current ledger lives in core's `ContextVar`, as in review, so a
test or a future batch can run several walks in one process with a ledger
each.

This module supplies the walkthrough's default ceiling and the names the rest
of the pipeline calls (`start`, `get_budget`, `record(label=)`,
`check(stage=)`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from multiplai_core import budget as _core
from multiplai_core.budget import (  # noqa: F401 — re-exported for the pipeline and its tests
    TOKEN_FIELDS,
    WARN_FRACTION,
    BudgetExceededError,
    SpendLedger,
    empty_stage,
)

log = logging.getLogger(__name__)

# The review pipeline's default since multiplai-cc-mktplace PR #250.
DEFAULT_MAX_USD = 50.0


@dataclass
class WalkthroughBudget(SpendLedger):
    max_usd: float | None = DEFAULT_MAX_USD
    run_name = "Walkthrough"


def get_budget() -> WalkthroughBudget:
    """The ledger of the walk this task runs (created on first use)."""
    return _core.get(ledger_cls=WalkthroughBudget)  # type: ignore[return-value]


def start(
    max_usd: float | None = DEFAULT_MAX_USD, state: dict | None = None
) -> WalkthroughBudget:
    """Begin a walk's ledger in the current context, restoring *state* if given."""
    return _core.start(max_usd, state, ledger_cls=WalkthroughBudget)  # type: ignore[return-value]


@dataclass
class ProcessSpend:
    """What this process's calls cost: every `record()`, never a ledger a resume
    restored. `run` and `resume` print it as `SPEND:<usd>:<tokens>` on exit,
    which buildme adds to its own spend when it regenerates a walkthrough."""

    cost_usd: float = 0.0
    tokens: int = 0


_process = ProcessSpend()


def spend_marker() -> str:
    """`SPEND:<usd>:<tokens>`, the line `run` and `resume` print last."""
    return f"SPEND:{_process.cost_usd:.6f}:{_process.tokens}"


def record(usage, *, label: str = "") -> None:
    get_budget().record(usage, label=label)
    try:
        cost = float(getattr(usage, "cost_usd", 0.0) or 0.0)
        tokens = sum(
            int(getattr(usage, name, 0) or 0)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        )
    except Exception as e:  # recording never raises, as the ledger's own does not
        log.warning("Process spend accounting failed for a call (ignored): %s", e)
        return
    _process.cost_usd += cost
    _process.tokens += tokens


def check(*, stage: str = "") -> None:
    get_budget().check(stage)
