"""Shared test guards."""

import pytest

from build_pipeline import git_ops


@pytest.fixture(autouse=True)
def no_real_pr_skill(monkeypatch):
    """PUBLISH runs the pr skill as a subprocess, which calls `gh pr create`.
    No test may reach the real one: a test that publishes stubs
    `git_ops._run_pr_skill` itself."""

    def refuse(argv, cwd=None, timeout=None):
        raise AssertionError(f"a test ran the real pr skill without stubbing it: {argv}")

    monkeypatch.setattr(git_ops, "_run_pr_skill", refuse)
