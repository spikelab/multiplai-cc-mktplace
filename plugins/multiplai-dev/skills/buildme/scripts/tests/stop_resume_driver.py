"""One `build` run for tests/test_stop_resume_logging.py, in its own process.

Run as ``python stop_resume_driver.py <spec.json>``. The run is the real
orchestrator with the model-calling phases replaced by fakes that record a
fixed spend, so a stop can be a real SIGKILL (nothing of the process runs
after it) or a real SIGTERM (the handlers `build` installs turn it into
SystemExit). The spec:

- ``project_dir``, ``change``: the build's ``--project-dir`` and ``--change``.
- ``spec_only``, ``force``: the CLI flags of the same name.
- ``stop_at``: where to stop — ``tdd-start`` (after REVIEW, before any block)
  or ``block-<n>-mid`` (block n started and its spend recorded).
- ``stop_how``: ``kill`` (SIGKILL), ``SIGTERM``, or ``hold`` (write
  ``<hold_dir>/held``, wait for ``<hold_dir>/release``, then SIGKILL).
- ``checkpoint_mid``: save the checkpoint after a block's spend, as the real
  engine does after the block's ``test(block-N)`` commit.
- ``tag_pid``: write this process's pid into the progress file per block.
- ``pr_log``: append each argv the pr skill is run with to this file, one
  JSON list per line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Every block costs this; the spec phases cost SPEC_USD and AUDIT_USD.
BLOCK_USD, SPEC_USD, AUDIT_USD = 2.0, 1.0, 0.5
BLOCKS = 2


def _usage(usd: float) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=int(usd * 1000),
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=usd,
    )


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())

    from build_pipeline import sdk

    def no_model_call(*a, **k):
        raise AssertionError("stop_resume_driver: a phase made a real model call")

    # Before anything imports these names from sdk.
    for name in (
        "llm_call",
        "agent_call",
        "llm_call_structured",
        "agent_call_structured",
    ):
        setattr(sdk, name, no_model_call)

    from build_pipeline import budget, git_ops
    from build_pipeline.config import BuildConfig
    from build_pipeline.models import BlockInfo, BlockStatus, PlanReviewResult
    from build_pipeline.orchestrator import run_orchestrator
    from build_pipeline.progress import ProgressWriter
    from build_pipeline.state import BuildState, TDDState

    def stop_point(name: str) -> None:
        if spec.get("stop_at") != name:
            return
        how = spec.get("stop_how", "kill")
        if how == "hold":
            hold = Path(spec["hold_dir"])
            (hold / "held").write_text(str(os.getpid()))
            while not (hold / "release").exists():
                time.sleep(0.05)
            how = "kill"
        os.kill(os.getpid(), signal.SIGKILL if how == "kill" else signal.SIGTERM)
        time.sleep(30)  # the handler raises here; a kill never returns

    async def fake_spec_generator(config, args, **kwargs):
        from tests.planning_fakes import _plan_fixture

        state = BuildState.load(config.state_file_path())
        _plan_fixture(config, state)
        budget.record(_usage(SPEC_USD), label="spec_generation")
        return 0

    async def fake_plan_check(*a, **k):
        budget.record(_usage(AUDIT_USD), label="plan_review")
        return PlanReviewResult(findings=[])

    async def fake_tdd(config, args, standalone=True):
        """The engine's progress, checkpoint and spend, without its agents."""
        state_path = config.state_file_path()
        state = BuildState.load(state_path)
        budget.get_budget().load_state(state.budget)
        progress = ProgressWriter(config.progress_file_path())
        if state.tdd is None:
            state.tdd = TDDState(
                blocks=[
                    BlockInfo(number=n, name=f"block-{n}", description="d")
                    for n in range(1, BLOCKS + 1)
                ]
            )
            state.checkpoint(state_path)
        progress.log_phase("TDD_BUILD", f"Blocks: {BLOCKS}")
        stop_point("tdd-start")
        for idx, block in enumerate(state.tdd.blocks):
            if block.status == BlockStatus.DONE:
                continue
            state.mark_block_status(idx, BlockStatus.IMPLEMENTING, state_path)
            progress.log_block(block.number, BLOCKS, block.name, "started")
            if spec.get("tag_pid"):
                progress.log_agent("pid", block.name, str(os.getpid()))
            budget.record(_usage(BLOCK_USD), label="implementer")
            if spec.get("checkpoint_mid"):
                state.checkpoint(state_path)
            stop_point(f"block-{block.number}-mid")
            progress.log_block(block.number, BLOCKS, block.name, "done")
            state.mark_block_status(idx, BlockStatus.DONE, state_path)
        return 0

    pr = f"https://github.com/acme/proj/pull/{spec.get('pr_number', 7)}"

    def pr_skill(argv, cwd=None, timeout=None):
        if spec.get("pr_log"):
            with Path(spec["pr_log"]).open("a") as f:
                f.write(json.dumps(list(argv)) + "\n")
        return git_ops.GitResult(argv, 0, f"PR:{pr}\n", "")

    git_ops._run_pr_skill = pr_skill
    # The test's origin is a local bare repo, not GitHub (conftest's
    # `gh_can_open_prs` does the same in-process).
    from build_pipeline import preflight

    preflight._gh_permission = lambda repo: ("WRITE", "")

    project = Path(spec["project_dir"])
    config = BuildConfig(
        project_dir=project,
        change_name=spec["change"],
        auto=True,
        spec_only=spec.get("spec_only", False),
        skip_research=True,
    )
    config.specs_dir = project / "specs"
    config.test_command = "true"
    os.environ["BUILDME_TRUST_REPO"] = "1"
    args = argparse.Namespace(
        change=spec["change"],
        project_dir=str(project),
        auto=True,
        spec_only=config.spec_only,
        skip_research=True,
        research_path="",
        session_id="",
        block=None,
        force=spec.get("force", False),
    )
    with (
        patch("build_pipeline.spec_generator.run_spec_generator", fake_spec_generator),
        patch(
            "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
            fake_plan_check,
        ),
        patch("build_pipeline.tdd_engine.run_tdd_engine", fake_tdd),
        patch(
            "build_pipeline.llm_steps.respec_steps.run_respec_audit",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "build_pipeline.orchestrator._run_docs_update_phase",
            new_callable=AsyncMock,
            return_value=None,
        ),
        git_ops._exit_on_termination(interrupt=False),
    ):
        return asyncio.run(run_orchestrator(config, args))


if __name__ == "__main__":
    sys.exit(main())
