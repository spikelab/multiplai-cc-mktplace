"""Tests for managing labels: create, edit, delete.

Delete is the only destructive command in this tool, and Plane has no undo for
it — removing a label detaches it from every issue silently. So most of what is
pinned down here is refusals: an ambiguous reference, a missing --yes, a name
that collides with a label that already exists, a colour that is not a colour.

The guardrail is checked directly too. A label UUID sits in the path next to a
project UUID and must not be judged against the allowlist, while the project
segment still must be.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

import plane
from conftest import ALLOWED, BAD, CFG, OK, Args, guard

L1 = "aaaa1111-2222-3333-4444-555566667777"
L2 = "aaaa1111-9999-8888-7777-666655554444"
L3 = "bbbb2222-3333-4444-5555-666677778888"

LABELS = [
    {"id": L1, "name": "bug", "color": "#ff0000", "description": ""},
    {"id": L2, "name": "chore", "color": "#00ff00", "description": ""},
    {"id": L3, "name": "docs", "color": "#0000ff", "description": "docs work"},
]

PROJECT = {"id": OK, "name": "Mine", "identifier": "SPK"}


class Calls:
    """Stands in for _request: records every call, answers from a route map."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple] = []

    def __call__(self, method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        bare = path.split("?")[0]
        self.calls.append((method, bare, body, dry_run))
        payload = self.routes.get(bare)
        return payload() if callable(payload) else payload


def stub(monkeypatch, *, labels=LABELS, issues=(), routes=None):
    """Wire resolve_project, the label list and the issue scan to fixtures."""
    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(plane, "project_labels", lambda cfg, pid: [dict(x) for x in labels])
    monkeypatch.setattr(
        plane, "_paginate",
        lambda path, cfg, **kw: iter([dict(x) for x in issues]),
    )
    calls = Calls(routes)
    monkeypatch.setattr(plane, "_request", calls)
    return calls


def run(fn, cfg, args):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(cfg, args)
    return out.getvalue()


# --- the guardrail still judges the project, and only the project ------------


def test_label_write_in_a_blocked_project_is_refused():
    with pytest.raises(plane.GuardError):
        guard("DELETE", f"/projects/{BAD}/labels/{L1}/")
    with pytest.raises(plane.GuardError):
        guard("POST", f"/projects/{BAD}/labels/")
    with pytest.raises(plane.GuardError):
        guard("PATCH", f"/projects/{BAD}/labels/{L1}/")


def test_a_label_uuid_is_not_judged_against_the_project_allowlist():
    # L1 is not in the allowlist and must not need to be: it names a label,
    # not a project.
    guard("DELETE", f"/projects/{OK}/labels/{L1}/")
    guard("PATCH", f"/projects/{OK}/labels/{L1}/")
    guard("POST", f"/projects/{OK}/labels/")


def test_check_self_test_still_passes_with_the_label_cases(monkeypatch):
    monkeypatch.setattr(plane, "list_projects", lambda cfg: [
        {"id": OK, "name": "Mine", "identifier": "SPK", "_allowed": True},
        {"id": BAD, "name": "Theirs", "identifier": "THR", "_allowed": False},
    ])
    out = run(plane.cmd_check, CFG, Args())
    assert "LEAKED" not in out
    assert "BROKEN" not in out
    assert "PASS:" in out


# --- colour parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "given,want",
    [("#FF8800", "#ff8800"), ("ff8800", "#ff8800"), ("#f80", "#ff8800"), ("f80", "#ff8800")],
)
def test_colour_forms_normalise_to_six_digit_hex(given, want):
    assert plane.normalize_color(given) == want


@pytest.mark.parametrize("given", ["", "red", "#gggggg", "#ff88", "#ff88000", "  "])
def test_a_colour_that_is_not_hex_is_refused_locally(given):
    with pytest.raises(plane.PlaneError, match="hex colour"):
        plane.normalize_color(given)


# --- resolving which label is meant ------------------------------------------


def test_a_label_resolves_by_name_uuid_or_unique_id_prefix(monkeypatch):
    stub(monkeypatch)
    assert plane.resolve_label_object(CFG, OK, "bug")["id"] == L1
    assert plane.resolve_label_object(CFG, OK, "BUG")["id"] == L1
    assert plane.resolve_label_object(CFG, OK, L2)["id"] == L2
    assert plane.resolve_label_object(CFG, OK, "bbbb2222")["id"] == L3


def test_an_ambiguous_id_prefix_is_refused_rather_than_guessed(monkeypatch):
    stub(monkeypatch)
    # L1 and L2 share the first eight characters.
    with pytest.raises(plane.PlaneError, match="give more of the id"):
        plane.resolve_label_object(CFG, OK, "aaaa1111")


def test_a_short_id_prefix_does_not_match_at_all(monkeypatch):
    stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="no label matching"):
        plane.resolve_label_object(CFG, OK, "bbbb")


def test_duplicate_names_are_refused_rather_than_resolved(monkeypatch):
    stub(monkeypatch, labels=[
        {"id": L1, "name": "bug"},
        {"id": L2, "name": "Bug"},
    ])
    with pytest.raises(plane.PlaneError, match="2 labels are named"):
        plane.resolve_label_object(CFG, OK, "bug")


def test_an_unknown_label_lists_what_does_exist(monkeypatch):
    stub(monkeypatch)
    with pytest.raises(plane.PlaneError) as exc:
        plane.resolve_label_object(CFG, OK, "nope")
    assert "bug" in str(exc.value) and "chore" in str(exc.value)


# --- create ------------------------------------------------------------------


def test_creating_a_label_posts_name_colour_and_description(monkeypatch):
    calls = stub(monkeypatch, routes={f"/projects/{OK}/labels/": {"id": L3}})
    out = run(plane.cmd_label_create, CFG, Args(
        project=None, name="triage", color="#F80", description="needs a look",
        dry_run=False,
    ))
    method, path, body, dry = calls.calls[0]
    assert (method, path, dry) == ("POST", f"/projects/{OK}/labels/", False)
    assert body == {"name": "triage", "color": "#ff8800", "description": "needs a look"}
    assert "created label 'triage'" in out


def test_creating_a_label_omits_fields_that_were_not_given(monkeypatch):
    calls = stub(monkeypatch, routes={f"/projects/{OK}/labels/": {"id": L3}})
    run(plane.cmd_label_create, CFG, Args(
        project=None, name="triage", color=None, description=None, dry_run=False,
    ))
    assert calls.calls[0][2] == {"name": "triage"}


def test_creating_a_label_that_already_exists_points_at_label_edit(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="already exists"):
        plane.cmd_label_create(CFG, Args(
            project=None, name="Bug", color=None, description=None, dry_run=False,
        ))
    assert calls.calls == []


def test_creating_a_label_with_a_bad_colour_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="hex colour"):
        plane.cmd_label_create(CFG, Args(
            project=None, name="triage", color="reddish", description=None, dry_run=False,
        ))
    assert calls.calls == []


def test_create_dry_run_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    out = run(plane.cmd_label_create, CFG, Args(
        project=None, name="triage", color=None, description=None, dry_run=True,
    ))
    assert calls.calls[0][3] is True
    assert "created label" not in out


# --- edit --------------------------------------------------------------------


def test_editing_a_label_patches_only_the_fields_given(monkeypatch):
    calls = stub(monkeypatch)
    out = run(plane.cmd_label_edit, CFG, Args(
        project=None, label="bug", name=None, color="#123abc", description=None,
        dry_run=False,
    ))
    method, path, body, dry = calls.calls[0]
    assert (method, path, dry) == ("PATCH", f"/projects/{OK}/labels/{L1}/", False)
    assert body == {"color": "#123abc"}
    assert "edited label 'bug'" in out


def test_editing_a_label_can_rename_it(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_label_edit, CFG, Args(
        project=None, label=L1, name="  defect  ", color=None, description=None,
        dry_run=False,
    ))
    assert calls.calls[0][2] == {"name": "defect"}


def test_renaming_onto_an_existing_name_is_refused(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="already named"):
        plane.cmd_label_edit(CFG, Args(
            project=None, label="bug", name="chore", color=None, description=None,
            dry_run=False,
        ))
    assert calls.calls == []


def test_renaming_a_label_to_its_own_name_is_not_a_collision(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_label_edit, CFG, Args(
        project=None, label="bug", name="bug", color=None, description=None,
        dry_run=False,
    ))
    assert calls.calls[0][2] == {"name": "bug"}


def test_an_edit_with_no_fields_is_refused_rather_than_sent_empty(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="nothing to change"):
        plane.cmd_label_edit(CFG, Args(
            project=None, label="bug", name=None, color=None, description=None,
            dry_run=False,
        ))
    assert calls.calls == []


def test_an_edit_can_clear_a_description_with_an_empty_string(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_label_edit, CFG, Args(
        project=None, label="docs", name=None, color=None, description="",
        dry_run=False,
    ))
    assert calls.calls[0][2] == {"description": ""}


def test_edit_dry_run_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    out = run(plane.cmd_label_edit, CFG, Args(
        project=None, label="bug", name=None, color="#111111", description=None,
        dry_run=True,
    ))
    assert calls.calls[0][3] is True
    assert "edited label" not in out


# --- delete ------------------------------------------------------------------


def issues_carrying(*label_ids):
    return [{"id": f"i{n}", "labels": [lid]} for n, lid in enumerate(label_ids)]


def test_deleting_without_yes_refuses_and_sends_nothing(monkeypatch):
    calls = stub(monkeypatch, issues=issues_carrying(L1, L1, L2))
    with pytest.raises(plane.PlaneError, match="without --yes"):
        with redirect_stdout(io.StringIO()):
            plane.cmd_label_delete(CFG, Args(
                project=None, label="bug", yes=False, dry_run=False,
            ))
    assert calls.calls == []


def test_deleting_reports_how_many_issues_carry_the_label_first(monkeypatch):
    calls = stub(monkeypatch, issues=issues_carrying(L1, L1, L2))
    out = run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=True, dry_run=False,
    ))
    assert "2 issue(s)" in out
    method, path, body, dry = calls.calls[0]
    assert (method, path, body, dry) == ("DELETE", f"/projects/{OK}/labels/{L1}/", None, False)
    assert "deleted label 'bug'" in out


def test_an_unused_label_says_so_rather_than_printing_zero(monkeypatch):
    stub(monkeypatch, issues=issues_carrying(L2))
    out = run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=True, dry_run=False,
    ))
    assert "no issues" in out


def test_delete_dry_run_sends_nothing(monkeypatch):
    calls = stub(monkeypatch, issues=issues_carrying(L1))
    out = run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=True, dry_run=True,
    ))
    assert calls.calls[0][3] is True
    assert "deleted label" not in out


def test_usage_counts_an_issue_once_even_with_expanded_labels(monkeypatch):
    monkeypatch.setattr(
        plane, "_paginate",
        lambda path, cfg, **kw: iter([
            {"id": "i1", "labels": [{"id": L1}, {"id": L2}]},
            {"id": "i2", "labels": [{"id": L2}]},
            {"id": "i3", "labels": []},
            {"id": "i4"},
        ]),
    )
    assert plane.label_usage(CFG, OK, L1) == 1
    assert plane.label_usage(CFG, OK, L2) == 2


# --- listing -----------------------------------------------------------------


def test_labels_listing_shows_the_description_it_can_now_set(monkeypatch):
    stub(monkeypatch)
    out = run(plane.cmd_labels, CFG, Args(project=None))
    assert "DESCRIPTION" in out
    assert "docs work" in out
