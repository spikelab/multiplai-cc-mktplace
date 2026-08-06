"""ResearchState: the pipeline's persistent state.

Serialized to JSON after every stage transition, enabling crash recovery and
fine-grained resume (per-source, not per-phase).
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import tempfile
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


def _replacement_mode(path: Path) -> int:
    """Mode a temp file should carry before it replaces *path*.

    ``mkstemp`` creates 0600, and ``os.replace`` carries the temp file's mode
    onto the destination — so without this, the first checkpoint silently turns
    a user-readable state file owner-only. Keep the existing file's mode when
    there is one; otherwise use what a plain ``open()`` would have produced.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


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
    # Recorded at dispatch time, so a batch whose search then fails still counts
    # as executed — the failed cycle is surfaced via refinement_error /
    # verification_error, not retried under a re-minted duplicate query.
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
        """Serialize state to disk. Called after every stage transition.

        Write-then-rename, not write-in-place: this runs after *every source*
        during READ, so a crash mid-write is a realistic way to lose a run —
        and it loses it badly, leaving truncated JSON that makes `load()` raise
        on resume, i.e. the checkpoint destroys the very thing it exists to
        protect. The temp file is created in the same directory so `os.replace`
        is atomic (a cross-filesystem rename is not).
        """
        self.updated_at = datetime.now(timezone.utc).isoformat()
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                # Rename is only atomic with respect to *ordering*; without
                # this the data may still be in the page cache when the rename
                # commits, so a machine crash can leave the new name pointing
                # at a zero-length file — the exact loss this method exists to
                # prevent. Once per source, not per byte.
                f.flush()
                os.fsync(f.fileno())
            # mkstemp creates 0600. Inheriting that would silently make a
            # user-visible artifact owner-only on the first checkpoint, so
            # carry the existing file's mode when there is one.
            with contextlib.suppress(OSError):
                os.chmod(tmp_name, _replacement_mode(path))
            os.replace(tmp_name, path)
            # Durability of the rename itself lives in the directory entry.
            with contextlib.suppress(OSError):
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

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
