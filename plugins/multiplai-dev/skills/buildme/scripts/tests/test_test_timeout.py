"""`tdd.test_timeout` reaches every full-suite run: the gates hand it to the
test command's process, every gate call in the pipeline passes it, and the
epic's merge tests read it from the child's `specs/config.yaml`."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest
from build_pipeline import gates
from build_pipeline.epic import merge

PIPELINE = Path(__file__).resolve().parents[1] / "build_pipeline"
FULL_SUITE_GATES = {"baseline_test_gate", "integration_gate", "final_verification"}


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


@pytest.mark.parametrize("gate", ["baseline_test_gate", "integration_gate"])
def test_the_gate_gives_the_test_process_its_timeout(tmp_path, trusted, gate):
    with patch("build_pipeline.gates.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = run.return_value.stderr = ""
        getattr(gates, gate)("true", tmp_path, timeout=1800)
    assert run.call_args.kwargs["timeout"] == 1800


def test_a_run_past_its_timeout_is_not_run(tmp_path, trusted):
    gate = gates.integration_gate("sleep 5", tmp_path, timeout=1)
    assert not gate.passed
    assert "timed out after 1 seconds" in gate.reason


def test_every_full_suite_gate_call_passes_the_timeout():
    """A gate call without `timeout=` would run at the 300 s default whatever
    `tdd.test_timeout` says."""
    missing = []
    for path in PIPELINE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            passed = {k.arg for k in node.keywords}
            if (
                path.name != "gates.py"
                and name in FULL_SUITE_GATES
                and "timeout" not in passed
            ):
                missing.append(f"{path.relative_to(PIPELINE)}:{node.lineno}")
    assert missing == []


def test_the_merge_tests_read_the_childs_timeout(tmp_path):
    (tmp_path / "specs").mkdir()
    assert merge.suite_timeout(tmp_path) == gates.DEFAULT_TEST_TIMEOUT
    (tmp_path / "specs" / "config.yaml").write_text("tdd:\n  test_timeout: 900\n")
    assert merge.suite_timeout(tmp_path) == 900
