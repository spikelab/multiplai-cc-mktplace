"""Whole runs through `main()` with fake agents: outputs, boundary.json, check, resume, logging."""

from __future__ import annotations

import json
import re
import subprocess

import fixture_repo
import pytest
from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.render import markdown_headings


def run_args(ws, tmp_path, *extra):
    return [
        "run",
        str(ws["engine"] / "bookings"),
        "--trust-repo",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--output",
        str(tmp_path / "out"),
        "--docs",
        "https://docs.acme.test",
        "--lsp-timeout",
        "0",
        *extra,
    ]


def test_run_writes_markdown_and_html_with_the_same_headings(done_run):
    """A module's run writes no walkthrough.json: the review-viewer links the HTML page instead."""
    md_path, _, _ = done_run
    md = md_path.read_text()
    html = md_path.with_suffix(".html").read_text()
    assert not md_path.with_suffix(".json").exists()
    # Code draws every diagram of the page itself; no Mermaid is left for the browser to draw.
    assert '<pre class="mermaid">' not in html
    md_heads = markdown_headings(md)
    html_heads = [
        re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h2>(.*?)</h2>", html)
    ]
    assert (
        md_heads
        == html_heads
        == [
            "What it is",
            "Use cases",
            "How it works",
            "What it creates",
            "Configuration",
            "Gotchas",
            "Reference",
            "How this was made",
        ]
    )
    assert "End-to-end scenarios" not in md_heads


def test_boundary_json_lists_every_planted_reference_once(done_run):
    _, run_dir, _ = done_run
    data = json.loads((run_dir / "boundary.json").read_text())
    refs = {
        (r["repo"], r["kind"], r["path"].split("/", 1)[1]) for r in data["references"]
    }
    assert ("engine", "python-import", "reports/usage.py") in refs
    assert ("engine", "url-include", "project/urls.py") in refs
    assert ("engine", "celery-name", "project/settings.py") in refs
    assert ("front", "http-route", "src/api.ts") in refs
    assert ("warehouse", "sql-table", "models.sql") in refs
    assert all(
        r["method"] in ("ast", "ast-grep", "lsp", "sql-identifier")
        for r in data["references"]
    )
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
    # The webhook route and the beat task, traced from their handlers as caller seeds.
    assert (
        "`HTTP /hooks/acme/` (caller seed)" in md
        and "`bookings.poll (beat)` (caller seed)" in md
    )
    assert (
        "[`engine/bookings/views.py:5`](" in md
        and "[`engine/bookings/tasks.py:5`](" in md
    )
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
    args = [
        "run",
        str(ws["engine"] / "bookings"),
        "--trust-repo",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--output",
        str(tmp_path / "out"),
        "--mode",
        "agent",
        "--lsp-timeout",
        "0",
    ]
    assert main(args) == 0
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["bookings-walkthrough.md"]
    md = (out / "bookings-walkthrough.md").read_text()
    assert markdown_headings(md) == [
        "Commits read",
        "Files (7)",
        "Architecture",
        "Patterns & Conventions",
        "Key Interfaces",
        "Gotchas",
        "Coverage",
    ]
    assert {c["schema"] for c in agents.calls} == {"ExploreOutput", "SectionOutput"}
    assert main(["check", str(out / "bookings-walkthrough.md")]) == 0


def _git_out(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def test_header_names_the_module_directory_depth_and_mode(done_run, ws):
    md_path, _, _ = done_run
    md = md_path.read_text()
    where = re.search(r"^- Module directory: `([^`]+)`$", md, re.MULTILINE).group(1)
    assert (md_path.parent / where).resolve() == (ws["engine"] / "bookings").resolve()
    assert re.search(r"^- Depth: `standard`$", md, re.MULTILINE) and re.search(
        r"^- Mode: `human`$", md, re.MULTILINE
    )


def test_repo_key_names_a_worktree_by_its_project_not_its_directory(
    fresh_ws, tmp_path, monkeypatch, fake_fetch
):
    """buildme regenerates a walkthrough inside its build worktree, `buildme-<change>`, writing it into
    that worktree: `--repo-key` keeps the project's own key in the "Commits read" row and every citation."""
    worktree = fresh_ws["ws"] / "buildme-c"
    _git_out(
        fresh_ws["engine"], "worktree", "add", "-q", "-b", "buildme/c", str(worktree)
    )
    out = worktree / "docs" / "walkthroughs"
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert (
        main(
            [
                "run",
                str(worktree / "bookings"),
                "--trust-repo",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--output",
                str(out),
                "--lsp-timeout",
                "0",
                "--repos",
                str(worktree),
                "--repo-key",
                "engine",
            ]
        )
        == 0
    )
    md = (out / "bookings-walkthrough.md").read_text()
    sha = _git_out(worktree, "rev-parse", "HEAD").strip()
    assert re.search(rf"^\| engine \| \.\./\.\. \| `{sha}` \|$", md, re.MULTILINE), md
    assert "[`engine/bookings/" in md
    assert "buildme-c" not in md + (out / "bookings-walkthrough.html").read_text()
    assert main(["check", str(out / "bookings-walkthrough.md")]) == 0


@pytest.mark.parametrize("key", ["", ".", "..", "a/b", "a b", "a|b"])
def test_repo_key_must_be_a_plain_name(key, capsys):
    with pytest.raises(SystemExit):
        main(["run", ".", "--repo-key", key])
    assert "argument --repo-key" in capsys.readouterr().err
