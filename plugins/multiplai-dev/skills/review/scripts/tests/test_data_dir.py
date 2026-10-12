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

from conftest import REAL_DATA_DIR, TESTS
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
    assert (reviews / f"example-booking-engine--{base}..{head}" / "findings.json").is_file()


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
    target_dir = out / f"{SLUG_REPO}--feature_x"
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
    target_dir = out / f"example-booking-engine--{base}..{head}"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    assert main(_review_args(repo, base, head, out)) == 0
    finished = _ledger_lines(tmp_path, "finished")
    assert len(finished) == 2
    first = finished[0]
    assert {k: first[k] for k in ("slug", "target_dir", "round", "head_sha", "base_sha", "kind", "pr", "stopped",
                                  "session_id")} == {
        "slug": target_dir.name, "target_dir": str(target_dir), "round": "01", "head_sha": head, "base_sha": base,
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
    target_dir = out / f"example-booking-engine--{base}..{head}"
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
    target_dir = out / f"example-booking-engine--{base}..{head}"
    ledger.started(slug=target_dir.name, target_dir=target_dir, head_sha=head, session_id="a run that died")
    fake = agents()
    assert main(_review_args(repo, base, head, out)) == 4
    err = capsys.readouterr().err
    assert f"interrupted run found; continue with: python -m review_pipeline resume {target_dir}" in err
    assert fake.calls == [] and not target_dir.exists()
    assert main(_review_args(repo, base, head, out, "--restart")) == 0
    assert _ledger_lines(tmp_path, "finished")[-1]["stopped"] is None


def test_a_review_running_now_is_not_read_as_interrupted_and_nothing_starts_a_second_run_in_its_directory(
        fixture_repo, tmp_path, agents, capsys):
    """A running review leaves the same `started` line as a killed one. While a
    process holds the target's run lock, `review`, `review --restart` and
    `resume` all refuse (exit 2) and write nothing; once it lets go, the same
    ledger line reads as interrupted again (exit 4)."""
    from review_pipeline import runlock

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    out.mkdir()
    target_dir = out / f"example-booking-engine--{base}..{head}"
    ledger.started(slug=target_dir.name, target_dir=target_dir, head_sha=head, session_id="a run still going")
    fake = agents()
    with runlock.hold(target_dir):
        for argv in (_review_args(repo, base, head, out), _review_args(repo, base, head, out, "--restart"),
                     ["resume", str(target_dir), "--trust-repo"]):
            capsys.readouterr()
            assert main(argv) == 2, argv
            err = capsys.readouterr().err
            assert f"a review of {target_dir} is running now (pid {os.getpid()} on " in err, err
            assert "interrupted run found" not in err
    assert fake.calls == [] and not target_dir.exists()
    assert [e["event"] for e in _ledger_lines(tmp_path)] == ["started"]

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
    target_dir = out / f"example-booking-engine--{base}..{head}"

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
    target_dir = out / f"example-booking-engine--{base}..{head}"
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
    target_dir = out / f"example-booking-engine--{base}..{head}"
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
