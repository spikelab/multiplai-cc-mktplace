from __future__ import annotations

import io
import json

import pytest
from viewer_kit.people import Person
from viewer_kit.request_checks import (
    TOKEN_HEADER,
    Reject,
    check_origin,
    check_person,
    check_token,
    public_origin,
    read_json_body,
)

ANA = Person(name="Ana", email="ana@example.com", role="pm")
PEOPLE = {"tok-ana": ANA}
PUBLIC = "https://demo.ngrok.app"


def _reject(fn, *args) -> Reject:
    with pytest.raises(Reject) as exc:
        fn(*args)
    return exc.value


# --- origin --------------------------------------------------------------------


def test_no_origin_header_passes():
    check_origin({"Host": "127.0.0.1:8765"})
    check_origin({"Host": "127.0.0.1:8765"}, PUBLIC)


def test_origin_naming_the_host_passes():
    check_origin({"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"})
    check_origin({"Origin": "https://Demo.Ngrok.App", "Host": "demo.ngrok.app"})


def test_the_public_origin_passes_when_host_is_the_local_address():
    headers = {"Origin": "https://demo.ngrok.app", "Host": "127.0.0.1:8765"}
    check_origin(headers, public_origin("https://demo.ngrok.app/"))
    check_origin({**headers, "Origin": "HTTPS://DEMO.NGROK.APP"}, PUBLIC)


def test_another_origin_is_refused():
    for origin in (
        "https://evil.example",
        "http://demo.ngrok.app",  # same host, other scheme
        "https://demo.ngrok.app.evil.example",
        "https://other.ngrok.app",
        "null",
    ):
        headers = {"Origin": origin, "Host": "127.0.0.1:8765"}
        rej = _reject(check_origin, headers, PUBLIC)
        assert (rej.status, rej.error, rej.log_reason) == (
            403,
            "forbidden",
            "foreign origin",
        ), origin


def test_the_public_origin_is_refused_when_none_is_configured():
    headers = {"Origin": PUBLIC, "Host": "127.0.0.1:8765"}
    assert _reject(check_origin, headers).status == 403


def test_public_origin_keeps_only_scheme_and_host():
    assert public_origin("https://Demo.ngrok.app") == PUBLIC
    assert public_origin(" https://demo.ngrok.app/ ") == PUBLIC
    assert public_origin("http://box.local:8000") == "http://box.local:8000"
    for bad in (
        "demo.ngrok.app",
        "ftp://demo.ngrok.app",
        "https://",
        "https://demo.ngrok.app/viewer",
        "https://demo.ngrok.app/?t=1",
        "https://user:pw@demo.ngrok.app",
    ):
        with pytest.raises(ValueError):
            public_origin(bad)


# --- tokens ----------------------------------------------------------------------


def test_one_token_per_start():
    check_token({TOKEN_HEADER: "abc"}, "abc")
    for headers in ({TOKEN_HEADER: "abd"}, {}):
        rej = _reject(check_token, headers, "abc")
        assert (rej.status, rej.error, rej.log_reason) == (
            401,
            "unauthorized",
            "bad token",
        )


def test_a_listed_token_is_accepted_and_names_the_person():
    assert check_person({TOKEN_HEADER: "tok-ana"}, PEOPLE) == ANA
    assert check_person({"X-Viewer-Token": "tok-ana"}, PEOPLE, "X-Viewer-Token") == ANA


def test_an_unlisted_token_is_refused():
    for headers in ({TOKEN_HEADER: "tok-bob"}, {TOKEN_HEADER: ""}, {}):
        rej = _reject(check_person, headers, PEOPLE)
        assert (rej.status, rej.log_reason) == (401, "bad token")
    assert _reject(check_person, {TOKEN_HEADER: "tok-ana"}, {}).status == 401


# --- body ------------------------------------------------------------------------


def _body(data: bytes, ctype="application/json", length=None, max_bytes=1024):
    headers = {
        "Content-Type": ctype,
        "Content-Length": str(len(data) if length is None else length),
    }
    return read_json_body(headers, io.BytesIO(data), max_bytes)


def test_body_reads_a_json_object():
    assert _body(json.dumps({"a": 1}).encode()) == {"a": 1}
    assert _body(b"", length=0) == {}
    assert _body(b'{"a": 1}', ctype="application/json; charset=utf-8") == {"a": 1}


@pytest.mark.parametrize(
    ("kw", "status"),
    [
        ({"data": b"{}", "ctype": "text/plain"}, 415),
        ({"data": b"{}", "length": "x"}, 400),
        ({"data": b"{}", "length": -1}, 400),
        ({"data": b"{}", "max_bytes": 1}, 413),
        ({"data": b"{nope"}, 400),
        ({"data": b"\xff\xfe"}, 400),
        ({"data": b"[1]"}, 400),
    ],
)
def test_body_refusals(kw, status):
    assert _reject(lambda: _body(**kw)).status == status
