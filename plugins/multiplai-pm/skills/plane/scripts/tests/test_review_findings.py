"""Review of upstream PR #1 (DolceClaudeMarketplace, this skill's original home) — one test per finding.

Each test asserts the behaviour the plugin *claims*. A failure here is the proof
that the claim does not hold on this commit; it is not a broken test.

Naming: test_f<N>_* maps to finding N in the review.
"""

from __future__ import annotations

import contextlib
import io
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plane  # noqa: E402

OK = "1fa4d2f6-e016-428a-aca7-5ebb1c8bca4f"
BAD = "b996f98b-0bdc-4bea-9ec0-92da5268f054"
ALLOWED = {OK: "Mine"}
CFG = {
    "token": "plane_api_x",
    "base": "https://api.plane.so",
    "workspace": "acme",
    "allowed": ALLOWED,
}


class _Args:
    def __init__(self, **kw):
        self.json = False
        self.__dict__.update(kw)


# --- F1: search must fail closed, not open ----------------------------------


def test_f1_search_withholds_a_hit_whose_project_field_is_absent(monkeypatch, capsys):
    """A hit we cannot attribute to a project must be withheld.

    cmd_search keys on `project_id`/`project`. If Plane renames or omits the
    field, `pid` is empty and the `if pid and ...` guard lets the hit through,
    printing an issue from a project that was explicitly excluded.
    """
    monkeypatch.setattr(
        plane,
        "_request",
        lambda *a, **k: {
            "issues": [
                {
                    "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                    "sequence_id": 9,
                    "name": "SHARED PROJECT SECRET",
                    "project__identifier": "DB",
                }
            ]
        },
    )
    plane.cmd_search(CFG, _Args(query="x"))
    out = capsys.readouterr()
    assert "SHARED PROJECT SECRET" not in out.out, "unattributable hit leaked"
    assert "withheld" in out.err


def test_f1_search_still_withholds_when_the_project_key_is_renamed(monkeypatch, capsys):
    """Under a field rename, search must go quiet — never start printing.

    We deliberately do NOT teach the parser new field names: an unrecognised
    shape is withheld. Losing your own results is a loud, safe failure; printing
    someone else's is a silent, unsafe one.
    """
    monkeypatch.setattr(
        plane,
        "_request",
        lambda *a, **k: {
            "issues": [
                {
                    "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                    "sequence_id": 9,
                    "name": "SHARED PROJECT SECRET",
                    "project_detail": {"id": BAD, "identifier": "DB"},
                }
            ]
        },
    )
    plane.cmd_search(CFG, _Args(query="x"))
    assert "SHARED PROJECT SECRET" not in capsys.readouterr().out


def test_f1_stderr_distinguishes_out_of_scope_from_unattributable(monkeypatch, capsys):
    """"0 results" must not read the same as "the filter stopped working".

    Withholding silently is how a broken read filter looks exactly like an empty
    board, so the reason is asserted, not just the count (WORKFLOW 7bis-R q.5).
    """
    monkeypatch.setattr(
        plane,
        "_request",
        lambda *a, **k: {
            "issues": [
                {"id": "1", "sequence_id": 1, "name": "out of scope",
                 "project_id": BAD},
                {"id": "2", "sequence_id": 2, "name": "unattributable"},
            ]
        },
    )
    plane.cmd_search(CFG, _Args(query="x"))
    err = capsys.readouterr().err
    assert "2 hit(s) outside the allowlist withheld" in err
    assert "1 of them with no project in the response" in err


# --- F2: attribute injection in generated HTML ------------------------------


def test_f2_link_target_cannot_break_out_of_the_href_attribute():
    """_esc escapes & < > but not the quote that delimits href.

    The property is that no event-handler attribute is ever emitted; the raw
    text may still appear as text.
    """
    html = plane.md_to_html('[click](" onmouseover="alert(1))')
    assert 'onmouseover="' not in html, html


def test_f2_esc_escapes_double_quotes():
    assert '"' not in plane._esc('a "b"')


class _Attrs(HTMLParser):
    """Collects the tags and attributes an HTML parser actually sees.

    Asserting on substrings ("onmouseover not in html") only proves the exact
    string we imagined is absent. Parsing proves what a browser would build.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)


HOSTILE = [
    '[click](" onmouseover="alert(1))',
    "[x](javascript:alert(1))",
    '<img src=x onerror="alert(1)">',
    '**bold** with " quote and \' apostrophe',
    "[a](http://ok) and [b](\" onfocus=\"x)",
    '`code with " quote`',
    "# heading with \" quote",
    '- item with " quote',
    '```\n<script>alert(1)</script>\n```',
]

ALLOWED_TAGS = {"p", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "code",
                "strong", "em", "a"}


@pytest.mark.parametrize("payload", HOSTILE)
def test_f2_generated_html_never_carries_an_event_handler(payload):
    p = _Attrs()
    p.feed(plane.md_to_html(payload))
    handlers = [k for k, _ in p.attrs if k.lower().startswith("on")]
    assert handlers == [], f"{payload!r} produced {handlers}"


@pytest.mark.parametrize("payload", HOSTILE)
def test_f2_generated_html_emits_only_expected_tags_and_href(payload):
    p = _Attrs()
    p.feed(plane.md_to_html(payload))
    assert set(p.tags) <= ALLOWED_TAGS, f"{payload!r} produced {set(p.tags)}"
    assert {k for k, _ in p.attrs} <= {"href"}, f"{payload!r} -> {p.attrs}"
    for key, value in p.attrs:
        if key == "href":
            assert plane._SAFE_URL.match((value or "").strip()), value


def test_f2_javascript_scheme_is_not_emitted_as_a_link_target():
    html = plane.md_to_html("[x](javascript:alert(1))")
    assert "<a " not in html, html
    assert 'href="javascript' not in html, html


# --- F3: pagination must not crash on a partial page ------------------------


def test_f3_paginate_stops_gracefully_when_next_cursor_is_missing(monkeypatch):
    """next_page_results true + no next_cursor must not raise KeyError."""
    monkeypatch.setattr(
        plane,
        "_request",
        lambda *a, **k: {"results": [{"id": 1}], "next_page_results": True},
    )
    items = list(plane._paginate(f"/projects/{OK}/issues/", CFG))
    assert items == [{"id": 1}]


def test_f3_paginate_treats_a_bare_list_as_one_full_page(monkeypatch):
    """A list response is a complete page, not an empty one.

    _paginate returned nothing for non-dict payloads, so any endpoint answering
    with a bare list read as "no results" — silently.
    """
    monkeypatch.setattr(plane, "_request", lambda *a, **k: [{"id": 1}, {"id": 2}])
    assert list(plane._paginate("/members/", CFG)) == [{"id": 1}, {"id": 2}]


# --- F4: --limit must bound the number of requests --------------------------


def test_f4_limit_stops_paginating_once_enough_rows_are_collected(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, path, cfg, *, params=None, **_kw):
        calls["n"] += 1
        return {
            "results": [
                {"id": str(i), "sequence_id": i, "name": "x"} for i in range(100)
            ],
            "next_page_results": calls["n"] < 40,
            "next_cursor": f"c{calls['n']}",
        }

    monkeypatch.setattr(plane, "_request", fake_request)
    monkeypatch.setattr(
        plane,
        "list_projects",
        lambda cfg: [
            {"id": OK, "identifier": "SPK", "name": "Mine", "_allowed": True}
        ],
    )
    with contextlib.redirect_stdout(io.StringIO()):
        plane.cmd_issues(CFG, _Args(json=True, project=None, state=None, limit=3,
                                    full=False))
    assert calls["n"] == 1, f"fetched {calls['n']} pages to show 3 rows"


def test_f4_one_get_hits_the_projects_endpoint_once(monkeypatch):
    """`get SPK-12` walked /projects/ twice: once to resolve, once for the label.

    Counted in HTTP requests, not python calls — the cost is network round trips.
    """
    hits = {"projects": 0}

    def fake_request(method, path, cfg, *, params=None, **_kw):
        if path.startswith("/projects/?") or path.rstrip("/") == "/projects":
            hits["projects"] += 1
            return {
                "results": [{"id": OK, "identifier": "SPK", "name": "Mine"}],
                "next_page_results": False,
            }
        return {
            "results": [
                {"id": "x", "sequence_id": 12, "name": "Thing", "project": OK}
            ],
            "next_page_results": False,
        }

    monkeypatch.setattr(plane, "_request", fake_request)
    cfg = dict(CFG)
    with contextlib.redirect_stdout(io.StringIO()):
        plane.cmd_get(cfg, _Args(ref="SPK-12", project=None, json=False))
    assert hits["projects"] == 1, f"/projects/ fetched {hits['projects']} times"


# --- F5: markdown fidelity --------------------------------------------------


def test_f5_code_fence_content_is_preserved_verbatim():
    src = "```\nlabel = “hello”  # x → y\n```"
    html = plane.md_to_html(src)
    assert "“hello”" in html, html


def test_f5_inline_code_is_not_reformatted():
    html = plane.md_to_html("use `arr[0] **not** bold`")
    assert "<strong>" not in html, html


# --- F6: minor ---------------------------------------------------------------


def test_f6_empty_env_var_does_not_shadow_the_env_file(monkeypatch, tmp_path):
    env = tmp_path / "env"
    env.write_text(
        f"PLANE_API_TOKEN=plane_api_real\nPLANE_WORKSPACE=acme\n"
        f"PLANE_ALLOWED_PROJECTS={OK}\n",
        encoding="utf-8",
    )
    for k in ("PLANE_WORKSPACE", "PLANE_ALLOWED_PROJECTS", "PLANE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PLANE_ENV_FILE", str(env))
    monkeypatch.setenv("PLANE_API_TOKEN", "")  # `export PLANE_API_TOKEN=`
    assert plane._cfg()["token"] == "plane_api_real"


def test_f6_html_to_text_does_not_double_decode_entities():
    assert plane.html_to_text("<p>literal &amp;lt;tag&amp;gt;</p>") == "literal &lt;tag&gt;"


def test_f6_members_are_paginated(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, path, cfg, *, params=None, **_kw):
        calls["n"] += 1
        return {
            "results": [
                {"display_name": f"u{i}", "email": f"u{i}@x", "id": str(i)}
                for i in range(100)
            ],
            "next_page_results": calls["n"] < 2,
            "next_cursor": "c1",
        }

    monkeypatch.setattr(plane, "_request", fake_request)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        plane.cmd_members(CFG, _Args(json=True))
    assert buf.getvalue().count('"email"') == 200, "members truncated at one page"


# --- Coverage gap: the guard's wiring, not the guard ------------------------


def test_gap_guard_runs_after_params_are_merged_into_the_path(monkeypatch):
    """The query-param protection lives in _request's ordering, untested today.

    Every existing guard test calls _guard with a pre-assembled path, so moving
    _guard above the params merge would keep all 63 green while silently killing
    the ?project_id= protection. This test pins the ordering. It PASSES on this
    commit — it documents a missing test, not a defect.
    """
    def boom(*a, **k):  # network must never be reached
        raise AssertionError("request left the guard")

    monkeypatch.setattr(plane.urllib.request, "urlopen", boom)
    with pytest.raises(plane.GuardError):
        plane._request("GET", "/issues/", CFG, params={"project_id": BAD})
