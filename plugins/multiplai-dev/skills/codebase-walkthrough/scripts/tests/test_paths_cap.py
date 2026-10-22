"""`--scenarios` caps the paths, and every entry left out is listed under Coverage with its stage and reason."""

from __future__ import annotations

import json
import re

import path_fixtures
import pytest
from code_facts import PathDrop
from code_facts.paths import ListingSkip, PathDiagnostic
from fakes import FakeAgents
from test_paths_render import _entry, _render, _result, _state
from test_paths_stage import _click_run, _paths_body
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.orchestrator import RunOptions
from walkthrough_pipeline.render import coverage_lines, section_texts
from walkthrough_pipeline.state import STATE_FILE

TWENTY_FIVE = [f"c{n:02d}" for n in range(1, 26)]
LEFT_OUT_HEAD = "| Entry | Stage | Reason | Detail |"


def _left_out_rows(text: str) -> list[list[str]]:
    """The cells of each row of the `### Left out` table in `text`."""
    after = text[text.index("### Left out") :]
    lines = after.splitlines()
    start = lines.index(LEFT_OUT_HEAD) + 2
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]])
    return rows


# --- the flag ----------------------------------------------------------------------------------


def test_the_default_cap_is_21(tmp_path, monkeypatch, spy):
    assert RunOptions(target=".").scenarios == 21
    code, _, _ = _click_run(
        tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert code == 0
    assert spy.calls[0]["cap"] == 21


def test_help_says_scenarios_caps_paths(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["run", "--help"])
    assert exit_.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "--scenarios" in out
    assert "Cap on the number of paths traced (default: 21)." in out


@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_a_cap_below_1_is_rejected_before_any_stage_runs(tmp_path, capsys, value):
    with pytest.raises(SystemExit) as exit_:
        main(
            [
                "run",
                str(tmp_path),
                "--trust-repo",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--scenarios",
                value,
            ]
        )
    assert exit_.value.code == 2
    assert "--scenarios" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


# --- a run over the cap ----------------------------------------------------------------------------


def test_25_commands_with_the_default_cap_trace_21_and_list_4_over_cap(
    tmp_path, monkeypatch, spy, capsys
):
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(TWENTY_FIVE))
    code, run_dir, md_path = _click_run(
        tmp_path, monkeypatch, agents, names=TWENTY_FIVE
    )
    assert code == 0
    result = spy.results[0]
    assert len(result.paths) == 21
    assert [d.reason for d in result.dropped] == ["over_cap"] * 4
    md = md_path.read_text()
    body = _paths_body(md)
    assert body.strip().splitlines()[0] == (
        "21 paths reach this module. 4 entries were left out; "
        "see **Left out** under Coverage."
    )
    coverage = section_texts(md, "Coverage")[0]
    assert re.search(r"^\| Entries left out \| 4 \|", coverage, re.MULTILINE)
    rows = _left_out_rows(coverage)
    assert len(rows) == 4
    for row, drop in zip(rows, result.dropped):
        loc = drop.entry.location
        assert row[0].startswith(f"`shop/{loc.path}:{loc.line}`")
        assert row[1:3] == ["cap", "`over_cap`"]
    assert "](" not in "\n".join(
        coverage.splitlines()[coverage.splitlines().index(LEFT_OUT_HEAD) :]
    )
    out = capsys.readouterr().out
    assert re.search(r"trace done: 21 paths, 0 cut short, 4 left out", out)
    assert "21 paths, 0 cut short, 4 left out" in (run_dir / "progress.log").read_text()
    assert main(["check", str(md_path)]) == 0  # the Left out entries are not links


def test_an_explicit_cap_keeps_at_most_that_many(tmp_path, monkeypatch, spy):
    names = ["sync", "push", "pull", "fetch", "merge"]
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(names))
    code, _, md_path = _click_run(
        tmp_path, monkeypatch, agents, "--scenarios", "3", names=names
    )
    assert code == 0
    assert len(spy.results[0].paths) == 3
    md = md_path.read_text()
    assert "2 entries were left out" in _paths_body(md)
    assert [r[2] for r in _left_out_rows(section_texts(md, "Coverage")[0])] == [
        "`over_cap`"
    ] * 2


# --- the Left out table, from a hand-built result ------------------------------------------------


def _drop(stage, reason, name, path, line, detail="why"):
    return PathDrop.model_construct(
        stage=stage,
        reason=reason,
        detail=detail,
        entry=_entry("seed", name, path, line),
    )


def _with_left_out(paths=True):
    base = _result()
    dropped = [
        _drop(
            "import_reachability",
            "unreachable",
            "GET /ping",
            "cli/later.py",
            1,
            "no import path | to a file",
        ),
        _drop("trace", "brand_new_reason", "sync2", "cli/main.py", 7),
    ]
    diagnostics = [
        PathDiagnostic(
            path="api/jobs.py", line=None, reason="seed_not_found", detail="no def poll"
        ),
        PathDiagnostic(
            path="cli/tool.py",
            line=10,
            reason="dynamic_name",
            detail="add_parser(name)",
        ),
        PathDiagnostic(
            path="cli/tool.py", line=3, reason="parse_error", detail="not listed"
        ),
    ]
    skips = [ListingSkip(file="tests/e2e/test_bad.py", reason="syntax_error")]
    return base.model_copy(
        update={
            "paths": base.paths if paths else [],
            "dropped": dropped,
            "diagnostics": diagnostics,
            "test_listing_skips": skips,
        }
    )


def test_left_out_rows_list_drops_then_diagnostics_then_listing_skips_verbatim():
    from walkthrough_pipeline.render import left_out_count, render_left_out

    text = render_left_out(_with_left_out(), "shop")
    assert text.splitlines()[0] == "### Left out"
    rows = _left_out_rows(text)
    assert [r[0].split(" ")[0] for r in rows] == [
        "`shop/cli/later.py:1`",
        "`shop/cli/main.py:7`",
        "`shop/api/jobs.py`",
        "`shop/cli/tool.py:10`",
        "`shop/tests/e2e/test_bad.py`",
    ]
    assert [r[1:3] for r in rows] == [
        ["import_reachability", "`unreachable`"],
        ["trace", "`brand_new_reason`"],
        ["discover", "`seed_not_found`"],
        ["discover", "`dynamic_name`"],
        ["e2e", "`e2e_listing`"],
    ]
    assert rows[0][3] == r"no import path \| to a file"
    assert rows[4][3] == "syntax_error"
    assert "parse_error" not in text
    assert "](" not in text
    # dynamic_name and e2e_listing rows are listed, not counted as entries left out.
    assert left_out_count(_with_left_out()) == 3


def test_no_left_out_rows_renders_no_subsection():
    from walkthrough_pipeline.render import left_out_count, render_left_out

    assert render_left_out(_result(), "shop") == ""
    assert left_out_count(_result()) == 0


def test_lead_sentences_count_entries_left_out(tmp_path):
    body = _render(_state(tmp_path, _with_left_out()), tmp_path / "out")
    assert body.splitlines()[0] == (
        "3 paths reach this module. 3 entries were left out; "
        "see **Left out** under Coverage."
    )
    empty = _render(
        _state(tmp_path / "b", _with_left_out(paths=False)), tmp_path / "out"
    )
    assert empty.splitlines()[0] == (
        "No paths were traced. 3 entries were left out; "
        "see **Left out** under Coverage."
    )


def test_coverage_has_the_entries_left_out_row_and_the_table(tmp_path):
    table = "\n".join(coverage_lines(_state(tmp_path, _with_left_out())))
    assert re.search(r"^\| Entries left out \| 3 \|", table, re.MULTILINE)
    assert "### Left out" in table and LEFT_OUT_HEAD in table


# --- the cap on resume (plan row S7) -----------------------------------------------------------------


def _saved(run_dir) -> dict:
    return json.loads((run_dir / STATE_FILE).read_text())


@pytest.mark.parametrize(
    "extra, cap, given", [((), 21, False), (("--scenarios", "3"), 3, True)]
)
def test_a_run_saves_whether_scenarios_was_passed(
    tmp_path, monkeypatch, spy, extra, cap, given
):
    code, run_dir, _ = _click_run(
        tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()), *extra
    )
    assert code == 0
    options = _saved(run_dir)["options"]
    assert (options["scenarios"], options["scenarios_given"]) == (cap, given)


@pytest.mark.parametrize(
    "saved, cap",
    [
        ({"scenarios": 6}, 21),  # saved before PR 6 with the old default
        ({"scenarios": 3}, 3),  # saved before PR 6; 3 was never a default
        ({"scenarios": 6, "scenarios_given": True}, 6),  # the user passed --scenarios 6
        (
            {"scenarios": 6, "scenarios_given": False},
            21,
        ),  # not passed: the default of today
    ],
)
def test_a_resume_before_trace_uses_the_saved_cap_only_when_scenarios_was_passed(
    tmp_path, monkeypatch, spy, saved, cap
):
    code, run_dir, md_path = _click_run(
        tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert code == 0
    state = _saved(run_dir)
    state["options"].pop("scenarios_given", None)
    state["options"].update(saved)
    state.update(stage="docs", paths=None, paths_error=None)  # stopped before trace
    (run_dir / STATE_FILE).write_text(json.dumps(state))

    monkeypatch.setattr(
        sdk, "agent_call_structured", FakeAgents(trace_plan=path_fixtures.click_plan())
    )
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert len(spy.calls) == 2 and spy.calls[1]["cap"] == cap
    assert (
        _saved(run_dir)["options"] == state["options"]
    )  # resume rewrites no saved option
