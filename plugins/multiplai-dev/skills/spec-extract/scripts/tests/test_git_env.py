"""`conftest._no_git_auto_maintenance` reaches both kinds of git call a test makes:
the test's own (`conftest.git`) and the pipeline's `--dir` import commit
(`target._import_env`). R27 compares two snapshots of `source/.git/` and depends
on it. The git here may never start auto-maintenance, so a passing R27 does not
show the setting arrived; these do."""

from __future__ import annotations

import subprocess

from conftest import FIXED, git, make_repo
from spec_extract_pipeline import target as target_mod


def test_test_git_calls_have_auto_maintenance_off(tmp_path):
    repo = make_repo(tmp_path / "r")
    assert git(repo, "config", "maintenance.auto") == "false"
    assert git(repo, "config", "gc.auto") == "0"


def test_import_git_calls_have_auto_maintenance_off(tmp_path):
    repo = make_repo(tmp_path / "r")
    env = target_mod._import_env(FIXED)
    for key, want in (("maintenance.auto", "false"), ("gc.auto", "0")):
        proc = subprocess.run(
            ["git", "-C", str(repo), "config", key],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert proc.stdout.strip() == want
