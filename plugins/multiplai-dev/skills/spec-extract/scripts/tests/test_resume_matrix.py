"""One test per row of the plan's "Stop and resume states" table that needs one
(Decided 11: resume could destroy work or pay for a model call twice).

Each test stops the step at its row's point, resumes with the same command, and
compares with a reference run that never stopped, on an identical fixture with
the same fixed clock: the same `<run>` under its root, byte-equal outputs
(`conftest.outputs`), and the same saved target and steps. PR 1a's rows: R13,
R18, R19, R23, R27. PR 1b's: R4, R5, R6, R12, R26, which also check that the
stopped run's answers equal the reference's for every key answered in both, and
that the fake `CallFn` was called no more often per unit than in the reference.
Later PRs add theirs here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (FIXED, SCRIPTS_DIR, Killed, commit_file, everything, fake_state, git, make_repo,
                      make_units_repo, outputs, read_state, run, source_head, unit_file, write)
from spec_extract_pipeline import report as report_mod
from spec_extract_pipeline import state as state_mod
from spec_extract_pipeline import target as target_mod

OTHER = "https://github.com/other/widgets.git"


def _state_text(run_dir: Path, root: Path) -> dict:
    """`state.json` with the root replaced, so two runs under two roots compare."""
    text = (run_dir / "state.json").read_text(encoding="utf-8").replace(str(root), "<root>")
    return json.loads(text)


def assert_same_as_reference(run_dir: Path, root: Path, ref_dir: Path, ref_root: Path) -> None:
    assert run_dir.relative_to(root) == ref_dir.relative_to(ref_root)
    assert outputs(run_dir) == outputs(ref_dir)
    assert _state_text(run_dir, root) == _state_text(ref_dir, ref_root)
    assert source_head(run_dir) == source_head(ref_dir)


class Target:
    """One fixture: a target's command line and where its run folder lands."""

    def __init__(self, kind: str, base: Path):
        self.kind = kind
        if kind == "tree":
            self.repo = make_repo(base / "widgets")
            sha = git(self.repo, "rev-parse", "HEAD")
            self.rel = Path("widgets") / f"tree@{sha[:12]}"
            self.argv = ["--repo", self.repo, "--tree", "HEAD"]
        else:
            src = base / "x" / "y"
            write(src / "pkg" / "mod.py", "def f():\n    return 1\n")
            name = "dir-y-" + hashlib.sha256(str(src.resolve()).encode()).hexdigest()[:6]
            self.rel = Path("y") / name
            self.argv = ["--dir", src]

    def run(self, root: Path, *extra: str) -> int:
        return run("run", *self.argv, "--out", root, *extra)


# --- R13: another process holds a candidate's lock -------------------------------------

_HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from spec_extract_pipeline import runlock
with runlock.hold(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.read()
"""


def _hold(run_dir: Path) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(SCRIPTS_DIR), str(run_dir)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def _release(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=30)


@pytest.mark.parametrize("held", ["state_names_this_target", "no_state_json", "only_the_lock_file"])
@pytest.mark.parametrize("kind", ["tree", "dir"])
def test_R13_another_process_holds_the_lock(tmp_path, capsys, kind, held):
    t = Target(kind, tmp_path / "a")
    root = tmp_path / "out"
    run_dir = root / t.rel
    if held == "state_names_this_target":
        assert t.run(root) == 0
    elif held == "no_state_json":
        run_dir.mkdir(parents=True)
    else:
        run_dir.parent.mkdir(parents=True)
    holder = _hold(run_dir)
    try:
        before = everything(root)
        capsys.readouterr()
        assert t.run(root) == 5
        err = capsys.readouterr().err
        assert f"pid {holder.pid}" in err
        assert everything(root) == before  # wrote nothing, and no second folder
    finally:
        _release(holder)

    assert t.run(root) == 0
    ref_root = tmp_path / "ref"  # the same repository: the same commit and remote
    assert t.run(ref_root) == 0
    assert_same_as_reference(run_dir, root, ref_root / t.rel, ref_root)
    assert sorted(p.name for p in run_dir.parent.iterdir()) == sorted([".locks", run_dir.name])


# --- R18: --restart stopped before or after its rename ------------------------------------

@pytest.mark.parametrize("resume", ["restart_again", "plain"])
@pytest.mark.parametrize("stop_at", ["before_rename", "after_rename", "after_new_state"])
@pytest.mark.parametrize("kind", ["tree", "dir"])
def test_R18_restart_stopped(tmp_path, monkeypatch, kind, stop_at, resume):
    t = Target(kind, tmp_path / "a")
    root = tmp_path / "out"
    run_dir = root / t.rel
    assert t.run(root) == 0
    (run_dir / "mine.txt").write_text("earlier work")
    stamp = FIXED.strftime("%Y%m%d%H%M%S")
    old = run_dir.parent / f"{run_dir.name}.old-{stamp}"

    real_restart, real_snapshot = target_mod.restart, target_mod.snapshot
    if stop_at == "before_rename":
        def restart(*a, **kw):
            raise Killed
        monkeypatch.setattr(target_mod, "restart", restart)
    elif stop_at == "after_rename":
        def restart(*a, **kw):
            real_restart(*a, **kw)
            raise Killed
        monkeypatch.setattr(target_mod, "restart", restart)
    else:
        def snapshot(*a, **kw):
            raise Killed
        monkeypatch.setattr(target_mod, "snapshot", snapshot)
    with pytest.raises(Killed):
        t.run(root, "--restart")
    monkeypatch.setattr(target_mod, "restart", real_restart)
    monkeypatch.setattr(target_mod, "snapshot", real_snapshot)

    renamed = everything(old) if old.exists() else None
    if stop_at == "before_rename":
        assert renamed is None and (run_dir / "mine.txt").read_text() == "earlier work"
    else:
        assert (old / "mine.txt").read_text() == "earlier work"

    if resume == "plain" and stop_at == "before_rename":
        # Without --restart the old run is resumed as it is.
        assert t.run(root) == 0
        assert (run_dir / "mine.txt").read_text() == "earlier work"
        return

    assert t.run(root, *(["--restart"] if resume == "restart_again" else [])) == 0
    # Nothing is deleted: what the first rename moved is still there, unchanged.
    assert (old / "mine.txt").read_text() == "earlier work"
    if renamed is not None:
        assert everything(old) == renamed
    if stop_at == "after_new_state" and resume == "restart_again":
        # The partial new run was renamed too, to the next free name.
        assert (run_dir.parent / f"{run_dir.name}.old-{stamp}-2" / "state.json").is_file()

    ref_root = tmp_path / "ref"
    ref_run = ref_root / t.rel
    assert t.run(ref_root) == 0
    (ref_run / "mine.txt").write_text("earlier work")
    assert t.run(ref_root, "--restart") == 0
    assert_same_as_reference(run_dir, root, ref_run, ref_root)


# --- R19: a resume finds its folder after an earlier candidate changed ------------------------

@pytest.mark.parametrize("change", ["deleted", "renamed", "emptied", "made_unreadable"])
def test_R19_resume_keeps_its_folder(tmp_path, monkeypatch, capsys, change):
    t = Target("tree", tmp_path / "a")
    root = tmp_path / "out"
    first = root / t.rel  # widgets/tree@…, another remote's run
    fake_state(first, kind="tree", path="", remote_url=OTHER)
    ours = root / "widgets@acme" / t.rel.name

    real_snapshot = target_mod.snapshot

    def snapshot(*a, **kw):
        raise Killed

    monkeypatch.setattr(target_mod, "snapshot", snapshot)
    with pytest.raises(Killed):
        t.run(root)
    monkeypatch.setattr(target_mod, "snapshot", real_snapshot)
    assert (ours / "state.json").is_file()

    if change == "deleted":
        shutil.rmtree(first)
    elif change == "renamed":
        first.rename(first.parent / "moved")
    elif change == "emptied":
        shutil.rmtree(first)
        first.mkdir()
    else:  # another target's state.json, now unreadable, does not name this target
        (first / "state.json").write_text("{not json", encoding="utf-8")

    capsys.readouterr()
    assert t.run(root) == 0
    assert f"SPEC_EXTRACT:RESOLVED {ours}" in capsys.readouterr().out
    assert first.exists() == (change in ("emptied", "made_unreadable"))
    if change == "emptied":
        assert list(first.iterdir()) == []
    if change == "made_unreadable":
        assert sorted(p.name for p in first.iterdir()) == ["state.json"]
        assert (first / "state.json").read_text(encoding="utf-8") == "{not json"

    ref_root = tmp_path / "ref"
    fake_state(ref_root / t.rel, kind="tree", path="", remote_url=OTHER)
    assert t.run(ref_root) == 0
    assert_same_as_reference(ours, root, ref_root / "widgets@acme" / t.rel.name, ref_root)


# --- R23: an unreadable state.json -------------------------------------------------------

@pytest.mark.parametrize("kind", ["tree", "dir"])
def test_R23_unreadable_state(tmp_path, capsys, kind):
    t = Target(kind, tmp_path / "a")
    root = tmp_path / "out"
    run_dir = root / t.rel
    assert t.run(root) == 0
    (run_dir / "state.json").write_text("{not json", encoding="utf-8")
    before = everything(run_dir)  # under <run>: taking the lock rewrites only the holder note beside it
    capsys.readouterr()
    assert t.run(root) == 1
    err = capsys.readouterr().err
    assert str(run_dir / "state.json") in err and "--restart" in err
    assert "--out" in err  # the folder may hold another target's run: --restart would move it aside
    assert everything(run_dir) == before
    assert sorted(p.name for p in run_dir.parent.iterdir()) == sorted([".locks", run_dir.name])


# --- R27: a --dir run's source/ lost its import commit after state was written -------------

@pytest.mark.parametrize("damage", ["source_deleted", "git_deleted", "another_commit"])
def test_R27_dir_source_without_its_import(tmp_path, capsys, damage):
    """The saved target names the import commit; a re-import would give another one,
    so the run would go on with a state the snapshot and source contradict."""
    t = Target("dir", tmp_path / "a")
    root = tmp_path / "out"
    run_dir = root / t.rel
    assert t.run(root) == 0
    if damage == "source_deleted":
        shutil.rmtree(run_dir / "source")
    elif damage == "git_deleted":
        shutil.rmtree(run_dir / "source" / ".git")
    else:
        write(run_dir / "source" / "pkg" / "new.py", "x = 1\n")
        git(run_dir / "source", "add", "-A")
        git(run_dir / "source", "commit", "-q", "-m", "user commit")
    before = everything(run_dir)
    capsys.readouterr()
    assert t.run(root) == 1
    err = capsys.readouterr().err
    assert str(run_dir / "source") in err and "--restart" in err
    assert everything(run_dir) == before

    assert t.run(root, "--restart") == 0
    ref_root = tmp_path / "ref"
    assert t.run(ref_root) == 0
    assert_same_as_reference(run_dir, root, ref_root / t.rel, ref_root)


# --- PR 1b: explore rows ------------------------------------------------------------------

def _tree(repo: Path, root: Path, *extra) -> int:
    return run("run", "--repo", repo, "--tree", "HEAD", "--out", root, *extra)


def _folder(root: Path) -> Path:
    (folder,) = [p for p in (root / "widgets").iterdir() if p.name != ".locks"]
    return folder


def _reference(fake, script: dict[str, list[str]], go, expect: int = 0) -> dict[str, int]:
    """Run *go* with a fresh copy of *script*; return the fake's calls per unit and reset it."""
    fake.calls.clear()
    fake.script.clear()
    fake.script.update({k: list(v) for k, v in script.items()})
    assert go() == expect
    calls = dict(fake.calls)
    fake.calls.clear()
    fake.script.clear()
    return calls


def _answered(state: dict) -> dict[str, dict]:
    return {k: v for k, v in state["explore_answers"].items() if "error" not in v}


def assert_explore_matches(run_dir: Path, root: Path, ref_dir: Path, ref_root: Path,
                           calls: dict[str, int], ref_calls: dict[str, int]) -> None:
    assert run_dir.relative_to(root) == ref_dir.relative_to(ref_root)
    assert outputs(run_dir) == outputs(ref_dir)
    mine, ref = _answered(read_state(run_dir)), _answered(read_state(ref_dir))
    for key in mine.keys() & ref.keys():
        assert mine[key] == ref[key], key
    assert all(n <= ref_calls.get(unit, 0) for unit, n in calls.items()), (calls, ref_calls)


def _stopped(go) -> None:
    """Run *go* and expect it killed. A `Killed` raised inside an explore task reaches here
    wrapped in a BaseExceptionGroup: `code_facts.bounded`'s `except* Exception` matches
    only exceptions and re-raises what is left as a group."""
    with pytest.raises((Killed, BaseExceptionGroup)) as e:
        go()
    assert e.type is Killed or e.group_contains(Killed)


def _stop_after_answers(monkeypatch, k: int) -> None:
    """Kill the run right after the save that holds its k-th explore answer.

    After the kill nothing reaches the disk: every later save raises without
    writing, as in a process that is gone. (Explore tasks already queued on the
    event loop still run their first steps after one task raises.)
    """
    real = state_mod.save
    dead = []

    def save(state, run_dir):
        if dead:
            raise Killed("the process is gone")
        real(state, run_dir)
        if len(state.explore_answers) >= k:
            dead.append(True)
            raise Killed(f"stopped after {k} answers")

    monkeypatch.setattr(state_mod, "save", save)


# --- R4: explore stopped with k of n answers returned ---------------------------------------

def test_R4_explore_stopped_part_way(tmp_path, monkeypatch, fake_call):
    repo = make_units_repo(tmp_path / "widgets", 4)
    script = {"d0/mod.py": ["fail"]}  # u1's first call raises; round 2 answers it
    ref_root = tmp_path / "ref"
    ref_calls = _reference(fake_call, script, lambda: _tree(repo, ref_root))

    root = tmp_path / "out"
    fake_call.script.update({k: list(v) for k, v in script.items()})
    with monkeypatch.context() as m:
        _stop_after_answers(m, 2)  # u1's error and u2's answer are saved; u3 and u4 never start
        _stopped(lambda: _tree(repo, root))
    stopped = read_state(_folder(root))
    assert stopped["explore_attempts"] == {"unit:u1": 1, "unit:u2": 1}
    assert list(stopped["explore_answers"]) == ["unit:u1", "unit:u2"]
    assert _tree(repo, root) == 0

    run_dir, ref_dir = _folder(root), _folder(ref_root)
    assert_explore_matches(run_dir, root, ref_dir, ref_root, dict(fake_call.calls), ref_calls)
    # The answers came back in another order, and facts.json is still the same bytes.
    assert list(read_state(run_dir)["explore_answers"]) != list(read_state(ref_dir)["explore_answers"])
    assert (run_dir / "facts.json").read_bytes() == (ref_dir / "facts.json").read_bytes()
    assert read_state(run_dir)["explore_attempts"] == read_state(ref_dir)["explore_attempts"]


# --- R5: the same unit stopped twice -----------------------------------------------------

def test_R5_unit_stopped_twice_is_not_asked_again(tmp_path, fake_call):
    repo = make_units_repo(tmp_path / "widgets", 3)
    ref_root = tmp_path / "ref"
    ref_calls = _reference(fake_call, {"d1/mod.py": ["fail", "fail"]}, lambda: _tree(repo, ref_root))

    root = tmp_path / "out"
    for _ in range(2):
        fake_call.script["d1/mod.py"] = ["kill"]
        _stopped(lambda: _tree(repo, root))
    assert read_state(_folder(root))["explore_attempts"]["unit:u2"] == 2
    assert _tree(repo, root) == 0
    assert fake_call.calls["d1/mod.py"] == 2  # never a third time

    run_dir, ref_dir = _folder(root), _folder(ref_root)
    assert_explore_matches(run_dir, root, ref_dir, ref_root, dict(fake_call.calls), ref_calls)
    assert "- `d1/mod.py`: not analysed: explore gave no answer in 2 attempts" in (run_dir / "report.md").read_text()


# --- R6: every answer saved, before `explore` is in steps_done -----------------------------

@pytest.mark.parametrize("left", ["absent", "old", "tmp"])
def test_R6_answers_saved_outputs_missing(tmp_path, monkeypatch, fake_call, left):
    repo = make_units_repo(tmp_path / "widgets", 2)
    ref_root = tmp_path / "ref"
    ref_calls = _reference(fake_call, {}, lambda: _tree(repo, ref_root))

    root = tmp_path / "out"
    real = report_mod.write_output

    def write_output(run_dir, name, text):
        if name == "facts.json":
            if left == "tmp":
                (run_dir / "facts.json.tmp").write_text(text[: len(text) // 2], encoding="utf-8")
            raise Killed("stopped while writing facts.json")
        return real(run_dir, name, text)

    with monkeypatch.context() as m:
        m.setattr(report_mod, "write_output", write_output)
        _stopped(lambda: _tree(repo, root))
    run_dir = _folder(root)
    assert "explore" not in read_state(run_dir)["steps_done"]
    if left == "old":
        (run_dir / "facts.json").write_text("stale\n")
        (run_dir / "report.md").write_text("stale\n")
    calls_before = dict(fake_call.calls)

    assert _tree(repo, root) == 0
    assert dict(fake_call.calls) == calls_before  # no call
    assert read_state(run_dir)["steps_done"] == ["resolve", "scope", "explore"]
    assert not (run_dir / "facts.json.tmp").exists()
    assert_explore_matches(run_dir, root, _folder(ref_root), ref_root, dict(fake_call.calls), ref_calls)


# --- R12: the budget is reached -------------------------------------------------------------

def test_R12_budget_reached_then_raised(tmp_path, capsys, fake_call):
    repo = make_units_repo(tmp_path / "widgets", 4)
    ref_root = tmp_path / "ref"
    ref_calls = _reference(fake_call, {}, lambda: _tree(repo, ref_root))

    root = tmp_path / "out"
    assert _tree(repo, root, "--max-usd", "2.5") == 3  # $1 a call: u1-u3 pass the check, u4's sees $3
    state = read_state(_folder(root))
    assert state["budget"]["cost_usd"] == 3.0
    assert "unit:u4" not in state["explore_attempts"]  # no counted attempt for the stopped task
    calls = dict(fake_call.calls)
    assert _tree(repo, root, "--max-usd", "2.5") == 3  # the same cap: exit 3 before any call
    assert dict(fake_call.calls) == calls
    assert _tree(repo, root, "--max-usd", "10") == 0  # a higher cap continues

    run_dir, ref_dir = _folder(root), _folder(ref_root)
    assert_explore_matches(run_dir, root, ref_dir, ref_root, dict(fake_call.calls), ref_calls)
    assert read_state(run_dir)["budget"]["cost_usd"] == read_state(ref_dir)["budget"]["cost_usd"] == 4.0
    assert read_state(run_dir)["explore_attempts"] == read_state(ref_dir)["explore_attempts"]


# --- R26: a --pr run's base moved, or the PR merged, after a stop ---------------------------

@pytest.mark.parametrize("change", ["base_moved", "merged"])
def test_R26_pr_keeps_its_base_and_scope(tmp_path, monkeypatch, pr_view, fake_call, change):
    repo = make_units_repo(tmp_path / "widgets", 3)
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    c1 = commit_file(repo, "d0/mod.py", unit_file(0, 2), "c1")
    write(repo / "d1" / "mod.py", unit_file(1, 2))
    head = commit_file(repo, "d2/mod.py", unit_file(2, 2), "head")
    git(repo, "checkout", "-q", "main")
    pr_view.info[7] = {"headRefOid": head, "baseRefOid": base}

    def go(root: Path) -> int:
        return run("run", "--repo", repo, "--pr", "7", "--out", root)

    ref_root = tmp_path / "ref"
    ref_calls = _reference(fake_call, {}, lambda: go(ref_root))
    ref_state = read_state(_folder(ref_root))
    assert ref_state["scope"]["files"] == ["d0/mod.py", "d1/mod.py", "d2/mod.py"]

    root = tmp_path / "out"
    with monkeypatch.context() as m:
        _stop_after_answers(m, 1)
        _stopped(lambda: go(root))
    assert len(read_state(_folder(root))["explore_answers"]) == 1
    if change == "base_moved":
        # Part of the PR (c1) lands on main, which moves on: the merge-base is now c1.
        git(repo, "merge", "-q", "--ff-only", c1)
        tip = commit_file(repo, "other.py", "def other():\n    pass\n", "main moves")
        assert git(repo, "merge-base", tip, head) == c1
    else:
        git(repo, "merge", "-q", "--ff-only", head)
        tip = head  # merged: the merge-base is the head itself, and the diff would be empty
    pr_view.info[7] = {"headRefOid": head, "baseRefOid": tip}

    assert go(root) == 0
    run_dir = _folder(root)
    state = read_state(run_dir)
    assert state["target"]["base_sha"] == base
    assert state["scope"] == ref_state["scope"]
    assert_explore_matches(run_dir, root, _folder(ref_root), ref_root, dict(fake_call.calls), ref_calls)
