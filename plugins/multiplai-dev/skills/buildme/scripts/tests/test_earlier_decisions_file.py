"""The `--earlier-decisions` file buildme hands the review of its PR (plan
factory-merged-2026-10-29, item 37): every finding a block review rejected, with
its reason, and every accepted finding still open in its block's latest record.

All data is made up."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from build_pipeline import review_rounds
from build_pipeline.config import EpicSettings
from build_pipeline.epic import review as epic_review
from build_pipeline.findings import loop
from build_pipeline.models import BlockInfo, ReviewFinding, ReviewResult

from .epic_row_helpers import use_workspace


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    return use_workspace(monkeypatch, tmp_path)


def block(n: int) -> BlockInfo:
    return BlockInfo(number=n, name=f"block {n}", description="d")


def rejected_finding(claim: str, *, evidence: str = "") -> ReviewFinding:
    return ReviewFinding(
        claim=claim, severity="Minor", evidence=evidence, file_path="a.py", line=7
    )


def record(
    change_dir: Path,
    n: int,
    *,
    accepted: list[ReviewFinding] = (),
    rejected: list[ReviewFinding] = (),
    carried: list[ReviewFinding] | None = None,
    new_open: list[int] | None = None,
) -> Path:
    path = review_rounds.record_round(
        change_dir,
        block(n),
        head_sha="a" * 40,
        reviewed_sha="a" * 40,
        review=ReviewResult(
            scores=[], findings=list(accepted), rejected_findings=list(rejected)
        ),
        gate_passed=True,
        gate_reason="ok",
        weighted_average=8.0,
        carried_open=carried,
        new_open=new_open,
    )
    assert path is not None
    return path


def test_rejected_with_its_reason_and_open_accepted_go_in(tmp_path):
    change = tmp_path / "change"
    record(
        change,
        1,
        accepted=[ReviewFinding(claim="the login keeps no role", file_path="b.py")],
        rejected=[
            rejected_finding(
                "the name is matched twice",
                evidence="seen in a.py\nREJECTED: the caller checks first",
            )
        ],
    )
    entries = review_rounds.earlier_decisions(change)
    assert entries == [
        {
            "id": "B01-R1-X1",
            "file": "a.py",
            "line_start": 7,
            "line_end": 7,
            "claim": "the name is matched twice",
            "decision": "rejected",
            "reason": "the caller checks first",
        },
        {
            "id": "B01-R1-F1",
            "file": "b.py",
            "line_start": None,
            "line_end": None,
            "claim": "the login keeps no role",
            "decision": "accepted-unfixed",
            "reason": "accepted by the block 1 review and still open after its round 1",
        },
    ]


@pytest.mark.parametrize("kept", [True, False])
def test_a_kept_fix_on_pass_reviews_record_decides_what_is_open(tmp_path, kept):
    """After a kept fix on pass, its `fix_on_pass_review` record holds what is
    open: the finding it fixed is gone, and the review's new open and
    rejected findings go in. A fix not kept was rolled back, so its review
    record says nothing about the code."""
    change = tmp_path / "change"
    first = record(change, 1, accepted=[ReviewFinding(claim="fixed on pass")])
    review_rounds.update_round(first, fix={"kept": kept})
    review_rounds.record_round(
        change,
        block(1),
        head_sha="b" * 40,
        reviewed_sha="b" * 40,
        review=ReviewResult(
            scores=[],
            findings=[ReviewFinding(claim="raised after the fix")],
            rejected_findings=[rejected_finding("not a defect")],
        ),
        gate_passed=True,
        gate_reason="ok",
        weighted_average=8.0,
        kind="fix_on_pass_review",
        carried_open=[],
        new_open=[0],
    )
    got = [(e["id"], e["claim"]) for e in review_rounds.earlier_decisions(change)]
    if kept:
        assert got == [
            ("B01-R2-X1", "not a defect"),
            ("B01-R2-F1", "raised after the fix"),
        ]
    else:
        assert got == [("B01-R1-F1", "fixed on pass")]


def test_the_component_review_hears_what_a_kept_fix_on_pass_left_open(tmp_path):
    from build_pipeline.tdd_engine import _left_by_block_reviews

    change = tmp_path / "change"
    first = record(change, 1, accepted=[ReviewFinding(claim="fixed on pass")])
    review_rounds.update_round(first, fix={"kept": True})
    review_rounds.record_round(
        change,
        block(1),
        head_sha="b" * 40,
        review=ReviewResult(
            scores=[], findings=[ReviewFinding(claim="raised after the fix")]
        ),
        gate_passed=True,
        gate_reason="ok",
        weighted_average=8.0,
        kind="fix_on_pass_review",
        carried_open=[],
        new_open=[0],
    )
    config = SimpleNamespace(change_dir=change, block_gate_severities=["Critical"])
    state = SimpleNamespace(tdd=SimpleNamespace(blocks=[block(1)]))
    assert _left_by_block_reviews(config, state) == (
        "- B01-R2-F1 [Minor] raised after the fix"
    )


def test_a_finding_closed_in_a_later_round_is_left_out(tmp_path):
    change = tmp_path / "change"
    record(change, 2, accepted=[ReviewFinding(claim="fixed later")])
    record(change, 2, carried=[], new_open=[])  # round 2 leaves nothing open
    assert review_rounds.earlier_decisions(change) == []


def test_no_entry_passes_no_option(tmp_path, data_dir):
    change = tmp_path / "change"
    change.mkdir()
    assert review_rounds.earlier_decisions_argv(change) == []
    assert review_rounds.earlier_decisions_argv(None) == []
    assert not (data_dir / "buildme" / "earlier-decisions").exists()


def test_the_file_is_written_outside_the_repo_and_passed(tmp_path, data_dir):
    change = tmp_path / "change"
    record(change, 1, rejected=[rejected_finding("x", evidence="REJECTED: no")])
    argv = review_rounds.earlier_decisions_argv(change)
    assert argv[0] == "--earlier-decisions"
    path = Path(argv[1])
    assert path.name == review_rounds.EARLIER_DECISIONS_FILE
    assert path.is_relative_to(data_dir / "buildme" / "earlier-decisions")
    assert json.loads(path.read_text()) == review_rounds.earlier_decisions(change)
    assert not path.with_name(path.name + ".tmp").exists()


def test_both_review_commands_pass_it(tmp_path, data_dir, monkeypatch):
    change = tmp_path / "change"
    record(change, 1, rejected=[rejected_finding("x", evidence="REJECTED: no")])
    monkeypatch.setattr(
        loop, "review_options", lambda config, exclude=True, verify_low="": []
    )
    expected = review_rounds.earlier_decisions_argv(change)

    run = SimpleNamespace(
        project_dir=tmp_path, pr=3, trust_repo=True, config=None, change_dir=change
    )
    assert loop.review_argv(run)[-2:] == expected
    assert loop.resume_argv(run, "/t")[-2:] == expected
    config = SimpleNamespace(epic=EpicSettings())
    assert epic_review.review_argv(tmp_path, 3, config, change)[-2:] == expected
    assert epic_review.resume_argv("/t", config, change)[-2:] == expected
    assert "--earlier-decisions" not in epic_review.review_argv(tmp_path, 3, config)


def test_the_epic_finds_the_childs_change_dir(tmp_path):
    change = tmp_path / "specs" / "changes" / "epic" / "components" / "child"
    change.mkdir(parents=True)
    found = epic_review.child_change_dir(tmp_path, "epic/child")
    assert found is not None and found.resolve() == change.resolve()
    assert epic_review.child_change_dir(tmp_path / "nowhere", "epic/child") is None
