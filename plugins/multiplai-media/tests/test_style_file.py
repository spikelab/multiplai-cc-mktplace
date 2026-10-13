"""stages/style_file.py: the standard-library reader for an overlay style's frontmatter."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import style_file as sf  # noqa: E402

STYLES = _SCRIPTS.parent / "references" / "styles"


def test_clean_tech_frontmatter_reads_whole() -> None:
    fm = sf.frontmatter((STYLES / "clean-tech.md").read_text())
    assert fm["name"] == "clean-tech"
    assert fm["palette"] == {"background": "#0B1220", "surface": "#111A2E", "text": "#FFFFFF", "muted": "#9AA7BD",
                             "accent": "#3DDC97", "key": "#00FF00"}
    assert fm["fonts"]["display"] == "Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif"
    assert fm["fonts"]["weights"] == {"display": 800, "body": 500}
    assert fm["shape"] == {"radius_px": 18, "stroke_px": 0}
    assert fm["motion"] == {"easing": "cubic-bezier(.2,.8,.2,1)", "enter_s": 0.5, "exit_s": 0.3,
                            "stagger_s": 0.12, "max_moving": 2}
    assert fm["layout_9x16"]["caption_band"] == [1180, 1420]


def test_flat_vector_frontmatter_reads_whole() -> None:
    fm = sf.frontmatter((STYLES / "flat-vector.md").read_text())
    assert fm["name"] == "flat-vector"
    assert fm["palette"]["accent_2"] == "#2EC4B6"
    assert fm["fonts"]["body"].startswith("'Avenir Next', 'Nunito'")
    assert fm["fonts"]["weights"] == {"display": 800, "body": 600}
    assert fm["motion"]["easing"] == "cubic-bezier(.34,1.56,.64,1)"
    assert (fm["shape"]["radius_px"], fm["motion"]["stagger_s"]) == (28, 0.15)


def test_the_readme_format_example_reads_with_its_comments() -> None:
    """The format block in references/styles/README.md carries a # comment on most lines."""
    text = (STYLES / "README.md").read_text()
    block = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    fm = sf.frontmatter(block)
    assert fm["brand"] == "path/to/brand.json"
    assert fm["palette"]["key"] == "#00FF00"           # the "# keyed overlays only" comment is gone
    assert fm["motion"]["max_moving"] == 2
    assert fm["layout_9x16"] == {"safe_top": 200, "safe_bottom": 480, "safe_side": 120, "caption_band": [1180, 1420]}


@pytest.mark.parametrize("name", ["clean-tech", "flat-vector"])
def test_panel_values_are_the_keys_the_panel_edits(name) -> None:
    got = sf.load(STYLES / f"{name}.md")
    assert got["status"] == "ok" and got["name"] == name
    keys = list(got["values"])
    palette = [k for k in keys if k.startswith("palette.")]
    assert palette and keys[:len(palette)] == palette                      # palette first, in file order
    assert keys[len(palette):] == ["fonts.display", "fonts.body", "fonts.weights.display", "fonts.weights.body",
                                   "shape.radius_px", "motion.enter_s", "motion.exit_s", "motion.stagger_s"]
    assert "motion.easing" not in got["values"] and "shape.stroke_px" not in got["values"]


def test_shipped_finds_only_style_files_by_name() -> None:
    assert sf.shipped("clean-tech") == STYLES / "clean-tech.md"
    for name in ("README", "nope", "../styles/clean-tech", "/etc/passwd", "", "clean-tech.md/.."):
        assert sf.shipped(name) is None, name


def test_scalars() -> None:
    assert [sf.scalar(x) for x in ("12", "-1.5", ".5", "1e3", "true", "false", "null", "~", '"x # y"', "'a'", "bare text")] \
        == [12, -1.5, 0.5, 1000.0, True, False, None, None, "x # y", "a", "bare text"]


def test_a_comment_inside_quotes_is_kept() -> None:
    fm = sf.frontmatter('---\na: "#123456"   # a colour\nb: "x # not a comment"\n---\n')
    assert fm == {"a": "#123456", "b": "x # not a comment"}


@pytest.mark.parametrize("text,match", [
    ("no frontmatter here\n", "no frontmatter"),
    ("---\na: 1\n", "no frontmatter"),                                  # never closed
    ("---\na:\n\tb: 1\n---\n", "tabs"),
    ("---\na: 1\na: 2\n---\n", "duplicate key"),
    ("---\na:\n    b: 1\n  c: 2\n---\n", "indentation"),
    ("---\n- item\n---\n", "expected `key: value`"),
    ('---\na: "open\n---\n', "unclosed quote"),
    ("---\na: {b: {c: 1}}\n---\n", "nested flow"),
    ("---\na: [1, 2\n---\n", "unclosed flow list"),
    ("---\na: {b 1}\n---\n", "bad flow mapping item"),
    ("---\na: [1, , 2]\n---\n", "empty item"),
])
def test_malformed_frontmatter_fails_loudly(text, match) -> None:
    with pytest.raises(sf.StyleFileError, match=match):
        sf.frontmatter(text)


def test_load_reports_a_broken_file_instead_of_raising(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("---\npalette:\n\taccent: '#fff'\n---\n")
    got = sf.load(bad)
    assert got["status"] == "invalid" and "tabs" in got["error"]
    assert sf.load(tmp_path / "missing.md")["status"] == "invalid"
