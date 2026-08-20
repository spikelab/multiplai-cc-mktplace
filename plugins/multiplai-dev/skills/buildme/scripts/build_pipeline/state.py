"""Build state management with checkpointing and resume.

Hierarchical state: orchestrator → spec generation → TDD → per-block.
Serialized to JSON after every significant transition.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field

from . import budget
from .models import BlockInfo, BlockStatus, BuildPhase

log = logging.getLogger(__name__)


class SpecGenState(BaseModel):
    """State for the spec generator sub-pipeline."""
    completed_artifacts: list[str] = Field(default_factory=list)
    research_path: str | None = None
    codebase_analysis_path: str | None = None
    # The tasks-shape audit runs AFTER tasks.md is written, so its completion
    # can't be inferred from file existence (a crash mid-audit leaves the
    # artifact DONE and the DAG loop never re-enters it). Recorded here so a
    # resume re-runs the audit; old checkpoints default to False (idempotent
    # re-audit, safe).
    tasks_audit_done: bool = False
    # Same durability problem for the unknowns/explainer gate: the gate and its
    # single regeneration pass run AFTER unknowns.md is written, so file
    # existence would mark the artifact DONE and skip the gate on resume.
    # Old checkpoints default to False (re-running the gate is idempotent).
    explainers_done: bool = False
    # Same durability argument for the prototype feedback pass: design.md and
    # tasks.md already exist when the prototype notes are folded back in, so
    # file existence proves nothing. Old checkpoints default to False.
    prototype_done: bool = False
    # The design audit's single regeneration pass rewrites design.md and
    # tasks.md, both of which already exist — so, again, file existence proves
    # nothing and the checkpoint is the only record. It also guards the
    # one-pass discipline across the TWO call sites that run the audit
    # (spec_generator and orchestrator): without it a resumed build would
    # regenerate a second time. Old checkpoints default to False (the pass has
    # not run, so running it once is correct).
    design_audit_regen_done: bool = False
    # The audit stage as a whole (audit → optional regeneration → optional
    # re-check) has run to completion. Distinct from the flag above, which only
    # records the regeneration: an audit that finds nothing actionable never
    # regenerates, so without this the second call site would re-run a
    # four-artifact audit purely to rediscover that there is nothing to do.
    # Old checkpoints default to False (the stage has not run).
    design_audit_done: bool = False
    # The PLAN_REVIEW phase's single regeneration pass rewrote tasks.md. Same
    # durability argument as design_audit_regen_done: tasks.md already exists
    # when the plan review runs, so file existence proves nothing and the
    # checkpoint is the only record that the one pass has been spent. Old
    # checkpoints default to False (the pass has not run, so running it once is
    # correct).
    plan_review_regen_done: bool = False
    # The plan-review stage as a whole (review -> optional regeneration ->
    # optional report-only re-check) has run to completion. Distinct from the
    # flag above, which only records the regeneration: a review that finds
    # nothing actionable never regenerates, so without this a resume would
    # re-run a five-artifact review purely to rediscover there is nothing to
    # do. Checked BEFORE the model call. Old checkpoints default to False.
    plan_review_done: bool = False


class TDDState(BaseModel):
    """State for the TDD engine sub-pipeline."""
    blocks: list[BlockInfo] = Field(default_factory=list)
    current_block: int = 0
    baseline_tests_pass: bool = False
    # The whole-change refactor pass runs after the block loop and commits its
    # own result, so nothing on disk records that it happened — a resume would
    # otherwise re-run it (and pay for it) every time. Old checkpoints default
    # to False, which re-runs a pass that is idempotent by construction.
    refactor_all_done: bool = False
    final_review_done: bool = False
    e2e_done: bool = False


class BuildState(BaseModel):
    """Root state for the entire build pipeline."""

    # Identity
    change_name: str
    mode: str  # scratch | brief | only
    tier: str  # advanced | standard

    # Phase tracking
    phase: BuildPhase = BuildPhase.INIT

    # Orchestrator state
    bootstrap_done: bool = False
    interview_summary: str | None = None
    research_path: str | None = None

    # Git lifecycle — set when the pipeline creates its own worktree/branch.
    # Persisted so a resume RE-BINDS to the existing worktree instead of
    # creating a second one. All four default to None, so a .build-state.json
    # written before the git lifecycle existed still loads.
    worktree_path: str | None = None
    branch: str | None = None
    source_repo: str | None = None
    pr_url: str | None = None

    # Documentation files the DOCS_UPDATE phase reported writing (empty when it
    # updated nothing). Persisted because the PR body is written later — after
    # the archive move, and possibly in a *resumed* process — so an in-memory
    # value would silently drop the "docs updated" line on any resume. Old
    # checkpoints default to [].
    docs_impact: list[str] = Field(default_factory=list)

    # Sub-pipeline state
    spec_gen: SpecGenState | None = None
    tdd: TDDState | None = None

    # Cumulative spend, snapshotted on every checkpoint. A resumed build
    # restores it (see run_tdd_engine) — otherwise resume would hand a runaway
    # build a fresh budget and the ceiling would never bind.
    budget: dict = Field(default_factory=dict)

    # Checkpointing
    state_file: str = ""
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def checkpoint(self, path: Path | None = None) -> None:
        """Serialize state to disk, atomically.

        The checkpoint is the build's only crash-recovery record, and it is
        rewritten after every block phase — so a truncated write is a real
        failure mode, and one there is no recovery from: `BuildState.load`
        raises on a half-written file and the orchestrator has nothing else to
        resume from. Write to a temp file in the SAME directory (so `os.replace`
        stays within one filesystem and is therefore atomic) and rename over the
        target. A crash then leaves either the previous checkpoint or the new
        one, never a partial one; a serialization failure leaves the previous
        checkpoint intact and no stray temp file.
        """
        target = path or Path(self.state_file)
        if not target.name:
            return
        self.budget = budget.get_budget().to_state()
        self.updated_at = datetime.now(timezone.utc).isoformat()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(self.model_dump_json(indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        log.debug("State checkpointed to %s", target)

    @classmethod
    def load(cls, path: Path) -> BuildState:
        """Load state from a JSON file."""
        data = json.loads(path.read_text())
        state = cls.model_validate(data)
        log.info("Resumed state from %s (phase=%s)", path, state.phase)
        return state

    def advance_to(self, phase: BuildPhase, path: Path | None = None) -> None:
        """Advance to a new phase and checkpoint."""
        self.phase = phase
        self.checkpoint(path)
        log.info("Advanced to phase: %s", phase.value)

    def cleanup(self, path: Path | None = None) -> None:
        """Delete state file on successful completion."""
        target = path or Path(self.state_file)
        if target.exists():
            target.unlink()
            log.info("State file cleaned up: %s", target)

    def is_phase_complete(self, phase: BuildPhase) -> bool:
        """Check if a phase has already been completed (for resume).

        FAILED is not a completion state — if the build failed, no phases
        count as complete so the pipeline retries from the beginning.
        """
        if self.phase == BuildPhase.FAILED:
            return False
        phase_order = list(BuildPhase)
        return phase_order.index(self.phase) > phase_order.index(phase)

    # --- TDD helpers ---

    def current_block(self) -> BlockInfo | None:
        if self.tdd and self.tdd.current_block < len(self.tdd.blocks):
            return self.tdd.blocks[self.tdd.current_block]
        return None

    def advance_block(self, path: Path | None = None) -> None:
        if self.tdd:
            self.tdd.current_block += 1
            self.checkpoint(path)

    def mark_block_status(self, block_idx: int, status: BlockStatus, path: Path | None = None) -> None:
        if self.tdd and block_idx < len(self.tdd.blocks):
            self.tdd.blocks[block_idx].status = status
            self.checkpoint(path)

    def all_blocks_done(self) -> bool:
        if not self.tdd:
            return False
        return all(b.status == BlockStatus.DONE for b in self.tdd.blocks)
