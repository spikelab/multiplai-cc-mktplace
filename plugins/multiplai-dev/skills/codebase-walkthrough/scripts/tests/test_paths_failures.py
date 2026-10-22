"""Trace failures: one entry's failure is a drop, a stage failure is recorded, trust and budget stops halt."""

from __future__ import annotations

import asyncio
import json

import code_facts
import path_fixtures
import pytest
from fakes import FakeAgents
from test_paths_render import _render, _result
from test_paths_render import _state as _render_state
from test_paths_stage import (
    _answer_files,
    _click_run,
    _paths_body,
    _state,
    _trace_answer_files,
    _trace_calls,
)
from walkthrough_pipeline import budget, sdk
from walkthrough_pipeline.__main__ import TRUST_MESSAGE, main
from walkthrough_pipeline.render import section_texts
from walkthrough_pipeline.state import STATE_FILE

THREE = ["sync", "push", "pull"]


def _coverage(md: str) -> str:
    return section_texts(md, "Coverage")[0]


def test_an_agent_failure_on_one_entry_drops_only_that_entry(
    tmp_path, monkeypatch, spy
):
    agents = FakeAgents(
        trace_plan=path_fixtures.click_plan(THREE), fail_on="- name: push\n"
    )
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, agents, names=THREE)
    assert code == 0
    result = spy.results[0]
    assert sorted(p.entry.name for p in result.paths) == ["pull", "sync"]
    assert [(d.entry.name, d.stage, d.reason) for d in result.dropped] == [
        ("push", "trace", "call_failed")
    ]
    md = md_path.read_text()
    assert "2 paths reach this module. 1 entry was left out" in _paths_body(md)
    assert "(`push`) | trace | `call_failed` |" in _coverage(md)
    assert _state(run_dir)["stage"] == "done"


def test_a_whole_stage_failure_is_recorded_and_the_walkthrough_is_still_written(
    tmp_path, monkeypatch
):
    async def boom(**kw):
        raise RuntimeError("the index broke")

    monkeypatch.setattr(code_facts, "map_paths", boom)
    code, run_dir, md_path = _click_run(
        tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert code == 0
    state = _state(run_dir)
    assert "trace: RuntimeError: the index broke" in state["errors"]
    assert (
        state["paths"] is None
        and state["paths_error"] == "RuntimeError: the index broke"
    )
    md = md_path.read_text()
    assert (
        _paths_body(md).strip() == "Path finding failed: RuntimeError: the index broke"
    )
    assert "trace: RuntimeError: the index broke" in _coverage(md)
    log = (run_dir / "progress.log").read_text()
    assert "trace: failed (RuntimeError)" in log and "the index broke" not in log


def test_the_failure_lead_sentence(tmp_path):
    state = _render_state(tmp_path, None).model_copy(
        update={"paths_error": "ValueError: bad seed"}
    )
    assert (
        _render(state, tmp_path / "out").strip()
        == "Path finding failed: ValueError: bad seed"
    )
    # A result and an error never both render: the error wins, because the result is not the run's.
    both = _render_state(tmp_path / "b", _result()).model_copy(
        update={"paths_error": "ValueError: x"}
    )
    assert (
        _render(both, tmp_path / "out").strip() == "Path finding failed: ValueError: x"
    )


def test_a_budget_stop_on_entry_2_halts_the_run_and_keeps_the_paid_answer(
    tmp_path, monkeypatch, spy, capsys
):
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(THREE), stop_trace_after=1)
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, agents, names=THREE)
    assert code == 2
    assert "STOPPED: circuit breaker" in capsys.readouterr().err
    state = _state(run_dir)
    assert state["stage"] == "docs" and not state["write_answers"]  # no later stage ran
    assert not md_path.exists()
    assert len(_trace_answer_files(run_dir)) == 1
    assert len([c for c in _trace_calls(agents) if c["schema"] == "TraceAnswer"]) == 2


def test_a_trust_stop_from_the_resolver_halts_the_run(
    tmp_path, monkeypatch, spy, capsys
):
    from walkthrough_pipeline.stages import trace

    def untrusted(state):
        def resolve(path, side="head"):
            raise sdk.RepoTrustError("not trusted")

        return resolve

    monkeypatch.setattr(trace, "_resolver", untrusted)
    code, run_dir, md_path = _click_run(
        tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert code == 1
    assert TRUST_MESSAGE in capsys.readouterr().err
    assert _state(run_dir)["stage"] == "docs" and not md_path.exists()
    assert _answer_files(run_dir)  # the answer paid for before the stop is kept


def test_a_trust_stop_inside_a_call_is_not_turned_into_a_drop(
    tmp_path, monkeypatch, spy, capsys
):
    class Untrusted(FakeAgents):
        def trace(self, prompt, cwd):
            raise sdk.RepoTrustError("not trusted")

    code, run_dir, _ = _click_run(
        tmp_path, monkeypatch, Untrusted(trace_plan=path_fixtures.click_plan())
    )
    assert code == 1
    assert TRUST_MESSAGE in capsys.readouterr().err
    assert _state(run_dir)["stage"] == "docs"


def test_the_latch_stops_every_call_after_a_fatal_one(monkeypatch, tmp_path):
    from walkthrough_pipeline.config import WalkConfig
    from walkthrough_pipeline.stages import RunContext
    from walkthrough_pipeline.stages.trace import _FatalLatch, _trace_call

    calls = []

    async def fake(prompt, schema, **kw):
        calls.append(prompt)
        raise budget.BudgetExceededError("circuit breaker: test", cost_usd=1.0)

    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    ctx = RunContext(
        config=WalkConfig(), run_dir=tmp_path, snap_root=tmp_path, repos={}
    )
    latch = _FatalLatch()
    call = _trace_call(ctx, tmp_path, latch)
    with pytest.raises(budget.BudgetExceededError):
        asyncio.run(call("one", dict))
    assert isinstance(latch.exc, budget.BudgetExceededError)
    with pytest.raises(budget.BudgetExceededError):
        asyncio.run(call("two", dict))
    assert calls == ["one"]  # the second call never reached the model


def test_an_agent_call_error_is_not_latched(monkeypatch, tmp_path):
    from walkthrough_pipeline.config import WalkConfig
    from walkthrough_pipeline.stages import RunContext
    from walkthrough_pipeline.stages.trace import _FatalLatch, _trace_call

    async def fake(prompt, schema, **kw):
        raise sdk.AgentCallError("no valid answer")

    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    ctx = RunContext(
        config=WalkConfig(), run_dir=tmp_path, snap_root=tmp_path, repos={}
    )
    latch = _FatalLatch()
    with pytest.raises(sdk.AgentCallError):
        asyncio.run(_trace_call(ctx, tmp_path, latch)("one", dict))
    assert latch.exc is None


@pytest.mark.parametrize("stage", ["trace", "write", "done"])
def test_a_state_written_before_the_change_and_past_trace_traces_again_on_resume(
    tmp_path, monkeypatch, spy, stage
):
    code, run_dir, md_path = _click_run(
        tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert code == 0
    old = _state(run_dir)
    old.pop("paths")
    old.pop("paths_error", None)
    old.update(
        stage=stage,
        scenarios=[{"seed": {"path": "shop/cli/main.py"}, "hops": []}],
        trace_answers={},
    )
    (run_dir / STATE_FILE).write_text(json.dumps(old))
    md_path.unlink()

    monkeypatch.setattr(
        sdk, "agent_call_structured", FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert len(spy.calls) == 2
    assert "1 path reaches this module." in _paths_body(md_path.read_text())
    assert _state(run_dir)["paths"] is not None


def test_rewind_leaves_new_and_agent_mode_states_alone(tmp_path):
    from walkthrough_pipeline.stages.trace import rewind_legacy_trace

    traced = _render_state(tmp_path, _result()).model_copy(update={"stage": "write"})
    assert rewind_legacy_trace(traced).stage == "write"
    failed = traced.model_copy(update={"paths": None, "paths_error": "RuntimeError: x"})
    assert rewind_legacy_trace(failed).stage == "write"
    agent = traced.model_copy(update={"paths": None, "options": {"mode": "agent"}})
    assert rewind_legacy_trace(agent).stage == "write"
    legacy = traced.model_copy(update={"paths": None})
    assert rewind_legacy_trace(legacy).stage == "docs"
    early = traced.model_copy(update={"paths": None, "stage": "explore"})
    assert rewind_legacy_trace(early).stage == "explore"
