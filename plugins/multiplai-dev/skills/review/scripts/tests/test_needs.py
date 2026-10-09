"""Needs: information the review could not get, each with a command for a person."""

from __future__ import annotations

import subprocess as sp

from conftest import REAL_BRANCH_RULES

from review_pipeline import target
from review_pipeline.models import Need


def _failing_gh(monkeypatch, *, returncode=1, stdout="", stderr="HTTP 403: Resource not accessible\nmore"):
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")
    monkeypatch.setattr(target.subprocess, "run",
                        lambda argv, **kw: sp.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr))


# --- target: the pipeline's own lookups -----------------------------------------


def test_failed_branch_rules_lookup_is_a_need_with_the_exact_command(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch)
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) is None
    assert len(needs) == 1
    need = needs[0]
    assert (need.cause, need.source, need.blocks) == ("lookup-failed", "pipeline", "review")
    assert need.command == "gh api repos/o/r/rules/branches/main"
    assert "HTTP 403: Resource not accessible" in need.what and "more" not in need.what


def test_branch_rules_lookup_without_json_is_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch, returncode=0, stdout="<html>", stderr="")
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) is None
    assert [n.command for n in needs] == ["gh api repos/o/r/rules/branches/main"]
    assert "no JSON" in needs[0].what


def test_branch_rules_that_read_fine_add_no_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch, returncode=0, stdout="[]", stderr="")
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) == []
    assert needs == []


def test_pr_view_with_empty_title_and_base_is_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(target, "_pr_view", lambda repo_path, number: {
        "headRefOid": head, "baseRefOid": base, "headRefName": "x", "baseRefName": "", "title": "",
        "body": ""})
    resolved = target.resolve(repo, pr=7)
    needs: list[Need] = []
    target.build_target(resolved, needs=needs)
    assert len(needs) == 1
    assert needs[0].command == "gh pr view 7 --repo example/booking-engine --json title,baseRefName"
    assert needs[0].cause == "lookup-failed" and needs[0].source == "pipeline"


def test_pr_view_with_only_an_empty_body_is_not_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(target, "_pr_view", lambda repo_path, number: {
        "headRefOid": head, "baseRefOid": base, "headRefName": "x", "baseRefName": "main",
        "title": "A title", "body": ""})
    needs: list[Need] = []
    target.build_target(target.resolve(repo, pr=7), needs=needs)
    assert needs == []
