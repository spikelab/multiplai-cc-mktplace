"""`python -m build_pipeline.stop_record_cli`: `stop_record.others_work` for a
process that cannot import buildme (spec-extract runs it as a subprocess).
Each test runs the module as a subprocess against a temporary git repo."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repo with one commit holding app.py."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("x = 1\n")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "base")
    return root


def _run(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "build_pipeline.stop_record_cli"],
                          cwd=SCRIPTS, input=stdin, capture_output=True, text=True,
                          timeout=60)


def _check(payload: dict) -> str | None:
    """The reason the module prints, after asserting it printed one JSON line
    and exited 0."""
    out = _run(json.dumps(payload))
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert len(lines) == 1, out.stdout
    answer = json.loads(lines[0])
    assert set(answer) == {"reason"}
    return answer["reason"]


def _fingerprint(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_stop_record_cli_no_record_clean_tree_passes(repo):
    out = _run(json.dumps({"project_dir": str(repo), "stop": None, "ignore": None,
                           "head": None, "origin_ref": None}))
    assert out.returncode == 0, out.stderr
    assert out.stdout == '{"reason": null}\n'


def test_stop_record_cli_no_record_uncommitted_file_is_refused(repo):
    (repo / "notes.md").write_text("mine\n")
    reason = _check({"project_dir": str(repo), "stop": None, "ignore": None,
                     "head": None, "origin_ref": None})
    assert reason is not None
    assert "uncommitted" in reason
    assert "notes.md" in reason


def test_stop_record_cli_record_matching_the_file_passes(repo):
    (repo / "app.py").write_text("x = 2\n")
    stop = {"head": None, "edits": {"app.py": _fingerprint(repo / "app.py")}}
    assert _check({"project_dir": str(repo), "stop": stop, "ignore": None,
                   "head": None, "origin_ref": None}) is None


def test_stop_record_cli_record_with_another_fingerprint_is_refused(repo):
    (repo / "app.py").write_text("x = 2\n")
    stop = {"head": None, "edits": {"app.py": "sha256:" + "0" * 64}}
    reason = _check({"project_dir": str(repo), "stop": stop, "ignore": None,
                     "head": None, "origin_ref": None})
    assert reason is not None
    assert "did not leave" in reason


def test_stop_record_cli_passes_ignore_and_head_through(repo):
    """`ignore` leaves the caller's own directory out; `head` adds the HEAD
    rule, so a commit added since the recorded head is refused."""
    (repo / "out").mkdir()
    (repo / "out" / "run.json").write_text("{}\n")
    head = _git(repo, "rev-parse", "HEAD")
    assert _check({"project_dir": str(repo), "stop": None, "ignore": "out",
                   "head": head, "origin_ref": None}) is None
    (repo / "lib.py").write_text("y = 1\n")
    _git(repo, "add", "lib.py")
    _git(repo, "commit", "-qm", "later")
    reason = _check({"project_dir": str(repo), "stop": None, "ignore": "out",
                     "head": head, "origin_ref": None})
    assert reason is not None
    assert "commits were added" in reason


def test_stop_record_cli_optional_keys_default_to_null(repo):
    assert _check({"project_dir": str(repo), "stop": None}) is None


@pytest.mark.parametrize("stdin", [
    "not json",
    "",
    "[]",
    '{"stop": null}',
    '{"project_dir": "relative/dir", "stop": null}',
    '{"project_dir": 3, "stop": null}',
    '{"project_dir": "/tmp", "stop": null, "head": 7}',
    '{"project_dir": "/tmp", "stop": null, "typo": 1}',
])
def test_stop_record_cli_malformed_input_exits_2(stdin):
    out = _run(stdin)
    assert out.returncode == 2
    assert out.stdout == ""
    assert out.stderr.strip()


def test_stop_record_cli_invalid_stop_record_exits_2(repo):
    out = _run(json.dumps({"project_dir": str(repo),
                           "stop": {"head": None, "edits": ["app.py"]}}))
    assert out.returncode == 2
    assert out.stdout == ""
    assert "stop" in out.stderr
