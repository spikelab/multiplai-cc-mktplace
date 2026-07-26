"""Tests for the per-build token/cost circuit-breaker."""

from dataclasses import dataclass

import pytest

from build_pipeline import budget as budget_mod
from build_pipeline.budget import BudgetExceededError, BuildBudget


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


@pytest.fixture(autouse=True)
def _clean_budget():
    budget_mod.reset()
    yield
    budget_mod.reset()


class TestAccounting:
    def test_total_counts_every_tier(self):
        b = BuildBudget()
        b.record(FakeUsage(input_tokens=10, output_tokens=5,
                           cache_read_tokens=100, cache_creation_tokens=2))
        assert b.total_tokens == 117
        assert b.calls == 1

    def test_labels_attribute_spend_to_a_phase(self):
        b = BuildBudget()
        b.record(FakeUsage(input_tokens=10), label="review")
        b.record(FakeUsage(input_tokens=5), label="review")
        b.record(FakeUsage(input_tokens=1), label="implementer")
        assert b.by_label == {"review": 15, "implementer": 1}

    def test_recording_never_raises(self):
        """Accounting must not be able to break a build."""
        b = BuildBudget()
        b.record(object())  # no usage attributes at all
        b.record(None)
        assert b.calls == 2

    def test_unbilled_run_records_zeros(self):
        b = BuildBudget()
        b.record(FakeUsage())
        assert b.total_tokens == 0


class TestCheck:
    def test_no_ceiling_never_stops(self):
        b = BuildBudget()
        b.record(FakeUsage(input_tokens=10_000_000))
        b.check(phase="anything")  # must not raise

    def test_stops_at_token_ceiling(self):
        b = BuildBudget(max_tokens=1000)
        b.record(FakeUsage(input_tokens=1000))
        with pytest.raises(BudgetExceededError) as exc:
            b.check(phase="block 2")
        assert "block 2" in str(exc.value)

    def test_stops_at_cost_ceiling(self):
        b = BuildBudget(max_usd=1.0)
        b.record(FakeUsage(cost_usd=1.5))
        with pytest.raises(BudgetExceededError):
            b.check()

    def test_under_ceiling_passes(self):
        b = BuildBudget(max_tokens=1000)
        b.record(FakeUsage(input_tokens=999))
        b.check()

    def test_diagnosis_names_the_top_phase(self):
        b = BuildBudget(max_tokens=100)
        b.record(FakeUsage(input_tokens=90), label="review")
        b.record(FakeUsage(input_tokens=20), label="rubric")
        with pytest.raises(BudgetExceededError) as exc:
            b.check()
        # The useful half of a stop is where the money went.
        assert "review=90" in exc.value.diagnosis
        assert exc.value.diagnosis.index("review=90") < exc.value.diagnosis.index("rubric=20")

    def test_warns_once_at_80_percent(self, caplog):
        b = BuildBudget(max_tokens=100)
        b.record(FakeUsage(input_tokens=85))
        with caplog.at_level("WARNING"):
            b.check()
            b.check()
        assert sum("used 85% of its budget" in r.getMessage()
                   for r in caplog.records if r.levelname == "WARNING") == 1


class TestPersistence:
    def test_round_trip_preserves_spend(self):
        b = BuildBudget(max_tokens=500)
        b.record(FakeUsage(input_tokens=100, output_tokens=20, cost_usd=0.5), label="review")

        restored = BuildBudget(max_tokens=500)
        restored.load_state(b.to_state())
        assert restored.total_tokens == 120
        assert restored.cost_usd == pytest.approx(0.5)
        assert restored.by_label == {"review": 100 + 20}

    def test_resume_keeps_the_ceiling_binding(self):
        """A resumed build inherits its spend — otherwise the ceiling is a no-op
        that any crash-loop resets."""
        first = BuildBudget(max_tokens=100)
        first.record(FakeUsage(input_tokens=100))

        resumed = BuildBudget(max_tokens=100)
        resumed.load_state(first.to_state())
        with pytest.raises(BudgetExceededError):
            resumed.check()

    def test_load_state_ignores_empty(self):
        b = BuildBudget()
        b.record(FakeUsage(input_tokens=7))
        b.load_state({})
        assert b.total_tokens == 7


class TestModuleSingleton:
    def test_configure_then_record_then_check(self):
        budget_mod.configure(max_tokens=50)
        budget_mod.record(FakeUsage(input_tokens=60), label="x")
        with pytest.raises(BudgetExceededError):
            budget_mod.check()

    def test_reset_clears_ceilings_and_spend(self):
        budget_mod.configure(max_tokens=50)
        budget_mod.record(FakeUsage(input_tokens=60))
        budget_mod.reset()
        budget_mod.check()  # no ceiling, no spend → no raise
        assert budget_mod.get_budget().total_tokens == 0
