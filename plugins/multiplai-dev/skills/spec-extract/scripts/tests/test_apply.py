"""The apply step: `run … --apply [--specs-dir <dir>]` (plan, item 21).

The uncommitted-work check runs for real: buildme's `stop_record_cli` through
`uv run --directory <skills>/buildme/scripts`, as an installed copy runs it.
make_repo's three modules give three capability files: `charge.md`, `one.md`
and `two.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from conftest import commit_file, everything, git, make_repo, read_state, run, write
from spec_extract_pipeline import apply as apply_mod

CAPS = ["charge.md", "one.md", "two.md"]


def _tree(repo: Path, root: Path, *extra, tree: str = "HEAD") -> int:
    return run("run", "--repo", repo, "--tree", tree, "--out", root, *extra)


def _folder(root: Path) -> Path:
    (folder,) = [p for p in (root / "widgets").iterdir() if p.name != ".locks"]
    return folder


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("SPEC_EXTRACT:")]


def test_check_runs_buildme_stop_record_cli():
    """`<skills>` is `parents[3]` of apply.py; the module it names is there."""
    argv = apply_mod.check_argv()
    assert argv[:3] == ["uv", "run", "--directory"]
    assert argv[4:] == ["python", "-m", "build_pipeline.stop_record_cli"]
    assert (Path(argv[3]) / "build_pipeline" / "stop_record_cli.py").is_file()
    assert Path(argv[3]) == Path(apply_mod.__file__).resolve().parents[3] / "buildme" / "scripts"


def test_dir_without_specs_dir_is_refused(tmp_path, capsys, fake_call):
    src = tmp_path / "x" / "y"
    write(src / "m.py", "def f():\n    return 1\n")
    before = everything(tmp_path)
    assert run("run", "--dir", src, "--out", tmp_path / "out", "--apply") == 2
    assert "SPEC_EXTRACT:APPLY_NEEDS_SPECS_DIR" in _lines(capsys.readouterr().out)
    assert everything(tmp_path) == before  # nothing written, the run folder included
    assert not fake_call.calls


def test_specs_dir_without_apply_is_refused(tmp_path, capsys, fake_call):
    repo = make_repo(tmp_path / "widgets")
    assert _tree(repo, tmp_path / "out", "--specs-dir", repo / "specs") == 2
    assert "--specs-dir" in capsys.readouterr().err
    assert not fake_call.calls


def test_specs_dir_outside_any_work_tree_is_refused(tmp_path, capsys, fake_call):
    repo = make_repo(tmp_path / "widgets")
    plain = tmp_path / "plain"
    plain.mkdir()
    specs = plain / "specs"
    assert _tree(repo, tmp_path / "out", "--apply", "--specs-dir", specs) == 2
    err = capsys.readouterr().err
    assert str(specs) in err and "git work tree" in err
    assert list(plain.iterdir()) == []
    assert not fake_call.calls  # refused before any model call


def test_specs_dir_under_the_run_folder_is_refused(tmp_path, capsys, fake_call):
    """A `--dir` run's `<run>/source/` is a git work tree, but not the user's."""
    src = tmp_path / "x" / "y"
    write(src / "m.py", "def f():\n    return 1\n")
    root = tmp_path / "out"
    name = "dir-y-" + hashlib.sha256(str(src.resolve()).encode()).hexdigest()[:6]
    specs = root / "y" / name / "source" / "specs"
    assert run("run", "--dir", src, "--out", root, "--apply", "--specs-dir", specs) == 2
    assert "inside the run folder" in capsys.readouterr().err
    assert not fake_call.calls


def _repo_with_registry(path: Path) -> Path:
    """make_repo, plus a committed registry: charge.md with other bytes, one unrelated file."""
    repo = make_repo(path)
    write(repo / "specs" / "registry" / "charge.md", "## ADDED Requirements\n\nwritten by hand\n")
    write(repo / "specs" / "registry" / "notes.md", "notes\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "registry")
    return repo


def test_apply_copies_new_files_and_commits_nothing(tmp_path, capsys):
    repo = _repo_with_registry(tmp_path / "widgets")
    head = git(repo, "rev-parse", "HEAD")
    root = tmp_path / "out"
    assert _tree(repo, root, "--apply") == 0
    run_dir = _folder(root)
    registry = repo / "specs" / "registry"
    lines = _lines(capsys.readouterr().out)
    assert lines[-2:] == ["SPEC_EXTRACT:APPLIED 2",
                          f"SPEC_EXTRACT:SKIPPED_EXISTING charge {run_dir / 'registry' / 'charge.md'}"]
    for name in ["one.md", "two.md"]:
        assert (registry / name).read_bytes() == (run_dir / "registry" / name).read_bytes()
    assert (registry / "charge.md").read_text() == "## ADDED Requirements\n\nwritten by hand\n"
    assert git(repo, "rev-parse", "HEAD") == head  # commits nothing
    assert git(repo, "status", "--porcelain") == "?? specs/registry/one.md\n?? specs/registry/two.md"
    assert not (registry / apply_mod.APPLY_TMP).exists()
    state = read_state(run_dir)
    assert state["steps_done"][-2:] == ["render", "apply"]
    assert state["apply_stop"] == {"head": None, "edits": {
        n: _sha((run_dir / "registry" / n).read_bytes()) for n in ["one.md", "two.md"]}}


def test_identical_file_counts_as_applied(tmp_path, capsys):
    repo = make_repo(tmp_path / "widgets")
    root, sha = tmp_path / "out", git(repo, "rev-parse", "HEAD")
    assert _tree(repo, root) == 0
    run_dir = _folder(root)
    for name in CAPS:  # an earlier apply, committed; the run is still the first commit's
        write(repo / "specs" / "registry" / name, (run_dir / "registry" / name).read_text())
    commit_file(repo, "specs/registry/one.md", (run_dir / "registry" / "one.md").read_text())
    capsys.readouterr()
    assert _tree(repo, root, "--apply", tree=sha) == 0
    assert _lines(capsys.readouterr().out)[-1] == "SPEC_EXTRACT:APPLIED 3"
    assert git(repo, "status", "--porcelain") == ""
    assert read_state(run_dir)["apply_stop"] is None  # nothing copied, nothing recorded


def test_apply_after_render_makes_no_call_and_copies_edited_files(tmp_path, fake_call):
    """Row R16: files edited in `<run>/registry/` after render are copied as they are."""
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert _tree(repo, root) == 0
    calls = dict(fake_call.calls)
    run_dir = _folder(root)
    edited = (run_dir / "registry" / "one.md").read_text() + "\nedited by hand\n"
    (run_dir / "registry" / "one.md").write_text(edited)
    assert _tree(repo, root, "--apply") == 0
    assert dict(fake_call.calls) == calls
    assert (repo / "specs" / "registry" / "one.md").read_text() == edited
    assert sorted(p.name for p in (repo / "specs" / "registry").iterdir()) == CAPS


def test_apply_again_changes_nothing(tmp_path, capsys, fake_call):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert _tree(repo, root, "--apply") == 0
    run_dir = _folder(root)
    registry, state = everything(repo / "specs"), read_state(run_dir)
    calls = dict(fake_call.calls)
    capsys.readouterr()
    assert _tree(repo, root, "--apply") == 0
    assert _lines(capsys.readouterr().out)[-1] == "SPEC_EXTRACT:APPLIED 3"
    assert everything(repo / "specs") == registry
    assert read_state(run_dir) == state
    assert dict(fake_call.calls) == calls


def test_dir_with_specs_dir_creates_the_registry(tmp_path, capsys):
    """`--dir` with `--specs-dir` in a repository that has a commit but no `specs/`."""
    src = tmp_path / "x" / "y"
    write(src / "m.py", "def f():\n    return 1\n")
    target = tmp_path / "t"
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    commit_file(target, "README", "hi\n")
    assert run("run", "--dir", src, "--out", tmp_path / "out", "--apply",
               "--specs-dir", target / "specs") == 0
    assert _lines(capsys.readouterr().out)[-1] == "SPEC_EXTRACT:APPLIED 1"
    assert [p.name for p in (target / "specs" / "registry").iterdir()] == ["m.md"]
    assert not (src / "specs").exists()


def test_repo_with_no_commit_is_refused(tmp_path, capsys):
    """`git diff HEAD` fails in a repository with no commit, so the check refuses."""
    repo = make_repo(tmp_path / "widgets")
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    assert _tree(repo, tmp_path / "out", "--apply", "--specs-dir", empty / "specs") == 2
    assert "git could not list the changes" in capsys.readouterr().err
    assert list((empty / "specs" / "registry").iterdir()) == []


@pytest.mark.parametrize("script, says", [
    ("print('not json')", "not one JSON line"),
    ("print('{\"reason\": null}'); print('{\"reason\": null}')", "not one JSON line"),
    ("print('[1]')", "not one JSON line"),
    ("import sys; sys.stderr.write('boom one\\nboom two\\n'); sys.exit(1)", "boom two"),
    ("import time; time.sleep(30)", "no answer within"),
])
def test_check_that_cannot_run_refuses(tmp_path, monkeypatch, capsys, script, says):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert _tree(repo, root) == 0
    monkeypatch.setattr(apply_mod, "check_argv", lambda: [sys.executable, "-c", script])
    monkeypatch.setattr(apply_mod, "CHECK_TIMEOUT", 1)
    assert _tree(repo, root, "--apply") == 2
    err = capsys.readouterr().err
    assert "the uncommitted-work check could not run" in err and says in err
    assert list((repo / "specs" / "registry").iterdir()) == []
    assert "apply" not in read_state(_folder(root))["steps_done"]


def test_missing_uv_refuses(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "widgets")
    monkeypatch.setattr(apply_mod, "check_argv", lambda: [str(tmp_path / "no-such-uv")])
    assert _tree(repo, tmp_path / "out", "--apply") == 2
    assert "the uncommitted-work check could not run" in capsys.readouterr().err
