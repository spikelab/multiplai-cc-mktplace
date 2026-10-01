"""Whole runs through `main()` with fake agents: outputs, boundary.json, check, resume, logging."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import fixture_repo
from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import markdown_headings


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
