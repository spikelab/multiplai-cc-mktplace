"""Tests for creating workflow states.

A state is a board column. Creating one is additive and changes no issue, so
what is pinned down here is the payload, the refusals that are knowable before
any request (a bad colour, an empty name, a name already taken), and that the
guardrail still judges the project segment of the states path.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import plane
import pytest
from conftest import BAD, CFG, OK, Args, guard

S1 = "cccc1111-2222-3333-4444-555566667777"
S2 = "cccc1111-9999-8888-7777-666655554444"

STATES = [
    {"id": S1, "name": "Backlog", "group": "backlog", "default": True},
    {"id": S2, "name": "In Progress", "group": "started", "default": False},
]

PROJECT = {"id": OK, "name": "Mine", "identifier": "SPK"}


def stub(monkeypatch, *, states=STATES, made=None):
    calls: list[tuple] = []

    def request(method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        calls.append((method, path, body, dry_run))
        return made

    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(
        plane, "_paginate", lambda path, cfg, **kw: iter([dict(s) for s in states])
    )
    monkeypatch.setattr(plane, "_request", request)
    return calls


def state_args(**kw):
    base = {
        "project": None,
        "name": "Shaping",
        "color": "#F80",
        "group": "started",
        "description": None,
        "sequence": None,
        "dry_run": False,
    }
    base.update(kw)
    return Args(**base)


def run(args):
    out = io.StringIO()
    with redirect_stdout(out):
        plane.cmd_state_create(CFG, args)
    return out.getvalue()


def test_creating_a_state_posts_name_colour_and_group(monkeypatch):
    calls = stub(monkeypatch, made={"id": "new-state"})
    out = run(state_args())
    method, path, body, dry = calls[0]
    assert (method, path, dry) == ("POST", f"/projects/{OK}/states/", False)
    assert body == {"name": "Shaping", "color": "#ff8800", "group": "started"}
    assert "created state 'Shaping'" in out


def test_description_and_sequence_are_sent_only_when_given(monkeypatch):
    calls = stub(monkeypatch, made={"id": "new-state"})
    run(state_args(description="specs being written", sequence=25000.0))
    assert calls[0][2] == {
        "name": "Shaping",
        "color": "#ff8800",
        "group": "started",
        "description": "specs being written",
        "sequence": 25000.0,
    }


def test_a_state_name_already_taken_is_refused_before_any_write(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="already exists"):
        plane.cmd_state_create(CFG, state_args(name="in progress"))
    assert calls == []


def test_a_bad_colour_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="hex colour"):
        plane.cmd_state_create(CFG, state_args(color="orange"))
    assert calls == []


def test_an_empty_name_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="empty state name"):
        plane.cmd_state_create(CFG, state_args(name="   "))
    assert calls == []


def test_a_response_without_an_id_is_an_error(monkeypatch):
    stub(monkeypatch, made={})
    with pytest.raises(plane.PlaneError, match="returned no id"):
        plane.cmd_state_create(CFG, state_args())


def test_dry_run_passes_the_flag_and_prints_no_success_line(monkeypatch):
    calls = stub(monkeypatch)
    out = run(state_args(dry_run=True))
    assert calls[0][3] is True
    assert "created state" not in out


def test_the_parser_requires_colour_and_group_and_rejects_triage():
    p = plane.build_parser()
    ok = p.parse_args(
        ["state-create", "--name", "Shaping", "--color", "#f80", "--group", "started"]
    )
    assert ok.func is plane.cmd_state_create
    with pytest.raises(SystemExit):
        p.parse_args(["state-create", "--name", "Shaping", "--group", "started"])
    with pytest.raises(SystemExit):
        p.parse_args(["state-create", "--name", "Shaping", "--color", "#f80"])
    with pytest.raises(SystemExit):
        p.parse_args(
            ["state-create", "--name", "X", "--color", "#f80", "--group", "triage"]
        )


def test_the_guard_allows_creating_a_state_in_an_allowed_project():
    guard("POST", f"/projects/{OK}/states/")


def test_the_guard_refuses_creating_a_state_in_a_blocked_project():
    with pytest.raises(plane.GuardError):
        guard("POST", f"/projects/{BAD}/states/")


# --- editing and deleting states ---------------------------------------------
#
# Both commands are gated on --yes. What is pinned down: the payload carries
# only the fields that change, nothing is written without --yes, the preview
# names every change and the issue count with its scope, and each run
# recomputes from the state as Plane has it now (stop rows S1-S6 of the plan).

S3 = "dddd1111-2222-3333-4444-555566667777"

BOARD = [
    {
        "id": S1,
        "name": "Backlog",
        "group": "backlog",
        "default": True,
        "color": "#aaaaaa",
        "sequence": 15000.0,
        "description": "",
    },
    {
        "id": S2,
        "name": "Todo",
        "group": "unstarted",
        "default": False,
        "color": "#bbbbbb",
        "sequence": 30000.0,
        "description": "",
    },
    {
        "id": S3,
        "name": "In Progress",
        "group": "started",
        "default": False,
        "color": "#cccccc",
        "sequence": 45000.0,
        "description": "",
    },
]


def board_stub(monkeypatch, *, states=BOARD, issues=(), fail_delete=None):
    """Stub the state list, the issue scan and every write.

    `_paginate` answers the states path with `states` and the issues path with
    `issues`; `scans` records the params of every issue scan.
    """
    calls: list[tuple] = []
    scans: list[dict] = []

    def paginate(path, cfg, **kw):
        if path.endswith("/states/"):
            return iter([dict(s) for s in states])
        scans.append(kw.get("params"))
        return iter([dict(i) for i in issues])

    def request(method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        calls.append((method, path, body, dry_run))
        if method == "DELETE" and fail_delete and not dry_run:
            raise plane.PlaneError(fail_delete)
        return {"id": "x"}

    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(plane, "_paginate", paginate)
    monkeypatch.setattr(plane, "_request", request)
    return calls, scans


def edit_args(state="Todo", **kw):
    base = {
        "project": None,
        "state": state,
        "name": None,
        "color": None,
        "group": None,
        "description": None,
        "sequence": None,
        "default": False,
        "yes": True,
        "dry_run": False,
    }
    base.update(kw)
    return Args(**base)


def delete_args(state="Todo", **kw):
    base = {"project": None, "state": state, "yes": True, "dry_run": False}
    base.update(kw)
    return Args(**base)


def in_state(state_id, n):
    return [{"id": f"i{k}", "state": state_id} for k in range(n)]


def run_cmd(fn, args):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(CFG, args)
    return out.getvalue()


def test_state_edit_patches_only_the_fields_given(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    out = run_cmd(plane.cmd_state_edit, edit_args(color="#3366FF"))
    assert calls == [
        ("PATCH", f"/projects/{OK}/states/{S2}/", {"color": "#3366ff"}, False)
    ]
    assert "edited state 'Todo'" in out


def test_state_edit_renames(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    run_cmd(plane.cmd_state_edit, edit_args(name="  Accepted  "))
    assert calls[0][2] == {"name": "Accepted"}


def test_state_edit_refuses_a_rename_onto_another_states_name(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="already named"):
        plane.cmd_state_edit(CFG, edit_args(name="in progress"))
    assert calls == []


def test_state_edit_allows_a_rename_to_its_own_name_in_other_case(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    run_cmd(plane.cmd_state_edit, edit_args(name="TODO"))
    assert calls[0][2] == {"name": "TODO"}


def test_state_edit_refuses_an_empty_name(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="empty state name"):
        plane.cmd_state_edit(CFG, edit_args(name="  "))
    assert calls == []


def test_state_edit_with_no_fields_is_refused(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="nothing to change"):
        plane.cmd_state_edit(CFG, edit_args())
    assert calls == []


def test_state_edit_default_on_the_current_default_is_no_change(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="nothing to change"):
        plane.cmd_state_edit(CFG, edit_args(state="Backlog", default=True))
    assert calls == []


def test_state_edit_bad_colour_sends_nothing(monkeypatch):
    calls, scans = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="hex colour"):
        plane.cmd_state_edit(CFG, edit_args(color="blue"))
    assert calls == [] and scans == []


def test_state_edit_parser_rejects_a_group_outside_the_list():
    p = plane.build_parser()
    ok = p.parse_args(["state-edit", "Todo", "--group", "started", "--yes"])
    assert ok.func is plane.cmd_state_edit and ok.yes is True
    with pytest.raises(SystemExit):
        p.parse_args(["state-edit", "Todo", "--group", "triage"])


def test_s1_state_edit_without_yes_sends_no_patch_and_refuses(monkeypatch):
    """S1: refused without --yes, nothing is written."""
    calls, _ = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="without --yes"):
        run_cmd(plane.cmd_state_edit, edit_args(name="Accepted", yes=False))
    assert calls == []


def test_state_edit_preview_lists_each_change_and_the_count_with_scope(monkeypatch):
    board_stub(monkeypatch, issues=in_state(S2, 3) + in_state(S3, 1))
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(plane.PlaneError):
        plane.cmd_state_edit(
            CFG, edit_args(name="Accepted", color="#3366ff", yes=False)
        )
    text = out.getvalue()
    assert "name: 'Todo' -> 'Accepted'" in text
    assert "color: '#bbbbbb' -> '#3366ff'" in text
    assert f"3 issue(s) in this state (counted over {plane._STATE_USAGE_SCOPE})" in text


def test_state_edit_preview_warns_about_a_group_change(monkeypatch):
    board_stub(monkeypatch, issues=in_state(S2, 2))
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(plane.PlaneError):
        plane.cmd_state_edit(CFG, edit_args(group="completed", yes=False))
    text = out.getvalue()
    assert "group: 'unstarted' -> 'completed'" in text
    assert "move from group 'unstarted' to 'completed'" in text
    assert "as closed" in text


def test_state_edit_preview_names_the_state_that_stops_being_default(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    out = run_cmd(plane.cmd_state_edit, edit_args(default=True))
    assert "'Backlog' stops being the project's default state" in out
    assert calls[0][2] == {"default": True}


def test_state_edit_dry_run_prints_the_patch_without_yes(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    out = run_cmd(
        plane.cmd_state_edit, edit_args(name="Accepted", yes=False, dry_run=True)
    )
    assert calls == [
        ("PATCH", f"/projects/{OK}/states/{S2}/", {"name": "Accepted"}, True)
    ]
    assert "edited state" not in out


def test_state_edit_json_refused_run_emits_the_changes(monkeypatch):
    board_stub(monkeypatch, issues=in_state(S2, 2))
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(plane.PlaneError):
        plane.cmd_state_edit(CFG, edit_args(name="Accepted", yes=False, json=True))
    got = json.loads(out.getvalue())
    assert got["applied"] is False
    assert got["changes"] == {"name": {"from": "Todo", "to": "Accepted"}}
    assert got["on_issues"] == 2
    assert got["counted_over"] == plane._STATE_USAGE_SCOPE


def test_state_edit_json_applied_run_emits_one_document(monkeypatch):
    board_stub(monkeypatch)
    out = run_cmd(plane.cmd_state_edit, edit_args(name="Accepted", json=True))
    got = json.loads(out)
    assert got["applied"] is True
    assert got["state"] == {"id": S2, "name": "Todo"}


def test_s2_rerun_after_an_applied_edit_previews_no_differences(monkeypatch):
    """S2: Plane applied the PATCH but the output was lost; re-running the
    same command finds nothing left to change and writes nothing."""
    applied = [dict(s) for s in BOARD]
    applied[1]["name"] = "Accepted"
    calls, _ = board_stub(monkeypatch, states=applied)
    with pytest.raises(plane.PlaneError, match="nothing to change"):
        plane.cmd_state_edit(CFG, edit_args(state="Accepted", name="Accepted"))
    assert calls == []


def test_s3_yes_run_recomputes_the_preview_from_the_current_state(monkeypatch):
    """S3: the state was recoloured in Plane between preview and --yes; the
    --yes run prints the change against the current value."""
    moved = [dict(s) for s in BOARD]
    moved[1]["color"] = "#00ff00"
    calls, _ = board_stub(monkeypatch, states=moved)
    out = run_cmd(plane.cmd_state_edit, edit_args(color="#3366ff"))
    assert "color: '#00ff00' -> '#3366ff'" in out
    assert calls[0][2] == {"color": "#3366ff"}


def test_state_delete_refuses_the_default_state_before_counting(monkeypatch):
    calls, scans = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="state-edit <other> --default"):
        plane.cmd_state_delete(CFG, delete_args(state="Backlog"))
    assert calls == [] and scans == []


def test_state_delete_refuses_a_state_that_holds_issues(monkeypatch):
    calls, _ = board_stub(monkeypatch, issues=in_state(S2, 2))
    with pytest.raises(plane.PlaneError) as exc:
        run_cmd(plane.cmd_state_delete, delete_args())
    assert "--state <other>" in str(exc.value)
    assert "state-edit" in str(exc.value)
    assert calls == []


def test_s4_state_delete_without_yes_sends_nothing(monkeypatch):
    """S4: refused without --yes, nothing is written."""
    calls, _ = board_stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="without --yes"):
        run_cmd(plane.cmd_state_delete, delete_args(yes=False))
    assert calls == []


def test_state_delete_with_yes_sends_one_delete_to_the_state(monkeypatch):
    calls, _ = board_stub(monkeypatch, issues=in_state(S3, 4))
    out = run_cmd(plane.cmd_state_delete, delete_args())
    assert calls == [("DELETE", f"/projects/{OK}/states/{S2}/", None, False)]
    assert "holds 0 issue(s)" in out
    assert "deleted state 'Todo'" in out


def test_state_delete_dry_run_previews_without_yes(monkeypatch):
    calls, _ = board_stub(monkeypatch)
    out = run_cmd(plane.cmd_state_delete, delete_args(yes=False, dry_run=True))
    assert calls == [("DELETE", f"/projects/{OK}/states/{S2}/", None, True)]
    assert "deleted state" not in out


def test_state_delete_json_refused_run_carries_the_count(monkeypatch):
    board_stub(monkeypatch)
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(plane.PlaneError):
        plane.cmd_state_delete(CFG, delete_args(yes=False, json=True))
    got = json.loads(out.getvalue())
    assert (got["on_issues"], got["deleted"]) == (0, False)


def test_state_delete_passes_a_server_refusal_through_with_the_scope(monkeypatch):
    board_stub(
        monkeypatch,
        fail_delete="DELETE x -> 400 "
        '{"error": "The state is not empty, only empty states can be deleted"}',
    )
    with pytest.raises(plane.PlaneError) as exc:
        run_cmd(plane.cmd_state_delete, delete_args())
    text = str(exc.value)
    assert "only empty states can be deleted" in text
    assert plane._STATE_USAGE_SCOPE in text
    assert "archived" in text


def test_s5_rerun_after_a_delete_fails_with_no_state_matching(monkeypatch):
    """S5: the DELETE landed but the output was lost; the re-run cannot find
    the state and lists what exists."""
    gone = [s for s in BOARD if s["id"] != S2]
    calls, _ = board_stub(monkeypatch, states=gone)
    with pytest.raises(plane.PlaneError, match="no state matching 'Todo'.*Backlog"):
        plane.cmd_state_delete(CFG, delete_args())
    assert calls == []


def test_s6_yes_run_recounts_and_refuses_a_state_that_filled_up(monkeypatch):
    """S6: an issue moved into the state between preview and --yes; the --yes
    run counts again and refuses."""
    calls, _ = board_stub(monkeypatch, issues=in_state(S2, 1))
    with pytest.raises(plane.PlaneError, match="still holds 1 issue"):
        run_cmd(plane.cmd_state_delete, delete_args())
    assert calls == []


def test_state_delete_parser_takes_yes():
    ok = plane.build_parser().parse_args(["state-delete", "Todo", "--yes"])
    assert ok.func is plane.cmd_state_delete and ok.yes is True


# --- resolving a state and counting its issues --------------------------------


def test_state_resolution_by_name_uuid_and_prefix():
    assert plane.resolve_state_object(CFG, OK, "todo", states=BOARD)["id"] == S2
    assert plane.resolve_state_object(CFG, OK, S3.upper(), states=BOARD)["id"] == S3
    assert plane.resolve_state_object(CFG, OK, "dddd1111", states=BOARD)["id"] == S3


def test_state_resolution_refuses_an_ambiguous_prefix():
    with pytest.raises(plane.PlaneError, match="2 states match"):
        plane.resolve_state_object(CFG, OK, "cccc1111", states=BOARD)


def test_state_usage_asks_for_id_and_state_only_and_counts_expanded_dicts(
    monkeypatch,
):
    _, scans = board_stub(
        monkeypatch,
        issues=[
            {"id": "a", "state": {"id": S2.upper(), "name": "Todo"}},
            {"id": "b", "state": S2},
            {"id": "c", "state": S3},
        ],
    )
    assert plane.state_usage(CFG, OK, S2) == 2
    assert scans == [{"fields": "id,state"}]


# --- guardrail rule 5 now reaches states --------------------------------------


def test_the_guard_allows_deleting_a_state_in_an_allowed_project():
    guard("DELETE", f"/projects/{OK}/states/{S2}/")


def test_the_guard_refuses_deleting_a_state_in_a_blocked_project():
    with pytest.raises(plane.GuardError):
        guard("DELETE", f"/projects/{BAD}/states/{S2}/")


def test_the_guard_refuses_deleting_the_whole_state_set():
    with pytest.raises(plane.GuardError):
        guard("DELETE", f"/projects/{OK}/states/")


def test_check_passes_and_prints_the_new_guard_cases(monkeypatch):
    monkeypatch.setattr(
        plane, "list_projects", lambda cfg: [{**PROJECT, "_allowed": True}]
    )
    out = io.StringIO()
    with redirect_stdout(out):
        assert plane.cmd_check(dict(CFG), Args()) == 0
    text = out.getvalue()
    for line in (
        "blocked  DELETE delete a state in a project not in the allowlist",
        "blocked  DELETE delete a project's whole state set",
        "blocked  PATCH  write via the workspace-scoped ref lookup",
        "allowed  DELETE delete a state in an allowed project",
    ):
        assert line in text
    assert "PASS" in text
