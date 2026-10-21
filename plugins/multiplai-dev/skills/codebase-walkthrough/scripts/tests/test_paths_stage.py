"""The trace stage through `code_facts.map_paths`: whole runs on git fixture repos with fake agents.

`map_paths` is the real one, wrapped by a spy that records its arguments and
result; only `sdk.agent_call_structured` is faked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import jsonschema
import pytest

import code_facts
import path_fixtures
from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.config import DEFAULT_CONCURRENCY
from walkthrough_pipeline.models import SectionOutput, TargetInfo, WalkState
from walkthrough_pipeline.render import markdown_headings, section_texts
from walkthrough_pipeline.state import STATE_FILE, load_state

HOP_ID = re.compile(r"p\d+h\d+")
TRACE_SCHEMAS = ("TraceAnswer", "FallbackAnswer")
FIVE = ["sync", "push", "pull", "fetch", "merge"]
VIEWER_SCHEMA = Path(__file__).resolve().parents[3] / "review-viewer" / "schema" / "walkthrough.v1.schema.json"


def _git_head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def _click_run(tmp_path, monkeypatch, agents, *extra, names=("sync",)):
    """`run` on the click fixture's `billing/` module; (exit code, run dir, Markdown path)."""
    ws = tmp_path / "ws"
    module = path_fixtures.click_repo(ws, list(names))
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    code = main(path_fixtures.run_args(module, ws, tmp_path, *extra))
    return code, next((tmp_path / "runs").iterdir()), tmp_path / "out" / "billing-walkthrough.md"


def _paths_body(md: str) -> str:
    bodies = section_texts(md, "Paths")
    assert len(bodies) == 1, markdown_headings(md)
    return bodies[0]


def _state(run_dir: Path) -> dict:
    return json.loads((run_dir / STATE_FILE).read_text())


def _trace_calls(agents: FakeAgents, since: int = 0) -> list[dict]:
    return [c for c in agents.calls[since:] if c["schema"] in TRACE_SCHEMAS]


def _answer_files(run_dir: Path) -> dict[str, bytes]:
    answers = run_dir / "paths-cache" / "paths-answers"
    return {p.name: p.read_bytes() for p in answers.glob("*.json")} if answers.is_dir() else {}


def _trace_answer_files(run_dir: Path) -> dict[str, bytes]:
    return {n: b for n, b in _answer_files(run_dir).items() if "hops" in json.loads(b)}


def _conf(text: str) -> None:
    home = Path(os.environ["CLAUDE_MULTIPLAI_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "multiplai.conf").write_text(text)


# --- the map_paths call ------------------------------------------------------------------------


def test_map_paths_is_called_once_with_the_module_as_changed_lines(tmp_path, monkeypatch, spy):
    code, run_dir, _ = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    assert len(spy.calls) == 1
    kw = spy.calls[0]
    assert Path(kw["root"]).resolve() == (run_dir / "snap" / "shop").resolve()
    assert kw["commit"] == _git_head(tmp_path / "ws" / "shop")
    # billing/__init__.py is empty: never a key. cli/ is outside the module.
    assert kw["changed_lines"] == {"billing/core.py": {1, 2, 3, 4, 5}}
    assert list(kw["seeds"]) == [] and list(kw["places"]) == []
    assert kw["trace_limit"] == 30
    assert kw["concurrency"] == DEFAULT_CONCURRENCY
    assert Path(kw["cache_dir"]) == run_dir / "paths-cache"
    # The resolver takes the repo-relative paths map_paths cites.
    found = kw["resolve"]("cli/main.py", "head")
    assert found is not None and found[1:] == (kw["commit"], "cli/main.py")
    assert Path(found[0]).resolve() == (tmp_path / "ws" / "shop").resolve()


def test_trace_calls_go_through_the_sdk_read_only_on_the_trace_model_and_count_against_the_ledger(
        tmp_path, monkeypatch, spy):
    _conf("walkthrough_trace_model = trace-model\nwalkthrough_explore_model = explore-model\n"
          "walkthrough_effort = high\n")
    agents = FakeAgents(cost=0.01, trace_plan=path_fixtures.click_plan())
    code, run_dir, _ = _click_run(tmp_path, monkeypatch, agents)
    assert code == 0
    calls = _trace_calls(agents)
    assert any(c["schema"] == "TraceAnswer" for c in calls)
    snapshot = (run_dir / "snap" / "shop").resolve()
    for c in calls:
        assert c["allowed_tools"] == sdk.TRACE_TOOLS
        assert not set(c["allowed_tools"]) & {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"}
        assert (c["model"], c["effort"]) == ("trace-model", "high")
        assert Path(c["cwd"]).resolve() == snapshot
        # Reads stop at the target repo's snapshot, not just start there.
        assert [Path(r).resolve() for r in c["read_roots"]] == [snapshot]
        assert c["budget_label"] == "trace"
    by_label = _state(run_dir)["budget"]["by_label"]
    assert by_label["trace"] == pytest.approx(0.01 * len(calls))


def test_scenarios_caps_the_paths(tmp_path, monkeypatch, spy):
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(FIVE))
    code, _, md_path = _click_run(tmp_path, monkeypatch, agents, "--scenarios", "3", names=FIVE)
    assert code == 0
    assert spy.calls[0]["cap"] == 3
    assert len(spy.results[0].paths) == 3
    body = _paths_body(md_path.read_text())
    assert "3 paths reach this module." in body
    assert "**p3** — " in body and "**p4**" not in body


# --- the rendered Paths section ------------------------------------------------------------------


def test_click_cli_run_renders_the_paths_section(tmp_path, monkeypatch, spy):
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    md = md_path.read_text()
    heads = markdown_headings(md, 3)
    assert heads.count("Paths") == 1 and "End-to-end scenarios" not in heads + markdown_headings(md)
    assert heads.index("Paths") + 1 == heads.index("Module tour")
    body = _paths_body(md)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    assert lines[0] == "1 path reaches this module."
    assert "**p1** — `sync` (click command)" in lines
    invoker = spy.results[0].paths[0].entry.invoked_by
    entry = next(line for line in lines if line.startswith("- Entry: invoked by "))
    # Hop 1 is the handler's `def` (line 7); the entry is declared at line 6, so it gets its own link.
    assert entry.startswith(f"- Entry: invoked by `{invoker}`")
    assert re.search(r'\[`shop/cli/main\.py:6`\]\(\S* "[^"]*"\)$', entry)
    assert re.search(r'<a id="p1h1"></a>`p1h1` \[`shop/cli/main\.py:7`\]\(', body)
    assert re.search(r'<a id="p1h2"></a>`p1h2` \[`shop/billing/core\.py:4`\]\(', body)
    assert body.index('id="p1h1"') < body.index('id="p1h2"')
    assert "- Ends at `p1h2` (trace complete)." in lines
    assert "no e2e test" in body
    coverage = section_texts(md, "Coverage")[0]
    assert re.search(r"^\| Paths traced \| 1\b", coverage, re.M)
    assert re.search(r"^\| Paths cut short \| 0\b", coverage, re.M)
    assert "Scenarios cut short" not in md
    assert main(["check", str(md_path)]) == 0           # every hop link re-reads at the commit


def test_paths_are_rendered_in_the_order_map_paths_returns(tmp_path, monkeypatch, spy):
    def reverse(result):
        paths = [p.model_copy(update={"id": f"p{n}"}) for n, p in enumerate(reversed(result.paths), start=1)]
        return result.model_copy(update={"paths": paths})

    spy.reorder = reverse
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(["sync", "push"]))
    code, _, md_path = _click_run(tmp_path, monkeypatch, agents, names=("sync", "push"))
    assert code == 0
    assert [p.entry.name for p in spy.results[0].paths] == ["push", "sync"]
    body = _paths_body(md_path.read_text())
    assert body.index("**p1** — `push`") < body.index("**p2** — `sync`")
    assert re.search(r'<a id="p1h1"></a>`p1h1` \[`shop/cli/main\.py:12`\]', body)   # push's handler


def test_skill_command_fixture_traces_a_skill_command_path(tmp_path, monkeypatch, spy):
    ws = tmp_path / "ws"
    module = path_fixtures.skill_repo(ws)
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents(trace_plan=path_fixtures.skill_plan()))
    assert main(path_fixtures.run_args(module, ws, tmp_path)) == 0
    assert spy.calls[0]["changed_lines"] == {
        "skills/t/SKILL.md": set(range(1, 8)),
        "skills/t/tool/__main__.py": {1, 2, 3},
        "skills/t/tool/core.py": {1, 2},
    }
    kinds = [p.entry.kind for p in spy.results[0].paths]
    assert "skill_command" in kinds
    n = kinds.index("skill_command") + 1
    body = _paths_body((tmp_path / "out" / "t-walkthrough.md").read_text())
    assert f"**p{n}** — `{path_fixtures.SKILL_NAME}` (skill command)" in body
    assert re.search(rf'<a id="p{n}h1"></a>`p{n}h1` \[`kit/skills/t/SKILL\.md:6`\]', body)


def test_paths_section_renders_when_the_writers_return_nothing(tmp_path, monkeypatch, spy):
    class SilentWriters(FakeAgents):
        def write(self, prompt):
            return SectionOutput()

    code, _, md_path = _click_run(tmp_path, monkeypatch, SilentWriters(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    body = _paths_body(md_path.read_text())
    assert "1 path reaches this module." in body
    assert '<a id="p1h1"></a>`p1h1`' in body and '<a id="p1h2"></a>`p1h2`' in body


def test_walkthrough_json_titles_the_step_paths(tmp_path, monkeypatch, spy):
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    wt = json.loads(md_path.with_suffix(".json").read_text())
    jsonschema.validate(wt, json.loads(VIEWER_SCHEMA.read_text()))
    titles = [s["title"] for s in wt["steps"]]
    assert titles.count("Paths") == 1 and "End-to-end scenarios" not in titles
    step = next(s for s in wt["steps"] if s["title"] == "Paths")
    assert {"path": "billing/core.py", "side": "head", "line_start": 4, "line_end": 4} in step["anchors"]


# --- agent mode -------------------------------------------------------------------------------------


def test_agent_mode_runs_no_trace(tmp_path, monkeypatch, spy):
    agents = FakeAgents(trace_plan=path_fixtures.click_plan())
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, agents, "--mode", "agent")
    assert code == 0
    assert spy.calls == []
    assert _trace_calls(agents) == []
    md = md_path.read_text()
    assert "Paths" not in markdown_headings(md)
    assert not HOP_ID.search(md)
    cache = run_dir / "paths-cache"
    assert not cache.exists() or not any(cache.rglob("*"))
    assert _state(run_dir)["paths"] is None


# --- state, cache and resume ---------------------------------------------------------------------------


def test_saved_state_holds_the_paths_and_no_legacy_trace_fields(tmp_path, monkeypatch, spy):
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(["sync", "push"]))
    code, run_dir, _ = _click_run(tmp_path, monkeypatch, agents, names=("sync", "push"))
    assert code == 0
    state = _state(run_dir)
    assert "trace_answers" not in state and "scenarios" not in state
    saved = code_facts.PathsResult.model_validate(state["paths"])
    assert saved == spy.results[0]


def test_a_budget_stop_during_trace_keeps_the_answers_and_resume_asks_only_the_missing(
        tmp_path, monkeypatch, spy, capsys):
    plan = path_fixtures.click_plan(FIVE)
    first = FakeAgents(trace_plan=plan, stop_trace_after=3)
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, first, names=FIVE)
    assert code == 2
    assert "STOPPED: circuit breaker" in capsys.readouterr().err
    assert _state(run_dir)["stage"] == "docs"
    paid = _trace_answer_files(run_dir)
    assert len(paid) == 3

    second = FakeAgents(trace_plan=plan)
    monkeypatch.setattr(sdk, "agent_call_structured", second)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert [c["schema"] for c in _trace_calls(second)] == ["TraceAnswer", "TraceAnswer"]
    after = _trace_answer_files(run_dir)
    assert len(after) == 5
    assert {n: after[n] for n in paid} == paid             # the cached answers are used unchanged
    body = _paths_body(md_path.read_text())
    assert "5 paths reach this module." in body
    assert all(f"**p{n}** — " in body for n in range(1, 6))


def test_a_fully_cached_resume_asks_nothing_and_renders_the_same_hop_ids(tmp_path, monkeypatch, spy):
    plan = path_fixtures.click_plan(["sync", "push"])
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=plan), names=("sync", "push"))
    assert code == 0
    before = _paths_body(md_path.read_text())
    # As if the run stopped after the last answer was cached but before trace was checkpointed.
    state = _state(run_dir)
    state["stage"] = "docs"
    state.pop("paths")
    (run_dir / STATE_FILE).write_text(json.dumps(state))

    again = FakeAgents(trace_plan=plan)
    monkeypatch.setattr(sdk, "agent_call_structured", again)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert _trace_calls(again) == []
    assert len(spy.calls) == 2
    after = _paths_body(md_path.read_text())
    assert HOP_ID.findall(after) == HOP_ID.findall(before) and HOP_ID.findall(after)
    assert after == before


def test_a_resume_after_trace_does_not_trace_again(tmp_path, monkeypatch, spy):
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    before = _paths_body(md_path.read_text())
    state = _state(run_dir)
    state["stage"] = "trace"                            # as if the run stopped during write
    (run_dir / STATE_FILE).write_text(json.dumps(state))

    again = FakeAgents(trace_plan=path_fixtures.click_plan())
    monkeypatch.setattr(sdk, "agent_call_structured", again)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert len(spy.calls) == 1 and _trace_calls(again) == []
    assert _paths_body(md_path.read_text()) == before   # rendered from the saved result


def test_a_new_run_on_the_same_commit_traces_again(tmp_path, monkeypatch, spy):
    """Only `resume` reuses stored answers: `run` starts a fresh state, so its traces are fresh too."""
    code, run_dir, _ = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0 and _trace_answer_files(run_dir)

    again = FakeAgents(trace_plan=path_fixtures.click_plan())
    monkeypatch.setattr(sdk, "agent_call_structured", again)
    module = tmp_path / "ws" / "shop" / "billing"
    assert main(path_fixtures.run_args(module, tmp_path / "ws", tmp_path)) == 0
    assert next((tmp_path / "runs").iterdir()) == run_dir           # the same run directory
    assert any(c["schema"] == "TraceAnswer" for c in _trace_calls(again))


# --- module_lines ----------------------------------------------------------------------------------


def _walk_state(files: dict[str, str | bytes], tmp_path: Path) -> tuple[WalkState, Path]:
    """A state whose target lists `files`; they exist only under the returned snapshot root."""
    root = tmp_path / "snap" / "shop"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text if isinstance(text, bytes) else text.encode())
    target = TargetInfo(repo_key="shop", repo_path=str(tmp_path / "not-the-snapshot"), subpath="billing",
                        head_sha="0" * 40, name="billing", package="billing", files=list(files))
    return WalkState(target=target), root


def test_module_lines_maps_each_module_file_to_all_its_lines(tmp_path):
    from walkthrough_pipeline.stages.trace import module_lines
    state, root = _walk_state({"billing/a.py": "x = 1\n" * 40, "billing/b.py": "y = 2\n" * 12,
                               "billing/__init__.py": ""}, tmp_path)
    (root / "cli.py").write_text("import billing\n")    # in the repo, outside the module
    assert module_lines(state, root) == {"billing/a.py": set(range(1, 41)), "billing/b.py": set(range(1, 13))}


def test_module_lines_of_empty_files_is_an_empty_dict_not_none(tmp_path):
    from walkthrough_pipeline.stages.trace import module_lines
    state, root = _walk_state({"billing/__init__.py": "", "billing/empty.py": ""}, tmp_path)
    assert module_lines(state, root) == {}


@pytest.mark.parametrize("data, lines", [
    (b"a = 1\nb = 2", {1, 2}),           # no newline at the end: a hop on the last line is still in scope
    (b"\n", {1}),
    (b"x = '\xe9'\ny = 1\n", {1, 2}),   # not UTF-8: counted, not an error that stops the stage
])
def test_module_lines_counts_every_line_of_unusual_files(tmp_path, data, lines):
    from walkthrough_pipeline.stages.trace import module_lines
    state, root = _walk_state({"billing/odd.py": data}, tmp_path)
    assert module_lines(state, root) == {"billing/odd.py": lines}


# --- the code it replaces -----------------------------------------------------------------------------


def test_the_walkthrough_tracing_code_is_gone():
    from walkthrough_pipeline import gates, models, prompts
    from walkthrough_pipeline.stages import trace
    assert trace.TRACE_LIMIT == 30
    assert not hasattr(trace, "pick_seeds")
    assert not hasattr(gates, "trace_gate")
    assert not hasattr(prompts, "trace_scenario")
    assert not hasattr(models, "Scenario") and not hasattr(models, "ScenarioHop")
    assert "trace_answers" not in WalkState.model_fields and "scenarios" not in WalkState.model_fields
    assert WalkState.model_fields["paths"].default is None


def test_a_state_file_written_before_the_change_still_loads(tmp_path):
    state, _ = _walk_state({"billing/core.py": "RATE = 2\n"}, tmp_path)
    old = {**state.model_dump(mode="json"), "stage": "docs",
           "trace_answers": {"s1": {"title": "A webhook arrives", "hops": []}},
           "scenarios": [{"seed": {"path": "shop/urls.py"}, "hops": [], "stops_at": 0, "break_reason": "x"}]}
    old.pop("paths", None)
    (tmp_path / STATE_FILE).write_text(json.dumps(old))
    loaded = load_state(tmp_path / STATE_FILE)
    assert loaded is not None and loaded.stage == "docs" and loaded.paths is None
    dumped = loaded.model_dump()
    assert "trace_answers" not in dumped and "scenarios" not in dumped


# --- SKILL.md and the release ---------------------------------------------------------------------------

DEV = Path(__file__).resolve().parents[4]                  # plugins/multiplai-dev
SKILL_MD = DEV / "skills" / "codebase-walkthrough" / "SKILL.md"


def test_skill_md_stage_5_describes_map_paths():
    text = SKILL_MD.read_text()
    stage5 = text[text.index("5. **trace**"):text.index("6. **write**")]
    assert "map_paths" in stage5
    assert "webhooks first" not in stage5
