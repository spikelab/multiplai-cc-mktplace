"""Stop and resume rows of plan-factory-merged-2026-10-29.md for the R5
levers built in buildme's TDD engine. S47 (item 62, `tdd.spec_tests`): a block
stopped after its tests were committed and before the implementer started
resumes at the implementer, with no new test writer, and ends as a run that
never stopped.

Agents are faked at `agent_call`; git and the files are real.
"""

import asyncio
import os
import shutil
from unittest.mock import patch

from build_pipeline.sdk import AgentResult
from build_pipeline.state import BuildState
from build_pipeline.tdd_engine import run_block_tdd

from tests.row_helpers import repo_snapshot
from tests.test_spec_tests import NEW_TEST, SPEC_TEST
from tests.test_spec_tests_off import RED, _gates, _setup  # noqa: F401


def _agent(config, calls, kill_at=None):
    async def agent(prompt, **kw):
        label = kw.get("budget_label", "")
        if label == kill_at:
            os._exit(137)
        calls.append(label)
        project = config.project_dir
        if label == "spec_test_writer":
            (project / "tests" / "test_roles_spec.py").write_text(SPEC_TEST)
        elif label == "test_writer":
            (project / "tests" / "test_new.py").write_text(NEW_TEST)
        elif label == "implementer":
            (project / "module.py").write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def set_role(who, role):\n    raise PermissionError(who)\n"
            )
        return AgentResult(success=True, output="STATUS: DONE")

    return agent


def _run(config, state, progress, block, calls, kill_at=None):
    with (
        patch(
            "build_pipeline.llm_steps.tdd_steps.agent_call",
            side_effect=_agent(config, calls, kill_at),
        ),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED),
    ):
        return asyncio.run(run_block_tdd(block, config, state, progress))


def _saved_block(config):
    block = BuildState.load(config.state_file_path()).tdd.blocks[0]
    return {
        "status": block.status,
        "red_evidence": block.red_evidence,
        "test_file_hashes": block.test_file_hashes,
        "notes": block.notes,
    }


def test_s47_spec_tests_committed_implementation_not_started_runs_the_implementer(
    tmp_path, monkeypatch
):
    # Both runs use the same directory, so the paths saved in their state
    # match: the run that never stopped is recorded, then removed.
    run = tmp_path / "run"
    run.mkdir()
    config, state, progress, block = _setup(run, monkeypatch)
    config.spec_tests = True
    clean_calls: list = []
    assert _run(config, state, progress, block, clean_calls) is True
    clean_repo, clean_block = repo_snapshot(config.project_dir), _saved_block(config)
    shutil.rmtree(run)
    run.mkdir()

    config, state, progress, block = _setup(run, monkeypatch)
    config.spec_tests = True
    pid = os.fork()
    if pid == 0:
        try:
            _run(config, state, progress, block, [], kill_at="implementer")
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 137, "never reached the implementer"

    # The row's state: the test commit on HEAD, the implementer not started.
    state = BuildState.load(config.state_file_path())
    block = state.tdd.blocks[state.tdd.current_block]
    assert block.test_commit and block.red_evidence
    assert (
        "test(block-1)"
        in os.popen(f"git -C {config.project_dir} log -1 --format=%s").read()
    )

    resumed_calls: list = []
    assert _run(config, state, progress, block, resumed_calls) is True
    assert "spec_test_writer" not in resumed_calls
    assert "test_writer" not in resumed_calls
    assert resumed_calls[0] == "implementer"
    assert clean_calls[: len(clean_calls) - len(resumed_calls)] == [
        "spec_test_writer",
        "test_writer",
    ]
    assert clean_calls[2:] == resumed_calls

    resumed_repo = repo_snapshot(config.project_dir)
    for key in clean_repo:
        assert resumed_repo[key] == clean_repo[key], key
    assert _saved_block(config) == clean_block
