"""`map_paths` tracing: one gated request per entry, one re-ask, cuts, drops, ordering and determinism."""

from __future__ import annotations

import json
import random

import pytest
from pydantic import BaseModel, field_serializer
from trace_fixtures import (
    CLI,
    NAMES,
    FakeCall,
    as_json,
    chain_hops,
    chain_source,
    cli_snapshot,
    cli_source,
    commit_snapshot,
    lib,
    run_map,
)

READ_ONLY = ["Read", "Grep", "Glob"]


def good(snap, count=2):
    """A responder answering each entry with its first `count` hops, as a plain dict."""
    return lambda name: {"hops": chain_hops(snap, name, count)}


def written(result, tmp_path, name="paths.json") -> bytes:
    out = tmp_path / name
    lib("write_paths_file")(result, out)
    return out.read_bytes()


def by_name(result):
    return {p.entry.name: p for p in result.paths}


def drops(result):
    return {d.entry.name: (d.stage, d.reason) for d in result.dropped}


# --- end to end ---------------------------------------------------------------------------------


def test_argparse_cli_maps_to_a_written_document_whose_hops_reread(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal", "ruby"])
    call = FakeCall(trace=good(snap, 3))
    result = run_map(snap, call)

    doc = json.loads(written(result, tmp_path))
    assert [p["id"] for p in doc["paths"]] == ["p1", "p2"]
    opal, ruby = doc["paths"]
    assert opal["entry"] == {
        "kind": "argparse",
        "name": "opal",
        "location": {"path": CLI, "line": snap.line(CLI, "add_parser('opal')")},
        "handler": {
            "path": CLI,
            "line": snap.line(CLI, "def cmd_opal("),
            "symbol": "cmd_opal",
        },
        "invocation": None,
        "invoked_by": "a person or a script running opal",
        "also_invoked_by": [],
    }
    assert ruby["entry"]["name"] == "ruby"
    for p in doc["paths"]:
        assert (p["stops_at"], p["break_reason"]) == (None, None)
        assert p["changed_lines_covered"] == []
        assert p["tests"] == {"e2e": [], "mocks_hop_2": []}
        assert [h["symbol_called"] for h in p["hops"]] == [
            f"cmd_{p['entry']['name']}",
            f"step1_{p['entry']['name']}",
            f"step2_{p['entry']['name']}",
        ]
        for h in p["hops"]:
            cite = lib("Citation")(
                path=h["path"],
                line_start=h["line_start"],
                line_end=h["line_end"],
                quote=h["quote"],
            )
            assert lib("check_citation")(cite, snap.resolve) is None
    assert [c.stage for c in call.calls] == ["trace", "trace"]
    assert {c.entry for c in call.calls} == {"opal", "ruby"}


def test_comparison_dispatch_keeps_the_add_parser_line_and_traces_from_it(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"], dispatch="comparison")
    line = snap.line(CLI, "add_parser('opal')")
    first = snap.hop(CLI, "main", line=line)
    call = FakeCall(trace=lambda name: {"hops": [first, snap.hop(CLI, "cmd_opal")]})
    (path,) = run_map(snap, call).paths
    assert (path.entry.kind, path.entry.location.line, path.entry.handler) == (
        "argparse",
        line,
        None,
    )
    assert (len(path.hops), path.stops_at) == (2, None)


@pytest.mark.parametrize("form", ["dict", "json", "model"])
def test_answer_forms_produce_the_same_document(tmp_path, form):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    hops = chain_hops(snap, "opal", 2)
    answer = {
        "dict": {"hops": hops},
        "json": as_json(hops),
        "model": lib("TraceAnswer").model_validate({"hops": hops}),
    }[form]
    result = run_map(snap, FakeCall(trace=lambda name: answer))
    assert [h.symbol_called for h in result.paths[0].hops] == ["cmd_opal", "step1_opal"]


def test_seed_chain_keeps_hops_in_answer_order(tmp_path):
    snap = commit_snapshot(tmp_path / "repo", {"pkg/chain.py": chain_source()})
    hops = [snap.hop("pkg/chain.py", f"f{k}") for k in range(1, 6)]
    call = FakeCall(trace=lambda name: {"hops": hops}, names=["f1"])
    result = run_map(snap, call, seeds=[lib("Seed")(path="pkg/chain.py", symbol="f1")])
    (path,) = result.paths
    assert path.entry.kind == "seed"
    assert [h.symbol_called for h in path.hops] == ["f1", "f2", "f3", "f4", "f5"]


# --- the requests -------------------------------------------------------------------------------


def test_trace_requests_are_read_only_and_overwrite_caller_tools_cwd_and_label(
    tmp_path,
):
    snap = cli_snapshot(tmp_path / "repo", NAMES)
    call = FakeCall(trace=good(snap))
    kwargs = {
        "allowed_tools": ["Bash", "Write"],
        "cwd": "/elsewhere",
        "budget_label": "mine",
        "model": "m-1",
    }
    run_map(snap, call, call_kwargs=kwargs)
    assert len(call.calls) == len(NAMES)
    labels = set()
    for c in call.calls:
        assert c.schema is lib("TraceAnswer")
        assert c.kwargs["allowed_tools"] == READ_ONLY
        assert c.kwargs["cwd"] == str(snap.root)
        assert c.kwargs["model"] == "m-1"
        label = c.kwargs["budget_label"]
        assert (
            label.startswith("paths:trace:") and len(label) == len("paths:trace:") + 12
        )
        labels.add(label)
    assert len(labels) == len(NAMES)
    assert kwargs["allowed_tools"] == ["Bash", "Write"]


def test_unscoped_trace_prompt_carries_entry_commit_edge_places_and_limit(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    call = FakeCall(trace=good(snap))
    place = lib("Location")(path="pkg/other.py", line=1)
    run_map(snap, call, places=[place])
    prompt = call.calls[0].prompt
    assert "no change scope; trace to the last effect" in prompt
    assert snap.head in prompt
    assert CLI in prompt and "pkg/other.py" in prompt
    assert "cmd_opal" in prompt and "argparse" in prompt
    for line in (
        snap.line(CLI, "add_parser('opal')"),
        snap.line(CLI, "return args.func(args)"),
        snap.line(CLI, "def cmd_opal("),
    ):
        assert str(line) in prompt
    assert "at most 20 hops" in prompt


def test_trace_prompt_names_the_change_scope(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    ent = lib("discover_entries")(snap.root).entries[0]
    prompt = lib("paths_trace")(
        ent,
        commit="c0ffee",
        changed_lines={"pkg/core.py": frozenset({10, 11, 12, 37})},
        known_edges=ent.known_edges,
        places=[],
        hop_limit=20,
    )
    assert "pkg/core.py" in prompt and "c0ffee" in prompt and "cmd_opal" in prompt
    assert "pkg/core.py: lines 10-12, 37" in prompt
    assert "no change scope" not in prompt


START_AT_HANDLER = "parser registration: start hop 1 at its handler's `def` line"


def test_trace_prompt_of_a_parser_registration_says_to_start_at_its_handler(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    ent = lib("discover_entries")(snap.root).entries[0]
    prompt = lib("paths_trace")(
        ent, commit="c0ffee", changed_lines=None, known_edges=ent.known_edges, places=[]
    )
    assert f"{START_AT_HANDLER}, {CLI}:{snap.line(CLI, 'def cmd_opal(')}" in prompt


@pytest.mark.parametrize(
    "kind,dispatch",
    [("argparse_subcommand", "comparison"), ("main_module", "set_defaults")],
)
def test_trace_prompt_without_a_resolved_parser_handler_names_no_start(
    tmp_path, kind, dispatch
):
    snap = cli_snapshot(tmp_path / "repo", ["opal"], dispatch=dispatch)
    entries = lib("discover_entries")(snap.root).entries
    ent = (
        next(e for e in entries if e.kind == kind)
        if kind == "argparse_subcommand"
        else entries[0].model_copy(update={"kind": kind})
    )
    prompt = lib("paths_trace")(
        ent, commit="c0ffee", changed_lines=None, known_edges=ent.known_edges, places=[]
    )
    assert "parser registration" not in prompt


def test_reask_prompt_carries_index_reason_detail_and_accepted_prefix(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    ent = lib("discover_entries")(snap.root).entries[0]
    accepted = [lib("PathHop")(**h) for h in chain_hops(snap, "opal", 3)]
    prompt = lib("paths_reask")(
        ent,
        commit="c0ffee",
        broken_at=4,
        break_reason="text_mismatch",
        detail="quote not at cited lines 31-31",
        accepted=accepted,
    )
    assert (
        "at hop 4: text_mismatch" in prompt
        and "quote not at cited lines 31-31" in prompt
    )
    assert all(h.quote in prompt for h in accepted)


def test_prompt_versions():
    assert (lib("PATHS_TRACE_PROMPT_VERSION"), lib("PATHS_REASK_PROMPT_VERSION")) == (
        "paths-trace.v1",
        "paths-reask.v1",
    )


# --- re-ask and cut -------------------------------------------------------------------------------


def broken_at_4(snap, name):
    hops = chain_hops(snap, name, 4)
    hops[3] = {**hops[3], "quote": "not the code"}
    return {"hops": hops}


def test_reask_repairs_a_broken_trace(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal", "ruby"])
    call = FakeCall(
        trace=lambda n: broken_at_4(snap, n) if n == "opal" else good(snap)(n),
        reask=good(snap, 4),
    )
    result = run_map(snap, call)
    opal = by_name(result)["opal"]
    assert (len(opal.hops), opal.stops_at, opal.break_reason) == (4, None, None)
    assert [c.stage for c in call.of("opal")] == ["trace", "reask"]
    assert [c.stage for c in call.of("ruby")] == ["trace"]
    (reask,) = call.of("opal", "reask")
    assert "at hop 4: text_mismatch" in reask.prompt
    assert "def step2_opal(args):" in reask.prompt  # the accepted prefix
    assert reask.kwargs["allowed_tools"] == READ_ONLY and reask.kwargs["cwd"] == str(
        snap.root
    )
    label = reask.kwargs["budget_label"]
    assert label.startswith("paths:reask:") and len(label) == len("paths:reask:") + 12


def test_unrepaired_trace_is_cut_to_the_reasked_prefix(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    # The re-answer is worse than the first (break at 3, not 4) and still replaces it.
    reanswer = [
        snap.hop(CLI, "cmd_opal"),
        snap.hop(CLI, "step1_opal"),
        snap.hop(CLI, "step3_opal"),
    ]
    call = FakeCall(
        trace=lambda n: broken_at_4(snap, n), reask=lambda n: {"hops": reanswer}
    )
    result = run_map(snap, call)
    (path,) = result.paths
    assert (path.stops_at, path.break_reason) == (2, "unconnected_hop")
    assert [h.symbol_called for h in path.hops] == ["cmd_opal", "step1_opal"]
    assert len(call.calls) == 2
    doc = json.loads(written(result, tmp_path))
    assert (doc["paths"][0]["stops_at"], len(doc["paths"][0]["hops"])) == (2, 2)


def test_break_at_hop_1_after_reask_drops_the_entry(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal", "ruby"])
    wrong = {"hops": [snap.hop("pkg/other.py", "elsewhere")]}
    call = FakeCall(
        trace=lambda n: good(snap)(n) if n == "ruby" else broken_at_4(snap, n),
        reask=lambda n: wrong,
    )
    result = run_map(snap, call)
    assert [p.entry.name for p in result.paths] == ["ruby"]
    assert drops(result) == {"opal": ("trace", "entry_mismatch")}
    (drop,) = result.dropped
    assert drop.entry == lib("to_path_entry")(
        lib("discover_entries")(snap.root).entries[0]
    )
    assert len(call.of("opal")) == 2


class Unserialisable(BaseModel):
    """An answer model whose dump fails, as a foreign CallFn's might."""

    hops: list = []

    @field_serializer("hops")
    def _refuse(self, hops):
        raise ValueError("cannot serialise hops")


@pytest.mark.parametrize(
    "bad,reason",
    [
        ("this is not json", "malformed_answer"),
        (Unserialisable(), "malformed_answer"),
        ({"hops": [], "alternatives": [[]]}, "malformed_answer"),
        ({"hops": []}, "empty_trace"),
    ],
)
def test_malformed_or_empty_answers_get_one_reask_then_drop(tmp_path, bad, reason):
    snap = cli_snapshot(tmp_path / "repo", ["opal", "ruby", "jade"])
    call = FakeCall(
        trace=lambda n: bad if n == "ruby" else good(snap)(n), reask=lambda n: bad
    )
    result = run_map(snap, call)
    assert sorted(by_name(result)) == ["jade", "opal"]
    assert drops(result) == {"ruby": ("trace", reason)}
    assert [c.stage for c in call.of("ruby")] == ["trace", "reask"]
    assert written(result, tmp_path)


def test_malformed_first_answer_repaired_by_reask(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    call = FakeCall(trace=lambda n: "{not json", reask=good(snap))
    (path,) = run_map(snap, call).paths
    assert (len(path.hops), path.stops_at) == (2, None)


def test_base_side_hop_is_cut_after_reask(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    hops = chain_hops(snap, "opal", 3)
    hops[1] = {**hops[1], "side": "base"}
    call = FakeCall(trace=lambda n: {"hops": hops}, reask=lambda n: {"hops": hops})
    (path,) = run_map(snap, call).paths
    assert (path.stops_at, path.break_reason, len(path.hops)) == (
        1,
        "base_side_citation",
        1,
    )


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd"])
def test_hop_escaping_the_root_is_cut(tmp_path, path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    hops = chain_hops(snap, "opal", 2) + [
        {**chain_hops(snap, "opal", 3)[2], "path": path}
    ]
    call = FakeCall(trace=lambda n: {"hops": hops}, reask=lambda n: {"hops": hops})
    (traced,) = run_map(snap, call).paths
    assert (traced.stops_at, traced.break_reason) == (2, "path_outside_root")
    assert all(not h.path.startswith(("/", "..")) for h in traced.hops)


# --- hop limit ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,stops_at,reason", [(20, None, None), (23, 20, "hop_limit")]
)
def test_hop_limit_keeps_twenty_and_is_never_reasked(tmp_path, count, stops_at, reason):
    snap = commit_snapshot(tmp_path / "repo", {"pkg/chain.py": chain_source()})
    hops = [snap.hop("pkg/chain.py", f"f{k}") for k in range(1, count + 1)]
    call = FakeCall(trace=lambda n: {"hops": hops}, names=["f1"])
    result = run_map(snap, call, seeds=[lib("Seed")(path="pkg/chain.py", symbol="f1")])
    (path,) = result.paths
    assert (len(path.hops), path.stops_at, path.break_reason) == (20, stops_at, reason)
    assert [h.symbol_called for h in path.hops] == [f"f{k}" for k in range(1, 21)]
    assert len(call.calls) == 1
    assert written(result, tmp_path)


# --- CallFn failures -----------------------------------------------------------------------------


def test_recoverable_failure_drops_only_that_entry_without_retry(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal", "ruby", "jade"])
    call = FakeCall(
        trace=lambda n: RuntimeError("budget") if n == "ruby" else good(snap)(n)
    )
    result = run_map(snap, call, recoverable=(RuntimeError,))
    assert sorted(by_name(result)) == ["jade", "opal"]
    assert drops(result) == {"ruby": ("trace", "call_failed")}
    assert len(call.of("ruby")) == 1


def test_recoverable_failure_on_the_reask_drops_as_call_failed(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    call = FakeCall(
        trace=lambda n: broken_at_4(snap, n), reask=lambda n: RuntimeError("budget")
    )
    result = run_map(snap, call, recoverable=(RuntimeError,))
    assert result.paths == [] and drops(result) == {"opal": ("trace", "call_failed")}
    assert len(call.calls) == 2


@pytest.mark.parametrize("stage", ["trace", "reask"])
def test_other_failures_propagate_unchanged_with_no_retry(tmp_path, stage):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    boom = RuntimeError("budget")
    if stage == "trace":
        call = FakeCall(trace=lambda n: boom)
    else:
        call = FakeCall(trace=lambda n: broken_at_4(snap, n), reask=lambda n: boom)
    with pytest.raises(RuntimeError) as info:
        run_map(snap, call, recoverable=(ValueError,))
    assert info.value is boom
    assert [c.stage for c in call.calls] == (
        ["trace"] if stage == "trace" else ["trace", "reask"]
    )


# --- ordering, ids, concurrency and determinism ---------------------------------------------------


def test_paths_are_ordered_by_hop_count_kind_then_location_and_numbered(tmp_path):
    prelude = "\ndef helper_seed(args):\n    return step1_opal(args)\n"
    snap = cli_snapshot(
        tmp_path / "repo", ["opal", "ruby", "jade", "onyx"], prelude=prelude
    )
    counts = {"opal": 4, "ruby": 2, "jade": 3, "onyx": 2}

    def trace(name):
        if name is None:  # the seed's prompt names no subcommand
            return {"hops": [snap.hop(CLI, "helper_seed"), snap.hop(CLI, "step1_opal")]}
        return {"hops": chain_hops(snap, name, counts[name])}

    call = FakeCall(trace=trace)
    result = run_map(snap, call, seeds=[lib("Seed")(path=CLI, symbol="helper_seed")])
    assert [(p.id, p.entry.kind, p.entry.name) for p in result.paths] == [
        ("p1", "argparse", "ruby"),
        ("p2", "argparse", "onyx"),
        ("p3", "seed", f"{CLI}:helper_seed"),
        ("p4", "argparse", "jade"),
        ("p5", "argparse", "opal"),
    ]


def test_concurrency_limit_is_respected(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", NAMES)
    call = FakeCall(trace=good(snap), delay=lambda: 0.02)
    result = run_map(snap, call, concurrency=2)
    assert call.max_in_flight == 2
    assert sorted(by_name(result)) == sorted(NAMES)


def test_concurrency_one_runs_serially(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", NAMES)
    call = FakeCall(trace=good(snap), delay=lambda: 0.01)
    run_map(snap, call, concurrency=1)
    assert call.max_in_flight == 1 and len(call.calls) == len(NAMES)


def test_concurrency_and_completion_order_do_not_change_the_file(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", NAMES)
    counts = dict(zip(NAMES, [2, 4, 3, 2, 1, 3], strict=True))
    rng = random.Random(7)

    def trace(name):
        hops = chain_hops(snap, name, counts[name])
        if name == "jade":  # one cut path and one drop, so both lists take part
            hops[2] = {**hops[2], "quote": "nope"}
        if name == "amber":
            return {"hops": [snap.hop("pkg/other.py", "elsewhere")]}
        return {"hops": hops}

    outputs = []
    for limit in (1, 8):
        call = FakeCall(trace=trace, reask=trace, delay=lambda: rng.uniform(0, 0.02))
        outputs.append(
            written(
                run_map(snap, call, concurrency=limit), tmp_path, f"paths-{limit}.json"
            )
        )
    assert outputs[0] == outputs[1]
    doc = json.loads(outputs[0])
    assert [p["id"] for p in doc["paths"]] == [f"p{n}" for n in range(1, 6)]
    assert [(d["stage"], d["reason"]) for d in doc["dropped"]] == [
        ("trace", "entry_mismatch")
    ]


def test_multi_repository_snapshot_locations_stay_root_relative(tmp_path):
    snap = commit_snapshot(
        tmp_path / "snap",
        {"repo-a/README.md": "# a\n", "repo-b/cli.py": cli_source(["opal"])},
    )
    path = "repo-b/cli.py"
    hops = [snap.hop(path, "cmd_opal"), snap.hop(path, "step1_opal")]
    (traced,) = run_map(snap, FakeCall(trace=lambda n: {"hops": hops})).paths
    assert traced.entry.location.path == path
    assert all(h.path == path for h in traced.hops)


# --- helpers the later blocks consume --------------------------------------------------------------


def test_output_kind_and_kind_rank():
    assert lib("OUTPUT_KIND") == {
        "argparse_subcommand": "argparse",
        "click_command": "click",
        "typer_command": "typer",
        "main_module": "main",
        "project_script": "script",
        "skill_command": "skill_command",
        "hook_command": "hook",
        "seed": "seed",
    }
    assert lib("KIND_RANK") == {
        "argparse": 1,
        "click": 2,
        "typer": 3,
        "main": 4,
        "script": 5,
        "skill_command": 6,
        "hook": 7,
        "seed": 8,
        "model": 9,
    }


def test_to_path_entry_maps_the_kind_and_drops_known_edges(tmp_path):
    snap = cli_snapshot(tmp_path / "repo", ["opal"])
    ent = lib("discover_entries")(snap.root).entries[0]
    assert (
        ent.known_edges
    )  # set_defaults dispatch gives an edge, which paths.v1 does not publish
    assert lib("to_path_entry")(ent) == lib("PathEntry")(
        kind="argparse",
        name="opal",
        location=ent.location,
        handler=ent.handler,
        invocation=None,
        invoked_by="a person or a script running opal",
        also_invoked_by=[],
    )


def test_assign_ids_numbers_in_order(tmp_path):
    TracedPath, PathEntry, PathHop = lib("TracedPath"), lib("PathEntry"), lib("PathHop")
    loc = {"path": "a.py", "line": 1}

    def path(pid, line):
        return TracedPath(
            id=pid,
            entry=PathEntry(
                kind="seed",
                name=f"s{line}",
                location={**loc, "line": line},
                handler=None,
                invocation=None,
                invoked_by="the caller (seed)",
                also_invoked_by=[],
            ),
            hops=[
                PathHop(
                    symbol_called="f",
                    path="a.py",
                    line_start=line,
                    line_end=line,
                    quote="x",
                )
            ],
            stops_at=None,
            break_reason=None,
            changed_lines_covered=[],
            tests={"e2e": [], "mocks_hop_2": []},
        )

    numbered = lib("assign_ids")([path("p9", 1), path("p4", 2), path("p7", 3)])
    assert [(p.id, p.entry.name) for p in numbered] == [
        ("p1", "s1"),
        ("p2", "s2"),
        ("p3", "s3"),
    ]
