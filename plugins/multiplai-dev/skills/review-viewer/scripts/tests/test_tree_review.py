"""A review of a whole tree (review --tree / --dir): base is git's empty tree."""

from __future__ import annotations

import json
import subprocess

from review_viewer.__main__ import _load_targets, build_parser
from review_viewer.gitdata import EMPTY_TREE, is_tree_review, tree_target
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
