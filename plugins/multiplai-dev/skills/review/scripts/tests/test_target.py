from __future__ import annotations

import subprocess

import pytest

from review_pipeline import target
from review_pipeline.target import TargetError


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_range_resolves_to_full_shas(fixture_repo):
    repo, base, head = fixture_repo
    r = target.resolve(repo, range_=f"{base[:8]}..{head[:8]}")
    assert (r.base_sha, r.head_sha, r.problem) == (base, head, "")
    assert target.target_gate(r, target.diff_text(r.repo, base, head)).passed


def test_branch_uses_merge_base_with_default_branch(fixture_repo):
    repo, base, head = fixture_repo
    r = target.resolve(repo, branch="feature/db-2038")
    assert (r.base_sha, r.head_sha) == (base, head)
    info = target.build_target(r)
    assert info.slug == "booking-engine--feature_db-2038"
    assert info.kind == "branch"


def test_missing_branch_fails_the_gate(fixture_repo):
    repo, *_ = fixture_repo
    r = target.resolve(repo, branch="nope")
    result = target.target_gate(r, None)
    assert not result.passed and "origin/nope not found" in result.reason


def test_empty_diff_fails_the_gate(fixture_repo):
    repo, _, head = fixture_repo
    r = target.resolve(repo, range_=f"{head}..{head}")
    result = target.target_gate(r, target.diff_text(r.repo, head, head))
    assert not result.passed and "empty" in result.reason


def test_unresolvable_range_end_fails_the_gate(fixture_repo):
    repo, base, _ = fixture_repo
    r = target.resolve(repo, range_=f"{base}..0000000")
    assert not target.target_gate(r, None).passed


def test_exactly_one_selector(fixture_repo):
    repo, base, head = fixture_repo
    with pytest.raises(TargetError):
        target.resolve(repo, branch="x", range_=f"{base}..{head}")
    with pytest.raises(TargetError):
        target.resolve(repo)
    with pytest.raises(TargetError):
        target.resolve(repo, range_=f"{base}...{head}")


def test_pr_uses_gh_view_and_merge_base(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    calls = []

    def fake_view(repo_path, number):
        calls.append(number)
        return {"headRefOid": head, "baseRefOid": base, "headRefName": "feature/db-2038"}

    monkeypatch.setattr(target, "_pr_view", fake_view)
    r = target.resolve(repo, pr=812)
    assert (r.base_sha, r.head_sha, r.pr, calls) == (base, head, 812, [812])
    info = target.build_target(r)
    assert info.slug == "booking-engine--pr-812" and info.pr == 812


def test_writes_diff_commits_and_files_and_leaves_the_repo_alone(fixture_repo, tmp_path):
    repo, base, head = fixture_repo
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "symbolic-ref", "HEAD"), _git(repo, "status", "--short"))
    r = target.resolve(repo, range_=f"{base}..{head}")
    info = target.build_target(r, tickets=["DB-2038"])
    info = target.write_target_files(info, target.diff_text(r.repo, base, head), tmp_path / info.slug)
    assert info.slug == f"booking-engine--{base}..{head}"
    assert sorted(info.files) == ["direct_booking.py", "rateplan_service.py", "settings.py"]
    assert info.commits == [(head, "DB-2038: direct booking from the modal")]
    assert "+KEYWORD = 'dolcebot'" in (tmp_path / info.slug / "diff.patch").read_text()
    assert (tmp_path / info.slug / "files.txt").read_text().count("\n") == 3
    assert info.remote_url == "https://github.com/example/booking-engine.git"
    after = (_git(repo, "rev-parse", "HEAD"), _git(repo, "symbolic-ref", "HEAD"), _git(repo, "status", "--short"))
    assert before == after


def test_sanitize_slug():
    assert target.sanitize_slug("repo--feature/x y!") == "repo--feature_x_y"
    assert target.sanitize_slug("a..b") == "a..b"


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git", "git@github.com:o/r.git", "https://github.com/o/r", "ssh://git@github.com/o/r.git",
])
def test_github_web_base(url):
    assert target.github_web_base(url) == "https://github.com/o/r"


def test_github_web_base_other_hosts():
    assert target.github_web_base("https://gitlab.com/o/r.git") is None
    assert target.github_web_base(None) is None


def test_snapshot_extracts_head(target_info, tmp_path):
    snap = target.snapshot_head(target_info, tmp_path / "tree")
    assert (snap / "rateplan_service.py").read_text().startswith("KEYWORD = 'dolcebot'")
    assert target.snapshot_head(target_info, snap) == snap  # reused when head matches


def test_deployed_in(target_info, fixture_repo):
    repo, base, head = fixture_repo
    assert target.deployed(target_info.model_copy(update={"deployed_in": "feature/db-2038"})) == "yes"
    assert target.deployed(target_info.model_copy(update={"deployed_in": "main"})) == "no"
    assert target.deployed(target_info.model_copy(update={"deployed_in": "gone"})).startswith("unknown")
    assert target.deployed(target_info) is None
