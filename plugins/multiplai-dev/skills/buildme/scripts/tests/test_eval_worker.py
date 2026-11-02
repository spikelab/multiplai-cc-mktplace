"""Offline checks of the actual evaluation worker and production bootstrap."""

import argparse
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from build_pipeline import git_ops, orchestrator, sdk
from multiplai_core.agent_runner import AgentRunError

MODULE = Path(__file__).parents[1] / "evals/pipeline_compare.py"


@pytest.mark.asyncio
async def test_worker_reaches_sdk_adapter_after_clean_bootstrap_and_preserves_source_staging(
    tmp_path, monkeypatch, capsys
):
    spec = importlib.util.spec_from_file_location("pipeline_compare", MODULE)
    pilot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pilot)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    checkout = tmp_path / "source"
    task = {
        "id": "offline",
        "files": {"app.py": "value = 0\n"},
        "instruction": "Return one from value",
        "test_command": "true",
    }
    pilot.initialize(checkout, task)
    public = tmp_path / "public.json"
    public.write_text(json.dumps({k: v for k, v in task.items() if k != "files"}))
    provider = AsyncMock(
        side_effect=AgentRunError("offline SDK adapter", reason="offline sentinel")
    )
    monkeypatch.setattr(sdk, "run_agent", provider)
    # Worker instrumentation is process-local in CLI use; restore it in this test.
    monkeypatch.setattr(git_ops, "push_branch", git_ops.push_branch)
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [])
    args = argparse.Namespace(
        worker=public,
        checkout=checkout,
        variant="current",
        model="offline-test",
        per_run_usd=1,
    )
    assert await pilot.worker(args) == 1
    assert provider.await_count >= 1
    assert pilot.preserved_user_work(checkout)
    output = capsys.readouterr().out
    metrics = json.loads(
        next(line[5:] for line in output.splitlines() if line.startswith("EVAL:"))
    )
    assert Path(metrics["project"]) != checkout
    assert not metrics["verified"]
    assert metrics["user_staging_seeded"]


@pytest.mark.asyncio
async def test_candidate_worker_patches_only_names_that_exist(tmp_path, monkeypatch):
    """A stub assigned to a name the pipeline no longer has replaces nothing and runs nothing."""
    from build_pipeline import spec_generator, tdd_engine
    from build_pipeline.llm_steps import plan_review_steps

    spec = importlib.util.spec_from_file_location("pipeline_compare", MODULE)
    pilot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pilot)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    checkout = tmp_path / "source"
    task = {
        "id": "offline",
        "files": {"app.py": "value = 0\n"},
        "instruction": "Return one from value",
        "test_command": "true",
    }
    pilot.initialize(checkout, task)
    public = tmp_path / "public.json"
    public.write_text(json.dumps({k: v for k, v in task.items() if k != "files"}))
    monkeypatch.setattr(
        sdk,
        "run_agent",
        AsyncMock(side_effect=AgentRunError("offline SDK adapter", reason="offline")),
    )
    # Worker instrumentation is process-local in CLI use; restore it in this test.
    monkeypatch.setattr(git_ops, "push_branch", git_ops.push_branch)
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [])
    monkeypatch.setattr(tdd_engine, "_run_refactor_all", tdd_engine._run_refactor_all)
    monkeypatch.setattr(
        plan_review_steps,
        "run_plan_review_stage",
        plan_review_steps.run_plan_review_stage,
    )
    modules = (spec_generator, tdd_engine, plan_review_steps)
    before = [set(vars(m)) for m in modules]
    args = argparse.Namespace(
        worker=public,
        checkout=checkout,
        variant="candidate",
        model="offline-test",
        per_run_usd=1,
    )
    await pilot.worker(args)
    assert [set(vars(m)) - names for m, names in zip(modules, before)] == [
        set(),
        set(),
        set(),
    ]
