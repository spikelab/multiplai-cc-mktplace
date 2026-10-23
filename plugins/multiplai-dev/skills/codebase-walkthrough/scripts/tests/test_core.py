"""The core stage: the part of the code that does its main job gets a deeper read and its own section.

No model is called; `FakeAgents` answers the choice from the files listed and the deep read from the
`def`/`class` lines of the chosen files.
"""

from __future__ import annotations

import re

import fixture_repo
import pytest
from fakes import FakeAgents
from test_explainer import _Corpus
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.gates import core_gate
from walkthrough_pipeline.models import Citation, CoreChoice
from walkthrough_pipeline.render import markdown_headings
from walkthrough_pipeline.state import STATE_FILE, load_state

TITLE = "How it stores bookings"
CITES = {
    "c1": Citation(path="engine/a.py", line_start=1, line_end=1, quote="x = 1", id="c1")
}


def _check(**changes) -> list[str]:
    choice = {
        "title": "How it finds bugs",
        "job": "It finds bugs in a change. [[c1]]",
        "files": ["engine/a.py"],
        "why": "That file runs the finders.",
        "cites": ["c1"],
        **changes,
    }
    return core_gate(
        CoreChoice.model_validate(choice),
        target_files={"engine/a.py", "engine/b.py"},
        citations=CITES,
        corpus=_Corpus(set()),
        labels=set(),
    )


def test_a_plain_choice_of_target_files_passes():
    assert _check() == []


def test_a_file_outside_the_target_fails():
    assert _check(files=["engine/a.py", "front/x.py"]) == [
        "front/x.py is not a file of the target"
    ]


def test_a_title_with_a_citation_a_path_or_backticks_fails():
    assert "citation in a title" in _check(title="How it finds bugs [[c1]]")
    assert "the title holds a path" in _check(title="How engine/a.py works")
    assert "the title holds backticks" in _check(title="How `run` works")
    assert "the title is over 60 characters" in _check(title="x" * 61)


def test_an_unknown_cite_and_a_failing_job_fail():
    reasons = _check(cites=["c9"], job="See engine/a.py:1.")
    assert "[[c9]] is not a gated citation" in reasons
    assert "engine/a.py:1 is a path with a line number typed in the text" in reasons


def _core_calls(agents: FakeAgents) -> list[dict]:
    return [
        c
        for c in agents.calls
        if c["schema"] == "CoreChoice" or "Core part: [core:read]" in c["prompt"]
    ]


def test_a_module_run_chooses_the_part_reads_it_and_writes_its_section(done_run):
    md_path, run_dir, agents = done_run
    calls = _core_calls(agents)
    assert [c["schema"] for c in calls] == ["CoreChoice", "ExploreOutput"]
    choice, read = calls
    assert choice["allowed_tools"] == [] and choice["budget_label"] == "core"
    assert read["allowed_tools"] == sdk.EXPLORE_TOOLS
    assert read["budget_label"] == "core:read"
    state = load_state(run_dir / STATE_FILE)
    assert state.core["title"] == TITLE
    assert state.core["files"] == ["engine/bookings/apps.py"]
    assert "core:read" in state.explore_answers
    assert "core:read" not in state.core_answers
    assert list(state.core_answers) == ["core:choice"]
    # The deep read's facts get ids after every other fact's.
    sources = [f.source for f in state.facts]
    first_core = next(i for i, s in enumerate(sources) if s.startswith("core:read/"))
    assert all(s.startswith("core:read/") for s in sources[first_core:])
    # The section comes right after How it works, in the Markdown and in the page.
    md = md_path.read_text()
    heads = markdown_headings(md)
    assert heads[heads.index("How it works") + 1] == TITLE
    html = md_path.with_suffix(".html").read_text()
    h2 = [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h2>(.*?)</h2>", html)]
    assert h2[h2.index("How it works") + 1] == TITLE
    core = html.split('<section id="core">', 1)[1].split("</section>", 1)[0]
    assert "It saves each booking as one row." in core
    assert "<dt>Looks at</dt>" in core


def test_the_core_section_material_leads_with_the_deep_read(done_run):
    _, _, agents = done_run
    prompt = next(c["prompt"] for c in agents.calls if c["schema"] == "CoreOutput")
    material = prompt.split("Material (", 1)[1]
    first = re.search(r"^- \[c\d+\] (.*)$", material, re.MULTILINE).group(1)
    assert first.startswith("core: ")


def test_a_choice_that_fails_twice_leaves_no_core_section(
    ws, tmp_path, monkeypatch, fake_fetch
):
    agents = FakeAgents(plant={"core_outside_file"})
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 0
    calls = _core_calls(agents)
    assert [c["schema"] for c in calls] == ["CoreChoice", "CoreChoice"]
    assert "is not a file of the target" in calls[1]["prompt"]
    run_dir = next((tmp_path / "runs").iterdir())
    state = load_state(run_dir / STATE_FILE)
    assert state.core == {} and "core:read" not in state.explore_answers
    assert list(state.core_answers) == ["core:choice", "core:choice:rewrite"]
    md = (tmp_path / "out" / "bookings-walkthrough.md").read_text()
    assert TITLE not in markdown_headings(md)
    cut = md.split("### Cut by the checks", 1)[1]
    assert "elsewhere/not_the_target.py is not a file of the target" in cut


def test_agent_mode_makes_no_core_call(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, "--mode", "agent")) == 0
    assert agents.calls and not _core_calls(agents)
    state = load_state(next((tmp_path / "runs").iterdir()) / STATE_FILE)
    assert state.core == {} and state.core_answers == {}


@pytest.fixture
def change(fresh_ws):
    base, head = fixture_repo.change_branch(fresh_ws["engine"])
    return fresh_ws, base, head


def test_a_change_run_makes_no_core_call(change, tmp_path, monkeypatch):
    from test_change_scope import _args

    ws, base, _ = change
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(ws, tmp_path, "--range", f"{base}..feature")) == 0
    assert agents.calls and not _core_calls(agents)
