"""`run --scope codebase` (and `--lib`), through `main()` with fake agents, on `plugin_fixture`.

Stop and resume rows of `INBOX/plan-walkthrough-codebase-scope-2026-10-23.md` covered here: S1, S2,
S3, S5, S6, S7 and S8. The others need no new test:
- S4 (a budget stop mid write): the write stage is unchanged; `test_resume.py` covers it.
- S9 (the working tree edited): every read is from the commits saved in the state, as for a module.
- S10 (a library's HEAD moved): the library's sha is saved at the start and read from then on, as the
  base commit of a change is (`test_change_scope.py`).
- S11 (stopped twice): each resume continues from the last saved answer, as S1 shows once.
- S12 (the stopped process still alive): no lock, as for every run.
"""

from __future__ import annotations

import json
import re

import plugin_fixture
import pytest
from fakes import FakeAgents
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import markdown_headings
from walkthrough_pipeline.state import STATE_FILE, load_state

CODEBASE_H2 = [
    "What it is",
    "Which command to run",
    "Use cases",
    "How it works",
    "Loops and caps",
    "Exit codes and markers",
    "State and resume",
    "What it creates",
    "Configuration",
    "Gotchas",
    "Reference",
    "How this was made",
]
TABLES = {
    "Which command to run": "`tool build --run-dir <dir>`",
    "Loops and caps": "`3 (MAX_TRIES)`",
    "Exit codes and markers": "`exit 2`",
    "State and resume": "A crash mid build",
}


@pytest.fixture
def pws(tmp_path):
    return plugin_fixture.build(tmp_path / "pws")


def _args(pws, tmp_path, *extra, runs="runs", out="out"):
    return [
        "run",
        str(pws["tool"]),
        "--scope",
        "codebase",
        "--repos",
        str(pws["kit"]),
        "--trust-repo",
        "--runs-dir",
        str(tmp_path / runs),
        "--output",
        str(tmp_path / out),
        "--lsp-timeout",
        "0",
        *extra,
    ]


def _section(md: str, title: str) -> str:
    return md.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def _no_run_line(md: str) -> str:
    """The Markdown without its spend line, which counts this process's calls."""
    return "\n".join(l for l in md.split("\n") if not l.startswith("> Generated"))


def _survey_calls(agents: FakeAgents) -> list[dict]:
    return [c for c in agents.calls if "Survey question: [survey:" in c["prompt"]]


def _facts(run_dir) -> list[tuple]:
    state = load_state(run_dir / STATE_FILE)
    return [
        (f.id, f.claim, f.source, [(c.id, c.path, c.line_start) for c in f.citations])
        for f in state.facts
    ]


def test_codebase_run_writes_the_twelve_sections_and_check_passes(
    pws, tmp_path, monkeypatch, capsys
):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path, "--lib", str(pws["corelib"] / "corelib"))) == 0
    md_path = tmp_path / "out" / "tool-walkthrough.md"
    md = md_path.read_text()
    assert markdown_headings(md) == CODEBASE_H2
    html = md_path.with_suffix(".html").read_text()
    assert [
        re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h2>(.*?)</h2>", html)
    ] == CODEBASE_H2
    for title, row in TABLES.items():
        assert row in _section(md, title), title
        assert "Nothing about this passed the checks." not in _section(md, title)
    # The config row cites a docs table line holding `|`; check below re-reads its quote.
    assert "| `--run-dir` | Where the build keeps its state." in _section(
        md, "Configuration"
    )
    # The three survey questions were asked once each, with the docs and the library listed.
    calls = _survey_calls(agents)
    assert sorted(
        re.search(r"\[survey:(\w+)\]", c["prompt"]).group(1) for c in calls
    ) == [
        "commands",
        "lifecycle",
        "loops",
    ]
    assert {c["budget_label"] for c in calls} == {
        "survey:commands",
        "survey:lifecycle",
        "survey:loops",
    }
    commands = next(c for c in calls if "[survey:commands]" in c["prompt"])["prompt"]
    tool = "kit/plugins/demo/skills/tool"
    for doc in (
        f"{tool}/SKILL.md",
        f"{tool}/scripts/CLAUDE.md",
        "kit/plugins/demo/README.md",
        "kit/plugins/demo/CHANGELOG.md",
    ):
        assert f"- {doc}\n" in commands, doc
    assert "- kit/README.md\n" not in commands  # the plugin's README.md is nearer
    assert "- corelib/corelib/\n" in commands
    # The overview default skipped the test file.
    run_dir = next((tmp_path / "runs").iterdir())
    state = load_state(run_dir / STATE_FILE)
    assert state.options["depth"] == "overview"
    assert (
        state.target.skipped["plugins/demo/skills/tool/scripts/tests/test_tool.py"]
        == "test file (depth overview)"
    )
    assert run_dir.name.endswith("--codebase")
    # The library: its facts cite it, and it is listed in How this was made.
    assert any(
        c.path.startswith("corelib/corelib/log.py")
        for f in state.facts
        for c in f.citations
    )
    made = _section(md, "How this was made")
    assert "| corelib |" in made and "- Library read: `corelib/corelib` at `" in made
    assert "- Scope: codebase" in made
    # The loops survey's gotcha reaches the Gotchas section's material.
    assert any(f.source == "survey:loops/gotcha" for f in state.facts)
    capsys.readouterr()
    assert main(["check", str(md_path)]) == 0


def test_the_planted_rows_are_cut_and_listed(pws, tmp_path, monkeypatch):
    agents = FakeAgents(plant={"command_flag", "exit_number", "loop_cap"})
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path)) == 0
    md = (tmp_path / "out" / "tool-walkthrough.md").read_text()
    assert "--no-such-flag" not in _section(md, "Which command to run")
    assert "exit 9" not in _section(md, "Exit codes and markers")
    loops = _section(md, "Loops and caps")
    assert "The build attempts" in loops and "7 (" not in loops
    cut = md.split("### Cut by the checks", 1)[1]
    assert "`--no-such-flag` in the command is in no cited quote and no snapshot" in cut
    assert "`9` in the signal is in no cited quote and no snapshot" in cut
    assert "the cap is in no cited quote and no snapshot" in cut


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--lib", "x"], "--lib goes with --scope codebase"),
        (
            ["--scope", "codebase", "--mode", "agent"],
            "--scope codebase writes the human-mode walkthrough only",
        ),
        (["--scope", "codebase", "--pr", "3"], "--pr goes with --scope pr"),
    ],
)
def test_argparse_refuses_the_wrong_flags(pws, extra, message, capsys):
    with pytest.raises(SystemExit) as e:
        main(["run", str(pws["tool"]), *extra])
    assert e.value.code == 2
    assert message in capsys.readouterr().err


def test_a_module_run_asks_no_survey_question(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 0
    assert not _survey_calls(agents)
    md = (tmp_path / "out" / "bookings-walkthrough.md").read_text()
    assert "Which command to run" not in markdown_headings(md)
    run_dir = next((tmp_path / "runs").iterdir())
    assert run_dir.name.endswith("--human")
    assert load_state(run_dir / STATE_FILE).options["depth"] == "standard"


def test_s1_a_budget_stop_mid_survey_resumes_to_the_same_facts(
    pws, tmp_path, monkeypatch, capsys
):
    """S1: the answers saved before the stop are kept; resume asks only the others, and the facts,
    their ids and the walkthrough equal a run that never stopped."""
    whole = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", whole)
    assert main(_args(pws, tmp_path, runs="runs-a", out="out-a")) == 0

    agents = FakeAgents(stop_survey_after=1)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path, runs="runs-b", out="out-b")) == 2
    assert "planted stop during survey" in capsys.readouterr().err
    run_dir = next((tmp_path / "runs-b").iterdir())
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "explore"
    saved = [k for k in state["explore_answers"] if k.startswith("survey:")]
    assert 1 <= len(saved) <= 2

    agents.stop_survey_after = None
    asked = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    resumed = [
        re.search(r"\[(survey:\w+)\]", c["prompt"]).group(1)
        for c in agents.calls[asked:]
        if "Survey question: [survey:" in c["prompt"]
    ]
    assert sorted(resumed) == sorted(
        {"survey:loops", "survey:lifecycle", "survey:commands"} - set(saved)
    )
    assert _facts(run_dir) == _facts(next((tmp_path / "runs-a").iterdir()))
    assert _no_run_line(
        (tmp_path / "out-b" / "tool-walkthrough.md").read_text()
    ) == _no_run_line((tmp_path / "out-a" / "tool-walkthrough.md").read_text())


def test_s2_a_failed_survey_task_is_recorded_and_the_run_finishes(
    pws, tmp_path, monkeypatch
):
    """S2: the failed task's answer is an error, recorded in `errors`; the run finishes and its table
    is written from the facts explore found."""
    agents = FakeAgents(fail_on="[survey:lifecycle]")
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    state = load_state(run_dir / STATE_FILE)
    assert "error" in state.explore_answers["survey:lifecycle"]
    assert any(e.startswith("explore survey:lifecycle") for e in state.errors)
    md = (tmp_path / "out" / "tool-walkthrough.md").read_text()
    assert markdown_headings(md) == CODEBASE_H2


def _rewind(run_dir, stage: str) -> None:
    state = json.loads((run_dir / STATE_FILE).read_text())
    state["stage"] = stage
    (run_dir / STATE_FILE).write_text(json.dumps(state))


def test_s3_a_kill_after_the_survey_before_its_save_asks_nothing_again(
    pws, tmp_path, monkeypatch
):
    """S3: the three answers are saved but the stage is still `explore`; resume runs the survey again,
    asks nothing, and writes the same walkthrough."""
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    md_path = tmp_path / "out" / "tool-walkthrough.md"
    before, facts = md_path.read_text(), _facts(run_dir)
    _rewind(run_dir, "explore")
    asked = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert not [
        c for c in agents.calls[asked:] if "Survey question: [survey:" in c["prompt"]
    ]
    assert _facts(run_dir) == facts
    assert _no_run_line(md_path.read_text()) == _no_run_line(before)


def test_s5_a_module_state_from_before_the_survey_resumes_without_asking(
    done_run, monkeypatch
):
    """S5: a module run's state at `explore` (as the version before the survey saved it) resumes through
    the survey without a call and keeps its headings."""
    md_path, run_dir, agents = done_run
    headings = markdown_headings(md_path.read_text())
    _rewind(run_dir, "explore")
    asked = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert not [
        c for c in agents.calls[asked:] if "Survey question: [survey:" in c["prompt"]
    ]
    assert load_state(run_dir / STATE_FILE).stage == "done"
    assert markdown_headings(md_path.read_text()) == headings


def test_s6_rewrite_keeps_the_survey_and_writes_the_tables_again(
    pws, tmp_path, monkeypatch
):
    """S6: `resume --rewrite` keeps the explore, survey, docs and trace answers and asks the writers
    again, the four tables included."""
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    asked = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo", "--rewrite"]) == 0
    again = agents.calls[asked:]
    assert not [c for c in again if c["schema"] == "ExploreOutput"]
    assert {"CommandsOutput", "LoopsOutput", "ExitsOutput", "ResumeOutput"} <= {
        c["schema"] for c in again
    }
    md = (tmp_path / "out" / "tool-walkthrough.md").read_text()
    assert markdown_headings(md) == CODEBASE_H2


def test_s7_render_writes_the_twelve_sections_with_no_call(pws, tmp_path, monkeypatch):
    """S7: `render` on a finished codebase run writes the same twelve H2s and asks no agent."""
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(_args(pws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    asked = len(agents.calls)
    assert main(["render", str(run_dir), "--output", str(tmp_path / "again")]) == 0
    assert len(agents.calls) == asked
    md = (tmp_path / "again" / "tool-walkthrough.md").read_text()
    assert markdown_headings(md) == CODEBASE_H2
    assert _no_run_line(md) == _no_run_line(
        (tmp_path / "out" / "tool-walkthrough.md").read_text()
    )


def test_s8_a_lib_that_does_not_resolve_leaves_nothing(
    pws, tmp_path, monkeypatch, capsys
):
    """S8: a `--lib` that is not a directory in a git repository stops the run before its directory
    exists."""
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    outside = tmp_path / "plain"
    outside.mkdir()
    for lib, why in (
        (tmp_path / "nowhere", "not a directory"),
        (outside, "not inside a git repository"),
    ):
        assert main(_args(pws, tmp_path, "--lib", str(lib))) == 1
        assert why in capsys.readouterr().err
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())
