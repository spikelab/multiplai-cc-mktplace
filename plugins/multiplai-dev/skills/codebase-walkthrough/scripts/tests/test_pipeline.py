"""Whole runs through `main()` with fake agents: outputs, boundary.json, check, resume, logging."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import jsonschema
import pytest

import fixture_repo
from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import markdown_headings, split_sections


def run_args(ws, tmp_path, *extra):
    return ["run", str(ws["engine"] / "bookings"), "--trust-repo", "--runs-dir", str(tmp_path / "runs"),
            "--output", str(tmp_path / "out"), "--docs", "https://docs.acme.test", "--lsp-timeout", "0", *extra]


@pytest.fixture
def done_run(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    return tmp_path / "out" / "bookings-walkthrough.md", run_dir, agents


def test_run_writes_markdown_and_html_with_the_same_headings(done_run):
    md_path, _, _ = done_run
    md = md_path.read_text()
    html = md_path.with_suffix(".html").read_text()
    assert "<svg" not in html
    md_heads = markdown_headings(md)
    html_heads = [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h2>(.*?)</h2>", html)]
    assert md_heads == html_heads
    for title in ("Overview", "Glossary", "End-to-end scenarios", "Module tour", "Who calls it and what it calls",
                  "Data stores and config", "Vendor docs", "Gotchas and questions for the owner", "Coverage"):
        assert title in md_heads


def test_boundary_json_lists_every_planted_reference_once(done_run):
    _, run_dir, _ = done_run
    data = json.loads((run_dir / "boundary.json").read_text())
    refs = {(r["repo"], r["kind"], r["path"].split("/", 1)[1]) for r in data["references"]}
    assert ("engine", "python-import", "reports/usage.py") in refs
    assert ("engine", "url-include", "project/urls.py") in refs
    assert ("engine", "celery-name", "project/settings.py") in refs
    assert ("front", "http-route", "src/api.ts") in refs
    assert ("warehouse", "sql-table", "models.sql") in refs
    assert all(r["method"] in ("ast", "ast-grep", "lsp", "sql-identifier") for r in data["references"])
    # comments are not references
    for r in data["references"]:
        assert not r["quote"].startswith(("#", "//", "--")), r


def test_finished_walkthrough_passes_check(done_run, capsys):
    md_path, _, _ = done_run
    capsys.readouterr()
    assert main(["check", str(md_path)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"check: [1-9]\d* checked, 0 failed", out)


def test_walkthrough_content(done_run):
    md_path, _, _ = done_run
    md = md_path.read_text()
    assert "engine/bookings/urls.py:6" in md                       # the webhook route, linked
    assert re.search(r"\| `ACME_ENV` \| \[`engine/project/settings.py:5`\]", md)
    assert "`/bookings/{}/ack`" in md and "`/webhooks`" in md
    assert "acme-api-collection/bookings/ack.yml" in md
    assert "Not in the collection `acme-api-collection`: `/webhooks`" in md
    assert "https://docs.acme.test/api/bookings-collection.md" in md
    assert fixture_repo.SECRET not in md
    assert "| ospite |" in md


def test_snapshots_never_hold_env_files_and_secrets_never_leave(done_run):
    md_path, run_dir, agents = done_run
    for call in agents.calls:
        assert fixture_repo.SECRET not in call["prompt"]
    for f in run_dir.rglob("*"):
        if f.is_file():
            assert not f.name.startswith(".env")
            assert fixture_repo.SECRET not in f.read_text(errors="replace"), f


def test_agent_mode_writes_compact_markdown_only(ws, tmp_path, monkeypatch, fake_fetch):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    args = ["run", str(ws["engine"] / "bookings"), "--trust-repo", "--runs-dir", str(tmp_path / "runs"),
            "--output", str(tmp_path / "out"), "--mode", "agent", "--lsp-timeout", "0"]
    assert main(args) == 0
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["bookings-walkthrough.md"]
    md = (out / "bookings-walkthrough.md").read_text()
    assert markdown_headings(md) == ["Commits read", "Files (7)", "Architecture", "Patterns & Conventions",
                                     "Key Interfaces", "Gotchas", "Coverage"]
    assert {c["schema"] for c in agents.calls} == {"ExploreOutput", "SectionOutput"}
    assert main(["check", str(out / "bookings-walkthrough.md")]) == 0


# --- walkthrough.json, for the review-viewer ---------------------------------------------

DEV = Path(__file__).resolve().parents[3]                  # plugins/multiplai-dev/skills
VIEWER_SCHEMA = DEV / "review-viewer" / "schema" / "walkthrough.v1.schema.json"
# The file the review-viewer suite loads through its own validation (its test_walkthrough_module.py).
VIEWER_FIXTURE = DEV / "review-viewer" / "scripts" / "tests" / "fixtures" / "module-walkthrough.json"
# Set to rewrite VIEWER_FIXTURE from this run, after a deliberate change to what `run` writes.
WRITE_FIXTURE = "WALKTHROUGH_WRITE_VIEWER_FIXTURE"


def _git_out(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_run_writes_a_walkthrough_json_the_viewer_schema_accepts(done_run, ws):
    md_path, _, _ = done_run
    wt = json.loads(md_path.with_suffix(".json").read_text())
    jsonschema.validate(wt, json.loads(VIEWER_SCHEMA.read_text()))
    empty_tree = subprocess.run(["git", "hash-object", "-t", "tree", "/dev/null"], capture_output=True,
                                text=True, check=True).stdout.strip()
    assert wt["base_sha"] == empty_tree
    assert wt["head_sha"] == _git_out(ws["engine"], "rev-parse", "HEAD").strip()
    assert wt["complete"] is True and "assessments" not in wt and "risk" not in wt
    tree = set(_git_out(ws["engine"], "ls-tree", "-r", "--name-only", "HEAD", "--", "bookings").split())
    anchored = {a["path"] for s in wt["steps"] for a in s["anchors"]}
    assert anchored and anchored <= tree                      # only the module's own files are anchors
    assert all(a["side"] == "head" for s in wt["steps"] for a in s["anchors"])
    assert {k["path"] for k in wt["skipped"]} == tree - anchored
    titles = [s["title"] for s in wt["steps"]]
    assert "Module tour" in titles and "Coverage" not in titles and "Commits read" not in titles
    assert "## Coverage" in wt["overview_md"] and "## Commits read" in wt["overview_md"]
    # Citations into the other repos stay as links in the text.
    who = next(s for s in wt["steps"] if s["title"] == "Who calls it and what it calls")
    assert "[`front/src/api.ts:" in who["body_md"]
    assert who["diagram"]["source"].startswith("graph LR") and "```mermaid" not in who["body_md"]


def test_the_viewer_fixture_is_what_run_writes(done_run):
    """review-viewer's suite cannot import this pipeline, so it loads a file this run wrote."""
    md_path, _, _ = done_run
    wt = json.loads(md_path.with_suffix(".json").read_text())
    if os.environ.get(WRITE_FIXTURE):
        VIEWER_FIXTURE.write_text(json.dumps(wt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def shape(w):     # the text holds paths relative to pytest's temporary directories
        return {**{k: v for k, v in w.items() if k not in ("generated_at", "overview_md")},
                "steps": [{k: v for k, v in s.items() if k != "body_md"} for s in w["steps"]]}

    assert shape(json.loads(VIEWER_FIXTURE.read_text())) == shape(wt), \
        f"run {WRITE_FIXTURE}=1 with this test to rewrite {VIEWER_FIXTURE.name}"


def test_header_names_the_module_directory_depth_and_mode(done_run, ws):
    md_path, _, _ = done_run
    md = md_path.read_text()
    where = re.search(r"^- Module directory: `([^`]+)`$", md, re.M).group(1)
    assert (md_path.parent / where).resolve() == (ws["engine"] / "bookings").resolve()
    assert re.search(r"^- Depth: `standard`$", md, re.M) and re.search(r"^- Mode: `human`$", md, re.M)


def test_split_sections_ignores_headings_inside_code_blocks():
    md = "# T\n\nintro\n\n## One\n\n````markdown\n## not a heading\n```\n````\n\n## Two\n\ntext\n"
    intro, sections = split_sections(md)
    assert intro.strip() == "# T\n\nintro"
    assert [t for t, _ in sections] == ["One", "Two"]
    assert "## not a heading" in sections[0][1]
