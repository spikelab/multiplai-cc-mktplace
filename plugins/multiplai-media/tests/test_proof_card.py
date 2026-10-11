"""The proof-shot overlay page (stages/proof_card.py, `pipeline.py proof-card`).

Pages are built from a generated PNG and shot.json, then checked with the
same lint render runs (stages/overlay_lint.py) and by reading the end of
their move keyframes: the highlight box must end inside the card's window.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402
from stages import overlay_lint, proof_card  # noqa: E402
from test_capture import png  # noqa: E402

BOX = {"text": "rise 4%", "x": 180, "y": 1900, "w": 520, "h": 44}


def shot(tmp_path: Path, w: int = 1280, h: int = 2400, box: dict | None = BOX,
         url: str = "https://www.news.example.org/post") -> Path:
    d = tmp_path / "shot"
    d.mkdir(exist_ok=True)
    (d / "shot.png").write_bytes(png(w, h))
    (d / "shot.json").write_text(json.dumps({"url": url, "captured_at": "2026-10-11T09:00:00Z",
                                             "width": w, "height": h, "source": "browser",
                                             "highlight": box}))
    return d


def style() -> proof_card.Style:
    return proof_card.load_style("clean-tech")


def window(page: str) -> tuple[float, float]:
    m = re.search(r"\.win\{position:relative;width:([\d.]+)px;height:([\d.]+)px", page)
    return float(m.group(1)), float(m.group(2))


def move_end(page: str) -> tuple[float, float, float]:
    m = re.search(r"@keyframes move\{from\{transform:[^}]*\}to\{transform:translate\(([-\d.]+)px,"
                  r"([-\d.]+)px\) scale\(([\d.]+)\)\}\}", page)
    assert m, "no move keyframes"
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def resting(page: str) -> tuple[float, float, float]:
    m = re.search(r"\.page\{[^}]*transform:translate\(([-\d.]+)px,([-\d.]+)px\) scale\(([\d.]+)\)", page)
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


@pytest.mark.parametrize("motion", ["scroll", "zoom", "none"])
def test_the_page_passes_the_overlay_lint(tmp_path: Path, motion: str) -> None:
    page, _ = proof_card.build(shot(tmp_path), motion, style())
    assert overlay_lint.errors(overlay_lint.lint(page, mode="alpha")) == []
    assert 'src="data:image/png;base64,' in page
    assert not re.search(r"https?://", page)


@pytest.mark.parametrize("motion", ["scroll", "zoom"])
def test_the_move_ends_with_the_highlight_inside_the_window(tmp_path: Path, motion: str) -> None:
    page, _ = proof_card.build(shot(tmp_path), motion, style())
    win_w, win_h = window(page)
    tx, ty, s = move_end(page)
    left, top = tx + BOX["x"] * s, ty + BOX["y"] * s
    right, bottom = left + BOX["w"] * s, top + BOX["h"] * s
    assert 0 <= left < right <= win_w
    assert 0 <= top < bottom <= win_h


def test_scroll_starts_at_the_top_and_zoom_scales_in(tmp_path: Path) -> None:
    d = shot(tmp_path)
    scroll, _ = proof_card.build(d, "scroll", style())
    assert resting(scroll)[:2] == (0.0, 0.0)
    assert move_end(scroll)[1] < 0
    zoom, _ = proof_card.build(d, "zoom", style())
    assert move_end(zoom)[2] > resting(zoom)[2]


def test_none_holds_the_highlight_in_view_without_moving(tmp_path: Path) -> None:
    page, _ = proof_card.build(shot(tmp_path), "none", style())
    assert "@keyframes move" in page and "animation:move" not in page
    tx, ty, s = resting(page)
    assert 0 <= ty + BOX["y"] * s <= window(page)[1]


def test_the_ring_is_drawn_in_the_accent_and_the_domain_is_shown(tmp_path: Path) -> None:
    page, _ = proof_card.build(shot(tmp_path), "scroll", style())
    assert "border-color:#3DDC97" in page
    assert '<span class="site">news.example.org</span>' in page
    assert "2026-10-11" in page
    assert page.count("<img") == 1                      # the screenshot itself, unedited


def test_the_card_fits_a_vertical_region(tmp_path: Path) -> None:
    page, _ = proof_card.build(shot(tmp_path), "zoom", style(), width=1080, height=1920)
    card = re.search(r"\.card\{position:absolute;left:([\d.]+)px;top:([\d.]+)px;width:([\d.]+)px", page)
    left, width = float(card.group(1)), float(card.group(3))
    assert left > 0 and left + width <= 1080


def test_a_short_overlay_warns_that_the_highlight_is_not_held_2_s(tmp_path: Path) -> None:
    _, warnings = proof_card.build(shot(tmp_path), "scroll", style(), duration=3.0)
    assert warnings and "2 s" in warnings[0]
    _, warnings = proof_card.build(shot(tmp_path), "scroll", style(), duration=6.0)
    assert warnings == []


def test_moving_without_a_highlight_is_refused(tmp_path: Path) -> None:
    d = shot(tmp_path, box=None)
    with pytest.raises(proof_card.ProofCardError, match="--motion none"):
        proof_card.build(d, "zoom", style())
    page, _ = proof_card.build(d, "none", style())
    assert 'class="hl"' not in page


def test_style_values_come_from_the_style_file() -> None:
    s = proof_card.load_style("flat-vector")
    assert s.accent == "#FF6B4A" and s.surface == "#FFFFFF"
    with pytest.raises(proof_card.ProofCardError, match="clean-tech"):
        proof_card.load_style("no-such-style")


def test_cli_writes_the_card_and_lints_it(tmp_path: Path, capsys) -> None:
    out = tmp_path / "card.html"
    args = argparse.Namespace(shot_dir=str(shot(tmp_path)), motion="zoom", style="clean-tech",
                              width=1920, height=1080, duration=6.0, out=str(out))
    assert pipeline.cmd_proof_card(args) == 0
    assert out.exists()
    assert "CARD:" in capsys.readouterr().out
    lint_args = argparse.Namespace(page=str(out), mode="alpha", key_color="#00FF00")
    assert pipeline.cmd_lint_overlay(lint_args) == 0
