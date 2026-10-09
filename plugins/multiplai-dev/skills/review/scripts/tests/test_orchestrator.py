"""The whole pipeline through the CLI, every agent call replaced."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import DEF_CITATION, KEYWORD_CITATION, SCHEMA, high_finding
from review_pipeline import budget, sdk
from review_pipeline.__main__ import main
from review_pipeline.config import DIMENSIONS
from review_pipeline.models import (AssessOutput, DuplicateSet, FinderOutput, MergeOutput, RepeatsOutput,
                                    ReviewState, Verdict)


def reworded_finding():
    """The HIGH finding as a second finder words it: merged into it by the merge stage."""
    return high_finding().with_location(claim="Rate plans are filtered on a literal 'dolcebot'",
                                        severity="MEDIUM", dimension="callers", finder="callers")


def settings_findings():
    """Two findings on one line of settings.py: a second group for the merge stage."""
    return [high_finding().with_location(claim=claim, file="settings.py", line_start=3, line_end=3,
                                          citations=[DEF_CITATION.model_dump()], finder="history")
            for claim in ("The OTA name default is hardcoded", "The OTA name is read without validation")]


class FakeAgents:
    """Canned answers by stage label. `kill_at` raises once at that stage;
    `budget_stop_at` raises BudgetExceededError once, on that call (1-based).
    `two_groups` adds two overlapping settings.py findings from `history`."""

    def __init__(self, kill_at: str | None = None, cost: float = 0.0, budget_stop_at: int | None = None,
                 two_groups: bool = False):
        self.calls: list[str] = []
        self.kill_at = kill_at
        self.cost = cost
        self.budget_stop_at = budget_stop_at
        self.two_groups = two_groups
        self.assess_answer: AssessOutput | None = None
        self.repeats_answer: RepeatsOutput | None = None

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        budget.check(stage=budget_label)
        self.calls.append(budget_label)
        if len(self.calls) == self.budget_stop_at:
            self.budget_stop_at = None
            raise budget.BudgetExceededError(f"circuit breaker stopped the run during {budget_label}")
        if self.cost:
            budget.record(SimpleNamespace(cost_usd=self.cost), label=budget_label)
        stage = budget_label.split(":")[0]
        if stage == self.kill_at:
            self.kill_at = None
            raise RuntimeError("killed")
        if budget_label == "find:diff-bugs":
            return FinderOutput(findings=[high_finding()])
        if budget_label == "find:callers":
            return FinderOutput(findings=[reworded_finding()])
        if budget_label == "find:history" and self.two_groups:
            return FinderOutput(findings=settings_findings())
        if stage == "find":
            return FinderOutput()
        if stage == "verify":
            return Verdict(status="confirmed", impact="breaks-users", reason="the keyword is the only filter", citations=[KEYWORD_CITATION],
                           expected_behaviour="Rate plans match whatever the channel is titled.")
        if stage == "merge":
            return MergeOutput(duplicate_sets=[DuplicateSet(
                finding_ids=[high_finding().id, reworded_finding().id], reason="the same literal keyword")])
        if stage == "assess":
            return self.assess_answer or AssessOutput()
        if stage == "repeats":
            return self.repeats_answer or RepeatsOutput()
        raise AssertionError(budget_label)


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> FakeAgents:
        fake = FakeAgents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake
    return install


def _findings_line(out: str) -> list[Path]:
    """The `findings:` line, which is followed by the final `checks:` line."""
    *_, line, last = out.strip().splitlines()
    assert line.startswith("findings: ")
    findings = [Path(p) for p in line[len("findings: "):].split()]
    assert last == "checks: " + " ".join(str(p.parent / "checks.json") for p in findings)
    return findings


def _review_args(repo, base, head, out, *extra):
    return ["--session-id", "sess-test", "--out", str(out), "review", "--repo", str(repo),
            "--range", f"{base}..{head}", "--ticket", "DB-2038", "--trust-repo", *extra]


def test_review_end_to_end(fixture_repo, tmp_path, agents, capsys):
    import jsonschema

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    stdout = capsys.readouterr().out
    (path,) = _findings_line(stdout)
    target_dir = out / f"booking-engine--{base}..{head}"
    assert path == target_dir / "findings.json"
    data = json.loads(path.read_text())
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    assert [f["status"] for f in data["findings"]] == ["confirmed"]
    assert f"summary: {target_dir / f'summary-booking-engine--{base}..{head}.md'}" in stdout.splitlines()
    for name in (f"review-booking-engine--{base}..{head}.md", f"summary-booking-engine--{base}..{head}.md",
                 "review-state.json", "progress.log", "diff.patch"):
        assert (target_dir / name).is_file(), name
    assert not (target_dir / "tree").exists()  # the snapshot is removed when done
    for rollup in ("HIGH-only.md", "MEDIUM-only.md", "LOW-only.md"):
        assert (out / rollup).is_file()
    progress = (target_dir / "progress.log").read_text()
    assert "STARTED" in progress and "DONE review finished: Code 1 HIGH" in progress
    assert ReviewState.model_validate_json((target_dir / "review-state.json").read_text()).stage == "done"
    assert "verify done: 2 confirmed, 0 refuted, 0 unverifiable" in stdout
    assert "merge done: 1 groups, 1 merged, 0 agent failures" in stdout
    assert data["findings"][0]["severity"] == "HIGH" and len(data["findings"]) == 1
    review = (target_dir / f"review-booking-engine--{base}..{head}.md").read_text()
    assert "**Reported by:** diff-bugs, callers" in review
    assert f"Merged into `{high_finding().id}`: the same defect as {high_finding().id}" in review
    assert "merge rateplan_service.py:1-1: 2 findings, 1 duplicates" in progress
    assert data["findings"][0]["expected_behaviour"] == "Rate plans match whatever the channel is titled."
    assert "**Expected behaviour:** Rate plans match whatever the channel is titled." in review


def test_resume_after_a_kill_following_verify(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    fake = agents(kill_at="merge")
    with pytest.raises(RuntimeError, match="killed"):
        main(_review_args(repo, base, head, out))
    target_dir = out / f"booking-engine--{base}..{head}"
    state = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert state.stage == "verify" and not (target_dir / "findings.json").exists()
    first_run = list(fake.calls)
    capsys.readouterr()

    assert main(["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo"]) == 0
    (path,) = _findings_line(capsys.readouterr().out)
    assert path == target_dir / "findings.json" and path.is_file()
    resumed = fake.calls[len(first_run):]
    assert not [c for c in resumed if c.startswith(("find", "verify"))]  # finished stages are not repeated
    assert resumed == ["merge"]
    assert "RESUMED after verify" in (target_dir / "progress.log").read_text()


def test_batch_reviews_every_target_and_writes_rollups(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    batch_file = tmp_path / "batch.yaml"
    batch_file.write_text(
        f"- repo: {repo}\n  range: {base}..{head}\n  tickets: [DB-2038]\n"
        f"- repo: {repo}\n  branch: feature/db-2038\n  deployed_in: main\n"
    )
    agents()
    assert main(["--out", str(out), "batch", str(batch_file), "--parallel", "2", "--trust-repo"]) == 0
    paths = _findings_line(capsys.readouterr().out)
    assert [p.parent.name for p in paths] == [f"booking-engine--{base}..{head}", "booking-engine--feature_db-2038"]
    high = (out / "HIGH-only.md").read_text()
    assert high.startswith("# HIGH findings — 2 across 2 of 2 targets")
    review = (out / "booking-engine--feature_db-2038" / "review-booking-engine--feature_db-2038.md").read_text()
    assert "- **Deployed in main:** no" in review


def test_untrusted_repo_exits_3_without_output(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    args = [a for a in _review_args(repo, base, head, out) if a != "--trust-repo"]
    assert main(args) == 3
    assert "--trust-repo" in capsys.readouterr().err
    assert not out.exists()


def test_failed_target_gate_exits_2_with_no_output_directory(fixture_repo, tmp_path, agents, capsys):
    repo, _, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, head, head, out)) == 2
    assert "empty" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_budget_breaker_stops_the_run_with_exit_4(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents(cost=6.0)
    assert main(_review_args(repo, base, head, out, "--max-cost-usd", "10")) == 4
    err = capsys.readouterr().err
    assert "circuit breaker stopped the run" in err
    target_dir = out / f"booking-engine--{base}..{head}"
    state = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert state.budget["cost_usd"] >= 10 and not (target_dir / "findings.json").exists()
    assert "FAILED circuit breaker" in (target_dir / "progress.log").read_text()


def _stop_then_resume(fixture_repo, tmp_path, agents, capsys, stop_at: int, **kwargs):
    """Stop the review with a budget error on call *stop_at*, then resume it.

    Returns (the state saved at the stop, the calls made before it, the calls the resume made)."""
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    fake = agents(budget_stop_at=stop_at, **kwargs)
    assert main(_review_args(repo, base, head, out)) == 4
    target_dir = out / f"booking-engine--{base}..{head}"
    saved = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    before, first_run = fake.calls[:stop_at - 1], len(fake.calls)
    capsys.readouterr()
    assert main(["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo"]) == 0
    assert (target_dir / "findings.json").is_file()
    return saved, before, fake.calls[first_run:]


def test_a_budget_stop_during_find_keeps_finished_finders_and_resume_runs_the_rest(
        fixture_repo, tmp_path, agents, capsys):
    saved, before, resumed = _stop_then_resume(fixture_repo, tmp_path, agents, capsys, stop_at=3)
    assert before == ["find:diff-bugs", "find:callers"]
    # Which finders after the stopped one also finish depends on when the
    # task group cancels them; the stopped one never has a result.
    assert saved.stage == "target" and saved.findings == []
    assert "history" not in saved.finder_results
    assert [f.id for f in saved.finder_results["diff-bugs"].findings] == [high_finding().id]
    missing = [f"find:{d}" for d in DIMENSIONS if d not in saved.finder_results]
    assert resumed == missing + ["verify", "verify", "merge"]


def test_a_budget_stop_during_verify_keeps_the_verdicts_and_resume_asks_only_the_rest(
        fixture_repo, tmp_path, agents, capsys):
    saved, before, resumed = _stop_then_resume(fixture_repo, tmp_path, agents, capsys, stop_at=7)
    assert before[-1] == "verify" and len(before) == 6
    assert saved.stage == "find" and list(saved.verdicts) == [high_finding().id]
    assert resumed == ["verify", "merge"]


def test_a_budget_stop_during_merge_keeps_the_answers_and_resume_asks_only_the_rest(
        fixture_repo, tmp_path, agents, capsys):
    # 5 finders, 4 verifiers, then one merge call per group: stop on the second.
    saved, before, resumed = _stop_then_resume(fixture_repo, tmp_path, agents, capsys, stop_at=11,
                                               two_groups=True)
    assert before[-1] == "merge" and before.count("merge") == 1
    assert saved.stage == "verify" and len(saved.merge_answers) == 1
    assert resumed == ["merge", "assess"]  # three findings remain, so assess runs once


def test_rollup_subcommand(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    for p in out.glob("*-only.md"):
        p.unlink()
    capsys.readouterr()
    assert main(["--out", str(out), "rollup"]) == 0
    assert (out / "HIGH-only.md").is_file()
    assert capsys.readouterr().out.startswith("rollups: ")


def test_out_and_session_id_are_accepted_after_the_subcommand(fixture_repo, tmp_path, agents, capsys):
    """The plan's acceptance command passes --out after `review`."""
    repo, base, head = fixture_repo
    out = tmp_path / "late-out"
    agents()
    assert main(["review", "--repo", str(repo), "--range", f"{base}..{head}", "--trust-repo",
                 "--out", str(out), "--session-id", "sess-late"]) == 0
    (path,) = _findings_line(capsys.readouterr().out)
    assert path.parent.parent == out


def test_global_flags_are_not_overwritten_when_absent_after_the_subcommand():
    from review_pipeline.__main__ import build_parser

    args = build_parser().parse_args(["--out", "/x", "--session-id", "s", "rollup"])
    assert (args.out, args.session_id) == ("/x", "s")


def test_default_out_is_the_workspace_inbox_else_home_never_cwd(tmp_path, monkeypatch):
    from review_pipeline.__main__ import default_out

    cfg, home, ran_from = tmp_path / "cfg", tmp_path / "home", tmp_path / "repo"
    for d in (cfg, home, ran_from):
        d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(ran_from)
    assert default_out() == home / ".multiplai" / "reviews"

    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (cfg / ".workspace").write_text(str(ws))
    assert default_out() == ws / "INBOX" / "reviews"


def test_a_resumed_run_adds_an_interval_instead_of_overwriting(fixture_repo, tmp_path, agents, capsys):
    saved, _, _ = _stop_then_resume(fixture_repo, tmp_path, agents, capsys, stop_at=7)
    assert len(saved.timings["run"]) == 1 and saved.timings["run"][0].ended_at  # closed at the stop
    assert saved.budget_stops == 1
    target_dir = tmp_path / "out" / f"booking-engine--{fixture_repo[1]}..{fixture_repo[2]}"
    state = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert len(state.timings["run"]) == 2 and all(i.ended_at for i in state.timings["run"])
    assert len(state.timings["verify"]) == 2  # stopped once, resumed once
    assert len(state.timings["find"]) == 1 and len(state.timings["merge"]) == 1
    assert {f"find:{d}" for d in DIMENSIONS} <= set(state.timings)
    assert state.timings["run"][1].started_at >= state.timings["run"][0].ended_at


def test_run_config_records_session_default_for_unset_models(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("verifier_model: claude-x\nconcurrency: 2\n")
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    state = ReviewState.model_validate_json(
        (out / f"booking-engine--{base}..{head}" / "review-state.json").read_text())
    assert state.run_config["stages"]["find"] == {"model": "session default", "effort": "session default"}
    assert state.run_config["stages"]["verify"]["model"] == "claude-x"
    assert state.run_config["stages"]["merge"]["model"] == "claude-x"
    assert state.run_config["concurrency"] == 2


def test_rollup_writes_runs_jsonl_and_counts_files_without_run(fixture_repo, tmp_path, agents, capsys):
    import shutil
    import subprocess

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents(cost=0.75)
    assert main(_review_args(repo, base, head, out)) == 0
    new = out / f"booking-engine--{base}..{head}" / "findings.json"
    old = out / "older-review" / "findings.json"
    old.parent.mkdir()
    data = json.loads(new.read_text())
    expected_cost = data["run"]["cost_usd"]
    del data["run"]  # as written before multiplai-dev 0.28
    old.write_text(json.dumps(data))
    capsys.readouterr()

    assert main(["--out", str(out), "rollup"]) == 0
    stdout = capsys.readouterr().out
    assert "(1 review, 1 skipped: no run recorded)" in stdout
    lines = (out / "runs.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["target"] == {"label": data["target"]["label"], "slug": data["target"]["slug"],
                             "head_sha": head}
    assert set(row) == {"target", "generated_at", "producer", "run"}
    assert row["run"]["cost_usd"] == expected_cost == pytest.approx(0.75 * row["run"]["calls"])
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    total = subprocess.run(["jq", "-s", "map(.run.cost_usd) | add", str(out / "runs.jsonl")],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert float(total) == pytest.approx(expected_cost)


def test_batch_keeps_runs_jsonl_lines_of_earlier_reviews(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    earlier = out / "earlier-review" / "findings.json"
    earlier.parent.mkdir()
    data = json.loads((out / f"booking-engine--{base}..{head}" / "findings.json").read_text())
    data["target"] = {**data["target"], "slug": "earlier-review", "label": "earlier"}
    earlier.write_text(json.dumps(data))
    batch_file = tmp_path / "batch.yaml"
    batch_file.write_text(f"- repo: {repo}\n  branch: feature/db-2038\n")
    capsys.readouterr()

    assert main(["--out", str(out), "batch", str(batch_file), "--trust-repo"]) == 0
    slugs = sorted(json.loads(line)["target"]["slug"] for line in (out / "runs.jsonl").read_text().splitlines())
    assert slugs == sorted([f"booking-engine--{base}..{head}", "booking-engine--feature_db-2038", "earlier-review"])


def test_calls_with_no_usage_leave_one_budget_line_that_a_resume_replaces(target_info):
    from review_pipeline.orchestrator import note_missing_usage

    ledger = budget.ReviewBudget(max_usd=10)
    state = ReviewState(target=target_info)
    state.errors = ["verify: something else"]
    note_missing_usage(state, ledger)
    assert state.errors == ["verify: something else"]

    ledger.record(None, label="find")
    note_missing_usage(state, ledger)
    assert state.errors == ["verify: something else",
                            "budget: 1 agent call returned no usage; their cost and tokens are counted as 0"]

    ledger.record(SimpleNamespace(), label="verify")  # a resume: the ledger carries on counting
    note_missing_usage(state, ledger)
    budget_lines = [e for e in state.errors if e.startswith("budget:")]
    assert budget_lines == ["budget: 2 agent calls returned no usage; their cost and tokens are counted as 0"]
    assert state.errors[0] == "verify: something else"

# --- rounds ----------------------------------------------------------------------


def _fake_round(target_dir: Path, head_sha: str) -> None:
    """A findings.json and review markdown as an earlier round on *head_sha* left them."""
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "findings.json").write_text(json.dumps(
        {"schema_version": 1, "generated_at": "2026-01-01T00:00:00Z", "producer": "test",
         "target": {"head_sha": head_sha}, "findings": []}))
    (target_dir / f"review-{target_dir.name}.md").write_text("# earlier round\n")


def test_a_run_on_a_new_head_keeps_the_earlier_round(fixture_repo, tmp_path, agents):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / f"booking-engine--{base}..{head}"
    old_head = "a" * 40
    _fake_round(target_dir, old_head)
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    kept = target_dir / "rounds" / old_head[:12]
    assert json.loads((kept / "findings.json").read_text())["target"]["head_sha"] == old_head
    assert (kept / f"review-{target_dir.name}.md").read_text() == "# earlier round\n"
    assert json.loads((target_dir / "findings.json").read_text())["target"]["head_sha"] == head


def test_a_resume_keeps_no_round_and_a_rerun_on_the_same_head_keeps_one(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / f"booking-engine--{base}..{head}"
    agents(kill_at="merge")
    with pytest.raises(RuntimeError):
        main(_review_args(repo, base, head, out))
    assert main(["resume", str(target_dir), "--trust-repo"]) == 0
    assert not (target_dir / "rounds").exists()
    first = json.loads((target_dir / "findings.json").read_text())["generated_at"]
    assert main(_review_args(repo, base, head, out)) == 0  # a second full run on the same head
    (kept,) = (target_dir / "rounds").iterdir()
    assert kept.name == head[:12]
    assert json.loads((kept / "findings.json").read_text())["generated_at"] == first


def test_mode_critical_is_recorded_and_kept_by_resume(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / f"booking-engine--{base}..{head}"
    agents(kill_at="merge")
    with pytest.raises(RuntimeError):
        main(_review_args(repo, base, head, out, "--mode", "critical"))
    assert main(["resume", str(target_dir), "--trust-repo"]) == 0
    data = json.loads((target_dir / "findings.json").read_text())
    assert data["mode"] == "critical"
    assert "Critical mode" in next(target_dir.glob("summary-*.md")).read_text()


def test_a_verifier_that_never_answers_stops_the_run_with_exit_5(fixture_repo, tmp_path, agents, monkeypatch, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    fake = agents()
    real = fake.__call__

    async def failing(prompt, schema, *, budget_label="", **kwargs):
        if budget_label == "verify":
            raise sdk.AgentCallError("verify: no valid answer after a re-ask")
        return await real(prompt, schema, budget_label=budget_label, **kwargs)

    monkeypatch.setattr(sdk, "agent_call_structured", failing)
    assert main(_review_args(repo, base, head, out)) == 5
    err = capsys.readouterr().err
    assert "STOPPED: the verifier gave no usable answer" in err and "after 3 tries each" in err
    assert "resume" in err
    target_dir = out / f"booking-engine--{base}..{head}"
    assert not (target_dir / "findings.json").exists()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    assert main(["resume", str(target_dir), "--trust-repo"]) == 0
    assert json.loads((target_dir / "findings.json").read_text())["findings"][0]["impact"] == "breaks-users"
