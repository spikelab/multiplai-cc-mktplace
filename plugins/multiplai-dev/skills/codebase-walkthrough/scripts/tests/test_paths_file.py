"""`<name>-paths.json`: the trace stage's result as a paths.v1 document beside the walkthrough, human mode only."""

from __future__ import annotations

import ast
import json
import re
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest

import code_facts
import fixture_repo
import path_fixtures
from code_facts import Seed
from fakes import FakeAgents
from test_paths_stage import VIEWER_SCHEMA, _click_run, _paths_body, _state, _trace_calls, spy  # noqa: F401
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE

SCHEMA = json.loads(files("code_facts").joinpath("schema", "paths.v1.schema.json").read_text())
BUILDME_WALKTHROUGHS = (Path(__file__).resolve().parents[3] / "buildme" / "scripts" / "build_pipeline"
                        / "walkthroughs.py")
ANCHOR_HOP = re.compile(r'<a id="(p\d+)h(\d+)"></a>`p\d+h\d+` \[`([^`]+):(\d+)(?:-(\d+))?`\]')


def _paths_json(md_path: Path) -> dict:
    doc = json.loads(md_path.with_name(md_path.name.replace("-walkthrough.md", "-paths.json")).read_text())
    jsonschema.validate(doc, SCHEMA)
    return doc


def test_paths_file_for_names_the_file_beside_the_walkthrough():
    from walkthrough_pipeline.render import paths_file_for
    assert paths_file_for(Path("/o/billing-walkthrough.md")) == Path("/o/billing-paths.json")


def test_a_run_writes_a_valid_paths_file_that_matches_the_paths_section(tmp_path, monkeypatch, spy, capsys):
    names = ["sync", "push"]
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan(names)),
                                  names=names)
    assert code == 0
    target = tmp_path / "out" / "billing-paths.json"
    doc = _paths_json(md_path)
    assert doc == json.loads(json.dumps(spy.results[0].model_dump(mode="json")))
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("walkthrough: ")]
    assert printed[0] == f"walkthrough: {md_path}" and f"walkthrough: {target}" in printed
    body = _paths_body(md_path.read_text())
    hops = {(m.group(1), int(m.group(2))): (m.group(3), int(m.group(4)), int(m.group(5) or m.group(4)))
            for m in ANCHOR_HOP.finditer(body)}
    assert len(hops) == sum(len(p["hops"]) for p in doc["paths"])
    assert f"{len(doc['paths'])} paths reach this module." in body
    for p in doc["paths"]:
        for k, hop in enumerate(p["hops"], start=1):
            # Repo-relative in the JSON, prefixed with the repo key in the Markdown.
            assert hops[(p["id"], k)] == (f"shop/{hop['path']}", hop["line_start"], hop["line_end"])
        block = body[body.index(f"**{p['id']}** — "):]
        tests = block[block.index("- E2E tests: "):].splitlines()[0]
        assert ("no e2e test" in tests) == (not p["tests"]["e2e"])
        assert tests.count("not counted)") == len(p["tests"]["mocks_hop_2"])


def test_a_skill_command_run_writes_its_paths_file(tmp_path, monkeypatch, spy):
    ws = tmp_path / "ws"
    module = path_fixtures.skill_repo(ws)
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents(trace_plan=path_fixtures.skill_plan()))
    assert main(path_fixtures.run_args(module, ws, tmp_path)) == 0
    doc = _paths_json(tmp_path / "out" / "t-walkthrough.md")
    assert "skill_command" in [p["entry"]["kind"] for p in doc["paths"]]


def test_an_empty_result_still_writes_a_valid_file(tmp_path, monkeypatch, spy):
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(), names=())
    assert code == 0
    doc = _paths_json(md_path)
    assert doc["paths"] == [] and doc["dropped"] == []


def test_left_out_entries_are_in_the_paths_file(tmp_path, monkeypatch, spy):
    from walkthrough_pipeline.stages import trace
    real = trace.web_seeds
    monkeypatch.setattr(trace, "web_seeds", lambda state: [*real(state),
                                                          Seed(path="billing/core.py", symbol="gone", name="GET /x")])
    names = ["sync", "push", "pull", "fetch"]
    agents = FakeAgents(trace_plan=path_fixtures.click_plan(names), fail_on="- name: pull\n")
    code, _, md_path = _click_run(tmp_path, monkeypatch, agents, "--scenarios", "2", names=names)
    assert code == 0
    doc = _paths_json(md_path)
    reasons = {d["entry"]["name"]: d["reason"] for d in doc["dropped"]}
    assert reasons.pop("pull") == "call_failed"
    assert list(reasons.values()) == ["over_cap"]
    assert [(d["reason"], d["path"]) for d in doc["diagnostics"]] == [("seed_not_found", "billing/core.py")]


def test_a_paths_file_write_failure_keeps_the_walkthrough(tmp_path, monkeypatch, spy):
    def fail(result, path):
        raise OSError("disk full")

    monkeypatch.setattr(code_facts, "write_paths_file", fail)
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    target = tmp_path / "out" / "billing-paths.json"
    assert md_path.exists() and not target.exists()
    assert f"paths file {target}: OSError: disk full" in _state(run_dir)["errors"]
    assert str(target) not in _state(run_dir)["outputs"]


def test_a_whole_stage_failure_removes_a_stale_paths_file(tmp_path, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("no index")

    stale = tmp_path / "out" / "billing-paths.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    monkeypatch.setattr(code_facts, "map_paths", boom)
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents())
    assert code == 0
    assert md_path.exists() and not stale.exists()


def test_agent_mode_writes_no_paths_file(tmp_path, monkeypatch, spy):
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()),
                                  "--mode", "agent")
    assert code == 0
    assert md_path.exists() and not list((tmp_path / "out").glob("*-paths.json"))


def test_a_rerun_after_the_module_changes_rewrites_the_file(tmp_path, monkeypatch, spy):
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    first = _paths_json(md_path)
    repo = tmp_path / "ws" / "shop"
    core = repo / "billing" / "core.py"
    core.write_text("# rates\n" + core.read_text())
    sha = fixture_repo.commit(repo, "move charge down")
    agents = FakeAgents(trace_plan=path_fixtures.click_plan())
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(path_fixtures.run_args(repo / "billing", tmp_path / "ws", tmp_path)) == 0
    second = _paths_json(md_path)
    assert first["commit"] != second["commit"] == sha
    assert second["paths"][0]["hops"][-1]["line_start"] == first["paths"][0]["hops"][-1]["line_start"] + 1


def test_a_fully_cached_resume_writes_a_byte_identical_file(tmp_path, monkeypatch, spy):
    plan = path_fixtures.click_plan(["sync", "push"])
    code, run_dir, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=plan), names=("sync", "push"))
    assert code == 0
    target = tmp_path / "out" / "billing-paths.json"
    before = target.read_bytes()
    state = _state(run_dir)
    state["stage"] = "docs"
    state.pop("paths")
    (run_dir / STATE_FILE).write_text(json.dumps(state))
    target.unlink()
    again = FakeAgents(trace_plan=plan)
    monkeypatch.setattr(sdk, "agent_call_structured", again)
    assert main(["resume", str(run_dir), "--trust-repo"]) == 0
    assert _trace_calls(again) == []
    assert target.read_bytes() == before


def test_the_walkthrough_json_does_not_refer_to_the_paths_file(tmp_path, monkeypatch, spy):
    code, _, md_path = _click_run(tmp_path, monkeypatch, FakeAgents(trace_plan=path_fixtures.click_plan()))
    assert code == 0
    text = md_path.with_suffix(".json").read_text()
    jsonschema.validate(json.loads(text), json.loads(VIEWER_SCHEMA.read_text()))
    assert "paths.json" not in text


def test_buildme_scans_only_the_markdown(tmp_path):
    if not BUILDME_WALKTHROUGHS.exists():
        pytest.skip("buildme is not in this checkout")
    tree = ast.parse(BUILDME_WALKTHROUGHS.read_text())
    suffix = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "SUFFIX" for t in n.targets))
    assert f'directory.glob(f"*{{SUFFIX}}")' in BUILDME_WALKTHROUGHS.read_text()
    (tmp_path / "x-walkthrough.md").write_text("# x\n")
    (tmp_path / "x-paths.json").write_text("{}")
    assert [p.name for p in tmp_path.glob(f"*{suffix}")] == ["x-walkthrough.md"]
