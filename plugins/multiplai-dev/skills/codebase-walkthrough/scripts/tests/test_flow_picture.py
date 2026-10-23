"""The flow's typed boxes: the rules `flow_gate` adds for them, the picture the HTML page draws from them,
and the Mermaid shape of each kind in the Markdown. No model is called."""

from __future__ import annotations

import re

from test_explainer import _calls, _cut, _run
from walkthrough_pipeline.gates import Corpus, flow_gate
from walkthrough_pipeline.models import Citation, FlowOutput
from walkthrough_pipeline.render import flow_mermaid, flow_picture, section_texts


class _Corpus(Corpus):
    def __init__(self, words: set[str]):
        self.words = words

    def contains(self, text: str, *, ignore_case: bool = False) -> bool:
        return text in self.words


CITES = {
    f"c{i}": Citation(
        path="engine/a.py",
        line_start=i,
        line_end=i,
        quote=f"MAX_FINDERS = 5  # line {i}",
        id=f"c{i}",
    )
    for i in range(1, 6)
}


def _gate(flow: dict, *, follow: bool = True) -> dict:
    return flow_gate(
        FlowOutput.model_validate(flow),
        citations=CITES,
        corpus=_Corpus(set()),
        labels=set(),
        steps_follow_boxes=follow,
    )


def _flow(**changes) -> dict:
    flow = {
        "nodes": [
            {"id": "a", "label": "The change", "cite": "c1", "kind": "INPUT"},
            {
                "id": "b",
                "label": "Find problems",
                "cite": "c2",
                "kind": "agent",
                "does": "Many finders read the change",
                "fan_out": "5 aspect finders",
                "reads": ["the diff"],
                "writes": ["candidate findings"],
            },
            {"id": "c", "label": "Check the quotes", "cite": "c3", "kind": "check"},
            {"id": "d", "label": "The findings file", "cite": "c4", "kind": "output"},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
            {"from": "c", "to": "d"},
            {"from": "c", "to": "b"},
        ],
        "steps": [
            {"node": "b", "title": "Find", "summary": "Finders read it. [[c2]]"},
            {"node": "c", "title": "Check", "summary": "Code checks it. [[c3]]"},
        ],
    }
    flow.update(changes)
    return flow


def test_a_flow_with_one_step_per_inner_box_in_order_passes():
    g = _gate(_flow())
    assert g["order"] == [] and g["graph"] == [] and g["cut"] == []
    assert [n["kind"] for n in g["nodes"]] == ["input", "agent", "check", "output"]
    assert g["nodes"][1]["fan_out"] == "5 aspect finders"
    assert [s["node"] for s in g["steps"]] == ["b", "c"]


def test_a_step_naming_no_box_fails_the_order_rule():
    flow = _flow()
    flow["steps"][1]["node"] = "zz"
    order = _gate(flow)["order"]
    assert "step 'Check' names no drawn box ('zz')" in order
    assert (
        "box 'Check the quotes' has 0 steps; each box that is not an input or an output has one"
        in order
    )


def test_a_box_with_two_steps_fails_and_steps_out_of_order_fail():
    flow = _flow()
    flow["steps"].append({"node": "b", "title": "Find again", "summary": "Again."})
    assert any("'Find problems' has 2 steps" in r for r in _gate(flow)["order"])
    flow = _flow()
    flow["steps"].reverse()
    assert _gate(flow)["order"] == ["the steps are not in the order of the boxes"]


def test_a_change_flow_does_not_need_one_step_per_box():
    flow = _flow(steps=[{"title": "All of it", "summary": "It runs. [[c2]]"}])
    assert _gate(flow, follow=False)["order"] == []
    assert _gate(flow)["order"]


def test_a_fan_out_number_in_no_quote_and_no_snapshot_drops_the_fan_out_alone():
    flow = _flow()
    flow["nodes"][1]["fan_out"] = "7 aspect finders"
    g = _gate(flow)
    box = g["nodes"][1]
    assert box["label"] == "Find problems" and box["fan_out"] == ""
    assert g["cut"] == [
        {
            "text": "fan-out of box 'Find problems': '7 aspect finders' (the box is kept)",
            "reasons": ["`7` is in no cited quote and no snapshot"],
        }
    ]


def test_a_does_line_that_fails_the_text_checks_is_dropped_alone():
    flow = _flow()
    flow["nodes"][1]["does"] = "See engine/a.py:2 for it"
    flow["nodes"][2]["does"] = "x" * 81
    g = _gate(flow)
    assert [n["does"] for n in g["nodes"]] == ["", "", "", ""]
    assert len(g["nodes"]) == 4
    reasons = [r for c in g["cut"] for r in c["reasons"]]
    assert "engine/a.py:2 is a path with a line number typed in the text" in reasons
    assert "over 80 characters" in reasons


def test_the_picture_draws_one_element_per_box_with_its_kind():
    g = _gate(_flow())
    page = flow_picture(g)
    kinds = re.findall(r'<div class="fp-box [^"]*" data-kind="(\w+)"', page)
    assert kinds == ["input", "agent", "check", "output"]
    assert "<script" not in page and "mermaid" not in page
    # The inputs and the outputs each sit in their own end of the picture.
    assert (
        page.index('class="fp-in"')
        < page.index('class="fp-stage"')
        < page.index('class="fp-out"')
    )


def test_a_fan_out_box_is_drawn_as_stacked_cards_with_its_text():
    page = flow_picture(_gate(_flow()))
    stacked = re.findall(r'<div class="fp-box k-(\w+) fp-stack"', page)
    assert stacked == ["agent"]
    assert "&#215; 5 aspect finders" in page


def test_an_arrow_back_is_a_note_on_the_box_and_reads_and_writes_are_its_tooltip():
    page = flow_picture(_gate(_flow()))
    check = page.split('data-kind="check"', 1)[1].split("</div>", 1)[0]
    assert "back to Find problems" in check
    assert 'title="Reads: the diff. Writes: candidate findings"' in page


def test_the_inputs_are_drawn_first_whatever_their_order_in_the_answer():
    flow = _flow()
    flow["nodes"] = flow["nodes"][1:3] + flow["nodes"][3:] + flow["nodes"][:1]
    kinds = re.findall(r'data-kind="(\w+)"', flow_picture(_gate(flow)))
    assert kinds == ["input", "agent", "check", "output"]


def test_the_mermaid_has_one_shape_per_kind():
    flow = _flow()
    flow["nodes"].insert(
        3, {"id": "e", "label": "Repeat it", "cite": "c5", "kind": "decision"}
    )
    flow["nodes"].insert(4, {"id": "f", "label": "Save it", "cite": "c5"})
    flow["edges"] += [{"from": "c", "to": "e"}, {"from": "e", "to": "f"}]
    flow["steps"] += [
        {"node": "e", "title": "Repeat", "summary": "It repeats. [[c5]]"},
        {"node": "f", "title": "Save", "summary": "It saves. [[c5]]"},
    ]
    src = flow_mermaid(_gate(flow))
    assert 'f1(["The change"])' in src
    assert 'f2("Find problems ×5 aspect finders")' in src
    assert 'f3{{"Check the quotes"}}' in src
    assert 'f4{"Repeat it"}' in src
    assert 'f5["Save it"]' in src
    assert 'f6(["The findings file"])' in src


# --- through a run ------------------------------------------------------------------------------------


def test_a_run_draws_the_picture_with_one_box_per_kind_and_no_mermaid(done_run):
    md_path, _, _ = done_run
    html = md_path.with_suffix(".html").read_text()
    assert '<pre class="mermaid">' not in html
    flow = html.split('<section id="flow">', 1)[1].split("</section>", 1)[0]
    assert re.findall(r'data-kind="(\w+)"', flow) == ["input", "code", "agent"]
    md = md_path.read_text()
    diagram = re.search(r"```mermaid\n(.*?)```", md, re.DOTALL).group(1)
    assert 'f1(["Acme sends a booking"])' in diagram
    assert 'f3("The task acknowledges it")' in diagram


def test_steps_out_of_order_get_one_reask_naming_the_rule(
    ws, tmp_path, monkeypatch, fake_fetch
):
    _, _, agents = _run(ws, tmp_path, monkeypatch, plant={"flow_step_order"})
    asks = _calls(agents, "FlowOutput")
    assert len(asks) == 2
    assert "the steps are not in the order of the boxes" in asks[1]["prompt"]


def test_a_planted_fan_out_number_is_cut_and_the_box_kept(
    ws, tmp_path, monkeypatch, fake_fetch
):
    md, _, _ = _run(ws, tmp_path, monkeypatch, plant={"flow_fan_number"})
    assert "`987654` is in no cited quote and no snapshot" in _cut(md)
    flow = section_texts(md, "How it works")[0]
    assert "The task acknowledges it" in flow and "987654" not in flow
