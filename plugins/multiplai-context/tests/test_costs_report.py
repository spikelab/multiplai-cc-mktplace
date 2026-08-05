"""Unit tests for costs_report.py — branch grouping and filtering."""

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import costs_report  # noqa: E402
from multiplai_core.costing import append_records  # noqa: E402


@pytest.fixture(autouse=True)
def _workspace(monkeypatch, tmp_path):
    """Isolated workspace so ledger reads/writes land in tmp."""
    for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR", "CLAUDE_PLUGIN_OPTION_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    from multiplai_core.paths import _reset_cache
    _reset_cache()
    yield
    _reset_cache()


def _rec(msg_id, *, session="sess-1", branch=None, cost=1.0, ts="2026-07-01T10:00:00Z",
         model="claude-opus-4-8", sidechain=False, span=None):
    rec = {
        "ts": ts, "source": "transcript", "session": session, "project": "p",
        "model": model, "msg_id": msg_id, "sidechain": sidechain, "span": span,
        "component": "", "tokens": {"in": 1, "out": 1, "cw5m": 0, "cw1h": 0, "cr": 0},
        "cost_usd": cost,
    }
    if branch is not None:
        rec["branch"] = branch
    return rec


def _run(monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["costs_report.py", *argv])
    code = costs_report.main()
    return code, capsys.readouterr().out


def _seed():
    append_records([
        _rec("m1", session="sess-1", branch="main", cost=1.0),
        _rec("m2", session="sess-1", branch="feat/x", cost=2.0),   # mid-session switch
        _rec("m3", session="sess-2", branch="feat/x", cost=4.0),
        _rec("m4", session="sess-2", branch="feat/x", cost=0.5, sidechain=True,
             span={"kind": "agent", "name": "Explore"}),
        _rec("m5", session="sess-3", cost=8.0),                    # no branch → (none)
    ])


def test_by_branch_grouping(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--all", "--by", "branch", "--json")
    assert code == 0
    assert json.loads(out) == {"feat/x": 6.5, "main": 1.0, "(none)": 8.0}


def test_branch_filter_summary(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "feat/x", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["branch"] == "feat/x"
    assert data["records"] == 3
    assert data["total_usd"] == 6.5
    assert data["main_usd"] == 6.0
    assert data["subagents_usd"] == 0.5
    assert data["spans"] == {"agent:Explore": 0.5}
    assert set(data["sessions"]) == {"sess-1", "sess-2"}


def test_branch_filter_none_bucket(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "(none)", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["records"] == 1
    assert data["total_usd"] == 8.0


def test_branch_plus_session_splits_switched_session(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "feat/x", "--session", "sess-1", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["session"] == "sess-1"
    assert data["total_usd"] == 2.0  # only sess-1's feat/x record, not the main one


def test_branch_filter_no_match_errors(monkeypatch, capsys):
    _seed()
    code, _ = _run(monkeypatch, capsys, "--branch", "no-such-branch")
    assert code == 1


def test_branch_report_text_output(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--branch", "feat/x")
    assert code == 0
    assert "Branch feat/x" in out
    assert "$6.50" in out
    assert "[all months]" in out  # --branch reads the whole ledger — say so


def test_grouped_text_output_names_window(monkeypatch, capsys):
    """Bare --by branch scopes to the current month while --branch reads all
    months; both text outputs must name their window so the totals can't be
    silently contradictory."""
    _seed()
    code, out = _run(monkeypatch, capsys, "--all", "--by", "branch")
    assert code == 0
    assert "[all months]" in out
    code, out = _run(monkeypatch, capsys, "--month", "2026-07", "--by", "branch")
    assert code == 0
    assert "[2026-07]" in out


# --- Cache utilization ------------------------------------------------------


def _cache_rec(msg_id, *, component="", inp=0, cr=0, cw5m=0, cost=1.0):
    return {
        "ts": "2026-07-01T10:00:00Z", "source": "sdk", "session": "s", "project": "p",
        "model": "claude-opus-4-8", "msg_id": msg_id, "sidechain": False, "span": None,
        "component": component,
        "tokens": {"in": inp, "out": 10, "cw5m": cw5m, "cw1h": 0, "cr": cr},
        "cost_usd": cost,
    }


class TestCacheStats:
    def test_hit_ratio_excludes_cache_writes_from_the_denominator(self):
        """A write is what makes later hits possible — counting it as a miss
        would penalize the call that establishes the prefix."""
        stats = costs_report.cache_stats([_cache_rec("m", inp=100, cr=300, cw5m=1000)])
        assert stats["hit_ratio"] == 0.75
        assert stats["cache_write_tokens"] == 1000

    def test_no_eligible_tokens_reports_none_not_zero(self):
        stats = costs_report.cache_stats([_cache_rec("m", inp=0, cr=0)])
        assert stats["hit_ratio"] is None

    def test_totals_are_summed_across_records(self):
        stats = costs_report.cache_stats([
            _cache_rec("m1", inp=100, cr=100, cost=1.0),
            _cache_rec("m2", inp=300, cr=500, cost=2.0),
        ])
        assert stats["input_tokens"] == 400
        assert stats["cache_read_tokens"] == 600
        assert stats["cost_usd"] == 3.0

    def test_missing_token_tiers_do_not_break_old_records(self):
        """Ledger records are read schemalessly; a pre-cache-tier record must
        still parse."""
        stats = costs_report.cache_stats([{"cost_usd": 1.0, "tokens": {"in": 10}}])
        assert stats["hit_ratio"] == 0.0
        assert stats["cache_write_tokens"] == 0


class TestCacheReportRows:
    def test_worst_hit_ratio_first(self):
        rows = costs_report.cache_report_rows([
            _cache_rec("m1", component="good", inp=10, cr=90),
            _cache_rec("m2", component="bad", inp=90, cr=10),
        ], "component")
        assert [k for k, _ in rows] == ["bad", "good"]

    def test_no_evidence_buckets_sort_last_not_worst(self):
        rows = costs_report.cache_report_rows([
            _cache_rec("m1", component="silent", inp=0, cr=0),
            _cache_rec("m2", component="bad", inp=90, cr=10),
        ], "component")
        assert [k for k, _ in rows] == ["bad", "silent"]


def test_cache_report_flags_rows_below_threshold(monkeypatch, capsys):
    append_records([
        _cache_rec("m1", component="buildme", inp=900, cr=100),
        _cache_rec("m2", component="deep-research", inp=100, cr=900),
    ])
    code, out = _run(monkeypatch, capsys, "--all", "--report", "cache",
                     "--by", "component", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["flagged"] == ["buildme"]
    assert data["rows"]["deep-research"]["hit_ratio"] == 0.9


def test_cache_report_threshold_is_configurable(monkeypatch, capsys):
    append_records([_cache_rec("m1", component="buildme", inp=400, cr=600)])
    code, out = _run(monkeypatch, capsys, "--all", "--report", "cache",
                     "--cache-threshold", "0.8", "--json")
    assert code == 0
    assert json.loads(out)["flagged"] == ["buildme"]


def test_cache_report_honours_group_as_well_as_by(monkeypatch, capsys):
    """`--group` is normalized into `args.by` AFTER the cache branch returns,
    so `--report cache --group session` used to group by component silently."""
    append_records([
        _cache_rec("m1", component="buildme", inp=900, cr=100),
        _cache_rec("m2", component="deep-research", inp=100, cr=900),
    ])
    code, out = _run(monkeypatch, capsys, "--all", "--report", "cache",
                     "--group", "model", "--json")
    assert code == 0
    rows = json.loads(out)["rows"]
    assert set(rows) == {"claude-opus-4-8"}   # grouped by model, not component


def test_cache_report_rejects_a_group_axis_it_cannot_use(monkeypatch, capsys):
    append_records([_cache_rec("m1", component="buildme", inp=900, cr=100)])
    monkeypatch.setattr(sys, "argv", ["costs_report.py", "--all", "--report", "cache",
                                      "--group", "task", "--json"])
    code = costs_report.main()
    assert code == 2
    assert "cannot group by" in capsys.readouterr().err


def test_cache_report_text_output(monkeypatch, capsys):
    append_records([_cache_rec("m1", component="buildme", inp=900, cr=100)])
    code, out = _run(monkeypatch, capsys, "--all", "--report", "cache")
    assert code == 0
    assert "overall hit ratio" in out
    assert "cache_control" in out  # the flag line says what to do about it


# --- Task outcomes ----------------------------------------------------------


class TestFetchPrMap:
    def _proc(self, stdout="[]", returncode=0, stderr=""):
        class P:
            pass
        p = P()
        p.stdout, p.returncode, p.stderr = stdout, returncode, stderr
        return p

    def test_maps_branches_to_outcomes(self, tmp_path):
        payload = json.dumps([
            {"number": 1, "headRefName": "feat/a", "mergedAt": "2026-07-01", "closedAt": "2026-07-01"},
            {"number": 2, "headRefName": "feat/b", "mergedAt": None, "closedAt": "2026-07-02"},
            {"number": 3, "headRefName": "feat/c", "mergedAt": None, "closedAt": None},
        ])
        out = costs_report.fetch_pr_map(
            tmp_path, runner=lambda cmd, cwd: self._proc(payload))
        assert out["feat/a"]["outcome"] == "merged"
        assert out["feat/b"]["outcome"] == "closed"
        assert out["feat/c"]["outcome"] == "open"

    def test_reused_branch_takes_the_latest_pr(self, tmp_path):
        payload = json.dumps([
            {"number": 1, "headRefName": "feat/a", "mergedAt": None, "closedAt": "x"},
            {"number": 7, "headRefName": "feat/a", "mergedAt": "y", "closedAt": "y"},
        ])
        out = costs_report.fetch_pr_map(
            tmp_path, runner=lambda cmd, cwd: self._proc(payload))
        assert out["feat/a"] == {"number": 7, "outcome": "merged"}

    def test_gh_failure_degrades_to_empty_not_an_exception(self, tmp_path, capsys):
        """A cost report must not die because a PR lookup did."""
        out = costs_report.fetch_pr_map(
            tmp_path, runner=lambda cmd, cwd: self._proc("", 1, "not authenticated"))
        assert out == {}
        assert "no-pr" in capsys.readouterr().err

    def test_gh_missing_degrades_to_empty(self, tmp_path, capsys):
        def boom(cmd, cwd):
            raise FileNotFoundError("gh")
        assert costs_report.fetch_pr_map(tmp_path, runner=boom) == {}
        assert "no-pr" in capsys.readouterr().err

    def test_result_is_cached_within_the_ttl(self, tmp_path):
        calls = []

        def runner(cmd, cwd):
            calls.append(cmd)
            return self._proc(json.dumps(
                [{"number": 1, "headRefName": "feat/a", "mergedAt": "x", "closedAt": "x"}]))

        costs_report.fetch_pr_map(tmp_path, runner=runner)
        costs_report.fetch_pr_map(tmp_path, runner=runner)
        assert len(calls) == 1

    def test_an_empty_map_is_not_cached(self, tmp_path):
        """On disk, a cached {} is indistinguishable from "we couldn't find
        out" — caching it would suppress the lookup for the whole TTL right
        when the repo's first PR appears."""
        calls = []

        def runner(cmd, cwd):
            calls.append(cmd)
            return self._proc("[]")

        assert costs_report.fetch_pr_map(tmp_path, runner=runner) == {}
        assert costs_report.fetch_pr_map(tmp_path, runner=runner) == {}
        assert len(calls) == 2

    def test_expired_cache_refetches(self, tmp_path):
        calls = []

        def runner(cmd, cwd):
            calls.append(cmd)
            return self._proc("[]")

        costs_report.fetch_pr_map(tmp_path, runner=runner)
        costs_report.fetch_pr_map(tmp_path, ttl_s=0, runner=runner)
        assert len(calls) == 2


class TestTaskRows:
    def test_unresolvable_branches_read_no_pr(self):
        rows = costs_report.task_rows([_rec("m1", branch="feat/x", cost=3.0)], {})
        assert rows[0]["outcome"] == "no-pr"
        assert rows[0]["pr"] is None

    def test_unattributed_records_are_not_tasks(self):
        rows = costs_report.task_rows([_rec("m1", cost=3.0)], {})
        assert rows == []

    def test_sorted_by_cost(self):
        rows = costs_report.task_rows([
            _rec("m1", branch="cheap", cost=1.0),
            _rec("m2", branch="pricey", cost=9.0),
        ], {})
        assert [r["task"] for r in rows] == ["pricey", "cheap"]


class TestTaskSummary:
    def _rows(self):
        return costs_report.task_rows([
            _rec("m1", branch="feat/a", cost=10.0),
            _rec("m2", branch="feat/b", cost=20.0),
            _rec("m3", branch="feat/dead", cost=6.0),
        ], {
            "feat/a": {"number": 1, "outcome": "merged"},
            "feat/b": {"number": 2, "outcome": "merged"},
            "feat/dead": {"number": 3, "outcome": "closed"},
        })

    def test_abandoned_work_counts_toward_the_cost_of_a_finished_task(self):
        """Task-branch spend ÷ merged tasks — the failed attempts are part of
        what a finished task actually cost."""
        s = costs_report.task_summary(self._rows())
        assert s["cost_per_merged_task"] == 18.0  # 36 total / 2 merged
        assert s["merged_usd"] == 30.0
        assert s["abandoned_usd"] == 6.0

    def test_median_covers_only_merged_tasks(self):
        s = costs_report.task_summary(self._rows())
        assert s["median_merged_task_usd"] == 15.0

    def test_no_merged_tasks_reports_none_not_zero(self):
        s = costs_report.task_summary(costs_report.task_rows(
            [_rec("m1", branch="feat/a", cost=5.0)], {}))
        assert s["cost_per_merged_task"] is None
        assert s["median_merged_task_usd"] is None


def test_group_task_without_pr_join_still_reports(monkeypatch, capsys):
    _seed()
    code, out = _run(monkeypatch, capsys, "--all", "--group", "task", "--json")
    assert code == 0
    data = json.loads(out)
    assert {r["task"] for r in data["rows"]} == {"main", "feat/x"}
    assert all(r["outcome"] == "no-pr" for r in data["rows"])
    assert data["summary"]["cost_per_merged_task"] is None


def test_group_task_with_pr_join_uses_gh(monkeypatch, capsys, tmp_path):
    append_records([_rec("m1", branch="feat/x", cost=4.0)])
    monkeypatch.setattr(costs_report, "_repo_dirs", lambda recs: [tmp_path])
    monkeypatch.setattr(costs_report, "fetch_pr_map",
                        lambda repo, **kw: {"feat/x": {"number": 9, "outcome": "merged"}})
    code, out = _run(monkeypatch, capsys, "--all", "--group", "task", "--pr-join", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["rows"][0]["outcome"] == "merged"
    assert data["rows"][0]["pr"] == 9
    assert data["summary"]["cost_per_merged_task"] == 4.0


def test_group_falls_back_to_plain_grouping(monkeypatch, capsys):
    """--group accepts everything --by does."""
    _seed()
    code, out = _run(monkeypatch, capsys, "--all", "--group", "branch", "--json")
    assert code == 0
    assert json.loads(out) == {"feat/x": 6.5, "main": 1.0, "(none)": 8.0}


# --- Build outcomes ---------------------------------------------------------


def _write_state(project_dir, change, blocks, *, cost=0.0, tokens=0, by_label=None,
                 phase="complete"):
    d = project_dir / "specs" / "changes" / change
    d.mkdir(parents=True, exist_ok=True)
    (d / ".build-state.json").write_text(json.dumps({
        "change_name": change, "mode": "scratch", "tier": "advanced", "phase": phase,
        "tdd": {"blocks": [{"number": i + 1, "name": f"b{i}", "status": s}
                           for i, s in enumerate(blocks)]},
        "budget": {"cost_usd": cost, "total_tokens": tokens,
                   "by_label": by_label or {}},
    }))


class TestReadBuildStates:
    def test_counts_done_and_failed_blocks(self, tmp_path):
        _write_state(tmp_path, "feat-a", ["done", "done", "failed"], cost=5.0)
        states = costs_report.read_build_states(tmp_path)
        assert states[0]["done"] == 2
        assert states[0]["failed"] == 1
        assert states[0]["cost_usd"] == 5.0

    def test_state_without_a_budget_reads_zero_not_an_error(self, tmp_path):
        """States written before buildme 0.5 carry no budget block."""
        d = tmp_path / "specs" / "changes" / "old"
        d.mkdir(parents=True)
        (d / ".build-state.json").write_text(json.dumps(
            {"change_name": "old", "mode": "s", "tier": "t",
             "tdd": {"blocks": [{"number": 1, "name": "b", "status": "done"}]}}))
        states = costs_report.read_build_states(tmp_path)
        assert states[0]["cost_usd"] == 0.0
        assert states[0]["done"] == 1

    def test_unreadable_state_is_skipped_not_fatal(self, tmp_path, capsys):
        d = tmp_path / "specs" / "changes" / "broken"
        d.mkdir(parents=True)
        (d / ".build-state.json").write_text("{not json")
        _write_state(tmp_path, "fine", ["done"])
        states = costs_report.read_build_states(tmp_path)
        assert [s["change"] for s in states] == ["fine"]
        assert "skipping unreadable" in capsys.readouterr().err


class TestBuildSummary:
    def test_cost_per_done_and_failed_block(self, tmp_path):
        _write_state(tmp_path, "clean", ["done", "done"], cost=10.0)
        _write_state(tmp_path, "messy", ["done", "failed"], cost=30.0)
        s = costs_report.build_summary(costs_report.read_build_states(tmp_path))
        assert s["blocks_done"] == 3
        assert s["blocks_failed"] == 1
        assert s["cost_per_done_block"] == round(40.0 / 3, 4)
        # Only the build that failed a block contributes to the failed figure.
        assert s["cost_per_failed_block"] == 30.0

    def test_no_failures_reports_none(self, tmp_path):
        _write_state(tmp_path, "clean", ["done"], cost=1.0)
        s = costs_report.build_summary(costs_report.read_build_states(tmp_path))
        assert s["cost_per_failed_block"] is None

    def test_phase_token_breakdown_is_aggregated(self, tmp_path):
        _write_state(tmp_path, "a", ["done"], by_label={"review": 100, "implementer": 50})
        _write_state(tmp_path, "b", ["done"], by_label={"review": 300})
        s = costs_report.build_summary(costs_report.read_build_states(tmp_path))
        assert list(s["tokens_by_phase"].items())[0] == ("review", 400)


def test_group_build_report(monkeypatch, capsys, tmp_path):
    _write_state(tmp_path, "feat-a", ["done", "failed"], cost=12.0)
    code, out = _run(monkeypatch, capsys, "--group", "build",
                     "--project-dir", str(tmp_path), "--json")
    assert code == 0
    data = json.loads(out)
    assert data["summary"]["blocks_done"] == 1
    assert data["summary"]["cost_per_done_block"] == 12.0
    assert data["summary"]["cost_per_failed_block"] == 12.0


def test_group_build_no_states_errors(monkeypatch, capsys, tmp_path):
    code, _ = _run(monkeypatch, capsys, "--group", "build", "--project-dir", str(tmp_path))
    assert code == 1


class TestStandingBranches:
    def _rows(self):
        return costs_report.task_rows([
            _rec("m1", branch="main", cost=1000.0),
            _rec("m2", branch="feat/a", cost=10.0),
        ], {"feat/a": {"number": 1, "outcome": "merged"}})

    def test_main_is_not_divided_into_the_per_task_cost(self):
        """Interactive work on main dwarfs every feature branch; folding it in
        would report 'cost of everything ÷ two PRs'."""
        s = costs_report.task_summary(self._rows())
        assert s["cost_per_merged_task"] == 10.0
        assert s["tasks"] == 1

    def test_standing_branch_spend_is_still_reported(self):
        s = costs_report.task_summary(self._rows())
        assert s["standing_branch_usd"] == 1000.0

    def test_standing_branches_still_appear_as_rows(self):
        assert "main" in {r["task"] for r in self._rows()}


class TestP90:
    def test_p90_is_never_below_the_median(self):
        rows = costs_report.task_rows(
            [_rec(f"m{i}", branch=f"feat/{i}", cost=float(i)) for i in range(1, 11)],
            {f"feat/{i}": {"number": i, "outcome": "merged"} for i in range(1, 11)},
        )
        s = costs_report.task_summary(rows)
        assert s["p90_merged_task_usd"] >= s["median_merged_task_usd"]
        assert s["p90_merged_task_usd"] == 9.0

    def test_two_task_p90_takes_the_pricier(self):
        """The old int(n*0.9)-1 index returned the *cheapest* task for n=2."""
        rows = costs_report.task_rows([
            _rec("m1", branch="feat/a", cost=1.0),
            _rec("m2", branch="feat/b", cost=9.0),
        ], {"feat/a": {"number": 1, "outcome": "merged"},
            "feat/b": {"number": 2, "outcome": "merged"}})
        assert costs_report.task_summary(rows)["p90_merged_task_usd"] == 9.0


class TestEmptyLedgerStillEmitsJson:
    """`--json` is a promise about output shape. An empty ledger is a normal
    state (fresh machine, narrow window), and printing only prose left JSON
    consumers parsing empty stdin — which is how CI caught it."""

    def test_summary_json_is_parseable_with_no_records(self, monkeypatch, capsys):
        code, out = _run(monkeypatch, capsys, "--all", "--json")
        assert code == 1
        assert json.loads(out)["rows"] == []

    def test_by_dimension_json_is_parseable_with_no_records(self, monkeypatch, capsys):
        code, out = _run(monkeypatch, capsys, "--all", "--by", "model", "--json")
        assert code == 1
        assert "error" in json.loads(out)

    def test_branch_json_is_parseable_with_no_records(self, monkeypatch, capsys):
        code, out = _run(monkeypatch, capsys, "--branch", "nope", "--json")
        assert code == 1
        assert "nope" in json.loads(out)["error"]

    def test_session_json_is_parseable_with_no_records(self, monkeypatch, capsys):
        code, out = _run(monkeypatch, capsys, "--all", "--session", "nope", "--json")
        assert code == 1
        assert "nope" in json.loads(out)["error"]

    def test_task_json_is_parseable_with_no_records(self, monkeypatch, capsys):
        code, out = _run(monkeypatch, capsys, "--all", "--group", "task", "--json")
        assert code == 1
        assert json.loads(out)["rows"] == []

    def test_cache_json_is_parseable_with_no_records(self, monkeypatch, capsys):
        code, out = _run(monkeypatch, capsys, "--all", "--report", "cache", "--json")
        assert code == 1
        assert "error" in json.loads(out)

    def test_build_json_is_parseable_with_no_state_files(self, monkeypatch, capsys, tmp_path):
        code, out = _run(monkeypatch, capsys, "--group", "build",
                         "--project-dir", str(tmp_path), "--json")
        assert code == 1
        assert json.loads(out)["project_dir"] == str(tmp_path)

    def test_without_json_the_message_stays_on_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["costs_report.py", "--all"])
        assert costs_report.main() == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "No ledger records" in captured.err
