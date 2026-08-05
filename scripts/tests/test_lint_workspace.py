"""Tests for lint_workspace.py.

Written alongside the nested-lockfile check, which exists because two orphaned
`uv.lock` files under `plugins/` shipped cryptography 49.0.0 (CVE-2026-69247,
high) to installed plugins for months. A gate that passes on the current tree
proves nothing, so each check here runs against a fixture tree built to break
it.

The checks read the module-global `REPO_ROOT`, so every test points that at a
tmp_path fixture rather than the real repo.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lint_workspace  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A minimal tree with one plugin member, rooted away from the real repo."""
    root = tmp_path / "repo"
    member = root / "plugins" / "myplug" / "skills" / "thing" / "scripts"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname = "thing"\n')
    (root / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["plugins/myplug/skills/thing/scripts"]\n'
    )
    (root / "uv.lock").write_text("# the one true lock\n")
    monkeypatch.setattr(lint_workspace, "REPO_ROOT", root)
    monkeypatch.setattr(lint_workspace, "ROOT_PYPROJECT", root / "pyproject.toml")
    return root


# --- nested lockfiles -------------------------------------------------------

def test_root_lock_alone_is_clean(repo):
    """The root uv.lock is the point — it must not trip its own gate."""
    assert lint_workspace.check_no_nested_locks() == []


def test_nested_lock_is_caught(repo):
    nested = repo / "plugins/myplug/skills/thing/scripts/uv.lock"
    nested.write_text("# frozen the day the workspace consolidated\n")
    problems = lint_workspace.check_no_nested_locks()
    assert len(problems) == 1
    assert "plugins/myplug/skills/thing/scripts/uv.lock" in problems[0]


def test_every_nested_lock_is_reported(repo):
    """Two orphans is the case that actually happened — not just the first."""
    for p in ("plugins/myplug/skills/thing/scripts", "plugins/myplug"):
        (repo / p).mkdir(parents=True, exist_ok=True)
        (repo / p / "uv.lock").write_text("# stale\n")
    assert len(lint_workspace.check_no_nested_locks()) == 2


def test_lock_inside_a_venv_is_ignored(repo):
    """A .venv is gitignored churn; the stray-venv check owns that failure."""
    venv = repo / "plugins/myplug/.venv/lib"
    venv.mkdir(parents=True)
    (venv / "uv.lock").write_text("# vendored inside an environment\n")
    assert lint_workspace.check_no_nested_locks() == []


def test_message_says_what_to_do(repo):
    """A gate nobody can act on is a gate that gets disabled."""
    (repo / "plugins/myplug/skills/thing/scripts/uv.lock").write_text("x\n")
    (problem,) = lint_workspace.check_no_nested_locks()
    assert "Delete it" in problem
    assert "root" in problem


# --- the check is wired in --------------------------------------------------

def test_nested_lock_check_is_registered():
    """Registration is the difference between a gate and a dead function."""
    assert any(label == "nested lockfiles" for label, _ in lint_workspace.CHECKS)
