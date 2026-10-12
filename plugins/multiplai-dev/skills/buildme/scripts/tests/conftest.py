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


@pytest.fixture(autouse=True)
def no_real_walkthrough_run(monkeypatch):
    """After a build, a stale walkthrough may be regenerated with a paid
    `walkthrough_pipeline run`. No test may start one: a test that regenerates
    stubs `git_ops._run_walkthrough_skill` itself. The model-free `check` is
    allowed through."""
    real = git_ops._run_walkthrough_skill

    def guarded(argv, cwd=None, timeout=None):
        if "walkthrough_pipeline" in argv and argv[argv.index("walkthrough_pipeline") + 1] != "check":
            raise AssertionError(f"a test ran a real walkthrough without stubbing it: {argv}")
        return real(argv, cwd=cwd) if timeout is None else real(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(git_ops, "_run_walkthrough_skill", guarded)
