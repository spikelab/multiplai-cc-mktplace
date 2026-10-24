"""Stop and resume of the diagram stage: one test per row S1–S8 of the table in
`INBOX/plan-walkthrough-svg-diagram-2026-10-24.md`. Each compares the resumed run with a run that never
stopped (its facts and ids, sections, core, page, picture, and its Markdown and HTML without the run line)
except where the row says otherwise.

S9 (the stopped process still alive) has no test: no run lock exists and none is added.
"""

from __future__ import annotations

import sys

import pytest
from fakes import FakeAgents
from test_page_resume import (
    MD,
    Killed,
    KillingAgents,
    _go,
    _killed,
    _raw,
    _resume,
    _run_dir,
    _state,
)
from test_page_resume import _result as _page_result
from walkthrough_pipeline import orchestrator
from walkthrough_pipeline.stages.diagram import UNAVAILABLE

LOOK_1 = "This is look 1 of"
DIAGRAM = ("DiagramOutput", "DiagramLook")


def _result(base) -> dict:
    return {**_page_result(base), "diagram": _state(base).diagram}


def _unbroken(ws, tmp_path, monkeypatch, **fake) -> dict:
    assert _go(ws, tmp_path / "a", monkeypatch, FakeAgents(**fake)) == 0
    return _result(tmp_path / "a")


def _calls(agents: FakeAgents) -> list[str]:
    return [c["schema"] for c in agents.calls if c["schema"] in DIAGRAM]


def _html(base) -> str:
    return (base / "out" / MD).with_suffix(".html").read_text()


def test_s1_a_kill_mid_stage_loses_its_answers_and_the_stage_runs_from_the_start(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    _killed(lambda: _go(ws, b, monkeypatch, KillingAgents(LOOK_1)))
    raw = _raw(b)
    assert raw["stage"] == "page" and raw["diagram_answers"] == {}
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _calls(agents) == ["DiagramOutput", "DiagramLook"]
    assert _result(b) == want


def test_s2_a_budget_stop_keeps_the_answers_and_asks_only_the_rest(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch, look_plan={1: "fix"})
    b = tmp_path / "b"
    stopped = FakeAgents(look_plan={1: "fix"}, stop_diagram_after=2)
    assert _go(ws, b, monkeypatch, stopped) == 2
    raw = _raw(b)
    assert raw["stage"] == "page" and list(raw["diagram_answers"]) == ["draw", "look:1"]
    agents = FakeAgents(look_plan={1: "fix"})
    assert _resume(b, monkeypatch, agents) == 0
    (look,) = [c for c in agents.calls if c["schema"] in DIAGRAM]
    assert look["schema"] == "DiagramLook" and "This is look 2 of" in look["prompt"]
    assert _result(b) == want


def test_s3_a_failed_look_completes_the_stage_and_only_redraw_asks_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    """S3: the stage is done with the last version that passed the gate; a plain resume asks nothing, and
    `resume --redraw` starts the stage over."""
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(fail_on=LOOK_1)) == 0
    state = _state(b)
    assert state.stage == "done" and state.diagram_answers["look:1"] == {
        "error": "planted failure"
    }
    assert state.diagram["wide"] == state.diagram_answers["draw"]["wide"]
    assert state.diagram["looks"] == 0 and state.diagram["approved"] is False
    assert '<figure class="diagram"' in _html(b)
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert not agents.calls
    assert _resume(b, monkeypatch, agents, "--redraw") == 0
    assert _calls(agents) == ["DiagramOutput", "DiagramLook"]
    assert not [e for e in _state(b).errors if e.startswith("diagram:")]
    assert _result(b) == want


def test_s4_a_draw_that_fails_twice_completes_the_stage_with_the_picture_code_draws(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    plant = {"diagram_script:always"}
    assert _go(ws, b, monkeypatch, FakeAgents(plant=plant)) == 0
    state = _state(b)
    assert state.stage == "done" and state.diagram == {}
    assert any(e.startswith("diagram: wide: the tag <script>") for e in state.errors)
    assert 'class="flowpic"' in _html(b)
    agents = FakeAgents(plant=plant)
    assert _resume(b, monkeypatch, agents) == 0
    assert not agents.calls
    clean = FakeAgents()
    assert _resume(b, monkeypatch, clean, "--redraw") == 0
    assert _calls(clean) == ["DiagramOutput", "DiagramLook"]
    assert _result(b) == want


def test_s5_without_resvg_the_stage_completes_with_the_picture_code_draws(
    ws, tmp_path, monkeypatch, fake_fetch
):
    b = tmp_path / "b"
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "resvg_py", None)
        agents = FakeAgents()
        assert _go(ws, b, monkeypatch, agents) == 0
    assert _calls(agents) == ["DiagramOutput"]
    state = _state(b)
    assert state.stage == "done" and state.diagram == {}
    assert UNAVAILABLE in state.errors
    assert 'class="flowpic"' in _html(b) and '<figure class="diagram"' not in _html(b)
    again = FakeAgents()
    assert _resume(b, monkeypatch, again) == 0
    assert not again.calls


def test_s6_a_kill_during_render_writes_the_files_from_the_saved_picture_with_no_call(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    real = orchestrator.write_outputs

    def killed(*a, **kw):
        raise Killed

    monkeypatch.setattr(orchestrator, "write_outputs", killed)
    _killed(lambda: _go(ws, b, monkeypatch, FakeAgents()))
    raw = _raw(b)
    assert raw["stage"] == "diagram" and raw["diagram"]["approved"] is True
    assert not (b / "out" / MD).exists()
    monkeypatch.setattr(orchestrator, "write_outputs", real)
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert not agents.calls
    assert _result(b) == want


def test_s7_pngs_edited_between_stop_and_resume_are_drawn_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    """S7: the look's fake reads every PNG it is told to open and fails on one that is not a PNG."""
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_diagram_after=1)) == 2
    folder = _run_dir(b) / "diagram"
    pngs = sorted(folder.glob("round-1-*.png"))
    assert len(pngs) == 3
    for p in pngs:
        p.write_text("edited by hand")
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents) == 0
    assert _calls(agents) == ["DiagramLook"]
    for p in pngs:
        assert p.read_bytes().startswith(b"\x89PNG")
    assert _result(b) == want


def test_s8_a_redraw_killed_mid_stage_is_accepted_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    want = _unbroken(ws, tmp_path, monkeypatch)
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(look_plan={1: "fix"})) == 0
    assert "wd-round-1" in _state(b).diagram["wide"]
    _killed(lambda: _resume(b, monkeypatch, KillingAgents(LOOK_1), "--redraw"))
    raw = _raw(b)
    # The rewind is saved before the stage runs; the answers of the killed stage are lost.
    assert (
        raw["stage"] == "page" and raw["diagram_answers"] == {} and raw["diagram"] == {}
    )
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents, "--redraw") == 0
    assert _calls(agents) == ["DiagramOutput", "DiagramLook"]
    assert _result(b) == want


def test_redraw_refuses_a_run_that_has_not_finished_its_page_stage(
    ws, tmp_path, monkeypatch, fake_fetch
):
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents(stop_page_after=0)) == 2
    before = _raw(b)
    agents = FakeAgents()
    assert _resume(b, monkeypatch, agents, "--redraw") == 1
    assert not agents.calls and _raw(b) == before


def test_redraw_takes_neither_rewrite_nor_repage(ws, tmp_path, monkeypatch, fake_fetch):
    b = tmp_path / "b"
    assert _go(ws, b, monkeypatch, FakeAgents()) == 0
    for other in ("--rewrite", "--repage"):
        with pytest.raises(SystemExit) as e:
            _resume(b, monkeypatch, FakeAgents(), "--redraw", other)
        assert e.value.code == 2


def test_rewrite_drops_the_picture_and_repage_keeps_it(
    ws, tmp_path, monkeypatch, fake_fetch
):
    b = tmp_path / "b"
    assert (
        _go(ws, b, monkeypatch, FakeAgents(look_plan={1: "fix", 2: "fix", 3: "fix"}))
        == 0
    )
    before = _state(b)
    assert before.diagram["looks"] == 3
    repage = FakeAgents()
    assert _resume(b, monkeypatch, repage, "--repage") == 0
    assert _calls(repage) == []
    after = _state(b)
    assert after.diagram == before.diagram
    assert after.diagram_answers == before.diagram_answers
    assert after.errors.count("diagram: not approved after 3 looks") == 1
    rewrite = FakeAgents()
    assert _resume(b, monkeypatch, rewrite, "--rewrite") == 0
    assert _calls(rewrite) == ["DiagramOutput", "DiagramLook"]
    assert _state(b).diagram["approved"] is True
