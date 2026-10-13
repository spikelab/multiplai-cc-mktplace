"""One test per row of the plan's "Stop and resume states" table that needs one
(Decided 11: resume could destroy work or pay for a model call twice).

Each test stops the step at its row's point, resumes with the same command, and
compares with a reference run that never stopped, on an identical fixture with
the same fixed clock: the same `<run>` under its root, byte-equal outputs
(`conftest.outputs`), and the same saved target and steps. PR 1a's rows: R13,
R18, R19, R23. Later PRs add theirs here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (FIXED, SCRIPTS_DIR, Killed, everything, fake_state, git, make_repo, outputs,
                      run, source_head, write)
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
def test_R13_another_process_holds_the_lock(tmp_path, capsys, held):
    t = Target("tree", tmp_path / "a")
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

@pytest.mark.parametrize("change", ["deleted", "renamed", "emptied"])
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
    else:
        shutil.rmtree(first)
        first.mkdir()

    capsys.readouterr()
    assert t.run(root) == 0
    assert f"SPEC_EXTRACT:RESOLVED {ours}" in capsys.readouterr().out
    assert first.exists() == (change == "emptied")
    if change == "emptied":
        assert list(first.iterdir()) == []

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
    before = everything(root)
    capsys.readouterr()
    assert t.run(root) == 1
    err = capsys.readouterr().err
    assert str(run_dir / "state.json") in err and "--restart" in err
    assert everything(root) == before
