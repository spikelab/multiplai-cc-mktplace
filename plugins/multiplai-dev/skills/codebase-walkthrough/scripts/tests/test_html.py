"""The HTML page a human-mode run writes beside the Markdown: links, hop anchors, title, pinned scripts,
one header, citations as numbered superscripts, and Reference and How this was made behind <details>."""

from __future__ import annotations

import html
import re
import subprocess

import pytest
from fakes import FakeAgents
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import markdown_html


def _md_and_page(done_run) -> tuple[str, str]:
    md_path, _, _ = done_run
    return md_path.read_text(), md_path.with_suffix(".html").read_text()


@pytest.fixture
def github_run(fresh_ws, tmp_path, monkeypatch, fake_fetch):
    """`done_run`, with every repo's origin on GitHub, so citations are https links to the commit."""
    for key in ("engine", "front", "warehouse"):
        subprocess.run(
            [
                "git",
                "-C",
                str(fresh_ws[key]),
                "remote",
                "add",
                "origin",
                f"https://github.com/acme/{key}.git",
            ],
            check=True,
            capture_output=True,
        )
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(fresh_ws, tmp_path)) == 0
    return (
        tmp_path / "out" / "bookings-walkthrough.md",
        next((tmp_path / "runs").iterdir()),
        agents,
    )


def test_every_https_link_in_the_markdown_is_an_href_in_the_page(github_run):
    md, page = _md_and_page(github_run)
    links = set(re.findall(r"\]\((https://[^\s)]+)", md))
    assert any(
        link.startswith("https://github.com/acme/engine/blob/") for link in links
    )
    hrefs = {html.unescape(h) for h in re.findall(r'href="([^"]*)"', page)}
    assert links <= hrefs, sorted(links - hrefs)[:5]


def test_every_hop_anchor_in_the_markdown_is_in_the_page(done_run):
    md, page = _md_and_page(done_run)
    ids = set(re.findall(r'id="(p\d+h\d+)"', md))
    assert ids
    assert ids <= set(re.findall(r'id="(p\d+h\d+)"', page))


def test_the_page_has_one_title_inside_its_first_8_kb(done_run):
    _, page = _md_and_page(done_run)
    assert page.startswith("<!doctype html>")
    assert page.count("<title>") == 1
    assert b"<title>bookings walkthrough</title>" in page.encode("utf-8")[:8192]


def test_every_script_is_a_pinned_cdnjs_url(done_run):
    _, page = _md_and_page(done_run)
    srcs = re.findall(r'<script src="([^"]+)"', page)
    assert srcs
    for src in srcs:
        assert src.startswith("https://cdnjs.cloudflare.com/ajax/libs/"), src
        assert re.search(r"/\d+\.\d+\.\d+/", src), src


def test_agent_mode_writes_no_html(ws, tmp_path, monkeypatch, fake_fetch):
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert (
        main(
            [
                "run",
                str(ws["engine"] / "bookings"),
                "--trust-repo",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--output",
                str(tmp_path / "out"),
                "--mode",
                "agent",
                "--lsp-timeout",
                "0",
            ]
        )
        == 0
    )
    assert not list((tmp_path / "out").glob("*.html"))


def visible_text(page: str) -> str:
    """What a reader sees: the body without scripts, styles and tags, entities decoded."""
    body = page[page.index("<body>") :]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


PATH_LINE_TEXT = re.compile(r"[\w.-]+\.\w+:\d+")


def test_no_path_with_a_line_number_is_in_the_visible_text(github_run):
    _, page = _md_and_page(github_run)
    assert not PATH_LINE_TEXT.findall(visible_text(page))
    titles = [
        html.unescape(t)
        for t in re.findall(r'<a class="cite" href="[^"]*" title="([^"]*)"', page)
    ]
    assert titles and all(
        PATH_LINE_TEXT.search(t) for t in titles
    )  # every location is in a cite's title


def test_a_citation_is_a_numbered_superscript_numbered_per_section(github_run):
    md, page = _md_and_page(github_run)
    sups = re.findall(
        r'<sup><a class="cite" href="([^"]+)" title="[^"]+">(\d+)</a></sup>', page
    )
    assert sups
    for section in re.findall(r'<section id="[a-z]+">(.*?)</section>', page, re.DOTALL):
        numbers = [
            int(n) for n in re.findall(r'<a class="cite"[^>]*>(\d+)</a>', section)
        ]
        if numbers and '<details class="sub"' not in section:
            assert min(numbers) == 1 and sorted(set(numbers)) == list(
                range(1, max(numbers) + 1)
            )


def test_the_header_appears_once(done_run):
    _, page = _md_and_page(done_run)
    assert page.count("<header") == 1 and page.count("<h1>") == 1
    assert page.count("Every citation in this file was checked") == 1
    assert '<nav class="pager">' not in page and 'class="step"' not in page


def test_reference_and_how_this_was_made_are_closed_details(done_run):
    md, page = _md_and_page(done_run)
    for key, titles in (
        (
            "reference",
            [
                "Paths",
                "Module tour",
                "Who calls it and what it calls",
                "Data stores",
                "Vendor docs",
                "Glossary",
                "Questions for the owner",
            ],
        ),
        ("made", ["Commits read", "Coverage", "Cut by the checks"]),
    ):
        section = re.search(
            rf'<section id="{key}">(.*?)</section>', page, re.DOTALL
        ).group(1)
        found = re.findall(
            r'<details class="sub" id="[^"]+"><summary><h3>(.*?)</h3></summary>',
            section,
        )
        assert found == titles
        assert "<details open" not in section
        assert section.count("<h3>") == len(titles)  # every H3 is a details' summary


def test_hop_anchors_are_inside_the_paths_details(done_run):
    _, page = _md_and_page(done_run)
    paths = re.search(
        r'<details class="sub" id="reference-paths">(.*?)</details>', page, re.DOTALL
    ).group(1)
    assert '<a id="p1h1"></a>' in paths
    assert 'href="#p1h1"' in page


def test_payload_examples_are_closed_details(done_run):
    _, page = _md_and_page(done_run)
    outputs = re.search(
        r'<section id="outputs">(.*?)</section>', page, re.DOTALL
    ).group(1)
    assert re.search(
        r'<details class="more"><summary>Example payload \(json\)</summary><pre><code '
        r'class="language-json">',
        outputs,
    )


# --- markdown_html on its own -------------------------------------------------------------


def test_a_citation_whose_title_holds_a_quote_or_a_bar_stays_one_link():
    url = "https://github.com/o/r/blob/abc/a.py#L3"
    title = (
        'says \\"hi\\" \\| `x`'  # escaped the way render.Linker.link escapes a quote
    )
    md = (
        f'See [`engine/a.py:3`]({url} "{title}") here.\n\n'
        f'| a | b |\n|---|---|\n| x | [`engine/a.py:3`]({url} "{title}") |\n'
    )
    page = markdown_html(md)
    anchors = re.findall(
        r'<sup><a class="cite" href="([^"]*)" title="([^"]*)">(\d+)</a></sup>', page
    )
    assert len(anchors) == 2
    assert {(html.unescape(h), html.unescape(t), n) for h, t, n in anchors} == {
        (url, 'engine/a.py:3 — says "hi" | `x`', "1")
    }  # the same link keeps its number
    assert (
        len(re.findall(r"<td>", page)) == 2
    )  # the bar in the title did not split the row


def test_a_placeholder_outside_backticks_shows_as_text_and_hop_anchors_stay():
    page = markdown_html(
        'Changes live in changes/<name>/.\n\n- <a id="p1h1"></a>`p1h1` hop\n'
    )
    assert "changes/&lt;name&gt;/" in page
    assert '<a id="p1h1"></a>' in page


def test_mermaid_is_a_mermaid_pre_and_other_code_is_marked_for_highlight_js():
    page = markdown_html(
        "```mermaid\ngraph LR\n  a-->b\n```\n\n```python\nx = 1\n```\n"
    )
    assert '<pre class="mermaid">graph LR\n  a--&gt;b\n</pre>' in page
    assert '<code class="language-python">' in page
    assert "MERMAIDBLOCK" not in page


def test_a_hop_pointer_is_an_arrow_not_its_id():
    page = markdown_html('It starts here [`e/a.py:3`](u "q") ([p2h3](#p2h3)).')
    assert '<a class="hop" href="#p2h3"' in page and ">p2h3<" not in page


def _linked(run_dir, text: str) -> str:
    """*text* through the page's citation rendering: links, numbered superscripts, then grouping."""
    from walkthrough_pipeline.render import (
        CiteNumbers,
        Linker,
        group_cites,
        markdown_html,
    )
    from walkthrough_pipeline.state import STATE_FILE, load_state

    lk = Linker(load_state(run_dir / STATE_FILE), run_dir)
    return group_cites(markdown_html(lk.expand(text), CiteNumbers()))


def test_adjacent_citations_are_one_superscript_reading_1_2(done_run):
    _, run_dir, _ = done_run
    out = _linked(run_dir, "a [[c1]] [[c2]]")
    assert out.count("<sup>") == 1
    sup = re.search(r"<sup>(.*?)</sup>", out).group(1)
    assert re.sub(r"<[^>]+>", "", sup) == "1, 2"


def test_the_sparse_pass_keeps_one_citation_under_each_h3(done_run):
    from walkthrough_pipeline.render import first_cites_only

    _, run_dir, _ = done_run
    body = (
        "<h3>One</h3>"
        + _linked(run_dir, "It reads. [[c1]]\n\nIt checks. [[c2]] [[c3]]")
        + "<h3>Two</h3>"
        + _linked(run_dir, "It writes. [[c3]]")
    )
    assert len(re.findall(r'class="cite"', body)) == 4
    out = first_cites_only(body)
    one, two = out.split("<h3>Two</h3>")
    assert len(re.findall(r'class="cite"', one)) == 1
    assert len(re.findall(r'class="cite"', two)) == 1
    # Numbered again in the order shown.
    assert re.findall(r'class="cite"[^>]*>(\d+)<', out) == ["1", "2"]
    assert "It checks." in one and "It writes." in two


def test_the_page_groups_adjacent_citations(done_run):
    md_path, _, _ = done_run
    page = md_path.with_suffix(".html").read_text()
    assert not re.search(r'</a></sup>\s*<sup><a class="cite"', page)
