"""Web entry points as `map_paths` caller seeds, and boundary edges as its known places."""

from __future__ import annotations

import logging

import pytest

import path_fixtures
from code_facts import Location, Seed
from fakes import FakeAgents
from test_paths_stage import _paths_body
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.models import Reference, SymbolEntry, TargetInfo, WalkState
from walkthrough_pipeline.render import section_texts

SEEDS = [Seed(path="shop/views.py", symbol="pay_hook", name=path_fixtures.WEBHOOK),
         Seed(path="shop/views.py", symbol="status", name=path_fixtures.CALLED),
         Seed(path="shop/tasks.py", symbol="sweep", name=path_fixtures.BEAT)]


def _web_run(tmp_path, monkeypatch, *extra, cli=True):
    ws = tmp_path / "ws"
    module = path_fixtures.web_repo(ws, cli=cli)
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents(trace_plan=path_fixtures.web_plan()))
    code = main(path_fixtures.run_args(module, ws, tmp_path, *extra))
    return code, tmp_path / "out" / "shop-walkthrough.md"


def _names(result) -> tuple[list[str], dict[str, str]]:
    return [p.entry.name for p in result.paths], {d.entry.name: d.reason for d in result.dropped}


def test_web_entries_are_caller_seeds_at_their_handlers_in_the_old_kind_order(tmp_path, monkeypatch, spy):
    code, _ = _web_run(tmp_path, monkeypatch)
    assert code == 0
    # Webhook routes, then routes another repository calls, then beat entries, then other routes:
    # `status` is called from mobile/, so it is seeded once, under that kind.
    assert list(spy.calls[0]["seeds"]) == SEEDS


def test_boundary_edges_are_the_known_places(tmp_path, monkeypatch, spy):
    code, _ = _web_run(tmp_path, monkeypatch)
    assert code == 0
    places = list(spy.calls[0]["places"])
    assert places == [
        Location(path="shop/urls.py", line=7),          # the route mobile/ calls: its target-repo side
        Location(path="shop/urls.py", line=6),          # the webhook route
        Location(path="shop/tasks.py", line=6),         # the task
        Location(path="project/settings.py", line=3),   # the beat entry naming it
    ]
    assert all(not p.path.startswith("mobile") for p in places)


def test_every_web_entry_is_traced_or_over_cap(tmp_path, monkeypatch, spy):
    code, md_path = _web_run(tmp_path, monkeypatch, "--scenarios", "2")
    assert code == 0
    traced, dropped = _names(spy.results[0])
    assert len(traced) == 2
    for seed in SEEDS:
        assert seed.name in traced or dropped.get(seed.name) == "over_cap", seed.name
    coverage = section_texts(md_path.read_text(), "Coverage")[0]
    for name, reason in dropped.items():
        assert f"(`{name}`) | cap | `{reason}` |" in coverage


def test_the_seed_reached_only_over_http_is_traced_or_listed(tmp_path, monkeypatch, spy):
    code, md_path = _web_run(tmp_path, monkeypatch)
    assert code == 0
    result = spy.results[0]
    traced, dropped = _names(result)
    missing = [d for d in result.diagnostics if d.reason == "seed_not_found"]
    assert path_fixtures.CALLED in traced or dropped.get(path_fixtures.CALLED) == "unreachable" or missing
    if path_fixtures.CALLED in traced:
        n = traced.index(path_fixtures.CALLED) + 1
        assert f"**p{n}** — `{path_fixtures.CALLED}` (caller seed)" in _paths_body(md_path.read_text())


def test_a_cli_entry_ranked_before_the_web_seeds_renders_first(tmp_path, monkeypatch, spy):
    code, md_path = _web_run(tmp_path, monkeypatch)
    assert code == 0
    traced, _ = _names(spy.results[0])
    assert traced[0] == "settle"                         # hop 2 covers all four lines of `charge`
    assert set(traced[1:]) == {s.name for s in SEEDS}
    body = _paths_body(md_path.read_text())
    assert body.index("**p1** — `settle` (click command)") < body.index("(caller seed)")
    for n, name in enumerate(traced, start=1):
        assert f"**p{n}** — `{name}`" in body
    assert main(["check", str(md_path)]) == 0


# --- web_seeds and known_places on hand-made states ------------------------------------------------


def _state(symbols=(), references=()) -> WalkState:
    target = TargetInfo(repo_key="site", repo_path="/nowhere/site", subpath="shop", head_sha="0" * 40,
                        name="shop", package="shop", files=["shop/urls.py"])
    return WalkState(target=target, symbols=list(symbols), references=list(references))


def _url(name, line, view):
    return SymbolEntry(kind="url", name=name, path="site/shop/urls.py", line=line, quote=f"path('{name}', {view})",
                       detail={"view": view})


def _ref(repo, kind, symbol, path, line):
    return Reference(repo=repo, kind=kind, symbol=symbol, path=path, line=line, quote=symbol, method="ast")


def test_an_unresolved_handler_falls_back_to_the_registration_line():
    from walkthrough_pipeline.stages.trace import web_seeds
    state = _state(symbols=[_url("docs/", 6, "include('other.urls')"),
                            _url("api/view/", 7, "views.Missing.as_view()")],
                   references=[_ref("site", "celery-name", "shop.gone", "site/project/settings.py", 3),
                               _ref("mobile", "celery-name", "shop.elsewhere", "mobile/jobs.py", 2)])
    assert web_seeds(state) == [
        Seed(path="project/settings.py", line=3, name="shop.gone (beat)"),   # no task: the beat entry itself
        Seed(path="shop/urls.py", line=6, name="HTTP /docs/"),
        Seed(path="shop/urls.py", line=7, name="HTTP /api/view/"),
    ]                                                                         # mobile/ is not under the root


def test_a_class_view_is_seeded_at_its_class():
    from walkthrough_pipeline.stages.trace import web_seeds
    cls = SymbolEntry(kind="class", name="Hook", path="site/shop/views.py", line=4, quote="class Hook(View):")
    state = _state(symbols=[_url("webhooks/in/", 6, "views.Hook.as_view()"), cls])
    assert web_seeds(state) == [Seed(path="shop/views.py", symbol="Hook", name="HTTP /webhooks/in/")]


@pytest.mark.parametrize("references", [[], [_ref("warehouse", "sql-table", "shop_order", "warehouse/m.sql", 4)]])
def test_no_target_repo_edges_means_no_places_and_no_seeds(references):
    from walkthrough_pipeline.stages.trace import known_places, web_seeds
    state = _state(references=references)
    assert known_places(state) == [] and web_seeds(state) == []


def test_a_route_another_repo_calls_is_matched_by_its_full_route_not_a_suffix():
    """Mounted at `v1/`, `items/status/` is the route mobile/ calls; `status/` only shares its last segment."""
    from walkthrough_pipeline.stages.trace import known_places, web_seeds
    fns = [SymbolEntry(kind="function", name=n, path="site/shop/views.py", line=line, quote=f"def {n}(request):")
           for n, line in (("status", 1), ("item_status", 5))]
    state = _state(symbols=[_url("status/", 6, "views.status"), _url("items/status/", 7, "views.item_status"), *fns],
                   references=[_ref("site", "url-include", "shop.urls mounted at 'v1/'", "site/project/urls.py", 3),
                               _ref("mobile", "http-route", "v1/items/status/", "mobile/api.py", 4)])
    assert web_seeds(state) == [
        Seed(path="shop/views.py", symbol="item_status", name="mobile: HTTP /v1/items/status/"),
        Seed(path="shop/views.py", symbol="status", name="HTTP /status/"),
    ]
    assert known_places(state)[0] == Location(path="shop/urls.py", line=7)


def test_an_edge_with_no_target_location_is_logged_by_its_type_only(caplog):
    from walkthrough_pipeline.stages.trace import known_places
    state = _state(references=[_ref("mobile", "http-route", "v1/nothing/", "mobile/api.py", 4),
                               _ref("mobile", "celery-name", "shop.missing", "mobile/jobs.py", 2)])
    with caplog.at_level(logging.INFO, logger="walkthrough_pipeline.stages.trace"):
        assert known_places(state) == []
    left_out = [r.getMessage() for r in caplog.records if "no location in the target repo" in r.getMessage()]
    assert len(left_out) == 2
    assert "http-route" in left_out[0] and "celery-name" in left_out[1]
    assert not any("v1/nothing" in m or "shop.missing" in m for m in left_out)
