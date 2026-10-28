"""The checks every request to a viewer passes, as functions of its headers.

In the order a server runs them:

1. `check_origin`: a request with an `Origin` header must come from the page
   itself: its origin names the request's `Host`, or the configured public
   origin (`public_origin`) when the page is reached through a tunnel such as
   ngrok. Anything else is 403.
2. `check_token` (one token per server start) or `check_person` (any token in
   `people.json`, which also says who is asking): `/api/*` without a matching
   token header is 401. Tokens are compared with `hmac.compare_digest`.
3. `read_json_body`: a `POST` whose `Content-Type` is not `application/json`
   is 415, which forces a CORS preflight the server never answers with allow
   headers; then the size and JSON checks.

Each failure raises `Reject`; the server answers `{"error": rej.error}` with
`rej.status`, and logs `rej.log_reason` when it is set.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import BinaryIO, Protocol
from urllib.parse import urlparse

from .people import Person, person_for_token

TOKEN_HEADER = "X-Review-Token"


class Headers(Protocol):
    def get(self, name: str, default=None): ...


class Reject(Exception):
    def __init__(self, status: int, error: str, log_reason: str | None = None):
        super().__init__(error)
        self.status, self.error, self.log_reason = status, error, log_reason


def public_origin(url: str) -> str:
    """The origin (`scheme://host[:port]`, lower case) of a public URL such as
    `https://x.ngrok.app`. Raises ValueError for anything that is not an
    http(s) URL with a host and no path, query or fragment."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not an http(s) URL with a host: {url!r}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"a public URL is an origin only, without a path: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"a public URL carries no user or password: {url!r}")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def check_origin(headers: Headers, public: str | None = None) -> None:
    """403 unless the request has no `Origin`, its origin's host is `Host`, or
    its origin is *public* (from `public_origin`)."""
    origin = headers.get("Origin")
    if origin is None:
        return
    parsed = urlparse(origin)
    host = headers.get("Host", "")
    if parsed.netloc.lower() == host.lower():
        return
    if public is not None:
        given = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if given == public:
            return
    raise Reject(403, "forbidden", "foreign origin")


def check_token(headers: Headers, token: str, header: str = TOKEN_HEADER) -> None:
    """401 unless *header* holds *token*."""
    given = headers.get(header, "")
    if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
        raise Reject(401, "unauthorized", "bad token")


def check_person(
    headers: Headers, people: Mapping[str, Person], header: str = TOKEN_HEADER
) -> Person:
    """The person whose token *header* holds; 401 when it is none of them."""
    person = person_for_token(people, headers.get(header, ""))
    if person is None:
        raise Reject(401, "unauthorized", "bad token")
    return person


def read_json_body(headers: Headers, rfile: BinaryIO, max_bytes: int) -> dict:
    """The request's JSON object body, read from *rfile*."""
    ctype = headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise Reject(415, "content type must be application/json", "wrong content type")
    try:
        length = int(headers.get("Content-Length") or 0)
    except ValueError:
        raise Reject(400, "bad content length")
    if length < 0:
        raise Reject(400, "bad content length")
    if length > max_bytes:
        raise Reject(413, "body too large")
    try:
        data = json.loads(rfile.read(length) or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Reject(400, "invalid json")
    if not isinstance(data, dict):
        raise Reject(400, "expected a json object")
    return data
