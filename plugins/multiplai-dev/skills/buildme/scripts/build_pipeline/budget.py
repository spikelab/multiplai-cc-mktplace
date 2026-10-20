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
  admission checks before provider calls can stop one.
- No budget configured → the tracker still accounts (so the run reports what
  it spent) but never stops anything.
- Beside the build's tracker, a process total (`ProcessSpend`) adds up every
  recorded call for the life of the process, and the `SPEND:` line of each
  model-calling skill it runs (`add_process_spend`, from
  `git_ops._count_skill_spend`; a skill that printed none makes it
  incomplete). Neither `reset()` (for a new run) nor `BuildBudget.load_state`
  (which a resumed engine runs) touches it, so `SPEND:<usd>:<tokens>`
  (`spend_marker`), printed by `build` and `findings` on exit, is what this
  process spent: an epic adds it up across attempts and rounds without
  counting a resumed build's earlier spend twice.
- This is not `multiplai_core.budget.SpendLedger`, which review and the
  walkthrough use: buildme's `by_label` counts tokens (USD per label is
  `usd_by_label`), its diagnosis ranks phases by tokens, and its saved state
  carries `total_tokens`. Moving onto the shared ledger would change those,
  and four tests assert them.
- The 80% warning goes to the log, to stdout as `BUDGET:WARN:<percent>:<usd>`,
  and to the progress file `warn_into` names, as a `BUDGET_WARNING` section.
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
    # {label: USD} — the same split in money, which is what a user decides on.
    usd_by_label: dict[str, float] = field(default_factory=dict)
    unaccounted_calls: list[str] = field(default_factory=list)
    accepted_unknown_calls: int = 0
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
                self.usd_by_label[label] = (
                    self.usd_by_label.get(label, 0.0)
                    + float(getattr(usage, "cost_usd", 0.0) or 0.0)
                )
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

    def diagnosis(self, *, advice: bool = True) -> str:
        """Where the build's spend went — the useful half of a stop.

        ``advice=False`` leaves out the advice on cutting spend, for the
        summary every run ends with, where nothing was stopped."""
        lines = [
            f"Budget: {self.total_tokens:,} tokens / ${self.cost_usd:.2f} over {self.calls} SDK calls.",
        ]
        if self.unaccounted_calls:
            lines.append("  unknown usage for interrupted calls: " + ", ".join(self.unaccounted_calls))
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
            lines.append("  by phase: " + ", ".join(
                f"{k}={v:,} tokens/${self.usd_by_label.get(k, 0.0):.2f}" for k, v in top))
        if advice:
            lines.append(
                "  The review/fix loop is the usual culprit: it re-runs the reviewer "
                "AND an implementer per iteration (MAX_REVIEW_ITERATIONS=3, times the "
                "panel size). Lower the panel, tighten the diff cap, or raise the budget."
            )
        return "\n".join(lines)

    def check(self, *, phase: str = "") -> None:
        """Stop the build if a ceiling is spent. Warn once at 80%.

        Called before provider dispatch inside the concurrency slot. An admitted
        call may finish above the ceiling; the next call stops, even mid-block.

        Raises:
            BudgetExceededError: a configured ceiling is at or past 100%.
        """
        if not (self.max_tokens or self.max_usd):
            return
        if len(self.unaccounted_calls) > self.accepted_unknown_calls:
            raise BudgetExceededError(
                "Interrupted provider usage is unknown; resume with --accept-unknown-spend to accept the missing usage. Totals remain lower bounds and the cap covers known spend only",
                diagnosis=self.diagnosis(),
            )
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
            where = f" ({phase})" if phase else ""
            diagnosis = self.diagnosis()
            log.warning("Build has used %.0f%% of its budget%s — %s", used * 100, where, diagnosis)
            print(f"BUDGET:WARN:{used * 100:.0f}:{self.cost_usd:.2f}", flush=True)
            if _warn_progress is not None:
                try:
                    _warn_progress.log_phase(
                        "BUDGET_WARNING",
                        f"{used * 100:.0f}% of the budget used{where}.\n{diagnosis}",
                    )
                except OSError as e:
                    log.warning("Could not write the budget warning to the progress file: %s", e)

    def to_state(self) -> dict:
        """Serializable snapshot for the build state file."""
        return {
            "calls": self.calls,
            "unaccounted_calls": list(self.unaccounted_calls),
            "accepted_unknown_calls": self.accepted_unknown_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "by_label": dict(self.by_label),
            "usd_by_label": {k: round(v, 6) for k, v in self.usd_by_label.items()},
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
        }

    def load_state(self, data: dict, *, accept_unknown_spend: bool = False) -> None:
        """Restore cumulative usage from a checkpoint, so a resumed build does
        not get a fresh budget (which would defeat the ceiling entirely)."""
        if not data:
            return
        self.calls = int(data.get("calls", 0) or 0)
        self.unaccounted_calls = list(data.get("unaccounted_calls", []))
        self.accepted_unknown_calls = min(len(self.unaccounted_calls), int(data.get("accepted_unknown_calls", 0)))
        if accept_unknown_spend and self.unaccounted_calls:
            self.accepted_unknown_calls = len(self.unaccounted_calls)
            log.warning("Accepted %d interrupted calls with unknown spend; totals remain lower bounds",
                        self.accepted_unknown_calls)
        self.input_tokens = int(data.get("input_tokens", 0) or 0)
        self.output_tokens = int(data.get("output_tokens", 0) or 0)
        self.cache_read_tokens = int(data.get("cache_read_tokens", 0) or 0)
        self.cache_creation_tokens = int(data.get("cache_creation_tokens", 0) or 0)
        self.cost_usd = float(data.get("cost_usd", 0.0) or 0.0)
        self.by_label = dict(data.get("by_label", {}) or {})
        self.usd_by_label = {
            k: float(v or 0.0) for k, v in (data.get("usd_by_label", {}) or {}).items()
        }


@dataclass
class ProcessSpend:
    """What this process has spent: every `record()` since it started, plus the
    `SPEND:` lines of the skills it ran (`add_process_spend`). `complete` is
    False once a skill that calls a model ended with no such line."""

    cost_usd: float = 0.0
    tokens: int = 0
    complete: bool = True


_budget = BuildBudget()
_process = ProcessSpend()
# The ProgressWriter the 80% warning is also written to (`warn_into`).
_warn_progress = None


def warn_into(progress) -> None:
    """Write the 80% budget warning into *progress* (a `ProgressWriter`) too.

    The TDD engine names its progress file at start; checks run from there."""
    global _warn_progress
    _warn_progress = progress


def process_spend() -> ProcessSpend:
    """This process's total spend, which `reset()` and `load_state` leave alone."""
    return _process


def spend_marker() -> str:
    """`SPEND:<usd>:<tokens>`, the line `build` and `findings` print on exit,
    with `:incomplete` after it when a skill's spend is unknown."""
    tail = "" if _process.complete else ":incomplete"
    return f"SPEND:{_process.cost_usd:.6f}:{_process.tokens}{tail}"


def add_process_spend(cost_usd: float, tokens: int) -> None:
    """Add what a skill subprocess spent (its own `SPEND:` line) to the process total.
    It is not added to the build's tracker, whose ceiling covers buildme's own calls."""
    _process.cost_usd += cost_usd
    _process.tokens += tokens


def process_spend_unknown(why: str) -> None:
    """A skill that calls a model ended without saying what it spent."""
    log.warning("Process spend is incomplete: %s", why)
    _process.complete = False


def get_budget() -> BuildBudget:
    """The current build's budget tracker."""
    return _budget


def configure(*, max_tokens: int | None = None, max_usd: float | None = None) -> BuildBudget:
    """Set the ceilings for this build (None = unlimited on that axis).

    Ceilings only — the counters are NOT cleared, so a second build in the same
    process inherits the first one's spend and can trip the breaker early. Call
    `reset()` for that (the CLI runs one build per process, so configure() is
    enough there).
    """
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
    """Record one SDK call's usage against the current build and the process total."""
    _budget.record(usage, label=label)
    try:
        _process.cost_usd += float(getattr(usage, "cost_usd", 0.0) or 0.0)
        _process.tokens += sum(int(getattr(usage, name, 0) or 0) for name in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
    except Exception as e:  # pragma: no cover — accounting must never break a build
        log.warning("Process spend accounting failed for a call (ignored): %s", e)


def record_unaccounted(label: str) -> None:
    """A provider stopped without usage evidence; totals are a lower bound."""
    _budget.unaccounted_calls.append(label)
    _process.complete = False
    log.warning("Provider usage unavailable after interruption: %s", label)


def check(*, phase: str = "") -> None:
    """Enforce the current build's budget. See :meth:`BuildBudget.check`."""
    _budget.check(phase=phase)
