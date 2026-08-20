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
import json
from contextlib import redirect_stdout

import pytest

import plane
from conftest import BAD, CFG, OK, Args, guard

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
        self.listings: list[str] = []
        self.scans: list[tuple] = []

    def __call__(self, method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        bare = path.split("?")[0]
        self.calls.append((method, bare, body, dry_run))
        payload = self.routes.get(bare)
        return payload() if callable(payload) else payload


def stub(monkeypatch, *, labels=LABELS, issues=(), routes=None):
    """Wire resolve_project, the label list and the issue scan to fixtures.

    The returned recorder also carries `listings` (one entry per project_labels
    call) and `scans` (the params of every _paginate call), because two findings
    here are about how often the tool fetches and how much it asks for.
    """
    calls = Calls(routes)

    def listing(cfg, pid):
        calls.listings.append(pid)
        return [dict(x) for x in labels]

    def paginate(path, cfg, **kw):
        calls.scans.append((path, kw.get("params")))
        return iter([dict(x) for x in issues])

    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(plane, "project_labels", listing)
    monkeypatch.setattr(plane, "_paginate", paginate)
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


@pytest.mark.parametrize(
    "given",
    ["", "red", "#gggggg", "#ff88", "#ff88000", "  ", "##ff8800", "###f80", "#"],
)
def test_a_colour_that_is_not_hex_is_refused_locally(given):
    # The repeated-hash cases are why this does not use lstrip("#"): that takes a
    # character set, so it would accept "##ff8800" as a colour.
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


def test_a_short_id_prefix_says_it_is_short_not_that_nothing_matches(monkeypatch):
    # "bbbb" does prefix L3's id. Reporting "no label matching" there tells the
    # user the label does not exist when it does.
    stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="too short"):
        plane.resolve_label_object(CFG, OK, "bbbb")


def test_a_reference_matching_nothing_at_all_lists_what_exists(monkeypatch):
    stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="no label matching"):
        plane.resolve_label_object(CFG, OK, "zzzz")


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


# --- one resolver, so a reference cannot mean two things ----------------------


def test_attaching_and_editing_read_a_reference_the_same_way(monkeypatch):
    """`--label <full-uuid>` used to fail on create/update and work on
    label-edit, because each command had its own matching rules."""
    stub(monkeypatch)
    assert plane.resolve_label(CFG, OK, L2) == L2
    assert plane.resolve_label(CFG, OK, "bbbb2222-3333") == L3
    # An ambiguous reference is never answered by creating a third label.
    with pytest.raises(plane.PlaneError, match="2 labels are named"):
        plane.resolve_label(
            CFG, OK, "bug",
            labels=[{"id": L1, "name": "bug"}, {"id": L2, "name": "Bug"}],
        )


def test_the_label_list_is_fetched_once_per_command_not_once_per_flag(monkeypatch):
    calls = stub(monkeypatch)
    payload: dict = {}
    plane._apply_assignment_flags(
        CFG,
        Args(assignee=None, label=["bug", "chore", "docs"], create_labels=False,
             estimate=None, dry_run=False),
        OK, payload,
    )
    assert payload["labels"] == [L1, L2, L3]
    assert len(calls.listings) == 1


def test_repeating_a_new_label_name_creates_it_once(monkeypatch):
    calls = stub(monkeypatch, labels=[], routes={f"/projects/{OK}/labels/": {"id": L3}})
    payload: dict = {}
    plane._apply_assignment_flags(
        CFG,
        Args(assignee=None, label=["triage", "triage"], create_labels=True,
             estimate=None, dry_run=False),
        OK, payload,
    )
    assert [c[0] for c in calls.calls] == ["POST"]
    assert payload["labels"] == [L3, L3]


def test_a_short_new_label_name_that_prefixes_an_id_can_still_be_created(monkeypatch):
    # "bbbb" prefixes L3's id, so resolution reports it as a truncated id. With
    # --create-labels that has to mean "create a label called bbbb", not fail.
    calls = stub(monkeypatch, routes={f"/projects/{OK}/labels/": {"id": L2}})
    assert plane.resolve_label(CFG, OK, "bbbb", create=True) == L2
    assert calls.calls[0][:2] == ("POST", f"/projects/{OK}/labels/")


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


def test_dry_run_previews_the_delete_without_needing_yes(monkeypatch):
    """The safe command must not be one flag away from the destructive one.

    Requiring --yes to *preview* means the only way to see the request is to
    type the confirmation, and dropping --dry-run from that line deletes.
    """
    calls = stub(monkeypatch, issues=issues_carrying(L1))
    run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=False, dry_run=True,
    ))
    method, path, _body, dry = calls.calls[0]
    assert (method, path, dry) == ("DELETE", f"/projects/{OK}/labels/{L1}/", True)


def test_the_usage_count_names_what_it_scanned(monkeypatch):
    """The count gates an irreversible write, so it must not read as absolute:
    archived issues are not in /issues/ and are not counted."""
    stub(monkeypatch, issues=issues_carrying(L2))
    out = run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=True, dry_run=False,
    ))
    assert "no issues" in out
    assert "archived issues are not scanned" in out


def test_the_usage_scan_does_not_ask_for_issue_bodies(monkeypatch):
    """A count needs every page, so the payload is the whole cost — and rule 11
    has the user run this twice."""
    calls = stub(monkeypatch, issues=issues_carrying(L1))
    run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=True, dry_run=False,
    ))
    scans = [params for path, params in calls.scans if path.endswith("/issues/")]
    assert scans and all((params or {}).get("fields") == "id,labels" for params in scans)


def test_deleting_a_label_group_parent_is_refused_not_counted(monkeypatch):
    """Plane deletes a group's children with it, and the issue count does not
    see that: a parent usually carries no issues at all."""
    calls = stub(monkeypatch, labels=[
        {"id": L1, "name": "Platform"},
        {"id": L2, "name": "backend", "parent": L1},
        {"id": L3, "name": "frontend", "parent": {"id": L1}},
    ])
    with pytest.raises(plane.PlaneError, match="label group with 2 child"):
        with redirect_stdout(io.StringIO()):
            plane.cmd_label_delete(CFG, Args(
                project=None, label="Platform", yes=True, dry_run=False,
            ))
    assert calls.calls == []


def test_a_child_label_is_still_deletable(monkeypatch):
    calls = stub(monkeypatch, labels=[
        {"id": L1, "name": "Platform"},
        {"id": L2, "name": "backend", "parent": L1},
    ])
    run(plane.cmd_label_delete, CFG, Args(
        project=None, label="backend", yes=True, dry_run=False,
    ))
    assert calls.calls[0][:2] == ("DELETE", f"/projects/{OK}/labels/{L2}/")


# --- --json is honoured by the writes too ------------------------------------


def test_json_output_carries_the_new_label_id(monkeypatch):
    stub(monkeypatch, routes={f"/projects/{OK}/labels/": {"id": L3}})
    out = run(plane.cmd_label_create, CFG, Args(
        project=None, name="triage", color="#f80", description=None,
        dry_run=False, json=True,
    ))
    assert json.loads(out)["id"] == L3


def test_json_output_carries_what_the_edit_changed(monkeypatch):
    stub(monkeypatch)
    out = run(plane.cmd_label_edit, CFG, Args(
        project=None, label="bug", name="defect", color=None, description=None,
        dry_run=False, json=True,
    ))
    got = json.loads(out)
    assert (got["id"], got["was"], got["changed"]) == (L1, "bug", {"name": "defect"})


def test_the_blast_radius_is_parseable_when_the_delete_is_refused(monkeypatch):
    """--json exists so a caller can act on the count. Printing it as prose and
    then erroring leaves the one number that matters unparseable."""
    stub(monkeypatch, issues=issues_carrying(L1, L1))
    out = io.StringIO()
    with pytest.raises(plane.PlaneError, match="without --yes"):
        with redirect_stdout(out):
            plane.cmd_label_delete(CFG, Args(
                project=None, label="bug", yes=False, dry_run=False, json=True,
            ))
    got = json.loads(out.getvalue())
    assert (got["on_issues"], got["deleted"]) == (2, False)
    assert "archived" in got["counted_over"]


def test_a_completed_delete_prints_one_json_document(monkeypatch):
    stub(monkeypatch, issues=issues_carrying(L1))
    out = run(plane.cmd_label_delete, CFG, Args(
        project=None, label="bug", yes=True, dry_run=False, json=True,
    ))
    assert json.loads(out)["deleted"] is True


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


def test_a_long_or_multiline_description_does_not_push_the_id_off_the_row(monkeypatch):
    """`_table` sizes a column to its longest value, and `id` is what
    `label-edit` and `label-delete` need. Free text goes last, one line, capped."""
    stub(monkeypatch, labels=[
        {"id": L1, "name": "bug", "color": "#ff0000",
         "description": "why this exists " * 12 + "\nand a second paragraph"},
        {"id": L2, "name": "chore", "color": "#00ff00", "description": ""},
    ])
    out = run(plane.cmd_labels, CFG, Args(project=None))
    lines = out.splitlines()
    assert any("bug" in ln and L1 in ln for ln in lines)
    assert any("chore" in ln and L2 in ln for ln in lines)
    assert max(len(ln) for ln in lines) < 110


def test_json_output_keeps_the_whole_description(monkeypatch):
    long = "why this exists " * 12
    stub(monkeypatch, labels=[
        {"id": L1, "name": "bug", "color": "#ff0000", "description": long},
    ])
    out = run(plane.cmd_labels, CFG, Args(project=None, json=True))
    assert json.loads(out)[0]["description"] == long
