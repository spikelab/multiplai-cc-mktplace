"""The single read-only model fallback: prompt, cache, parsing, citation and duplicate filtering, tracing."""

from __future__ import annotations

import asyncio
import json

import pytest

from trace_fixtures import CLI, FakeCall, chain_hops, cli_source, commit_snapshot, lib, run_map

READ_ONLY = ["Read", "Grep", "Glob"]
HIDDEN = "pkg/hidden.py"
NOTES = "docs/notes.md"

# An entry point no static scanner sees: the registry is filled at runtime.
HIDDEN_SRC = """\
def ruby_job():
    return helper()


def helper():
    return 0


REGISTRY = {"ruby": ruby_job}


class Jobs:
    LABEL = "nightly"

    def run(self):
        return ruby_job()
"""
NOTES_SRC = "# Notes\n\nRun the ruby job from the registry.\n"


@pytest.fixture
def snap(tmp_path):
    return commit_snapshot(tmp_path / "repo", {
        "pkg/__init__.py": "", CLI: cli_source(["opal"]), HIDDEN: HIDDEN_SRC, NOTES: NOTES_SRC,
        "pkg/other.py": "def elsewhere(args):\n    return 0\n",
    })


def cite(snap, path, needle, *, quote=None, end=None):
    line = snap.line(path, needle)
    text = snap.files[path].splitlines()[line - 1].strip()
    return {"path": path, "line_start": line, "line_end": end or line, "quote": text if quote is None else quote}


def fb(name, citation, kind="route"):
    return {"kind": kind, "name": name, "citation": citation, "reason": "reaches the scope"}


def ruby_entry(snap):
    return fb("ruby registry job", cite(snap, HIDDEN, "def ruby_job("))


def ruby_hops(snap):
    return [snap.hop(HIDDEN, "ruby_job"), snap.hop(HIDDEN, "helper")]


def trace_responder(snap):
    """opal gets its deterministic chain; the model entry (named for ruby) gets its two hops."""
    return lambda name: {"hops": ruby_hops(snap) if name == "ruby" else chain_hops(snap, "opal", 2)}


def fake(snap, answer):
    return FakeCall(trace=trace_responder(snap), fallback=lambda _: answer)


def fallback_calls(call):
    return [c for c in call.calls if c.stage == "paths:fallback"]


def drops(result):
    return sorted((d.stage, d.reason, d.entry.kind, d.entry.name) for d in result.dropped)


def traced_names(call):
    return sorted({c.entry for c in call.calls if c.stage == "trace"})


def ctx_for(snap, call, *, answers=None, recoverable=(), changed=None):
    index = lib("PyIndex").build(snap.root)
    ctx = lib("TraceContext")(root=snap.root, commit=snap.head, resolve=snap.resolve, call=call, index=index,
                              changed_lines=changed, places=(), answers={} if answers is None else answers,
                              recoverable=recoverable, call_kwargs={})
    return ctx, lib("discover_entries")(snap.root, index=index).entries


def run_fallback(ctx, entries, scope=None):
    return asyncio.run(lib("run_fallback")(ctx=ctx, entries=entries, scope=scope))


# --- the call -----------------------------------------------------------------------------------


def test_one_read_only_fallback_call_before_any_trace(snap):
    call = fake(snap, {"entries": []})
    run_map(snap, call, fallback=True, call_kwargs={"allowed_tools": ["Bash", "Write"], "model": "m1"})
    (fb_call,) = fallback_calls(call)
    assert call.calls[0] is fb_call
    assert fb_call.kwargs["budget_label"] == "paths:fallback"
    assert fb_call.kwargs["allowed_tools"] == READ_ONLY
    assert fb_call.kwargs["cwd"] == str(snap.root)
    assert fb_call.kwargs["model"] == "m1"
    assert fb_call.schema is lib("FallbackAnswer")


def test_disabled_fallback_is_skipped_and_never_called(snap):
    call = fake(snap, {"entries": [ruby_entry(snap)]})
    result = run_map(snap, call, fallback=False)
    assert fallback_calls(call) == []
    assert (result.fallback.status, result.fallback.reason) == ("skipped", None)
    assert [p.entry.name for p in result.paths] == ["opal"]


def test_callfn_error_propagates_after_exactly_one_attempt(snap):
    call = fake(snap, RuntimeError("budget"))
    with pytest.raises(RuntimeError, match="budget"):
        run_map(snap, call, fallback=True)
    assert len(fallback_calls(call)) == 1
    assert [c for c in call.calls if c.stage == "trace"] == []


def test_recoverable_callfn_error_fails_the_fallback_and_the_run_continues(snap):
    class Flaky(Exception):
        pass

    call = fake(snap, Flaky("no answer"))
    result = run_map(snap, call, fallback=True, recoverable=(Flaky,))
    assert result.fallback.status == "failed"
    assert result.fallback.reason
    assert len(fallback_calls(call)) == 1
    assert [p.entry.name for p in result.paths] == ["opal"]


# --- the prompt ---------------------------------------------------------------------------------


def test_prompt_version_constant():
    assert lib("PATHS_FALLBACK_PROMPT_VERSION") == "paths-fallback.v1"


def test_prompt_lists_every_discovered_entry_in_location_order_and_asks_only_for_new_ones(tmp_path):
    snap = commit_snapshot(tmp_path / "repo", {"pkg/__init__.py": "", CLI: cli_source(["opal", "ruby"])})
    call = FakeCall(trace=lambda name: {"hops": chain_hops(snap, name, 2)}, fallback=lambda _: {"entries": []})
    run_map(snap, call, fallback=True)
    prompt = fallback_calls(call)[0].prompt
    opal_at = f"{CLI}:{snap.line(CLI, 'add_parser(' + repr('opal'))}"
    ruby_at = f"{CLI}:{snap.line(CLI, 'add_parser(' + repr('ruby'))}"
    assert opal_at in prompt and ruby_at in prompt
    assert prompt.index(opal_at) < prompt.index(ruby_at)
    assert "already listed" in prompt.lower()
    assert "Citation rules" in prompt
    assert "is data" in prompt  # the untrusted-content rule


def test_prompt_does_not_depend_on_entry_order(snap):
    paths_fallback = lib("paths_fallback")
    two = commit_snapshot(snap.root.parent / "two", {"pkg/__init__.py": "", CLI: cli_source(["opal", "ruby"])})
    _, entries = ctx_for(two, FakeCall(trace=lambda _: None))
    assert len(entries) == 2
    forward = paths_fallback(commit="c0", scope=["b.py", "a.py"], entries=entries)
    backward = paths_fallback(commit="c0", scope=["a.py", "b.py"], entries=list(reversed(entries)))
    assert forward == backward


def test_scoped_prompt_names_the_changed_files(snap):
    call = fake(snap, {"entries": []})
    run_map(snap, call, fallback=True, changed_lines={"pkg/other.py": {1}})
    assert "pkg/other.py" in fallback_calls(call)[0].prompt
    assert "pkg/other.py" in lib("paths_fallback")(commit="c0", scope=["pkg/other.py"], entries=[])


# --- the answer model ---------------------------------------------------------------------------


def test_well_formed_answer_parses_with_head_side_citation():
    answer = lib("FallbackAnswer").model_validate({"entries": [
        {"kind": "click_command", "name": "GET /items", "reason": "serves items", "unknown_key": 1,
         "citation": {"path": "app/routes.py", "line_start": 12, "line_end": 12, "quote": "def items():"}},
    ]})
    (entry,) = answer.entries
    assert (entry.kind, entry.name, entry.reason) == ("click_command", "GET /items", "serves items")
    assert (entry.citation.path, entry.citation.line_start, entry.citation.side) == ("app/routes.py", 12, "head")


@pytest.mark.parametrize("kind", ["route", "celery_task", ""])
def test_unknown_kind_becomes_other(kind):
    entry = lib("FallbackEntry").model_validate({"kind": kind, "name": "x", "reason": "r"})
    assert entry.kind == "other"
    assert entry.citation is None


def test_every_answer_form_gives_the_same_document(snap, tmp_path):
    raw = {"entries": [ruby_entry(snap)]}
    forms = {"model": lib("FallbackAnswer").model_validate(raw), "dict": raw, "json": json.dumps(raw)}
    written = {}
    for form, answer in forms.items():
        result = run_map(snap, fake(snap, answer), fallback=True)
        assert (result.fallback.status, result.fallback.reason) == ("ok", None)
        out = lib("write_paths_file")(result, tmp_path / f"paths-{form}.json")
        written[form] = out.read_bytes()
    assert written["model"] == written["dict"] == written["json"]
    paths = json.loads(written["model"])["paths"]
    assert [(p["entry"]["kind"], p["entry"]["location"]["path"]) for p in paths] == [("argparse", CLI),
                                                                                       ("model", HIDDEN)]


@pytest.mark.parametrize("answer", ["not json at all", {"entries": "nope"}, {"entries": [{"citation": 3}]}])
def test_malformed_answer_fails_the_fallback_and_keeps_deterministic_paths(snap, answer):
    call = fake(snap, answer)
    result = run_map(snap, call, fallback=True)
    assert result.fallback.status == "failed"
    assert result.fallback.reason
    assert [p.entry.name for p in result.paths] == ["opal"]
    assert result.dropped == []
    assert traced_names(call) == ["opal"]


def test_empty_answer_is_ok_and_adds_nothing(snap):
    result = run_map(snap, fake(snap, {"entries": []}), fallback=True)
    assert (result.fallback.status, result.fallback.reason) == ("ok", None)
    assert [p.entry.kind for p in result.paths] == ["argparse"]


# --- kept entries -------------------------------------------------------------------------------


def test_kept_entry_is_traced_as_a_model_path(snap, tmp_path):
    call = fake(snap, {"entries": [ruby_entry(snap)]})
    result = run_map(snap, call, fallback=True)
    assert traced_names(call) == ["opal", "ruby"]
    p1, p2 = result.paths
    assert (p1.id, p1.entry.kind) == ("p1", "argparse")
    assert (p2.id, p2.entry.kind) == ("p2", "model")
    assert "ruby registry job" in p2.entry.name
    line = snap.line(HIDDEN, "def ruby_job(")
    assert (p2.entry.location.path, p2.entry.location.line) == (HIDDEN, line)
    assert (p2.entry.handler.path, p2.entry.handler.line, p2.entry.handler.symbol) == (HIDDEN, line, "ruby_job")
    assert [h.symbol_called for h in p2.hops] == ["ruby_job", "helper"]
    lib("write_paths_file")(result, tmp_path / "paths.json")


def test_a_model_found_entry_is_invoked_by_another_program_skill_or_agent(snap):
    result = run_map(snap, fake(snap, {"entries": [ruby_entry(snap)]}), fallback=True)
    by_kind = {p.entry.kind: p.entry.invoked_by for p in result.paths}
    assert by_kind == {"argparse": "a person or a script running opal",
                       "model": "another program, skill or agent (found by model)"}


def test_handler_is_the_enclosing_def_only_for_python_lines_inside_one(snap):
    answer = {"entries": [
        fb("in body", cite(snap, HIDDEN, "return helper()")),
        fb("module level", cite(snap, HIDDEN, "REGISTRY =")),
        fb("helper def", cite(snap, HIDDEN, "def helper(")),
        fb("in docs", cite(snap, NOTES, "Run the ruby job")),
        fb("class body", cite(snap, HIDDEN, "LABEL =")),
        fb("in method", cite(snap, HIDDEN, "return ruby_job()")),
    ]}
    ctx, entries = ctx_for(snap, fake(snap, answer))
    status, kept, dropped = run_fallback(ctx, entries)
    assert (status.status, status.reason, dropped) == ("ok", None, [])
    got = {(e.location.path, e.location.line):
           (e.handler.path, e.handler.line, e.handler.symbol) if e.handler else None for e in kept}
    ruby, helper = snap.line(HIDDEN, "def ruby_job("), snap.line(HIDDEN, "def helper(")
    run = snap.line(HIDDEN, "def run(")
    assert got == {
        (HIDDEN, snap.line(HIDDEN, "return helper()")): (HIDDEN, ruby, "ruby_job"),
        (HIDDEN, snap.line(HIDDEN, "REGISTRY =")): None,
        (HIDDEN, helper): (HIDDEN, helper, "helper"),
        (NOTES, snap.line(NOTES, "Run the ruby job")): None,
        # A class is not a handler: its body outside a method has none, a method's body has the method.
        (HIDDEN, snap.line(HIDDEN, "LABEL =")): None,
        (HIDDEN, snap.line(HIDDEN, "return ruby_job()")): (HIDDEN, run, "run"),
    }
    # Kept entries come back in location order, whatever order the model listed them in.
    assert [(e.location.path, e.location.line) for e in kept] == sorted(got)
    assert {lib("to_path_entry")(e).kind for e in kept} == {"model"}


def test_multi_repository_citation_is_recorded_as_written(tmp_path):
    cli_b = "import sys\n\n\ndef main():\n    return sys.argv\n"
    snap = commit_snapshot(tmp_path / "snap", {
        "repo_a/pyproject.toml": "[project]\nname = 'a'\n", "repo_a/a.py": "X = 1\n",
        "repo_b/pyproject.toml": "[project]\nname = 'b'\n", "repo_b/cli.py": cli_b,
    })
    answer = {"entries": [fb("b main", {"path": "repo_b/cli.py", "line_start": 5, "line_end": 5,
                                        "quote": "return sys.argv"})]}
    ctx, entries = ctx_for(snap, fake(snap, answer))
    _, kept, dropped = run_fallback(ctx, entries)
    assert dropped == []
    (entry,) = kept
    assert (entry.location.path, entry.location.line) == ("repo_b/cli.py", 5)
    assert (entry.handler.path, entry.handler.line, entry.handler.symbol) == ("repo_b/cli.py", 4, "main")


def test_model_order_does_not_change_the_written_file(snap, tmp_path):
    a, b = ruby_entry(snap), fb("helper job", cite(snap, HIDDEN, "def helper("))
    helper_hops = [snap.hop(HIDDEN, "helper")]

    def responder(name):
        return {"hops": ruby_hops(snap) if name == "ruby" else
                helper_hops if name is None else chain_hops(snap, "opal", 2)}

    outs = []
    for n, entries in enumerate(([a, b], [b, a])):
        call = FakeCall(trace=responder, fallback=lambda _, e=entries: {"entries": e})
        out = tmp_path / f"paths{n}.json"
        lib("write_paths_file")(run_map(snap, call, fallback=True), out)
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert [p["entry"]["kind"] for p in json.loads(outs[0])["paths"]].count("model") == 2


# --- dropped entries ----------------------------------------------------------------------------


def test_entry_without_citation_is_dropped_and_not_traced(snap):
    call = fake(snap, {"entries": [fb("ruby registry job", None)]})
    result = run_map(snap, call, fallback=True)
    assert drops(result) == [("model_fallback_citation", "missing_citation", "model", "ruby registry job")]
    assert traced_names(call) == ["opal"]
    assert result.fallback.status == "ok"


@pytest.mark.parametrize("make", [
    pytest.param(lambda s: {"path": "pkg/missing.py", "line_start": 3, "line_end": 3, "quote": "x"}, id="no-file"),
    pytest.param(lambda s: {"path": HIDDEN, "line_start": 9999, "line_end": 9999, "quote": "x"}, id="past-eof"),
    pytest.param(lambda s: cite(s, HIDDEN, "def ruby_job(", end=9999), id="range-runs-past-eof"),
    pytest.param(lambda s: cite(s, HIDDEN, "def ruby_job(", quote="def ruby_task():"), id="text-mismatch"),
    pytest.param(lambda s: {"path": "../outside.py", "line_start": 1, "line_end": 1, "quote": "x"}, id="escapes"),
    pytest.param(lambda s: {**cite(s, HIDDEN, "def ruby_job("), "path": str(s.root / HIDDEN)}, id="absolute"),
])
def test_unverified_citation_is_dropped_and_not_traced(snap, make):
    call = fake(snap, {"entries": [fb("ruby registry job", make(snap))]})
    result = run_map(snap, call, fallback=True)
    assert drops(result) == [("model_fallback_citation", "citation_unverified", "model", "ruby registry job")]
    assert traced_names(call) == ["opal"]
    assert [p.entry.name for p in result.paths] == ["opal"]


@pytest.mark.parametrize("needle", [
    pytest.param("add_parser('opal')", id="entry-location"),
    pytest.param("def cmd_opal(", id="handler-line"),
    pytest.param("return args.func(args)", id="known-edge-from"),
])
def test_line_of_a_deterministic_entry_is_a_duplicate(snap, needle):
    call = fake(snap, {"entries": [fb("opal again", cite(snap, CLI, needle))]})
    result = run_map(snap, call, fallback=True)
    assert drops(result) == [("model_fallback_duplicate", "duplicate", "model", "opal again")]
    assert [p.entry.kind for p in result.paths] == ["argparse"]
    assert len([c for c in call.calls if c.stage == "trace"]) == 1


def test_line_inside_a_deterministic_entry_handler_is_a_duplicate(snap):
    # The cited body line is no entry's location, edge or handler line; only its enclosing def matches.
    answer = {"entries": [fb("opal body", cite(snap, CLI, "return step1_opal(args)"))]}
    ctx, entries = ctx_for(snap, fake(snap, answer))
    status, kept, dropped = run_fallback(ctx, entries)
    assert (status.status, kept) == ("ok", [])
    (drop,) = dropped
    assert (drop.stage, drop.reason, drop.entry.kind) == ("model_fallback_duplicate", "duplicate", "model")
    assert "cmd_opal" in drop.detail and "handler" in drop.detail
    assert (drop.entry.handler.line, drop.entry.handler.symbol) == (snap.line(CLI, "def cmd_opal("), "cmd_opal")


def test_same_line_number_in_another_file_is_not_a_duplicate(tmp_path):
    cli = cli_source(["opal"])
    line = cli.splitlines().index("    p_opal = sub.add_parser('opal')") + 1
    hidden = "\n" * (line - 1) + "def ruby_job():\n    return 0\n"
    snap = commit_snapshot(tmp_path / "repo", {"pkg/__init__.py": "", CLI: cli, HIDDEN: hidden})
    answer = {"entries": [fb("ruby registry job", cite(snap, HIDDEN, "def ruby_job("))]}
    ctx, entries = ctx_for(snap, fake(snap, answer))
    _, kept, dropped = run_fallback(ctx, entries)
    assert dropped == []
    assert [(e.location.path, e.location.line) for e in kept] == [(HIDDEN, line)]


def test_two_entries_citing_one_line_keep_exactly_one(snap):
    call = fake(snap, {"entries": [ruby_entry(snap), fb("ruby again", cite(snap, HIDDEN, "def ruby_job("))]})
    result = run_map(snap, call, fallback=True)
    assert [(d.stage, d.reason) for d in result.dropped] == [("model_fallback_duplicate", "duplicate")]
    assert [p.entry.kind for p in result.paths] == ["argparse", "model"]
    assert len([c for c in call.calls if c.stage == "trace" and c.entry == "ruby"]) == 1


def test_first_failing_check_decides_the_drop(snap):
    # Both cite a deterministic entry's handler line; the one whose quote is wrong fails verification first.
    call = fake(snap, {"entries": [fb("bad quote", cite(snap, CLI, "def cmd_opal(", quote="def nope():")),
                                   fb("no cite", None)]})
    result = run_map(snap, call, fallback=True)
    assert drops(result) == [("model_fallback_citation", "citation_unverified", "model", "bad quote"),
                             ("model_fallback_citation", "missing_citation", "model", "no cite")]


def test_kept_and_dropped_account_for_every_entry(snap):
    listed = [ruby_entry(snap), fb("no cite", None),
              fb("ghost", {"path": "pkg/missing.py", "line_start": 1, "line_end": 1, "quote": "x"}),
              fb("dup", cite(snap, CLI, "add_parser('opal')"))]
    result = run_map(snap, fake(snap, {"entries": listed}), fallback=True)
    discovered = len(lib("discover_entries")(snap.root).entries)
    assert len(result.paths) + len(result.dropped) == discovered + len(listed)


# --- caching ------------------------------------------------------------------------------------


def test_rerun_with_the_cache_makes_no_calls_and_writes_the_same_bytes(snap, tmp_path):
    outs = []
    for n in range(2):
        call = fake(snap, {"entries": [ruby_entry(snap)]})
        out = tmp_path / f"paths{n}.json"
        lib("write_paths_file")(run_map(snap, call, fallback=True, cache_dir=tmp_path / "cache"), out)
        outs.append((out.read_bytes(), len(call.calls)))
    assert outs[0][1] == 3  # fallback + two traces
    assert outs[1] == (outs[0][0], 0)


def test_cache_key_ignores_entry_and_scope_order_but_not_scope(snap):
    two = commit_snapshot(snap.root.parent / "two", {"pkg/__init__.py": "", CLI: cli_source(["opal", "ruby"])})
    call = FakeCall(trace=lambda _: None, fallback=lambda _: {"entries": []})
    ctx, entries = ctx_for(two, call)
    run_fallback(ctx, entries, ["a.py", "b.py"])
    status, kept, dropped = run_fallback(ctx, list(reversed(entries)), ["b.py", "a.py"])
    assert len(fallback_calls(call)) == 1
    assert (status.status, kept, dropped) == ("ok", [], [])
    run_fallback(ctx, entries, ["a.py", "c.py"])
    assert len(fallback_calls(call)) == 2
