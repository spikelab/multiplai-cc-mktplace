"""The report's `## Paths` section: each mapped path's entry, invokers, findings on it and e2e tests, then a gap line.

Built from the state, `paths.json` (through `paths_io`) and the final
findings only: the tests patch `map_paths`, the model calls and every way to
start a process to raise.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess

import code_facts
import code_facts.paths as code_facts_paths
import pytest
from code_facts.paths import (
    E2EListing,
    E2ETest,
    FallbackStatus,
    Handler,
    Location,
    MockedE2ETest,
    PathEntry,
    PathHop,
    PathsResult,
    TracedPath,
    write_paths_file,
)
from review_pipeline import sdk
from review_pipeline.__main__ import main
from review_pipeline.export import to_findings_file
from review_pipeline.models import CoverageRecord
from review_pipeline.render import render_review, write_review
from test_reported_finders import make_repo, mapped, model, resume, review, saved  # noqa: F401


def new(name: str):
    return getattr(importlib.import_module("review_pipeline.render"), name)


@pytest.fixture(autouse=True)
def nothing_runs(request, monkeypatch):
    """Rendering maps nothing, asks no model and starts no process (except in the tests that run a review)."""
    if "running" in request.fixturenames:
        return
    if "canned_state" in request.fixturenames:
        request.getfixturevalue("canned_state")  # built from a git repository before the patches go in

    def boom(*args, **kwargs):
        raise AssertionError("rendering the Paths section must not call this")

    for owner, name in ((code_facts, "map_paths"), (code_facts_paths, "map_paths"), (sdk, "agent_call_structured"),
                        (sdk, "agent_call_text"), (sdk, "run_agent"), (subprocess, "run"), (subprocess, "Popen"),
                        (os, "system")):
        monkeypatch.setattr(owner, name, boom, raising=False)


# --- building paths ----------------------------------------------------------------------------------------------


def e2e_record(node: str, *, mocked: bool = False):
    if mocked:
        return MockedE2ETest(node_id=node, file="tests/t.py", line=9, match="cli_argv", invocation_line=10,
                             patch_file="tests/t.py", patch_line=8)
    return E2ETest(node_id=node, file="tests/t.py", line=3, match="cli_argv", invocation_line=4)


def traced(pid: str, *, name: str = "review_cmd", file: str = "cli.py", line: int = 42, invoked_by: str = "a CLI",
           e2e=(), mocked=(), stops_at: int | None = None) -> TracedPath:
    entry = PathEntry(kind="argparse", name=name, location=Location(path=file, line=line),
                      handler=Handler(path=file, line=line + 1, symbol="run"), invocation=None,
                      invoked_by=invoked_by, also_invoked_by=[])
    hops = [PathHop(symbol_called="run", path=file, line_start=line, line_end=line, quote="x") for _ in range(4)]
    return TracedPath.model_construct(
        id=pid, entry=entry, hops=hops, stops_at=stops_at, break_reason="text_mismatch" if stops_at else None,
        changed_lines_covered=[], tests=E2EListing(e2e=[e2e_record(t) for t in e2e],
                                                   mocks_hop_2=[e2e_record(t, mocked=True) for t in mocked]))


def result(*paths: TracedPath) -> PathsResult:
    return PathsResult.model_construct(schema_version="paths.v1", commit="a" * 40, scoped=True, cap=21,
                                       fallback=FallbackStatus(status="ok", reason=None), paths=list(paths),
                                       dropped=[], diagnostics=[], test_listing_skips=[])


def ok(canned_state, **changes):
    canned_state.paths_status = "ok"
    for key, value in changes.items():
        setattr(canned_state, key, value)
    return canned_state


def rows(*paths_lists) -> list[dict]:
    return [{"id": f"{i:010x}", **({"paths": p} if p is not None else {})} for i, p in enumerate(paths_lists, 1)]


def section(state, paths, findings) -> str:
    return new("render_paths_section")(state, paths, findings)


def block(text: str, pid: str) -> str:
    """The lines of one path's block."""
    start = text.index(f"### Path {pid}:")
    rest = text[start + 1:]
    end = rest.find("\n### ")
    return text[start:] if end < 0 else text[start:start + 1 + end]


# --- the blocks ------------------------------------------------------------------------------------------------


def test_each_path_shows_its_entry_invokers_findings_and_tests_in_paths_json_order(canned_state):
    paths = result(traced("p3", name="third", e2e=["tests/e2e/test_review.py::test_full_run"]),
                   traced("p1", name="first", mocked=["tests/e2e/test_mock.py::test_x"]),
                   traced("p2", name="second", invoked_by="/multiplai-dev:review"))
    findings = rows(["p1"], ["p1", "p3"], None, ["p1"])
    text = section(ok(canned_state), paths, findings)
    assert text.startswith("## Paths\n") and text.count("## Paths") == 1
    assert [line.split(":")[0] for line in text.splitlines() if line.startswith("### ")] == [
        "### Path p3", "### Path p1", "### Path p2"]
    assert "### Path p3: `third` at `cli.py:42`" in text
    p1 = block(text, "p1")
    assert "- Invoked by: `a CLI`" in p1
    assert "- Findings on this path: 3 (`0000000001`, `0000000002`, `0000000004`)" in p1
    assert "  - `tests/e2e/test_mock.py::test_x` — mocked (not counted)" in p1
    assert "  - no e2e test" in p1
    p3 = block(text, "p3")
    assert "- Findings on this path: 1 (`0000000002`)" in p3
    assert "  - `tests/e2e/test_review.py::test_full_run` — counted" in p3 and "no e2e test" not in p3
    p2 = block(text, "p2")
    assert "- Invoked by: `/multiplai-dev:review`" in p2
    assert "- Findings on this path: 0" in p2 and "0 (" not in p2
    assert "  - no e2e test" in p2


def test_mixed_tests_list_both_markers_and_no_missing_line(canned_state):
    paths = result(traced("p4", e2e=["tests/a.py::test_real"], mocked=["tests/a.py::test_fake"]))
    p4 = block(section(ok(canned_state), paths, []), "p4")
    assert "`tests/a.py::test_real` — counted" in p4 and "`tests/a.py::test_fake` — mocked (not counted)" in p4
    assert "no e2e test" not in p4


def test_a_broken_path_says_where_its_trace_stops(canned_state):
    text = section(ok(canned_state), result(traced("p1", stops_at=4), traced("p2")), [])
    assert "- Trace stops at hop 4." in block(text, "p1") and "Trace stops" not in block(text, "p2")


def test_a_finding_counts_once_per_path_in_its_list(canned_state):
    text = section(ok(canned_state), result(traced("p1"), traced("p2")), rows(["p1", "p2"], ["p2"]))
    assert "- Findings on this path: 1 (`0000000001`)" in block(text, "p1")
    assert "- Findings on this path: 2 (`0000000001`, `0000000002`)" in block(text, "p2")


def test_an_entry_with_no_invoker_reads_unknown_invoker(canned_state):
    path = traced("p3")
    path.entry.__dict__["invoked_by"] = ""
    assert "- Invoked by: unknown invoker" in section(ok(canned_state), result(path), [])


# --- when there are no paths ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["skipped", "not-run"])
def test_a_review_whose_paths_stage_did_not_run_has_no_section(canned_state, status):
    canned_state.paths_status = status
    assert section(canned_state, None, []) == ""
    assert "## Paths" not in render_review(canned_state)


def test_a_failed_mapping_shows_the_heading_the_reason_and_the_rest_of_the_report(canned_state):
    canned_state.paths_status, canned_state.paths_failure = "failed", "index exploded"
    canned_state.coverage = [CoverageRecord(stage="paths", subject="mapping", level="error",
                                            reason="mapping-failed", detail="index exploded")]
    text = render_review(canned_state)
    assert "## Paths\n\nNo paths were mapped: index exploded." in text
    assert "## Appendix" in text and text.index("## Paths") < text.index("## Appendix")


def test_an_empty_mapping_shows_the_heading_and_the_notice(canned_state):
    canned_state.paths_status = "empty"
    canned_state.paths_failure = "no entry point reaches the lines under review"
    text = section(canned_state, result(), [])
    assert text.startswith("## Paths\n\nNo paths were mapped: no entry point reaches the lines under review.")


def test_the_section_comes_after_the_finding_sections_and_before_the_appendix(canned_state):
    text = render_review(ok(canned_state), paths=result(traced("p1")))
    assert text.index("## Code") < text.index("## Paths") < text.index("## Appendix")


# --- the gap line --------------------------------------------------------------------------------------------------


def cov(reason: str, level: str = "info", stage: str = "paths") -> CoverageRecord:
    return CoverageRecord(stage=stage, subject="x", level=level, reason=reason)


@pytest.mark.parametrize(("records", "line"), [
    ([cov("over-cap"), cov("out-of-scope"), cov("broken-trace", "error")],
     "Gaps: 2 entries dropped (1 over cap, 1 out of scope); 1 broken trace."),
    ([cov("over-cap"), cov("over-cap")], "Gaps: 2 entries dropped (2 over cap)."),
    ([cov("out-of-scope")], "Gaps: 1 entry dropped (1 out of scope)."),
    ([cov("trace-failed", "error"), cov("broken-trace", "error"), cov("fallback-failed", "error")],
     "Gaps: 3 broken traces."),
    ([cov("finder-failed", "error", stage="find"), cov("mapping-failed", "error")], ""),
    ([], ""),
])
def test_the_gap_line_counts_dropped_entries_and_broken_traces(records, line):
    assert new("gap_line")(records) == line


def test_the_gap_line_ends_the_section_and_is_absent_when_nothing_was_dropped(canned_state):
    paths = result(traced("p1"))
    ok(canned_state, coverage=[cov("over-cap"), cov("broken-trace", "error")])
    text = section(canned_state, paths, [])
    assert text.rstrip("\n").splitlines()[-1] == "Gaps: 1 entry dropped (1 over cap); 1 broken trace."
    canned_state.coverage = []
    assert "Gaps:" not in section(canned_state, paths, [])


# --- inert rendering ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "code"), [
    ("plain", "`plain`"),
    ("a `tick` b", "``a `tick` b``"),
    ("``two``", "``` ``two`` ```"),
    ("line\nbreak\x07bell", "`line break bell`"),
])
def test_inline_code_wraps_any_text_in_a_code_span_longer_than_its_backtick_runs(text, code):
    assert new("inline_code")(text) == code


def test_hostile_names_invokers_and_tests_add_no_structure(canned_state):
    hostile_name = "## Injected [x](http://evil) `tick`"
    path = traced("p1", name=hostile_name, invoked_by="<script>alert(1)</script>",
                  e2e=["tests/t.py::test_x\n# Heading"])
    text = section(ok(canned_state), result(path), [])
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == ["## Paths", "### Path p1: `` ## Injected [x](http://evil) `tick` `` at `cli.py:42`"]
    assert "- Invoked by: `<script>alert(1)</script>`" in text
    outside = text.replace("`<script>alert(1)</script>`", "")
    assert "<script>" not in outside
    assert "`tests/t.py::test_x # Heading` — counted" in text


# --- through a review -----------------------------------------------------------------------------------------------


@pytest.fixture
def running():
    """Marks the end-to-end tests, which run git and the fake model: `nothing_runs` patches nothing for them."""


def test_a_reviewed_change_with_three_paths_has_one_section_with_three_blocks(tmp_path, running, model, mapped):  # noqa: F811
    repo, base, head = make_repo(tmp_path, 1)
    mapped(3)
    model()
    assert review(repo, base, head, tmp_path / "out") == 0
    target_dir, state = saved(tmp_path / "out")
    text = (target_dir / f"review-{state.target.slug}.md").read_text()
    assert text.count("## Paths") == 1 and text.count("### Path p") == 3


def test_a_tree_review_without_paths_has_no_section_and_no_paths_json(tmp_path, running, model, mapped):  # noqa: F811
    repo, _, _ = make_repo(tmp_path, 1)
    mapped(2)
    model()
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("concurrency: 1\n")
    assert main(["--session-id", "sess-test", "--out", str(out), "review", "--repo", str(repo), "--tree",
                 "--trust-repo"]) == 0
    target_dir, state = saved(out)
    assert not (target_dir / "paths.json").exists()
    assert "## Paths" not in (target_dir / f"review-{state.target.slug}.md").read_text()


def test_re_rendering_reads_the_stored_paths_json(tmp_path, canned_state):
    paths = result(traced("p1", name="stored_cmd"))
    ok(canned_state, path_ids=["p1"])
    paths = PathsResult.model_validate(json.loads(paths.model_dump_json()))
    write_paths_file(paths, tmp_path / "paths.json")
    write_review(canned_state, tmp_path)
    text = (tmp_path / f"review-{canned_state.target.slug}.md").read_text()
    assert "### Path p1: `stored_cmd` at `cli.py:42`" in text
    data = to_findings_file(canned_state)
    assert section(canned_state, paths, data["findings"]) in text
