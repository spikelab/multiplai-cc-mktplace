from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from code_facts import (
    UNRESOLVED,
    Citation,
    ExploreOutput,
    Unit,
    ask,
    bounded,
    check_citation,
    citation_keeper,
    gate_answers,
    reask_units,
    run_tasks,
    ws_path,
)


def cit(path="app/main.py", a=4, b=4, quote="def greet(name):", side="head"):
    return {"path": path, "line_start": a, "line_end": b, "quote": quote, "side": side}


# --- the citation check ----------------------------------------------------------


def test_check_citation_passes_across_whitespace_and_reads_the_cited_side(resolve):
    assert check_citation(Citation(**cit(quote="def   greet(name):")), resolve) is None
    # The same quote at line 1 is in the base commit only.
    assert check_citation(Citation(**cit(a=1, b=1, side="base")), resolve) is None
    assert check_citation(Citation(**cit(a=1, b=1)), resolve) == "quote not at cited lines"


def test_check_citation_reasons(resolve):
    assert check_citation(Citation(**cit(path="app/util.py", side="base", quote="X")), resolve) == \
        "path not at commit"
    assert check_citation(Citation(**cit(path="elsewhere/x.py")), resolve) == UNRESOLVED
    assert check_citation(Citation(**cit(path="elsewhere/x.py", quote="  ")), resolve) == "empty quote"


def test_keeper_makes_paths_relative_and_records_reasons(resolve, tmp_path):
    reasons: list[str] = []
    keep = citation_keeper(resolve, root=tmp_path, reasons=reasons)
    kept = keep(Citation(**cit(path=f"{tmp_path}/app/main.py")))
    assert kept is not None and kept.path == "app/main.py"
    assert keep(Citation(**cit(quote="nope"))) is None
    assert keep(None) is None
    assert reasons == ["quote not at cited lines"]


def test_ws_path_strips_root_dots_and_backticks(tmp_path):
    assert ws_path(f"`{tmp_path}/a/b.py`", tmp_path) == "a/b.py"
    assert ws_path("././a/b.py", Path("/elsewhere")) == "a/b.py"


# --- gating -----------------------------------------------------------------------


def _answers():
    return {
        "unit:u1": ExploreOutput.model_validate({
            "facts": [{"claim": "greet builds a greeting", "citations": [cit()]},
                      {"claim": "invented", "citations": [cit(quote="nothing like this")]}],
            "files": [{"path": "app/main.py", "purpose": "says hello", "citation": cit()}],
            "interfaces": [{"claim": "greet(name)", "citations": [cit()]}],
            "gotchas": [{"claim": "logs every call", "citations": [cit(a=5, b=5, quote="logging.info")]}],
            "terms": [{"term": "greet", "meaning": "say hi", "citation": cit()},
                      {"term": "ghost", "meaning": "?", "citation": cit(quote="ghost")}],
            "conventions": [{"claim": "logs with the logging module", "citations": [cit(a=1, b=1,
                                                                                         quote="import logging")]}],
        }).model_dump(),
        "unit:u2": {"error": "planted failure"},
    }


def test_gate_answers_keeps_only_cited_facts_and_numbers_them(resolve):
    g = gate_answers(_answers(), citation_keeper(resolve))
    assert [f.id for f in g.facts] == ["f1", "f2", "f3", "f4"]
    assert [f.source for f in g.facts] == ["unit:u1/fact", "unit:u1/interface", "unit:u1/gotcha", "unit:u1/file"]
    assert [c.id for f in g.facts for c in f.citations] == ["c1", "c2", "c3", "c4"]
    assert (g.interfaces, g.gotchas, g.conventions) == (["f2"], ["f3"], [])
    assert g.purposes == {"app/main.py": "says hello"}
    assert [t.term for t in g.terms] == ["greet"]
    assert [(d.what, d.reason) for d in g.dropped] == [("invented", "no citation passed"),
                                                       ("term ghost", "no citation passed")]


def test_conventions_are_kept_only_when_asked_for(resolve):
    g = gate_answers(_answers(), citation_keeper(resolve), conventions=True)
    assert g.conventions == ["f4"]
    assert g.facts[3].claim == "logs with the logging module"


# --- asking -----------------------------------------------------------------------


class Fatal(Exception):
    pass


class Failed(Exception):
    pass


def test_ask_stores_answers_records_failures_and_reraises_fatal():
    calls: list[dict] = []

    async def call(prompt, schema, **kw):
        calls.append(kw)
        if prompt == "fail":
            raise Failed("no answer\nmore detail")
        if prompt == "fatal":
            raise Fatal("untrusted repo")
        return schema.model_validate({"facts": [{"claim": "c", "citations": [cit()]}]})

    answers: dict[str, dict] = {}
    errors: list[str] = []
    lines: list[str] = []
    kw = dict(call_kwargs={"allowed_tools": ["Read"], "cwd": "/snap"}, errors=errors, recoverable=(Failed,),
              fatal=(Fatal,), progress=lines.append)
    asyncio.run(ask("unit:u1", "ok", call, answers, **kw))
    asyncio.run(ask("unit:u2", "fail", call, answers, **kw))
    with pytest.raises(Fatal):
        asyncio.run(ask("unit:u3", "fatal", call, answers, **kw))
    assert len(answers["unit:u1"]["facts"]) == 1
    assert answers["unit:u2"] == {"error": "no answer"}
    assert errors == ["explore unit:u2: no answer"]
    assert "unit:u3" not in answers
    assert lines == ["  explore unit:u1: 1 facts", "  explore unit:u2: 0 facts"]
    assert calls[0] == {"budget_label": "explore:unit", "allowed_tools": ["Read"], "cwd": "/snap"}


def test_ask_treats_an_encode_failure_like_a_call_failure():
    async def call(prompt, schema, **kw):
        return object()

    answers: dict[str, dict] = {}
    errors: list[str] = []
    asyncio.run(ask("unit:u1", "ok", call, answers, call_kwargs={}, errors=errors))
    assert answers["unit:u1"]["error"]
    assert errors == [f"explore unit:u1: {answers['unit:u1']['error']}"]


def test_run_tasks_skips_answered_keys():
    asked: list[str] = []

    async def call(prompt, schema, **kw):
        asked.append(prompt)
        return schema()

    answers = {"unit:u1": {"facts": []}}
    asyncio.run(run_tasks([("unit:u1", "p1"), ("unit:u2", "p2")], call, answers, concurrency=2,
                          call_kwargs={}, errors=[]))
    assert asked == ["p2"]
    assert set(answers) == {"unit:u1", "unit:u2"}


def test_reask_names_missed_files_once():
    units = [Unit(id="u1", files=["a.py", "b.py"], lines=10), Unit(id="u2", files=["c.py"], lines=5)]
    retry = reask_units(units, {"b.py"}, {}, lambda u, miss: f"{u.id}:{','.join(miss)}")
    assert retry == [("unit:u1:reask", "u1:b.py")]
    assert reask_units(units, {"b.py"}, {"unit:u1:reask": {}}, lambda u, m: "") == []


def test_bounded_keeps_order_limits_concurrency_and_raises_the_first_failure():
    running = 0
    peak = 0

    async def fn(i):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return i * 2

    assert asyncio.run(bounded(range(6), fn, 2)) == [0, 2, 4, 6, 8, 10]
    assert peak == 2

    async def boom(i):
        if i == 1:
            raise Failed("stop")
        await asyncio.sleep(0.05)

    with pytest.raises(Failed):
        asyncio.run(bounded(range(4), boom, 4))
