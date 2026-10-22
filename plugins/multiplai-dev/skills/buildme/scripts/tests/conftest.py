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


@pytest.fixture(autouse=True)
def no_real_spec_extract(monkeypatch):
    """CODEBASE_ANALYSIS may run spec-extract, a paid `spec_extract_pipeline run`.
    No test may start one: a test that reaches the step stubs
    `git_ops._run_spec_extract_skill` or `orchestrator._run_spec_extract_step`."""
    real = git_ops._run_spec_extract_skill

    def guarded(argv, cwd=None, timeout=None):
        if "spec_extract_pipeline" in argv:
            raise AssertionError(f"a test ran a real spec-extract without stubbing it: {argv}")
        return real(argv, cwd=cwd) if timeout is None else real(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(git_ops, "_run_spec_extract_skill", guarded)


@pytest.fixture(autouse=True)
def no_inherited_build_env(monkeypatch):
    """`build --trust-repo` sets BUILDME_TRUST_REPO=1 for every subprocess, and an
    epic child build runs with BUILDME_EPIC_CHILD=1, the test command of a build of
    this repo included. A test that wants either sets it itself; none inherits it,
    so every test runs as in CI. WORKSPACE is unset too, as in CI: a build
    run without it puts its worktree beside the test's temp repo, never in
    the workspace of whoever runs the suite."""
    monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
    monkeypatch.delenv("BUILDME_EPIC_CHILD", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """Code under test commits through `git_ops` with the process environment, and
    a CI runner has no git identity, so such a commit fails with "Author identity
    unknown". Every test gets one here, so none depends on the machine's. A test
    that wants another identity sets it itself."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "buildme test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "buildme-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "buildme test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "buildme-test@example.invalid")


# multiplai-core 0.21: a step with no configured model or effort runs on
# MULTIPLAI_MODEL / MULTIPLAI_EFFORT, else the Claude Code session's own
# (multiplai_core.session). Run inside a session, the developer's values would
# set a test's model, so clear them and hide the developer's multiplai.conf
# from that lookup. A test that wants one sets it with monkeypatch.
_SESSION_DEFAULT_VARS = (
    "MULTIPLAI_MODEL",
    "MULTIPLAI_EFFORT",
    "MULTIPLAI_SESSION_MODEL",
    "MULTIPLAI_SESSION_EFFORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
)


@pytest.fixture(autouse=True)
def _no_session_defaults(monkeypatch):
    for key in _SESSION_DEFAULT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("multiplai_core.session.load_multiplai_conf",
                        lambda: {"_sections": {}})


@pytest.fixture(autouse=True)
def no_real_model_provider(monkeypatch):
    """Correctness tests must never dispatch paid model calls."""
    async def refuse(*args, **kwargs):
        raise AssertionError("Test attempted an unstubbed model provider call")
    monkeypatch.setattr("build_pipeline.sdk.run_agent", refuse)


@pytest.fixture(autouse=True)
def isolated_budget():
    from build_pipeline import budget
    budget.reset()
    yield
    budget.reset()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_handoff: the build sees the change dir as the test left it; "
                   "pm_spec_handoff does not mark its specs ready")


@pytest.fixture(autouse=True)
def pm_spec_handoff(monkeypatch, request):
    """Play pm-spec's handoff for tests of what buildme does with ready specs:
    when a build starts (or resumes) on a change pm-spec never marked (no
    `spec_status`), write any missing specs, mark them ready and commit
    them, then run buildme's own readiness check unchanged. A change that
    was marked and then edited is left as it is, so the check still refuses
    it. Tests of the refusal itself use `@pytest.mark.no_handoff`."""
    if request.node.get_closest_marker("no_handoff"):
        return
    from build_pipeline import orchestrator

    from tests.spec_fixtures import never_marked, ready_specs

    real = orchestrator._refuse_unless_ready

    def handoff(config, state_path):
        if config.change_name and never_marked(config.change_dir):
            ready_specs(config.change_dir, config.project_dir)
        return real(config, state_path)

    monkeypatch.setattr(orchestrator, "_refuse_unless_ready", handoff)
