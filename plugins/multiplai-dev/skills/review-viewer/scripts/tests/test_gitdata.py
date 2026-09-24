from __future__ import annotations

import pytest

from fixture_repo import SERVICE_HEAD
from review_viewer.gitdata import (
    DELETED_PREVIEW_LINES, PathNotInReview, allowed_paths, diff_target, file_view,
    parse_unified,
)
from review_viewer.models import load_findings


@pytest.fixture
def review(findings_path):
    ff = load_findings(findings_path)
    return ff.target, ff


def test_parse_unified_rows():
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
            "@@ -10,2 +10,3 @@\n j\n+k\n l\n\\ No newline at end of file\n")
    rows = parse_unified(diff)
    kinds = [r.k for r in rows]
    assert kinds == ["ctx", "del", "add", "ctx", "gap", "ctx", "add", "ctx"]
    assert (rows[1].o, rows[1].n) == (2, None)
    assert (rows[2].o, rows[2].n) == (None, 2)
    assert rows[4].t.startswith("@@ -10,2")
    assert (rows[6].n, rows[6].t) == (11, "k")


def test_modified_file_is_full_with_marks(review):
    target, ff = review
    view = file_view(target, "app/service.py", ff)
    assert not (view.truncated or view.deleted or view.binary)
    assert view.language == "python"
    head_lines = SERVICE_HEAD.splitlines()
    shown = [r for r in view.rows if r.k in ("ctx", "add")]
    assert [r.t for r in shown] == head_lines
    added = {r.n for r in view.rows if r.k == "add"}
    assert {3, 5, 9, 13, 14, 20} <= added
    deleted = [r for r in view.rows if r.k == "del"]
    assert [d.t for d in deleted] == ['    return sum(i["price"] for i in items)']
    # The deleted line sits right before its replacement.
    idx = view.rows.index(deleted[0])
    assert view.rows[idx + 1].n == 9
    assert (9, 9) in view.cited_ranges and (12, 15) in view.cited_ranges


def test_added_file_is_all_added(review):
    target, ff = review
    view = file_view(target, "app/new_feature.py", ff)
    assert {r.k for r in view.rows} == {"add"}
    assert len(view.rows) == 5


def test_deleted_file_gets_a_preview(review):
    target, ff = review
    view = file_view(target, "app/old_module.py", ff)
    assert view.deleted and view.truncated
    assert len(view.rows) == DELETED_PREVIEW_LINES
    assert all(r.k == "del" for r in view.rows)
    assert view.rows[0].t == "LEGACY_1 = 1"


def test_binary_file_has_no_rows(review):
    target, ff = review
    view = file_view(target, "assets/logo.bin", ff)
    assert view.binary and view.rows == []


def test_large_file_is_truncated_to_hunks(review):
    target, ff = review
    view = file_view(target, "app/big.py", ff)
    assert view.truncated
    assert len(view.rows) < 100
    assert any(r.k == "add" and r.t == "value_2000 = 'changed'" for r in view.rows)
    assert any(r.k == "del" and r.t == "value_2000 = 2000" for r in view.rows)
    assert view.rows[0].k == "gap" or view.rows[0].n > 1


def test_path_outside_review_is_refused(review):
    target, ff = review
    assert "setup.py" not in allowed_paths(target, ff)
    with pytest.raises(PathNotInReview):
        file_view(target, "setup.py", ff)
    with pytest.raises(PathNotInReview):
        file_view(target, "../../etc/passwd", ff)


def test_diff_target_resolves_range(fixture_repo):
    repo, base, head = fixture_repo
    target = diff_target(repo, "main~1..main")
    assert (target.base_sha, target.head_sha) == (base, head)
    assert target.slug == f"fixture-repo--{base[:8]}..{head[:8]}"
    assert sorted(target.files_changed) == [
        "app/big.py", "app/new_feature.py", "app/old_module.py", "app/service.py",
        "assets/logo.bin"]


def test_missing_git_names_git(monkeypatch, fixture_repo):
    from review_viewer import gitdata
    monkeypatch.setenv("PATH", "/nonexistent")
    repo, _, _ = fixture_repo
    with pytest.raises(gitdata.GitError, match="install git"):
        diff_target(repo, "main~1..main")
