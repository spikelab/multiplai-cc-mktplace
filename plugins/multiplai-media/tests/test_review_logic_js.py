"""Runs the job page's pure-function tests (tests/review_logic.test.js) under node.

They fail rather than skip without node, as review-viewer's do: CI's runner
image ships node.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
STATIC = TESTS.parent / "skills" / "video-edit" / "scripts" / "static"


def test_page_logic_under_node() -> None:
    node = shutil.which("node")
    assert node, "node is required for the page-logic tests"
    proc = subprocess.run([node, "--test", str(TESTS / "review_logic.test.js")], capture_output=True,
                          text=True, cwd=TESTS.parent, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.js")))
def test_every_page_script_parses(name: str) -> None:
    node = shutil.which("node")
    assert node, "node is required for the page-logic tests"
    proc = subprocess.run([node, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_the_page_loads_logic_and_app_first_then_one_script_per_tab() -> None:
    html = (STATIC / "review.html").read_text()
    scripts = re.findall(r'<script src="/static/([^"]+)"></script>', html)
    assert scripts == ["logic.js", "app.js", "tab-clips.js", "tab-speakers.js", "tab-look.js",
                       "tab-music.js", "tab-review.js"]
    assert all((STATIC / s).is_file() for s in scripts)
    assert "<script>" not in html                      # CSP: script-src 'self', no inline scripts


def test_each_tab_file_registers_the_tab_it_is_named_for() -> None:
    """A wrong id in registerTab leaves its panel blank; the script-order test cannot see that."""
    logic = (STATIC / "logic.js").read_text()
    tabs = re.findall(r'"([a-z]+)"', re.search(r"const TABS = \[([^\]]+)\]", logic).group(1))
    files = sorted(p.stem.removeprefix("tab-") for p in STATIC.glob("tab-*.js"))
    assert sorted(tabs) == files
    for tab in tabs:
        text = (STATIC / f"tab-{tab}.js").read_text()
        assert re.findall(r'App\.registerTab\("([^"]+)"', text) == [tab], tab
