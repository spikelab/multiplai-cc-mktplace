"""Adversarial round 2: the five fixes and the five new capabilities.

Same convention as the baseline file. A passing test documents a claim that
survived the attack; `xfail(strict=True)` documents a defect that is still open
and turns green by itself the day it is closed.

Live evidence was gathered against a Plane Cloud workspace on api.plane.so on
2026-08-02: project DB (`b996f98b-...`) read-only, project DFT
(`6155159a-...`) as the write sandbox.
"""

from __future__ import annotations

import types
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

import plane
from conftest import ALLOWED, BAD, OK, guard


# --- the guardrail after the dot-segment fix ---------------------------------


def test_dot_segments_are_refused_however_deeply_encoded():
    """The fix holds through three rounds of percent-decoding."""
    for spelling in ("..", "%2e%2e", "%252e%252e", "%25252e%25252e", "."):
        with pytest.raises(plane.GuardError, match="dot segments"):
            guard("POST", f"/projects/{OK}/{spelling}/{BAD}/issues/")


def test_decoding_first_made_the_encoded_spellings_stricter_not_looser():
    """`%2f` used to leave path_ids empty, which only the workspace-write rule
    caught — so a GET slipped through as "workspace-scoped". Decoded, it is now
    recognised as the project it is and refused by name, for every method."""
    for method in ("GET", "POST", "PATCH", "DELETE"):
        with pytest.raises(plane.GuardError, match="not in the allowlist"):
            guard(method, f"/projects%2f{BAD}/issues/")


def test_guard_recognises_a_project_uuid_without_hyphens():
    """`/projects/<32 hex chars>/issues/` is the same project, spelled differently.

    The hyphenless spelling is re-hyphenated before the allowlist lookup, so a
    GET to a blocked project is refused by name rather than sliding through as
    "workspace-scoped". Plane's own router 404s this spelling on Cloud, but the
    guard is documented as the layer that does not rely on the router.
    """
    with pytest.raises(plane.GuardError, match="not in the allowlist"):
        guard("GET", f"/projects/{BAD.replace('-', '')}/issues/")


def test_guard_refuses_backslash_traversal():
    """Same reasoning as `/../`, one character different.

    Backslashes are normalised to `/` before the path is split, so
    `/projects/<allowed>\\..\\<blocked>/` is seen as the dot-segment walk it is.
    nginx does not resolve backslashes, but IIS and several WAFs normalise them
    to `/` before routing — the guard must not depend on who resolves the path.
    """
    with pytest.raises(plane.GuardError, match="dot segments"):
        guard("POST", f"/projects/{OK}" + "\\..\\" + f"{BAD}/issues/")


# --- fetch_asset: the one call outside the chokepoint ------------------------


@pytest.mark.parametrize(
    "url",
    [
        # userinfo: the host is what follows the @, and urlparse knows it
        "https://s3.amazonaws.com@evil.com/x",
        "https://api.plane.so@evil.com/x",
        # the suffix has to be a real label boundary
        "https://notamazonaws.com/x",
        "https://amazonaws.com/x",
        "https://x.amazonaws.com.evil.net/x",
        "https://x.amazonaws.com./x",
        # the allowed host must be the host, not decoration
        "https://evil.com/?h=.amazonaws.com",
        "https://evil.com#.amazonaws.com",
        "https://evil.com/a?next=https://api.plane.so",
        "https://api.plane.so\\@evil.com/x",
        # scheme and SSRF targets
        "http://api.plane.so/x",
        "file:///etc/passwd",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://169.254.169.254/latest/meta-data/",
        "https:///x",
        "//api.plane.so/x",
    ],
)
def test_fetch_asset_refuses_every_host_smuggling_attempt(url):
    with pytest.raises(plane.PlaneError, match="not a host"):
        plane.fetch_asset(url, "https://api.plane.so")


def test_fetch_asset_accepts_only_the_base_host_and_aws(monkeypatch):
    reached = []
    monkeypatch.setattr(
        plane.urllib.request, "build_opener", lambda *a: _Opener(reached)
    )
    for url in (
        "https://api.plane.so/x",
        "HTTPS://API.PLANE.SO/x",
        "https://uploads.s3.amazonaws.com/x",
    ):
        with pytest.raises(plane.PlaneError):
            plane.fetch_asset(url, "https://api.plane.so")
    assert len(reached) == 3, "a legitimate asset host was refused"


class _Opener:
    def __init__(self, log, blob=b""):
        self.log, self.blob = log, blob

    def open(self, req, timeout=None):  # noqa: ARG002
        self.log.append(dict(req.header_items()))
        raise urllib.error.URLError("stopped before the socket")


def test_fetch_asset_sends_no_plane_credential(monkeypatch):
    """The whole reason this call is outside `_request`: urllib copies custom
    headers onto a cross-host redirect, so an `X-API-Key` here would end up at
    amazonaws.com."""
    seen = []
    monkeypatch.setattr(plane.urllib.request, "build_opener", lambda *a: _Opener(seen))
    with pytest.raises(plane.PlaneError):
        plane.fetch_asset("https://api.plane.so/x", "https://api.plane.so")
    keys = {k.lower() for k in seen[0]}
    assert keys == {"user-agent"}, keys


def test_fetch_asset_does_not_follow_redirects():
    """Two proofs, because the class alone is not one.

    Structural: `build_opener(_NoRedirect)` replaces the default handler rather
    than adding to it, and `redirect_request` returns None, which makes urllib
    raise instead of chasing the Location.

    Live (2026-08-02): `https://s3.amazonaws.com/` answers 307, and fetch_asset
    surfaces `asset download -> 307`. A followed redirect would have returned a
    body or a different code.
    """
    opener = urllib.request.build_opener(plane._NoRedirect)
    handlers = [type(h).__name__ for h in opener.handlers if "Redirect" in type(h).__name__]
    assert handlers == ["_NoRedirect"]
    assert plane._NoRedirect().redirect_request(None, None, 302, "m", {}, "https://x") is None


def test_fetch_asset_caps_the_download(monkeypatch):
    class Big:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"x" * n

    monkeypatch.setattr(
        plane.urllib.request,
        "build_opener",
        lambda *a: types.SimpleNamespace(open=lambda req, timeout=None: Big()),
    )
    with pytest.raises(plane.PlaneError, match="larger than"):
        plane.fetch_asset("https://api.plane.so/x", "https://api.plane.so")


def test_downloaded_names_cannot_steer_the_write():
    for hostile in ("../../etc/passwd", "..\\..\\win.ini", "/abs/path", "...", ""):
        safe = plane._safe_filename(hostile, "fallback.bin")
        assert "/" not in safe and "\\" not in safe and not safe.startswith(".")


# --- cycles ------------------------------------------------------------------

# Real bounds, read from DB on 2026-08-02. Note the shape: each sprint ends at
# 21:59:00Z and the next starts at 22:00:01Z on the same calendar day, which is
# local midnight in Europe/Rome. Plane produces these itself — posting a cycle
# with `start_date: "2026-09-01"` to DFT came back as 2026-08-31T22:00:01Z.
S16 = {"name": "S16", "start_date": "2026-07-21T10:31:41.143422Z",
       "end_date": "2026-08-03T21:59:00Z"}
S17 = {"name": "S17", "start_date": "2026-08-03T22:00:01Z",
       "end_date": "2026-08-17T21:59:00Z"}


def _freeze(monkeypatch, when: str) -> None:
    """Pin `datetime.now(utc)` without breaking `_parse_ts`, which needs the
    real class off the same module attribute."""
    fixed = plane._parse_ts(when)
    monkeypatch.setattr(
        plane, "datetime",
        types.SimpleNamespace(now=lambda tz=None: fixed, fromisoformat=datetime.fromisoformat),
    )


@pytest.mark.parametrize(
    "when,live",
    [
        ("2026-08-03T12:00:00Z", ["S16"]),          # the changeover day itself
        ("2026-08-03T21:58:59Z", ["S16"]),
        ("2026-08-03T21:59:00Z", ["S16"]),          # inclusive end
        ("2026-08-03T22:00:01Z", ["S17"]),          # inclusive start
        ("2026-08-04T00:00:00Z", ["S17"]),
        ("2026-08-03T21:59:30Z", []),               # the 61-second seam
    ],
)
def test_cycle_bounds_are_compared_as_instants(when, live):
    """Comparing the first ten characters would match both cycles all day on
    2026-08-03. Comparing instants picks exactly one — except in the 61 seconds
    between 21:59:00Z and 22:00:01Z, when Plane's own bounds cover nothing."""
    now = plane._parse_ts(when)
    got = [c["name"] for c in (S16, S17) if plane.cycle_is_live(c, now)]
    assert got == live


def test_the_changeover_seam_refuses_instead_of_guessing(monkeypatch):
    """In the seam `--cycle active` fails rather than picking a neighbour. That
    is the right call; the message is the wrong one, because it tells the user
    to check sprint dates that are in fact contiguous by Plane's own convention.
    """
    monkeypatch.setattr(plane, "list_cycles", lambda cfg, pid: [S16, S17])
    _freeze(monkeypatch, "2026-08-03T21:59:30Z")
    with pytest.raises(plane.PlaneError, match="no cycle is active right now"):
        plane.active_cycle({}, OK)


def test_overlapping_cycles_are_refused_not_ranked(monkeypatch):
    a = {"id": "a" * 36, "name": "A", "start_date": "2026-08-01T00:00:00Z",
         "end_date": "2026-08-10T00:00:00Z"}
    b = {"id": "b" * 36, "name": "B", "start_date": "2026-08-05T00:00:00Z",
         "end_date": "2026-08-15T00:00:00Z"}
    monkeypatch.setattr(plane, "list_cycles", lambda cfg, pid: [a, b])
    _freeze(monkeypatch, "2026-08-07T00:00:00Z")
    with pytest.raises(plane.PlaneError, match="2 cycles are active"):
        plane.active_cycle({}, OK)


def test_already_in_cycle_is_read_from_a_field_the_list_endpoint_really_sends():
    """`--cycle` idempotency depends on `cycle_id` surviving the projection that
    `resolve_issue` uses for `DFT-2`-style refs, not just the detail endpoint.

    Verified live on DFT: the list endpoint sends `cycle_id` populated
    (`232c71b1-...`) for the issue in a cycle and `None` for the one that is not.
    So the claim holds for both ref forms, and this test pins the shape.
    """
    in_cycle = {"id": "i", "cycle_id": "232c71b1-0033-40a4-9e36-9e5d0968b141"}
    out_of_cycle = {"id": "j", "cycle_id": None}
    assert plane.issue_cycle_id(in_cycle) == "232c71b1-0033-40a4-9e36-9e5d0968b141"
    assert plane.issue_cycle_id(out_of_cycle) is None
    # There is no `cycle` key on an issue; reading one would report every issue
    # as cycle-less.
    assert plane.issue_cycle_id({"cycle": "something"}) is None


# --- search ------------------------------------------------------------------


def _search(monkeypatch, hits, **kw):
    seen = {}

    def fake_request(method, path, cfg, *, params=None, **_):  # noqa: ARG001
        seen["params"] = dict(params or {})
        return {"issues": hits}

    monkeypatch.setattr(plane, "_request", fake_request)
    args = types.SimpleNamespace(query="x", json=True, **kw)
    plane.cmd_search({"allowed": dict(ALLOWED)}, args)
    return seen


def _hit(n, pid=OK):
    return {"id": str(n), "name": f"h{n}", "project_id": pid, "sequence_id": n}


def test_truncation_is_announced_even_when_the_allowlist_ate_the_page(
    monkeypatch, capsys
):
    """The case worth checking: the server returns a full page and the filter
    drops most of it. The user must not read the survivors as the whole answer.
    """
    hits = [_hit(n, BAD) for n in range(70)] + [_hit(n) for n in range(70, 100)]
    _search(monkeypatch, hits, limit=100)
    err = capsys.readouterr().err
    assert "70 hit(s) outside the allowlist withheld" in err
    assert "TRUNCATED" in err


def test_no_truncation_notice_when_the_page_is_short(monkeypatch, capsys):
    _search(monkeypatch, [_hit(n) for n in range(99)], limit=100)
    assert "TRUNCATED" not in capsys.readouterr().err


def test_the_requested_limit_is_the_one_that_is_sent(monkeypatch):
    """Measured on api.plane.so: `search=a` returns 10 / 50 / 100 / 200 / 300 as
    asked and 310 for both limit=500 and limit=1000, i.e. the real result set.
    The server imposes no cap below the request, so `len(hits) >= limit` is a
    sound truncation signal rather than a guess."""
    assert _search(monkeypatch, [], limit=250)["params"]["limit"] == 250
    assert _search(monkeypatch, [], limit=None)["params"]["limit"] == 100


# --- markdown ----------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="plane.py:374 - two-space nesting still flattens into the parent list",
)
def test_two_space_nesting_degrades_like_four_space_nesting():
    """README and SKILL both say nested list items degrade to plain paragraphs,
    "hierarchy and all". True at four spaces; at two — which is a nested list in
    CommonMark, the content column of `- a` being 2 — the item is still absorbed
    into the parent <ul> and the depth vanishes with no warning. The documented
    behaviour is the safe one; only one of the two indents delivers it."""
    assert plane.md_to_html("- a\n  - b") == plane.md_to_html("- a\n    - b")


@pytest.mark.xfail(
    strict=True,
    reason="plane.py:421 - inline markup inside alt lands raw in the attribute",
)
def test_image_alt_text_stays_inside_its_attribute():
    """`![**x**](url)` produces `alt="<strong>x</strong>"`.

    The bold and code-span rules run before the image rule, so by the time the
    alt text is interpolated into the attribute it already contains tags this
    module inserted after escaping. The result is malformed HTML in
    description_html — no injection, since a quote cannot get there, but Plane's
    sanitiser is then free to make of it what it likes.
    """
    out = plane.md_to_html("![**x**](https://a)")
    assert "<" not in out.split('alt="', 1)[1].split('"', 1)[0]


def test_images_hold_on_everything_else():
    assert plane.md_to_html("![a](https://x/y.png)") == (
        '<p><img src="https://x/y.png" alt="a"></p>'
    )
    # An unsafe target is text, never a src.
    for bad in ("javascript:alert(1)", "data:text/html,x", "vbscript:x"):
        assert "<img" not in plane.md_to_html(f"![a]({bad})")
    # A quote in either position cannot break out of the attribute.
    out = plane.md_to_html('![a"onerror=1](https://x)')
    assert "&quot;onerror=1" in out and out.count('"') == 4
    # The common linked-image form comes out as valid nesting.
    assert plane.md_to_html("[![a](https://i)](https://l)") == (
        '<p><a href="https://l"><img src="https://i" alt="a"></a></p>'
    )


def test_fences_are_tracked_by_delimiter_and_length():
    """The CommonMark rule the boolean toggle got wrong, and its edges."""
    nested = "````\nouter — a\n```\ninner — b\n```\nouter — c\n````"
    assert plane._sanitize_prose(nested) == nested
    for src in (
        "~~~\na — b\n~~~",
        "```\na — b\n```",
        "```py\na — b\n```",
        "```\na — b",                      # never closed
        "````\na — b\n```\n",              # closed only by a longer fence: isn't
        "~~~\na — b\n```\nstill inside — c\n~~~",  # wrong character does not close
    ):
        assert plane._sanitize_prose(src) == src, src
    # A closing fence may not carry an info string.
    tricky = "```\na — b\n``` trailing\nstill inside — c\n```"
    assert plane._sanitize_prose(tricky) == tricky
    assert plane._sanitize_prose("a — b") == "a - b"


def test_a_nul_in_the_body_no_longer_kills_the_process():
    assert plane.md_to_html("\x000\x00") == "<p>0</p>"
    assert plane.md_to_html("ciao \x000\x00 mondo") == "<p>ciao 0 mondo</p>"
    assert "<code>c</code>" in plane.md_to_html("`c` \x000\x00")


def test_not_found_says_what_it_checked_rather_than_what_exists(monkeypatch):
    """DB-1974 really is absent from the list endpoint — 320 issues, 1973 and
    1975 present — and an independent client agrees. What the scan cannot see is
    everything the API does not list, so the message must not promote "not
    listed" into "does not exist"."""
    monkeypatch.setattr(plane, "_paginate", lambda *a, **k: iter([]))
    monkeypatch.setattr(
        plane, "resolve_project",
        lambda cfg, ref: {"id": OK, "identifier": "DB", "name": "Acme"},
    )
    with pytest.raises(plane.PlaneError) as exc:
        plane.resolve_issue({"allowed": dict(ALLOWED)}, "DB-1974")
    assert "listed by the API" in str(exc.value)
    assert "unlisted issues would not appear" in str(exc.value)
