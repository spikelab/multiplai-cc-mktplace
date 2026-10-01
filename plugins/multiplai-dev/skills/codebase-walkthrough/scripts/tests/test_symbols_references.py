"""symbols.py and references.py find every planted reference, and none that sits in a comment."""

from __future__ import annotations

import shutil

import pytest

from walkthrough_pipeline import lsp, references, repos as repos_mod, symbols, target
from walkthrough_pipeline.partition import partition


@pytest.fixture
def built(ws, tmp_path):
    info, repo = target.build_target(ws["engine"] / "bookings", tmp_path)
    return info, repo, tmp_path / "snap" / "engine"


def test_target_lists_files_and_counts_migrations(built):
    info, _, snap = built
    assert info.package == "bookings"
    assert "bookings/migrations/0001_initial.py" not in info.files
    assert info.migrations == 2 and info.latest_migration.endswith("0002_ospite.py")
    assert not (snap / ".env").exists()          # committed, but never snapshotted


def test_symbols_defines_and_calls(built):
    info, _, snap = built
    entries, warnings = symbols.collect(info, snap)
    got = {(e.kind, e.name) for e in entries}
    assert {("url", "hooks/acme/"), ("celery-task", "bookings.poll"), ("model", "Reservation"),
            ("function", "ack_booking"), ("setting", "ACME_ENV"), ("env", "ACME_API_KEY"),
            ("http", "/bookings/{booking_id}/ack"), ("http", "/webhooks")} <= got
    model = next(e for e in entries if e.kind == "model")
    assert model.detail["table"] == "bookings_reservation"
    assert symbols.outbound_api_paths(entries) == ["/bookings/{}/ack", "/webhooks"]
    assert warnings == []
    defs = symbols.setting_definitions(info, snap, {"ACME_ENV"})
    assert [(d.name, d.path, d.detail["env"]) for d in defs] == [("ACME_ENV", "engine/project/settings.py", "ACME_ENV")]


def test_symbols_warns_for_a_language_without_a_pack(built):
    info, _, snap = built
    ts = info.model_copy(update={"files": info.files + ["bookings/widget.ts"]})
    _, warnings = symbols.collect(ts, snap)
    assert warnings == ["no symbol pack for TypeScript: 1 file(s) in the target were not parsed for symbols"]


def test_references_find_every_planted_one_and_skip_comments(ws, built, tmp_path):
    info, repo, snap = built
    entries, _ = symbols.collect(info, snap)
    found, root, _ = repos_mod.discover(repo)
    assert root == ws["ws"].resolve() and set(found) == {"engine", "front", "warehouse"}
    refs = references.scan_all(found, info, entries, tmp_path / "scan")
    got = sorted((r.repo, r.kind, r.path, r.line, r.method) for r in refs)
    assert got == [
        ("engine", "celery-name", "engine/project/settings.py", 9, "ast"),
        ("engine", "python-import", "engine/reports/usage.py", 2, "ast"),
        ("engine", "url-include", "engine/project/urls.py", 4, "ast"),
        ("front", "http-route", "front/src/api.ts", 5, "ast-grep"),
        ("warehouse", "sql-table", "warehouse/models.sql", 3, "sql-identifier"),
    ]


@pytest.mark.parametrize("literal, route, hit", [
    ("`${BASE}/hooks/acme/`", "hooks/acme/", True),
    ("'/direct-booking/create/'", "api/channex/direct-booking/create/", True),
    ("`/closures/${id}/`", "api/channex/calendar/closures/list/", False),
    ("`/calendar/closures/${id}/`", "api/channex/calendar/closures/<int:pk>/", True),
    ("'/create/'", "api/channex/rate-plans/create/", False),
])
def test_route_matching(literal, route, hit):
    assert references.route_matches(literal, route) is hit


def test_search_root_is_the_nearest_ancestor_with_several_repos(ws):
    assert repos_mod.default_search_root(ws["engine"]) == ws["ws"].resolve()


@pytest.mark.skipif(shutil.which("pyright-langserver") is None, reason="pyright-langserver not installed")
async def test_lsp_finds_the_import_outside_the_target(built):
    info, _, snap = built
    entries, _ = symbols.collect(info, snap)
    refs, report = await lsp.find_references(info, snap, entries, timeout_s=120)
    assert report["status"] == "ok"
    assert ("engine/reports/usage.py", 2) in {(r.path, r.line) for r in refs}
    assert all(r.method == "lsp" for r in refs)


async def test_lsp_skips_when_told(built):
    info, _, snap = built
    refs, report = await lsp.find_references(info, snap, [], timeout_s=0)
    assert refs == [] and report["status"].startswith("skipped")


def test_partition_keeps_directories_together_and_isolates_big_files(built):
    info, _, _ = built
    units = partition(info, 20)
    flat = [f for u in units for f in u.files]
    assert sorted(flat) == sorted(info.ws(f) for f in info.files)
    assert all(u.lines <= 20 or (u.oversize and len(u.files) == 1) for u in units)
