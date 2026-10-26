"""Tests for creating workflow states.

A state is a board column. Creating one is additive and changes no issue, so
what is pinned down here is the payload, the refusals that are knowable before
any request (a bad colour, an empty name, a name already taken), and that the
guardrail still judges the project segment of the states path.
"""

from __future__ import annotations

import io
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
