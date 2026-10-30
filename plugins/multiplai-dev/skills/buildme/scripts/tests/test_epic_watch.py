"""`build_pipeline epic watch` (plan-factory-merged-2026-10-29.md, item 68)
and its stop rows S49 (killed while the epic runs: a new loop waits for the
live lock holder and never starts a second epic) and S50 (killed while
waiting for a usage reset: a new loop reads the reset time from `watch.log`
and waits until then).

The epic command, the clock, `epic status`, the lock check and the Slack
post are fakes: no epic runs and nothing is posted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from build_pipeline import __main__ as cli
from build_pipeline.epic import records, watch

T0 = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
DONE_ROWS = [{"child": "a", "state": "merged"}, {"child": "b", "state": "merged"}]


class FakeClock(watch.Clock):
    def __init__(self, now: datetime = T0):
        self.t = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


class Epic:
    """The epic command: each run appends its scripted output and returns its
    exit code; `rows` are what `epic status` reads after it."""

    def __init__(self, clock: FakeClock, script: list[tuple[int, str, list[dict]]]):
        self.clock, self.script = clock, list(script)
        self.runs: list[datetime] = []
        self.rows: list[dict] = []

    def __call__(self, output: Path) -> int:
        self.runs.append(self.clock.now())
        code, text, rows = self.script.pop(0)
        with output.open("a") as f:
            f.write(text + "\n")
        self.rows = rows
        return code


class Posts:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, key: str, text: str) -> str:
        self.sent.append((key, text))
        return "SLACK:POSTED:1.0"


def make(tmp_path, clock, script, *, epic_live=lambda d: None, read_status=None):
    epic = Epic(clock, script)
    posts = Posts()
    w = watch.Watch(
        change="acct",
        epic_dir=tmp_path / "epic",
        run_epic=epic,
        read_status=read_status or (lambda: epic.rows),
        notify=posts,
        clock=clock,
        epic_live=epic_live,
    )
    (tmp_path / "epic").mkdir(parents=True, exist_ok=True)
    return w, epic, posts


def log_lines(tmp_path) -> list[str]:
    return (tmp_path / "epic" / "watch.log").read_text().splitlines()


# --- reading a run ---------------------------------------------------------------


def test_a_done_epic_stops_with_exit_0():
    out = watch.decide(0, "EPIC:SUMMARY:x", DONE_ROWS, [], "", T0, 300)
    assert out.stop and out.code == watch.EXIT_DONE


def test_exit_0_with_a_child_not_merged_is_not_done():
    rows = [{"child": "a", "state": "merged"}, {"child": "b", "state": "skipped"}]
    out = watch.decide(0, "", rows, [], "", T0, 300)
    assert out.stop and out.code == watch.EXIT_FAILED


@pytest.mark.parametrize(
    "line",
    [
        "EPIC:MERGE_WAITING:https://github.com/o/r/pull/37",
        "EPIC:BUDGET:500.00",
        "FEATURE_BRANCH:CONFLICT:ENG-1-x:a.py",
    ],
)
def test_a_marker_that_waits_on_a_person_stops_with_exit_3(line):
    out = watch.decide(3, f"noise\n{line}\n", [], [], "", T0, 300)
    assert out.stop and out.code == watch.EXIT_PERSON and line in out.why


def test_a_parked_child_needs_a_person():
    rows = [{"child": "a", "state": "parked", "blocker": "SPLIT:REQUIRED:p"}]
    out = watch.decide(1, "", rows, [], "", T0, 300)
    assert out.stop and out.code == watch.EXIT_PERSON and "a parked" in out.why


def test_a_child_waiting_on_its_plan_approval_needs_a_person():
    rows = [{"child": "a", "state": "waiting", "blocker": "PLAN:WAITING:x.md"}]
    assert watch.decide(3, "", rows, [], "", T0, 300).code == watch.EXIT_PERSON


def test_a_network_failure_waits_and_runs_again():
    logs = "ERROR: getaddrinfo EAI_AGAIN api.anthropic.com"
    out = watch.decide(1, "", [], [], logs, T0, 300)
    assert not out.stop and out.until == T0 + timedelta(seconds=300)
    assert out.reason == "backoff"


def test_an_agent_timeout_waits_and_runs_again():
    out = watch.decide(4, "SPEND:1.0:10", [], [], "", T0, 600)
    assert not out.stop and out.until == T0 + timedelta(seconds=600)


def test_a_childs_agent_timeout_waits_and_runs_again(tmp_path):
    a = records.Attempt(
        1, "build", "a", tmp_path / "001-build-a", {}, {"exit_code": 4, "markers": []}
    )
    assert not watch.decide(1, "", [], [a], "", T0, 300).stop


def test_exit_4_with_spec_issues_is_not_a_timeout():
    out = watch.decide(
        4, "SPEC:ISSUES:specs/changes/x/spec-issues.md", [], [], "", T0, 300
    )
    assert out.stop and out.code == watch.EXIT_PERSON


def test_a_usage_limit_waits_until_its_reset_time():
    logs = "FAIL run_agent [x] You've hit your session limit · resets 9:40am (UTC)"
    out = watch.decide(1, "", [], [], logs, T0, 300)
    assert not out.stop and out.reason == "usage"
    assert out.until == datetime(2026, 10, 29, 9, 41, tzinfo=UTC)


def test_a_usage_limit_with_no_reset_time_backs_off():
    out = watch.decide(1, "You've hit your weekly limit", [], [], "", T0, 300)
    assert not out.stop and out.until == T0 + timedelta(seconds=300)


def test_an_unnamed_failure_stops_and_names_the_last_markers():
    out = watch.decide(
        1, "EPIC:SUMMARY:runs/01\nEPIC: child a failed: exit 1", [], [], "", T0, 300
    )
    assert out.stop and out.code == watch.EXIT_FAILED
    assert "exited 1" in out.why and "EPIC:SUMMARY:runs/01" in out.why


@pytest.mark.parametrize(
    "text,expected",
    [
        ("9:40am (UTC)", datetime(2026, 10, 29, 9, 40, tzinfo=UTC)),
        ("5pm", datetime(2026, 10, 29, 17, 0, tzinfo=UTC)),
        ("5am", datetime(2026, 10, 30, 5, 0, tzinfo=UTC)),  # past today: tomorrow
        ("Oct 31, 4am (UTC)", datetime(2026, 10, 31, 4, 0, tzinfo=UTC)),
        ("9am (Europe/Rome)", datetime(2026, 10, 29, 8, 0, tzinfo=UTC)),
        ("soon", None),
        ("9am (Not/AZone)", None),
    ],
)
def test_reset_time_reads_the_clis_shapes(text, expected):
    assert watch.reset_time(text, T0) == expected


# --- the loop ----------------------------------------------------------------------


def test_the_loop_runs_again_after_a_network_failure_then_stops_done(tmp_path):
    clock = FakeClock()
    w, epic, posts = make(
        tmp_path,
        clock,
        [
            (1, "fatal: Could not resolve host: github.com", []),
            (0, "EPIC:SUMMARY:x", DONE_ROWS),
        ],
    )
    assert w.run() == watch.EXIT_DONE
    assert epic.runs == [T0, T0 + timedelta(seconds=300)]
    assert len(posts.sent) == 1 and "is done" in posts.sent[0][1]
    lines = log_lines(tmp_path)
    assert any(" WAIT reason=backoff until=2026-10-29T06:05:00Z" in ln for ln in lines)
    assert lines[-1].endswith("NOTIFY SLACK:POSTED:1.0")


def test_the_backoff_doubles_up_to_60_minutes(tmp_path):
    clock = FakeClock()
    net = (1, "ECONNRESET", [])
    w, epic, _ = make(tmp_path, clock, [net] * 6 + [(0, "", DONE_ROWS)])
    w.run()
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(epic.runs, epic.runs[1:])]
    assert gaps == [5, 10, 20, 40, 60, 60]


def test_a_person_needed_stops_and_posts_once(tmp_path):
    clock = FakeClock()
    rows = [{"child": "a", "state": "parked", "blocker": "SPLIT:REQUIRED:p"}]
    w, epic, posts = make(tmp_path, clock, [(1, "", rows)])
    assert w.run() == watch.EXIT_PERSON
    assert len(epic.runs) == 1 and len(posts.sent) == 1
    assert "needs a person" in posts.sent[0][1]


def test_the_same_stop_posts_with_the_same_key(tmp_path):
    rows = [{"child": "a", "state": "parked", "blocker": "SPLIT:REQUIRED:p"}]
    w1, _, p1 = make(tmp_path / "1", FakeClock(), [(1, "", rows)])
    w2, _, p2 = make(tmp_path / "2", FakeClock(), [(1, "", rows)])
    w1.run(), w2.run()
    assert p1.sent[0][0] == p2.sent[0][0]  # the viewer's `post` sends a key once


def test_a_live_child_of_an_earlier_run_is_waited_for(tmp_path):
    """Checked by pid and command line, as `_recover` does (`epic status`'s
    `process.alive`): the epic is not run while it lives."""
    clock = FakeClock()
    alive = {"n": 2}

    def status_rows():
        if alive["n"]:
            alive["n"] -= 1
            return [
                {
                    "child": "a",
                    "state": "running",
                    "process": {"kind": "build", "alive": True},
                }
            ]
        return epic.rows

    w, epic, _ = make(
        tmp_path, clock, [(0, "", DONE_ROWS)], read_status=lambda: status_rows()
    )
    w.run()
    assert epic.runs == [T0 + timedelta(seconds=2 * watch.POLL)]
    assert any(
        "a's build from an earlier run is alive" in ln for ln in log_lines(tmp_path)
    )


def test_a_child_ps_cannot_answer_for_is_waited_for(tmp_path):
    clock = FakeClock()
    calls = {"n": 0}

    def status_rows():
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"child": "a", "state": "unknown",
                     "blocker": "cannot tell whether its build is running: ps failed"}]  # fmt: skip
        return epic.rows

    w, epic, _ = make(
        tmp_path, clock, [(0, "", DONE_ROWS)], read_status=lambda: status_rows()
    )
    w.run()
    assert epic.runs == [T0 + timedelta(seconds=watch.POLL)]


def test_no_epic_yet_runs_the_epic(tmp_path):
    from build_pipeline.epic import status

    def none():
        raise status.StatusError("no epic")

    clock = FakeClock()
    w, epic, _ = make(tmp_path, clock, [(1, "something else", [])], read_status=none)
    assert w.run() == watch.EXIT_FAILED and epic.runs == [T0]


def test_s49_a_new_loop_waits_for_the_live_lock_holder(tmp_path):
    """Row S49: the loop was killed while the epic ran; the epic still holds
    its lock. A new loop waits and runs no second epic until the holder is
    gone."""
    clock = FakeClock()
    left = {"n": 3}

    def live(_dir):
        if left["n"]:
            left["n"] -= 1
            return {"pid": 4242, "host": "h"}
        return None

    w, epic, _ = make(tmp_path, clock, [(0, "", DONE_ROWS)], epic_live=live)
    assert w.run() == watch.EXIT_DONE
    assert epic.runs == [T0 + timedelta(seconds=3 * watch.POLL)]
    busy = [ln for ln in log_lines(tmp_path) if " BUSY " in ln]
    assert len(busy) == 1 and "pid 4242" in busy[0]


def test_s49_a_lock_taken_between_the_check_and_the_launch_is_waited_for(tmp_path):
    clock = FakeClock()
    w, epic, posts = make(
        tmp_path,
        clock,
        [
            (2, "ERROR: epic acct is running now (pid 9 on h, since x)", []),
            (0, "", DONE_ROWS),
        ],
    )
    assert w.run() == watch.EXIT_DONE and len(epic.runs) == 2 and len(posts.sent) == 1


def test_s50_a_new_loop_waits_until_the_logged_reset(tmp_path):
    """Row S50: the loop was killed while it waited for a usage reset. A new
    loop reads the reset time from `watch.log` and waits until then before it
    runs the epic, and a run that never stopped waits the same."""
    logs = "You've hit your session limit · resets 9:40am (UTC)"
    reset = datetime(2026, 10, 29, 9, 41, tzinfo=UTC)

    class Killed(Exception):
        pass

    # The stopped loop: killed during its wait, after the WAIT line.
    clock = FakeClock()
    w, _epic, _ = make(tmp_path / "stopped", clock, [(1, logs, [])])

    def die(seconds):
        raise Killed

    clock.sleep = die
    with pytest.raises(Killed):
        w.run()
    assert (
        log_lines(tmp_path / "stopped")[-1]
        .split(" ", 1)[1]
        .startswith("WAIT reason=usage until=2026-10-29T09:41:00Z")
    )

    # The new loop, an hour later.
    clock2 = FakeClock(T0 + timedelta(hours=1))
    w2, epic2, _posts2 = make(tmp_path / "stopped", clock2, [(0, "", DONE_ROWS)])
    assert w2.run() == watch.EXIT_DONE
    assert epic2.runs == [reset]

    # A loop that never stopped runs again at the same time.
    clock3 = FakeClock()
    w3, epic3, _ = make(tmp_path / "never", clock3, [(1, logs, []), (0, "", DONE_ROWS)])
    assert w3.run() == watch.EXIT_DONE
    assert epic3.runs == [T0, reset]


def test_s50_a_logged_reset_already_past_runs_at_once(tmp_path):
    (tmp_path / "epic").mkdir()
    (tmp_path / "epic" / "watch.log").write_text(
        "2026-10-29T05:00:00Z WAIT reason=usage until=2026-10-29T05:30:00Z why=x\n"
    )
    w, epic, _ = make(tmp_path, FakeClock(), [(0, "", DONE_ROWS)])
    w.run()
    assert epic.runs == [T0]


def test_a_backoff_wait_in_the_log_is_not_resumed(tmp_path):
    """Only a usage wait is read back (row S50); a network backoff is not."""
    (tmp_path / "epic").mkdir()
    (tmp_path / "epic" / "watch.log").write_text(
        "2026-10-29T05:59:00Z WAIT reason=backoff until=2026-10-29T06:30:00Z why=x\n"
    )
    w, epic, _ = make(tmp_path, FakeClock(), [(0, "", DONE_ROWS)])
    w.run()
    assert epic.runs == [T0]


def test_only_this_runs_attempt_logs_are_read(tmp_path):
    """A network error in an earlier run's child log does not make this run's
    unnamed failure a network failure."""
    epic_dir = tmp_path / "epic"
    epic_dir.mkdir()
    old = records.start(epic_dir, "build", "a", ["python"])
    old.log.write_text("EAI_AGAIN\n")
    records.finish(old, 1, interrupted=False)
    w, _epic, _ = make(tmp_path, FakeClock(), [(1, "", [])])
    assert w.run() == watch.EXIT_FAILED


def test_a_new_attempts_log_is_read(tmp_path):
    epic_dir = tmp_path / "epic"
    epic_dir.mkdir()
    clock = FakeClock()

    class WithChild(Epic):
        def __call__(self, output):
            if not self.runs:
                a = records.start(epic_dir, "build", "a", ["python"])
                a.log.write_text("Error: connect ECONNREFUSED 1.2.3.4:443\n")
                records.finish(a, 1, interrupted=False)
            return super().__call__(output)

    epic = WithChild(clock, [(1, "", []), (0, "", DONE_ROWS)])
    w = watch.Watch(
        "acct", epic_dir, epic, lambda: epic.rows, Posts(), clock, lambda d: None
    )
    assert w.run() == watch.EXIT_DONE and len(epic.runs) == 2


# --- the command line --------------------------------------------------------------


def test_the_command_line_parses_epic_watch():
    args = cli.build_parser().parse_args(
        ["epic", "watch", "--change", "acct", "--project-dir", "/p", "--trust-repo"]
    )
    assert (args.epic_command, args.change, args.trust_repo) == ("watch", "acct", True)
    assert args.viewer_repo is None


def test_the_epic_command_is_the_plans():
    argv = watch.epic_argv("acct", Path("/p"), session_id="s1", trust=True)
    assert argv[1:] == [
        "-m", "build_pipeline", "--session-id", "s1", "build", "--change", "acct",
        "--project-dir", "/p", "--auto", "--trust-repo",
    ]  # fmt: skip
    assert cli.build_parser().parse_args(argv[3:]).command == "build"


def test_slack_post_runs_the_pipeline_viewers_post(monkeypatch):
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "SLACK:SKIPPED:no token\n", ""

    def fake_run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw.get("input")
        return Done()

    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    line = watch.slack_post(Path("/repo"), "acct", "k", "hello")
    assert line == "SLACK:SKIPPED:no token" and seen["input"] == "hello"
    argv = seen["argv"]
    assert argv[argv.index("--directory") + 1] == str(watch.pipeline_viewer_dir())
    assert argv[argv.index("pipeline_viewer") + 1 :] == [
        "post", "--repo", "/repo", "--change", "acct", "--key", "k",
    ]  # fmt: skip
    assert watch.pipeline_viewer_dir().is_dir()
