"""The diagram stage: a model draws the "How it works" picture as SVG, code renders it to PNG, a model looks
at the PNGs and corrects the SVG. One test per rule of `diagram_gate`, then the PNGs, the page's figure, the
stage's outcomes and the page that shows the picture. No model is called."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fakes import FakeAgents
from test_change_scope import _args as change_args
from test_change_scope import change  # noqa: F401  (the fixture)
from test_codebase_scope import _args as codebase_args
from test_codebase_scope import pws  # noqa: F401  (the fixture)
from test_explainer import _Corpus
from test_pipeline import run_args
from walkthrough_pipeline import design, sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.diagram import (
    DiagramUnavailable,
    page_figure,
    render_pngs,
    standalone,
)
from walkthrough_pipeline.gates import diagram_gate
from walkthrough_pipeline.models import DiagramOutput
from walkthrough_pipeline.stages.diagram import UNAVAILABLE
from walkthrough_pipeline.state import STATE_FILE, load_state

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
WIDE = (TEMPLATES / "diagram-example-wide.svg").read_text()
TALL = (TEMPLATES / "diagram-example-tall.svg").read_text()
CSS = (TEMPLATES / "diagram-example.css").read_text()
BOXES = [
    "Input",
    "Copy the code",
    "Plan the work",
    "Finders × N",
    "quotes match the code?",
    "Check each result",
    "Write the files",
    "Decide what to keep",
]
PNG = b"\x89PNG\r\n\x1a\n"


def _out(wide: str = WIDE, tall: str = TALL, css: str = CSS) -> DiagramOutput:
    return DiagramOutput(wide=wide, tall=tall, css=css, alt="How it works.")


def _check(out: DiagramOutput, *, labels=(), corpus=(), boxes=BOXES) -> list[str]:
    return diagram_gate(
        out, labels=set(labels), corpus=_Corpus(set(corpus)), box_labels=boxes
    )


def _wide(old: str, new: str) -> DiagramOutput:
    assert old in WIDE, old
    return _out(wide=WIDE.replace(old, new, 1))


def _tokens() -> str:
    return design.css(None)[1]


# --- diagram_gate: one test per rule ---------------------------------------------------------------------


def test_the_shipped_example_passes_with_its_own_labels():
    assert _check(_out()) == []


def test_each_svg_must_be_there():
    assert "tall: the SVG is empty" in _check(_out(tall=""))


def test_each_svg_is_at_most_40000_characters():
    long = WIDE.replace("</svg>", "<g>" + " " * 40_000 + "</g></svg>")
    assert any("characters; at most 40,000" in r for r in _check(_out(wide=long)))


def test_each_svg_is_exactly_one_root_svg():
    reason = "wide: it must be exactly one root <svg> element"
    assert reason in _check(_out(wide=WIDE + WIDE))
    assert reason in _check(_out(wide="Here it is: " + WIDE))
    assert reason in _check(_out(wide='<g class="wd-root">' + WIDE + "</g>"))


def test_the_view_box_width_is_1000_wide_and_420_tall():
    assert any(
        'needs viewBox="0 0 1000 H"' in r
        for r in _check(_wide('viewBox="0 0 1000 620"', 'viewBox="0 0 900 620"'))
    )
    assert any(
        'needs viewBox="0 0 420 H"' in r
        for r in _check(_out(tall=TALL.replace("0 0 420 1190", "0 0 1000 1190")))
    )
    assert any("viewBox" in r for r in _check(_wide('viewBox="0 0 1000 620"', "")))


@pytest.mark.parametrize(
    ("planted", "reason"),
    [
        ("<script>alert(1)</script>", "wide: the tag <script> is not allowed"),
        (
            "<foreignObject><p>x</p></foreignObject>",
            "wide: the tag <foreignobject> is not allowed",
        ),
        ('<image href="#ah"/>', "wide: the tag <image> is not allowed"),
        ("<style>.wd-a{}</style>", "wide: the tag <style> is not allowed"),
        ("<!-- a note -->", "wide: a comment is not allowed"),
    ],
)
def test_only_the_allowed_tags_and_no_comment(planted, reason):
    assert reason in _check(_wide("</defs>", "</defs>" + planted))


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            '<rect x="10" y="80"',
            '<rect style="fill:red" x="10" y="80"',
            "wide: the attribute style is not allowed",
        ),
        (
            '<rect x="10" y="80"',
            '<rect onclick="go()" x="10" y="80"',
            "wide: the attribute onclick is not allowed",
        ),
        (
            '<rect x="10" y="80"',
            '<rect filter="blur" x="10" y="80"',
            "wide: the attribute filter on <rect> is not allowed",
        ),
        (
            'href="#i-input"',
            'href="https://example.com/x.svg"',
            "wide: href='https://example.com/x.svg' is not a reference inside the SVG",
        ),
        (
            'd="M160 170 H182" marker-end="url(#ah)"',
            'd="M160 170 H182" marker-end="#ah"',
            "wide: marker-end='#ah' is not url(#id)",
        ),
        (
            '<rect x="10" y="80"',
            '<rect fill="#ff0000" x="10" y="80"',
            "wide: fill='#ff0000' on <rect>: a colour goes in the CSS as var(--token)",
        ),
    ],
)
def test_only_the_allowed_attributes(old, new, reason):
    assert reason in _check(_wide(old, new))


def test_fill_none_and_current_colour_pass():
    assert _check(_wide('<rect x="10" y="80"', '<rect fill="none" x="10" y="80"')) == []
    assert (
        _check(
            _wide('<rect x="10" y="80"', '<rect stroke="currentColor" x="10" y="80"')
        )
        == []
    )


def test_ids_are_lower_case_and_unique_and_every_reference_resolves():
    upper = _wide('<marker id="ah-bad"', '<marker id="Ah-bad"')
    assert "wide: the id 'Ah-bad' is not lower-case letters, digits and -" in _check(
        upper
    )
    twice = _wide('<marker id="ah-bad"', '<marker id="ah"')
    assert "wide: the id 'ah' is used more than once" in _check(twice)
    missing = _wide('marker-end="url(#ah-loop)"', 'marker-end="url(#nowhere)"')
    assert "wide: #nowhere points at no id of this SVG and no icon" in _check(missing)
    icon = _wide('href="#i-input"', 'href="#i-sparkle"')
    assert "wide: #i-sparkle points at no id of this SVG and no icon" in _check(icon)


def test_a_doctype_and_a_malformed_svg_fail():
    assert any(
        "<!DOCTYPE svg> is not allowed" in r
        for r in _check(_out(wide="<!DOCTYPE svg>" + WIDE))
    )
    broken = _wide('<text class="wd-title" x="22" y="132">Input</text>', "<text>Input")
    assert any("not well-formed XML" in r for r in _check(broken))


def test_no_path_with_a_line_number_in_the_text():
    out = _wide(">a read-only copy<", ">engine/a.py:12<")
    assert (
        "wide: engine/a.py:12 is a path with a line number typed in the text"
        in _check(out, labels={"engine/a.py"})
    )


def test_a_word_that_looks_like_code_must_be_a_label_or_in_the_corpus():
    out = _wide(">aspect one<", ">run_finders()<")
    reason = "wide: 'run_finders()' looks like code and occurs in no snapshot"
    assert reason in _check(out)
    assert reason not in _check(out, corpus={"run_finders"})
    assert reason not in _check(out, labels={"run_finders()"})
    camel = _wide(">aspect one<", ">readTheDiff<")
    assert "wide: 'readTheDiff' looks like code and occurs in no snapshot" in _check(
        camel
    )
    # Ordinary words, numbers and sentence punctuation are not code.
    plain = _wide(">aspect one<", ">one, two (and 3.5) more.<")
    assert _check(plain) == []


def test_every_box_label_is_in_both_svgs():
    assert (
        "wide: the box label 'Read the queue' is not in the picture's text"
        in _check(_out(), boxes=[*BOXES, "Read the queue"])
    )
    no_title = TALL.replace(">Plan the work<", ">Plan<")
    assert "tall: the box label 'Plan the work' is not in the picture's text" in _check(
        _out(tall=no_title)
    )


def test_the_css_passes_css_gate_and_styles_only_wd_classes():
    assert "the CSS types the colour '#123456'; use var(--token)" in _check(
        _out(css=CSS + "\n.wd-x { fill: #123456; }")
    )
    assert "the CSS selector 'body' does not start with .wd-" in _check(
        _out(css=CSS + "\nbody { margin: 0; }")
    )
    assert "the CSS selector '.page-body p' does not start with .wd-" in _check(
        _out(css=CSS + "\n.wd-x, .page-body p { margin: 0; }")
    )
    assert any(
        "does not start with .wd-" in r
        for r in _check(_out(css=CSS + "\n@media (max-width: 9px) { .wd-x { x: 0 } }"))
    )


# --- the PNGs and the page's figure ----------------------------------------------------------------------


def test_render_pngs_writes_three_pngs(tmp_path):
    paths = render_pngs(_out(), _tokens(), tmp_path / "diagram", 2)
    assert [p.name for p in paths] == [
        "round-2-light-wide.png",
        "round-2-dark-wide.png",
        "round-2-light-tall.png",
    ]
    for p in paths:
        assert p.read_bytes().startswith(PNG)
    assert paths[0].read_bytes() != paths[1].read_bytes()


def test_standalone_inlines_the_tokens_the_icons_and_a_background():
    light = standalone(WIDE, CSS, _tokens(), "light", 1600)
    dark = standalone(WIDE, CSS, _tokens(), "dark", 1600)
    for svg in (light, dark):
        assert "var(" not in svg
        assert '<symbol id="i-agent"' in svg and '<symbol id="i-check"' in svg
        assert 'width="1600" height="992"' in svg
        assert re.search(r"\.wd-title \{ font-family:\s*sans-serif;", svg)
    background = re.compile(
        r'<rect x="0" y="0" width="1000" height="620" fill="([^"]+)"/>'
    )
    assert background.search(light).group(1) != background.search(dark).group(1)


def test_standalone_restores_the_case_an_xml_renderer_needs():
    svg = standalone(
        WIDE.replace("viewBox", "viewbox").replace("refX", "refx"),
        CSS,
        _tokens(),
        "light",
        800,
    )
    assert 'viewBox="0 0 1000 620"' in svg and "refX=" in svg and "refx=" not in svg


def test_render_pngs_raises_diagram_unavailable_without_resvg(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "resvg_py", None)
    with pytest.raises(DiagramUnavailable):
        render_pngs(_out(), _tokens(), tmp_path, 1)
    assert not list(tmp_path.iterdir())


def test_page_figure_makes_every_id_unique_and_keeps_its_references():
    fig = page_figure(
        {"wide": WIDE, "tall": TALL, "css": CSS, "alt": "Six steps."},
        caption="How it works",
    )
    assert fig.startswith('<figure class="diagram" aria-label="How it works">')
    ids = re.findall(r'\sid="([^"]+)"', fig)
    assert len(ids) == len(set(ids)) == 6
    assert set(ids) == {
        "dw-ah",
        "dw-ah-bad",
        "dw-ah-loop",
        "dt-ah",
        "dt-ah-bad",
        "dt-ah-loop",
    }
    for ref in re.findall(r"url\(#([^)]+)\)", fig):
        assert ref in ids
    assert set(re.findall(r'href="#([^"]+)"', fig)) <= {
        "i-input",
        "i-code",
        "i-agent",
        "i-check",
        "i-output",
        "i-decision",
    }
    roots = re.findall(r"<svg\b[^>]*>", fig)
    assert len(roots) == 2
    assert 'class="dg-wide wd-root" role="img" aria-label="Six steps."' in roots[0]
    assert (
        'class="dg-tall wd-root wd-tall" role="img" aria-label="Six steps."' in roots[1]
    )


# --- the stage ---------------------------------------------------------------------------------------------


def _run(ws, tmp_path, monkeypatch, *extra, **fake):
    """A module run with FakeAgents(**fake): (state, html, agents)."""
    agents = FakeAgents(**fake)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, *extra)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    html = (tmp_path / "out" / "bookings-walkthrough.html").read_text()
    return load_state(run_dir / STATE_FILE), html, agents


def _diagram_calls(agents: FakeAgents) -> list[dict]:
    return [c for c in agents.calls if c["schema"] in ("DiagramOutput", "DiagramLook")]


def _schemas(agents: FakeAgents) -> list[str]:
    return [c["schema"] for c in _diagram_calls(agents)]


def _summary(html: str) -> str:
    return html.split('<section id="summary">', 1)[1].split("</section>", 1)[0]


def _errors(state) -> list[str]:
    return [e for e in state.errors if e.startswith("diagram:")]


def test_ok_on_the_first_look_puts_the_drawn_picture_on_the_page(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, html, agents = _run(ws, tmp_path, monkeypatch)
    assert _schemas(agents) == ["DiagramOutput", "DiagramLook"]
    draw, look = _diagram_calls(agents)
    assert draw["allowed_tools"] == [] and draw["budget_label"] == "diagram"
    assert '<untrusted-content source="diagram material">' in draw["prompt"]
    assert (
        "diagram-example" not in draw["prompt"] and "AGENTS · AT ONCE" in draw["prompt"]
    )
    folder = next((tmp_path / "runs").iterdir()) / "diagram"
    assert look["allowed_tools"] == ["Read"] and look["read_roots"] == [str(folder)]
    assert sorted(p.name for p in folder.iterdir()) == [
        "round-1-dark-wide.png",
        "round-1-light-tall.png",
        "round-1-light-wide.png",
    ]
    for p in folder.iterdir():
        assert f"- {p}" in look["prompt"]
    assert state.diagram["approved"] is True and state.diagram["looks"] == 1
    assert state.diagram["wide"] == state.diagram_answers["draw"]["wide"]
    assert list(state.diagram_answers) == ["draw", "look:1"]
    assert _errors(state) == []
    summary = _summary(html)
    assert '<figure class="diagram"' in summary and 'class="flowpic"' not in summary
    assert 'class="dg-wide wd-root" role="img"' in summary
    # The fold-out of what each stage reads and writes stays under the picture.
    assert summary.index('<figure class="diagram"') < summary.index(
        "What each stage reads"
    )
    assert "figure.diagram {\n.wd-root { fill: var(--fg); }" in html


def test_a_fix_that_passes_the_gate_is_used_and_looked_at_again(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, html, agents = _run(ws, tmp_path, monkeypatch, look_plan={1: "fix"})
    assert _schemas(agents) == ["DiagramOutput", "DiagramLook", "DiagramLook"]
    assert "wd-round-1" in state.diagram["wide"] and "wd-round-1" in html
    assert state.diagram["looks"] == 2 and state.diagram["approved"] is True
    assert _errors(state) == []


def test_a_fix_that_fails_the_gate_keeps_the_picture_and_tells_the_next_look(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, _, agents = _run(ws, tmp_path, monkeypatch, look_plan={1: "fix_bad"})
    assert _schemas(agents) == ["DiagramOutput", "DiagramLook", "DiagramLook"]
    assert "wd-round-1" not in state.diagram["wide"]
    assert state.diagram["wide"] == state.diagram_answers["draw"]["wide"]
    second = _diagram_calls(agents)[2]["prompt"]
    assert "wide: the tag <image> is not allowed" in second
    assert state.diagram["looks"] == 2 and state.diagram["approved"] is True


def test_a_failed_look_ends_the_loop_and_keeps_the_last_version(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, html, agents = _run(
        ws, tmp_path, monkeypatch, look_plan={1: "fix"}, fail_on="This is look 2 of"
    )
    assert _schemas(agents) == ["DiagramOutput", "DiagramLook", "DiagramLook"]
    assert "wd-round-1" in state.diagram["wide"]
    assert state.diagram["looks"] == 1 and state.diagram["approved"] is False
    assert state.diagram_answers["look:2"] == {"error": "planted failure"}
    assert _errors(state) == ["diagram: look 2: planted failure"]
    assert '<figure class="diagram"' in html


def test_a_picture_never_approved_is_used_after_three_looks(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, html, agents = _run(
        ws, tmp_path, monkeypatch, look_plan={1: "fix", 2: "fix", 3: "fix"}
    )
    assert _schemas(agents) == ["DiagramOutput", *["DiagramLook"] * 3]
    assert "wd-round-3" in state.diagram["wide"]
    assert state.diagram["looks"] == 3 and state.diagram["approved"] is False
    assert _errors(state) == ["diagram: not approved after 3 looks"]
    assert "wd-round-3" in html


def test_a_draw_that_fails_twice_leaves_the_picture_code_draws(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, html, agents = _run(
        ws, tmp_path, monkeypatch, plant={"diagram_script:always"}
    )
    assert _schemas(agents) == ["DiagramOutput", "DiagramOutput"]
    assert (
        "wide: the tag <script> is not allowed" in _diagram_calls(agents)[1]["prompt"]
    )
    assert list(state.diagram_answers) == ["draw", "draw:rewrite"]
    assert state.diagram == {}
    (error,) = _errors(state)
    assert error.startswith("diagram: wide: the tag <script> is not allowed")
    assert 'class="flowpic"' in _summary(html) and "<script>alert" not in html
    assert '<figure class="diagram"' not in html and "figure.diagram {\n" not in html


def test_a_draw_that_passes_on_the_reask_is_looked_at(
    ws, tmp_path, monkeypatch, fake_fetch
):
    state, _, agents = _run(ws, tmp_path, monkeypatch, plant={"diagram_script"})
    assert _schemas(agents) == ["DiagramOutput", "DiagramOutput", "DiagramLook"]
    assert state.diagram["wide"] == state.diagram_answers["draw:rewrite"]["wide"]
    assert _errors(state) == []


def test_without_resvg_no_look_is_made_and_code_draws_the_picture(
    ws, tmp_path, monkeypatch, fake_fetch
):
    monkeypatch.setitem(sys.modules, "resvg_py", None)
    state, html, agents = _run(ws, tmp_path, monkeypatch)
    assert _schemas(agents) == ["DiagramOutput"]
    assert state.diagram == {} and _errors(state) == [UNAVAILABLE]
    assert 'class="flowpic"' in _summary(html)


def test_agent_mode_makes_no_diagram_call(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, "--mode", "agent")) == 0
    state = load_state(next((tmp_path / "runs").iterdir()) / STATE_FILE)
    assert _diagram_calls(agents) == [] and state.diagram == {}


def test_a_change_makes_no_diagram_call(change, tmp_path, monkeypatch):  # noqa: F811
    ws, base, head = change
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(change_args(ws, tmp_path, "--range", f"{base}..{head}")) == 0
    state = load_state(next((tmp_path / "runs").iterdir()) / STATE_FILE)
    assert _diagram_calls(agents) == [] and state.diagram == {}


def test_a_codebase_page_shows_the_drawn_picture(pws, tmp_path, monkeypatch):  # noqa: F811
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(codebase_args(pws, tmp_path)) == 0
    html = (tmp_path / "out" / "tool-walkthrough.html").read_text()
    assert _schemas(agents) == ["DiagramOutput", "DiagramLook"]
    assert '<figure class="diagram"' in _summary(html)


def test_render_draws_the_page_from_the_saved_picture_with_no_call(
    done_run, tmp_path, monkeypatch
):
    md_path, run_dir, _ = done_run
    html = md_path.with_suffix(".html").read_text()
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(["render", str(run_dir), "--output", str(tmp_path / "again")]) == 0
    again = (tmp_path / "again" / md_path.name).with_suffix(".html").read_text()
    assert not agents.calls
    assert '<figure class="diagram"' in again
    strip = re.compile(r'<p class="run">.*?</p>|href="[^"]*"')
    assert strip.sub("", again) == strip.sub("", html)
