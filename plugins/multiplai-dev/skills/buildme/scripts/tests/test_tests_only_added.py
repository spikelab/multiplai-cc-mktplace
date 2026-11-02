"""A test file that only gained lines since it gated the block passes the
test-integrity gate flagged, with a claim for the reviewer (DFACCMGMT-1
account-user-api block 3: a review-fix appended 80 test lines, declared
nothing, and failed the block twice)."""

from __future__ import annotations

import hashlib
import subprocess
from unittest.mock import patch

from build_pipeline.config import BuildConfig
from build_pipeline.gates import unchanged_tests_gate
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BlockInfo, BuildState, TDDState
from build_pipeline.tdd_engine import _enforce_test_integrity, _only_added_lines

ORIGINAL = "def test_a():\n    assert f() == 1\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _repo(tmp_path, name="tests/test_a.py", text=ORIGINAL):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "tests")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    block = BlockInfo(number=1, name="B", description="d")
    block.test_commit = sha
    block.test_file_hashes = {name: _sha(text)}
    state = BuildState(change_name="c", tier="standard", tdd=TDDState(blocks=[block]))
    return config, block, state


def _gate(config, block, state, name, report=""):
    after = {name: _sha((config.project_dir / name).read_text())}
    with patch("build_pipeline.tdd_engine._snapshot_test_files", return_value=after):
        return _enforce_test_integrity(
            block,
            config,
            state,
            ProgressWriter(config.progress_file_path()),
            0,
            "review-fix 1",
            report,
        )


def test_appended_tests_pass_flagged_with_a_claim(tmp_path):
    name = "tests/test_a.py"
    config, block, state = _repo(tmp_path)
    (tmp_path / name).write_text(ORIGINAL + "\n\ndef test_b():\n    assert f() != 2\n")
    assert _gate(config, block, state, name) is True
    assert block.test_change_claims == [
        f"added tests without a declaration in {name}; no existing line changed"
    ]
    # Re-baselined, as a declared change is: the next window measures from here.
    assert block.test_file_hashes[name] == _sha((tmp_path / name).read_text())


def test_a_changed_line_still_fails(tmp_path):
    name = "tests/test_a.py"
    config, block, state = _repo(tmp_path)
    (tmp_path / name).write_text(
        ORIGINAL.replace("== 1", "== 2") + "\ndef test_b():\n    pass\n"
    )
    assert _gate(config, block, state, name) is False
    assert block.test_change_claims == []


def test_a_removed_line_still_fails(tmp_path):
    name = "tests/test_a.py"
    config, block, state = _repo(tmp_path)
    (tmp_path / name).write_text("def test_a():\n    pass\n")
    assert _gate(config, block, state, name) is False


def test_lines_added_to_a_conftest_still_fail(tmp_path):
    name = "tests/conftest.py"
    config, block, state = _repo(tmp_path, name, "import pytest\n")
    (tmp_path / name).write_text(
        "import pytest\n\n@pytest.fixture(autouse=True)\ndef x(monkeypatch):\n    pass\n"
    )
    assert _gate(config, block, state, name) is False


def test_a_rebaselined_snapshot_is_not_compared_with_the_test_commit(tmp_path):
    """After a declared change the snapshot is no longer the test commit's
    content, so the commit cannot show what the gate's baseline was."""
    name = "tests/test_a.py"
    config, block, state = _repo(tmp_path)
    declared = ORIGINAL.replace("== 1", "== 3")
    block.test_file_hashes = {name: _sha(declared)}
    (tmp_path / name).write_text(declared + "\ndef test_b():\n    pass\n")
    assert _only_added_lines(config, block, name) is False
    assert _gate(config, block, state, name) is False


def test_a_block_with_no_test_commit_is_not_checked_for_additions(tmp_path):
    name = "tests/test_a.py"
    config, block, state = _repo(tmp_path)
    block.test_commit = ""
    (tmp_path / name).write_text(ORIGINAL + "\ndef test_b():\n    pass\n")
    assert _gate(config, block, state, name) is False


def test_a_deleted_file_fails_even_when_the_others_only_gained_lines():
    gate = unchanged_tests_gate(
        {"a.py": "1", "b.py": "2"}, {"a.py": "3"}, "", additions_only=lambda p: True
    )
    assert gate.passed is False


def test_a_declared_change_keeps_its_own_claim():
    gate = unchanged_tests_gate(
        {"a.py": "1"},
        {"a.py": "2"},
        "TEST CHANGE REQUIRED: the spec says 409",
        additions_only=lambda p: True,
    )
    assert gate.passed is True
    assert gate.metadata["claims"] == ["the spec says 409"]
