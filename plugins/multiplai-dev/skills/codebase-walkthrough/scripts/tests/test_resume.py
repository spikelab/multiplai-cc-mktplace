"""A budget stop exits 2 with a checkpoint that keeps paid answers; resume finishes without re-asking them.

`resume --rewrite`, rows of plan-walkthrough-explainer-2026-10-21 (S3 is PR #411's
`test_resume_after_a_stop_between_the_markdown_and_the_html` below):

- S1: a stop after the reset, before the first write call, leaves a run a plain `resume` finishes.
- S2: a budget stop mid write keeps the answers paid for; `resume` asks only the rest.
- S4: a run not past its trace stage is refused, exit 1, and nothing is written.
- S5: outputs the user edited are overwritten by the rewrite; they are generated, as `resume` does today.
- S6: when the repo's HEAD has moved, the rewrite still cites the saved commits.
- S7: a rewrite stopped twice is resumed as S1 or S2, as `resume` does today.
- S8: two processes on the same run dir are not locked against each other, as `resume` today.
"""

from __future__ import annotations

import json
import re

from fakes import FakeAgents
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE


def test_resume_after_a_budget_stop(ws, tmp_path, monkeypatch, fake_fetch, capsys):
    agents = FakeAgents(cost=1.0)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, "--max-usd", "1.5")) == 2
    assert "STOPPED: circuit breaker" in capsys.readouterr().err
    run_dir = next((tmp_path / "runs").iterdir())
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "partition"  # explore did not finish
    paid = dict(state["explore_answers"])
    assert paid, "answers paid for before the stop are in the checkpoint"
    assert state["budget"]["cost_usd"] >= 1.5
    assert "FAILED circuit breaker" in (run_dir / "progress.log").read_text()

    paid_prompts = {c["prompt"] for c in agents.calls if c["schema"] == "ExploreOutput"}
    asked_before = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]) == 0
    resumed = {
        c["prompt"]
        for c in agents.calls[asked_before:]
        if c["schema"] == "ExploreOutput"
    }
    assert not paid_prompts & resumed, (
        "resume re-asked an explore task it had already paid for"
    )
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "done"
    assert (tmp_path / "out" / "bookings-walkthrough.md").is_file()


def test_resume_after_a_docs_gate_stop_takes_a_new_decision(
    ws, tmp_path, monkeypatch, capsys
):
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

    assert (
        main(["resume", str(run_dir), "--trust-repo"]) == 1
    )  # same decision, same stop
    capsys.readouterr()
    assert main(["resume", str(run_dir), "--trust-repo", "--no-docs"]) == 0
    assert (
        len([c for c in agents.calls if c["schema"] == "ExploreOutput"]) == explored
    )  # explore not re-asked
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "done" and not state["options"]["docs"]


def test_a_run_without_rg_stops_before_any_agent_call(
    ws, tmp_path, monkeypatch, fake_fetch, capsys
):
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k),
    )
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 1
    assert "ripgrep" in capsys.readouterr().err
    assert not agents.calls


def test_resume_after_a_stop_between_the_markdown_and_the_html(
    done_run, monkeypatch, capsys
):
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
    assert [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith("walkthrough: ")
    ] == [f"walkthrough: {out / n}" for n in OUTPUTS]
    assert_same_outputs(out, original)
    assert json.loads((run_dir / STATE_FILE).read_text())["stage"] == "done"


# --- resume --rewrite ---------------------------------------------------------------------------

WRITE_SCHEMAS = {
    "SectionOutput",
    "SummaryOutput",
    "UseCasesOutput",
    "FlowOutput",
    "OutputsOutput",
    "ConfigOutput",
}
KEPT = (
    "facts",
    "explore_answers",
    "docs",
    "docs_answers",
    "paths",
    "references",
    "symbols",
    "units",
    "terms",
)


def _kept(run_dir) -> dict:
    state = json.loads((run_dir / STATE_FILE).read_text())
    return {k: json.dumps(state[k], sort_keys=True) for k in KEPT}


def _body(md: str) -> str:
    """The Markdown without its run line, whose call count and spend differ between runs."""
    return "\n".join(ln for ln in md.splitlines() if not ln.startswith("> Generated "))


def test_rewrite_keeps_the_paid_answers_and_asks_only_write_calls(
    done_run, monkeypatch, capsys
):
    md_path, run_dir, _ = done_run
    before = _kept(run_dir)
    state = json.loads((run_dir / STATE_FILE).read_text())
    state["sections"]["summary"]["data"]["tagline"] = (
        "a stale tagline from the earlier write"
    )
    (run_dir / STATE_FILE).write_text(json.dumps(state))
    md_path.write_text("edited by hand\n")  # S5: overwritten
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 0
    assert agents.calls and {c["schema"] for c in agents.calls} <= WRITE_SCHEMAS
    assert {c["budget_label"] for c in agents.calls} == {"write"}
    assert {"UseCasesOutput", "FlowOutput", "SectionOutput"} <= {
        c["schema"] for c in agents.calls
    }
    assert _kept(run_dir) == before
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "done"
    assert (
        state["sections"]["summary"]["data"]["tagline"]
        == "Bookings takes reservations from Acme. [[c1]]"
    )
    assert "edited by hand" not in md_path.read_text()
    assert "SPEND:" in capsys.readouterr().out


def test_rewrite_refuses_a_run_before_its_trace_stage(done_run, monkeypatch, capsys):
    """S4."""
    md_path, run_dir, _ = done_run
    state = json.loads((run_dir / STATE_FILE).read_text())
    state["stage"] = "docs"
    (run_dir / STATE_FILE).write_text(json.dumps(state))
    saved = (run_dir / STATE_FILE).read_bytes()
    md = md_path.read_bytes()
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    capsys.readouterr()
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 1
    assert (
        f"resume --rewrite: {run_dir.resolve()} has not finished its trace stage"
        in capsys.readouterr().err
    )
    assert not agents.calls
    assert (run_dir / STATE_FILE).read_bytes() == saved and md_path.read_bytes() == md


def test_rewrite_needs_the_trust_gate(done_run, monkeypatch, capsys):
    _, run_dir, _ = done_run
    monkeypatch.delenv(
        "WALKTHROUGH_TRUST_REPO", raising=False
    )  # the fixture's run set it
    saved = (run_dir / STATE_FILE).read_bytes()
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["resume", str(run_dir), "--rewrite"]) == 1
    assert "--trust-repo" in capsys.readouterr().err
    assert not agents.calls and (run_dir / STATE_FILE).read_bytes() == saved


def test_a_stop_before_the_first_write_call_is_finished_by_a_plain_resume(
    done_run, monkeypatch
):
    """S1: the reset is saved before any write call, so the stop leaves the stage before `write` and no
    write answer; `resume` then writes what an unbroken rewrite writes."""
    md_path, run_dir, _ = done_run
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 0
    unbroken = md_path.read_text()

    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents(stop_write_after=0))
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 2
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert (
        state["stage"] == "trace"
        and state["write_answers"] == {}
        and state["sections"] == {}
    )
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert {c["schema"] for c in agents.calls} <= WRITE_SCHEMAS
    assert _body(md_path.read_text()) == _body(unbroken)


def test_a_budget_stop_mid_write_keeps_the_answers_and_resume_asks_only_the_rest(
    done_run, monkeypatch
):
    """S2."""
    md_path, run_dir, _ = done_run
    first = FakeAgents(stop_write_after=3)
    monkeypatch.setattr(sdk, "agent_call_structured", first)
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 2
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "trace" and len(state["write_answers"]) == 3
    paid = {c["prompt"] for c in first.calls[:3]}
    rest = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", rest)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert rest.calls and not paid & {c["prompt"] for c in rest.calls}
    assert json.loads((run_dir / STATE_FILE).read_text())["stage"] == "done"
    assert main(["check", str(md_path)]) == 0


def test_a_rewrite_after_the_head_moved_cites_the_saved_commits(
    fresh_ws, tmp_path, monkeypatch, fake_fetch, capsys
):
    """S6."""
    import fixture_repo

    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert main(run_args(fresh_ws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    saved = json.loads((run_dir / STATE_FILE).read_text())["repos"]["engine"][
        "head_sha"
    ]
    views = fresh_ws["engine"] / "bookings" / "views.py"
    views.write_text("# moved down\n\n\n" + views.read_text())
    moved = fixture_repo.commit(fresh_ws["engine"], "move every line")
    assert moved != saved
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 0
    md_path = tmp_path / "out" / "bookings-walkthrough.md"
    md = md_path.read_text()
    assert f"`{saved}`" in md and moved not in md
    capsys.readouterr()
    assert main(["check", str(md_path)]) == 0
    assert re.search(r"check: [1-9]\d* checked, 0 failed", capsys.readouterr().out)
