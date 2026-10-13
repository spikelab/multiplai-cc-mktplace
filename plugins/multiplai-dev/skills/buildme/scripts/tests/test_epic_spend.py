"""Epic mode, PR B2: report and cap spend (plan-buildme-epic-mode-2026-10-12.md,
items 18-20).

`build` and `findings` print `SPEND:<usd>:<tokens>` from a process total that
neither `budget.reset()` nor `BuildBudget.load_state` touches; the coordinator
adds up each record's `spend` and the review spend in the ledger, and stops
launching at `epic.max_usd` with `EPIC:BUDGET:<spent>` and exit 3."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from build_pipeline import __main__ as cli
from build_pipeline import budget
from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records, spend
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    approve,
    child,
    make_build,
    make_project,
    run_coordinator,
    use_workspace,
)

INDEPENDENT = EpicSplitAnswer(single=False, reason="Three parts.",
                              children=[child("a"), child("b"), child("c")])


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """Each test is a new process: nothing spent yet."""
    monkeypatch.setattr(budget, "_process", budget.ProcessSpend())
    budget.reset()
    yield
    budget.reset()
    epic_lock.release_all()


def _usage(usd: float, tokens: int = 0) -> SimpleNamespace:
    return SimpleNamespace(cost_usd=usd, input_tokens=tokens, output_tokens=0,
                           cache_read_tokens=0, cache_creation_tokens=0)


def _spend_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("SPEND:")]


# --- item 18: the process total and the SPEND line ---------------------------------

class TestProcessTotal:
    def test_record_adds_to_the_process_total(self):
        budget.record(_usage(0.25, 100))
        budget.record(_usage(0.5, 20))
        assert budget.process_spend().cost_usd == pytest.approx(0.75)
        assert budget.process_spend().tokens == 120

    def test_reset_does_not_touch_it(self):
        budget.record(_usage(1.0, 10))
        budget.reset()
        budget.record(_usage(2.0, 5))
        assert budget.get_budget().cost_usd == pytest.approx(2.0)
        assert budget.process_spend().cost_usd == pytest.approx(3.0)
        assert budget.process_spend().tokens == 15

    def test_load_state_does_not_touch_it(self):
        budget.get_budget().load_state({"cost_usd": 5.0, "input_tokens": 1000, "calls": 3})
        budget.record(_usage(1.0, 10))
        assert budget.get_budget().cost_usd == pytest.approx(6.0)
        assert budget.process_spend().cost_usd == pytest.approx(1.0)
        assert budget.spend_marker() == "SPEND:1.000000:10"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    use_workspace(monkeypatch, tmp_path)
    (tmp_path / "p").mkdir()
    return tmp_path / "p"


def _patch_build(monkeypatch, body):
    from build_pipeline import orchestrator

    async def fake_run_orchestrator(config, args):
        return body()

    monkeypatch.setattr(orchestrator, "run_orchestrator", fake_run_orchestrator)


@pytest.mark.parametrize("code", [0, 1, 3])
def test_build_prints_spend_on_each_exit(workspace, monkeypatch, capsys, code):
    def body():
        budget.record(_usage(0.4, 40))
        print("PR:https://example.invalid/pull/1" if code == 0 else "SPLIT:REQUIRED:/x", flush=True)
        return code

    _patch_build(monkeypatch, body)
    assert cli.main(["build", "--change", "x", "--project-dir", str(workspace)]) == code
    out = capsys.readouterr().out
    assert _spend_lines(out) == ["SPEND:0.400000:40"]
    assert out.splitlines()[-1] == "SPEND:0.400000:40"


def test_build_prints_spend_when_it_raises(workspace, monkeypatch, capsys):
    def body():
        budget.record(_usage(0.1, 1))
        raise RuntimeError("boom")

    _patch_build(monkeypatch, body)
    with pytest.raises(RuntimeError):
        cli.main(["build", "--change", "x", "--project-dir", str(workspace)])
    assert _spend_lines(capsys.readouterr().out) == ["SPEND:0.100000:1"]


def test_a_resumed_build_reports_only_its_own_spend(workspace, monkeypatch, capsys):
    def body():
        # What the engine does on resume: the saved spend comes back into the
        # build's budget, so its ceiling holds; it was not spent by this process.
        budget.get_budget().load_state({"cost_usd": 7.5, "input_tokens": 9000, "calls": 4})
        budget.record(_usage(0.5, 50))
        return 0

    _patch_build(monkeypatch, body)
    assert cli.main(["build", "--change", "x", "--project-dir", str(workspace)]) == 0
    assert _spend_lines(capsys.readouterr().out) == ["SPEND:0.500000:50"]


def test_findings_prints_both_rounds_spend(workspace, monkeypatch, capsys):
    from build_pipeline.findings import cli as findings_cli

    def run_cli(args):
        budget.record(_usage(0.3, 30))     # round 1
        budget.reset()                     # what each findings engine run does
        budget.record(_usage(0.2, 20))     # round 2
        return 0

    monkeypatch.setattr(findings_cli, "run_cli", run_cli)
    assert cli.main(["findings", "--pr", "7", "--project-dir", str(workspace)]) == 0
    assert _spend_lines(capsys.readouterr().out) == ["SPEND:0.500000:50"]


def test_other_commands_print_no_spend(workspace, monkeypatch, capsys):
    from build_pipeline import setup as setup_mod

    monkeypatch.setattr(setup_mod, "config_get", lambda args: 0)
    cli.main(["config-get", "intake.provider", "--project-dir", str(workspace)])
    assert _spend_lines(capsys.readouterr().out) == []


# --- item 19: what a record and the ledger say was spent ----------------------------

class TestRecordSpend:
    def _attempt(self, tmp_path, kind="build", log=""):
        a = records.start(tmp_path, kind, "a", ["x"])
        a.log.write_text(log)
        return a

    def test_the_result_holds_the_logs_spend(self, tmp_path):
        a = self._attempt(tmp_path, log="WORKTREE:/w\nSPEND:1.250000:300\nPR:u\n")
        assert records.finish(a, 0, interrupted=False)["spend"] == pytest.approx(1.25)

    def test_no_spend_line_is_null(self, tmp_path):
        a = self._attempt(tmp_path, log="WORKTREE:/w\n")
        assert records.finish(a, None, interrupted=True)["spend"] is None

    def test_killed_after_spend_reads_it_from_the_log(self, tmp_path):
        """B2.S2: the result written on resume takes the spend from the log."""
        a = self._attempt(tmp_path, log="WORKTREE:/w\nPR:u\nSPEND:0.5:10\n")
        assert records.finish(a, None, interrupted=True)["spend"] == pytest.approx(0.5)

    def test_epic_spend_sums_results_and_null_is_incomplete(self, tmp_path):
        a = self._attempt(tmp_path, log="SPEND:1.0:1\n")
        records.finish(a, 0, interrupted=False)
        b = self._attempt(tmp_path, log="SPEND:2.5:1\n")
        records.finish(b, 1, interrupted=False)
        assert spend.attempts_spend(records.attempts(tmp_path)) == (pytest.approx(3.5), True)
        c = self._attempt(tmp_path, log="WORKTREE:/w\n")          # B2.S1
        records.finish(c, None, interrupted=True)
        assert spend.attempts_spend(records.attempts(tmp_path)) == (pytest.approx(3.5), False)

    def test_a_running_attempt_is_not_complete(self, tmp_path):
        self._attempt(tmp_path)
        assert spend.attempts_spend(records.attempts(tmp_path)) == (0.0, False)

    def test_kinds_that_report_no_spend_are_left_out(self, tmp_path):
        a = self._attempt(tmp_path, kind="post", log="")
        records.finish(a, 0, interrupted=False)
        assert spend.attempts_spend(records.attempts(tmp_path)) == (0.0, True)


def _ledger(path: Path, *lines: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _started(slug, head, target="/r/pr-1"):
    return {"event": "started", "slug": slug, "target_dir": target, "head_sha": head}


def _finished(slug, head, cost, target="/r/pr-1", stopped=None):
    return {"event": "finished", "slug": slug, "target_dir": target, "head_sha": head,
            "pr": 1, "stopped": stopped, "run": {"cost_usd": cost}}


class TestReviewSpend:
    SLUGS = {"o-r--pr-1", "o-r--pr-2"}

    def test_a_review_resumed_after_its_budget_stop_counts_once(self, tmp_path):
        """B2.S9: the resumed run starts from the saved spend, so the last line holds both."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 3.0, stopped="budget"),
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 5.0))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(5.0), True)

    def test_each_head_and_each_child_counts(self, tmp_path):
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 1.0),
                         _started("o-r--pr-1", "h2"), _finished("o-r--pr-1", "h2", 2.0),
                         _started("o-r--pr-2", "h3", "/r/pr-2"), _finished("o-r--pr-2", "h3", 4.0, "/r/pr-2"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(7.0), True)

    def test_other_prs_and_other_repos_are_left_out(self, tmp_path):
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _finished("o-r--pr-9", "h1", 50.0), _finished("o-other--pr-1", "h1", 50.0),
                         _started("o-r--pr-9", "h2"))
        assert spend.review_spend(ledger, self.SLUGS) == (0.0, True)

    def test_a_killed_review_is_not_counted_and_not_complete(self, tmp_path):
        """B2.S8."""
        ledger = _ledger(tmp_path / "ledger.jsonl", _finished("o-r--pr-1", "h1", 1.0),
                         _started("o-r--pr-1", "h2"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(1.0), False)

    def test_no_ledger_is_nothing_spent(self, tmp_path):
        assert spend.review_spend(tmp_path / "missing.jsonl", self.SLUGS) == (0.0, True)

    def test_a_line_that_does_not_parse_is_skipped(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("{not json\n" + json.dumps(_finished("o-r--pr-1", "h1", 2.0)) + "\n")
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(2.0), True)

    def test_child_slugs_come_from_numbered_pr_urls(self):
        prs = ["https://github.com/o/r/pull/12", None, "https://example.invalid/o/r/pull/e-a"]
        assert spend.review_slugs("o-r", prs) == {"o-r--pr-12"}


# --- item 19: the cap -------------------------------------------------------------

class Epic:
    def __init__(self, root: Path, monkeypatch, steps, *, max_usd, parallel=1):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel, max_usd=max_usd)
        approve(self.config, INDEPENDENT, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def launches(self) -> list[str]:
        calls = CallLog(self.root / "calls.log").calls()
        return [Path(c["argv"][c["argv"].index("--plan-file") + 1]).stem for c in calls]

    def epic_json(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())


def test_the_budget_stop_fires(tmp_path, monkeypatch, capsys):
    each = [{"spend": 4.0}]
    e = Epic(tmp_path, monkeypatch, {"a": each, "b": each, "c": each}, max_usd=5.0)
    assert run_coordinator(e.config) == 3
    out = capsys.readouterr().out
    assert e.launches() == ["a", "b"]           # 4 < 5 before b; 8 >= 5 before c
    assert "EPIC:BUDGET:8.00" in out.splitlines()
    assert "EPIC:MERGE_WAITING" not in out
    saved = e.epic_json()
    assert saved["spent_usd"] == pytest.approx(8.0) and saved["spend_complete"] is True
    assert saved["child_status"]["c"]["status"] == "pending"
    summary = json.loads((e.dir / "runs" / "01" / "summary.json").read_text())
    assert summary["spent_usd"] == pytest.approx(8.0) and summary["spend_complete"] is True


def test_the_budget_stop_waits_for_running_children(tmp_path, monkeypatch, capsys):
    e = Epic(tmp_path, monkeypatch, {"a": [{"spend": 6.0}], "b": [{"spend": 6.0, "block": True}],
                                      "c": [{"spend": 6.0}]}, max_usd=5.0, parallel=2)
    ticks = {"n": 0}

    def tick(coord):
        ticks["n"] += 1
        if ticks["n"] > 20:
            e.fakes.release("b")

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert run_coordinator(e.config) == 3
    assert sorted(e.launches()) == ["a", "b"]
    assert "EPIC:BUDGET:12.00" in capsys.readouterr().out.splitlines()
    assert e.epic_json()["child_status"]["b"]["status"] == "built"


def test_no_ceiling_never_stops(tmp_path, monkeypatch, capsys):
    each = [{"spend": 40.0}]
    e = Epic(tmp_path, monkeypatch, {"a": each, "b": each, "c": each}, max_usd=None)
    assert run_coordinator(e.config) == 0
    assert sorted(e.launches()) == ["a", "b", "c"]
    assert e.epic_json()["spent_usd"] == pytest.approx(120.0)
    assert "EPIC:BUDGET" not in capsys.readouterr().out


def test_review_spend_in_the_ledger_counts_toward_the_cap(tmp_path, monkeypatch, capsys):
    e = Epic(tmp_path, monkeypatch, {"a": [{"spend": 1.0}], "b": [{"spend": 1.0}], "c": [{"spend": 1.0}]},
             max_usd=5.0)
    monkeypatch.setattr(coordinator.Coordinator, "_review_slugs", lambda self: {"o-r--pr-1"})
    _ledger(e.data / "reviews" / "ledger.jsonl", _finished("o-r--pr-1", "h1", 4.5))
    assert run_coordinator(e.config) == 3
    assert e.launches() == ["a"]                 # 4.5 + 1 >= 5 before b
    assert "EPIC:BUDGET:5.50" in capsys.readouterr().out.splitlines()
