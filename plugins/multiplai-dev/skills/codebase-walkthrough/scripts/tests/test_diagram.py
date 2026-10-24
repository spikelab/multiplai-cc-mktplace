"""The diagram stage: a model draws the "How it works" picture as SVG, code renders it to PNG, a model looks
at the PNGs and corrects the SVG. One test per rule of `diagram_gate`, then the PNGs, the page's figure, the
stage's outcomes and the page that shows the picture. No model is called."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from test_explainer import _Corpus
from walkthrough_pipeline import design
from walkthrough_pipeline.diagram import (
    DiagramUnavailable,
    page_figure,
    render_pngs,
    standalone,
)
from walkthrough_pipeline.gates import diagram_gate
from walkthrough_pipeline.models import DiagramOutput

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
    assert 'class="dg-tall wd-root" role="img" aria-label="Six steps."' in roots[1]
