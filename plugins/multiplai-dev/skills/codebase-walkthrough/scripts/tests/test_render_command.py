"""`render <run dir>` rewrites a finished run's four files from walkthrough-state.json, with no model call.

Stop and resume rows of plan-walkthrough-html-page-2026-10-21: S2/S5, S4 and S7 are here; S1 is in
test_resume.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from walkthrough_pipeline import budget, sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE

# In the order `write_outputs` returns them, which is the order the `walkthrough:` lines print.
OUTPUTS = (
    "bookings-walkthrough.md",
    "bookings-walkthrough.html",
    "bookings-paths.json",
)


def _comparable(name: str, data: bytes):
    """The bytes, except that a JSON file's `generated_at` is the time it was written."""
    if not name.endswith(".json"):
        return data
    doc = json.loads(data)
    doc.pop("generated_at", None)
    return doc


def assert_same_outputs(out: Path, original: dict[str, bytes]) -> None:
    for name, data in original.items():
        assert _comparable(name, (out / name).read_bytes()) == _comparable(
            name, data
        ), name
    assert (out / OUTPUTS[0]).read_bytes() == original[
        OUTPUTS[0]
    ]  # the Markdown, byte for byte


@pytest.fixture
def no_model(monkeypatch):
    """Every way to reach a model, and the budget ledger, fail the test if called."""

    def called(*args, **kwargs):
        raise AssertionError("render reached a model or started a ledger")

    for name in ("agent_call_structured", "_run", "run_agent"):
        monkeypatch.setattr(sdk, name, called)
    monkeypatch.setattr(budget, "start", called)
    monkeypatch.setattr(
        budget, "_process", budget.ProcessSpend()
    )  # a new process: the fakes' spend is not its


def _edit_state(run_dir: Path, **changes) -> None:
    state = json.loads((run_dir / STATE_FILE).read_text())
    for key, value in changes.items():
        if key == "repo":
            repo_key, field, new = value
            state["repos"][repo_key][field] = new
        else:
            state[key] = value
    (run_dir / STATE_FILE).write_text(json.dumps(state))


def test_render_brings_back_deleted_or_truncated_outputs_twice_running(
    done_run, no_model, capsys
):
    """S2, S5: outputs deleted or truncated by a kill mid-write, then `render` run twice: each time the
    same four files as an unbroken render, the Markdown byte-identical; the state file and progress.log
    are left alone, and the last line is a zero spend."""
    md_path, run_dir, _ = done_run
    out = md_path.parent
    original = {n: (out / n).read_bytes() for n in OUTPUTS}
    state_before = (run_dir / STATE_FILE).read_bytes()
    log_before = (run_dir / "progress.log").read_bytes()

    for n in OUTPUTS:
        (out / n).unlink()
    capsys.readouterr()
    assert main(["render", str(run_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"walkthrough: {out / n}" for n in OUTPUTS
    ] + ["SPEND:0.000000:0"]
    assert_same_outputs(out, original)

    (out / OUTPUTS[2]).write_bytes(original[OUTPUTS[2]][:100])  # a truncated page
    (out / OUTPUTS[1]).unlink()
    assert main(["render", str(run_dir), "--trust-repo"]) == 0
    assert_same_outputs(out, original)
    assert (run_dir / STATE_FILE).read_bytes() == state_before
    assert (run_dir / "progress.log").read_bytes() == log_before


def test_render_output_writes_there(done_run, no_model, tmp_path):
    _, run_dir, _ = done_run
    elsewhere = tmp_path / "elsewhere"
    assert main(["render", str(run_dir), "--output", str(elsewhere)]) == 0
    assert sorted(p.name for p in elsewhere.iterdir()) == sorted(OUTPUTS)


def test_render_refuses_a_run_before_its_write_stage(done_run, no_model, capsys):
    """S7: the saved stage is before `write`: exit 1 with the resume hint, and nothing is written."""
    md_path, run_dir, _ = done_run
    _edit_state(run_dir, stage="explore")
    for n in OUTPUTS:
        (md_path.parent / n).unlink()
    capsys.readouterr()
    assert main(["render", str(run_dir)]) == 1
    run_dir = run_dir.resolve()
    assert (
        f"render: {run_dir} has not finished its write stage; continue it with resume {run_dir} --trust-repo"
        in capsys.readouterr().err
    )
    assert not any((md_path.parent / n).exists() for n in OUTPUTS)


def test_render_refuses_a_directory_with_no_state(tmp_path, no_model, capsys):
    """S7, the state missing: the same refusal."""
    empty = (tmp_path / "no-run").resolve()
    empty.mkdir()
    assert main(["render", str(empty)]) == 1
    assert (
        f"render: {empty} has not finished its write stage" in capsys.readouterr().err
    )
    assert list(empty.iterdir()) == []


@pytest.mark.parametrize("field, value", [("head_sha", "0" * 40), ("path", "gone")])
def test_render_stops_when_a_cited_commit_cannot_be_read(
    done_run, no_model, capsys, tmp_path, field, value
):
    """S4: the repo lacks the saved commit, or is gone: exit 1 naming the repo and commit, nothing written."""
    md_path, run_dir, _ = done_run
    state = json.loads((run_dir / STATE_FILE).read_text())
    key = state["target"]["repo_key"]
    if field == "path":
        value = str(tmp_path / value)
    _edit_state(run_dir, repo=(key, field, value))
    sha = json.loads((run_dir / STATE_FILE).read_text())["repos"][key]["head_sha"]
    for n in OUTPUTS:
        (md_path.parent / n).unlink()
    capsys.readouterr()
    assert main(["render", str(run_dir)]) == 1
    err = capsys.readouterr().err
    assert f"render: cannot read {key} at {sha}: " in err
    assert len(err.split(f"{sha}: ", 1)[1].strip()) > 0  # git's own message follows
    assert not any((md_path.parent / n).exists() for n in OUTPUTS)
