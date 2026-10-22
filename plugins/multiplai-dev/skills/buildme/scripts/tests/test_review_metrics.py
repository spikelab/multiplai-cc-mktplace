"""`review_metrics.py` reads the round records `review_rounds.record_round`
writes and tells, for each finding accepted in round 2 or later, whether its
line was already there at the first review or was written by a fix.

The records here are written by the real `record_round`, into a real git repo,
so a renamed record field breaks these tests rather than the script.
"""
import subprocess
from pathlib import Path

import pytest
import review_metrics
from build_pipeline import review_rounds
from build_pipeline.models import BlockInfo, ReviewFinding, ReviewResult


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _record(change_dir: Path, block: int, head_sha: str | None, passed: bool,
            accepted: list[ReviewFinding], rejected: int = 0,
            kind: str = "review", reviewed: bool = True) -> Path:
    """A round record whose reviewer read *head_sha*'s tree, or (with *reviewed*
    false) a tree with uncommitted changes on top of it."""
    review = ReviewResult(
        scores=[], findings=accepted,
        rejected_findings=[ReviewFinding(claim=f"wrong {i}", evidence="REJECTED: not so")
                           for i in range(rejected)],
    )
    path = review_rounds.record_round(
        change_dir, BlockInfo(number=block, name=f"B{block}", description="d"),
        head_sha=head_sha, reviewed_sha=head_sha if reviewed else None,
        reviewed_sha_unknown=None if reviewed else "uncommitted changes",
        review=review, gate_passed=passed,
        gate_reason="ok" if passed else "low score", weighted_average=4.0, kind=kind)
    assert path is not None
    return path


@pytest.fixture
def project(tmp_path):
    """A project with three blocks' records under specs/changes/demo/reviews/.

    Block 1 passes at round 1. Block 2 fails rounds 1 and 2 and passes at
    round 3; its round-2 findings sit on a line from the first commit and on a
    line a fix commit added. Block 3's round-2 record has `head_sha: null`."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 6)))
    first = _commit(repo, "block 2 implementation")
    with (repo / "app.py").open("a") as fh:
        fh.write("added by the fix\n")
    fixed = _commit(repo, "fix(block-2): review round 1")

    change = repo / "specs" / "changes" / "demo"
    _record(change, 1, first, True, [ReviewFinding(claim="nit", file_path="app.py", line=1)],
            rejected=2)
    _record(change, 2, first, False,
            [ReviewFinding(claim="a", file_path="app.py", line=2),
             ReviewFinding(claim="b", file_path="app.py", line=3)], rejected=1)
    _record(change, 2, fixed, False,
            [ReviewFinding(claim="old line", file_path="app.py", line=2),
             ReviewFinding(claim="new line", file_path="app.py", line=6)], rejected=3)
    _record(change, 2, fixed, True, [])
    _record(change, 3, first, False, [ReviewFinding(claim="c", file_path="app.py", line=4)])
    _record(change, 3, None, True,
            [ReviewFinding(claim="null head", file_path="app.py", line=4),
             ReviewFinding(claim="no place")])
    return repo


def _block(changes, number):
    (change,) = changes
    return next(b for b in change.blocks if b.block == number)


def test_rounds_and_passed_at(project):
    changes, warnings = review_metrics.measure([project / "specs"])
    assert warnings == []
    (change,) = changes
    assert change.project_dir == project
    assert change.record_count == 6
    assert [(b.block, b.rounds, b.passed_at) for b in change.blocks] == [
        (1, 1, 1), (2, 3, 3), (3, 2, 2)]


def test_round_two_findings_are_traced_to_the_first_review_or_a_fix(project):
    changes, _ = review_metrics.measure([project / "specs"])
    assert _block(changes, 1).origins == {}
    assert dict(_block(changes, 2).origins) == {
        review_metrics.PRESENT: 1, review_metrics.BY_FIX: 1}


def test_a_null_head_sha_is_unknown(project):
    changes, _ = review_metrics.measure([project / "specs"])
    assert dict(_block(changes, 3).origins) == {
        review_metrics.UNKNOWN: 1, review_metrics.NO_LINE: 1}


def test_table_rows_and_totals(project):
    changes, _ = review_metrics.measure([project / "specs"])
    text = review_metrics.render(changes, [project / "specs"])
    assert "6 round records, 3 blocks." in text
    assert "| 1 | 1 | 1 | r1 1/2 | 0 | 0 | 0 | 0 |" in text
    assert "| 2 | 3 | 3 | r1 2/1, r2 2/3, r3 0/0 | 1 | 1 | 0 | 0 |" in text
    assert "| 3 | 2 | 2 | r1 1/0, r2 2/0 | 0 | 0 | 1 | 1 |" in text
    assert ("**Totals** (1 change(s), 3 blocks): mean rounds-to-pass 2.00 "
            "(over 3 blocks that passed; 0 did not pass); "
            "blocks passing at round 1: 1 of 3 (33%); "
            "round-2+ findings present at first review: 1 of 2 (50%) with a known "
            "origin (1 written by a fix, 1 unknown, 1 with no line); "
            "in `review` records only: 1 of 2 (50%) (1 written by a fix, 1 unknown, "
            "1 with no line).") in text


def test_fix_on_pass_reviews_are_listed_but_not_counted(project):
    change = project / "specs" / "changes" / "demo"
    head = _git(project, "rev-parse", "HEAD")
    _record(change, 1, head, False, [ReviewFinding(claim="d", file_path="app.py", line=6)],
            kind="fix_on_pass_review")
    changes, _ = review_metrics.measure([project / "specs"])
    block = _block(changes, 1)
    assert (block.rounds, block.passed_at) == (1, 1)
    assert dict(block.origins) == {review_metrics.BY_FIX: 1}
    assert block.review_origins == {}
    assert "r2† 1/0" in review_metrics.render(changes, [project / "specs"])


def test_a_block_that_never_passed(tmp_path):
    change = tmp_path / "specs" / "archive" / "2026-10-20-x"
    _record(change, 1, None, False, [])
    changes, _ = review_metrics.measure([tmp_path])
    assert _block(changes, 1).passed_at is None
    text = review_metrics.render(changes, [tmp_path])
    assert "| 1 | 1 | none |" in text
    assert "mean rounds-to-pass n/a (over 0 blocks that passed; 1 did not pass)" in text


def test_unreadable_record_is_skipped_with_a_warning(project):
    bad = project / "specs" / "changes" / "demo" / "reviews" / "block-04-round-1.json"
    bad.write_text("{not json")
    changes, warnings = review_metrics.measure([project / "specs"])
    assert len(warnings) == 1 and str(bad) in warnings[0]
    assert [b.block for b in changes[0].blocks] == [1, 2, 3]


def test_overlapping_directories_count_each_record_once(project):
    changes, _ = review_metrics.measure([project, project / "specs"])
    assert changes[0].record_count == 6


def test_writes_nothing(project):
    before = _git(project, "status", "--porcelain", "--untracked-files=all")
    assert review_metrics.main([str(project / "specs")]) == 0
    assert _git(project, "status", "--porcelain", "--untracked-files=all") == before


def test_no_records(tmp_path, capsys):
    assert review_metrics.main([str(tmp_path)]) == 0
    assert "No review round records found." in capsys.readouterr().out


def test_missing_directory_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        review_metrics.main([str(tmp_path / "nope")])
    assert exc.value.code == 2


@pytest.mark.parametrize("which", ["first", "later"])
def test_a_round_whose_reviewer_read_uncommitted_code_gives_unknown(project, which):
    import json
    change = project / "specs" / "changes" / "demo"
    first = _git(project, "rev-list", "--max-parents=0", "HEAD")
    head = _git(project, "rev-parse", "HEAD")
    findings = [ReviewFinding(claim="old line", file_path="app.py", line=2),
                ReviewFinding(claim="new line", file_path="app.py", line=6)]
    _record(change, 4, first, False, [], reviewed=which != "first")
    path = _record(change, 4, head, True, findings, reviewed=which != "later")
    assert json.loads(path.read_text())["head_sha"] == head
    changes, _ = review_metrics.measure([project / "specs"])
    assert dict(_block(changes, 4).origins) == {review_metrics.UNKNOWN: 2}


def test_a_record_written_before_the_reviewed_commit_was_checked_gives_unknown(project):
    import json
    change = project / "specs" / "changes" / "demo"
    first = _git(project, "rev-list", "--max-parents=0", "HEAD")
    head = _git(project, "rev-parse", "HEAD")
    for path in (_record(change, 4, first, False, []),
                 _record(change, 4, head, True, [ReviewFinding(claim="old", file_path="app.py", line=2)])):
        data = json.loads(path.read_text())
        del data["reviewed_sha"]
        path.write_text(json.dumps(data))
    changes, warnings = review_metrics.measure([project / "specs"])
    assert warnings == []
    assert dict(_block(changes, 4).origins) == {review_metrics.UNKNOWN: 1}
