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
from build_pipeline import budget, git_ops
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
    merge_waiting,
    pr_url,
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


def _started(slug, head, target="/r/pr-1", at=None):
    """A `started` line; *at* is its `generated_at`, which the review skill
    writes to the second."""
    line = {"event": "started", "slug": slug, "target_dir": target, "head_sha": head}
    if at is not None:
        line["generated_at"] = at
    return line


def _finished(slug, head, cost, target="/r/pr-1", stopped=None, started_at="2026-10-13T10:00:00.000Z"):
    """A `finished` line. `run.started_at` is the first start of the run a
    `resume` continues; a fresh `review` gets a new one."""
    return {"event": "finished", "slug": slug, "target_dir": target, "head_sha": head,
            "pr": 1, "stopped": stopped, "run": {"cost_usd": cost, "started_at": started_at}}


class TestReviewSpend:
    SLUGS = {"o-r--pr-1", "o-r--pr-2"}

    def test_a_review_resumed_after_its_budget_stop_counts_once(self, tmp_path):
        """B2.S9: the resumed run starts from the saved spend, so the last line holds both."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 3.0, stopped="budget"),
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 5.0))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(5.0), True)

    def test_a_fresh_review_of_a_reviewed_head_adds_its_spend(self, tmp_path):
        """B2.S10: a `review` (not `resume`) of a head already reviewed starts
        from no spend, with a new `run.started_at`; both runs count."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1"),
                         _finished("o-r--pr-1", "h1", 3.0, stopped="budget", started_at="T1"),
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 2.0, started_at="T2"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(5.0), True)

    def test_a_resumed_chain_and_a_fresh_rerun_on_one_head(self, tmp_path):
        """The resume's line holds its chain's spend (3 + 2); the rerun adds its own."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1"),
                         _finished("o-r--pr-1", "h1", 3.0, stopped="budget", started_at="T1"),
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 5.0, started_at="T1"),
                         _started("o-r--pr-1", "h1"), _finished("o-r--pr-1", "h1", 1.5, started_at="T2"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(6.5), True)

    @pytest.mark.parametrize("run", [None, {}, {"cost_usd": "x"}, {"cost_usd": True}],
                             ids=["null", "no-cost", "text", "bool"])
    def test_a_finished_line_with_no_cost_is_incomplete(self, tmp_path, run):
        line = {"event": "finished", "slug": "o-r--pr-1", "target_dir": "/r/pr-1", "head_sha": "h1",
                "round": "01", "stopped": None, "run": run}
        ledger = _ledger(tmp_path / "ledger.jsonl", _finished("o-r--pr-1", "h0", 1.0), line)
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(1.0), False)

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

    def test_a_killed_review_then_a_fresh_review_is_not_complete(self, tmp_path):
        """A review killed at 10:00 (no `finished`), then a fresh `review` of
        the same head (`--restart`) that began at 10:05 and finished: the
        killed run's spend is not in any line, so the total is incomplete."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:00:00Z"),
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:05:00Z"),
                         _finished("o-r--pr-1", "h1", 2.0, started_at="2026-10-13T10:05:00.300Z"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(2.0), False)

    def test_a_killed_review_that_was_resumed_is_complete(self, tmp_path):
        """The `resume` keeps the killed run's first start, which is before
        the resume's own `started` line: its last line holds the whole run."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:00:00Z"),
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:05:00Z"),
                         _finished("o-r--pr-1", "h1", 5.0, started_at="2026-10-13T10:00:00.200Z"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(5.0), True)

    def test_a_killed_review_a_killed_rerun_and_the_reruns_resume_is_not_complete(self, tmp_path):
        """The first killed run is never resumed: a fresh run replaced it."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:00:00Z"),
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:05:00Z"),
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:09:00Z"),
                         _finished("o-r--pr-1", "h1", 3.0, started_at="2026-10-13T10:05:00.100Z"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(3.0), False)

    def test_a_run_with_no_run_record_then_its_resume_is_complete(self, tmp_path):
        """A `finished` line whose run record could not be built (`run: null`),
        then a `resume` of that run: the resume's line holds the whole run."""
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:00:00Z"),
                         {"event": "finished", "slug": "o-r--pr-1", "target_dir": "/r/pr-1", "head_sha": "h1",
                          "round": "01", "stopped": "budget", "run": None},
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:05:00Z"),
                         _finished("o-r--pr-1", "h1", 4.0, started_at="2026-10-13T10:00:00.200Z"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(4.0), True)

    def test_a_run_with_no_run_record_then_a_fresh_review_is_not_complete(self, tmp_path):
        ledger = _ledger(tmp_path / "ledger.jsonl",
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:00:00Z"),
                         {"event": "finished", "slug": "o-r--pr-1", "target_dir": "/r/pr-1", "head_sha": "h1",
                          "round": "01", "stopped": "budget", "run": None},
                         _started("o-r--pr-1", "h1", at="2026-10-13T10:05:00Z"),
                         _finished("o-r--pr-1", "h1", 2.0, started_at="2026-10-13T10:05:00.300Z"))
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(2.0), False)

    def test_no_ledger_is_nothing_spent(self, tmp_path):
        assert spend.review_spend(tmp_path / "missing.jsonl", self.SLUGS) == (0.0, True)

    def test_a_line_that_does_not_parse_is_skipped(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("{not json\n" + json.dumps(_finished("o-r--pr-1", "h1", 2.0)) + "\n")
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(2.0), True)

    def test_a_line_with_a_bad_byte_is_skipped(self, tmp_path):
        """The ledger is shared: one line that is not UTF-8 must not stop the
        coordinator, whose `_save` and `_summary` read it."""
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_bytes(b'{"slug": "\xff"}\n' + json.dumps(_finished("o-r--pr-1", "h1", 2.0)).encode() + b"\n")
        assert spend.review_spend(ledger, self.SLUGS) == (pytest.approx(2.0), True)

    def test_child_slugs_come_from_numbered_pr_urls(self):
        prs = ["https://github.com/o/r/pull/12", None, "https://example.invalid/o/r/pull/e-a"]
        assert spend.review_slugs("o-r", prs) == {"o-r--pr-12"}


# --- item 19: the cap -------------------------------------------------------------

class Epic:
    def __init__(self, root: Path, monkeypatch, steps, *, max_usd, parallel=1, answer=INDEPENDENT):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel, max_usd=max_usd)
        approve(self.config, answer, CallLog(root / "split.log"))
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
    assert run_coordinator(e.config) == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
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


def test_the_coordinator_ties_ledger_lines_to_its_childrens_prs(tmp_path, monkeypatch, capsys):
    """The review slugs come from the source repo's origin (`o/r.git`: `o-r`)
    and each child's numbered PR URL; a review of another PR is left out."""
    e = Epic(tmp_path, monkeypatch, {"a": [{"spend": 1.0, "pr_number": 12}],
                                      "b": [{"spend": 1.0, "pr_number": 13}], "c": [{"spend": 1.0}]},
             max_usd=5.0)
    _ledger(e.data / "reviews" / "ledger.jsonl",
            _finished("o-r--pr-12", "h1", 4.5, target="/r/pr-12"),
            _finished("o-r--pr-99", "h1", 40.0, target="/r/pr-99"))
    assert run_coordinator(e.config) == 3
    assert e.launches() == ["a"]                 # 1 + 4.5 >= 5 before b
    assert "EPIC:BUDGET:5.50" in capsys.readouterr().out.splitlines()


DEPENDENT = EpicSplitAnswer(single=False, reason="Three parts.",
                            children=[child("a"), child("b", ("a",)), child("c")])


@pytest.mark.parametrize("steps,answer", [
    ({"a": [{"spend": 6.0, "exit": 1}], "b": [{}], "c": [{}]}, INDEPENDENT),
    ({"a": [{"spend": 6.0}], "b": [{}], "c": [{}]}, DEPENDENT),
], ids=["a-child-failed", "a-pr-waits-to-merge"])
def test_the_budget_stop_wins_over_the_other_exits(tmp_path, monkeypatch, capsys, steps, answer):
    """Once spend reached epic.max_usd, the run ends with EPIC:BUDGET and exit 3,
    not exit 1 for a failed child (which is not relaunched) and not
    EPIC:MERGE_WAITING for a PR a child waits on."""
    e = Epic(tmp_path, monkeypatch, steps, max_usd=5.0, answer=answer)
    assert run_coordinator(e.config) == 3
    out = capsys.readouterr().out
    assert e.launches() == ["a"]
    assert "EPIC:BUDGET:6.00" in out.splitlines()
    assert "EPIC:MERGE_WAITING" not in out


# --- SPEND on SIGTERM and SIGHUP: a stop the coordinator sends ---------------------

_SIGNALLED = r"""
import sys, time
from types import SimpleNamespace
from build_pipeline import __main__ as cli, budget, git_ops, orchestrator
from build_pipeline.findings import cli as findings_cli

pidfile = sys.argv[1]


def body():
    budget.record(SimpleNamespace(cost_usd=2.5, input_tokens=100, output_tokens=0,
                                  cache_read_tokens=0, cache_creation_tokens=0))
    print("WORKTREE:/w", flush=True)
    if pidfile != "-":      # a wait on a skill, as when the build opens its PR
        git_ops._run_skill(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], None, 120)
    time.sleep(60)
    return 0


async def fake_run_orchestrator(config, args):
    return body()


orchestrator.run_orchestrator = fake_run_orchestrator
findings_cli.run_cli = lambda args: body()
sys.exit(cli.main(sys.argv[2:]))
"""


def _signalled(tmp_path, workspace, argv, sig, *, skill=False):
    """Run the real `main(argv)` with a body that spends $2.50, send *sig*
    once it printed WORKTREE:, and return (exit code, stdout, the skill's pid)."""
    import os
    import subprocess
    import sys
    import time

    scripts_dir = Path(cli.__file__).resolve().parent.parent
    pidfile = tmp_path / "skill.pid"
    proc = subprocess.Popen([sys.executable, "-c", _SIGNALLED, str(pidfile) if skill else "-", *argv],
                            cwd=str(scripts_dir), env={**os.environ, "PYTHONPATH": str(scripts_dir)},
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            if line == "WORKTREE:/w\n":
                break
        else:
            pytest.fail("the body never started")
        if skill:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
                time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(sig)
        out = proc.stdout.read()
        proc.wait(timeout=20)
        return proc.returncode, out, int(pidfile.read_text()) if skill else None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("command", ["build", "findings"])
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_spend_is_printed_when_a_signal_stops_the_process(tmp_path, workspace, command, signame):
    """B2.S11: the coordinator passes its own SIGTERM on to every running child.
    Before, Python's default action ended the child with no `finally`, so its
    attempt's spend was lost for good; now the stop unwinds as Ctrl-C does."""
    import signal

    sig = getattr(signal, signame)
    argv = (["build", "--change", "x"] if command == "build" else ["findings", "--pr", "7"])
    code, out, _ = _signalled(tmp_path, workspace, [*argv, "--project-dir", str(workspace)], sig)
    assert code == 128 + sig
    assert out.splitlines()[-1] == "SPEND:2.500000:100"


def test_a_sigterm_during_a_skill_wait_still_kills_the_skill(tmp_path, workspace):
    """The handler `main` installs leaves `git_ops._exit_on_termination` working:
    the skill's group is killed and SPEND is still printed."""
    import signal

    from .test_git_ops import _gone

    code, out, skill = _signalled(tmp_path, workspace,
                                  ["build", "--change", "x", "--project-dir", str(workspace)],
                                  signal.SIGTERM, skill=True)
    try:
        assert code == 128 + signal.SIGTERM
        assert out.splitlines()[-1] == "SPEND:2.500000:100"
        assert _gone(skill)
    finally:
        try:
            import os
            os.kill(skill, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("signame", ["SIGINT", "SIGTERM"])
def test_children_stopped_with_the_epic_keep_their_spend(tmp_path, monkeypatch, signame):
    """B2.S11 end to end: the epic is stopped while a and b run; each
    interrupted result holds the spend its child printed as it stopped."""
    import signal

    from .epic_row_helpers import run_signalled

    sig = getattr(signal, signame)
    e = Epic(tmp_path, monkeypatch, {"a": [{"spend": 4.0, "block": True}], "b": [{"spend": 3.0, "block": True}],
                                      "c": [{"spend": 1.0}]}, max_usd=None, parallel=2)
    monkeypatch.setattr(coordinator, "STOP_WAIT", 3000.0)

    def ready() -> bool:
        logs = list((e.dir / "agents").glob("*.log")) if (e.dir / "agents").exists() else []
        return len(logs) == 2 and all("WORKTREE:" in p.read_text() for p in logs)

    assert run_signalled(lambda: run_coordinator(e.config), ready, sig) == 128 + sig
    tries = records.attempts(e.dir)
    assert sorted((a.child, a.result["interrupted"], a.result["spend"]) for a in tries) == [
        ("a", True, 4.0), ("b", True, 3.0)]
    assert spend.attempts_spend(tries) == (pytest.approx(7.0), True)


# --- spend in the skills a build runs: the pr skill and the walkthrough -----------

class TestSkillSpend:
    """The pr skill (`open`, `edit`) and a walkthrough `run` or `resume` call a
    model in a subprocess of their own; each prints its own `SPEND:` line,
    which `_run_skill` adds to this process's total. The review skill's spend
    is in its ledger and is not added here."""

    def _run(self, *argv, out="", code=0):
        import sys

        script = f"import sys; sys.stdout.write({out!r}); sys.exit({code})"
        return git_ops._run_skill([sys.executable, "-c", script, *argv], None, 60)

    @pytest.mark.parametrize("argv", [("pr_pipeline", "open"), ("pr_pipeline", "edit", "7"),
                                      ("walkthrough_pipeline", "run", "/m"),
                                      ("walkthrough_pipeline", "resume", "/r")])
    def test_a_skills_spend_line_is_added(self, argv):
        budget.record(_usage(1.0, 10))
        self._run(*argv, out="PR:u\nSPEND:0.250000:40\n")
        assert budget.spend_marker() == "SPEND:1.250000:50"

    def test_a_review_skills_line_is_not_added(self):
        self._run("review_pipeline", "review", out="SPEND:9.0:9\n")
        assert budget.spend_marker() == "SPEND:0.000000:0"

    @pytest.mark.parametrize("argv", [("pr_pipeline", "open"), ("walkthrough_pipeline", "run", "/m")])
    def test_no_spend_line_makes_the_total_incomplete(self, argv):
        """Killed on a timeout, or by buildme's own stop: what it spent is unknown."""
        budget.record(_usage(1.0, 10))
        self._run(*argv, out="", code=-9 % 256)
        assert budget.spend_marker() == "SPEND:1.000000:10:incomplete"

    def test_a_skill_that_times_out_makes_the_total_incomplete(self):
        """Row B2.S12, the timeout: the group is killed before its `SPEND:`."""
        import sys

        budget.record(_usage(1.0, 10))
        r = git_ops._run_skill([sys.executable, "-c", "import time; time.sleep(30)", "pr_pipeline", "open"],
                               None, 1)
        assert r.returncode == 124
        assert budget.spend_marker() == "SPEND:1.000000:10:incomplete"

    def test_a_stop_during_the_wait_makes_the_total_incomplete(self):
        """Row B2.S12, buildme's own stop: a SIGTERM during the wait kills the
        group before its `SPEND:`, and the stop still ends buildme."""
        import os
        import signal
        import sys
        import threading

        budget.record(_usage(1.0, 10))
        timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            with pytest.raises(SystemExit) as stop:
                git_ops._run_skill([sys.executable, "-c", "import time; time.sleep(30)",
                                    "walkthrough_pipeline", "run", "/m"], None, 60)
        finally:
            timer.cancel()
        assert stop.value.code == 128 + signal.SIGTERM
        assert budget.spend_marker() == "SPEND:1.000000:10:incomplete"

    def test_a_walkthrough_check_calls_no_model(self):
        self._run("walkthrough_pipeline", "check", "/w.md", out="check: 3 checked, 0 failed\n")
        assert budget.spend_marker() == "SPEND:0.000000:0"

    def test_a_marked_attempt_counts_its_known_spend_and_is_incomplete(self, tmp_path):
        a = records.start(tmp_path, "build", "a", ["x"])
        a.log.write_text("WORKTREE:/w\nSPEND:1.500000:10:incomplete\n")
        assert records.finish(a, 0, interrupted=False)["spend"] == pytest.approx(1.5)
        assert spend.attempts_spend(records.attempts(tmp_path)) == (pytest.approx(1.5), False)
