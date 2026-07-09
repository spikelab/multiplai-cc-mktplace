"""Pydantic models for all structured data flowing through the pipeline."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


# --- Build phases ---

class BuildPhase(str, Enum):
    INIT = "init"
    BOOTSTRAP = "bootstrap"
    INTERVIEW_DONE = "interview_done"
    RESEARCH = "research"
    SPEC_GENERATION = "spec_generation"
    DESIGN_AUDIT = "design_audit"
    REVIEW = "review"
    TDD_BUILD = "tdd_build"
    COMPLETE = "complete"
    FAILED = "failed"


class BlockStatus(str, Enum):
    PENDING = "pending"
    TESTING = "testing"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"


class ArtifactStatus(str, Enum):
    DONE = "done"
    READY = "ready"
    BLOCKED = "blocked"


# --- Quality evaluation ---

class ReviewScore(BaseModel):
    dimension: str
    weight: int = Field(ge=1, le=3)
    score: int = Field(ge=1, le=5)
    evidence: str


class ReviewIssue(BaseModel):
    dimension: str
    severity: str  # Critical, Major, Minor, Note
    description: str
    file_path: str | None = None
    line: int | None = None


class ReviewResult(BaseModel):
    scores: list[ReviewScore]
    issues: list[ReviewIssue] = Field(default_factory=list)

    @property
    def weighted_average(self) -> float:
        if not self.scores:
            return 0.0
        total_weight = sum(s.weight for s in self.scores)
        if total_weight == 0:
            return 0.0
        return sum(s.score * s.weight for s in self.scores) / total_weight

    @property
    def passed(self) -> bool:
        return self.weighted_average >= 3.5 and all(s.score > 1 for s in self.scores)

    @property
    def failing_dimensions(self) -> list[str]:
        return [s.dimension for s in self.scores if s.score == 1]


# --- Gates ---

class GateResult(BaseModel):
    passed: bool
    reason: str
    action: str | None = None  # e.g., "fix_low_scores", "retry_search"
    metadata: dict = Field(default_factory=dict)


# --- Agent results ---

class AgentResult(BaseModel):
    success: bool
    output: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commit_hash: str | None = None
    error: str | None = None
    # True when the underlying agent call failed specifically because it timed
    # out (AgentRunTimeout). agent_call never raises on timeout — it degrades to
    # a failed AgentResult — so this flag is the only signal a real timeout
    # happened. The TDD engine propagates it to block.timed_out → EXIT_AGENT_TIMEOUT.
    timed_out: bool = False
    turns_used: int = 0
    elapsed_seconds: float = 0.0


# --- Block state ---

class BlockInfo(BaseModel):
    """Parsed from tasks.md — represents one implementation block."""
    number: int
    name: str
    description: str
    satisfies: list[str] = Field(default_factory=list)
    status: BlockStatus = BlockStatus.PENDING
    # True when the block failed specifically because an agent LLM call timed
    # out (vs an ordinary build/test failure) — lets the orchestrator return
    # EXIT_AGENT_TIMEOUT only for real timeouts.
    timed_out: bool = False
    test_commit: str | None = None
    impl_commit: str | None = None
    review_scores: ReviewResult | None = None
    review_iterations: int = 0


# --- Change artifacts ---

class ArtifactInfo(BaseModel):
    id: str
    generates: str  # filename or glob
    requires: list[str] = Field(default_factory=list)
    status: ArtifactStatus = ArtifactStatus.BLOCKED


class ChangeStatus(BaseModel):
    change_name: str
    artifacts: list[ArtifactInfo]
    is_complete: bool = False
