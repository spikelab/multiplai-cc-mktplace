"""Stop and resume of the core and page stages: one test per row S1–S17 and S19 of the table in
`INBOX/plan-walkthrough-page-writer-2026-10-23.md`. Each compares the resumed run with a run that never
stopped (its facts and their ids, its sections, `state.core`, `state.page`, and its Markdown and HTML
without the run line) except where the row says otherwise.

S18 (a repository's HEAD moved) is `test_resume.py::test_a_rewrite_after_the_head_moved_cites_the_saved_commits`.
S20 (the stopped process still alive) has no test: no lock exists and none is added.
"""

from __future__ import annotations

import json
import re

import pytest
from fakes import FakeAgents
from test_pipeline import run_args
from walkthrough_pipeline import budget, orchestrator, sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import markdown_headings
from walkthrough_pipeline.state import STATE_FILE, load_state

CORE_TITLE = "How it stores bookings"
# The title of the commands table this version removed, built in two parts so a search of the skill for
# the old title finds only that it is gone.
OLD_COMMANDS_TITLE = "Which command " + "to run"
MD = "bookings-walkthrough.md"


class Killed(BaseException):
    """The process killed mid-stage: nothing catches it, so nothing is saved after the last stage."""


class KillingAgents(FakeAgents):
    """FakeAgents that kill the process on the first call whose prompt holds *kill_on*."""

    def __init__(self, kill_on: str, **kw):
        super().__init__(**kw)
        self.kill_on = kill_on

    async def __call__(self, prompt: str, schema, **kw):
        if self.kill_on in prompt:
            raise Killed
        return await super().__call__(prompt, schema, **kw)


def _killed(call) -> None:
    """*call* ends with `Killed`, alone or inside the exception group of a task group."""
    try:
        call()
    except BaseExceptionGroup as e:
        assert e.subgroup(Killed) is not None, e
    except Killed:
        pass
    else:
        raise AssertionError("not killed")


def _go(ws, base, monkeypatch, agents, *extra) -> int:
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    return main(run_args(ws, base, *extra))


def _run_dir(base):
    return next((base / "runs").iterdir())


def _resume(base, monkeypatch, agents, *extra) -> int:
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    return main(["resume", str(_run_dir(base)), "--trust-repo", *extra])


def _state(base):
    return load_state(_run_dir(base) / STATE_FILE)


def _raw(base) -> dict:
    return json.loads((_run_dir(base) / STATE_FILE).read_text())


def _result(base) -> dict:
    """What the comparisons hold equal: facts and ids, sections, core, page, and the two files."""
    state = _state(base)
    md = (base / "out" / MD).read_text()
    html = (base / "out" / MD).with_suffix(".html").read_text()
    return {
        "facts": [
            (
                f.id,
                f.claim,
                f.source,
                [(c.id, c.path, c.line_start, c.line_end) for c in f.citations],
            )
            for f in state.facts
        ],
        "sections": {k: v.model_dump() for k, v in state.sections.items()},
        "core": state.core,
        "page": state.page,
        "md": "\n".join(l for l in md.split("\n") if not l.startswith("> Generated")),
        "html": re.sub(r'<p class="run">.*?</p>', "", html),
    }


def _unbroken(ws, tmp_path, monkeypatch, **fake) -> dict:
    assert _go(ws, tmp_path / "a", monkeypatch, FakeAgents(**fake)) == 0
    return _result(tmp_path / "a")


def _schemas(agents: FakeAgents) -> list[str]:
    return [c["schema"] for c in agents.calls]


def _core_calls(agents: FakeAgents) -> list[str]:
    return [
        "choice" if c["schema"] == "CoreChoice" else "read"
        for c in agents.calls
        if c["schema"] == "CoreChoice" or "Core part: [core:read]" in c["prompt"]
    ]


def _page_calls(agents: FakeAgents) -> list[dict]:
    return [c for c in agents.calls if c["schema"] == "PageOutput"]


# --- core ---------------------------------------------------------------------------------------------


def test_s1_a_stop_after_the_core_choice_reuses_it_and_asks_only_the_deep_read(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_core_after=1)) == 2
    raw = _raw(b)
    assert raw["stage"] == "survey" and list(raw["core_answers"]) == ["core:choice"]
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == ["read"]
    assert _result(b) == want


def test_s2_a_stop_before_the_core_choice_asks_the_choice_and_the_deep_read(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_core_after=0)) == 2
    raw = _raw(b)
    assert raw["stage"] == "survey" and raw["core_answers"] == {}
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == ["choice", "read"]
    assert _result(b) == want


def test_s3_a_core_choice_that_fails_twice_completes_the_stage_and_is_not_asked_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    """S3: no deep read and no core section; a later resume (here after a stop in the page stage) asks
    no core call again."""
    plant = {"core_outside_file"}
    want = _unbroken(ws, tmp_path, monkeypatch, plant=plant)
    assert CORE_TITLE not in markdown_headings(want["md"])
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(plant=plant, stop_page_after=0)) == 2
    state = _state(b)
    assert list(state.core_answers) == ["core:choice", "core:choice:rewrite"]
    assert state.core == {} and "core:read" not in state.explore_answers
    assert [d.stage for d in state.dropped if d.stage == "core"] == ["core"]
    agents = FakeAgents(plant=plant)
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == [] and _schemas(agents) == ["PageOutput"]
    assert _result(b) == want


def test_s4_a_failed_deep_read_completes_the_stage_and_writes_the_section_from_the_choice(
    ws, tmp_path, monkeypatch, fake_fetch
):
    """S4: the deep read's error is saved and recorded; the core section is still written, and a later
    resume does not ask the deep read again."""
    b = tmp_path / "b"
    agents = FakeAgents(fail_on="Core part: [core:read]", stop_page_after=0)
    assert _go(ws, b, monkeypatch, agents) == 2
    state = _state(b)
    assert "error" in state.explore_answers["core:read"]
    assert any("core:read" in e for e in state.errors)
    assert state.core["title"] == CORE_TITLE
    assert not [f for f in state.facts if f.source.startswith("core:read/")]
    again = FakeAgents()
    assert _resume(b, monkeypatch, again) == 0
    assert _core_calls(again) == [] and _schemas(again) == ["PageOutput"]
    assert CORE_TITLE in markdown_headings((b / "out" / MD).read_text())


def test_s5_a_kill_mid_core_loses_its_answers_and_the_stage_runs_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    _killed(lambda: _go(ws, b, monkeypatch, KillingAgents("Core part: [core:read]")))
    raw = _raw(b)
    assert raw["stage"] == "survey" and raw["core_answers"] == {}
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == ["choice", "read"]
    assert _result(b) == want


# --- page ---------------------------------------------------------------------------------------------


def test_s6_a_stop_after_the_first_page_answer_asks_only_the_reask(
    ws, tmp_path, monkeypatch, fake_fetch
):
    plant = {"page_script"}
    want = _unbroken(ws, tmp_path, monkeypatch, plant=plant)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(plant=plant, stop_page_after=1)) == 2
    raw = _raw(b)
    assert raw["stage"] == "write" and list(raw["page_answers"]) == ["page"]
    agents = FakeAgents(plant=plant)
    assert _resume(b, monkeypatch, agents) == 0
    (call,) = _page_calls(agents)
    assert _schemas(agents) == ["PageOutput"]
    assert "the tag <script> is not allowed" in call["prompt"]
    assert _result(b) == want


def test_s7_a_stop_before_the_first_page_answer_asks_the_page_call(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_page_after=0)) == 2
    raw = _raw(b)
    assert raw["stage"] == "write" and raw["page_answers"] == {}
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _schemas(agents) == ["PageOutput"]
    assert _result(b) == want


def test_s8_a_kill_mid_page_loses_its_answers_and_asks_the_page_call_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    _killed(lambda: _go(ws, b, monkeypatch, KillingAgents("open body of a web page")))
    raw = _raw(b)
    assert raw["stage"] == "write" and raw["page_answers"] == {}
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _schemas(agents) == ["PageOutput"]
    assert _result(b) == want


def test_s9_a_page_that_fails_is_drawn_by_code_and_asked_again_only_on_rewrite(
    ws, tmp_path, monkeypatch, fake_fetch
):
    plant = {"page_script:always"}
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(plant=plant)) == 0
    state = _state(b)
    assert state.page == {}
    assert any(e.startswith("page: ") for e in state.errors)
    html_path = (b / "out" / MD).with_suffix(".html")
    html = html_path.read_text()
    assert 'class="page-body"' not in html
    order = re.findall(r'<section id="([a-z]+)">', html)
    assert order[:7] == [
        "summary",
        "flow",
        "core",
        "outputs",
        "config",
        "usecases",
        "gotchas",
    ]
    # `render` draws the same page with no call.
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["render", str(_run_dir(b))]) == 0
    assert not agents.calls and html_path.read_text() == html
    # A plain resume asks nothing.
    assert _resume(b, monkeypatch, agents) == 0
    assert not agents.calls
    # `resume --rewrite` asks the write and page calls again.
    assert _resume(b, monkeypatch, agents, "--rewrite") == 0
    assert "PageOutput" in _schemas(agents) and "CoreChoice" not in _schemas(agents)
    assert _state(b).page


def test_s10_a_page_over_the_limit_is_written_with_its_count_and_not_asked_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    plant = {"page_long:always"}
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(plant=plant)) == 0
    state = _state(b)
    words = state.page["words"]
    assert words > 1200
    assert f"page: open body {words} words, limit 1200" in state.errors
    assert (
        f"Open body of the HTML page: {words:,} words" in (b / "out" / MD).read_text()
    )
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert not agents.calls


# --- render ---------------------------------------------------------------------------------------------


def test_s11_a_kill_after_the_page_stage_writes_the_files_with_no_call(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    real = orchestrator.write_outputs

    def killed(*a, **kw):
        raise Killed

    monkeypatch.setattr(orchestrator, "write_outputs", killed)
    _killed(lambda: _go(ws, b, monkeypatch, FakeAgents()))
    assert _raw(b)["stage"] == "page" and not (b / "out" / MD).exists()
    monkeypatch.setattr(orchestrator, "write_outputs", real)
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert not agents.calls
    assert (b / "out" / MD.replace("-walkthrough.md", "-paths.json")).exists()
    assert _result(b) == want


def test_s12_render_of_a_finished_run_rewrites_the_files_with_no_call(
    done_run, monkeypatch, capsys
):
    md_path, run_dir, _ = done_run
    out = md_path.parent
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert sorted(before) == [
        "bookings-paths.json",
        "bookings-walkthrough.html",
        "bookings-walkthrough.md",
    ]
    for p in out.iterdir():
        p.write_text("edited by hand")
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    monkeypatch.setattr(budget, "_process", budget.ProcessSpend())
    capsys.readouterr()
    assert main(["render", str(run_dir)]) == 0
    assert capsys.readouterr().out.strip().split("\n")[-1] == "SPEND:0.000000:0"
    assert not agents.calls
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_s13_render_of_a_run_from_before_this_version_draws_the_page_by_code(
    done_run, tmp_path, monkeypatch
):
    """S13: a state with no core and no page fields, and the commands section of the old
    codebase tables: the Markdown and the page follow the new order with neither section."""
    _, run_dir, _ = done_run
    raw = json.loads((run_dir / STATE_FILE).read_text())
    for field in ("core", "core_answers", "page", "page_answers"):
        del raw[field]
    del raw["sections"]["core"]
    raw["sections"]["commands"] = {
        "key": "commands",
        "title": OLD_COMMANDS_TITLE,
        "data": {"rows": []},
    }
    (run_dir / STATE_FILE).write_text(json.dumps(raw))
    again = tmp_path / "again"
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["render", str(run_dir), "--output", str(again)]) == 0
    assert not agents.calls
    md = (again / MD).read_text()
    html = (again / MD).with_suffix(".html").read_text()
    assert markdown_headings(md) == [
        "What it is",
        "How it works",
        "What it creates",
        "Configuration",
        "Use cases",
        "Gotchas",
        "Reference",
        "How this was made",
    ]
    assert OLD_COMMANDS_TITLE not in md + html
    assert 'class="page-body"' not in html and 'id="core"' not in html
    assert (
        'class="flowpic"'
        in html.split('<section id="summary">', 1)[1].split("</section>")[0]
    )


# --- resume ---------------------------------------------------------------------------------------------


def _strip_new_fields(base) -> None:
    raw = _raw(base)
    for field in ("core", "core_answers", "page", "page_answers"):
        raw.pop(field, None)
    (_run_dir(base) / STATE_FILE).write_text(json.dumps(raw))


def test_s14_a_state_from_before_this_version_stopped_at_survey_runs_core_and_page(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_core_after=0)) == 2
    assert _raw(b)["stage"] == "survey"
    _strip_new_fields(b)
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == ["choice", "read"]
    assert _schemas(agents)[-1] == "PageOutput"
    assert _result(b) == want


@pytest.mark.parametrize(
    ("saved", "stop_in"), [("docs", "trace"), ("trace", "write"), ("write", "page")]
)
def test_s15_a_state_from_before_this_version_past_survey_skips_core_and_runs_page(
    ws, tmp_path, monkeypatch, fake_fetch, saved, stop_in
):
    """S15: a run of the earlier version, which had no core stage, stopped in *stop_in*, so its state
    is saved at *saved*; then the fields this version adds are taken out of the state."""
    b = tmp_path / "b"
    real = orchestrator.STAGES

    async def stopping(state, ctx):
        raise budget.BudgetExceededError(
            f"circuit breaker: planted stop during {stop_in}"
        )

    monkeypatch.setattr(
        orchestrator,
        "STAGES",
        tuple((n, stopping if n == stop_in else f) for n, f in real if n != "core"),
    )
    assert _go(ws, b, monkeypatch, FakeAgents()) == 2
    assert _raw(b)["stage"] == saved
    _strip_new_fields(b)
    monkeypatch.setattr(orchestrator, "STAGES", real)
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == []
    assert _schemas(agents)[-1] == "PageOutput"
    state = _state(b)
    assert state.core == {} and state.page
    md = (b / "out" / MD).read_text()
    assert CORE_TITLE not in markdown_headings(md)
    assert 'id="core"' not in (b / "out" / MD).with_suffix(".html").read_text()


def test_s16_rewrite_keeps_the_core_and_asks_only_the_write_and_page_calls(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    a = tmp_path / "a"
    kept = {
        k: _raw(a)[k]
        for k in ("explore_answers", "core_answers", "core", "docs_answers", "paths")
    }
    agents = FakeAgents()
    assert _resume(a, monkeypatch, agents, "--rewrite") == 0
    assert set(_schemas(agents)) - {"PageOutput"} <= {
        "SectionOutput",
        "SummaryOutput",
        "UseCasesOutput",
        "FlowOutput",
        "CoreOutput",
        "OutputsOutput",
        "ConfigOutput",
    }
    assert _schemas(agents).count("PageOutput") == 1
    assert {k: _raw(a)[k] for k in kept} == kept
    assert list(_state(a).page_answers) == ["page"]
    assert _result(a) == want


def test_s16_rewrite_clears_the_page_answers_and_the_page_at_once(
    ws, tmp_path, monkeypatch, fake_fetch
):
    """S16: the rewind is saved before the first write call, so a stop there leaves no page behind."""
    a = tmp_path / "a"
    assert _go(ws, a, monkeypatch, FakeAgents()) == 0
    assert _resume(a, monkeypatch, FakeAgents(stop_write_after=0), "--rewrite") == 2
    raw = _raw(a)
    assert (
        raw["page_answers"] == {} and raw["page"] == {} and raw["write_answers"] == {}
    )
    assert raw["core"]["title"] == CORE_TITLE and raw["core_answers"]


# --- perturbations ----------------------------------------------------------------------------------------


def test_s17_an_edited_working_tree_between_stop_and_resume_changes_nothing(
    fresh_ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(fresh_ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(fresh_ws, b, monkeypatch, FakeAgents(stop_core_after=1)) == 2
    for f in (fresh_ws["engine"] / "bookings").glob("*.py"):
        f.write_text("# edited after the stop\n" + f.read_text())
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _result(b) == want


def test_s19_a_core_step_stopped_twice_continues_from_the_last_saved_answer(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_core_after=1)) == 2
    first = FakeAgents(stop_core_after=0)
    assert _resume(b, monkeypatch, first) == 2
    assert _core_calls(first) == ["read"]
    assert list(_raw(b)["core_answers"]) == ["core:choice"]
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _core_calls(agents) == ["read"]
    assert _result(b) == want


def test_s19_a_page_step_stopped_twice_continues_from_the_last_saved_answer(
    ws, tmp_path, monkeypatch, fake_fetch
):
    plant = {"page_script"}
    want = _unbroken(ws, tmp_path, monkeypatch, plant=plant)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(plant=plant, stop_page_after=1)) == 2
    first = FakeAgents(plant=plant, stop_page_after=0)
    assert _resume(b, monkeypatch, first) == 2
    assert list(_raw(b)["page_answers"]) == ["page"]
    agents = FakeAgents(plant=plant)
    assert _resume(b, monkeypatch, agents) == 0
    assert _schemas(agents) == ["PageOutput"]
    assert _result(b) == want
