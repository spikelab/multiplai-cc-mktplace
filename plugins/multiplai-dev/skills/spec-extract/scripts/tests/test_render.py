"""The render step: registry files, report.md and summary.json against golden files (plan, item 17).

`fixtures/golden/registry/sample.md` is also the file buildme's reader test
merges a delta into (`buildme/scripts/tests/test_change_manager.py`).
Set `SPEC_EXTRACT_UPDATE_GOLDEN=1` to rewrite the golden files after a
deliberate format change, then read the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import read_state
from test_write_gate import CALC_ANSWER, folder, go, make_calc_repo

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
SAMPLE_GROUP = {"capabilities": [{"name": "sample", "summary": "Arithmetic on two numbers.",
                                  "symbols": ["src/calc.py::add", "src/calc.py::sub", "src/calc.py::mul"]}]}


@pytest.fixture
def rendered(tmp_path, fake_call) -> Path:
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.answers["group"] = SAMPLE_GROUP
    fake_call.answers["write:sample"] = CALC_ANSWER
    assert go(repo, tmp_path / "out") == 0
    return folder(tmp_path / "out")


@pytest.mark.parametrize("name", ["registry/sample.md", "report.md", "summary.json"])
def test_matches_golden(rendered, name):
    got = (rendered / name).read_text(encoding="utf-8")
    golden = GOLDEN / name
    if os.environ.get("SPEC_EXTRACT_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(got, encoding="utf-8")
    assert got == golden.read_text(encoding="utf-8")


def test_render_is_recorded_and_the_snapshot_deleted(rendered, capsys):
    state = read_state(rendered)
    assert state["steps_done"] == ["resolve", "scope", "explore", "group", "write", "render"]
    assert not (rendered / "snapshot").exists()
    assert f"SPEC_EXTRACT:RENDERED 1 {rendered / 'registry'}" in capsys.readouterr().out


def test_after_render_a_rerun_makes_no_call_and_leaves_the_outputs_alone(rendered, tmp_path, fake_call):
    """Row R10 (documentation): a stop part-way through deleting `snapshot/`. The re-run
    deletes the rest and never re-renders; after render the outputs are the user's (R24)."""
    (rendered / "snapshot" / "src").mkdir(parents=True)
    (rendered / "snapshot" / "src" / "calc.py").write_text("partial\n")
    (rendered / "report.md").write_text("edited by the user\n")
    calls = dict(fake_call.calls)
    assert go(tmp_path / "widgets", tmp_path / "out") == 0
    assert dict(fake_call.calls) == calls
    assert not (rendered / "snapshot").exists()
    assert (rendered / "report.md").read_text() == "edited by the user\n"
