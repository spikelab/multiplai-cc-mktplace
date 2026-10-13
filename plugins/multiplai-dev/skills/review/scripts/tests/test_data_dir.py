"""Review output in the data directory: stable slugs, every round kept, the ledger, migrate.

Every test writes under its own tmp_path: conftest points `ledger.data_dir` there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import REAL_DATA_DIR, REAL_LEGACY_REVIEWS_ROOT, TESTS
from fixture_repo import _git as fixture_git
from review_pipeline import ledger, render, rounds, sdk, target
from review_pipeline.__main__ import default_out, main
from test_orchestrator import FakeAgents, _review_args

ORIGIN = "https://github.com/spikelab/github-tests.git"
SLUG_REPO = "spikelab-github-tests"


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> FakeAgents:
        fake = FakeAgents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake
    return install


def _ledger_lines(tmp_path: Path, event: str | None = None) -> list[dict]:
    path = tmp_path / "data" / "reviews" / "ledger.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in lines if event is None or e["event"] == event]


def _clone(fixture_repo, dest: Path, origin: str = ORIGIN) -> Path:
    src, _, _ = fixture_repo
    subprocess.run(["git", "clone", "-q", str(src), str(dest)], check=True, capture_output=True)
    fixture_git(dest, "remote", "set-url", "origin", origin)
    return dest


# --- 1. the default output directory ---------------------------------------------


def test_default_out_is_the_data_dirs_reviews_never_the_inbox_or_cwd(tmp_path, monkeypatch):
    import multiplai_core.paths

    monkeypatch.setattr(ledger, "data_dir", REAL_DATA_DIR)
    monkeypatch.setattr(multiplai_core.paths, "get_paths", lambda: SimpleNamespace(data_dir=tmp_path / "d"))
    cfg, ws, ran_from = tmp_path / "cfg", tmp_path / "ws", tmp_path / "repo"
    (ws / "INBOX").mkdir(parents=True)
    cfg.mkdir()
    ran_from.mkdir()
    (cfg / ".workspace").write_text(str(ws))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(ran_from)
    assert default_out() == tmp_path / "d" / "reviews"


def test_a_review_without_out_writes_under_the_data_dir(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    agents()
    assert main(["review", "--repo", str(repo), "--range", f"{base}..{head}", "--trust-repo"]) == 0
    stdout = capsys.readouterr().out
    reviews = tmp_path / "data" / "reviews"
    assert stdout.splitlines()[0] == f"out: {reviews}"
    assert (reviews / "booking-engine" / f"range-{base}..{head}" / "findings.json").is_file()


# --- 2. the slug comes from the origin remote --------------------------------------


@pytest.mark.parametrize("url", [
    "https://github.com/spikelab/github-tests.git",
    "https://github.com/spikelab/github-tests",
    "git@github.com:spikelab/github-tests.git",
    "ssh://git@github.com/spikelab/github-tests.git/",
    "https://token@github.com/spikelab/github-tests.git",
])
def test_origin_name_is_owner_dash_repo(url):
    assert target.origin_name(url) == SLUG_REPO


def test_origin_name_needs_an_owner_and_a_repo():
    assert target.origin_name(None) is None
    assert target.origin_name("") is None
    assert target.origin_name("repo.git") is None


def test_two_checkouts_with_one_origin_and_different_names_give_one_slug(fixture_repo, tmp_path, monkeypatch):
    _, base, head = fixture_repo
    a = _clone(fixture_repo, tmp_path / "multiplai-cc-mktplace")
    b = tmp_path / "buildme-mutation-testing"
    fixture_git(a, "worktree", "add", "-q", "--detach", str(b), head)

    def slug(repo: Path, **kw) -> str:
        return target.build_target(target.resolve(repo, **kw)).slug

    assert slug(a, range_=f"{base}..{head}") == slug(b, range_=f"{base}..{head}") == f"{SLUG_REPO}--{base}..{head}"
    assert slug(a, tree=head) == slug(b, tree=head) == f"{SLUG_REPO}--tree"

    def fake_view(repo_path, number):
        return {"headRefOid": head, "baseRefOid": base, "headRefName": "feature", "baseRefName": "main",
                "title": "t", "body": ""}

    monkeypatch.setattr(target, "_pr_view", fake_view)
    assert slug(a, pr=283) == slug(b, pr=283) == f"{SLUG_REPO}--pr-283"
    # The label keeps the short directory name.
    assert target.build_target(target.resolve(b, pr=283)).label == "buildme-mutation-testing PR #283"


def test_no_origin_falls_back_to_the_directory_name_and_logs_it(fixture_repo, tmp_path, caplog):
    _, base, head = fixture_repo
    repo = _clone(fixture_repo, tmp_path / "local-only")
    fixture_git(repo, "remote", "remove", "origin")
    with caplog.at_level(logging.INFO, logger="review_pipeline.target"):
        info = target.build_target(target.resolve(repo, range_=f"{base}..{head}"))
    assert info.slug == f"local-only--{base}..{head}"
    assert any("no origin remote" in r.getMessage() for r in caplog.records)


# --- 3. every round kept in full, numbered ----------------------------------------


@pytest.fixture
def branch_clone(fixture_repo, tmp_path):
    """A clone whose origin/feature/x is at head; returns (repo, head, a second head on top of it)."""
    _, base, head = fixture_repo
    repo = _clone(fixture_repo, tmp_path / "checkout")
    fixture_git(repo, "update-ref", "refs/remotes/origin/main", base)
    fixture_git(repo, "update-ref", "refs/remotes/origin/feature/x", head)
    fixture_git(repo, "checkout", "-q", "-b", "next", head)
    (repo / "notes.txt").write_text("a second commit\n")
    fixture_git(repo, "add", "notes.txt")
    fixture_git(repo, "commit", "-q", "-m", "second head", date="2026-09-17T12:00:00Z")
    return repo, head, fixture_git(repo, "rev-parse", "HEAD")


def test_four_runs_on_two_heads_keep_three_numbered_rounds_in_full(branch_clone, tmp_path, agents):
    repo, head1, head2 = branch_clone
    out = tmp_path / "out"
    args = ["--out", str(out), "review", "--repo", str(repo), "--branch", "feature/x", "--trust-repo"]
    target_dir = out / "github-tests" / "branch-feature_x"
    agents()
    assert main(args) == 0
    (target_dir / "viewer").mkdir()
    (target_dir / "viewer" / "decisions.json").write_text("{}")
    assert main(args) == 0  # a rerun on the same head
    fixture_git(repo, "update-ref", "refs/remotes/origin/feature/x", head2)
    assert main(args) == 0
    assert main(args) == 0

    names = sorted(p.name for p in (target_dir / "rounds").iterdir())
    assert names == [f"01-{head1[:12]}", f"02-{head1[:12]}", f"03-{head2[:12]}"]
    logs = []
    for name in names:
        kept = target_dir / "rounds" / name
        for f in ("progress.log", "review-state.json", "diff.patch", "commits.txt", "files.txt", "findings.json",
                  "checks.json"):
            assert (kept / f).is_file(), (name, f)
        for d in ("viewer", "tree", "rounds"):
            assert not (kept / d).exists(), (name, d)
        state = json.loads((kept / "review-state.json").read_text())
        assert state["target"]["head_sha"] == json.loads((kept / "findings.json").read_text())["target"]["head_sha"]
        logs.append((kept / "progress.log").read_text())
    assert len(set(logs)) == 3  # each round keeps its own run's progress, not the next one's
    # The ledger numbers the runs the same way.
    assert [e["round"] for e in _ledger_lines(tmp_path, "finished")] == ["01", "02", "03", "04"]


def test_a_run_stopped_before_export_is_kept_as_its_own_round(fixture_repo, tmp_path, agents):
    """A budget stop writes no findings.json; the next run still keeps that run's files, not its own."""
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0  # round 01 finishes
    agents(budget_stop_at=2)
    assert main(_review_args(repo, base, head, out)) == 4  # round 02 stops before export
    stopped_log = (target_dir / "progress.log").read_text()
    stopped_state = (target_dir / "review-state.json").read_text()
    agents()
    assert main(_review_args(repo, base, head, out)) == 0  # round 03 must not write over round 02
    names = sorted(p.name for p in (target_dir / "rounds").iterdir())
    assert names == [f"01-{head[:12]}", f"02-{head[:12]}"]
    second = target_dir / "rounds" / names[1]
    assert (second / "progress.log").read_text() == stopped_log
    assert (second / "review-state.json").read_text() == stopped_state
    assert (second / "diff.patch").is_file()
    # The stopped run exported nothing: round 01's findings are not copied in as round 02's.
    assert not (second / "findings.json").exists() and not (second / "checks.json").exists()
    assert not list(second.glob("review-*.md"))
    # Its ledger number is the round it is kept as; the finished run after it is 03.
    assert [e["round"] for e in _ledger_lines(tmp_path, "finished")] == ["01", "02", "03"]
    # A fourth run keeps round 03 once, and round 02 is not copied again.
    assert main(_review_args(repo, base, head, out)) == 0
    names = sorted(p.name for p in (target_dir / "rounds").iterdir())
    assert names == [f"01-{head[:12]}", f"02-{head[:12]}", f"03-{head[:12]}"]
    assert (target_dir / "rounds" / names[2] / "findings.json").is_file()


def test_keep_round_keeps_a_stopped_run_once(tmp_path):
    """A stopped run (state, no findings.json of its own) is kept once, and not again on a second start."""
    h = "e" * 40
    state = {"target": {"head_sha": h, "slug": "t"}, "timings": {"run": [{"started_at": "2026-01-01T00:00:00.100Z"}]}}
    (tmp_path / "review-state.json").write_text(json.dumps(state))
    (tmp_path / "progress.log").write_text("stopped\n")
    first = rounds.keep_round(tmp_path, "t", h)
    assert first == tmp_path / "rounds" / f"01-{h[:12]}"
    assert (first / "progress.log").read_text() == "stopped\n"
    assert rounds.keep_round(tmp_path, "t", h) == first
    assert len(list((tmp_path / "rounds").iterdir())) == 1


def _write_findings(d: Path, head: str, generated_at: str, claim: str = "", run: dict | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": 1, "generated_at": generated_at, "producer": "test",
            "target": {"head_sha": head, "slug": "t", "label": "t"},
            "findings": [{"id": "abcdef0123", "status": "confirmed", "claim": claim}] if claim else []}
    if run is not None:
        data["run"] = run
    (d / "findings.json").write_text(json.dumps(data))


def test_keep_round_numbers_after_legacy_names_and_load_rounds_reads_them_all(tmp_path):
    a, b = "a" * 40, "b" * 40
    _write_findings(tmp_path / "rounds" / a[:12], a, "2026-01-01T00:00:00Z", "first")
    _write_findings(tmp_path / "rounds" / f"{a[:12]}-2", a, "2026-01-02T00:00:00Z", "second")
    _write_findings(tmp_path, b, "2026-01-03T00:00:00Z", "third")
    (tmp_path / "progress.log").write_text("the third run\n")
    kept = rounds.keep_round(tmp_path, "t", b)
    assert kept == tmp_path / "rounds" / f"03-{b[:12]}"
    assert (kept / "progress.log").read_text() == "the third run\n"
    assert rounds.keep_round(tmp_path, "t", b) == kept  # started again before it finished: one copy
    assert len(list((tmp_path / "rounds").iterdir())) == 3
    (e,) = rounds.load_rounds(tmp_path)
    assert e.finding["claim"] == "third" and e.head_sha == b


def test_two_runs_in_one_second_are_two_rounds(tmp_path):
    """`generated_at` has whole seconds; the run's start time (milliseconds) tells two quick runs apart."""
    h = "c" * 40
    _write_findings(tmp_path, h, "2026-01-01T00:00:00Z", "x", run={"started_at": "2026-01-01T00:00:00.100Z"})
    first = rounds.keep_round(tmp_path, "t", h)
    _write_findings(tmp_path, h, "2026-01-01T00:00:00Z", "x", run={"started_at": "2026-01-01T00:00:00.700Z"})
    second = rounds.keep_round(tmp_path, "t", h)
    assert (first.name, second.name) == (f"01-{h[:12]}", f"02-{h[:12]}")


def test_the_marker_round_id_stays_the_head_sha_with_a_count(tmp_path):
    """Directory names changed; the PR marker's `round` keeps its v1 form, so posted markers still parse."""
    h = "d" * 40
    _write_findings(tmp_path, h, "2026-01-01T00:00:00Z", "x")
    first = json.loads((tmp_path / "findings.json").read_text())
    assert rounds.round_name(tmp_path, first) == h[:12]
    rounds.keep_round(tmp_path, "t", h)
    assert rounds.round_name(tmp_path, first) == h[:12]  # the kept copy keeps its id
    _write_findings(tmp_path, h, "2026-01-02T00:00:00Z", "x")
    assert rounds.round_name(tmp_path, json.loads((tmp_path / "findings.json").read_text())) == f"{h[:12]}-2"


# --- 4. the append-only ledger ------------------------------------------------------


def test_two_runs_give_two_ledger_lines_and_a_deleted_target_dir_a_third(fixture_repo, tmp_path, agents):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    assert main(_review_args(repo, base, head, out)) == 0
    finished = _ledger_lines(tmp_path, "finished")
    assert len(finished) == 2
    first = finished[0]
    assert {k: first[k] for k in ("slug", "target_dir", "round", "head_sha", "base_sha", "kind", "pr", "stopped",
                                  "session_id")} == {
        "slug": f"example-booking-engine--{base}..{head}", "target_dir": str(target_dir), "round": "01", "head_sha": head, "base_sha": base,
        "kind": "range", "pr": None, "stopped": None, "session_id": "sess-test"}
    assert first["producer"].startswith("multiplai-dev:review ")
    assert "cost_usd" in first["run"] and first["generated_at"]
    assert finished[1]["round"] == "02"

    shutil.rmtree(target_dir)
    assert main(_review_args(repo, base, head, out)) == 0
    finished = _ledger_lines(tmp_path, "finished")
    assert len(finished) == 3 and finished[2]["round"] == "01"
    events = [e["event"] for e in _ledger_lines(tmp_path)]
    assert events == ["started", "finished"] * 3


def test_a_budget_stop_and_its_resume_each_end_with_a_ledger_line(fixture_repo, tmp_path, agents):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents(budget_stop_at=3)
    assert main(_review_args(repo, base, head, out)) == 4
    assert _ledger_lines(tmp_path, "finished")[-1]["stopped"] == "budget"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    assert main(["resume", str(target_dir), "--trust-repo"]) == 0
    finished = _ledger_lines(tmp_path, "finished")
    assert len(finished) == 2 and finished[-1]["stopped"] is None and finished[-1]["round"] == "01"


def test_each_target_of_a_batch_gets_a_ledger_line(fixture_repo, tmp_path, agents):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    batch_file = tmp_path / "targets.yaml"
    batch_file.write_text(f"- repo: {repo}\n  range: {base}..{head}\n- repo: {repo}\n  branch: feature/db-2038\n")
    agents()
    assert main(["--out", str(out), "batch", str(batch_file), "--trust-repo"]) == 0
    assert sorted(e["slug"] for e in _ledger_lines(tmp_path, "finished")) == sorted(
        [f"example-booking-engine--{base}..{head}", "example-booking-engine--feature_db-2038"])


def test_a_started_line_with_no_end_makes_a_plain_run_exit_4_and_restart_starts_over(
        fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    # A rerun on this head saved its state, wrote its `started` line, then died.
    ledger.started(slug=target_dir.name, target_dir=target_dir, head_sha=head, session_id="a run that died")
    state = (target_dir / "review-state.json").read_bytes()
    fake = agents()
    capsys.readouterr()
    assert main(_review_args(repo, base, head, out)) == 4
    err = capsys.readouterr().err
    assert f"interrupted run found; continue with: python -m review_pipeline resume {target_dir}" in err
    assert fake.calls == [] and (target_dir / "review-state.json").read_bytes() == state
    assert main(_review_args(repo, base, head, out, "--restart")) == 0
    assert _ledger_lines(tmp_path, "finished")[-1]["stopped"] is None


def _break_state(target_dir: Path, how: str) -> None:
    if how == "dir-deleted":
        shutil.rmtree(target_dir)
    elif how == "state-deleted":
        (target_dir / "review-state.json").unlink()
    else:
        (target_dir / "review-state.json").write_text("{not json")


@pytest.mark.parametrize("how", ["dir-deleted", "state-deleted", "state-unreadable"])
def test_a_started_line_with_no_saved_state_left_starts_a_fresh_run_that_closes_it(
        fixture_repo, tmp_path, agents, how):
    """`resume` could only fail there (no readable review-state.json), so a plain run starts over instead."""
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    ledger.started(slug=target_dir.name, target_dir=target_dir, head_sha=head, session_id="a run that died")
    _break_state(target_dir, how)
    fake = agents()
    assert main(_review_args(repo, base, head, out)) == 0
    assert fake.calls
    assert _ledger_lines(tmp_path)[-1]["event"] == "finished" and not ledger.interrupted(target_dir, head)
    assert main(_review_args(repo, base, head, out)) == 0


def test_a_started_line_for_a_head_the_saved_state_is_not_on_starts_a_fresh_run(branch_clone, tmp_path, agents):
    """The directory's state is another head's: resuming it would finish a review of the wrong commit."""
    repo, _, head2 = branch_clone
    out = tmp_path / "out"
    args = ["--out", str(out), "review", "--repo", str(repo), "--branch", "feature/x", "--trust-repo"]
    target_dir = out / "github-tests" / "branch-feature_x"
    agents()
    assert main(args) == 0
    # A run on head2 died earlier; the directory now holds head1's finished run.
    ledger.started(slug=target_dir.name, target_dir=target_dir, head_sha=head2, session_id="a run that died")
    fixture_git(repo, "update-ref", "refs/remotes/origin/feature/x", head2)
    fake = agents()
    assert main(args) == 0
    assert fake.calls
    assert json.loads((target_dir / "findings.json").read_text())["target"]["head_sha"] == head2
    assert not ledger.interrupted(target_dir, head2)


def test_a_review_running_now_is_not_read_as_interrupted_and_nothing_starts_a_second_run_in_its_directory(
        fixture_repo, tmp_path, agents, capsys):
    """A running review leaves the same `started` line as a killed one. While a
    process holds the target's run lock, `review`, `review --restart` and
    `resume` all refuse (exit 2) and write nothing; once it lets go, the same
    ledger line reads as interrupted again (exit 4)."""
    from review_pipeline import runlock

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    # A rerun on this head has saved its state and written its `started` line, and is still going.
    ledger.started(slug=target_dir.name, target_dir=target_dir, head_sha=head, session_id="a run still going")
    state = (target_dir / "review-state.json").read_bytes()
    fake = agents()
    with runlock.hold(target_dir):
        for argv in (_review_args(repo, base, head, out), _review_args(repo, base, head, out, "--restart"),
                     ["resume", str(target_dir), "--trust-repo"]):
            capsys.readouterr()
            assert main(argv) == 2, argv
            err = capsys.readouterr().err
            assert f"a review of {target_dir} is running now (pid {os.getpid()} on " in err, err
            assert "interrupted run found" not in err
    assert fake.calls == [] and (target_dir / "review-state.json").read_bytes() == state
    assert [e["event"] for e in _ledger_lines(tmp_path)] == ["started", "finished", "started"]

    capsys.readouterr()
    assert main(_review_args(repo, base, head, out)) == 4
    assert "interrupted run found" in capsys.readouterr().err


def test_two_targets_of_a_batch_in_one_directory_do_not_run_at_once(fixture_repo, tmp_path, agents, capsys):
    """The second of two batch entries naming the same target fails while the first holds its
    directory, and says it is running, not that it was interrupted."""
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    out.mkdir()
    batch_file = tmp_path / "batch.yaml"
    entry = {"repo": str(repo), "range": f"{base}..{head}"}
    batch_file.write_text(json.dumps([entry, entry]))
    agents()
    assert main(["--out", str(out), "batch", str(batch_file), "--trust-repo", "--parallel", "2"]) == 1
    printed = capsys.readouterr().out
    assert "is running now" in printed and "interrupted run found" not in printed, printed
    assert len(_ledger_lines(tmp_path, "started")) == 1


@pytest.mark.parametrize("stop", [KeyboardInterrupt, asyncio.CancelledError, SystemExit],
                         ids=["KeyboardInterrupt", "CancelledError", "SystemExit"])
def test_a_run_stopped_by_the_user_or_the_process_writes_no_end_line(fixture_repo, tmp_path, monkeypatch, stop):
    """Ctrl-C, a cancelled task or an exiting process is an interrupted run, not a finished one."""
    from conftest import high_finding
    from review_pipeline.models import FinderOutput

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"

    async def fake(prompt, schema, *, budget_label="", **kwargs):
        if budget_label == "find:diff-bugs":
            return FinderOutput(findings=[high_finding()])
        if budget_label.startswith("find:"):
            return FinderOutput()
        raise stop()

    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    with pytest.raises(stop):
        main(_review_args(repo, base, head, out))
    assert [e["event"] for e in _ledger_lines(tmp_path)] == ["started"]
    assert ledger.interrupted(target_dir, head)


def test_the_started_line_comes_after_the_runs_state_is_saved(fixture_repo, tmp_path, agents, monkeypatch):
    """A run killed right after its `started` line must not leave the last round's state for `resume`."""
    from review_pipeline import orchestrator

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    seen = []

    def started(**kwargs):
        seen.append(json.loads((target_dir / "review-state.json").read_text())["stage"])
        raise KeyboardInterrupt

    agents()
    assert main(_review_args(repo, base, head, out)) == 0  # the last round: its state says "done"
    monkeypatch.setattr(orchestrator.review_ledger, "started", started)
    with pytest.raises(KeyboardInterrupt):
        main(_review_args(repo, base, head, out))
    assert seen == ["target"]


DRIVER = """
import asyncio, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from conftest import high_finding
from review_pipeline import ledger, sdk
from review_pipeline.__main__ import main
from review_pipeline.models import FinderOutput

data = Path(sys.argv[2])
ledger.data_dir = lambda: data


async def fake(prompt, schema, *, budget_label="", **kwargs):
    sdk.require_trusted_repo()
    if budget_label == "find:diff-bugs":
        return FinderOutput(findings=[high_finding()])
    if budget_label.startswith("find:"):
        return FinderOutput()
    await asyncio.sleep(600)  # the verifier never answers; the test stops the run here


sdk.agent_call_structured = fake
sys.exit(main(sys.argv[3:]))
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_run_killed_after_find_leaves_a_started_line_and_resume_skips_find(fixture_repo, tmp_path, agents, capsys,
                                                                             sig):
    """Done means 8: SIGTERM once progress.log shows find finished; a plain run exits 4; resume skips find.

    SIGINT (Ctrl-C) cancels the run's task instead of killing the process: it
    must still read as interrupted, not as a run that finished."""
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    args = _review_args(repo, base, head, out)
    proc = subprocess.Popen([sys.executable, str(driver), str(TESTS), str(tmp_path / "data"), *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        deadline = time.monotonic() + 120
        progress = target_dir / "progress.log"
        while not (progress.is_file() and "STAGE find: find done" in progress.read_text()):
            assert proc.poll() is None, proc.stderr.read()
            assert time.monotonic() < deadline, "find never finished"
            time.sleep(0.1)
        proc.send_signal(sig)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert [e["event"] for e in _ledger_lines(tmp_path)] == ["started"]
    assert ledger.interrupted(target_dir, head)

    fake = agents()
    capsys.readouterr()
    assert main(args) == 4
    assert (f"interrupted run found; continue with: python -m review_pipeline resume {target_dir}"
            in capsys.readouterr().err)
    assert fake.calls == []

    assert main(["resume", str(target_dir), "--trust-repo"]) == 0
    assert fake.calls and not [c for c in fake.calls if c.startswith("find")]
    log = progress.read_text()
    assert "RESUMED after find" in log and "STAGE find" not in log.split("RESUMED after find", 1)[1]
    assert [e["event"] for e in _ledger_lines(tmp_path)] == ["started", "started", "finished"]
    assert not ledger.interrupted(target_dir, head)


# --- 5. runs.jsonl keeps every round ------------------------------------------------


def test_write_runs_lists_earlier_rounds_sorted_by_generated_at(tmp_path):
    out = tmp_path / "out"
    run = {"cost_usd": 1.0, "started_at": "x"}
    _write_findings(out / "t", "a" * 40, "2026-01-03T00:00:00Z", run=dict(run, started_at="3"))
    _write_findings(out / "t" / "rounds" / "02-aaaaaaaaaaaa", "a" * 40, "2026-01-02T00:00:00Z",
                    run=dict(run, started_at="2"))
    _write_findings(out / "t" / "rounds" / "aaaaaaaaaaaa", "a" * 40, "2026-01-01T00:00:00Z",
                    run=dict(run, started_at="1"))
    _write_findings(out / "u", "b" * 40, "2026-01-02T12:00:00Z", run=dict(run, started_at="4"))
    _write_findings(out / "t" / "rounds" / "03-aaaaaaaaaaaa", "a" * 40, "2026-01-03T00:00:00Z",
                    run=dict(run, started_at="3"))  # the top-level round, kept by a run that then died
    path, n, skipped = render.write_runs(out)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert n == 4 and skipped == 0
    assert [line["generated_at"] for line in lines] == [
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-02T12:00:00Z", "2026-01-03T00:00:00Z"]


def test_runs_jsonl_lists_every_run_of_a_target_through_the_cli(fixture_repo, tmp_path, agents):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    for _ in range(3):
        assert main(_review_args(repo, base, head, out)) == 0
    lines = (out / "runs.jsonl").read_text().splitlines()
    assert len(lines) == 3


# --- 7. migrate ---------------------------------------------------------------------


def _legacy_reviews(root: Path) -> None:
    run = {"cost_usd": 2.5}
    t = {"slug": "repo--pr-1", "label": "repo PR #1", "head_sha": "e" * 40, "base_sha": "f" * 40, "kind": "pr",
         "pr": 1}
    d = root / "repo--pr-1"
    (d / "rounds" / "eeeeeeeeeeee").mkdir(parents=True)
    (d / "viewer").mkdir()
    (d / "findings.json").write_text(json.dumps({"generated_at": "2026-02-02T00:00:00Z", "producer": "p",
                                                  "target": t, "findings": [], "run": run}))
    (d / "rounds" / "eeeeeeeeeeee" / "findings.json").write_text(json.dumps(
        {"generated_at": "2026-02-01T00:00:00Z", "producer": "p", "target": t, "findings": []}))
    (d / "viewer" / "decisions.json").write_text("{}")
    (d / "progress.log").write_text("DONE\n")
    archived = root / "r2__repo--pr-1" / "1791-abc"
    archived.mkdir(parents=True)
    (archived / "findings.json").write_text(json.dumps({"generated_at": "2026-02-03T00:00:00Z", "target": t,
                                                         "findings": []}))
    (root / "HIGH-only.md").write_text("a rollup, not a review\n")


def _snapshot(root: Path) -> dict[str, tuple[bytes, float]]:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime) for p in sorted(root.rglob("*"))
            if p.is_file()}


def test_migrate_copies_appends_ledger_lines_leaves_the_source_and_skips_on_a_second_run(tmp_path, capsys):
    src = tmp_path / "INBOX" / "reviews"
    _legacy_reviews(src)
    before = _snapshot(src)
    listing = sorted(p.name for p in src.iterdir())

    assert main(["migrate", "--from", str(src)]) == 0
    legacy = tmp_path / "data" / "reviews" / "legacy"
    assert sorted(p.name for p in legacy.iterdir() if p.is_dir()) == ["r2__repo--pr-1", "repo--pr-1"]
    for rel in ("findings.json", "rounds/eeeeeeeeeeee/findings.json", "viewer/decisions.json", "progress.log"):
        assert (legacy / "repo--pr-1" / rel).read_bytes() == (src / "repo--pr-1" / rel).read_bytes()
    assert (legacy / "r2__repo--pr-1" / "1791-abc" / "findings.json").is_file()
    assert not (legacy / "HIGH-only.md").exists()

    migrated = _ledger_lines(tmp_path, "migrated")
    assert len(migrated) == 3
    by_from = {e["migrated_from"]: e for e in migrated}
    assert set(by_from) == {str(src / "repo--pr-1"), str(src / "repo--pr-1" / "rounds" / "eeeeeeeeeeee"),
                            str(src / "r2__repo--pr-1" / "1791-abc")}
    top = by_from[str(src / "repo--pr-1")]
    assert (top["slug"], top["head_sha"], top["kind"], top["pr"], top["run"], top["target_dir"]) == (
        "repo--pr-1", "e" * 40, "pr", 1, {"cost_usd": 2.5}, str(legacy / "repo--pr-1"))
    assert by_from[str(src / "repo--pr-1" / "rounds" / "eeeeeeeeeeee")]["round"] == "eeeeeeeeeeee"
    assert _snapshot(src) == before and sorted(p.name for p in src.iterdir()) == listing

    capsys.readouterr()
    assert main(["migrate", "--from", str(src)]) == 0
    assert "copied 0" in capsys.readouterr().out
    assert len(_ledger_lines(tmp_path, "migrated")) == 3
    assert _snapshot(src) == before


def test_migrate_keeps_both_of_two_sources_with_one_directory_name(tmp_path, capsys):
    """INBOX/reviews and an archive can both hold `repo--pr-1`; the second is not taken for the first."""
    first, second = tmp_path / "INBOX" / "reviews", tmp_path / "archive"
    _legacy_reviews(first)
    _legacy_reviews(second)
    (second / "repo--pr-1" / "progress.log").write_text("the archived copy\n")
    assert main(["migrate", "--from", str(first)]) == 0
    assert main(["migrate", "--from", str(second)]) == 0
    legacy = tmp_path / "data" / "reviews" / "legacy"
    dirs = sorted(p.name for p in legacy.iterdir() if p.is_dir())
    assert len(dirs) == 4 and "repo--pr-1" in dirs
    (other,) = [d for d in dirs if d.startswith("repo--pr-1~")]
    assert (legacy / other / "progress.log").read_text() == "the archived copy\n"
    assert (legacy / "repo--pr-1" / "progress.log").read_text() == "DONE\n"
    lines = len(_ledger_lines(tmp_path, "migrated"))
    capsys.readouterr()
    assert main(["migrate", "--from", str(second)]) == 0
    assert main(["migrate", "--from", str(first)]) == 0
    assert capsys.readouterr().out.count("copied 0") == 2
    assert len(_ledger_lines(tmp_path, "migrated")) == lines


class _Killed(BaseException):
    """A migrate run killed between two writes."""


@pytest.mark.parametrize("at", [1, 2, 3], ids=["before-any-line", "after-one-dir", "mid-dir"])
def test_migrate_stopped_part_way_finishes_on_a_rerun_without_a_second_copy(tmp_path, monkeypatch, at):
    """A run stopped after a copy was renamed into place, at any ledger line, is completed by the
    next run: no `<name>~<hash>` copy, and every findings.json gets exactly one ledger line."""
    from review_pipeline import migrate as migrate_mod

    src = tmp_path / "INBOX" / "reviews"
    _legacy_reviews(src)
    real_append, calls = ledger.append, []

    def append(entry):
        calls.append(entry)
        if len(calls) == at:
            raise _Killed
        real_append(entry)

    monkeypatch.setattr(migrate_mod.ledger, "append", append)
    with pytest.raises(_Killed):
        migrate_mod.migrate(src, tmp_path / "data" / "reviews")
    monkeypatch.setattr(migrate_mod.ledger, "append", real_append)

    assert main(["migrate", "--from", str(src)]) == 0
    legacy = tmp_path / "data" / "reviews" / "legacy"
    assert sorted(p.name for p in legacy.iterdir() if p.is_dir()) == ["r2__repo--pr-1", "repo--pr-1"]
    froms = sorted(e["migrated_from"] for e in _ledger_lines(tmp_path, "migrated"))
    assert froms == sorted([str(src / "repo--pr-1"), str(src / "repo--pr-1" / "rounds" / "eeeeeeeeeeee"),
                            str(src / "r2__repo--pr-1" / "1791-abc")])
    assert main(["migrate", "--from", str(src)]) == 0
    assert len(_ledger_lines(tmp_path, "migrated")) == 3
    assert sorted(p.name for p in legacy.iterdir() if p.is_dir()) == ["r2__repo--pr-1", "repo--pr-1"]


def test_migrate_refuses_a_source_that_is_not_a_directory(tmp_path, capsys):
    assert main(["migrate", "--from", str(tmp_path / "missing")]) == 2


# --- review.yaml written in the old default directory ------------------------------


def _old_workspace(tmp_path: Path, monkeypatch, yaml_text: str = "effort: low\n") -> Path:
    """A workspace whose INBOX/reviews (the default before 0.45) holds a review.yaml."""
    ws = tmp_path / "ws"
    old = ws / "INBOX" / "reviews"
    old.mkdir(parents=True)
    (old / "review.yaml").write_text(yaml_text)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".workspace").write_text(str(ws))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(ledger, "legacy_reviews_root", REAL_LEGACY_REVIEWS_ROOT)
    return old


def test_the_default_out_still_reads_a_review_yaml_left_in_the_old_default(tmp_path, monkeypatch, caplog):
    from review_pipeline.config import load_config
    old = _old_workspace(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert load_config(default_out()).effort == "low"
    assert str(old / "review.yaml") in caplog.text
    # One in the new place wins over the old one.
    default_out().mkdir(parents=True, exist_ok=True)
    (default_out() / "review.yaml").write_text("effort: high\n")
    assert load_config(default_out()).effort == "high"


def test_an_explicit_out_never_reads_the_old_default_review_yaml(tmp_path, monkeypatch):
    from review_pipeline.config import load_config
    _old_workspace(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert load_config(other).effort != "low"


def test_migrate_copies_the_sources_review_yaml_but_never_over_one_already_there(tmp_path, monkeypatch, capsys):
    old = _old_workspace(tmp_path, monkeypatch)
    assert main(["migrate", "--from", str(old)]) == 0
    assert (default_out() / "review.yaml").read_text() == "effort: low\n"
    assert (old / "review.yaml").read_text() == "effort: low\n"  # the source is left as it was
    (old / "review.yaml").write_text("effort: medium\n")
    (default_out() / "review.yaml").write_text("effort: high\n")
    assert main(["migrate", "--from", str(old)]) == 0
    assert (default_out() / "review.yaml").read_text() == "effort: high\n"


# --- the first review after the upgrade sees the PR's history under its old name ---


def _old_round(d: Path, head: str, generated_at: str, finding_id: str, remote: str = ORIGIN,
               slug: str = "checkout-x--pr-7") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "findings.json").write_text(json.dumps({
        "schema_version": 1, "generated_at": generated_at, "producer": "multiplai-dev:review 0.32.0",
        "target": {"head_sha": head, "slug": slug, "label": slug, "remote_url": remote},
        "findings": [{"id": finding_id, "status": "confirmed", "claim": f"claim {finding_id}"}]}))


def _new_state(slug: str = f"{SLUG_REPO}--pr-7", head: str = "f" * 40):
    from review_pipeline.models import ReviewState, TargetInfo
    return ReviewState(target=TargetInfo(repo_path="/r", remote_url=ORIGIN, base_sha="0" * 40, head_sha=head,
                                         slug=slug, kind="pr", ref="7", pr=7))


def test_a_prs_first_review_in_the_data_dir_reads_its_rounds_and_rejections_under_the_old_name(
        tmp_path, monkeypatch):
    from review_pipeline.stages import RunContext
    from review_pipeline.stages.repeats import earlier_findings
    old = _old_workspace(tmp_path, monkeypatch)
    a, b = "a" * 40, "b" * 40
    # The old directory: one kept round and its last (top-level) round, and the person's rejection.
    _old_round(old / "checkout-x--pr-7" / "rounds" / a[:12], a, "2026-01-01T00:00:00Z", "1111111111")
    _old_round(old / "checkout-x--pr-7", b, "2026-01-02T00:00:00Z", "2222222222")
    (old / "checkout-x--pr-7" / "viewer").mkdir()
    (old / "checkout-x--pr-7" / "viewer" / "decisions.json").write_text(json.dumps(
        {"2222222222": {"decision": "reject", "note": "by design", "ts": "2026-01-02T01:00:00Z"}}))
    # Another worktree's name for the same PR; migrate's copy of the first (read once, not twice).
    _old_round(old / "other-worktree--pr-7", a, "2026-01-01T12:00:00Z", "3333333333", slug="other-worktree--pr-7")
    shutil.copytree(old / "checkout-x--pr-7", default_out() / "legacy" / "checkout-x--pr-7")
    # Not this PR: another repository's PR 7, and this repository's PR 70.
    _old_round(old / "elsewhere--pr-7", a, "2026-01-01T00:00:00Z", "4444444444",
               remote="https://github.com/someone/else", slug="elsewhere--pr-7")
    _old_round(old / "checkout-x--pr-70", a, "2026-01-01T00:00:00Z", "5555555555", slug="checkout-x--pr-70")

    target_dir = default_out() / f"{SLUG_REPO}--pr-7"
    target_dir.mkdir(parents=True)
    ctx = RunContext(config=None, snapshot=None, diff="", target_dir=target_dir)
    earlier = earlier_findings(_new_state(), ctx)
    assert sorted(e.id for e in earlier) == ["1111111111", "2222222222", "3333333333"]
    by_id = {e.id: e for e in earlier}
    assert (by_id["2222222222"].decision, by_id["2222222222"].note) == ("reject", "by design")
    assert by_id["2222222222"].head_sha == b


def test_a_slug_with_no_origin_has_no_history_under_other_names(tmp_path, monkeypatch):
    from review_pipeline.stages import RunContext
    from review_pipeline.stages.repeats import earlier_findings
    old = _old_workspace(tmp_path, monkeypatch)
    _old_round(old / "checkout-x--pr-7", "a" * 40, "2026-01-01T00:00:00Z", "1111111111")
    target_dir = default_out() / "checkout-y--pr-7"
    target_dir.mkdir(parents=True)
    state = _new_state(slug="checkout-y--pr-7")
    state.target.remote_url = None
    ctx = RunContext(config=None, snapshot=None, diff="", target_dir=target_dir)
    assert earlier_findings(state, ctx) == []


# --- the folder: <out>/<repo>/<object>/ ---------------------------------------------


def _fake_pr(monkeypatch, base: str, head: str) -> None:
    def fake_view(repo_path, number):
        return {"headRefOid": head, "baseRefOid": base, "headRefName": "feature", "baseRefName": "main",
                "title": "t", "body": ""}
    monkeypatch.setattr(target, "_pr_view", fake_view)


@pytest.fixture
def layout_clone(fixture_repo, tmp_path, monkeypatch):
    """A clone of github-tests with branches feat/x and feat_x, a src/ directory at its head, and PR 7."""
    _, base, first = fixture_repo
    repo = _clone(fixture_repo, tmp_path / "checkout")
    fixture_git(repo, "checkout", "-q", "-b", "work", first)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("A = 1\n")
    fixture_git(repo, "add", "src/a.py")
    fixture_git(repo, "commit", "-q", "-m", "src", date="2026-09-17T12:00:00Z")
    head = fixture_git(repo, "rev-parse", "HEAD")
    fixture_git(repo, "update-ref", "refs/remotes/origin/main", base)
    for name in ("feat/x", "feat_x"):
        fixture_git(repo, "update-ref", f"refs/remotes/origin/{name}", head)
    _fake_pr(monkeypatch, base, head)
    return repo, base, head


def _plan(out: Path, *argv: str) -> Path:
    """Run `review --plan-only` and return the folder its `plan:` line names."""
    from review_pipeline import config, orchestrator
    spec = orchestrator.TargetSpec(**{k: v for k, v in zip(argv[::2], argv[1::2])})
    _, path = orchestrator.plan_only(spec, out, config.load_config(out))
    return path.parent


def _hex6(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:6]


def test_each_kind_of_target_gets_its_own_object_folder_under_the_repo(layout_clone, tmp_path, agents):
    """Done means 4. A folder records whose it is in its saved state, so feat/x is a full review."""
    repo, base, head = layout_clone
    out = tmp_path / "out"
    r = str(repo)
    assert _plan(out, "repo", r, "pr", 7) == out / "github-tests" / "pr-7"
    agents()
    assert main(["--out", str(out), "review", "--repo", r, "--branch", "feat/x", "--trust-repo"]) == 0
    assert (out / "github-tests" / "branch-feat_x" / "findings.json").is_file()
    assert _plan(out, "repo", r, "branch", "feat_x") == out / "github-tests" / f"branch-feat_x-{_hex6('feat_x')}"
    assert _plan(out, "repo", r, "branch", "feat/x") == out / "github-tests" / "branch-feat_x"  # again: its own
    assert _plan(out, "repo", r, "range", f"{base}..{head}") == out / "github-tests" / f"range-{base}..{head}"
    assert _plan(out, "repo", r, "tree", "HEAD") == out / "github-tests" / "tree"
    assert _plan(out, "repo", r, "tree", "HEAD", "path", "src") == out / "github-tests" / "tree-src"
    for name, repo_name in (("proj", "proj"), ("legacy", "legacy@local")):
        src = tmp_path / name
        src.mkdir()
        (src / "m.py").write_text("X = 1\n")
        assert _plan(out, "repo", "", "dir", str(src)) == out / repo_name / f"dir-{name}-{_hex6(str(src))}"


def _state_file(folder: Path, remote: str, ref: str, slug: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-state.json").write_text(json.dumps(
        {"target": {"remote_url": remote, "ref": ref, "slug": slug, "head_sha": "e" * 40}, "stage": "done"}))


def test_a_folder_another_remote_wrote_is_left_alone_and_the_fork_gets_owner_folders(layout_clone, tmp_path):
    """Done means 5."""
    repo, _, _ = layout_clone
    out = tmp_path / "out"
    upstream = out / "github-tests" / "pr-7"
    _state_file(upstream, ORIGIN, "7", f"{SLUG_REPO}--pr-7")
    before = _snapshot(upstream)
    fixture_git(repo, "remote", "set-url", "origin", "git@github.com:someone/github-tests.git")
    assert _plan(out, "repo", str(repo), "pr", 7) == out / "github-tests@someone" / "pr-7"
    assert _snapshot(upstream) == before

    # spikelab's feat/x holds branch-feat_x; the fork's feat/x holds its own; the fork's feat_x gets a hash.
    _state_file(out / "github-tests" / "branch-feat_x", ORIGIN, "feat/x", f"{SLUG_REPO}--feat_x")
    fork = "https://github.com/someone/github-tests"
    _state_file(out / "github-tests@someone" / "branch-feat_x", fork, "feat/x", "someone-github-tests--feat_x")
    assert (_plan(out, "repo", str(repo), "branch", "feat_x")
            == out / "github-tests@someone" / f"branch-feat_x-{_hex6('feat_x')}")
    # The same remote in its other URL form and case is the same remote.
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com/SpikeLab/github-tests")
    assert _plan(out, "repo", str(repo), "pr", 7) == upstream


def test_a_folder_no_choice_can_use_is_an_error_that_suggests_out(layout_clone, tmp_path):
    from review_pipeline import orchestrator
    repo, _, _ = layout_clone
    out = tmp_path / "out"
    _state_file(out / "github-tests" / "pr-7", ORIGIN, "7", "x")
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com/someone/github-tests")
    # Not a folder any run writes: only a hand-made one names a third owner under `@someone`.
    _state_file(out / "github-tests@someone" / "pr-7", "https://github.com/third/github-tests", "7", "x")
    with pytest.raises(orchestrator.ReviewError, match="--out"):
        _plan(out, "repo", str(repo), "pr", 7)


def test_a_dir_review_with_no_repo_folder_yet_runs_locked(tmp_path, agents, caplog):
    """Done means 10."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "m.py").write_text("X = 1\n")
    out = tmp_path / "out"
    agents()
    with caplog.at_level(logging.WARNING, logger="review_pipeline.runlock"):
        assert main(["--out", str(out), "review", "--dir", str(src), "--trust-repo"]) == 0
    assert not [r for r in caplog.records if "running unlocked" in r.getMessage()]
    assert (out / "proj" / ".locks" / f"dir-proj-{_hex6(str(src))}.lock").is_file()
    assert (out / "proj" / f"dir-proj-{_hex6(str(src))}" / "findings.json").is_file()


def _layout_review(folder: Path, slug: str, remote: str, head: str = "a" * 40, rounds_kept: int = 0) -> Path:
    """A finished review in *folder*, with *rounds_kept* kept rounds, as a run writes them."""
    run = {"cost_usd": 1.0, "started_at": "1"}
    t = {"slug": slug, "label": slug, "head_sha": head, "base_sha": "b" * 40, "remote_url": remote}
    for i in range(rounds_kept):
        _write_findings(folder / "rounds" / f"{i + 1:02d}-{head[:12]}", head, f"2026-01-0{i + 1}T00:00:00Z",
                        run=dict(run, started_at=str(i)))
        data = json.loads((folder / "rounds" / f"{i + 1:02d}-{head[:12]}" / "findings.json").read_text())
        data["target"] = t
        (folder / "rounds" / f"{i + 1:02d}-{head[:12]}" / "findings.json").write_text(json.dumps(data))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "findings.json").write_text(json.dumps({
        "schema_version": 1, "generated_at": "2026-01-09T00:00:00Z", "producer": "p", "target": t,
        "findings": [], "run": dict(run, started_at="9")}))
    (folder / "files.txt").write_text("")
    return folder


@pytest.mark.parametrize("repo_name", ["tree", "source", "viewer", "rounds", "github-tests"])
def test_rollups_list_new_layout_reviews_whatever_the_repo_is_called(tmp_path, repo_name):
    """Done means 9 (the pipeline's half): repos named like a snapshot, and a whole-tree review."""
    out = tmp_path / "out"
    remote = f"https://github.com/spikelab/{repo_name}.git"
    new = _layout_review(out / repo_name / "pr-7", f"spikelab-{repo_name}--pr-7", remote, rounds_kept=1)
    whole = _layout_review(out / repo_name / "tree", f"spikelab-{repo_name}--tree", remote, head="c" * 40)
    flat = _layout_review(out / "spikelab-other--pr-1", "spikelab-other--pr-1", remote, head="d" * 40)
    (out / repo_name / ".DS_Store").write_text("finder")
    (out / ".DS_Store").write_text("finder")
    # Never a review: legacy/, locks, partial copies, and a 0.42 review nested under a new-layout one.
    _layout_review(out / "legacy" / "x" / "pr-1", "legacy-x--pr-1", remote, head="e" * 40)
    _layout_review(out / repo_name / "pr-7" / "checkout--pr-7", "checkout--pr-7", remote, head="f" * 40)
    (out / repo_name / ".locks").mkdir()
    render.write_rollups(out)
    _, lines, _ = render.write_runs(out)
    high = (out / "HIGH-only.md").read_text()
    assert "across 0 of 3 targets" in high, high
    heads = sorted(json.loads(line)["target"]["head_sha"][0] for line in (out / "runs.jsonl").read_text().splitlines())
    assert heads == ["a", "a", "c", "d"] and lines == 4
    assert new.is_dir() and whole.is_dir() and flat.is_dir()


# --- history across layouts -------------------------------------------------------


def _decide(folder: Path, finding_id: str, decision: str = "reject", ts: str = "2026-01-01T06:00:00Z") -> None:
    (folder / "viewer").mkdir(parents=True, exist_ok=True)
    (folder / "viewer" / "decisions.json").write_text(json.dumps(
        {finding_id: {"decision": decision, "note": f"note on {finding_id}", "ts": ts}}))


def _three_old_layouts(out: Path) -> dict[str, Path]:
    """A 0.46 folder with 2 kept rounds, a 0.42 folder nested in the new one, and a legacy/ copy:
    each holds one finding the person rejected."""
    flat = out / f"{SLUG_REPO}--pr-7"
    for i, (name, fid) in enumerate((("01-aaaaaaaaaaaa", "1000000001"), ("02-aaaaaaaaaaaa", "1000000002"))):
        _old_round(flat / "rounds" / name, "a" * 40, f"2026-01-0{i + 1}T00:00:00Z", fid, slug=f"{SLUG_REPO}--pr-7")
    _old_round(flat, "a" * 40, "2026-01-03T00:00:00Z", "1000000003", slug=f"{SLUG_REPO}--pr-7")
    _decide(flat, "1000000003")
    nested = out / "github-tests" / "pr-7" / "checkout--pr-7"
    _old_round(nested / "rounds" / "bbbbbbbbbbbb", "b" * 40, "2026-01-04T00:00:00Z", "2000000001",
               slug="checkout--pr-7")
    _old_round(nested, "b" * 40, "2026-01-05T00:00:00Z", "2000000002", slug="checkout--pr-7")
    _decide(nested, "2000000002")
    copy = out / "legacy" / "old-checkout--pr-7"
    _old_round(copy, "c" * 40, "2026-01-06T00:00:00Z", "3000000001", slug="old-checkout--pr-7")
    _decide(copy, "3000000001", ts="2026-01-06T06:00:00Z")
    for name in ("HIGH-only.md", "MEDIUM-only.md", "LOW-only.md", "runs.jsonl"):
        (out / "github-tests" / "pr-7" / name).write_text(f"0.42 {name}\n")
    return {"flat": flat, "nested": nested, "copy": copy}


def test_a_first_new_layout_review_reads_the_rounds_and_decisions_of_every_older_layout(tmp_path):
    """Done means 6."""
    from review_pipeline.stages import RunContext
    from review_pipeline.stages.repeats import earlier_findings
    out = default_out()
    folders = _three_old_layouts(out)
    before = {k: _snapshot(v) for k, v in folders.items()}
    target_dir = out / "github-tests" / "pr-7"
    ctx = RunContext(config=None, snapshot=None, diff="", target_dir=target_dir)
    earlier = {e.id: e for e in earlier_findings(_new_state(), ctx)}
    assert sorted(earlier) == ["1000000001", "1000000002", "1000000003", "2000000001", "2000000002", "3000000001"]
    assert {i for i, e in earlier.items() if e.decision == "reject"} == {"1000000003", "2000000002", "3000000001"}
    assert {k: _snapshot(v) for k, v in folders.items()} == before


def test_out_is_the_parent_of_a_046_folder_and_the_grandparent_of_a_new_one(tmp_path, agents, fixture_repo):
    """Done means 7."""
    out = tmp_path / "work" / "out"
    flat = out / f"{SLUG_REPO}--pr-7"
    new = out / "github-tests" / "pr-7"
    _old_round(flat, "a" * 40, "2026-01-01T00:00:00Z", "1000000001", slug=f"{SLUG_REPO}--pr-7")
    assert rounds.out_root(flat) == out
    assert rounds.out_root(new, f"{SLUG_REPO}--pr-7") == out
    _state_file(new, ORIGIN, "7", f"{SLUG_REPO}--pr-7")
    assert rounds.out_root(new) == out
    assert rounds.out_root(tmp_path / "nothing-here") == tmp_path  # no readable slug: its parent
    # A matching round one level above <out> is never read as this target's history.
    above = tmp_path / "work" / "x--pr-7"
    _old_round(above, "a" * 40, "2026-01-01T00:00:00Z", "9999999999", slug="x--pr-7")
    for folder in (flat, new):
        found = rounds.history_elsewhere(folder, f"{SLUG_REPO}--pr-7", ORIGIN)
        assert above / "findings.json" not in found and not any("9999" in p.read_text() for p in found)


def test_resume_of_a_new_layout_folder_writes_rollups_to_out_and_reads_its_review_yaml(
        fixture_repo, tmp_path, agents, capsys):
    """Done means 7: `resume` uses the `<out>` the review used, not `<out>/<repo>/`."""
    from review_pipeline.models import ReviewState
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    target_dir = out / "booking-engine" / f"range-{base}..{head}"
    agents(budget_stop_at=2)
    assert main(_review_args(repo, base, head, out)) == 4
    for name in ("HIGH-only.md", "runs.jsonl"):
        (out / name).unlink(missing_ok=True)
    (out / "review.yaml").write_text("verifier_model: claude-resume\n")
    agents()
    assert main(["resume", str(target_dir), "--trust-repo"]) == 0
    assert (out / "HIGH-only.md").is_file() and (out / "runs.jsonl").is_file()
    assert not (out / "booking-engine" / "HIGH-only.md").exists()
    assert not (out / "booking-engine" / "runs.jsonl").exists()
    state = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert state.run_config["stages"]["verify"]["model"] == "claude-resume"


def test_history_finds_a_042_folder_nested_under_a_repo_called_tree(tmp_path):
    """Done means 9 (history): `tree` is a repo here, not a snapshot to skip."""
    out = tmp_path / "out"
    remote = "https://github.com/spikelab/tree.git"
    nested = out / "tree" / "pr-7" / "checkout--pr-7"
    _old_round(nested, "a" * 40, "2026-01-01T00:00:00Z", "1000000001", remote=remote, slug="checkout--pr-7")
    (out / "tree" / ".DS_Store").write_text("finder")
    found = rounds.history_elsewhere(out / "tree" / "pr-7", "spikelab-tree--pr-7", remote)
    assert found == [nested / "findings.json"]


# --- the plan's stop and resume table: one test per row ------------------------------
#
# Each test stops a run at its row's point (a child process that exits there, as a
# killed run does, so the kernel drops its run lock), or finds the row's state left
# on disk, then reruns or resumes. It compares the result with a run of the same
# target that never stopped: the finding ids, the names under rounds/, the ledger
# events of the chosen folder, and a snapshot of every folder that was there before.

STOP_DRIVER = """
import os, shutil, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from review_pipeline import ledger, orchestrator, rounds, sdk, target
from test_orchestrator import FakeAgents

data = Path(sys.argv[2])
ledger.data_dir = lambda: data
ledger.legacy_reviews_root = lambda: data.parent / "no-legacy-reviews"
target.branch_rules = lambda repo, branch, needs=None: None
base, head = sys.argv[4], sys.argv[5]
target._pr_view = lambda repo, n: {"headRefOid": head, "baseRefOid": base, "headRefName": "feature",
                                   "baseRefName": "main", "title": "t", "body": ""}
sdk.agent_call_structured = FakeAgents()
where, nth = sys.argv[3].split(":")
calls = [0]


def dies(owner, name, before=False):
    real = getattr(owner, name)

    def wrapped(*a, **k):
        calls[0] += 1
        if before and calls[0] == int(nth):
            os._exit(137)
        result = real(*a, **k)
        if calls[0] == int(nth):
            os._exit(137)
        return result
    setattr(owner, name, wrapped)


if where == "hold":          # R1: after <out>/<repo>/ exists, before the lock
    dies(orchestrator, "_hold", before=True)
elif where == "folder":      # R2, R3: holding a folder's lock, having read whose it is
    dies(rounds, "folder_target")
elif where == "resumable":   # R12: holding the 0.46 folder's lock, before reading the ledger for it
    dies(orchestrator, "_resumable", before=True)
elif where == "write":       # R6: after the target files, before the state
    dies(target, "write_target_files")
elif where == "started":     # R7, R8: after the state and the started line
    dies(orchestrator.review_ledger, "started")
elif where == "copy":        # R4 (import_dir), R5 (keep_round): part way through a copytree
    real_copytree = shutil.copytree

    def copytree(src, dst, *a, **k):
        copied = [0]

        def copy(s, d, **kw):
            copied[0] += 1
            if copied[0] > 1:
                os._exit(137)
            return shutil.copy2(s, d, **kw)
        return real_copytree(src, dst, *a, **dict(k, copy_function=copy))
    shutil.copytree = copytree

from review_pipeline.__main__ import main
sys.exit(main(sys.argv[6:]))
"""


def _stop(tmp_path: Path, base: str, head: str, where: str, *argv: str) -> int:
    """Run `main(argv)` in a child process that exits (137) at *where* (`<point>:<nth call>`)."""
    driver = tmp_path / "stop_driver.py"
    driver.write_text(STOP_DRIVER)
    proc = subprocess.run([sys.executable, str(driver), str(TESTS), str(tmp_path / "data"), where, base, head,
                           *argv], capture_output=True, text=True, env=dict(os.environ), timeout=120)
    return proc.returncode


def _pr_args(repo: Path, out: Path, *extra: str) -> list[str]:
    return ["--out", str(out), "review", "--repo", str(repo), "--pr", "7", "--trust-repo", *extra]


def _result(tmp_path: Path, folder: Path) -> dict:
    data = json.loads((folder / "findings.json").read_text())
    kept = folder / "rounds"
    return {"ids": sorted(f["id"] for f in data["findings"]),
            "rounds": sorted(p.name for p in kept.iterdir()) if kept.is_dir() else [],
            "events": [e["event"] for e in _ledger_lines(tmp_path) if e.get("target_dir") == str(folder)]}


PR7 = ("github-tests", "pr-7")


def test_row_U1_a_046_folder_with_finished_rounds_is_read_as_history_and_left_alone(layout_clone, tmp_path, agents):
    repo, _, _ = layout_clone
    out, never, gen = tmp_path / "out", tmp_path / "never-stopped", tmp_path / "gen"
    agents()
    assert main(_pr_args(repo, gen)) == 0
    assert main(_pr_args(repo, gen)) == 0  # a top-level round and one kept round, as 0.46 writes them
    flat = out / f"{SLUG_REPO}--pr-7"
    shutil.copytree(gen.joinpath(*PR7), flat)
    _decide(flat, json.loads((flat / "findings.json").read_text())["findings"][0]["id"])
    for _ in range(2):
        ledger.started(slug=flat.name, target_dir=flat, head_sha="a" * 40, session_id="0.46")
        ledger.finished(producer="multiplai-dev:review 0.46.0", session_id="0.46", slug=flat.name,
                        target_dir=flat, round_="01", head_sha="a" * 40, base_sha="b" * 40, kind="pr", pr=7,
                        stopped=None, run={})
    before = _snapshot(flat)
    assert main(_pr_args(repo, out)) == 0
    state = json.loads((out.joinpath(*PR7) / "review-state.json").read_text())
    assert state["repeats"]  # the old folder's rejection was read
    assert len(rounds.history_elsewhere(out.joinpath(*PR7), f"{SLUG_REPO}--pr-7", ORIGIN)) == 2
    assert _snapshot(flat) == before
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))


def _unfinished_046(tmp_path: Path, repo: Path, base: str, head: str, out: Path) -> Path:
    """A 0.46 folder `<out>/<its slug>/` holding a run stopped after its `started` line on *head*."""
    gen = tmp_path / "gen"
    assert _stop(tmp_path, base, head, "started:1", *_pr_args(repo, gen)) == 137
    flat = out / f"{SLUG_REPO}--pr-7"
    shutil.copytree(gen.joinpath(*PR7), flat)
    state = flat / "review-state.json"
    state.write_text(state.read_text().replace(str(gen.joinpath(*PR7)), str(flat)))
    ledger.started(slug=flat.name, target_dir=flat, head_sha=head, session_id="0.46 run that died")
    return flat


def test_row_U2_an_unfinished_046_run_exits_4_naming_the_old_folder_and_resume_finishes_it_there(
        layout_clone, tmp_path, agents, capsys):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    flat = _unfinished_046(tmp_path, repo, base, head, out)
    agents()
    assert main(_pr_args(repo, never)) == 0
    fake = agents()
    capsys.readouterr()
    assert main(_pr_args(repo, out)) == 4
    assert f"continue with: python -m review_pipeline resume {flat}" in capsys.readouterr().err
    assert fake.calls == [] and not out.joinpath(*PR7).exists()
    assert main(["resume", str(flat), "--trust-repo"]) == 0
    expected = _result(tmp_path, never.joinpath(*PR7))
    assert _result(tmp_path, flat) == dict(expected, events=["started", "started", "finished"])
    assert (out / "HIGH-only.md").is_file() and not (out / "github-tests" / "HIGH-only.md").exists()


def test_row_U2_restart_starts_in_the_new_folder_and_leaves_the_046_one_as_it_is(
        layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    flat = _unfinished_046(tmp_path, repo, base, head, out)
    before = _snapshot(flat)
    agents()
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, out, "--restart")) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))
    assert _snapshot(flat) == before


def test_row_U3_once_the_new_folder_has_state_the_046_folder_is_history_only(layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    flat = _unfinished_046(tmp_path, repo, base, head, out)
    agents()
    assert main(_pr_args(repo, out, "--restart")) == 0
    before = _snapshot(flat)
    assert main(_pr_args(repo, out)) == 0  # not exit 4: the old folder is not checked
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))
    assert _snapshot(flat) == before


def test_row_U4_a_run_still_going_in_the_046_folder_exits_2_until_the_new_folder_has_state(
        layout_clone, tmp_path, agents, capsys):
    from review_pipeline import runlock
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    flat = _unfinished_046(tmp_path, repo, base, head, out)
    before = _snapshot(flat)
    fake = agents()
    with runlock.hold(flat):
        capsys.readouterr()
        assert main(_pr_args(repo, out)) == 2
        assert f"a review of {flat} is running now" in capsys.readouterr().err
        assert fake.calls == []
        assert main(_pr_args(repo, out, "--restart")) == 0  # the new folder now has state
        assert main(_pr_args(repo, out)) == 0  # two folders: neither run waits for the other
    assert _snapshot(flat) == before
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))


def _nested_042(folder: Path, remote: str = ORIGIN) -> Path:
    """What multiplai-dev 0.42 leaves in a new-layout folder given to it as `--out`: its own
    `<checkout>--pr-7/` with a kept round and a rejection, and its four rollups."""
    nested = folder / "checkout--pr-7"
    _old_round(nested / "rounds" / "aaaaaaaaaaaa", "a" * 40, "2026-01-01T00:00:00Z", "4000000001",
               remote=remote, slug="checkout--pr-7")
    _old_round(nested, "a" * 40, "2026-01-02T00:00:00Z", "4000000002", remote=remote, slug="checkout--pr-7")
    _decide(nested, "4000000002")
    for name in rounds.ROLLUP_FILES:
        (folder / name).write_text(f"0.42 {name}\n")
    return nested


def _042_snapshot(folder: Path) -> dict:
    return {"nested": _snapshot(folder / "checkout--pr-7"),
            "rollups": {n: (folder / n).read_bytes() for n in sorted(rounds.ROLLUP_FILES)}}


def test_row_U5_a_042_folder_is_used_its_nested_review_is_history_and_never_copied_into_a_round(
        layout_clone, tmp_path, agents):
    """Also Done means 8, first test."""
    repo, _, _ = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    folder = out.joinpath(*PR7)
    _nested_042(folder)
    before = _042_snapshot(folder)
    fake = agents()
    assert main(_pr_args(repo, out)) == 0
    assert [c for c in fake.calls if c.startswith("repeats")]  # the nested rejection was read
    assert main(_pr_args(repo, out)) == 0
    (kept,) = (folder / "rounds").iterdir()
    assert not [p for p in kept.iterdir() if p.is_dir()]
    assert not [p.name for p in kept.iterdir() if p.name in rounds.ROLLUP_FILES]
    assert _042_snapshot(folder) == before
    agents()
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, never.joinpath(*PR7))
    # resume or assess-only given the nested folder use its parent as <out>, as 0.42 did.
    assert rounds.out_root(folder / "checkout--pr-7") == folder


def test_row_U6_legacy_and_the_old_inbox_reviews_are_read_and_never_written(
        layout_clone, tmp_path, agents, monkeypatch):
    repo, _, _ = layout_clone
    out, never = default_out(), tmp_path / "never-stopped"
    copy = out / "legacy" / "old-checkout--pr-7"
    _old_round(copy, "c" * 40, "2026-01-06T00:00:00Z", "3000000001", slug="old-checkout--pr-7")
    _decide(copy, "3000000001")
    inbox = tmp_path / "no-legacy-reviews"
    _old_round(inbox / "inbox-checkout--pr-7", "d" * 40, "2026-01-07T00:00:00Z", "3000000002",
               slug="inbox-checkout--pr-7")
    before = (_snapshot(out / "legacy"), _snapshot(inbox))
    fake = agents()
    assert main(["review", "--repo", str(repo), "--pr", "7", "--trust-repo"]) == 0
    assert [c for c in fake.calls if c.startswith("repeats")]
    assert (_snapshot(out / "legacy"), _snapshot(inbox)) == before
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["github-tests", "legacy"]
    agents()
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))


def test_row_U7_a_042_process_writing_its_nested_folder_now_does_not_stop_or_leak_into_the_new_run(
        layout_clone, tmp_path, agents, monkeypatch):
    repo, _, _ = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    folder = out.joinpath(*PR7)
    nested = _nested_042(folder)
    agents()
    assert main(_pr_args(repo, out)) == 0
    real_keep = rounds.keep_round

    def keep_while_042_writes(*a, **k):
        # 0.42 is part way through its own run: a snapshot and a findings.json not yet complete.
        (nested / "tree").mkdir(exist_ok=True)
        (nested / "tree" / "x.py").write_text("X = 1\n")
        (nested / "findings.json").write_text('{"schema_version": 1, "find')
        return real_keep(*a, **k)

    monkeypatch.setattr(rounds, "keep_round", keep_while_042_writes)
    assert main(_pr_args(repo, out)) == 0
    (kept,) = (folder / "rounds").iterdir()
    assert not [p for p in kept.iterdir() if p.is_dir()]
    assert (nested / "findings.json").read_text() == '{"schema_version": 1, "find'  # never written by us
    monkeypatch.setattr(rounds, "keep_round", real_keep)
    agents()
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, never.joinpath(*PR7))


def test_row_R1_stopped_after_creating_the_repo_folder_before_the_lock(layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    assert _stop(tmp_path, base, head, "hold:1", *_pr_args(repo, out)) == 137
    assert [p.name for p in (out / "github-tests").iterdir()] == []
    agents()
    assert main(_pr_args(repo, out)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))


def test_row_R2_stopped_holding_another_remotes_folder_before_switching_to_owner(layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    other = "https://github.com/someone/github-tests"
    results = []
    for out, stop in ((tmp_path / "out", True), (tmp_path / "never-stopped", False)):
        theirs = out.joinpath(*PR7)
        _state_file(theirs, other, "7", "someone-github-tests--pr-7")
        before = _snapshot(theirs)
        if stop:
            assert _stop(tmp_path, base, head, "folder:1", *_pr_args(repo, out)) == 137
            assert json.loads((out / "github-tests" / ".locks" / "pr-7.lock").read_text())["pid"]
            assert not (out / "github-tests@spikelab").exists()
        agents()
        assert main(_pr_args(repo, out)) == 0
        assert _snapshot(theirs) == before
        results.append(_result(tmp_path, out / "github-tests@spikelab" / "pr-7"))
    assert results[0] == results[1]


@pytest.mark.parametrize("fork", [False, True], ids=["same-remote", "after-the-fork-step"])
def test_row_R3_stopped_holding_another_refs_folder_before_switching_to_the_hash(
        layout_clone, tmp_path, agents, fork):
    repo, base, head = layout_clone
    other = "https://github.com/someone/github-tests"
    part = "github-tests@spikelab" if fork else "github-tests"
    results = []
    for out, stop in ((tmp_path / "out", True), (tmp_path / "never-stopped", False)):
        if fork:
            _state_file(out / "github-tests" / "branch-feat_x", other, "feat/x", "someone-github-tests--feat_x")
        _state_file(out / part / "branch-feat_x", ORIGIN, "feat/x", f"{SLUG_REPO}--feat_x")
        before = _snapshot(out)
        argv = ["--out", str(out), "review", "--repo", str(repo), "--branch", "feat_x", "--trust-repo"]
        if stop:
            assert _stop(tmp_path, base, head, f"folder:{2 if fork else 1}", *argv) == 137
            assert (out / part / ".locks" / "branch-feat_x.lock").is_file()
        agents()
        assert main(argv) == 0
        assert {k: v for k, v in _snapshot(out).items() if k in before} == before
        results.append(_result(tmp_path, out / part / f"branch-feat_x-{_hex6('feat_x')}"))
    assert results[0] == results[1]


def test_row_R4_stopped_part_way_through_the_dir_copy(tmp_path, agents):
    src = tmp_path / "proj"
    src.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (src / name).write_text(f"{name[0].upper()} = 1\n")
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    argv = ["--out", str(out), "review", "--dir", str(src), "--trust-repo"]
    assert _stop(tmp_path, "-", "-", "copy:0", *argv) == 137
    folder = out / "proj" / f"dir-proj-{_hex6(str(src))}"
    assert (out / "proj" / ".locks" / f"{folder.name}.lock").is_file()
    assert len(list((folder / "source").iterdir())) == 1  # a partial copy, never committed
    agents()
    assert main(argv) == 0
    assert main(["--out", str(never), "review", "--dir", str(src), "--trust-repo"]) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, never / "proj" / folder.name)
    git_files = subprocess.run(["git", "-C", str(folder / "source"), "ls-files"], capture_output=True, text=True)
    assert git_files.stdout.split() == ["a.py", "b.py", "c.py"]


def test_row_R5_stopped_during_keep_rounds_copy(layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    agents()
    assert main(_pr_args(repo, out)) == 0
    assert _stop(tmp_path, base, head, "copy:0", *_pr_args(repo, out)) == 137
    assert [p.name for p in (out.joinpath(*PR7) / "rounds").iterdir()][0].startswith(".partial-")
    assert main(_pr_args(repo, out)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))


def test_row_R6_stopped_after_the_target_files_before_the_state(layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    assert _stop(tmp_path, base, head, "write:1", *_pr_args(repo, out)) == 137
    folder = out.joinpath(*PR7)
    assert sorted(p.name for p in folder.iterdir()) == ["commits.txt", "diff.patch", "files.txt"]
    assert rounds.folder_target(folder) == {}  # names no remote: the next run uses it
    agents()
    assert main(_pr_args(repo, out)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, never.joinpath(*PR7))


def test_row_R7_stopped_after_the_state_is_saved_exits_4_and_resume_finishes(
        layout_clone, tmp_path, agents, capsys):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    folder = out.joinpath(*PR7)
    assert _stop(tmp_path, base, head, "started:1", *_pr_args(repo, out)) == 137
    fake = agents()
    capsys.readouterr()
    assert main(_pr_args(repo, out)) == 4
    assert f"continue with: python -m review_pipeline resume {folder}" in capsys.readouterr().err
    assert fake.calls == []
    assert main(["resume", str(folder), "--trust-repo"]) == 0
    assert (out / "HIGH-only.md").is_file() and not (out / "github-tests" / "HIGH-only.md").exists()
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, folder) == dict(_result(tmp_path, never.joinpath(*PR7)),
                                             events=["started", "started", "finished"])


def test_row_R8_stopped_twice_at_the_same_point_one_resume_finishes_and_the_state_is_kept_once(
        layout_clone, tmp_path, agents):
    repo, base, head = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    folder = out.joinpath(*PR7)
    assert _stop(tmp_path, base, head, "started:1", *_pr_args(repo, out)) == 137
    assert _stop(tmp_path, base, head, "started:1", "resume", str(folder), "--trust-repo") == 137
    assert [e["event"] for e in _ledger_lines(tmp_path) if e["target_dir"] == str(folder)] == ["started"] * 2
    agents()
    assert main(["resume", str(folder), "--trust-repo"]) == 0
    assert main(_pr_args(repo, out)) == 0  # keeps the finished round once
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    expected = _result(tmp_path, never.joinpath(*PR7))
    assert _result(tmp_path, folder) == dict(expected, events=["started"] * 3 + ["finished", "started", "finished"])


def test_row_R9_a_run_of_the_same_folder_still_alive_exits_2(layout_clone, tmp_path, agents, capsys):
    from review_pipeline import runlock
    repo, _, _ = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    folder = out.joinpath(*PR7)
    (out / "github-tests").mkdir(parents=True)
    fake = agents()
    with runlock.hold(folder):
        capsys.readouterr()
        assert main(_pr_args(repo, out)) == 2
        assert f"a review of {folder} is running now" in capsys.readouterr().err
    assert fake.calls == [] and not folder.exists()
    assert main(_pr_args(repo, out)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, never.joinpath(*PR7))


def test_row_R10_origin_changed_between_rounds_uses_the_owner_folder_and_not_the_old_history(
        layout_clone, tmp_path, agents):
    repo, _, _ = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    agents()
    assert main(_pr_args(repo, out)) == 0
    first = out.joinpath(*PR7)
    _decide(first, json.loads((first / "findings.json").read_text())["findings"][0]["id"])
    before = _snapshot(first)
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com/someone/github-tests.git")
    agents()
    assert main(_pr_args(repo, out)) == 0
    state = json.loads((out / "github-tests@someone" / "pr-7" / "review-state.json").read_text())
    assert not state["repeats"]  # the old origin's rejection is not read
    assert _snapshot(first) == before
    assert main(_pr_args(repo, never)) == 0
    assert (_result(tmp_path, out / "github-tests@someone" / "pr-7")
            == _result(tmp_path, never.joinpath(*PR7)))


def test_row_R11_edits_to_the_working_tree_or_to_decisions_between_runs(layout_clone, tmp_path, agents):
    repo, _, _ = layout_clone
    out, never = tmp_path / "out", tmp_path / "never-stopped"
    folder = out.joinpath(*PR7)
    agents()
    assert main(_pr_args(repo, out)) == 0
    (repo / "settings.py").write_text("edited by the person\n")
    (repo / "scratch.txt").write_text("not committed\n")
    _decide(folder, json.loads((folder / "findings.json").read_text())["findings"][0]["id"])
    decisions = (folder / "viewer" / "decisions.json").read_bytes()
    agents()
    assert main(_pr_args(repo, out)) == 0
    state = json.loads((folder / "review-state.json").read_text())
    assert state["repeats"], "the decision is read where it is"
    assert (folder / "viewer" / "decisions.json").read_bytes() == decisions
    assert main(_pr_args(repo, never)) == 0
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, never.joinpath(*PR7))


def test_row_R12_stopped_holding_the_046_folders_lock_during_the_interrupted_run_check(
        layout_clone, tmp_path, agents):
    """A row this change adds: the check takes the 0.46 lock `.locks/<its slug>.lock` under `<out>`, which stays on disk."""
    repo, base, head = layout_clone
    out, never, gen = tmp_path / "out", tmp_path / "never-stopped", tmp_path / "gen"
    agents()
    assert main(_pr_args(repo, gen)) == 0
    flat = out / f"{SLUG_REPO}--pr-7"
    shutil.copytree(gen.joinpath(*PR7), flat)
    before = _snapshot(flat)
    assert _stop(tmp_path, base, head, "resumable:2", *_pr_args(repo, out)) == 137
    lock = out / ".locks" / f"{flat.name}.lock"
    assert json.loads(lock.read_text())["pid"]
    assert main(_pr_args(repo, out)) == 0
    assert lock.is_file() and _snapshot(flat) == before
    assert main(_pr_args(repo, never)) == 0
    assert _result(tmp_path, out.joinpath(*PR7)) == _result(tmp_path, never.joinpath(*PR7))


def test_every_top_level_directory_a_review_writes_is_in_not_kept(layout_clone, tmp_path, agents, monkeypatch):
    """Done means 8, second test: a later directory the pipeline adds fails this and forces a decision."""
    from review_pipeline import orchestrator
    repo, _, _ = layout_clone
    out = tmp_path / "out"
    folder = out.joinpath(*PR7)
    seen: set[str] = set()
    real_save = orchestrator.save_state

    def save_and_look(state, target_dir):
        seen.update(p.name for p in target_dir.iterdir() if p.is_dir())
        return real_save(state, target_dir)

    monkeypatch.setattr(orchestrator, "save_state", save_and_look)
    agents()
    assert main(_pr_args(repo, out)) == 0
    assert main(_pr_args(repo, out)) == 0
    seen.update(p.name for p in folder.iterdir() if p.is_dir())
    assert seen and seen <= rounds.NOT_KEPT, seen - rounds.NOT_KEPT
