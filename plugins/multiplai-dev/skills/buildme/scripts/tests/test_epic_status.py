"""`build_pipeline epic status` (plan-factory-merged-2026-10-29.md, item 69)
and its stop row S51: called while a child runs, it reports the child as
running and never launches. It reads the records and the live lock and
never takes the lock, so the running epic goes on as if it was never called.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path

import pytest
from build_pipeline import __main__ as cli
from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records, status
from build_pipeline.epic import lock as epic_lock

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
    Fakes,
    approve,
    child,
    commit_names,
    die_at,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    merge_waiting,
    name_commits,
    pr_url,
    records_snapshot,
    run_coordinator,
    run_killed,
    use_workspace,
)

CHAIN = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))]
)


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Root:
    def __init__(self, root: Path, monkeypatch, steps: dict):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=1, merge="manual")
        approve(self.config, CHAIN, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def use(self) -> None:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)

    def run(self, tick) -> int:
        self.use()
        self.mp.setattr(coordinator, "_tick", tick)
        return run_coordinator(self.config)

    def status(self) -> dict[str, dict]:
        return {
            row["child"]: row
            for row in status.epic_status(self.project, self.config.change_name)
        }

    def launches(self) -> list[dict]:
        return CallLog(self.root / "calls.log").calls()

    def files(self) -> dict[str, tuple[str, int]]:
        """Every file under the epic dir: its hash and mtime."""
        return {
            str(p.relative_to(self.dir)): (
                hashlib.sha1(p.read_bytes()).hexdigest(),
                p.stat().st_mtime_ns,
            )
            for p in sorted(self.dir.rglob("*"))
            if p.is_file()
        }

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        return name_commits(
            {
                "epic": epic_snapshot(self.dir, self.root),
                "records": records_snapshot(self.dir, self.root),
                "summary": last_summary(self.dir, self.root),
            },
            names,
        )


def test_s51_called_while_a_child_runs_reports_it_running_and_never_launches(
    tmp_path, monkeypatch
):
    """S51: while `a` builds under a running epic, status says `a` is running
    (its live build, the epic's live lock), `b` waits on `a`; the call adds
    no launch and changes no file in the epic dir, and the epic ends as a run
    nobody asked about."""
    steps = {"a": [{"block": True, "spend": 1.5}], "b": [{}]}
    # The coordinator runs in this pytest process, whose command line does
    # not name build_pipeline as a real epic's does.
    monkeypatch.setattr(status, "_runs_build_pipeline", lambda pid: True)
    asked = Root(tmp_path / "asked", monkeypatch, steps)
    seen: list[dict] = []

    def tick(coord):
        if not seen and coord.children["a"].status == "running":
            before_files, before_calls = asked.files(), asked.launches()
            seen.append(asked.status())
            assert asked.files() == before_files  # nothing written
            assert asked.launches() == before_calls  # nothing launched
            assert epic_lock.held(asked.dir)  # the epic's own hold is untouched
        asked.fakes.release("a")

    assert asked.run(tick) == 3 and merge_waiting() == [pr_url("e-a")]
    a, b = seen[0]["a"], seen[0]["b"]
    assert a["state"] == "running" and a["epic_running"] is True
    assert a["process"] == {"kind": "build", "record": "001-build-a", "alive": True}
    assert a["target"] == "ENG-40-e" and a["head"]
    assert a["spend"]["known_usd"] == 0.0
    assert any("001-build" in u for u in a["spend"]["unknown"])  # no SPEND: yet
    assert b["state"] == "pending" and b["blocker"] == "depends on a (running)"
    assert b["next"] == "launches once a merged"

    clean = Root(tmp_path / "clean", monkeypatch, steps)
    assert clean.run(lambda coord: clean.fakes.release("a")) == 3
    s, c = asked.snapshot(), clean.snapshot()
    assert s == c
    assert len(asked.launches()) == len(clean.launches())

    after = asked.status()["a"]  # the epic ended: its built record now
    assert after["state"] == "built" and after["pr"] == pr_url("e-a")
    assert after["spend"]["known_usd"] >= 1.5
    assert not any("001-build" in u for u in after["spend"]["unknown"])
    assert after["process"] is None


def test_a_child_left_running_by_a_killed_epic_is_running_then_interrupted(
    tmp_path, monkeypatch
):
    """The epic is gone (its lock is free) and `a`'s build still runs: status
    reads `ps` and says running; once that build is killed, `interrupted`,
    which the next epic run resolves. Neither call writes anything."""
    r = Root(tmp_path, monkeypatch, {"a": [{"block": True}], "b": [{}]})
    r.use()
    r.mp.setattr(coordinator, "_tick", lambda coord: None)
    run_killed(lambda: run_coordinator(r.config), die_at(coordinator, "_tick"))
    files = r.files()
    a = r.status()["a"]
    assert a["state"] == "running" and a["epic_running"] is False
    assert a["process"]["alive"] is True

    pid = coordinator.build_pid("e-a", str(r.project.absolute()))
    os.killpg(pid, signal.SIGKILL)
    for _ in range(500):
        if coordinator.build_pid("e-a", str(r.project.absolute())) is None:
            break
    a = r.status()["a"]
    assert a["state"] == "interrupted" and a["process"]["alive"] is False
    assert a["next"].startswith("the next epic run writes its result")
    assert r.files() == files
    assert records.attempts(r.dir)[-1].result is None  # status wrote no result


def test_cli_prints_one_json_line_per_child(tmp_path, monkeypatch, capsys):
    r = Root(tmp_path, monkeypatch, {"a": [{}], "b": [{}]})
    assert r.run(lambda coord: None) == 3
    capsys.readouterr()
    code = cli.main(
        ["epic", "status", "--change", "e", "--project-dir", str(r.project)]
    )
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    rows = [json.loads(x) for x in lines]
    assert [row["child"] for row in rows] == ["a", "b"]
    assert {"state", "head", "target", "blocker", "next", "spend"} <= set(rows[0])


def test_cli_with_no_epic_exits_2(tmp_path, monkeypatch, capsys):
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    assert (
        cli.main(["epic", "status", "--change", "e", "--project-dir", str(project)])
        == 2
    )
    assert "no epic e" in capsys.readouterr().err


def test_epic_live_reads_the_lock_without_taking_it(tmp_path, monkeypatch):
    assert status.epic_live(tmp_path) is None  # no lock file
    epic_lock.hold(tmp_path, "e")
    assert status.epic_live(tmp_path) is None  # pytest is not build_pipeline
    monkeypatch.setattr(status, "_runs_build_pipeline", lambda pid: True)
    info = status.epic_live(tmp_path)
    assert info is not None and info["pid"] == os.getpid()
    (tmp_path / ".lock").write_text(json.dumps({"pid": 2**22 + 7, "host": "x"}))
    assert status.epic_live(tmp_path) is None  # another host
