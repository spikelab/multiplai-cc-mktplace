from __future__ import annotations

import json
import stat

import pytest
from pydantic import ValidationError
from viewer_kit.people import (
    PeopleError,
    Person,
    add_person,
    load_people,
    person_for_token,
)

ANA = Person(name="Ana", email="ana@example.com", role="pm", slack_id="U001")
BEN = Person(name="Ben", email="ben@example.com", role="engineer")


def test_missing_file_lists_nobody(tmp_path):
    assert load_people(tmp_path / "people.json") == {}


def test_add_person_saves_a_private_file_and_returns_the_token(tmp_path):
    path = tmp_path / "people.json"
    token = add_person(path, ANA)
    assert len(token) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_people(path) == {token: ANA}
    assert not list(tmp_path.glob(".people.json.*")), "temp file left behind"


def test_a_listed_token_names_its_person(tmp_path):
    path = tmp_path / "people.json"
    ana, ben = add_person(path, ANA), add_person(path, BEN)
    people = load_people(path)
    assert person_for_token(people, ana) == ANA
    assert person_for_token(people, ben) == BEN


def test_an_unlisted_token_names_nobody(tmp_path):
    path = tmp_path / "people.json"
    token = add_person(path, ANA)
    people = load_people(path)
    for given in ("", "nope", token[:-1], token + "x"):
        assert person_for_token(people, given) is None


def test_inviting_again_revokes_the_earlier_link(tmp_path):
    path = tmp_path / "people.json"
    old = add_person(path, ANA)
    new = add_person(path, ANA.model_copy(update={"email": "ana@example.org"}))
    people = load_people(path)
    assert person_for_token(people, old) is None
    assert person_for_token(people, new).email == "ana@example.org"


def test_an_invalid_entry_is_refused_without_naming_its_token(tmp_path):
    path = tmp_path / "people.json"
    secret = "s3cr3t-token-value"
    path.write_text(json.dumps({secret: {"name": "Ana", "role": "boss"}}))
    with pytest.raises(PeopleError) as exc:
        load_people(path)
    assert "entry 1" in str(exc.value)
    assert secret not in str(exc.value)


def test_a_damaged_file_is_refused(tmp_path):
    path = tmp_path / "people.json"
    path.write_text("[1, 2]")
    with pytest.raises(PeopleError):
        load_people(path)
    path.write_text("{not json")
    with pytest.raises(PeopleError):
        load_people(path)


def test_role_is_pm_or_engineer_and_slack_is_optional():
    assert BEN.slack_id is None
    with pytest.raises(ValidationError):
        Person(name="C", email="c@example.com", role="admin")
    with pytest.raises(ValidationError):
        Person(name="C", email="c@example.com", role="pm", token="x")
