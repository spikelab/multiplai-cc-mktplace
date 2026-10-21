"""A budget stop exits 2 with a checkpoint that keeps paid answers; resume finishes without re-asking them."""

from __future__ import annotations

import json

from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE
from test_pipeline import run_args


def test_resume_after_a_budget_stop(ws, tmp_path, monkeypatch, fake_fetch, capsys):
    agents = FakeAgents(cost=1.0)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, "--max-usd", "1.5")) == 2
    assert "STOPPED: circuit breaker" in capsys.readouterr().err
    run_dir = next((tmp_path / "runs").iterdir())
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "partition"                 # explore did not finish
    paid = dict(state["explore_answers"])
    assert paid, "answers paid for before the stop are in the checkpoint"
    assert state["budget"]["cost_usd"] >= 1.5
    assert "FAILED circuit breaker" in (run_dir / "progress.log").read_text()

    paid_prompts = {c["prompt"] for c in agents.calls if c["schema"] == "ExploreOutput"}
    asked_before = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]) == 0
    resumed = {c["prompt"] for c in agents.calls[asked_before:] if c["schema"] == "ExploreOutput"}
    assert not paid_prompts & resumed, "resume re-asked an explore task it had already paid for"
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "done"
    assert (tmp_path / "out" / "bookings-walkthrough.md").is_file()


def test_resume_after_a_docs_gate_stop_takes_a_new_decision(ws, tmp_path, monkeypatch, capsys):
    from walkthrough_pipeline import fetcher

    async def no_llms(url, client, **kw):
        return fetcher.Fetched(url=url, status=404, error="HTTP 404")
    monkeypatch.setattr(fetcher, "fetch_url", no_llms)
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 1
    err = capsys.readouterr().err
    assert "STOP (docs gate)" in err and "--no-docs" in err
    run_dir = next((tmp_path / "runs").iterdir())
    explored = len([c for c in agents.calls if c["schema"] == "ExploreOutput"])
    assert explored

    assert main(["resume", str(run_dir), "--trust-repo"]) == 1         # same decision, same stop
    capsys.readouterr()
    assert main(["resume", str(run_dir), "--trust-repo", "--no-docs"]) == 0
    assert len([c for c in agents.calls if c["schema"] == "ExploreOutput"]) == explored   # explore not re-asked
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "done" and not state["options"]["docs"]


def test_a_run_without_rg_stops_before_any_agent_call(ws, tmp_path, monkeypatch, fake_fetch, capsys):
    import shutil
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k))
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 1
    assert "ripgrep" in capsys.readouterr().err
    assert not agents.calls


def test_resume_after_a_stop_between_the_markdown_and_the_html(done_run, monkeypatch, capsys):
    """S1: a run stopped in its render stage after writing the Markdown, before the HTML (the saved stage
    is still `write`): resume writes all four files, the same as a run that never stopped, and asks no agent."""
    from test_render_command import OUTPUTS, assert_same_outputs

    md_path, run_dir, _ = done_run
    out = md_path.parent
    original = {n: (out / n).read_bytes() for n in OUTPUTS}
    state = json.loads((run_dir / STATE_FILE).read_text())
    state["stage"] = "write"
    (run_dir / STATE_FILE).write_text(json.dumps(state))
    for n in OUTPUTS[1:]:
        (out / n).unlink()

    def no_agent(*args, **kwargs):
        raise AssertionError("resume asked an agent after the write stage")
    monkeypatch.setattr(sdk, "agent_call_structured", no_agent)
    capsys.readouterr()
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("walkthrough: ")] == \
        [f"walkthrough: {out / n}" for n in OUTPUTS]
    assert_same_outputs(out, original)
    assert json.loads((run_dir / STATE_FILE).read_text())["stage"] == "done"
