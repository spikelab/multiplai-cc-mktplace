"""The page stage: one call writes the HTML page's open body, `page_gate` checks it, and code draws the
page around it. One test per rule of `page_gate`, then the re-ask, the page code draws when the writer
fails, the record of a body over the word limit, and `render` rebuilding the page with no call. No model
is called."""

from __future__ import annotations

import json
import re

import pytest
from fakes import FakeAgents
from test_codebase_scope import _args as codebase_args
from test_codebase_scope import pws  # noqa: F401  (the fixture)
from test_explainer import _Corpus
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.gates import css_gate, page_gate, page_words
from walkthrough_pipeline.models import Citation, PageOutput
from walkthrough_pipeline.prompts import PAGE_ICONS
from walkthrough_pipeline.stages.page import OPEN_WORDS_MAX
from walkthrough_pipeline.state import STATE_FILE, load_state

KEYS = ["summary", "flow", "outputs"]
CITES = {
    "c1": Citation(
        path="engine/a.py", line_start=1, line_end=1, quote="x = 1", id="c1"
    ),
    "c2": Citation(
        path="engine/a.py", line_start=2, line_end=2, quote="y = 2", id="c2"
    ),
}


def _page(css: str = "", **html) -> PageOutput:
    sections = {
        "summary": "<p>It reads a change. [[c1]]</p><p>{{flow}}</p>",
        "flow": "<h3>Read</h3><p>It reads the diff. [[c2]]</p>",
        "outputs": "<p>It writes one file. [[c1]]</p>",
        **html,
    }
    return PageOutput.model_validate(
        {"css": css, "sections": [{"key": k, "html": v} for k, v in sections.items()]}
    )


def _check(out: PageOutput, *, keys=KEYS, core_flow=False, corpus=()) -> list[str]:
    return page_gate(
        out,
        open_keys=keys,
        core_flow=core_flow,
        citations=CITES,
        corpus=_Corpus(set(corpus)),
        labels={"engine"},
    )


def test_a_plain_page_passes():
    assert _check(_page(".lead { color: var(--fg); }")) == []


def test_the_section_keys_must_be_the_open_keys_in_order():
    out = _page()
    out.sections.reverse()
    assert _check(out) == [
        (
            "the sections are ['outputs', 'flow', 'summary']; they must be exactly "
            "['summary', 'flow', 'outputs'], in that order"
        )
    ]
    assert _check(_page(), keys=[*KEYS, "gotchas"])


def test_the_flow_placeholder_must_be_in_the_summary_exactly_once():
    reason = "{{flow}} must appear exactly once, in the summary"
    assert reason in _check(_page(summary="<p>It reads a change. [[c1]]</p>"))
    assert reason in _check(_page(flow="<p>{{flow}}</p>"))
    twice = "<p>It reads. [[c1]]</p><p>{{flow}}</p><p>{{flow}}</p>"
    assert reason in _check(_page(summary=twice))


def test_the_core_flow_placeholder_needs_a_core_flow():
    out = _page(outputs="<p>{{flow:core}}</p>")
    assert _check(out) == ["{{flow:core}} is used, but the core section has no flow"]
    assert _check(out, core_flow=True) == []


def test_an_unknown_citation_or_snippet_id_fails():
    reasons = _check(_page(outputs="<p>It writes. [[c9]]</p><p>[[snippet:c8]]</p>"))
    assert "[[c9]] is not a gated citation" in reasons
    assert "[[c8]] is not a gated citation" in reasons


def test_a_citation_in_a_heading_or_a_summary_fails():
    reasons = _check(
        _page(
            flow="<h3>Read [[c2]]</h3><details><summary>More [[c1]]</summary><p>x</p></details>"
        )
    )
    assert "flow: a citation in a heading or summary: 'Read [[c2]]'" in reasons
    assert "flow: a citation in a heading or summary: 'More [[c1]]'" in reasons


def test_the_components_the_prompt_shows_pass_the_gate():
    stages = (
        '<ol class="stages"><li class="k-agent"><div><span class="badge"><svg class="ic">'
        '<use href="#i-agent"/></svg><span class="num">1</span></span></div><div class="stage-body">'
        '<p class="stage-kind">Model agent</p><h3>Read</h3><p>It reads the diff. [[c2]]</p>'
        '<div class="checkline"><svg class="ic"><use href="#i-check"/></svg><p><strong>Code checks '
        "the result.</strong> It drops a quote that is not there.</p></div></div></li></ol>"
    )
    cards = (
        '<p class="lead">It writes one file.</p><div class="cards"><div class="card k-output">'
        "<h3>The review</h3><p>One file. [[c1]]</p></div></div>"
        '<ul class="traps"><li><svg class="ic"><use href="#i-warn"/></svg><div><strong>Slow'
        "</strong> It reads every file.</div></li></ul>"
        '<details class="fold"><summary>Every file</summary><p>All of them.</p></details>'
    )
    assert _check(_page(flow=stages, outputs=cards)) == []


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        ("<script>alert(1)</script>", "outputs: the tag <script> is not allowed"),
        ("<h2>Mine</h2>", "outputs: the tag <h2> is not allowed"),
        ("<img src=x>", "outputs: the tag <img> is not allowed"),
        ('<p style="color: red">x</p>', "outputs: the attribute style is not allowed"),
        ('<p onclick="go()">x</p>', "outputs: the attribute onclick is not allowed"),
        (
            '<a href="https://evil.test/">x</a>',
            "outputs: href='https://evil.test/' on <a> is not a link inside the page",
        ),
        ('<p data-x="1">x</p>', "outputs: the attribute data-x on <p> is not allowed"),
        ("<!-- hidden -->", "outputs: an HTML comment is not allowed"),
    ],
)
def test_a_tag_or_attribute_outside_the_allow_list_fails(html, reason):
    assert reason in _check(_page(outputs=f"<p>It writes. [[c1]]</p>{html}"))


def test_svg_and_aria_and_in_page_links_pass():
    svg = (
        '<figure><svg viewBox="0 0 10 10" role="img" aria-label="a box"><rect x="1" y="1" '
        'width="8" height="8" fill="none" stroke="currentColor"/></svg><figcaption>A box</figcaption>'
        '</figure><p><a href="#flow">See the flow</a>.</p>'
    )
    assert _check(_page(outputs=f"<p>It writes. [[c1]]</p>{svg}")) == []


@pytest.mark.parametrize(
    ("css", "reason"),
    [
        (".a { background: url(x.png); }", "the CSS holds 'url('"),
        ('@import "x.css";', "the CSS holds '@import'"),
        (".a { width: expression(1); }", "the CSS holds 'expression('"),
        (".a { content: '</style>'; }", "the CSS holds '</'"),
        (
            ".a { color: #ff0000; }",
            "the CSS types the colour '#ff0000'; use var(--token)",
        ),
        (
            ".a { color: rgb(1, 2, 3); }",
            "the CSS types the colour 'rgb('; use var(--token)",
        ),
        (
            ".a { color: hsl(1, 2%, 3%); }",
            "the CSS types the colour 'hsl('; use var(--token)",
        ),
        ("} body { display: none; }", "the CSS braces do not balance"),
    ],
)
def test_css_that_loads_runs_types_a_colour_or_escapes_its_block_fails(css, reason):
    assert reason in css_gate(css)
    assert reason in _check(_page(css))


def test_a_code_text_must_be_in_the_snapshots():
    out = _page(outputs="<p>It calls <code>run_review</code>. [[c1]]</p>")
    assert _check(out) == ["outputs: `run_review` occurs in no snapshot"]
    assert _check(out, corpus={"run_review"}) == []


def test_a_typed_path_with_a_line_number_fails():
    out = _page(outputs="<p>See engine/a.py:2 for it. [[c1]]</p>")
    assert _check(out) == [
        "outputs: engine/a.py:2 is a path with a line number typed in the text"
    ]


def test_the_word_count_skips_fold_outs_placeholders_and_citations():
    html = (
        "<p>One two three. [[c1]]</p><p>{{flow}}</p><p>[[snippet:c2]]</p>"
        "<details><summary>Four five</summary><p>hidden hidden hidden</p></details>"
    )
    assert page_words(html) == 5


# --- through a run ------------------------------------------------------------------------------------


def _run(ws, tmp_path, monkeypatch, **fake):
    agents = FakeAgents(**fake)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    md_path = tmp_path / "out" / "bookings-walkthrough.md"
    return md_path, run_dir, agents


def _page_calls(agents: FakeAgents) -> list[dict]:
    return [c for c in agents.calls if c["schema"] == "PageOutput"]


def _section(html: str, key: str) -> str:
    return html.split(f'<section id="{key}">', 1)[1].split("</section>", 1)[0]


def test_a_run_asks_one_page_call_with_no_tools_and_the_design_tokens(done_run):
    _, run_dir, agents = done_run
    (call,) = _page_calls(agents)
    assert call["allowed_tools"] == [] and call["budget_label"] == "page"
    assert re.search(r"from this list: --[\w-]+", call["prompt"])
    assert '<untrusted-content source="page material">' in call["prompt"]
    keys = re.findall(r'^- "([a-z]+)" \(', call["prompt"], re.MULTILINE)
    assert keys == [
        "summary",
        "flow",
        "core",
        "outputs",
        "config",
        "usecases",
        "gotchas",
    ]
    state = load_state(run_dir / STATE_FILE)
    assert list(state.page_answers) == ["page"]
    assert list(state.page["sections"]) == keys
    assert 0 < state.page["words"] <= OPEN_WORDS_MAX


def test_the_page_shows_the_writer_html_with_its_placeholders_filled(done_run):
    md_path, run_dir, _ = done_run
    html = md_path.with_suffix(".html").read_text()
    summary = _section(html, "summary")
    assert '<div class="page-body">' in summary and '<figure class="diagram"' in summary
    assert "{{" not in html and "[[" not in html
    # The snippet the writer asked for, drawn from the commit.
    outputs = _section(html, "outputs")
    assert '<pre><code class="language-python">' in outputs
    # The writer's CSS, nested in the block of its sections.
    m = re.search(r"<style>\.page-body \{\n(.*?)\n\}</style>", html, re.DOTALL)
    assert m and re.fullmatch(r"\.lead \{ color: var\(--[\w-]+\); \}", m.group(1))
    # The fold-out the writer chose stays closed; its words are not counted.
    assert "<details><summary>Every setting</summary>" in _section(html, "config")
    made = _section(html, "made")
    words = load_state(run_dir / STATE_FILE).page["words"]
    assert (
        f"Open body of the HTML page: {words:,} words (limit {OPEN_WORDS_MAX:,})"
        in made
    )


def test_the_prompt_shows_the_components_and_the_header_line(done_run):
    _, _, agents = done_run
    (call,) = _page_calls(agents)
    prompt = call["prompt"]
    assert f"KIND is one of {', '.join(PAGE_ICONS)}" in prompt
    for cls in (
        'class="stages"',
        'class="checkline"',
        'class="cards"',
        'class="traps"',
    ):
        assert cls in prompt
    # No rule against a badge or an icon on every item: the stage cards carry one each.
    assert "becomes noise" not in prompt
    assert 'do not repeat it: "Bookings takes reservations from Acme."' in prompt


def test_the_page_carries_one_symbol_per_icon_and_the_component_styles(done_run):
    md_path, _, _ = done_run
    html = md_path.with_suffix(".html").read_text()
    for kind in PAGE_ICONS:
        assert html.count(f'<symbol id="i-{kind}"') == 1, kind
    for rule in (
        "ol.stages",
        ".badge",
        ".checkline",
        ".cards",
        "ul.traps",
        "details.fold",
    ):
        assert rule in html, rule


def test_every_cite_the_writer_page_shows_is_a_checked_citation(done_run):
    md_path, run_dir, _ = done_run
    html = md_path.with_suffix(".html").read_text()
    state = load_state(run_dir / STATE_FILE)
    where = {
        f"{c.path}:{c.line_start}"
        + (f"-{c.line_end}" if c.line_end != c.line_start else "")
        for c in state.citation_index().values()
    }
    for key in ("summary", "flow", "core", "outputs", "config", "usecases", "gotchas"):
        titles = re.findall(
            r'<a class="cite" href="[^"]*" title="([^"]*)"', _section(html, key)
        )
        assert titles, key
        assert all(t.split(" — ", 1)[0] in where for t in titles), key


@pytest.mark.parametrize(
    ("plant", "reason"),
    [
        ("page_script", "summary: the tag <script> is not allowed"),
        ("page_unknown_id", "[[c9999]] is not a gated citation"),
        ("page_colour", "the CSS types the colour '#ff0000'; use var(--token)"),
        ("page_heading_cite", "flow: a citation in a heading or summary"),
        ("page_no_flow", "{{flow}} must appear exactly once, in the summary"),
        ("page_long", "the open body is "),
    ],
)
def test_a_failing_page_gets_one_reask_naming_the_failure(
    ws, tmp_path, monkeypatch, fake_fetch, plant, reason
):
    _, run_dir, agents = _run(ws, tmp_path, monkeypatch, plant={plant})
    first, second = _page_calls(agents)
    assert "Your previous answer failed" not in first["prompt"]
    assert reason in second["prompt"]
    state = load_state(run_dir / STATE_FILE)
    assert list(state.page_answers) == ["page", "page:rewrite"]
    assert state.page and not [e for e in state.errors if e.startswith("page:")]


def test_a_page_that_fails_twice_is_drawn_by_code(
    ws, tmp_path, monkeypatch, fake_fetch
):
    md_path, run_dir, agents = _run(
        ws, tmp_path, monkeypatch, plant={"page_script:always"}
    )
    assert len(_page_calls(agents)) == 2
    state = load_state(run_dir / STATE_FILE)
    assert state.page == {}
    assert any(
        e.startswith("page: summary: the tag <script> is not allowed")
        for e in state.errors
    )
    html = md_path.with_suffix(".html").read_text()
    assert "<script>alert" not in html and 'class="page-body"' not in html
    assert ".page-body" not in html.split("</style>", 2)[1]
    # Code draws every section, with the diagram stage's picture in What it is.
    assert '<figure class="diagram"' in _section(html, "summary")
    assert "Open body of the HTML page" not in html


def test_a_failed_page_call_is_recorded_and_drawn_by_code(
    ws, tmp_path, monkeypatch, fake_fetch
):
    _, run_dir, agents = _run(
        ws, tmp_path, monkeypatch, fail_on="open body of a web page"
    )
    assert len(_page_calls(agents)) == 1
    state = load_state(run_dir / STATE_FILE)
    assert state.page == {} and state.page_answers == {
        "page": {"error": "planted failure"}
    }
    assert "page: planted failure" in state.errors


def test_a_page_over_the_limit_after_the_reask_is_kept_and_recorded(
    ws, tmp_path, monkeypatch, fake_fetch
):
    md_path, run_dir, agents = _run(
        ws, tmp_path, monkeypatch, plant={"page_long:always"}
    )
    assert len(_page_calls(agents)) == 2
    state = load_state(run_dir / STATE_FILE)
    words = state.page["words"]
    assert words > OPEN_WORDS_MAX
    assert f"page: open body {words} words, limit {OPEN_WORDS_MAX}" in state.errors
    md = md_path.read_text()
    assert (
        f"Open body of the HTML page: {words:,} words (limit {OPEN_WORDS_MAX:,})" in md
    )


def test_render_rebuilds_the_page_with_no_call(done_run, monkeypatch, capsys):
    md_path, run_dir, _ = done_run
    page = md_path.with_suffix(".html")
    before = page.read_text()
    page.write_text("edited by hand")
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    capsys.readouterr()
    assert main(["render", str(run_dir)]) == 0
    assert not agents.calls
    assert page.read_text() == before


def test_a_codebase_page_has_the_open_sections_then_the_closed_appendices(
    pws,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    # The diagram is not drawn, so the picture is the boxes code draws, one per box of the flow.
    agents = FakeAgents(plant={"diagram_script:always"})
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(codebase_args(pws, tmp_path)) == 0
    html = (tmp_path / "out" / "tool-walkthrough.html").read_text()
    order = re.findall(r'<section id="([a-z]+)">', html)
    assert order == [
        "summary",
        "flow",
        "core",
        "outputs",
        "config",
        "usecases",
        "gotchas",
        "loops",
        "exits",
        "resume",
        "reference",
        "made",
    ]
    for key, title in (
        ("loops", "Loops and caps"),
        ("exits", "Exit codes and markers"),
        ("resume", "State and resume"),
        ("reference", "Reference"),
        ("made", "How this was made"),
    ):
        assert _section(html, key).startswith(
            f'<details class="appendix"><summary><h2>{title}</h2></summary>'
        ), key
    for key in order[:7]:
        assert '<details class="appendix"' not in _section(html, key), key
    summary = _section(html, "summary")
    state = load_state(next((tmp_path / "runs").iterdir()) / STATE_FILE)
    kinds = [n["kind"] for n in state.sections["flow"].data["nodes"]]
    assert re.findall(r'data-kind="(\w+)"', summary) == kinds
    assert '<pre class="mermaid">' not in html
    assert json.loads((next((tmp_path / "runs").iterdir()) / STATE_FILE).read_text())[
        "page"
    ]
