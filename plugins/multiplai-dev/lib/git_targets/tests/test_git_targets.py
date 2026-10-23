"""git_targets on a small fixture repo with a branch. No network: `gh` is a stub script on PATH."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import git_targets as gt
import pytest

DATE = "2026-01-02T03:04:05+00:00"


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": DATE,
        "GIT_COMMITTER_DATE": DATE,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _lines(n: int, word: str) -> str:
    return "".join(f"{word} {i}\n" for i in range(1, n + 1))


@pytest.fixture
def repo(tmp_path) -> tuple[Path, str, str]:
    """main: keep.py, edit.py, gone.py, old_name.py. feature: edit.py changed, gone.py deleted,
    old_name.py renamed, new.py added, one commit each for two subjects."""
    r = tmp_path / "repo"
    r.mkdir()
    (r / "keep.py").write_text(_lines(5, "keep"))
    (r / "edit.py").write_text(_lines(10, "edit"))
    (r / "gone.py").write_text(_lines(3, "gone"))
    (r / "old_name.py").write_text(_lines(20, "same"))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    base = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "-b", "feature")
    lines = _lines(10, "edit").splitlines(keepends=True)
    lines[2] = "edit three changed\n"  # line 3 changed
    del lines[6]  # old line 7 removed
    lines.append("edit added\n")
    (r / "edit.py").write_text("".join(lines))
    (r / "gone.py").unlink()
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "change edit, drop gone")
    _git(r, "mv", "old_name.py", "new_name.py")
    (r / "new.py").write_text(_lines(2, "new"))
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "rename and add")
    head = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    return r, base, head


def test_rev_parse_and_merge_base(repo):
    r, base, head = repo
    assert gt.rev_parse(r, "feature") == head
    assert gt.rev_parse(r, "no-such-ref") is None
    assert gt.merge_base(r, "main", "feature") == base
    assert gt.merge_base(r, "main", "0" * 40) is None


def test_changed_files_lists_each_status_with_its_lines(repo):
    r, base, head = repo
    files = {f.path: f for f in gt.changed_files(r, base, head)}
    assert set(files) == {"edit.py", "gone.py", "new_name.py", "new.py"}
    assert files["new.py"].status == "A" and files["new.py"].added == [(1, 2)]
    assert files["gone.py"].status == "D" and files["gone.py"].removed == [(1, 3)]
    ren = files["new_name.py"]
    assert (ren.status, ren.old_path, ren.added, ren.removed) == (
        "R",
        "old_name.py",
        [],
        [],
    )
    ed = files["edit.py"]
    assert ed.status == "M"
    assert ed.removed == [(3, 3), (7, 7)]  # the changed line and the pure deletion
    assert ed.added == [(3, 3), (10, 10)]
    assert json.loads(json.dumps(ed.to_dict()))["added"] == [[3, 3], [10, 10]]


def test_changed_files_falls_back_to_one_diff_per_file(repo, monkeypatch):
    r, base, head = repo
    want = [f.to_dict() for f in gt.changed_files(r, base, head)]
    real = gt._hunks
    calls = []

    def short(patch):
        calls.append(patch)
        out = real(patch)
        return (
            out[:-1] if len(calls) == 1 else out
        )  # the whole patch "loses" a section once

    monkeypatch.setattr(gt, "_hunks", short)
    got = [f.to_dict() for f in gt.changed_files(r, base, head)]
    assert got == want and len(calls) == 1 + len(want)


def test_commit_subjects_oldest_first(repo):
    r, base, head = repo
    subjects = [s for _, s in gt.commit_subjects(r, base, head)]
    assert subjects == ["change edit, drop gone", "rename and add"]


def _stub_gh(tmp_path: Path, stdout: str, code: int = 0) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    (tmp_path / "gh-out.json").write_text(stdout)
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{tmp_path}/gh-args"\n'
        f'cat "{tmp_path}/gh-out.json"\n'
        f"exit {code}\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_pr_view_runs_gh_with_the_fields(repo, tmp_path, monkeypatch):
    r, base, head = repo
    payload = {"headRefOid": head, "baseRefOid": base, "title": "T", "body": ""}
    monkeypatch.setenv(
        "PATH", f"{_stub_gh(tmp_path, json.dumps(payload))}:/usr/bin:/bin"
    )
    assert gt.pr_view(r, 7) == payload
    assert (tmp_path / "gh-args").read_text().split() == [
        "pr",
        "view",
        "7",
        "--json",
        gt.PR_VIEW_FIELDS,
    ]


def test_pr_view_without_gh_names_range(repo, monkeypatch):
    r, _, _ = repo
    monkeypatch.setattr(gt.shutil, "which", lambda name: None)
    with pytest.raises(gt.GitTargetError, match="--range"):
        gt.pr_view(r, 7)


def test_pr_view_failure_is_an_error(repo, tmp_path, monkeypatch):
    r, _, _ = repo
    monkeypatch.setenv("PATH", f"{_stub_gh(tmp_path, '', code=1)}:/usr/bin:/bin")
    with pytest.raises(gt.GitTargetError, match="gh pr view 7 failed"):
        gt.pr_view(r, 7)


def test_archive_batches_fit_the_limit(monkeypatch):
    monkeypatch.setattr(gt, "ARCHIVE_ARGV_BYTES", 12)
    batches = gt.archive_batches(["a.py", "b.py", "c.py", "a-very-long-name.py"])
    assert batches == [["a.py", "b.py"], ["c.py"], ["a-very-long-name.py"]]
