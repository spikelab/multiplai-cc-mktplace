"""Tests for the plane CLI.

The guardrail carries the plugin's safety claim ("it cannot touch projects
outside the allowlist"), so it gets adversarial coverage: every test named
test_blocks_* is an attempt to reach a non-allowlisted project.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plane  # noqa: E402

OK = "1fa4d2f6-e016-428a-aca7-5ebb1c8bca4f"
OK2 = "6155159a-ff3b-447a-83db-6104be74ffb4"
BAD = "b996f98b-0bdc-4bea-9ec0-92da5268f054"

ALLOWED = {OK: "Mine", OK2: "Also mine"}

ISSUE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def guard(method, path, allowed=None):
    plane._guard(method, path, allowed if allowed is not None else ALLOWED)


# --- the allowlist is enforced ----------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_blocks_every_method_on_excluded_project(method):
    with pytest.raises(plane.GuardError):
        guard(method, f"/projects/{BAD}/issues/")


@pytest.mark.parametrize(
    "path",
    [
        f"/projects/{BAD}/issues/",
        f"/projects/{BAD}/issues/{ISSUE}/",
        f"/projects/{BAD}/issues/{ISSUE}/comments/",
        f"/projects/{BAD}/states/",
        f"/projects/{BAD}/labels/",
        f"/projects/{BAD}/modules/",
        f"/projects/{BAD}/cycles/",
    ],
)
def test_blocks_excluded_project_on_every_subresource(path):
    with pytest.raises(plane.GuardError):
        guard("GET", path)


def test_blocks_excluded_project_in_uppercase():
    """UUIDs are case-insensitive; the allowlist must not be bypassable by case."""
    with pytest.raises(plane.GuardError):
        guard("GET", f"/projects/{BAD.upper()}/issues/")


def test_allows_allowlisted_project_in_uppercase():
    guard("GET", f"/projects/{OK.upper()}/issues/")


def test_blocks_when_a_second_project_segment_is_excluded():
    """Every /projects/<uuid> segment is checked, not just the first."""
    with pytest.raises(plane.GuardError):
        guard("GET", f"/projects/{OK}/duplicates/projects/{BAD}/issues/")


def test_non_project_uuid_positions_are_not_checked():
    """Scope of the guard: only /projects/<uuid> and project-ish query keys.

    Issue, comment and module ids sit in other path positions and are never in
    the allowlist, so treating any bare UUID as a project would reject all
    normal traffic. Cross-project references in those positions are prevented by
    Plane itself, which 404s an issue id that does not belong to the project in
    the path.
    """
    guard("GET", f"/projects/{OK}/issues/{BAD}/")


def test_blocks_excluded_project_in_query_parameter():
    with pytest.raises(plane.GuardError):
        guard("GET", f"/issues/?project_id={BAD}")


def test_blocks_excluded_project_in_alternate_query_key():
    with pytest.raises(plane.GuardError):
        guard("GET", f"/issues/?projects={BAD}&per_page=100")


def test_allows_allowlisted_project_in_query_parameter():
    guard("GET", f"/issues/?project_id={OK}")


def test_unrelated_uuid_in_query_is_not_treated_as_a_project():
    """Only project-ish keys are checked; an assignee filter must still work."""
    guard("GET", f"/projects/{OK}/issues/?assignees={BAD}")


# --- the project object itself is read-only ---------------------------------


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "POST"])
def test_blocks_mutating_the_project_object(method):
    with pytest.raises(plane.GuardError):
        guard(method, f"/projects/{OK}/")


def test_allows_reading_the_project_object():
    guard("GET", f"/projects/{OK}/")


def test_blocks_creating_a_project():
    with pytest.raises(plane.GuardError):
        guard("POST", "/projects/")


def test_blocks_project_delete_with_trailing_query():
    with pytest.raises(plane.GuardError):
        guard("DELETE", f"/projects/{OK}/?force=true")


# --- workspace-scoped paths are read-only ----------------------------------


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_blocks_writes_on_workspace_scoped_paths(method):
    with pytest.raises(plane.GuardError):
        guard(method, "/issues/search/")


def test_blocks_workspace_write_even_with_allowed_project_in_query():
    """An allowed project in the query does not license a workspace-wide write."""
    with pytest.raises(plane.GuardError):
        guard("POST", f"/issues/?project_id={OK}")


def test_allows_workspace_reads():
    guard("GET", "/members/")
    guard("GET", "/issues/search/?q=test")


# --- allowed operations still work -----------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_allows_every_method_on_allowlisted_subresource(method):
    guard(method, f"/projects/{OK}/issues/{ISSUE}/")
    guard(method, f"/projects/{OK2}/issues/{ISSUE}/")


def test_empty_allowlist_blocks_everything():
    with pytest.raises(plane.GuardError):
        guard("GET", f"/projects/{OK}/issues/", allowed={})


# --- allowlist parsing ------------------------------------------------------


def test_parses_bare_uuids():
    assert plane._parse_allowlist(f"{OK},{OK2}") == {OK: OK, OK2: OK2}


def test_parses_labelled_uuids():
    assert plane._parse_allowlist(f"{OK}:Spike, {OK2}:Dark Factory") == {
        OK: "Spike",
        OK2: "Dark Factory",
    }


def test_parse_lowercases_uuid_keys():
    assert list(plane._parse_allowlist(OK.upper())) == [OK]


def test_parse_tolerates_blank_entries():
    assert plane._parse_allowlist(f",{OK},,") == {OK: OK}


def test_parse_rejects_non_uuid():
    with pytest.raises(plane.PlaneError):
        plane._parse_allowlist("my-project")


def test_parse_rejects_project_url_instead_of_uuid():
    with pytest.raises(plane.PlaneError):
        plane._parse_allowlist("https://app.plane.so/acme/projects/nope/issues")


# --- configuration ----------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        "PLANE_API_TOKEN",
        "PLANE_WORKSPACE",
        "PLANE_ALLOWED_PROJECTS",
        "PLANE_BASE_URL",
        "PLANE_ENV_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_cfg_requires_allowlist(clean_env):
    clean_env.setenv("PLANE_API_TOKEN", "plane_api_x")
    clean_env.setenv("PLANE_WORKSPACE", "acme")
    with pytest.raises(plane.PlaneError, match="PLANE_ALLOWED_PROJECTS"):
        plane._cfg()


def test_cfg_requires_token(clean_env):
    clean_env.setenv("PLANE_WORKSPACE", "acme")
    clean_env.setenv("PLANE_ALLOWED_PROJECTS", OK)
    with pytest.raises(plane.PlaneError, match="PLANE_API_TOKEN"):
        plane._cfg()


def test_cfg_requires_workspace(clean_env):
    clean_env.setenv("PLANE_API_TOKEN", "plane_api_x")
    clean_env.setenv("PLANE_ALLOWED_PROJECTS", OK)
    with pytest.raises(plane.PlaneError, match="PLANE_WORKSPACE"):
        plane._cfg()


def test_cfg_defaults_to_plane_cloud(clean_env):
    clean_env.setenv("PLANE_API_TOKEN", "plane_api_x")
    clean_env.setenv("PLANE_WORKSPACE", "acme")
    clean_env.setenv("PLANE_ALLOWED_PROJECTS", OK)
    assert plane._cfg()["base"] == "https://api.plane.so"


def test_cfg_reads_env_file_when_vars_absent(clean_env, tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "\n".join(
            [
                "# a comment",
                "UNRELATED=ignored",
                "PLANE_API_TOKEN=plane_api_fromfile",
                'PLANE_WORKSPACE="acme"',
                f"PLANE_ALLOWED_PROJECTS={OK}:Mine",
                "PLANE_BASE_URL=https://plane.internal",
            ]
        ),
        encoding="utf-8",
    )
    clean_env.setenv("PLANE_ENV_FILE", str(env))
    cfg = plane._cfg()
    assert cfg["token"] == "plane_api_fromfile"
    assert cfg["workspace"] == "acme"
    assert cfg["base"] == "https://plane.internal"
    assert cfg["allowed"] == {OK: "Mine"}
    assert "UNRELATED" not in os.environ


def test_real_environment_beats_env_file(clean_env, tmp_path):
    env = tmp_path / "env"
    env.write_text(f"PLANE_WORKSPACE=fromfile\nPLANE_ALLOWED_PROJECTS={OK}\n")
    clean_env.setenv("PLANE_ENV_FILE", str(env))
    clean_env.setenv("PLANE_API_TOKEN", "plane_api_x")
    clean_env.setenv("PLANE_WORKSPACE", "fromenv")
    assert plane._cfg()["workspace"] == "fromenv"


def test_missing_env_file_is_an_error(clean_env, tmp_path):
    clean_env.setenv("PLANE_ENV_FILE", str(tmp_path / "nope"))
    with pytest.raises(plane.PlaneError, match="could not be read"):
        plane._cfg()


# --- content conversion -----------------------------------------------------


def test_sanitize_replaces_arrows_and_dashes():
    assert plane.sanitize("a → b — c") == "a -> b - c"


def test_md_to_html_paragraph_and_inline():
    html = plane.md_to_html("Hello **bold** and `code` and [link](http://x)")
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert '<a href="http://x">link</a>' in html


def test_md_to_html_escapes_html_injection():
    assert "<script>" not in plane.md_to_html("<script>alert(1)</script>")


def test_md_to_html_lists():
    assert plane.md_to_html("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"
    assert plane.md_to_html("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"


def test_md_to_html_fenced_code_is_not_inline_formatted():
    html = plane.md_to_html("```\na = **not bold**\n```")
    assert "<pre><code>" in html
    assert "<strong>" not in html


def test_md_to_html_heading_levels_are_clamped():
    assert "<h3>" in plane.md_to_html("# Title")
    assert "<h6>" in plane.md_to_html("###### Deep")


def test_md_to_html_never_returns_empty():
    assert plane.md_to_html("") == "<p></p>"


def test_html_to_text_roundtrip_is_readable():
    text = plane.html_to_text("<p>Hello <strong>world</strong></p><ul><li>a</li></ul>")
    assert "Hello world" in text
    assert "- a" in text


def test_html_to_text_decodes_entities():
    assert plane.html_to_text("<p>a &amp; b &lt;c&gt;</p>") == "a & b <c>"


# --- list projection --------------------------------------------------------


def test_slim_builds_ref_and_flattens_nested_objects():
    row = plane.slim(
        {
            "id": ISSUE,
            "sequence_id": 12,
            "name": "Thing",
            "description_html": "<p>huge</p>",
            "state": {"name": "Backlog", "group": "backlog"},
            "assignees": [{"display_name": "Spike"}],
            "labels": [{"name": "bug"}],
        },
        "SPK",
    )
    assert row["ref"] == "SPK-12"
    assert row["state"] == "Backlog"
    assert row["assignees"] == ["Spike"]
    assert row["labels"] == ["bug"]
    assert "description_html" not in row


def test_slim_handles_unexpanded_relations():
    row = plane.slim({"id": ISSUE, "sequence_id": 3, "state": None, "assignees": []})
    assert row["ref"] == 3
    assert row["state"] is None
    assert row["assignees"] == []


def test_project_id_of_handles_both_shapes():
    assert plane._project_id_of({"project": OK}) == OK
    assert plane._project_id_of({"project": {"id": OK}}) == OK


# --- search filtering (the read-side half of the allowlist) -----------------


class _Args:
    def __init__(self, **kw):
        self.json = False
        self.__dict__.update(kw)


@pytest.fixture
def fake_search(monkeypatch):
    """Capture the request and return canned workspace-wide search hits."""
    calls = {}

    def fake_request(method, path, cfg, *, params=None, **_kw):
        calls["method"] = method
        calls["path"] = path
        calls["params"] = params
        return {
            "issues": [
                {
                    "id": ISSUE,
                    "sequence_id": 1,
                    "name": "Mine",
                    "project_id": OK,
                    "project__identifier": "SPK",
                },
                {
                    "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                    "sequence_id": 99,
                    "name": "Shared team secret",
                    "project_id": BAD,
                    "project__identifier": "DB",
                },
            ]
        }

    monkeypatch.setattr(plane, "_request", fake_request)
    return calls


def test_search_withholds_hits_outside_the_allowlist(fake_search, capsys):
    plane.cmd_search({"allowed": ALLOWED}, _Args(query="x"))
    out = capsys.readouterr()
    assert "Mine" in out.out
    assert "Shared team secret" not in out.out
    assert "1 hit(s) outside the allowlist withheld" in out.err


def test_search_uses_the_search_parameter_not_q(fake_search):
    """Plane returns an empty list for `q`, which is indistinguishable from
    'no matches'. Locking the parameter name prevents a silent regression."""
    plane.cmd_search({"allowed": ALLOWED}, _Args(query="hello"))
    assert fake_search["params"]["search"] == "hello"
    assert "q" not in fake_search["params"]


def test_search_builds_refs_from_the_response_identifier(fake_search, capsys):
    plane.cmd_search({"allowed": ALLOWED}, _Args(query="x"))
    assert "SPK-1" in capsys.readouterr().out
