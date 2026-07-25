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
    ClaimVerdict,
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
    # Per-claim verdicts issued by the VERIFY node after the verification
    # read — rendered as a binding table in the synthesis prompt.
    verdicts: list[ClaimVerdict] = Field(default_factory=list)
    # Set when a reassess-cycle leg raised — surfaced to synthesis so the
    # report never silently pretends refinement/verification happened.
    refinement_error: str = ""
    verification_error: str = ""
    # Overall robustness score of the adversarial review, persisted so a
    # resumed run can re-emit the CHALLENGE: line for the dispatcher.
    challenge_overall: float | None = None
    total_fetches: int = 0  # cumulative count across READ + link follows
    tavily_fallback_count: int = 0  # Tavily content fallbacks used (max 10 per run)
    # Normalized queries already dispatched to the search router across every
    # cycle (initial + coverage recovery + refinement + verification). REASSESS
    # mints refinement/verify queries fresh each cycle, independent of the
    # diverge queries and of each other, so without this the same query re-fires
    # as a full WebSearch subprocess and usually returns already-known URLs — the
    # fetch is saved by the URL dedup, but the search itself is fully re-paid.
    # Persisted so a resumed run keeps the dedup.
    executed_queries: list[str] = Field(default_factory=list)

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
    # Search-query dedup (across cycles)
    # ------------------------------------------------------------------

    def select_unseen_queries(self, queries: list[str]) -> list[str]:
        """Return the queries not yet dispatched this run, and record them.

        Normalizes for comparison (whitespace-collapsed, lowercased) so
        trivially different phrasings of the same query don't both fire, and
        de-duplicates within the batch. Returns the ORIGINAL strings of the
        newly-seen queries, in order. Mutates ``executed_queries`` with the new
        normalized forms — call once per batch, immediately before dispatching
        to the router. Recording the initial-search queries (even though the
        first batch filters to a no-op) is what lets later reassess cycles skip
        re-searching them.
        """
        seen = set(self.executed_queries)
        fresh: list[str] = []
        for q in queries:
            norm = " ".join(q.split()).lower()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            self.executed_queries.append(norm)
            fresh.append(q)
        return fresh

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
                # Findings carry the signal; content is kept only as a debug
                # excerpt. Full content would bloat the checkpoint (rewritten
                # after every source) to tens of MB on a thorough run.
                source.extracted_content = content[:2000]
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
