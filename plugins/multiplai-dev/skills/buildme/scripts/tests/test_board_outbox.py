"""The board outbox (plan-factory-merged-2026-10-29.md, item 70) and its stop
row S52: a send failed, or was killed mid-send, leaves the outbox line not
marked sent; the next run sends it again from the file, never rebuilds code,
and marks a transition already on the board sent with no second call.

Every Plane call goes to a fake `plane.py` (`epic.plane_script`) that keeps
the cards in `<root>/plane/board.json`; nothing reaches a real board.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import outbox

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
    Fakes,
    approve,
    child,
    commit_names,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    merge_waiting,
    name_commits,
    pr_url,
    records_snapshot,
    run_coordinator,
    use_workspace,
)

ONE = EpicSplitAnswer(single=False, reason="One part.", children=[child("a")])
COLUMNS = ["Shaping", "In Development", "In Review"]

FAKE_PLANE = r'''#!{python}
"""A fake plane.py: `get <ref> --json` and `update <ref> --state <name>`
on `<dir>/board.json`. `<dir>/fail` holds how many more updates fail."""
import json, pathlib, sys
d = pathlib.Path({dir!r})
args = sys.argv[1:]
with (d / "calls.log").open("a") as f:
    f.write(json.dumps(args) + "\n")
board = json.loads((d / "board.json").read_text()) if (d / "board.json").exists() else {{}}
if args[0] == "get":
    print(json.dumps({{"id": args[1], "state": {{"name": board.get(args[1])}}}}))
    sys.exit(0)
if args[0] == "update":
    fail = d / "fail"
    left = int(fail.read_text()) if fail.exists() else 0
    if left:
        fail.write_text(str(left - 1))
        print("error: PATCH -> 503 Service Unavailable", file=sys.stderr)
        sys.exit(1)
    board[args[1]] = args[args.index("--state") + 1]
    (d / "board.json").write_text(json.dumps(board))
    print("updated")
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def fake_plane(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "plane.py"
    script.write_text(FAKE_PLANE.format(python=sys.executable, dir=str(directory)))
    return script


def plane_calls(directory: Path) -> list[list[str]]:
    log = directory / "calls.log"
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def board(directory: Path) -> dict:
    path = directory / "board.json"
    return json.loads(path.read_text()) if path.exists() else {}


class Root:
    """One root: project, workspace, fakes, a one-child epic whose child has
    the ticket ENG-41 and prints the board columns, under `epic.board`."""

    def __init__(self, root: Path, monkeypatch, *, board_mode="plane", tickets=None):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, {"a": [{"board": COLUMNS}]})
        self.plane = root / "plane"
        script = fake_plane(self.plane)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(
            max_parallel=1, merge="manual", board=board_mode, plane_script=str(script)
        )
        approve(
            self.config,
            ONE,
            CallLog(root / "split.log"),
            tickets={"a": "ENG-41"} if tickets is None else tickets,
        )
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def run(self) -> int:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)
        return run_coordinator(self.config)

    def builds(self) -> list[dict]:
        return [
            c for c in CallLog(self.root / "calls.log").calls() if c["kind"] == "build"
        ]

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        epic = epic_snapshot(self.dir, self.root) or {}
        epic.pop(outbox.OUTBOX, None)  # compared by `sent` below
        return name_commits(
            {
                "epic": epic,
                "records": records_snapshot(self.dir, self.root),
                "summary": last_summary(self.dir, self.root),
            },
            names,
        )

    def sent(self) -> list[tuple[str, str, bool]]:
        return [(t.key, t.column, t.sent) for t in outbox.read(self.dir)]


def test_s52_a_failed_send_is_sent_again_from_the_file(tmp_path, monkeypatch):
    """S52: the first run's Plane refuses every move, so its transitions stay
    unsent (the second and third held behind the first). The next run sends
    them from the outbox, in order, launches no build, and ends with the card
    and the outbox of a run whose sends never failed."""
    stopped = Root(tmp_path / "stopped", monkeypatch)
    (stopped.plane / "fail").write_text("99")
    assert stopped.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert board(stopped.plane) == {}
    assert [(c, s) for _, c, s in stopped.sent()] == [(c, False) for c in COLUMNS]
    builds = stopped.builds()
    assert len(builds) == 1

    (stopped.plane / "fail").write_text("0")
    assert stopped.run() == 3
    assert stopped.builds() == builds  # never rebuilds code
    assert board(stopped.plane) == {"ENG-41": "In Review"}
    updates = [c for c in plane_calls(stopped.plane) if c[0] == "update"]
    assert [c[3] for c in updates[-3:]] == COLUMNS  # sent again, in order

    clean = Root(tmp_path / "clean", monkeypatch)
    assert clean.run() == 3
    assert board(clean.plane) == board(stopped.plane)
    assert stopped.sent() == clean.sent()
    assert all(s for _, _, s in clean.sent())
    s, c = stopped.snapshot(), clean.snapshot()
    assert s["records"] == c["records"]
    assert s["epic"] == c["epic"]


class FakeBoard:
    """An in-process stand-in for `PlaneBoard`."""

    def __init__(self, cards: dict[str, str] | None = None):
        self.cards = dict(cards or {})
        self.moves: list[tuple[str, str]] = []
        self.reads: list[str] = []

    def move(self, ref: str, column: str) -> str | None:
        self.moves.append((ref, column))
        self.cards[ref] = column
        return None

    def column_of(self, ref: str):
        self.reads.append(ref)
        return self.cards.get(ref), None


def _one_transition(epic_dir: Path) -> None:
    outbox.append(
        epic_dir,
        [{"event": "transition", "key": "1:0", "child": "a", "column": "In Review"}],
    )


def test_s52_killed_mid_send_after_the_card_moved_marks_it_sent_with_no_second_call(
    tmp_path,
):
    """S52: killed after `sending` was saved and after Plane moved the card:
    the resume reads the card, finds it in the column, and marks the line
    sent with no update. The outbox then says what a never-stopped send says."""
    stopped, clean = tmp_path / "stopped", tmp_path / "clean"
    _one_transition(stopped)
    outbox.append(stopped, [{"event": "sending", "key": "1:0"}])
    fake = FakeBoard({"ENG-41": "In Review"})
    outbox.drain(stopped, lambda cid: "ENG-41", fake)
    assert fake.moves == [] and fake.reads == ["ENG-41"]

    _one_transition(clean)
    outbox.drain(clean, lambda cid: "ENG-41", FakeBoard())
    assert [(t.key, t.sent) for t in outbox.read(stopped)] == [
        (t.key, t.sent) for t in outbox.read(clean)
    ]


def test_s52_killed_mid_send_before_the_card_moved_sends_again(tmp_path):
    """S52: killed after `sending` was saved, before Plane moved the card:
    the resume reads the card, finds another column, and sends once."""
    _one_transition(tmp_path)
    outbox.append(tmp_path, [{"event": "sending", "key": "1:0"}])
    fake = FakeBoard({"ENG-41": "Planning"})
    outbox.drain(tmp_path, lambda cid: "ENG-41", fake)
    assert fake.moves == [("ENG-41", "In Review")]
    assert [t.sent for t in outbox.read(tmp_path)] == [True]


def test_a_line_cut_short_is_skipped_and_the_next_append_starts_a_new_line(tmp_path):
    _one_transition(tmp_path)
    with (tmp_path / outbox.OUTBOX).open("a") as f:
        f.write('{"event": "sent", "key": "1:')  # a kill mid-write
    outbox.append(tmp_path, [{"event": "sent", "key": "1:0"}])
    assert [t.sent for t in outbox.read(tmp_path)] == [True]


def test_collect_adds_each_board_line_once(tmp_path):
    from build_pipeline.epic import records

    a = records.start(tmp_path, "build", "a", ["python"])
    a.log.write_text(
        "WORKTREE:/w\nBOARD:e-a:Shaping\nBOARD:e-a:In Dev"
    )  # no newline yet
    assert outbox.collect(tmp_path, records.attempts(tmp_path)) == 1
    with a.log.open("a") as f:
        f.write("elopment\nPHASE:X:COMPLETE\n")
    assert outbox.collect(tmp_path, records.attempts(tmp_path)) == 1
    assert outbox.collect(tmp_path, records.attempts(tmp_path)) == 0
    assert outbox.unsent(tmp_path) == {"a": ["Shaping", "In Development"]}


def test_a_child_with_no_ticket_is_not_sent(tmp_path):
    _one_transition(tmp_path)
    fake = FakeBoard()
    outbox.drain(tmp_path, lambda cid: None, fake)
    assert fake.moves == [] and outbox.unsent(tmp_path) == {"a": ["In Review"]}


def test_board_off_writes_no_outbox_and_calls_no_plane(tmp_path, monkeypatch):
    """`epic.board: off` (the default) keeps today's behaviour."""
    assert EpicSettings().board == "off"
    off = Root(tmp_path, monkeypatch, board_mode="off")
    assert off.run() == 3
    assert not (off.dir / outbox.OUTBOX).exists()
    assert plane_calls(off.plane) == []


def test_plane_board_runs_the_script(tmp_path):
    plane = tmp_path / "plane"
    pb = outbox.PlaneBoard(fake_plane(plane))
    assert pb.move("ENG-1", "In Review") is None
    assert pb.column_of("ENG-1") == ("In Review", None)
    (plane / "fail").write_text("1")
    assert "503" in pb.move("ENG-1", "Planning")
    assert outbox.PlaneBoard(None).move("ENG-1", "X") == outbox.NO_SCRIPT


def test_plane_script_is_found_beside_this_plugin():
    found = outbox.plane_script(None)
    assert found is not None and found.parts[-5:] == (
        "multiplai-pm",
        "skills",
        "plane",
        "scripts",
        "plane.py",
    )
    assert outbox.plane_script("~/x/plane.py") == Path.home() / "x" / "plane.py"
