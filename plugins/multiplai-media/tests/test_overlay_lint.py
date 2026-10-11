"""Overlay page checks (stages/overlay_lint.py, `pipeline.py lint-overlay`, render).

Each rule has a fixture page that trips it and one that narrowly passes it,
in fixtures/overlay-lint/. Fonts are checked against a stand-in for
fontconfig, so the result does not depend on what this machine has installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402
from stages import composite, overlay, overlay_lint  # noqa: E402
from stages.edl import EDL  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "overlay-lint"
INSTALLED = {"arial", "liberation sans"}


@pytest.fixture(autouse=True)
def fonts(monkeypatch):
    monkeypatch.setattr(overlay_lint, "installed", lambda family: family.lower() in INSTALLED)


def _lint(name: str, **kw) -> list[overlay_lint.Finding]:
    return overlay_lint.lint((FIX / name).read_text(encoding="utf-8"), **kw)


def _cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "lint-overlay", *argv])
    return pipeline.main()


ERRORS = {
    "math-random": "Math.random()",
    "date-now": "Date.now()",
    "new-date": "new Date()",
    "get-random-values": "crypto.getRandomValues()",
    "transition": "CSS transition",
    "infinite-css": "infinite CSS animation",
    "infinite-waapi": "iterations: Infinity",
    "font-missing": "no family in `font-family",
    "network": "https://example.com/logo.png",
}
WARNINGS = {
    "layout-keyframes": "@keyframes grow animates width",
    "layout-script": "a script animates fontSize",
    "key-colour": "close to the key colour #00FF00",
}


@pytest.mark.parametrize("rule,text", ERRORS.items())
def test_each_error_rule_fires_on_its_page(rule, text) -> None:
    errs = overlay_lint.errors(_lint(f"{rule}.html", mode="panel" if rule != "key-colour" else "keyed"))
    assert len(errs) == 1 and text in errs[0].message, errs


@pytest.mark.parametrize("rule", [r for r in ERRORS if r != "new-date"])
def test_each_error_rule_passes_its_near_miss(rule) -> None:
    assert _lint(f"{rule}-ok.html", mode="panel") == []


@pytest.mark.parametrize("rule,text", WARNINGS.items())
def test_each_warning_rule_fires_without_an_error(rule, text) -> None:
    found = _lint(f"{rule}.html")
    assert [f.severity for f in found] == ["warning"] and text in found[0].message, found


@pytest.mark.parametrize("rule", ["layout-keyframes", "key-colour"])
def test_each_warning_rule_passes_its_near_miss(rule) -> None:
    assert _lint(f"{rule}-ok.html") == []


def test_the_clean_page_has_no_findings() -> None:
    for mode in ("alpha", "panel", "keyed"):
        assert _lint("clean.html", mode=mode) == []


def test_findings_carry_the_line() -> None:
    assert _lint("transition.html", mode="panel")[0].line == 1
    assert _lint("key-colour.html")[0].line == 2


def test_key_colour_is_only_checked_for_keyed_pages() -> None:
    assert _lint("key-colour.html", mode="alpha") == []
    assert _lint("key-colour.html", key_color="#FF00FF") == []


def test_a_fallback_font_in_the_stack_is_a_warning() -> None:
    html = "<style>.t{font-family:Inter,-apple-system,'Helvetica Neue',Arial,sans-serif}</style>"
    found = overlay_lint.lint(html, mode="panel")
    assert [f.severity for f in found] == ["warning"]
    assert "Inter is not available here, so the text renders in Arial" in found[0].message


def test_fonts_unchecked_without_fontconfig_is_a_warning(monkeypatch) -> None:
    monkeypatch.setattr(overlay_lint, "installed", lambda family: None)
    found = _lint("font-missing.html", mode="panel")
    assert [f.severity for f in found] == ["warning"] and "no fc-match" in found[0].message


def test_installed_uses_fontconfig_resolution(monkeypatch) -> None:
    monkeypatch.undo()
    answers = {"Arial": "Liberation Sans", "Inter": "DejaVu Sans", "DejaVu Sans": "DejaVu Sans",
               "video-edit-no-such-font-family": "DejaVu Sans"}
    monkeypatch.setattr(overlay_lint, "_fc_match", lambda family: answers[family])
    overlay_lint._fc_default.cache_clear()
    try:
        assert overlay_lint.installed("Arial") is True          # an alias counts
        assert overlay_lint.installed("Inter") is False         # the default fallback does not
        assert overlay_lint.installed("DejaVu Sans") is True    # the default font itself does
    finally:
        overlay_lint._fc_default.cache_clear()


def test_key_distance_matches_colorkey() -> None:
    assert overlay_lint.key_distance((0, 255, 0), (0, 255, 0)) == 0
    assert overlay_lint.key_distance((0, 0, 0), (255, 255, 255)) == pytest.approx(1)


# --- the command ---------------------------------------------------------------------

@pytest.mark.parametrize("rule", ERRORS)
def test_cli_exits_non_zero_on_each_error_page(monkeypatch, capsys, rule) -> None:
    assert _cli(monkeypatch, str(FIX / f"{rule}.html"), "--mode", "panel") == 1
    assert "error: line" in capsys.readouterr().out


def test_cli_exits_zero_on_the_clean_page(monkeypatch, capsys) -> None:
    assert _cli(monkeypatch, str(FIX / "clean.html")) == 0
    assert "no problems found" in capsys.readouterr().out


def test_cli_exits_zero_on_warnings(monkeypatch, capsys) -> None:
    assert _cli(monkeypatch, str(FIX / "key-colour.html")) == 0
    assert "warning: line 2" in capsys.readouterr().out


# --- render ----------------------------------------------------------------------------

def test_render_refuses_a_bad_page_before_any_frame(tmp_path, monkeypatch) -> None:
    def no_browser():
        raise AssertionError("no browser may start for a page that fails its checks")
    monkeypatch.setattr(overlay, "_Chromium", no_browser)
    monkeypatch.setattr(composite.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may run")))
    e = EDL.from_dict({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 10}],
                       "overlays": [{"html": str(FIX / "math-random.html"), "style": "clean-tech",
                                     "start": 1, "end": 3, "mode": "alpha"}]})
    with pytest.raises(ValueError, match=r"math-random.html: error: line 1: Math.random\(\)"):
        composite.render(e, tmp_path / "o.mp4", work_dir=tmp_path)


def test_render_prints_warnings_and_goes_on(capsys) -> None:
    e = EDL.from_dict({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 10}],
                       "overlays": [{"html": str(FIX / "key-colour.html"), "style": "clean-tech",
                                     "start": 1, "end": 3}]})
    composite.lint_overlays(e)
    assert "⚠ overlay key-colour.html: warning: line 2" in capsys.readouterr().out
