"""Item 4 of plan-buildme-epic-speedups-2026-11-03.md: a fixer parked for an
environment reason (`fix.park_kind`) is run again by the next epic run, at
most once per run and `epic.env_relaunches` times in all; a decision park
stays. Rows S9-S15 of its "Stop and resume states" table. The build, `gh`,
the review skill and the fixer are fakes first on PATH
(`epic_row_helpers.Fakes`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import coordinator, fix, records, status
from build_pipeline.epic import lock as epic_lock

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
    Fakes,
    approve,
    child,
    last_summary,
    make_build,
    make_project,
    pr_number,
    run_coordinator,
    use_workspace,
)

CHAIN = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))]
)
N = pr_number("e-a")
HIGH = {
    "id": "f1",
    "severity": "HIGH",
    "status": "confirmed",
    "claim": "f1 is wrong",
    "file": "src/app.py",
    "line_start": 3,
}
BLOCKED = {str(N): [{"findings": [HIGH]}]}
# A fixer whose fetch hit a DNS outage, and its park reason.
ENV = {
    "exit": 1,
    "stdout": "FINDINGS:FAILED:fetch:gh pr view failed: Could not resolve host: api.github.com",
}
ENV_WHY = "the fixer failed at fetch: gh pr view failed: Could not resolve host: api.github.com"
# A red baseline whose reason holds a timeout: a decision.
BASELINE_TIMEOUT = (
    "the PR's test suite fails before any fix (the TDD engine's baseline gate: "
    "Test command failed to run: timed out after 1800 seconds), so no fix can be "
    "tested; make the suite pass, then re-run"
)
DONE = {"rounds": [{}]}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


# --- the classifier ------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason, cause",
    [
        ("tdd.setup_command failed: exit 2", "setup-command"),
        ("TDD.SETUP_COMMAND FAILED: timed out", "setup-command"),
        ("getaddrinfo EAI_AGAIN api.github.com", "network"),
        ("fatal: unable to access: Could not resolve host: github.com", "network"),
        ("Temporary failure in name resolution", "network"),
        ("read: Connection reset by peer", "network"),
        ("ssh: connect to host github.com port 22: Connection timed out", "network"),
        ("Failed to connect to github.com port 443", "network"),
        ("net/http: TLS handshake timeout", "network"),
        ("gh api failed: HTTP 502", "network"),
        ("gh api failed: HTTP 503", "network"),
        ("gh api failed: HTTP 504", "network"),
        ("remote: Internal Server Error", "network"),
        ("remote: Invalid username or token.", "network"),
        ("gh: Bad credentials (HTTP 401)", "network"),
    ],
)
def test_park_kind_names_each_environment_pattern(reason, cause):
    assert fix.park_kind(f"the fixer failed at fetch: {reason}") == (
        "environment",
        cause,
    )


@pytest.mark.parametrize(
    "reason",
    [
        (
            "the fixer failed at code: the PR's test suite fails before any fix "
            "(the TDD engine's baseline gate: 3 failed, 120 passed), so no fix can be "
            "tested; make the suite pass, then re-run"
        ),
        f"the fixer failed at code: {BASELINE_TIMEOUT}",
        "Test command failed to run: timed out after 1800 seconds",
        (
            "the fixer failed at code: RuntimeError: the self-check agent failed: "
            "Reached maximum number of turns (30)"
        ),
        "the fixer left review findings with no decision: f1, f2",
        "",
    ],
)
def test_park_kind_calls_everything_else_a_decision(reason):
    assert fix.park_kind(reason) == ("decision", "")


# The `parked` reasons of fixers 075, 085, 094 and 098 of the DFACCMGMT-1 epic,
# copied verbatim from their result.json on 2026-11-03.
RECORDED = {
    "075": "the fixer failed at code: the PR's test suite fails before any fix (the TDD engine's baseline gate), so no fix can be tested; make the suite pass, then re-run",
    "085": "the fixer failed at code: RuntimeError: the self-check agent failed: failed: Claude Code returned an error result: Reached maximum number of turns (30) (exit code: 1) CLI stderr: 2026-11-02T06:06:47.985Z [DEBUG] hooks module cc-plugin-telemetry@builtin telemetry.log settled in 0.3ms (native link, next() included) 2026-11-02T06:06:47.989Z [INFO] [Stall] tool_dispatch_",
    "094": "the fixer failed at code: the PR's test suite fails before any fix (the TDD engine's baseline gate), so no fix can be tested; make the suite pass, then re-run",
    "098": "the fixer failed at code: the PR's test suite fails before any fix (the TDD engine's baseline gate), so no fix can be tested; make the suite pass, then re-run",
}


@pytest.mark.parametrize("fixer", sorted(RECORDED))
def test_the_recorded_parks_of_the_dfaccmgmt_epic_stay_decisions(fixer):
    assert fix.park_kind(RECORDED[fixer]) == ("decision", "")
    # A record from before `park_kind` is classified from its reason.
    assert fix.park_of({"parked": RECORDED[fixer]}) == ("decision", "")


def test_park_of_prefers_the_saved_kind_and_skips_a_result_with_no_park():
    assert fix.park_of(
        {"parked": "x", "park_kind": "environment", "park_cause": "network"}
    ) == (
        "environment",
        "network",
    )
    assert fix.park_of({"parked": ENV_WHY}) == ("environment", "network")
    assert fix.park_of({"exit_code": 0}) is None and fix.park_of(None) is None


# --- config --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "yaml_text, want",
    [
        ("", 2),
        ("epic:\n  env_relaunches: 0\n", 0),
        ("epic:\n  env_relaunches: 5\n", 5),
        ("epic:\n  env_relaunches: -1\n", 2),
        ("epic:\n  env_relaunches: false\n", 2),
        ("epic:\n  env_relaunches: lots\n", 2),
    ],
)
def test_env_relaunches_defaults_to_2_and_0_turns_it_off(tmp_path, yaml_text, want):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text("context: x\n" + yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    config._load_specs_config()
    assert config.epic.env_relaunches == want
    assert EpicSettings().env_relaunches == 2


# --- the coordinator -----------------------------------------------------------------


class Epic:
    def __init__(self, root: Path, monkeypatch, fixes, reviews=None, relaunches=2):
        self.root = root
        use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(
            root,
            monkeypatch,
            {},
            BLOCKED if reviews is None else reviews,
            {str(N): fixes},
        )
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(
            max_parallel=2,
            max_review_rounds=6,
            merge="manual",
            env_relaunches=relaunches,
        )
        approve(self.config, CHAIN, CallLog(root / "split.log"))
        self.dir = root / "ws" / ".multiplai" / "data" / "buildme" / "epics" / "o-r--e"

    def run(self) -> int:
        return run_coordinator(self.config)

    def status(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"]["a"]

    def fixes(self) -> list[records.Attempt]:
        return [
            a for a in records.attempts(self.dir) if a.child == "a" and a.kind == "fix"
        ]

    def relaunches(self) -> list[int | None]:
        return [a.started.get("env_relaunch") for a in self.fixes()]

    def summary_a(self) -> dict:
        return next(
            c for c in last_summary(self.dir, self.root)["children"] if c["id"] == "a"
        )


def test_s9_an_environment_park_saves_its_kind_and_the_next_run_relaunches_it(
    tmp_path, monkeypatch
):
    """S9: the park's `result.json` holds `park_kind` and `park_cause`; the
    next run's `_recover` reads them and the fixer runs again, as launched,
    with `env_relaunch: 1` on its started record."""
    e = Epic(tmp_path, monkeypatch, [ENV, DONE])
    assert e.run() == 0
    [parked] = e.fixes()
    assert (
        parked.result["parked"],
        parked.result["park_kind"],
        parked.result["park_cause"],
    ) == (ENV_WHY, "environment", "network")
    assert (e.status()["status"], e.status()["reason"]) == ("parked", ENV_WHY)
    assert e.summary_a()["park_kind"] == "environment"
    [row] = [r for r in status.epic_status(e.project, "e") if r["child"] == "a"]
    assert (row["state"], row["park_kind"]) == ("parked", "environment")

    e.run()
    assert e.relaunches() == [None, 1]
    first, again = e.fixes()
    assert again.started["argv"] == first.started["argv"]  # as launched
    assert again.result["exit_code"] == 0
    assert e.status()["status"] == "built"
    assert "park_kind" not in e.summary_a()


def test_a_decision_park_stays_parked(tmp_path, monkeypatch):
    """A red baseline, even one whose reason holds a timeout, is a decision."""
    e = Epic(
        tmp_path,
        monkeypatch,
        [{"exit": 1, "stdout": f"FINDINGS:FAILED:code:{BASELINE_TIMEOUT}"}, DONE],
    )
    assert e.run() == 0
    [parked] = e.fixes()
    assert (parked.result["park_kind"], parked.result["park_cause"]) == ("decision", "")
    assert e.summary_a()["park_kind"] == "decision"
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "parked"
    assert e.status()["reason"] == f"the fixer failed at code: {BASELINE_TIMEOUT}"


def test_an_old_environment_record_with_no_park_kind_is_classified_by_its_reason(
    tmp_path, monkeypatch
):
    """A result written before this release has no `park_kind`."""
    e = Epic(tmp_path, monkeypatch, [ENV, DONE])
    assert e.run() == 0
    [parked] = e.fixes()
    old = {
        k: v for k, v in parked.result.items() if k not in ("park_kind", "park_cause")
    }
    parked.result_path.write_text(json.dumps(old))
    e.run()
    assert e.relaunches() == [None, 1]


def test_s11_s12_once_per_run_then_parked_at_the_cap(tmp_path, monkeypatch):
    """S12: a relaunched fixer that parks for the environment again is not
    run again in that run. S11: the next run relaunches while the total is
    under `epic.env_relaunches` (2), then parks with `relaunched 2 times`."""
    e = Epic(tmp_path, monkeypatch, [ENV, ENV, ENV, DONE])
    assert e.run() == 0
    assert e.run() == 0
    assert e.relaunches() == [None, 1]  # S12: one relaunch in the run
    assert e.status()["status"] == "parked"
    assert e.run() == 0
    assert e.relaunches() == [None, 1, 2]
    assert e.run() == 0
    assert e.relaunches() == [None, 1, 2]
    st = e.status()
    assert st["status"] == "parked"
    assert st["reason"] == (
        f"{ENV_WHY} (environment: network; relaunched 2 times, epic.env_relaunches)"
    )
    assert e.summary_a()["park_kind"] == "environment"


def test_s12_a_second_environment_park_in_the_run_is_not_relaunched(
    tmp_path, monkeypatch
):
    """S12 on `_env_relaunch` itself: with `env_relaunched` set in memory, a
    child whose fixer parked for the environment again parks."""
    e = Epic(tmp_path, monkeypatch, [ENV, ENV, DONE], relaunches=5)
    assert e.run() == 0
    seen = []
    real = coordinator.Coordinator._env_relaunch

    async def env_relaunch(self, c, last, cause, number, head):
        seen.append(c.env_relaunched)
        out = await real(self, c, last, cause, number, head)
        if seen == [False]:  # ask once more within the run
            seen.append(c.env_relaunched)
            fixes = [
                a
                for a in records.attempts(self.epic_dir)
                if a.child == c.id and a.kind == "fix"
            ]
            out = await real(self, c, fixes[-1], cause, number, head)
        return out

    monkeypatch.setattr(coordinator.Coordinator, "_env_relaunch", env_relaunch)
    assert e.run() == 0
    assert seen == [False, True]
    assert e.relaunches() == [None, 1]
    assert e.status()["status"] == "parked"


def test_s13_env_relaunches_0_relaunches_nothing_and_a_raised_value_is_used(
    tmp_path, monkeypatch
):
    """S13: the next run uses the value it reads against the count in the records."""
    e = Epic(tmp_path, monkeypatch, [ENV, DONE], relaunches=0)
    assert e.run() == 0
    assert e.run() == 0
    assert e.relaunches() == [None]
    assert e.status()["reason"] == (
        f"{ENV_WHY} (environment: network; relaunched 0 times, epic.env_relaunches)"
    )
    e.config.epic = EpicSettings(
        max_parallel=2, max_review_rounds=6, merge="manual", env_relaunches=1
    )
    e.run()
    assert e.relaunches() == [None, 1]
    assert e.status()["status"] == "built"


def test_s14_a_moved_head_is_reviewed_not_relaunched(tmp_path, monkeypatch):
    """S14: the PR head moved since the park: `_fix_pending` returns None and
    the new head is reviewed as today."""
    reviews = {str(N): [{"findings": [HIGH]}, {"findings": []}]}
    e = Epic(tmp_path, monkeypatch, [ENV, DONE], reviews=reviews)
    assert e.run() == 0
    assert len(e.fakes.review_launches()) == 1
    e.fakes.set_head("e-a", "c" * 40)
    e.run()
    assert len(e.fakes.review_launches()) == 2
    assert e.relaunches() == [None] and len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "built"


def test_s10_a_relaunch_stopped_with_no_result_runs_again_as_launched(
    tmp_path, monkeypatch
):
    """S10: the relaunch's started record (`env_relaunch: 1`) has no result:
    today's path for an unfinished fixer runs it again (R2), with no new
    `env_relaunch`, so the total stays 1."""
    e = Epic(tmp_path, monkeypatch, [ENV, {"die": True}, DONE])
    assert e.run() == 0
    e.run()
    assert e.relaunches() == [None, 1]
    e.fixes()[-1].result_path.unlink()
    e.run()
    assert e.relaunches() == [None, 1, None]
    assert e.fixes()[-1].started["argv"] == e.fixes()[1].started["argv"]
    assert e.fixes()[-1].result["exit_code"] == 0
    c = coordinator.Coordinator.__new__(coordinator.Coordinator)
    c.epic_dir = e.dir
    assert c._env_relaunches("a") == 1


def _hand_fixer(pr: int, *extra: str) -> subprocess.Popen:
    """A process whose command line reads as `findings` on the PR."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "-m",
            "build_pipeline",
            "findings",
            *extra,
        ]
    )


def test_s15_a_live_fixer_is_waited_for_before_the_relaunch(tmp_path, monkeypatch):
    """S15: a fixer still alive on the PR is waited for (`_fixer_gone`); the
    relaunch starts only once it is gone."""
    e = Epic(tmp_path, monkeypatch, [ENV, DONE])
    assert e.run() == 0
    hand = _hand_fixer(N, "--pr", str(N), "--project-dir", str(e.project))
    looks = []

    def tick(coord):
        if hand.poll() is None and coord.children["a"].env_relaunched:
            looks.append(1)
            assert len(e.fakes.fix_launches()) == 1  # never a second fixer
            if len(looks) == 5:
                hand.terminate()
                hand.wait()

    monkeypatch.setattr(coordinator, "_tick", tick)
    try:
        e.run()
    finally:
        hand.kill()
        hand.wait()
    assert len(looks) == 5
    assert e.relaunches() == [None, 1] and e.fixes()[-1].result["exit_code"] == 0
