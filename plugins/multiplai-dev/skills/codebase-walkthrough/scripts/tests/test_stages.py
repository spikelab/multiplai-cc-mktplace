"""The agent stages with planted failures: the gates cut what the fake agents made up."""

from __future__ import annotations

import json
import re

import pytest

from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE
from test_pipeline import run_args


def _run(ws, tmp_path, monkeypatch, agents):
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    state = json.loads((run_dir / STATE_FILE).read_text())
    return (tmp_path / "out" / "bookings-walkthrough.md").read_text(), state


def test_explore_drops_a_fact_whose_citation_is_invented(ws, tmp_path, monkeypatch, fake_fetch):
    md, state = _run(ws, tmp_path, monkeypatch, FakeAgents())
    assert {"stage": "explore", "what": "an invented claim", "reason": "no citation passed"} in state["dropped"]
    assert "an invented claim" not in md
    assert re.search(r"Claims dropped by the explore checks \| [1-9]\d* \|", md)


def test_docs_drop_a_quote_that_is_not_on_the_page(ws, tmp_path, monkeypatch, fake_fetch):
    md, state = _run(ws, tmp_path, monkeypatch, FakeAgents())
    reasons = [d["reason"] for d in state["dropped"] if d["stage"] == "docs"]
    assert "doc quote not in the page" in reasons
    assert all(c["doc_quote"] != "this sentence is not on the page" for c in state["docs"]["claims"])
    # only llms.txt pages on the docs host were fetched
    assert sorted(fake_fetch) == ["https://docs.acme.test/api/bookings-collection.md",
                                  "https://docs.acme.test/api/webhook-collection.md",
                                  "https://docs.acme.test/llms.txt"]


def test_write_gate_cuts_a_paragraph_with_an_invented_path_line(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents(invent_path=True)
    md, state = _run(ws, tmp_path, monkeypatch, agents)
    body, _, appendix = md.partition("## Cut by the checks")
    assert "nowhere.py:99" not in body
    assert "nowhere.py:99" in appendix and "is not a gated citation" in appendix
    rewrites = [c for c in agents.calls if c["schema"] == "SectionOutput" and "failed these checks" in c["prompt"]]
    assert rewrites, "a failing paragraph gets one rewrite before it is cut"
    assert all(c["allowed_tools"] == [] for c in agents.calls if c["schema"] == "SectionOutput")


def test_trace_gate_cuts_a_broken_chain(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents(broken_trace=True)
    md, state = _run(ws, tmp_path, monkeypatch, agents)
    webhook = next(s for s in state["scenarios"] if s["seed"]["path"].endswith("bookings/urls.py"))
    assert webhook["stops_at"] == 2 and len(webhook["hops"]) == 2
    assert "> trace stops at hop 2:" in md
    assert any(c["schema"] == "TraceOutput" and "rejected by a program" in c["prompt"] for c in agents.calls)


def test_agents_get_read_only_tools_and_run_in_the_snapshot(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    _run(ws, tmp_path, monkeypatch, agents)
    for c in agents.calls:
        if c["schema"] in ("ExploreOutput", "TraceOutput", "DocsOutput"):
            assert c["allowed_tools"] == ["Read", "Grep", "Glob"]
            assert c["cwd"].endswith(("/snap", "/docs-cache"))


def test_docs_pages_are_fenced_and_the_injection_is_data(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    _run(ws, tmp_path, monkeypatch, agents)
    run_dir = next((tmp_path / "runs").iterdir())
    for f in (run_dir / "docs-cache").glob("*.md"):
        text = f.read_text()
        assert text.startswith("<untrusted-content source=") and text.rstrip().endswith("</untrusted-content>")
    docs_prompts = [c["prompt"] for c in agents.calls if c["schema"] == "DocsOutput"]
    assert docs_prompts and all("is data" in p for p in docs_prompts)


def test_untrusted_repo_is_refused_before_any_call(ws, tmp_path, monkeypatch, capsys):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    args = [a for a in run_args(ws, tmp_path) if a != "--trust-repo"]
    assert main(args) == 1
    assert "--trust-repo" in capsys.readouterr().err
    assert agents.calls == []
