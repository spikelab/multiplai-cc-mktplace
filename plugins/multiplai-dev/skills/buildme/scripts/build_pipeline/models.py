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
    # Strengths-first review: what the diff genuinely does well.
    strengths: list[str] = Field(default_factory=list)
    # Spec-compliance verdict (two-verdict review, ported from the superpowers
    # task reviewer): spec behavior absent from the diff, implementation beyond
    # the spec, and implementation that got a scenario's meaning wrong.
    missing: list[str] = Field(default_factory=list)
    extra: list[str] = Field(default_factory=list)
    misunderstood: list[str] = Field(default_factory=list)

    @property
    def weighted_average(self) -> float:
        if not self.scores:
            return 0.0
        total_weight = sum(s.weight for s in self.scores)
        if total_weight == 0:
            return 0.0
        return sum(s.score * s.weight for s in self.scores) / total_weight

    @property
    def spec_compliant(self) -> bool:
        """Clean-or-minor spec verdict. Missing or misunderstood spec behavior
        means the block cannot be trusted; `extra` alone is scope creep the
        dimension scores already price in."""
        return not self.missing and not self.misunderstood

    @property
    def passed(self) -> bool:
        """Both verdicts must hold: spec compliance AND the score threshold."""
        return (
            self.spec_compliant
            and self.weighted_average >= 3.5
            and all(s.score > 1 for s in self.scores)
        )

    @property
    def failing_dimensions(self) -> list[str]:
        return [s.dimension for s in self.scores if s.score == 1]


class WeakTestFinding(BaseModel):
    """One weak test flagged by the LLM test-quality auditor."""
    file: str = ""
    test_name: str = ""
    pattern: str = ""
    suggestion: str = ""


class TestQualityAudit(BaseModel):
    """Structured verdict from the LLM test-quality auditor (TEST_QUALITY_PROMPT).

    Adjudicates the static weak-pattern scan: the regex scan is cheap but
    coarse, so its failures are confirmed or overturned by this audit before
    the pipeline fails a block over test quality.
    """
    passed: bool
    weak_tests: list[WeakTestFinding] = Field(default_factory=list)
    total_tests: int = 0
    weak_count: int = 0

    def findings_text(self) -> str:
        return "\n".join(
            f"- {w.file}::{w.test_name}: {w.pattern} — {w.suggestion}"
            for w in self.weak_tests
        )


class FinalReviewVerdict(BaseModel):
    """Structured verdict for the final comprehensive review — replaces the
    old string-match on 'PASSED' in free text."""
    passed: bool
    summary: str = ""
    issues: list[str] = Field(default_factory=list)


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
    # Cross-block interface contract parsed from the block's `Interfaces:`
    # section: exact signatures this block creates (produces) and the earlier-
    # block signatures it calls (consumes). Threaded into dependent blocks'
    # agent prompts so signatures match across blocks.
    produces: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    status: BlockStatus = BlockStatus.PENDING
    # True when the block failed specifically because an agent LLM call timed
    # out (vs an ordinary build/test failure) — lets the orchestrator return
    # EXIT_AGENT_TIMEOUT only for real timeouts.
    timed_out: bool = False
    # HEAD of the project repo when the block started — the diff baseline for
    # the evidence-based quality review (git diff <baseline> = everything the
    # block changed). None when the project isn't a git repo.
    baseline_commit: str | None = None
    test_commit: str | None = None
    impl_commit: str | None = None
    review_scores: ReviewResult | None = None
    review_iterations: int = 0
    # Red-green proof captured by the engine (trimmed suite output): RED is
    # stored when the RED gate confirms the block's tests fail before
    # implementation; GREEN when the integration gate passes after it. Both
    # feed the reviewer (as evidence to verify, not trust) and build-progress.md.
    red_evidence: str = ""
    green_evidence: str = ""


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
