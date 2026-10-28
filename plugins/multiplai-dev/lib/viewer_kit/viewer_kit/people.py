"""Who is asking: `people.json` maps each person's token to who they are.

A viewer shared with several people gives each one a personal link carrying
their own token. The file is `{token: {"name", "email", "role", "slack_id"}}`,
written atomically with mode 0600: the tokens are the only thing that grants
access, so the file is as secret as review-viewer's `server.token`.

Opt-in. A viewer that never loads a `people.json` keeps its one token per
server start (`request_checks.check_token`). One that does checks each request
with `request_checks.check_person`, and puts the `Person` it returns on the
inbox rows that request writes.

No message from here names a token, and no token is logged: errors name the
entry's position or person instead.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .mailbox import atomic_write

PEOPLE_FILE = "people.json"


class PeopleError(ValueError):
    pass


class Person(BaseModel):
    """One person who may use the page; also the `author` of the inbox rows
    their requests write."""

    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    role: Literal["pm", "engineer"]
    slack_id: str | None = None


def load_people(path: str | Path) -> dict[str, Person]:
    """{token: Person} from *path*; {} when the file does not exist."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PeopleError(f"{path} is not valid JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise PeopleError(f"{path} must hold a JSON object")
    people: dict[str, Person] = {}
    for n, (token, entry) in enumerate(data.items(), start=1):
        if not isinstance(token, str) or not token:
            raise PeopleError(f"{path}: entry {n} has an empty token")
        try:
            people[token] = Person.model_validate(entry)
        except ValidationError as exc:
            # The token is the entry's key; pydantic's message never names it
            # because each entry is validated on its own.
            raise PeopleError(f"{path}: entry {n} is invalid: {exc}") from None
    return people


def save_people(path: str | Path, people: Mapping[str, Person]) -> None:
    data = {token: p.model_dump(mode="json") for token, p in people.items()}
    atomic_write(Path(path), json.dumps(data, indent=2, sort_keys=True) + "\n")


def add_person(path: str | Path, person: Person) -> str:
    """Give *person* a new token and save it; returns the token.

    An entry with the same name is replaced, so inviting someone again
    revokes their earlier link."""
    people = {
        token: p for token, p in load_people(path).items() if p.name != person.name
    }
    token = secrets.token_urlsafe(32)
    people[token] = person
    save_people(path, people)
    return token


def person_for_token(people: Mapping[str, Person], given: str) -> Person | None:
    """The person whose token is *given*, or None. Compares against every
    listed token with `hmac.compare_digest` and does not stop at a match."""
    found = None
    given_bytes = given.encode("utf-8")
    for token, person in people.items():
        if hmac.compare_digest(given_bytes, token.encode("utf-8")):
            found = person
    return found
