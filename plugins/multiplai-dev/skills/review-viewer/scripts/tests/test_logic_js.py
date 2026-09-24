from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_page_logic_under_node():
    node = shutil.which("node")
    assert node, "node is required for the page-logic tests"
    proc = subprocess.run([node, str(TESTS / "logic.test.js")], capture_output=True, text=True,
                          cwd=TESTS.parent, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
