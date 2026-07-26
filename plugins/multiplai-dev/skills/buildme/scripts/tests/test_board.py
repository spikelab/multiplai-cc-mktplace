"""Tests for the board seam (work item 5 / Done-means criterion 15).

Three things are proved here:

1. `column_for` covers **every** `(phase, block_status)` pair — including the
   columns the pipeline never drives, so the mapping stays honest if someone
   later wires them.
2. A completed run leaves `.board.json` whose `history` ends at `In Review`
   with a populated `pr_url`, `branch` and `worktree_path`.
3. Stdout carries at least one `BOARD:` line per column entered, plus a `PR:`
   line.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline import board, git_ops
from build_pipeline.board import (
    BoardCard,
    board_path,
    card_id_for,
    column_for,
    owner_for,
    record,
    record_failure,
)
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.git_ops import GitResult
from build_pipeline.models import BlockStatus, BoardColumn, BuildPhase
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState
from tests.test_git_ops import make_bare_origin, make_repo


# --- 1. The pure mapping --------------------------------------------------

# Written out independently of board.py's own table: if the mapping changes,
# this has to change with it deliberately.
EXPECTED_COLUMNS = {
    BuildPhase.INIT: BoardColumn.ACCEPTED,
    BuildPhase.BOOTSTRAP: BoardColumn.SHAPING,
    BuildPhase.INTERVIEW_DONE: BoardColumn.SHAPING,
    BuildPhase.RESEARCH: BoardColumn.SHAPING,
    BuildPhase.SPEC_GENERATION: BoardColumn.SHAPING,
    BuildPhase.DESIGN_AUDIT: BoardColumn.PLANNING,
    BuildPhase.PROTOTYPE: BoardColumn.PLANNING,
    BuildPhase.REVIEW: BoardColumn.PLANNING,
    BuildPhase.TDD_BUILD: BoardColumn.IN_DEVELOPMENT,
    BuildPhase.RESPEC: BoardColumn.IN_DEVELOPMENT,
    BuildPhase.PUBLISH: BoardColumn.IN_DEVELOPMENT,
    BuildPhase.COMPLETE: BoardColumn.IN_REVIEW,
    BuildPhase.FAILED: BoardColumn.CANCELLED,
}

ALL_BLOCK_STATES = [None, *list(BlockStatus)]


class TestColumnFor:
    def test_mapping_is_exhaustive_over_the_phase_enum(self):
        assert set(EXPECTED_COLUMNS) == set(BuildPhase)

    @pytest.mark.parametrize("phase", list(BuildPhase))
    @pytest.mark.parametrize("block_status", ALL_BLOCK_STATES)
    def test_every_phase_and_block_status_case(self, phase, block_status):
        """Criterion 15a — every (phase, block_status) pair, all 7 block
        states (including None) against all 13 phases."""
        assert column_for(phase, block_status) is EXPECTED_COLUMNS[phase]

    def test_block_status_never_moves_the_card_today(self):
        """The parameter exists as the future refinement point and currently
        changes nothing — asserted so a silent change is caught."""
        for phase in BuildPhase:
            columns = {column_for(phase, s) for s in ALL_BLOCK_STATES}
            assert len(columns) == 1

    def test_reviewing_block_is_not_in_review(self):
        """The in-process block review has no pushed branch to fetch — it must
        not be confused with the In Review column."""
        assert column_for(BuildPhase.TDD_BUILD, BlockStatus.REVIEWING) is (
            BoardColumn.IN_DEVELOPMENT
        )

    def test_columns_the_pipeline_never_drives_are_unreachable_from_any_phase(self):
        """Backlog / Testing / Ready for Prod / Deploying / Deployed exist in
        the schema and are reachable from no (phase, block_status) pair."""
        reachable = {
            column_for(p, s) for p in BuildPhase for s in ALL_BLOCK_STATES
        }
        never_driven = {
            BoardColumn.BACKLOG,
            BoardColumn.TESTING,
            BoardColumn.READY_FOR_PROD,
            BoardColumn.DEPLOYING,
            BoardColumn.DEPLOYED,
        }
        assert reachable & never_driven == set()
        # ...but they are in the enum, so .board.json's schema carries them.
        assert never_driven <= set(BoardColumn)

    def test_eleven_columns(self):
        assert len(list(BoardColumn)) == 11

    @pytest.mark.parametrize("column", list(BoardColumn))
    def test_owner_defined_for_every_column(self, column):
        owner = owner_for(column)
        assert owner is None or isinstance(owner, str)

    def test_owner_seats(self):
        assert owner_for(BoardColumn.SHAPING) == "product"
        assert owner_for(BoardColumn.PLANNING) == "eng"
        assert owner_for(BoardColumn.IN_DEVELOPMENT) == "author"
        assert owner_for(BoardColumn.IN_REVIEW) == "reviewer"
        assert owner_for(BoardColumn.BACKLOG) is None


# --- 2. The card file -----------------------------------------------------


def _config(tmp_path: Path, change: str = "card-change") -> BuildConfig:
    config = BuildConfig(
        project_dir=tmp_path, change_name=change,
        git=GitToggles(worktree=False, push=False, pr="none"),
    )
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    cm.create_change(change)
    return config


def _state(config: BuildConfig, **over) -> BuildState:
    base = dict(
        change_name=config.change_name, mode="scratch", tier="advanced",
        state_file=str(config.state_file_path()),
    )
    base.update(over)
    return BuildState(**base)


class TestRecord:
    def test_writes_card_and_prints_board_line(self, tmp_path, capsys):
        config = _config(tmp_path)
        state = _state(config)
        assert record(config, state, BuildPhase.BOOTSTRAP) is BoardColumn.SHAPING

        path = config.change_dir / ".board.json"
        card = BoardCard.model_validate(json.loads(path.read_text()))
        assert card.column is BoardColumn.SHAPING
        assert card.owner_agent == "product"
        assert card.card_id == card_id_for("card-change")
        assert [e.column for e in card.history] == [BoardColumn.SHAPING]
        assert "BOARD:card-change:Shaping" in capsys.readouterr().out

    def test_same_column_twice_is_not_a_second_history_entry(self, tmp_path, capsys):
        config = _config(tmp_path)
        state = _state(config)
        record(config, state, BuildPhase.BOOTSTRAP)
        capsys.readouterr()
        assert record(config, state, BuildPhase.RESEARCH) is None  # still Shaping

        card = BoardCard.model_validate(
            json.loads((config.change_dir / ".board.json").read_text())
        )
        assert [e.column for e in card.history] == [BoardColumn.SHAPING]
        assert "BOARD:" not in capsys.readouterr().out

    def test_history_accumulates_across_columns(self, tmp_path):
        config = _config(tmp_path)
        state = _state(config)
        for phase in (BuildPhase.BOOTSTRAP, BuildPhase.DESIGN_AUDIT, BuildPhase.TDD_BUILD):
            record(config, state, phase)
        card = BoardCard.model_validate(
            json.loads((config.change_dir / ".board.json").read_text())
        )
        assert [e.column for e in card.history] == [
            BoardColumn.SHAPING, BoardColumn.PLANNING, BoardColumn.IN_DEVELOPMENT,
        ]
        assert card.entered_at == card.history[-1].at

    def test_git_identity_comes_from_the_live_state(self, tmp_path):
        """pr_url is only ever in memory at publish time (the --auto archive
        deletes the state file first), so the card must take it from the live
        BuildState, not from .build-state.json."""
        config = _config(tmp_path)
        state = _state(
            config, branch="buildme/card-change",
            worktree_path="/ws/.worktrees/buildme-card-change",
            pr_url="https://github.com/o/r/pull/7",
        )
        assert not config.state_file_path().exists()
        record(config, state, column=BoardColumn.IN_REVIEW)

        card = BoardCard.model_validate(
            json.loads((config.change_dir / ".board.json").read_text())
        )
        assert card.branch == "buildme/card-change"
        assert card.worktree_path == "/ws/.worktrees/buildme-card-change"
        assert card.pr_url == "https://github.com/o/r/pull/7"

    def test_writes_to_the_progress_file_on_a_move(self, tmp_path):
        config = _config(tmp_path)
        state = _state(config)
        progress = ProgressWriter(config.progress_file_path())
        record(config, state, BuildPhase.TDD_BUILD, progress=progress, note="3 blocks")
        text = config.progress_file_path().read_text()
        assert "BOARD → In Development" in text
        assert "owner: author" in text and "3 blocks" in text

    def test_corrupt_card_does_not_break_the_build(self, tmp_path):
        config = _config(tmp_path)
        (config.change_dir / ".board.json").write_text("{not json")
        state = _state(config)
        assert record(config, state, BuildPhase.TDD_BUILD) is BoardColumn.IN_DEVELOPMENT


class TestBoardPath:
    def test_follows_the_change_into_the_archive(self, tmp_path):
        """--auto archives before PUBLISH, so the In Review write must land on
        the archived card instead of resurrecting changes/<name>/."""
        config = _config(tmp_path, "archived-change")
        state = _state(config)
        record(config, state, BuildPhase.TDD_BUILD)
        change_dir = config.change_dir

        dest = ChangeManager(config.specs_dir).archive_change(change_dir)
        assert not change_dir.exists()

        assert board_path(config.specs_dir, "archived-change") == dest / ".board.json"
        record(config, state, column=BoardColumn.IN_REVIEW)
        assert not change_dir.exists(), "must not recreate the active change dir"
        card = BoardCard.model_validate(json.loads((dest / ".board.json").read_text()))
        assert card.history[-1].column is BoardColumn.IN_REVIEW


class TestRecordFailure:
    def test_resumable_failure_keeps_the_card_where_it_is(self, tmp_path, capsys):
        config = _config(tmp_path)
        state = _state(config)
        record(config, state, BuildPhase.TDD_BUILD)
        state.checkpoint()  # a resumable checkpoint survives
        capsys.readouterr()

        assert record_failure(config, state, "build failed") is None
        card = BoardCard.model_validate(
            json.loads((config.change_dir / ".board.json").read_text())
        )
        assert card.column is BoardColumn.IN_DEVELOPMENT
        assert "BOARD:" not in capsys.readouterr().out

    def test_unrecoverable_failure_records_cancelled(self, tmp_path, capsys):
        config = _config(tmp_path)
        state = _state(config)
        record(config, state, BuildPhase.TDD_BUILD)
        assert not config.state_file_path().exists()
        capsys.readouterr()

        assert record_failure(config, state, "no checkpoint left") is BoardColumn.CANCELLED
        card = BoardCard.model_validate(
            json.loads((config.change_dir / ".board.json").read_text())
        )
        assert card.column is BoardColumn.CANCELLED
        assert card.history[-1].note == "no checkpoint left"
        assert "BOARD:card-change:Cancelled" in capsys.readouterr().out


# --- 3. A whole run -------------------------------------------------------


def _args(project_dir: Path, change: str, **over) -> argparse.Namespace:
    base = dict(
        mode="scratch", change=change, project_dir=str(project_dir), auto=True,
        spec_only=False, skip_research=True, interview_summary="an idea",
        research_path="", context_files=[], session_id="", block=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestFullRunBoardTrail:
    """Criterion 15b/15c — a completed run's card ends at In Review with the
    git identity populated, and stdout carries a BOARD: line per column
    entered plus a PR: line."""

    @pytest.mark.asyncio
    async def test_completed_run_ends_in_review(self, tmp_path, monkeypatch, capsys):
        from build_pipeline.orchestrator import run_orchestrator

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")

        config = BuildConfig(project_dir=repo, change_name="boarded", auto=True)
        config.specs_dir = repo / "specs"

        pr_url = "https://github.com/acme/proj/pull/42"
        monkeypatch.setattr(
            git_ops, "_run_gh",
            lambda argv, cwd=None, timeout=None: GitResult(argv, 0, pr_url, ""),
        )

        with patch("build_pipeline.spec_generator.run_spec_generator",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   new_callable=AsyncMock, return_value=None):
            rc = await run_orchestrator(config, _args(repo, "boarded"))

        assert rc == 0
        out = capsys.readouterr().out

        # 15c — one BOARD: line per column entered, plus the PR: line
        board_lines = [ln for ln in out.splitlines() if ln.startswith("BOARD:")]
        assert board_lines == [
            "BOARD:boarded:Shaping",
            "BOARD:boarded:Planning",
            "BOARD:boarded:In Development",
            "BOARD:boarded:In Review",
        ]
        assert f"PR:{pr_url}" in out

        # 15b — the card, which the --auto archive carried into archive/
        archived = list((config.specs_dir / "archive").glob("*-boarded"))
        assert len(archived) == 1
        card = BoardCard.model_validate(
            json.loads((archived[0] / ".board.json").read_text())
        )
        assert [e.column for e in card.history] == [
            BoardColumn.SHAPING,
            BoardColumn.PLANNING,
            BoardColumn.IN_DEVELOPMENT,
            BoardColumn.IN_REVIEW,
        ]
        assert card.column is BoardColumn.IN_REVIEW
        assert card.owner_agent == "reviewer"
        assert card.pr_url == pr_url
        assert card.branch == "buildme/boarded"
        assert card.worktree_path == str(tmp_path / "ws" / ".worktrees" / "buildme-boarded")
        # The change dir the card lives in is the archived one — the write
        # never resurrected specs/changes/boarded/.
        assert not (config.specs_dir / "changes" / "boarded").exists()

    @pytest.mark.asyncio
    async def test_run_without_a_pr_stays_in_development(self, tmp_path, monkeypatch, capsys):
        """In Review is entered only when the branch is pushed AND a PR is
        open. Reaching COMPLETE with no PR is not a reviewable surface."""
        from build_pipeline.orchestrator import run_orchestrator

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")

        config = BuildConfig(
            project_dir=repo, change_name="nopr", auto=False,
            git=GitToggles(worktree=False, push=False, pr="none"),
        )
        config.specs_dir = repo / "specs"

        with patch("build_pipeline.spec_generator.run_spec_generator",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   new_callable=AsyncMock, return_value=None):
            rc = await run_orchestrator(config, _args(repo, "nopr", auto=False))

        assert rc == 0
        out = capsys.readouterr().out
        assert "BOARD:nopr:In Review" not in out
        card = BoardCard.model_validate(
            json.loads((config.change_dir / ".board.json").read_text())
        )
        assert card.column is BoardColumn.IN_DEVELOPMENT
        assert card.pr_url is None
