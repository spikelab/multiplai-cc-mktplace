"""A review of a whole tree (review --tree / --dir): base is git's empty tree."""

from __future__ import annotations

import json
import subprocess

from review_viewer.__main__ import _load_targets, build_parser
from review_viewer.gitdata import EMPTY_TREE, is_tree_review, tree_path, tree_target
from review_viewer.models import Target
from review_viewer.stats import change_stats, read_commits


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def _load(*argv):
    return _load_targets(build_parser().parse_args(["serve", *argv]))


def _files_at(repo, head, path=""):
    spec = ["--", path] if path else []
    return [f for f in _git(repo, "ls-tree", "-r", "-z", "--name-only", head, *spec).split("\0") if f]


def test_serve_tree_resolves_every_file_at_head_against_the_empty_tree(fixture_repo, tmp_path, monkeypatch):
    repo, _, head = fixture_repo
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    loaded, meta = _load("--tree", "--repo", str(repo), "--reviews-dir", str(tmp_path / "none"))
    (ff, _box), = loaded
    t = ff.target
    assert (t.base_sha, t.head_sha) == (EMPTY_TREE, head) and is_tree_review(t)
    assert t.files_changed == _files_at(repo, head)
    assert t.slug == "fixture-repo--tree" and t.label == f"fixture-repo: whole tree at {head[:8]}"
    assert meta[t.slug]["review"] == "none"


def test_serve_tree_path_limits_the_files(fixture_repo, tmp_path, monkeypatch):
    repo, _, head = fixture_repo
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    loaded, _ = _load("--tree", "--path", "app", "--repo", str(repo), "--reviews-dir", str(tmp_path / "none"))
    (ff, _box), = loaded
    assert ff.target.files_changed == _files_at(repo, head, "app") and ff.target.slug == "fixture-repo--tree-app"
    assert _load("--path", "app", "--repo", str(repo)) == 2  # --path needs --tree
    assert _load("--tree", "--range", "a..b", "--repo", str(repo)) == 2


def test_serve_tree_with_a_subdirectory_repo_shows_that_directory_under_the_review_slug(
        fixture_repo, tmp_path, monkeypatch):
    repo, _, head = fixture_repo
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    loaded, _ = _load("--tree", "--repo", str(repo / "app"), "--reviews-dir", str(tmp_path / "none"))
    (ff, _box), = loaded
    # The slug and root-relative files `review --repo <repo>/app --tree` writes.
    assert ff.target.slug == "fixture-repo--tree-app" and ff.target.repo_path == str(repo.resolve())
    assert ff.target.files_changed == _files_at(repo, head, "app")
    # --path is relative to the subdirectory.
    first = _files_at(repo, head, "app")[0]
    assert tree_path(repo / "app", repo.resolve(), "./" + first.split("/", 1)[1]) == first
    assert tree_path(repo, repo.resolve(), "app") == "app"


def test_a_tree_review_has_no_commits(fixture_repo):
    repo, base, head = fixture_repo
    assert read_commits(str(repo), EMPTY_TREE, head) == []
    assert read_commits(str(repo), base, head)  # a change still lists its commits
    stats = change_stats(tree_target(repo, "HEAD"))
    assert stats.commits == [] and stats.files == len(_files_at(repo, head))


def test_a_tree_review_opens_with_every_file_added_and_no_commits(fixture_repo, findings_path, start_live):
    repo, _, head = fixture_repo
    data = json.loads(findings_path.read_text())
    files = _files_at(repo, head)
    data["target"].update(base_sha=EMPTY_TREE, slug="fixture-repo--tree",
                          label=f"fixture-repo: whole tree at {head[:8]}", files_changed=files)
    findings_path.write_text(json.dumps(data))
    live = start_live()
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and detail["findings"]["target"]["base_sha"] == EMPTY_TREE
    for path in files:
        status, view = live.request("GET", f"/api/targets/{live.slug}/file?path={path}")
        assert status == 200
        if view.get("binary"):
            continue
        kinds = {row["k"] for row in view["rows"]}
        assert kinds <= {"add", "gap"} and "add" in kinds, (path, kinds)
    stats = change_stats(Target.model_validate(data["target"]))
    assert stats.commits == [] and not any(b.id == "risk" for b in stats.badges)


def _tree_review(dirs, name, repo, head, slug, files):
    """A findings.json for a tree review of *slug* at *head*, written to `<dirs>/<name>/`."""
    from conftest import FIXTURE

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["target"].update(repo_path=str(repo), base_sha=EMPTY_TREE, head_sha=head, slug=slug,
                          label=slug, files_changed=files)
    data["findings"] = []
    out = dirs / name / "findings.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def test_find_review_matches_a_tree_review_only_of_the_same_directory(fixture_repo, tmp_path):
    from review_viewer.__main__ import find_review

    repo, _, head = fixture_repo
    reviews = tmp_path / "reviews"
    whole = _tree_review(reviews, "whole", repo, head, "fixture-repo--tree", _files_at(repo, head))
    app = _tree_review(reviews, "app", repo, head, "fixture-repo--tree-app", _files_at(repo, head, "app"))
    assert find_review(tree_target(repo, "HEAD", "app"), [reviews])[:2] == ("match", app)
    assert find_review(tree_target(repo, "HEAD"), [reviews])[:2] == ("match", whole)
    # Same commit, but no review of this directory: none, not the newest tree review.
    other = sorted({f.split("/", 1)[0] for f in _files_at(repo, head) if "/" in f} - {"app"})[0]
    assert find_review(tree_target(repo, "HEAD", other), [reviews]) == ("none", None, None)


def test_change_stats_of_a_tree_limited_by_path_counts_only_that_directory(fixture_repo):
    repo, _, head = fixture_repo
    app = _files_at(repo, head, "app")
    stats = change_stats(tree_target(repo, "HEAD", "app"))
    assert app and len(app) < len(_files_at(repo, head))
    assert stats.files == len(app)
    assert set(stats.per_file) == set(app)
    whole = change_stats(tree_target(repo, "HEAD"))
    # The fixture's only file outside app/ is a binary one: the whole tree counts it, app/ does not.
    assert stats.added == sum(f["added"] or 0 for f in stats.per_file.values())
    assert (stats.binary, whole.binary) == (0, 1) and stats.files < whole.files
    assert sum(k["files"] for k in stats.by_kind.values()) == len(app)
