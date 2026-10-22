from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import REAL_BRANCH_RULES
from review_pipeline import target
from review_pipeline.target import TargetError


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


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
    assert info.slug == "example-booking-engine--feature_db-2038"
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
        return {
            "headRefOid": head,
            "baseRefOid": base,
            "headRefName": "feature/db-2038",
            "baseRefName": "main",
            "title": "DB-2038: direct booking",
            "body": "Only the keyword filter.\n\nCI gates the merge.",
        }

    rules = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1}}
    ]
    asked = []

    def fake_rules(repo_path, branch, needs=None):
        asked.append(branch)
        return rules

    monkeypatch.setattr(target, "_pr_view", fake_view)
    monkeypatch.setattr(target, "branch_rules", fake_rules)
    r = target.resolve(repo, pr=812)
    assert (r.base_sha, r.head_sha, r.pr, calls) == (base, head, 812, [812])
    assert (r.title, r.base_ref) == ("DB-2038: direct booking", "main")
    info = target.build_target(r)
    assert info.slug == "example-booking-engine--pr-812" and info.pr == 812
    assert (info.title, info.description, info.base_ref) == (
        "DB-2038: direct booking",
        "Only the keyword filter.\n\nCI gates the merge.",
        "main",
    )
    assert info.branch_rules == rules and asked == ["main"]


def test_pr_view_asks_for_title_body_and_base():
    for field in (
        "headRefOid",
        "baseRefOid",
        "headRefName",
        "baseRefName",
        "title",
        "body",
    ):
        assert field in target.PR_VIEW_FIELDS.split(",")


def test_branch_rules_degrade_to_empty(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(target.shutil, "which", lambda name: None)
    assert REAL_BRANCH_RULES(repo, "main") is None  # no gh
    assert REAL_BRANCH_RULES(repo, "") is None
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://gitlab.com/o/r.git")
    assert REAL_BRANCH_RULES(repo, "main") is None  # not GitHub


def test_branch_rules_parse_gh_api_output(fixture_repo, monkeypatch):
    import subprocess as sp

    repo, base, head = fixture_repo
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return sp.CompletedProcess(
            argv, 0, stdout='[{"type": "deletion"}, 7]', stderr=""
        )

    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")
    monkeypatch.setattr(target.subprocess, "run", fake_run)
    assert REAL_BRANCH_RULES(repo, "main") == [{"type": "deletion"}]
    assert seen[0][1:] == ["api", "repos/{owner}/{repo}/rules/branches/main"]


def test_branch_rules_api_failure_is_empty_not_fatal(fixture_repo, monkeypatch):
    import subprocess as sp

    repo, base, head = fixture_repo
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")
    monkeypatch.setattr(
        target.subprocess,
        "run",
        lambda argv, **kw: sp.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404"),
    )
    assert REAL_BRANCH_RULES(repo, "main") is None


def test_branch_rules_empty_list_means_no_rules(fixture_repo, monkeypatch):
    import subprocess as sp

    repo, base, head = fixture_repo
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")
    monkeypatch.setattr(
        target.subprocess,
        "run",
        lambda argv, **kw: sp.CompletedProcess(argv, 0, stdout="[]", stderr=""),
    )
    assert REAL_BRANCH_RULES(repo, "main") == []


def test_writes_diff_commits_and_files_and_leaves_the_repo_alone(
    fixture_repo, tmp_path
):
    repo, base, head = fixture_repo
    before = (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "symbolic-ref", "HEAD"),
        _git(repo, "status", "--short"),
    )
    r = target.resolve(repo, range_=f"{base}..{head}")
    info = target.build_target(r, tickets=["DB-2038"])
    info = target.write_target_files(
        info, target.diff_text(r.repo, base, head), tmp_path / info.slug
    )
    assert info.slug == f"example-booking-engine--{base}..{head}"
    assert sorted(info.files) == [
        "direct_booking.py",
        "rateplan_service.py",
        "settings.py",
    ]
    assert info.commits == [(head, "DB-2038: direct booking from the modal")]
    assert "+KEYWORD = 'dolcebot'" in (tmp_path / info.slug / "diff.patch").read_text()
    assert (tmp_path / info.slug / "files.txt").read_text().count("\n") == 3
    assert info.remote_url == "https://github.com/example/booking-engine.git"
    assert (info.title, info.description, info.base_ref, info.branch_rules) == (
        "",
        "",
        "",
        None,
    )
    after = (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "symbolic-ref", "HEAD"),
        _git(repo, "status", "--short"),
    )
    assert before == after


def test_sanitize_slug():
    assert target.sanitize_slug("repo--feature/x y!") == "repo--feature_x_y"
    assert target.sanitize_slug("a..b") == "a..b"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r.git",
        "git@github.com:o/r.git",
        "https://github.com/o/r",
        "ssh://git@github.com/o/r.git",
    ],
)
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
    assert (
        target.deployed(
            target_info.model_copy(update={"deployed_in": "feature/db-2038"})
        )
        == "yes"
    )
    assert (
        target.deployed(target_info.model_copy(update={"deployed_in": "main"})) == "no"
    )
    assert target.deployed(
        target_info.model_copy(update={"deployed_in": "gone"})
    ).startswith("unknown")
    assert target.deployed(target_info) is None


# --- folder names: <out>/<repo>/<object>/ -------------------------------------------


def _resolved(
    kind: str, ref: str, pr: int | None = None, path: str = ""
) -> target.Resolved:
    return target.Resolved(
        repo=Path("/x"),
        kind=kind,
        ref=ref,
        base_sha=None,
        head_sha=None,
        pr=pr,
        path=path,
    )


@pytest.mark.parametrize(
    ("resolved", "name"),
    [
        (_resolved("pr", "7", pr=7), "pr-7"),
        (_resolved("branch", "feat/x"), "branch-feat_x"),
        (_resolved("branch", "main"), "branch-main"),
        (_resolved("range", "a..b"), "range-a..b"),
        (_resolved("tree", "."), "tree"),
        (_resolved("tree", "src", path="src"), "tree-src"),
        (_resolved("tree", "src/a b", path="src/a b"), "tree-src_a_b"),
    ],
    ids=[
        "pr",
        "branch-slash",
        "branch",
        "range",
        "whole-tree",
        "tree-path",
        "tree-path-cleaned",
    ],
)
def test_object_name_says_what_kind_of_thing_was_reviewed(resolved, name):
    assert target.object_name(resolved) == name


@pytest.mark.parametrize(
    ("url", "pair"),
    [
        ("https://github.com/spikelab/github-tests.git", ("spikelab", "github-tests")),
        ("git@github.com:spikelab/github-tests.git", ("spikelab", "github-tests")),
        ("ssh://git@github.com/spikelab/github-tests", ("spikelab", "github-tests")),
        ("github-tests", None),
        ("", None),
        (None, None),
    ],
)
def test_origin_owner_repo_and_origin_name_agree(url, pair):
    assert target.origin_owner_repo(url) == pair
    assert target.origin_name(url) == ("-".join(pair) if pair else None)


@pytest.mark.parametrize(
    ("name", "owner", "part"),
    [
        ("github-tests", "spikelab", "github-tests"),
        ("legacy", "spikelab", "legacy@spikelab"),
        (".locks", "spikelab", ".locks@spikelab"),
        ("legacy", None, "legacy@local"),
        ("my repo", None, "my_repo"),
    ],
)
def test_repo_part_renames_the_two_names_the_review_root_uses(name, owner, part):
    assert target.repo_part(name, owner) == part


def test_checkout_repo_part_reads_origin_else_the_directory_name(
    fixture_repo, tmp_path
):
    repo, _, _ = fixture_repo
    assert target.checkout_repo_part(repo) == ("booking-engine", "example")
    bare = tmp_path / "legacy"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    assert target.checkout_repo_part(bare) == ("legacy@local", None)


def test_dir_object_names_the_directory_and_its_path_hash(tmp_path):
    src = tmp_path / "proj"
    repo, obj = target.dir_object(src)
    assert repo == "proj"
    assert obj == f"dir-{target.dir_name(src)}"
    assert obj.startswith("dir-proj-") and len(obj) == len("dir-proj-") + 6
    repo, obj = target.dir_object(tmp_path / "legacy")
    assert repo == "legacy@local" and obj.startswith("dir-legacy-")


def test_is_review_folder_needs_a_file_the_pipeline_writes(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    assert not target.is_review_folder(d)
    (d / ".DS_Store").write_text("x")
    (d / "rounds").mkdir()
    assert not target.is_review_folder(d)
    for name in ("findings.json", "review-state.json", "files.txt"):
        f = d / name
        f.write_text("{}")
        assert target.is_review_folder(d)
        f.unlink()
    assert not target.is_review_folder(tmp_path / "missing")
