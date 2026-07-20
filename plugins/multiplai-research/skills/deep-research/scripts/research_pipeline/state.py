"""ResearchState: the pipeline's persistent state.

Serialized to JSON after every stage transition, enabling crash recovery and
fine-grained resume (per-source, not per-phase).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

from pydantic import BaseModel, Field

from .models import (
    Finding,
    PlanResult,
    ReassessResult,
    SearchResult,
    Source,
    SourceStatus,
)


class Stage(str, Enum):
    """Pipeline stages. State advances through these in order."""

    INIT = "init"
    PLAN_COMPLETE = "plan_complete"
    DIVERGE_COMPLETE = "diverge_complete"
    CHALLENGE_COMPLETE = "challenge_complete"
    DIVERSITY_GATE_PASSED = "diversity_gate_passed"
    SEARCH_COMPLETE = "search_complete"
    TRIAGE_COMPLETE = "triage_complete"
    MIN_SOURCES_GATE_PASSED = "min_sources_gate_passed"
    READ_IN_PROGRESS = "read_in_progress"
    READ_COMPLETE = "read_complete"
    COVERAGE_GATE_PASSED = "coverage_gate_passed"
    CRITICAL_SOURCE_GATE_PASSED = "critical_source_gate_passed"
    REASSESS_COMPLETE = "reassess_complete"
    REASSESS_GATE_PASSED = "reassess_gate_passed"
    QUALITY_CHECK_PASSED = "quality_check_passed"
    SYNTHESIZE_COMPLETE = "synthesize_complete"
    CHALLENGE_REVIEW_COMPLETE = "challenge_review_complete"
    DONE = "done"


# Ordered list for computing "is stage X complete?"
STAGE_ORDER: list[Stage] = list(Stage)


def stage_index(stage: Stage) -> int:
    return STAGE_ORDER.index(stage)


class ResearchState(BaseModel):
    """Complete pipeline state, persisted to disk after each transition."""

    # Metadata
    query: str
    output_file: str  # absolute path
    state_file: str  # absolute path
    started_at: str  # ISO timestamp
    updated_at: str  # ISO timestamp
    stage: Stage = Stage.INIT

    # Stage outputs
    plan: PlanResult | None = None
    search_results: list[SearchResult] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)  # after triage
    findings: list[Finding] = Field(default_factory=list)
    reassessment: ReassessResult | None = None
    # Set when a reassess-cycle leg raised — surfaced to synthesis so the
    # report never silently pretends refinement/verification happened.
    refinement_error: str = ""
    verification_error: str = ""
    total_fetches: int = 0  # cumulative count across READ + link follows
    tavily_fallback_count: int = 0  # Tavily content fallbacks used (max 10 per run)

    # Parallel mode
    sub_state_files: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction & persistence
    # ------------------------------------------------------------------

    @classmethod
    def new(cls, query: str, output_file: Path, state_file: Path) -> "ResearchState":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            query=query,
            output_file=str(output_file),
            state_file=str(state_file),
            started_at=now,
            updated_at=now,
            stage=Stage.INIT,
        )

    def checkpoint(self) -> None:
        """Serialize state to disk. Called after every stage transition."""
        self.updated_at = datetime.now(timezone.utc).isoformat()
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, state_file: Path) -> "ResearchState":
        """Load state from JSON file."""
        return cls.model_validate_json(state_file.read_text())

    def cleanup(self, *, keep_on_incomplete: bool = False) -> None:
        """Remove state file on successful completion.

        When keep_on_incomplete=True, the state file is preserved so the
        findings can be used for retry or manual synthesis. This prevents
        data loss when the pipeline aborts after expensive fetch+extract.
        """
        if keep_on_incomplete:
            log.info("Keeping state file (incomplete run): %s", self.state_file)
            return
        path = Path(self.state_file)
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------------
    # Stage transitions
    # ------------------------------------------------------------------

    def advance_to(self, stage: Stage) -> None:
        """Move state to a new stage and checkpoint."""
        self.stage = stage
        self.checkpoint()

    def is_complete(self, stage: Stage) -> bool:
        """Is the given stage already complete?"""
        return stage_index(self.stage) >= stage_index(stage)

    # ------------------------------------------------------------------
    # Per-source tracking (for fine-grained resume)
    # ------------------------------------------------------------------

    def pending_sources(self) -> list[Source]:
        """Sources that haven't been fetched/extracted yet."""
        return [s for s in self.sources if s.status == SourceStatus.PENDING]

    def completed_sources(self) -> list[Source]:
        """Sources successfully extracted."""
        return [s for s in self.sources if s.status == SourceStatus.EXTRACTED]

    def failed_sources(self) -> list[Source]:
        """Sources that failed (final — not retried on resume)."""
        return [s for s in self.sources if s.status == SourceStatus.FAILED]

    def mark_source_extracted(
        self, url: str, content: str, findings: list[Finding]
    ) -> None:
        for source in self.sources:
            if source.url == url:
                source.status = SourceStatus.EXTRACTED
                source.extracted_content = content
                break
        self.findings.extend(findings)
        self.checkpoint()

    def mark_source_failed(self, url: str, error: str) -> None:
        for source in self.sources:
            if source.url == url:
                source.status = SourceStatus.FAILED
                source.error = error
                break
        self.checkpoint()
