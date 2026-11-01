"""`check`, `new` and `render` on small pages. No network, no model."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from explainer_check import Options, check_page, parse_page
from explainer_check.__main__ import SHELL, main, new_page

NEUTRAL = Path(__file__).resolve().parents[3] / "design-system" / "assets" / "neutral"

TOKENS = """:root{--bg:#ffffff;--surface:#ffffff;--fg:#111111;--accent:#1f5fc4;--line:#cccccc}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#000000;--surface:#111111;--fg:#eeeeee}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#000000;--surface:#111111;--fg:#eeeeee}"""

SVG = (
    '<svg viewBox="0 0 960 120" role="img" aria-label="Two stages in a row">'
    '<defs><marker id="ah-w" viewBox="0 0 10 10" refX="10" refY="5"><path class="ah" d="M0 0L10 5L0 10z"/></marker></defs>'
    '<rect class="node" x="10" y="10" width="200" height="60" rx="6"/>'
    '<text class="lbl" x="30" y="45">Tag a release</text>'
    '<path class="edge" d="M210 40H400" marker-end="url(#ah-w)"/>'
    '<rect x="400" y="10" width="200" height="60" fill="var(--surface)" stroke="currentColor"/>'
    "</svg>"
)


def page(
    *,
    title="Release Pipeline",
    body="<p>One sentence.</p>",
    head_extra="",
    tokens=TOKENS,
    css="body{background:var(--bg);color:var(--fg)}",
):
    return (
        f"<!doctype html><html><head><title>{title}</title>"
        f'<style id="tokens">{tokens}</style><style id="components">{css}</style>{head_extra}</head>'
        f'<body><main><h1>What happens on release?</h1><p class="lede">It ships.</p>{body}</main></body></html>'
    )


def reasons(src: str, **opts) -> list[str]:
    return check_page(parse_page(src), Options(**opts))


def test_a_good_page_passes():
    assert reasons(page(body=f'<figure class="fig">{SVG}</figure>')) == []


def test_words_inside_a_closed_fold_out_are_not_counted_but_its_summary_is():
    words = " ".join(["word"] * 50)
    src = page(
        body=f'<details class="more"><summary>Two words</summary><p>{words}</p></details>'
    )
    p = parse_page(src)
    # "What happens on release?" (4) + "It ships." (2) + "Two words" (2)
    assert p.open_words == 8
    assert reasons(src) == []


def test_too_many_open_words_fails():
    src = page(body="<p>" + " ".join(["word"] * 30) + "</p>")
    got = reasons(src, max_words=20)
    assert len(got) == 1 and "words are shown before any fold-out" in got[0]


@pytest.mark.parametrize(
    "title", ["Release", "A title that is far too long"], ids=["one", "seven"]
)
def test_title_must_be_two_to_four_words(title):
    assert any("<title>" in r for r in reasons(page(title=title)))


def test_exactly_one_h1():
    assert any("<h1>" in r for r in reasons(page(body="<h1>Again</h1>")))


def test_typed_colour_in_a_style_block_fails():
    got = reasons(
        page(css="body{color:#ff0000} .a{background:rgb(1,2,3)} .b{fill:red}")
    )
    assert any("#ff0000" in r for r in got)
    assert any("rgb(" in r for r in got)
    assert any("red" in r for r in got)


def test_typed_colour_in_a_style_attribute_fails():
    got = reasons(page(body='<p style="color:#abc">x</p>'))
    assert any("style attribute" in r for r in got)


def test_marker_url_reference_is_not_a_colour():
    assert reasons(page(css=".e{marker-end:url(#ah-w)}")) == []


def test_missing_tokens_block_fails():
    got = reasons(page(tokens=""))
    assert any('<style id="tokens">' in r for r in got)


def test_tokens_without_dark_blocks_fail():
    got = reasons(page(tokens=":root{--bg:#fff;--fg:#000}"))
    assert any("prefers-color-scheme" in r for r in got)
    assert any('data-theme="dark"' in r for r in got)


def test_undefined_var_fails_unless_it_has_a_fallback():
    assert any("--nope" in r for r in reasons(page(css="p{color:var(--nope)}")))
    assert reasons(page(css="p{color:var(--nope, var(--fg))}")) == []


@pytest.mark.parametrize(
    "extra,ok",
    [
        (
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js"></script>',
            True,
        ),
        (
            '<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>',
            True,
        ),
        ('<script src="https://evil.example/x.js"></script>', False),
        (
            '<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>',
            False,
        ),
        (
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
            True,
        ),
        ('<link rel="stylesheet" href="https://example.com/a.css">', False),
        ('<link rel="preconnect" href="https://fonts.gstatic.com">', True),
    ],
)
def test_outside_resources(extra, ok):
    got = reasons(page(head_extra=extra))
    assert (got == []) is ok, got


def test_outside_image_fails():
    assert any(
        "<img>" in r for r in reasons(page(body='<img src="https://x.example/a.png">'))
    )


def test_import_outside_the_fonts_block_fails():
    got = reasons(
        page(css="@import url('https://x.example/a.css'); body{color:var(--fg)}")
    )
    assert any("loads a resource" in r for r in got)


@pytest.mark.parametrize(
    "svg,expect",
    [
        (
            '<svg viewBox="0 0 10 10" role="img" aria-label="x"><rect></svg>',
            "well-formed",
        ),
        ('<svg role="img" aria-label="x"></svg>', "viewBox"),
        ('<svg viewBox="0 0 10 10"></svg>', 'role="img"'),
        (
            '<svg viewBox="0 0 10 10" role="img" aria-label="x"><script>1</script></svg>',
            "<script>",
        ),
        (
            '<svg viewBox="0 0 10 10" role="img" aria-label="x"><foreignObject/></svg>',
            "<foreignobject>",
        ),
        (
            '<svg viewBox="0 0 10 10" role="img" aria-label="x"><rect onclick="x()"/></svg>',
            "event handler",
        ),
        (
            '<svg viewBox="0 0 10 10" role="img" aria-label="x"><a href="https://x.example"><rect/></a></svg>',
            "only #id",
        ),
        (
            '<svg viewBox="0 0 10 10" role="img" aria-label="x"><rect fill="#123456"/></svg>',
            "types a colour",
        ),
    ],
    ids=["xml", "viewbox", "label", "script", "foreign", "handler", "link", "fill"],
)
def test_svg_failures(svg, expect):
    got = reasons(page(body=svg))
    assert any(expect in r for r in got), got


def test_svg_with_a_title_child_is_labelled():
    svg = '<svg viewBox="0 0 10 10" role="img"><title>A picture</title></svg>'
    assert reasons(page(body=svg)) == []


def test_duplicate_ids_fail():
    got = reasons(page(body=SVG + SVG))
    assert any("'ah-w' is used 2 times" in r for r in got)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "Repo"
    r.mkdir()
    (r / "a.py").write_text("one = 1\ntwo = 2\nthree = 3\n")

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(r), *a], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("add", "a.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return r, git("rev-parse", "HEAD")


def cite(raw, quote=""):
    q = f' data-quote="{quote}"' if quote else ""
    return page(body=f'<p>x<a class="cite" href="#" data-cite="{raw}"{q}>1</a></p>')


def test_citation_at_its_commit_passes(repo):
    path, sha = repo
    assert reasons(cite(f"Repo@{sha}:a.py:2-3", "two = 2"), repos={"Repo": path}) == []


def test_citation_failures(repo):
    path, sha = repo
    repos = {"Repo": path}
    assert any("is not <repo key>" in r for r in reasons(cite("nonsense"), repos=repos))
    assert any("pass --repo" in r for r in reasons(cite(f"Other@{sha}:a.py:1")))
    assert any(
        "is not at" in r for r in reasons(cite(f"Repo@{sha}:b.py:1"), repos=repos)
    )
    assert any(
        "outside the file" in r
        for r in reasons(cite(f"Repo@{sha}:a.py:3-9"), repos=repos)
    )
    assert any(
        "data-quote" in r
        for r in reasons(cite(f"Repo@{sha}:a.py:1", "two = 2"), repos=repos)
    )


def test_new_writes_the_shell_with_tokens_and_never_overwrites(tmp_path):
    out = tmp_path / "p.html"
    assert new_page(out, NEUTRAL) == ""
    text = out.read_text()
    assert "/*TOKENS*/" not in text and "--accent-soft" in text
    assert "exists" in new_page(out, NEUTRAL)
    assert "no tokens.css" in new_page(tmp_path / "q.html", tmp_path)


def test_the_shell_only_fails_on_its_placeholder_title(tmp_path):
    out = tmp_path / "p.html"
    new_page(out, NEUTRAL)
    got = reasons(out.read_text())
    assert len(got) == 1 and "<title>" in got[0], got
    assert SHELL.is_file()


def test_render_draws_each_svg_light_and_dark(tmp_path):
    pytest.importorskip("resvg_py")
    out = tmp_path / "p.html"
    new_page(out, NEUTRAL)
    assert main(["render", str(out), "--out", str(tmp_path / "png")]) == 0
    pngs = sorted(p.name for p in (tmp_path / "png").iterdir())
    assert pngs == ["1-dark.png", "1-light.png", "2-dark.png", "2-light.png"]
    assert all((tmp_path / "png" / n).read_bytes()[:4] == b"\x89PNG" for n in pngs)


def test_check_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.html"
    good.write_text(page())
    bad = tmp_path / "bad.html"
    bad.write_text(page(title="x"))
    assert main(["check", str(good)]) == 0
    assert "check: ok" in capsys.readouterr().out
    assert main(["check", str(bad)]) == 1
    assert "FAIL:" in capsys.readouterr().out
