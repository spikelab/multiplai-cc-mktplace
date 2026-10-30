"""Stop and resume rows of plan-factory-merged-2026-10-29 for R5's TDD levers.

S46: `tdd.context` changed between a stop and the resume.
"""

import asyncio
import subprocess
from unittest.mock import patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.sdk import AgentResult
from build_pipeline.spec_generator import _plan_hashes
from build_pipeline.tdd_engine import assemble_context, run_block_tdd

from tests.test_tdd_context_full import NEW_TEST, RED, _gates, setup_change  # noqa: F401


class _Stopped(Exception):
    """The process stopping while the implementer runs."""


def _reload(config: BuildConfig, yaml_text: str) -> None:
    """The user edits config.yaml before resuming; buildme reads it again.
    `setup_change` set the project description by hand, so it is kept."""
    description = config.project_description
    (config.specs_dir / "config.yaml").write_text(yaml_text)
    config._load_specs_config()
    config.project_description = description


def _run(config, state, progress, block, *, stop_at_implementer=False):
    prompts: list[tuple[str, str]] = []

    async def agent(prompt, **kw):
        label = kw.get("budget_label", "")
        prompts.append((label, prompt))
        if label == "test_writer":
            (config.project_dir / "tests" / "test_new.py").write_text(NEW_TEST)
        elif label == "implementer":
            if stop_at_implementer:
                raise _Stopped
            (config.project_dir / "module.py").write_text(
                "def add(a, b):\n    return a + b\n"
            )
        return AgentResult(success=True, output="STATUS: DONE")

    with (
        patch("build_pipeline.llm_steps.tdd_steps.agent_call", side_effect=agent),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED),
    ):
        try:
            ok = asyncio.run(run_block_tdd(block, config, state, progress))
        except _Stopped:
            ok = None
    return ok, prompts


def _log(config) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s", "main"],
        cwd=config.project_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


@pytest.mark.parametrize(
    ("before", "after"),
    [("", "tdd:\n  context: sliced\n"), ("tdd:\n  context: sliced\n", "")],
    ids=["full-to-sliced", "sliced-to-full"],
)
def test_s46_resumed_block_uses_the_current_tdd_context(
    tmp_path, monkeypatch, before, after
):
    # The run that never stopped, with the setting the resume has.
    clean = tmp_path / "clean"
    clean.mkdir()
    c_config, c_state, c_progress, c_block = setup_change(clean, monkeypatch, after)
    c_ok, c_prompts = _run(c_config, c_state, c_progress, c_block)

    stopped = tmp_path / "stopped"
    stopped.mkdir()
    config, state, progress, block = setup_change(stopped, monkeypatch, before)
    hashes = _plan_hashes(config)
    previous = {
        role: assemble_context(block, config, role, blocks=state.tdd.blocks)
        for role in ("implementer", "refactorer")
    }
    ok, _ = _run(config, state, progress, block, stop_at_implementer=True)
    assert ok is None
    assert _log(config) == ["test(block-1): Add", "init"]

    _reload(config, after)
    # The plan is not marked stale: no planning input changed.
    assert _plan_hashes(config) == hashes

    current = {
        role: assemble_context(block, config, role, blocks=state.tdd.blocks)
        for role in ("implementer", "refactorer")
    }
    ok, prompts = _run(config, state, progress, block)
    assert ok is c_ok is True
    # The resume goes straight to the implementer (today's resume of a block
    # whose tests are committed and RED), then the refactorer, as the clean run.
    assert [label for label, _ in prompts] == ["implementer", "refactor"]
    assert [label for label, _ in c_prompts] == [
        "test_writer",
        "implementer",
        "refactor",
    ]
    # Each prompt carries the bundle the current setting builds, the one the
    # run that never stopped got, and not the one from before the stop.
    resumed = dict(prompts)
    clean_prompts = dict(c_prompts)
    for role, label in (("implementer", "implementer"), ("refactorer", "refactor")):
        assert current[role] in resumed[label]
        assert current[role].replace(str(stopped), str(clean)) in clean_prompts[label]
        assert previous[role] not in resumed[label]
    # The refactor prompt does not depend on the stop: it equals the clean run's.
    assert resumed["refactor"].replace(str(stopped), "<TMP>") == clean_prompts[
        "refactor"
    ].replace(str(clean), "<TMP>")
    assert _log(config) == _log(c_config)
