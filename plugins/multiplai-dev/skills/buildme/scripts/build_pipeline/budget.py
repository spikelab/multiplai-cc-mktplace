"""Per-build token/cost accounting and circuit-breaker.

Every loop bound in this pipeline is an *iteration count*
(MAX_REVIEW_ITERATIONS, MAX_INTEGRATION_FIX_ATTEMPTS). Iteration counts do not
bound spend: three review iterations over a 150k-char diff with a panel of
reviewers costs an order of magnitude more than three over a small one, and a
runaway review/fix loop is exactly the shape that has burned real money
elsewhere. This module adds the missing axis — a budget the build stops
against, with a diagnosis rather than a silent exhaustion.

Design notes:
- The tracker is a module-level singleton because `llm_call`/`agent_call` are
  called from everywhere and threading a budget object through every call site
  would be a far larger change than the feature warrants. `reset()` makes it
  test-friendly.
- Recording never raises. Accounting must not be able to break a build; only
  the explicit `check()` call, at loop boundaries, can stop one.
- No budget configured → the tracker still accounts (so the run reports what
  it spent) but never stops anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from multiplai_core.costing import TokenCounts

log = logging.getLogger(__name__)

# Fraction of the budget at which the build warns once. Warning earlier is
# pointless (nothing is actionable at 20%); later gives no room to react.
WARN_FRACTION = 0.8


class BudgetExceededError(Exception):
    """Raised when a build has spent its configured budget.

    Carries the diagnosis text the orchestrator surfaces — a bare "budget
    exceeded" tells the user nothing about where the money went.
    """

    def __init__(self, message: str, *, diagnosis: str = "") -> None:
        super().__init__(message)
        self.diagnosis = diagnosis


@dataclass
class BuildBudget:
    """Cumulative usage for one build, against optional ceilings."""

    max_tokens: int | None = None
    max_usd: float | None = None

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    # {label: tokens} — which phase spent it. The whole point of stopping is to
    # say what to fix, and "the review loop" vs "spec generation" is the answer.
    by_label: dict[str, int] = field(default_factory=dict)
    _warned: bool = False

    @property
    def total_tokens(self) -> int:
        """Every token the build was billed for, cache tiers included."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def as_token_counts(self) -> TokenCounts:
        """The ledger's shape, so this reuses `multiplai_core.costing` pricing.

        Undifferentiated cache creation goes to the cheaper 5-minute tier, per
        that module's documented convention (err low, not high).
        """
        return TokenCounts(
            input=self.input_tokens,
            output=self.output_tokens,
            cr=self.cache_read_tokens,
            cw5m=self.cache_creation_tokens,
        )

    def record(self, usage, *, label: str = "") -> None:
        """Add one SDK call's usage. Never raises.

        *usage* is an `AgentUsage` (or anything with the same attributes); a
        run that died before the SDK emitted a ResultMessage reports zeros,
        which is correct — we were not billed for what we never got.
        """
        try:
            self.calls += 1
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            self.cache_read_tokens += int(getattr(usage, "cache_read_tokens", 0) or 0)
            self.cache_creation_tokens += int(getattr(usage, "cache_creation_tokens", 0) or 0)
            self.cost_usd += float(getattr(usage, "cost_usd", 0.0) or 0.0)
            if label:
                spent = (
                    int(getattr(usage, "input_tokens", 0) or 0)
                    + int(getattr(usage, "output_tokens", 0) or 0)
                    + int(getattr(usage, "cache_read_tokens", 0) or 0)
                    + int(getattr(usage, "cache_creation_tokens", 0) or 0)
                )
                self.by_label[label] = self.by_label.get(label, 0) + spent
        except Exception as e:  # pragma: no cover — accounting must never break a build
            log.warning("Budget accounting failed for a call (ignored): %s", e)

    @property
    def fraction_used(self) -> float:
        """Highest fraction consumed across the configured ceilings (0.0 if none)."""
        fractions = []
        if self.max_tokens:
            fractions.append(self.total_tokens / self.max_tokens)
        if self.max_usd:
            fractions.append(self.cost_usd / self.max_usd)
        return max(fractions) if fractions else 0.0

    def diagnosis(self) -> str:
        """Where the build's spend went — the useful half of a stop."""
        lines = [
            f"Budget: {self.total_tokens:,} tokens / ${self.cost_usd:.2f} over {self.calls} SDK calls.",
        ]
        if self.max_tokens:
            lines.append(f"  token ceiling: {self.max_tokens:,}")
        if self.max_usd:
            lines.append(f"  cost ceiling:  ${self.max_usd:.2f}")
        lines.append(
            f"  breakdown: in={self.input_tokens:,} out={self.output_tokens:,} "
            f"cache_read={self.cache_read_tokens:,} cache_write={self.cache_creation_tokens:,}"
        )
        if self.by_label:
            top = sorted(self.by_label.items(), key=lambda kv: -kv[1])
            lines.append("  by phase: " + ", ".join(f"{k}={v:,}" for k, v in top))
        lines.append(
            "  The review/fix loop is the usual culprit: it re-runs the reviewer "
            "AND an implementer per iteration (MAX_REVIEW_ITERATIONS=3, times the "
            "panel size). Lower the panel, tighten the diff cap, or raise the budget."
        )
        return "\n".join(lines)

    def check(self, *, phase: str = "") -> None:
        """Stop the build if a ceiling is spent. Warn once at 80%.

        Called at loop boundaries rather than per call, so a single expensive
        call cannot be interrupted half-way (which would waste what it spent).

        Raises:
            BudgetExceededError: a configured ceiling is at or past 100%.
        """
        if not (self.max_tokens or self.max_usd):
            return
        used = self.fraction_used
        if used >= 1.0:
            where = f" during {phase}" if phase else ""
            log.error("Build budget exhausted%s (%.0f%%)", where, used * 100)
            raise BudgetExceededError(
                f"Build budget exhausted{where} ({used * 100:.0f}% of the configured ceiling)",
                diagnosis=self.diagnosis(),
            )
        if used >= WARN_FRACTION and not self._warned:
            self._warned = True
            log.warning(
                "Build has used %.0f%% of its budget%s — %s",
                used * 100, f" ({phase})" if phase else "", self.diagnosis(),
            )

    def to_state(self) -> dict:
        """Serializable snapshot for the build state file."""
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "by_label": dict(self.by_label),
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
        }

    def load_state(self, data: dict) -> None:
        """Restore cumulative usage from a checkpoint, so a resumed build does
        not get a fresh budget (which would defeat the ceiling entirely)."""
        if not data:
            return
        self.calls = int(data.get("calls", 0) or 0)
        self.input_tokens = int(data.get("input_tokens", 0) or 0)
        self.output_tokens = int(data.get("output_tokens", 0) or 0)
        self.cache_read_tokens = int(data.get("cache_read_tokens", 0) or 0)
        self.cache_creation_tokens = int(data.get("cache_creation_tokens", 0) or 0)
        self.cost_usd = float(data.get("cost_usd", 0.0) or 0.0)
        self.by_label = dict(data.get("by_label", {}) or {})


_budget = BuildBudget()


def get_budget() -> BuildBudget:
    """The current build's budget tracker."""
    return _budget


def configure(*, max_tokens: int | None = None, max_usd: float | None = None) -> BuildBudget:
    """Set the ceilings for this build (None = unlimited on that axis)."""
    _budget.max_tokens = max_tokens
    _budget.max_usd = max_usd
    _budget._warned = False
    if max_tokens or max_usd:
        log.info("Build budget configured: max_tokens=%s max_usd=%s", max_tokens, max_usd)
    return _budget


def reset() -> BuildBudget:
    """Clear all accounting and ceilings (test seam / new build)."""
    global _budget
    _budget = BuildBudget()
    return _budget


def record(usage, *, label: str = "") -> None:
    """Record one SDK call's usage against the current build."""
    _budget.record(usage, label=label)


def check(*, phase: str = "") -> None:
    """Enforce the current build's budget. See :meth:`BuildBudget.check`."""
    _budget.check(phase=phase)
