"""Adversarial baseline: attempts to falsify what the plane skill claims about itself.

Round 1 found five defects; all five are now closed. The tests that proved them
were `xfail(strict=True)` and are kept here, unmarked, as permanent
non-regression tests: each one still describes the defect it was written for, so
a re-introduction fails with the reasoning attached rather than with a bare
assert. The rest document claims that held under attack the first time round.

Round 2 lives in test_adversarial_round2.py.
"""

from __future__ import annotations

import io
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import plane
from conftest import ALLOWED, BAD, OK, guard


# --- D1: dot-segment traversal walks out of an allowlisted project ------------


def test_guard_rejects_dot_segment_traversal():
    """`/projects/<allowed>/../<blocked>/issues/` must not be accepted.

    The path regex anchors on the first `/projects/<uuid>` it finds and never
    looks at what follows, so the allowlisted UUID acts as a prefix that
    launders the rest of the path. Any fronting proxy that resolves dot
    segments before routing (nginx does, by default) turns this into a write on
    the blocked project. api.plane.so happens to 404 it today; the guardrail is
    documented as the thing that does not depend on that.
    """
    with pytest.raises(plane.GuardError):
        guard("POST", f"/projects/{OK}/../{BAD}/issues/")


def test_guard_rejects_encoded_dot_segment_traversal():
    with pytest.raises(plane.GuardError):
        guard("POST", f"/projects/{OK}/%2e%2e/{BAD}/issues/")


def test_guard_holds_against_the_non_traversal_smuggling_attempts():
    """Everything else thrown at the path/query rules is refused. This holds."""
    attacks = [
        ("GET", f"/projects/{BAD}/issues/"),
        ("post", f"/projects/{BAD}/issues/"),
        ("POST", f"/projects/{BAD.upper()}/issues/"),
        ("POST", f"//projects/{BAD}/issues/"),
        ("POST", f"/PROJECTS/{BAD}/issues/"),
        ("POST", f"/projects%2f{BAD}/issues/"),
        ("POST", f"/projects/{BAD}%2fissues/"),
        ("POST", f"/projects/{OK}/issues/?project_id={BAD}"),
        ("GET", f"/projects/{OK}/issues/?a=1&project_id={BAD}"),
        ("POST", f"/projects/{OK}/issues/?target_project={BAD}"),
        ("POST", f"/projects/{OK}/issues/#/projects/{BAD}/issues/"),
        ("POST", f"/projects/{OK}/issues/;/projects/{BAD}/"),
        ("POST", "/issues/search/"),
        ("DELETE", f"/projects/{OK}/"),
    ]
    for method, path in attacks:
        with pytest.raises(plane.GuardError, match="allowlist|refusing"):
            guard(method, path)


# --- D2: `check` self-tests the guard function, not the chokepoint ------------


def _load_mutant(source_transform):
    src = Path(plane.__file__).read_text(encoding="utf-8")
    mutated = source_transform(src)
    assert mutated != src, "mutation did not apply - the source moved"
    mod = types.ModuleType("plane_mutant")
    mod.__file__ = plane.__file__
    exec(compile(mutated, "plane_mutant", "exec"), mod.__dict__)
    return mod


PROJECTS = [
    {"id": OK, "identifier": "SPK", "name": "Mine", "_allowed": True},
    {"id": BAD, "identifier": "DFT", "name": "Shared", "_allowed": False},
]


def _run_check(mod):
    """Run `cmd_check` against a fixed project list, restoring the module after.

    `list_projects` is patched by hand rather than with monkeypatch because the
    mutant module is loaded inside the test, but the pristine module is the
    shared import — leaving a lambda on it poisons every later test.
    """
    cfg = {
        "base": "https://example.invalid",
        "workspace": "ws",
        "token": "t" * 20,
        "allowed": dict(ALLOWED),
        "_projects": PROJECTS,
    }
    original = mod.list_projects
    mod.list_projects = lambda _cfg: PROJECTS
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = mod.cmd_check(cfg, None)
    finally:
        mod.list_projects = original
    return rc, buf.getvalue()


def test_check_notices_when_the_chokepoint_is_disconnected():
    """`check` must fail if `_request` stops consulting the guard.

    The round-1 defect was that it could not: cmd_check exercised `_guard`
    directly and nothing in it went through `_request`, so deleting the one
    line the safety claim rests on (plane.py:219) left every case green. Now
    there is a second block that drives `_request` in dry-run mode
    (plane.py:1043-1058) and the mutation is caught.

    This test checks both directions, because half of it proves nothing on its
    own: an assertion that the mutant fails is worthless if the pristine module
    fails too, and an assertion that the pristine module passes is worthless if
    nothing can make it fail. That is exactly the criticism `check` earned.
    """
    rc, out = _run_check(plane)
    assert rc == 0 and "PASS" in out and "LEAKED" not in out

    mutant = _load_mutant(
        lambda s: s.replace('    _guard(method, path, cfg["allowed"])\n', "", 1)
    )
    rc, out = _run_check(mutant)
    assert rc == 1, "check certified a build whose chokepoint is gone"
    assert "FAIL" in out and "LEAKED" in out
    assert "_request did not consult the guard" in out


def test_check_selftest_covers_the_query_parameter_rule():
    """README sells `?project_id=<blocked>` as a refusal class, so `check` has
    to try it: a self-test narrower than the promise certifies the promise it
    does not test. Round 1 had no case carrying a query string at all."""
    _, out = _run_check(plane)
    assert "?project_id=" in out


# --- D3: search silently truncates to the server default ----------------------


def _capture_search(monkeypatch, hits):
    seen = {}

    def fake_request(method, path, cfg, *, params=None, **kw):  # noqa: ARG001
        seen["path"] = path
        seen["params"] = dict(params or {})
        return {"issues": hits}

    monkeypatch.setattr(plane, "_request", fake_request)
    return seen


def test_search_asks_for_more_than_the_server_default(monkeypatch):
    """Observed against a live Plane Cloud workspace, project DB:

        search=calendario                -> 10 hits
        search=calendario&limit=100      -> 29 hits

    cmd_search sends only `search`, so it shows 10 of 29 and says nothing about
    the other 19. The only count it prints on stderr is the allowlist one,
    which reads as "that was everything, minus what I withheld".
    """
    seen = _capture_search(monkeypatch, [])
    args = types.SimpleNamespace(query="calendario", json=False)
    plane.cmd_search({"allowed": dict(ALLOWED)}, args)
    assert "limit" in seen["params"] or "per_page" in seen["params"]


def test_search_filter_fails_closed_on_unattributable_hits(monkeypatch, capsys):
    """This holds: a hit with no recognisable project is withheld, not shown."""
    hits = [
        {"id": "1", "name": "mine", "project_id": OK, "sequence_id": 1},
        {"id": "2", "name": "theirs", "project_id": BAD, "sequence_id": 2},
        {"id": "3", "name": "nowhere", "sequence_id": 3},
        {"id": "4", "name": "mangled", "project_id": "not-a-uuid", "sequence_id": 4},
    ]
    _capture_search(monkeypatch, hits)
    args = types.SimpleNamespace(query="x", json=True)
    plane.cmd_search({"allowed": dict(ALLOWED)}, args)
    out = capsys.readouterr()
    assert "theirs" not in out.out and "nowhere" not in out.out
    assert "mangled" not in out.out and "mine" in out.out
    assert "3 hit(s) outside the allowlist withheld" in out.err


# --- D4: markdown conversion -------------------------------------------------


def test_md_to_html_does_not_crash_on_the_parking_sentinel():
    """`\\x00<n>\\x00` is the internal sentinel for a parked code span.

    It is never escaped on the way in, so a body carrying a NUL byte lands in
    the un-parking substitution and indexes a list that is too short:
    IndexError, uncaught by main() (plane.py:1014-1025), so the CLI dies with a
    traceback instead of `error: ...`.
    """
    assert plane.md_to_html("\x000\x00")


def test_nested_lists_degrade_to_paragraphs_as_documented():
    """Documented: "nested lists ... degrade to plain paragraphs".

    Actual: `^\\s*[-*+]\\s+` matches the indented items too, so three levels
    collapse into one flat <ul> and the hierarchy is lost with no warning.
    Blockquotes and tables really do degrade to paragraphs; this one does not.
    """
    out = plane.md_to_html("- a\n    - b\n        - c")
    assert out.count("<li>") <= 1, out


def test_image_syntax_is_not_mangled_into_a_link():
    """`![alt](url)` is neither supported nor listed as unsupported; it becomes
    `!<a href="url">alt</a>` — a literal bang glued to a link in the ticket."""
    out = plane.md_to_html("![alt](https://x/y.png)")
    assert not out.startswith("<p>!<a"), out


def test_md_to_html_holds_on_escaping_and_link_targets():
    """This holds: no path found from markdown to executable or broken HTML."""
    assert plane.md_to_html("<script>alert(1)</script>") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    )
    for target in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x"):
        out = plane.md_to_html(f"[x]({target})")
        assert "<a" not in out and "href" not in out
    # A quote in the target cannot close the attribute and open new ones.
    out = plane.md_to_html('[x](https://a/"onmouseover=1)')
    assert out == '<p><a href="https://a/&quot;onmouseover=1">x</a></p>'
    # An unterminated fence still closes its own tags.
    assert plane.md_to_html("t\n```\ncode") == "<p>t</p><pre><code>code</code></pre>"
    # Tables and blockquotes really do flatten to paragraphs.
    assert plane.md_to_html("> q") == "<p>&gt; q</p>"
    assert plane.md_to_html("| a | b |") == "<p>| a | b |</p>"


# --- D5: sanitize and fenced code --------------------------------------------


NESTED_FENCE = "````\nouter - a — b\n```\ninner — c\n```\nouter — d\n````"


def test_sanitize_leaves_a_nested_fence_byte_for_byte_intact():
    """Documented: fenced code blocks survive byte-for-byte.

    A four-backtick fence wrapping a three-backtick one — the standard way to
    quote markdown that itself contains code — is read as four separate
    fences, so the inner block falls "outside" and gets its punctuation
    rewritten. The sample the fence exists to preserve is the part that is
    corrupted.
    """
    assert plane._sanitize_prose(NESTED_FENCE) == NESTED_FENCE


def test_sanitize_leaves_a_tilde_fence_intact():
    src = "~~~\na — b\n~~~"
    assert plane._sanitize_prose(src) == src


def test_sanitize_holds_on_plain_indented_and_unclosed_fences():
    """This holds."""
    for src in (
        "```\nx -> y — z\n```",
        "    ```\n    a — b\n    ```",
        "```\na — b",
        "```py\na — b\n```",
    ):
        assert plane._sanitize_prose(src) == src
    assert plane._sanitize_prose("a — b") == "a - b"


# --- D6: resolving a reference costs a full scan of the project ---------------


def test_resolve_issue_scans_every_page_before_saying_not_found(monkeypatch):
    """A miss costs one request per page of the whole project, every time.

    Measured against a live Plane Cloud workspace: project DB lists 320 issues, so
    `get DB-1974` (a miss) burns 4 sequential GETs at per_page=100. There is no
    cache and no early stop, and the misses are exactly the calls an agent
    retries. The message it ends on — "no issue with sequence N in DB" — is
    stated as fact about the project when what was searched is the list
    endpoint; two independent clients agree on 1974, but the sentence claims
    more than the scan can prove.
    """
    pages = [
        {
            "results": [{"id": str(n), "sequence_id": n} for n in range(p, p + 100)],
            "next_cursor": f"c{p}",
            "next_page_results": p < 300,
        }
        for p in (0, 100, 200, 300)
    ]
    calls = []

    def fake_request(method, path, cfg, *, params=None, **kw):  # noqa: ARG001
        calls.append(params.get("cursor"))
        return pages[len(calls) - 1]

    monkeypatch.setattr(plane, "_request", fake_request)
    monkeypatch.setattr(
        plane,
        "resolve_project",
        lambda cfg, ref: {"id": OK, "identifier": "SPK", "name": "Mine"},
    )
    with pytest.raises(plane.PlaneError, match="no issue with sequence 999999"):
        plane.resolve_issue({"allowed": dict(ALLOWED)}, "SPK-999999")
    assert len(calls) == 4
