"""The HTML page a human-mode run writes beside the Markdown: links, hop anchors, title, pinned scripts."""

from __future__ import annotations

import html
import re
import subprocess

import pytest

from fakes import FakeAgents
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import render_html


def _md_and_page(done_run) -> tuple[str, str]:
    md_path, _, _ = done_run
    return md_path.read_text(), md_path.with_suffix(".html").read_text()


@pytest.fixture
def github_run(fresh_ws, tmp_path, monkeypatch, fake_fetch):
    """`done_run`, with every repo's origin on GitHub, so citations are https links to the commit."""
    for key in ("engine", "front", "warehouse"):
        subprocess.run(["git", "-C", str(fresh_ws[key]), "remote", "add", "origin",
                        f"https://github.com/acme/{key}.git"], check=True, capture_output=True)
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(fresh_ws, tmp_path)) == 0
    return tmp_path / "out" / "bookings-walkthrough.md", next((tmp_path / "runs").iterdir()), agents


def test_every_https_link_in_the_markdown_is_an_href_in_the_page(github_run):
    md, page = _md_and_page(github_run)
    links = set(re.findall(r"\]\((https://[^\s)]+)", md))
    assert any(link.startswith("https://github.com/acme/engine/blob/") for link in links)
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
    assert main(["run", str(ws["engine"] / "bookings"), "--trust-repo", "--runs-dir", str(tmp_path / "runs"),
                 "--output", str(tmp_path / "out"), "--mode", "agent", "--lsp-timeout", "0"]) == 0
    assert not list((tmp_path / "out").glob("*.html"))


# --- render_html on its own ---------------------------------------------------------------


def test_a_citation_whose_title_holds_a_quote_or_a_bar_stays_one_link():
    url = "https://github.com/o/r/blob/abc/a.py#L3"
    title = 'says \\"hi\\" \\| `x`'                 # escaped the way render.Linker.link escapes a quote
    md = (f'## One\n\nSee [`engine/a.py:3`]({url} "{title}") here.\n\n'
          f'| a | b |\n|---|---|\n| x | [`engine/a.py:3`]({url} "{title}") |\n')
    page = render_html(md, "t")
    anchors = re.findall(r'<a href="([^"]*)" title="([^"]*)"><code>engine/a.py:3</code></a>', page)
    assert len(anchors) == 2
    assert {(html.unescape(h), html.unescape(t)) for h, t in anchors} == {(url, 'says "hi" | `x`')}
    assert len(re.findall(r"<td>", page)) == 2      # the bar in the title did not split the row


def test_a_placeholder_outside_backticks_shows_as_text_and_hop_anchors_stay():
    page = render_html('## One\n\nChanges live in changes/<name>/.\n\n- <a id="p1h1"></a>`p1h1` hop\n', "t")
    assert "changes/&lt;name&gt;/" in page
    assert '<a id="p1h1"></a>' in page


def test_mermaid_is_a_mermaid_pre_and_other_code_is_marked_for_highlight_js():
    page = render_html("## One\n\n```mermaid\ngraph LR\n  a-->b\n```\n\n```python\nx = 1\n```\n", "t")
    assert '<pre class="mermaid">graph LR\n  a--&gt;b\n</pre>' in page
    assert '<code class="language-python">' in page
    assert "MERMAIDBLOCK" not in page
