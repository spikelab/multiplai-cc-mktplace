from __future__ import annotations

import json
import os
import subprocess
from sys import executable as PYTHON

import pytest
from review_viewer.__main__ import _load_targets, build_parser, find_review
from review_viewer.gitdata import EMPTY_TREE


@pytest.fixture
def env(tmp_path, monkeypatch):
    """No workspace: output goes under ~/.multiplai, never the directory the
    user ran from."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    ran_from = tmp_path / "ran-from"
    ran_from.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("PWD", str(ran_from))
    monkeypatch.setenv("HOME", str(home))
    return home / ".multiplai"


def _review(reviews, findings_path, name="fixture-review", **target_changes):
    data = json.loads(findings_path.read_text())
    data["target"].update(target_changes)
    d = reviews / name
    d.mkdir(parents=True)
    (d / "findings.json").write_text(json.dumps(data))
    return d / "findings.json"


def _load(*argv):
    args = build_parser().parse_args(["serve", *argv])
    return _load_targets(args)


def test_exact_match_review_loads_its_findings(
    env, fixture_repo, findings_path, tmp_path
):
    repo, base, head = fixture_repo
    reviews = tmp_path / "reviews"
    path = _review(reviews, findings_path, repo_path="/somewhere/else")
    loaded, meta = _load(
        "--target", "main~1..main", "--repo", str(repo), "--reviews-dir", str(reviews)
    )
    ((ff, box),) = loaded
    assert box == path.parent / "viewer"
    assert len(ff.findings) == 5 and ff.target.repo_path == str(repo.resolve())
    assert meta[ff.target.slug] == {"pr": None, "notice": None, "review": "findings: 5"}


def test_stale_review_gives_a_notice_and_loads_nothing(
    env, fixture_repo, findings_path, tmp_path
):
    repo, base, head = fixture_repo
    reviews = tmp_path / "reviews"
    _review(reviews, findings_path, head_sha="f" * 40)
    loaded, meta = _load(
        "--repo", str(repo), "--range", "main~1..main", "--reviews-dir", str(reviews)
    )
    ((ff, box),) = loaded
    assert ff.findings == [] and (ff.target.base_sha, ff.target.head_sha) == (
        base,
        head,
    )
    m = meta[ff.target.slug]
    assert m["review"] == "stale ffffffff"
    assert m["notice"].startswith(
        f"a review exists for ffffffff; this diff is at {head[:8]}"
    )
    assert box == env / "review-viewer" / ff.target.slug / "viewer"


def test_no_review_is_plain_diff_under_the_output_root(env, fixture_repo, tmp_path):
    repo, base, head = fixture_repo
    loaded, meta = _load(
        "--target",
        "main~1..main",
        "--repo",
        str(repo),
        "--reviews-dir",
        str(tmp_path / "empty"),
    )
    ((ff, box),) = loaded
    assert ff.findings == [] and meta[ff.target.slug]["review"] == "none"
    assert box == env / "review-viewer" / ff.target.slug / "viewer"
    assert not (tmp_path / "ran-from" / "review-viewer").exists()


def test_workspace_inbox_wins_when_there_is_one(
    env, fixture_repo, tmp_path, monkeypatch
):
    repo, _, _ = fixture_repo
    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (tmp_path / "cfg" / ".workspace").write_text(str(ws))
    loaded, _ = _load(
        "--target",
        "main~1..main",
        "--repo",
        str(repo),
        "--reviews-dir",
        str(tmp_path / "empty"),
    )
    ((ff, box),) = loaded
    assert box == ws / "INBOX" / "review-viewer" / ff.target.slug / "viewer"


def test_default_reviews_dir_is_the_review_skills(
    env, fixture_repo, findings_path, tmp_path
):
    repo, _, _ = fixture_repo
    _review(tmp_path / "data" / "reviews", findings_path)
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo))
    assert meta[loaded[0][0].target.slug]["review"] == "findings: 5"


def test_a_review_not_in_the_data_dir_is_found_in_the_old_inbox_location(
    env, fixture_repo, findings_path, tmp_path
):
    repo, _, _ = fixture_repo
    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (tmp_path / "cfg" / ".workspace").write_text(str(ws))
    path = _review(ws / "INBOX" / "reviews", findings_path)
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo))
    ((ff, box),) = loaded
    m = meta[ff.target.slug]
    assert m["review"] == "findings: 5" and box == path.parent / "viewer"
    assert (
        m["notice"]
        == f"This review was loaded from the old location {path.parent}, not from {tmp_path / 'data' / 'reviews'}."
    )


def test_the_data_dir_wins_over_the_old_inbox_location(
    env, fixture_repo, findings_path, tmp_path
):
    repo, _, _ = fixture_repo
    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (tmp_path / "cfg" / ".workspace").write_text(str(ws))
    _review(ws / "INBOX" / "reviews", findings_path)
    path = _review(tmp_path / "data" / "reviews", findings_path)
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo))
    ((ff, box),) = loaded
    assert box == path.parent / "viewer" and meta[ff.target.slug]["notice"] is None


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (tmp_path / "cfg" / ".workspace").write_text(str(ws))
    return ws


@pytest.mark.parametrize(
    "where",
    ["old-name", "from-scratchpad/review-r4_old-name"],
    ids=["copied-directory", "archive-layout"],
)
def test_a_review_migrate_copied_into_the_data_dir_is_found(
    env, fixture_repo, findings_path, tmp_path, where
):
    """`migrate` copies old reviews to `<data_dir>/reviews/legacy/<name>/`, one level (or, for an
    archive's own layout, two) below where new reviews are; the INBOX originals may be gone."""
    repo, _, _ = fixture_repo
    _workspace(tmp_path)
    copies = tmp_path / "data" / "reviews" / "legacy"
    path = _review(copies, findings_path, name=where)
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo))
    ((ff, box),) = loaded
    m = meta[ff.target.slug]
    assert m["review"] == "findings: 5" and box == path.parent / "viewer"
    assert m["notice"] == (
        f"This review was loaded from {path.parent}, a copy of a review written before "
        f"multiplai-dev 0.46, not from {tmp_path / 'data' / 'reviews'}."
    )


def test_a_kept_round_in_a_migrated_copy_is_not_loaded_as_the_review(
    env, fixture_repo, findings_path, tmp_path
):
    repo, _, _ = fixture_repo
    _workspace(tmp_path)
    _review(
        tmp_path / "data" / "reviews" / "legacy",
        findings_path,
        name="old-name/rounds/abcdef012345",
    )
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo))
    assert meta[loaded[0][0].target.slug]["review"] == "none"


def _github_clone(fixture_repo, dest):
    """A checkout whose `origin` names a GitHub repo, so its new slugs differ from the old ones."""
    src, _, _ = fixture_repo
    subprocess.run(
        ["git", "clone", "-q", str(src), str(dest)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(dest),
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/fixture.git",
        ],
        check=True,
        capture_output=True,
    )
    return dest


def test_an_old_inbox_tree_review_named_after_the_directory_is_found(
    env, fixture_repo, findings_path, tmp_path
):
    """Reviews written before 0.45 carry `<dirname>--…` slugs; the viewer now computes `<owner>-<repo>--…`."""
    _, _, head = fixture_repo
    clone = _github_clone(fixture_repo, tmp_path / "old-checkout")
    ws = _workspace(tmp_path)
    path = _review(
        ws / "INBOX" / "reviews",
        findings_path,
        name="old-checkout--tree",
        base_sha=EMPTY_TREE,
        head_sha=head,
        slug="old-checkout--tree",
    )
    loaded, meta = _load("--tree", "HEAD", "--repo", str(clone))
    ((ff, box),) = loaded
    assert box == path.parent / "viewer" and ff.target.slug == "old-checkout--tree"
    assert meta[ff.target.slug]["notice"].startswith(
        f"This review was loaded from the old location {path.parent}"
    )


def test_an_old_inbox_review_named_after_the_directory_is_reported_stale(
    env, fixture_repo, findings_path, tmp_path
):
    clone = _github_clone(fixture_repo, tmp_path / "old-checkout")
    ws = _workspace(tmp_path)
    _review(
        ws / "INBOX" / "reviews",
        findings_path,
        name="old-checkout--tree",
        base_sha=EMPTY_TREE,
        head_sha="f" * 40,
        slug="old-checkout--tree",
    )
    loaded, meta = _load("--tree", "HEAD", "--repo", str(clone))
    ((ff, _),) = loaded
    assert (
        ff.target.slug == "acme-fixture--tree"
        and meta[ff.target.slug]["review"] == "stale ffffffff"
    )


def test_find_review_skips_broken_files(fixture_repo, findings_path, tmp_path):
    reviews = tmp_path / "reviews"
    (reviews / "broken").mkdir(parents=True)
    (reviews / "broken" / "findings.json").write_text("{not json")
    from review_viewer.models import load_findings

    target = load_findings(findings_path).target
    assert find_review(target, [reviews])[0] == "none"


def test_bad_combinations_exit_2(env, fixture_repo, findings_path):
    repo, _, _ = fixture_repo
    assert _load("--range", "main~1..main") == 2
    assert _load("--target", "x", "--range", "a..b", "--repo", str(repo)) == 2
    assert _load(str(findings_path), "--target", "main~1..main") == 2
    assert _load(str(findings_path), "--repo", str(repo)) == 2
    assert _load("--target", "no-such-branch", "--repo", str(repo)) == 2


def test_serve_prints_the_summary_line_last(env, fixture_repo, tmp_path):
    from conftest import free_port

    repo, base, head = fixture_repo
    proc = subprocess.Popen(
        [
            PYTHON,
            "-m",
            "review_viewer",
            "--session-id",
            "sess-W",
            "serve",
            "--target",
            "main~1..main",
            "--repo",
            str(repo),
            "--reviews-dir",
            str(tmp_path / "none"),
            "--port",
            str(free_port()),
            "--idle",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if line.startswith("summary:"):
                break
        prefixes = [line.split(":", 1)[0] for line in lines]
        assert prefixes[0] == "open" and prefixes[-3:] == [
            "monitor",
            "pending",
            "summary",
        ]
        assert "mailbox" in prefixes and "url" in prefixes
        box = lines[prefixes.index("mailbox")].split(": ", 1)[1]
        assert lines[-1] == f"summary: {os.path.dirname(box)}/summary.json (none)"
        served = json.loads(open(os.path.join(box, "target.json")).read())
        assert served["findings"]["target"]["head_sha"] == head
    finally:
        subprocess.run(
            [PYTHON, "-m", "review_viewer", "stop", "--all"], capture_output=True
        )
        try:
            proc.wait(10)
        finally:
            if proc.poll() is None:
                proc.kill()


# --- where reviews are: <dir>/<repo>/<object>/, 0.46's <dir>/<slug>/, and 0.42's nested folders ---


def _target(findings_path):
    from review_viewer.models import load_findings

    return load_findings(findings_path).target


@pytest.mark.parametrize(
    "name",
    [
        "acme-fixture--pr-7",  # 0.46: <dir>/<slug>/
        "fixture/pr-7",  # new: <dir>/<repo>/<object>/
        "fixture/pr-7/checkout--pr-7",  # 0.42 nested under a new-layout folder
        "fixture/tree",  # a whole-tree review
        "tree/pr-7",
        "source/pr-7",
        "viewer/pr-7",
        "rounds/pr-7",  # repos named like a snapshot
    ],
    ids=[
        "046",
        "new",
        "042-nested",
        "whole-tree",
        "repo-tree",
        "repo-source",
        "repo-viewer",
        "repo-rounds",
    ],
)
def test_find_review_finds_a_review_in_each_layout(findings_path, tmp_path, name):
    reviews = tmp_path / "reviews"
    path = _review(reviews, findings_path, name=name)
    (reviews / name.split("/")[0]).joinpath(".DS_Store").write_text("finder")
    (reviews / ".DS_Store").write_text("finder")
    assert find_review(_target(findings_path), [reviews])[:2] == ("match", path)


@pytest.mark.parametrize(
    "name",
    [
        "legacy/x--pr-7",  # migrate's copies are read as deep_dirs, not here
        "fixture/rounds",  # never an object
        "fixture/pr-7/rounds/01-aaaaaaaaaaaa",  # a kept round
        "fixture/pr-7/viewer/x",
        "fixture/pr-7/tree/x",  # a snapshot inside a review folder
        "acme-fixture--pr-7/sub",  # under a 0.46 review folder
        "fixture/pr-7/checkout--pr-7/deeper",
    ],
    ids=[
        "legacy",
        "rounds-object",
        "kept-round",
        "viewer",
        "snapshot",
        "under-046",
        "too-deep",
    ],
)
def test_find_review_never_reads_a_round_snapshot_or_copy_as_the_review(
    findings_path, tmp_path, name
):
    reviews = tmp_path / "reviews"
    (reviews / "acme-fixture--pr-7").mkdir(parents=True)
    (reviews / "acme-fixture--pr-7" / "files.txt").write_text("")
    _review(reviews, findings_path, name=name)
    assert find_review(_target(findings_path), [reviews])[0] == "none"


@pytest.mark.parametrize(
    "name",
    [
        "rounds/01-aaaaaaaaaaaa",  # the folder's own kept round
        "tree",
        "source",
        "base-tree",  # its snapshots, which may hold a findings.json
        "viewer/x",
    ],
    ids=["kept-round", "tree", "source", "base-tree", "viewer"],
)
def test_find_review_in_a_review_folder_given_as_the_dir_reads_only_the_042_review_nested_in_it(
    findings_path, tmp_path, name
):
    """`--reviews-dir` set to a new-layout folder a 0.42 run used as `--out`: the 0.42 review
    nested in it is found, the folder's own rounds and snapshots never are."""
    folder = tmp_path / "reviews" / "fixture" / "pr-7"
    folder.mkdir(parents=True)
    (folder / "files.txt").write_text("")
    _review(folder, findings_path, name=name)
    assert find_review(_target(findings_path), [folder])[0] == "none"
    nested = _review(folder, findings_path, name="checkout--pr-7")
    assert find_review(_target(findings_path), [folder])[:2] == ("match", nested)
