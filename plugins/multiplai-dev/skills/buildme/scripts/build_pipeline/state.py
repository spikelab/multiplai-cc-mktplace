"""Build state management with checkpointing and resume.

Hierarchical state: orchestrator → spec generation → TDD → per-block.
Serialized to JSON after every significant transition.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field

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


class TDDState(BaseModel):
    """State for the TDD engine sub-pipeline."""
    blocks: list[BlockInfo] = Field(default_factory=list)
    current_block: int = 0
    baseline_tests_pass: bool = False
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

    # Sub-pipeline state
    spec_gen: SpecGenState | None = None
    tdd: TDDState | None = None

    # Checkpointing
    state_file: str = ""
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def checkpoint(self, path: Path | None = None) -> None:
        """Serialize state to disk."""
        target = path or Path(self.state_file)
        if not target.name:
            return
        self.updated_at = datetime.now(timezone.utc).isoformat()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2))
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
