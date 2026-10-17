"""The on-disk answer cache: reruns resume, re-ask nothing they already have, and write the same bytes."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

from trace_fixtures import CLI, NAMES, FakeCall, chain_hops, cli_source, commit_snapshot, lib, run_map

HEX_FILE = re.compile(r"^[0-9a-f]{64}\.json$")
ANSWERS_DIR = "paths-answers"


def cache(name: str):
    """A block-4 cache name from whichever code_facts module defines it."""
    for mod in ("code_facts.cache", "code_facts.answers", "code_facts.paths", "code_facts.tracing",
                "code_facts.explore", "code_facts"):
        try:
            module = importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AssertionError(f"code_facts does not define {name!r}")


def snapshot(tmp_path, names=NAMES):
    files = {"pkg/__init__.py": "", CLI: cli_source(names),
             "pkg/other.py": "def elsewhere(args):\n    return 0\n",
             "pkg/extra.py": "def extra_one(args):\n    return 0\n\n\ndef extra_two(args):\n    return 0\n"}
    return commit_snapshot(tmp_path / "repo", files)


def good(snap, count=2):
    """Each named entry answers with its first `count` hops; a seed with its own `def` line."""
    seeds = {"elsewhere": "pkg/other.py", "extra_one": "pkg/extra.py"}

    def answer(name):
        if name in seeds:
            return {"hops": [snap.hop(seeds[name], name)]}
        return {"hops": chain_hops(snap, name, count)}
    return answer


def written(result, path) -> bytes:
    lib("write_paths_file")(result, path)
    return path.read_bytes()


def answer_files(cache_dir):
    return sorted(p.name for p in (cache_dir / ANSWERS_DIR).iterdir())


def bump(monkeypatch, constant: str, value: str) -> None:
    """Set `constant` in every loaded code_facts module that holds it, wherever keys read it from."""
    hit = False
    for name, module in list(sys.modules.items()):
        if name.startswith("code_facts") and hasattr(module, constant):
            monkeypatch.setattr(module, constant, value)
            hit = True
    assert hit, f"no code_facts module defines {constant}"


# --- _DiskAnswers ----------------------------------------------------------------------------


KEY_A = "a" * 64
KEY_B = "b" * 64


def test_disk_answers_persist_across_instances_one_json_file_per_key(tmp_path):
    first = cache("_DiskAnswers")(tmp_path / "answers")
    first[KEY_A] = {"hops": [{"path": "é.py", "line_start": 1}]}
    first[KEY_B] = {"error": "boom"}

    files = {p.name: json.loads(p.read_text("utf-8")) for p in (tmp_path / "answers").rglob("*.json")}
    assert files == {f"{KEY_A}.json": {"hops": [{"path": "é.py", "line_start": 1}]},
                     f"{KEY_B}.json": {"error": "boom"}}

    again = cache("_DiskAnswers")(tmp_path / "answers")
    assert dict(again) == {KEY_A: {"hops": [{"path": "é.py", "line_start": 1}]}, KEY_B: {"error": "boom"}}
    assert KEY_A in again and "c" * 64 not in again
    assert len(again) == 2


def test_disk_answers_overwrite_and_delete_reach_the_disk(tmp_path):
    answers = cache("_DiskAnswers")(tmp_path / "answers")
    answers[KEY_A] = {"hops": []}
    answers[KEY_A] = {"hops": [{"x": 1}]}
    answers[KEY_B] = {"error": "e"}
    del answers[KEY_B]

    again = cache("_DiskAnswers")(tmp_path / "answers")
    assert dict(again) == {KEY_A: {"hops": [{"x": 1}]}}
    assert [p.name for p in (tmp_path / "answers").rglob("*") if p.is_file()] == [f"{KEY_A}.json"]
    with pytest.raises(KeyError):
        again[KEY_B]


def test_disk_answers_write_atomically_leaving_the_old_file_on_failure(tmp_path, monkeypatch):
    answers = cache("_DiskAnswers")(tmp_path / "answers")
    answers[KEY_A] = {"hops": ["old"]}
    (path,) = (tmp_path / "answers").rglob("*.json")
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        answers[KEY_A] = {"hops": ["new"]}
    monkeypatch.undo()

    assert path.read_bytes() == before
    # The temp file lives beside the target and is cleaned up.
    assert [p for p in (tmp_path / "answers").rglob("*") if p.is_file()] == [path]
    assert cache("_DiskAnswers")(tmp_path / "answers")[KEY_A] == {"hops": ["old"]}


def test_disk_answers_skip_an_unreadable_file_at_start_up(tmp_path):
    cache("_DiskAnswers")(tmp_path / "answers")[KEY_A] = {"hops": []}
    (empty,) = (tmp_path / "answers").rglob("*.json")
    (empty.parent / f"{KEY_B}.json").write_bytes(b"")

    answers = cache("_DiskAnswers")(tmp_path / "answers")
    assert KEY_B not in answers
    assert dict(answers) == {KEY_A: {"hops": []}}


def test_disk_answers_without_a_directory_are_in_memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answers = cache("_DiskAnswers")(None)
    answers[KEY_A] = {"hops": []}
    assert dict(answers) == {KEY_A: {"hops": []}}
    del answers[KEY_A]
    assert len(answers) == 0
    assert list(tmp_path.iterdir()) == []


def test_purge_failed_deletes_only_error_entries_from_memory_and_disk(tmp_path):
    answers = cache("_DiskAnswers")(tmp_path / "answers")
    answers[KEY_A] = {"hops": []}
    answers[KEY_B] = {"error": "rate limited"}
    cache("purge_failed")(answers)

    assert dict(answers) == {KEY_A: {"hops": []}}
    assert dict(cache("_DiskAnswers")(tmp_path / "answers")) == {KEY_A: {"hops": []}}


# --- cache_key ---------------------------------------------------------------------------------


def test_cache_key_is_a_sha256_hex_that_ignores_key_order_and_depends_on_kind_and_payload():
    key = cache("cache_key")
    payload = {"version": "paths-trace.v1", "commit": "abc", "entry": ["argparse", CLI, 3, None], "hop_limit": 20}
    reordered = dict(reversed(list(payload.items())))

    k = key("trace", payload)
    assert re.fullmatch(r"[0-9a-f]{64}", k)
    assert key("trace", reordered) == k
    assert key("reask", payload) != k
    assert key("fallback", payload) != k
    assert key("trace", {**payload, "commit": "abd"}) != k


def test_trace_key_covers_the_entry_name_and_invocation_the_prompt_shows():
    tracing = importlib.import_module("code_facts.tracing")
    ctx = SimpleNamespace(commit="c" * 40, changed_lines=None, places=[])
    base = lib("Entry")(kind="skill_command", name="pr: pr_pipeline open", location={"path": CLI, "line": 3},
                        handler=None, invocation="python3 -m pr_pipeline open", invoked_by="an agent following x",
                        known_edges=[], also_invoked_by=[])
    renamed = base.model_copy(update={"name": "pr: pr_pipeline open --draft"})
    reinvoked = base.model_copy(update={"invocation": "python3 -m pr_pipeline open --draft"})
    for other in (renamed, reinvoked):
        assert tracing._trace_prompt(other, ctx) != tracing._trace_prompt(base, ctx)
        assert tracing.trace_key(other, ctx) != tracing.trace_key(base, ctx)


# --- map_paths with a cache_dir ------------------------------------------------------------


def test_second_run_on_the_same_cache_makes_no_calls_and_writes_the_same_bytes(tmp_path):
    snap = snapshot(tmp_path, ["opal", "ruby"])
    # opal's first answer breaks at hop 3 (wrong file), so it is re-asked once.
    broken = lambda name: ({"hops": chain_hops(snap, name, 2) + [snap.hop("pkg/other.py", "elsewhere")]}
                           if name == "opal" else {"hops": chain_hops(snap, name, 3)})
    first = FakeCall(trace=broken, reask=lambda name: {"hops": chain_hops(snap, name, 3)})
    cache_dir = tmp_path / "cache"
    one = written(run_map(snap, first, cache_dir=cache_dir), tmp_path / "one.json")
    assert sorted(c.stage for c in first.calls) == ["reask", "trace", "trace"]

    files = answer_files(cache_dir)
    assert len(files) == 3 and all(HEX_FILE.match(f) for f in files)

    second = FakeCall(trace=broken, reask=lambda name: {"hops": chain_hops(snap, name, 3)})
    two = written(run_map(snap, second, cache_dir=cache_dir), tmp_path / "two.json")
    assert second.calls == []
    assert two == one
    assert answer_files(cache_dir) == files


def test_without_a_cache_dir_nothing_carries_over_between_runs(tmp_path):
    snap = snapshot(tmp_path, ["opal", "ruby"])
    for _ in range(2):
        call = FakeCall(trace=good(snap))
        run_map(snap, call)
        assert len(call.calls) == 2


def test_reordered_seeds_changed_lines_and_places_hit_the_cache(tmp_path):
    snap = snapshot(tmp_path, ["opal", "ruby", "jade"])
    Seed, Location = lib("Seed"), lib("Location")
    seeds = [Seed(path="pkg/other.py", symbol="elsewhere"), Seed(path="pkg/extra.py", symbol="extra_one")]
    changed = {CLI: [snap.line(CLI, "def cmd_opal("), snap.line(CLI, "def step1_ruby(")],
               "pkg/other.py": [1, 2]}
    places = [Location(path=CLI, line=snap.line(CLI, "def step2_jade(")), Location(path="pkg/extra.py", line=5)]
    cache_dir = tmp_path / "cache"

    first = FakeCall(trace=good(snap), names=NAMES + ["elsewhere", "extra_one"])
    one = written(run_map(snap, first, cache_dir=cache_dir, seeds=seeds, changed_lines=changed, places=places),
                  tmp_path / "one.json")
    assert {c.stage for c in first.calls} >= {"trace"}

    reversed_changed = {path: list(reversed(lines)) for path, lines in reversed(list(changed.items()))}
    second = FakeCall(trace=good(snap), names=NAMES + ["elsewhere", "extra_one"])
    two = written(run_map(snap, second, cache_dir=cache_dir, seeds=list(reversed(seeds)),
                          changed_lines=reversed_changed, places=list(reversed(places))), tmp_path / "two.json")
    assert second.calls == []
    assert two == one


def test_adding_a_changed_line_rerequests_the_entries_whose_scope_it_changes(tmp_path):
    snap = snapshot(tmp_path, ["opal", "ruby", "jade"])
    cache_dir = tmp_path / "cache"
    changed = {CLI: [snap.line(CLI, "def cmd_opal(")]}
    first = FakeCall(trace=good(snap))
    run_map(snap, first, cache_dir=cache_dir, changed_lines=changed)
    assert len(first.calls) == 3
    same = FakeCall(trace=good(snap))
    run_map(snap, same, cache_dir=cache_dir, changed_lines=changed)
    assert same.calls == []

    more = {CLI: changed[CLI] + [snap.line(CLI, "def cmd_ruby(")]}
    second = FakeCall(trace=good(snap))
    run_map(snap, second, cache_dir=cache_dir, changed_lines=more)
    # Every trace prompt carries the whole change scope, so every entry is asked again.
    assert sorted(c.entry for c in second.calls if c.stage == "trace") == ["jade", "opal", "ruby"]


def test_bumping_the_trace_prompt_version_rerequests_every_entry(tmp_path, monkeypatch):
    snap = snapshot(tmp_path, ["opal", "ruby", "jade"])
    cache_dir = tmp_path / "cache"
    run_map(snap, FakeCall(trace=good(snap)), cache_dir=cache_dir)
    warm = FakeCall(trace=good(snap))
    run_map(snap, warm, cache_dir=cache_dir)
    assert warm.calls == []

    bump(monkeypatch, "PATHS_TRACE_PROMPT_VERSION", "paths-trace.v2")
    second = FakeCall(trace=good(snap))
    run_map(snap, second, cache_dir=cache_dir)
    assert sorted(c.entry for c in second.calls if c.stage == "trace") == ["jade", "opal", "ruby"]


def test_bumping_the_reask_prompt_version_rerequests_only_reasks(tmp_path, monkeypatch):
    snap = snapshot(tmp_path, ["opal", "ruby"])
    broken = lambda name: ({"hops": chain_hops(snap, name, 1) + [snap.hop("pkg/other.py", "elsewhere")]}
                           if name == "opal" else {"hops": chain_hops(snap, name, 2)})
    fix = lambda name: {"hops": chain_hops(snap, name, 2)}
    cache_dir = tmp_path / "cache"
    run_map(snap, FakeCall(trace=broken, reask=fix), cache_dir=cache_dir)

    bump(monkeypatch, "PATHS_REASK_PROMPT_VERSION", "paths-reask.v2")
    second = FakeCall(trace=broken, reask=fix)
    run_map(snap, second, cache_dir=cache_dir)
    assert [(c.stage, c.entry) for c in second.calls] == [("reask", "opal")]


def test_a_failed_call_is_asked_again_on_the_next_run_but_not_within_its_run(tmp_path):
    class Flaky(Exception):
        pass

    snap = snapshot(tmp_path, ["opal", "ruby"])
    cache_dir = tmp_path / "cache"
    first = FakeCall(trace=lambda name: Flaky("rate limited") if name == "opal" else good(snap)(name))
    result = run_map(snap, first, cache_dir=cache_dir, recoverable=(Flaky,))
    assert len(first.of("opal")) == 1
    assert [(d.entry.name, d.reason) for d in result.dropped] == [("opal", "call_failed")]

    second = FakeCall(trace=good(snap))
    result = run_map(snap, second, cache_dir=cache_dir, recoverable=(Flaky,))
    assert [(c.stage, c.entry) for c in second.calls] == [("trace", "opal")]
    assert sorted(p.entry.name for p in result.paths) == ["opal", "ruby"]
    assert result.dropped == []


def test_a_cached_answer_that_is_not_a_valid_answer_is_asked_again(tmp_path):
    snap = snapshot(tmp_path, ["opal", "ruby"])
    cache_dir = tmp_path / "cache"
    first = FakeCall(trace=good(snap), fallback=lambda _: {"entries": []})
    one = written(run_map(snap, first, cache_dir=cache_dir, fallback=True), tmp_path / "one.json")
    assert sorted(c.stage for c in first.calls) == ["paths:fallback", "trace", "trace"]
    # Valid JSON objects, with no error or malformed marker, that are neither a trace nor a fallback answer.
    for name in answer_files(cache_dir):
        (cache_dir / ANSWERS_DIR / name).write_text(json.dumps({"hops": "none", "entries": "none"}))

    second = FakeCall(trace=good(snap), fallback=lambda _: {"entries": []})
    two = written(run_map(snap, second, cache_dir=cache_dir, fallback=True), tmp_path / "two.json")
    assert sorted((c.stage, c.entry) for c in second.calls) == [
        ("paths:fallback", None), ("trace", "opal"), ("trace", "ruby")]
    assert two == one


def test_interrupted_run_resumes_with_only_the_missing_traces_and_the_same_output(tmp_path):
    class Interrupted(Exception):
        pass

    names = NAMES[:5]
    snap = snapshot(tmp_path, names)
    late = set(names[3:])
    recorder = FakeCall(trace=good(snap))

    async def interrupting(prompt, schema, **kwargs):
        if recorder._entry(prompt) in late:
            # The three quick answers are stored before the interruption lands.
            await asyncio.sleep(0.2)
            raise Interrupted("killed")
        return await recorder(prompt, schema, **kwargs)

    cache_dir = tmp_path / "cache"
    with pytest.raises(Interrupted):
        run_map(snap, interrupting, cache_dir=cache_dir, concurrency=5)
    assert len(answer_files(cache_dir)) == 3

    resumed_call = FakeCall(trace=good(snap))
    resumed = written(run_map(snap, resumed_call, cache_dir=cache_dir), tmp_path / "resumed.json")
    assert sorted(c.entry for c in resumed_call.calls) == sorted(late)
    assert all(c.stage == "trace" for c in resumed_call.calls)

    whole = written(run_map(snap, FakeCall(trace=good(snap)), cache_dir=tmp_path / "fresh"), tmp_path / "whole.json")
    assert resumed == whole
