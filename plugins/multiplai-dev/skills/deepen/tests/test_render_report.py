"""Snapshot-ish tests for render_report.

Not a full snapshot diff — that's brittle against template tweaks. Instead, assert
on the contract: glossary terms, badge classes, candidate count, top-recommendation
linkage. If a future refactor changes the visible HTML, these still pass as long
as the contract holds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from render_report import render, strength_class  # type: ignore[import-not-found]  # noqa: E402


@pytest.fixture
def payload() -> dict:
    return json.loads((HERE / "fixtures" / "sample-candidates.json").read_text())


def test_strength_classes_distinct() -> None:
    assert strength_class("Strong") != strength_class("Worth exploring")
    assert strength_class("Speculative") != strength_class("Strong")
    assert "emerald" in strength_class("Strong")
    assert "amber" in strength_class("Worth exploring")


def test_renders_html_with_doctype(payload: dict) -> None:
    html = render(payload)
    assert html.startswith("<!doctype html>")
    assert "</html>" in html


def test_includes_tailwind_and_mermaid_cdns(payload: dict) -> None:
    html = render(payload)
    assert "cdn.tailwindcss.com" in html
    assert "mermaid" in html.lower()


def test_renders_each_candidate(payload: dict) -> None:
    html = render(payload)
    for c in payload["candidates"]:
        assert c["title"] in html
        for f in c["files"]:
            assert f in html


def test_top_recommendation_links_correct_card(payload: dict) -> None:
    html = render(payload)
    idx = payload["top_recommendation"]["index"]
    assert f'href="#candidate-{idx}"' in html
    assert f'id="candidate-{idx}"' in html


def test_adr_callout_rendered_when_present(payload: dict) -> None:
    html = render(payload)
    assert payload["candidates"][1]["adr_callout"] in html


def test_missing_optional_fields_default_safely() -> None:
    minimal = {
        "repo": "test",
        "candidates": [
            {
                "title": "Bare candidate",
                "problem": "p",
                "solution": "s",
                "strength": "Speculative",
                "dependency_category": "in-process",
            }
        ],
    }
    html = render(minimal)
    assert "Bare candidate" in html
    assert "Speculative" in html


def test_glossary_terms_passable_through(payload: dict) -> None:
    """Smoke check: the template does not strip the architectural vocabulary."""
    html = render(payload)
    for term in ("interface", "seam", "module"):
        assert term in html.lower()
