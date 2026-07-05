"""Pydantic models for pipeline data types.

All structured data flowing through the pipeline uses these models for
validation, serialization, and clear typing at LLM boundaries.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Search & Sources
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    """A single result from a search API, normalized across providers."""

    url: str
    title: str
    snippet: str
    source_api: str  # "tavily" | "exa" | "serper" | "you" | ...
    score: float | None = None  # provider-specific relevance score if available
    published_date: str | None = None
    # Set by triage when the URL matches an authority domain. A declared field
    # (not a private attr) so it survives model_dump / checkpoint / resume.
    is_authority: bool = False


class ReputationTier(str, Enum):
    AUTHORITATIVE = "authoritative"
    ESTABLISHED = "established"
    EMERGING = "emerging"
    QUESTIONABLE = "questionable"
    UNRELIABLE = "unreliable"


class SourceStatus(str, Enum):
    PENDING = "pending"
    FETCHED = "fetched"
    EXTRACTED = "extracted"
    FAILED = "failed"


class Source(BaseModel):
    """A source selected after triage, tracked through READ."""

    url: str
    title: str
    snippet: str
    reputation: ReputationTier = ReputationTier.EMERGING
    relevance_score: int | None = None  # 1-5 from LLM scoring
    source_api: str = ""
    status: SourceStatus = SourceStatus.PENDING
    error: str | None = None  # populated when status=FAILED
    extracted_content: str | None = None  # markdown from trafilatura
    published_date: str | None = None
    # Carried from the SearchResult; a declared field so the authority-budget
    # reservation in READ survives checkpoint/resume.
    is_authority: bool = False


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    """A single fact extracted from a source by the LLM."""

    fact: str
    source_url: str
    source_title: str
    reputation: ReputationTier = ReputationTier.EMERGING
    confidence: Confidence = Confidence.MEDIUM
    quote: str | None = None  # direct quote supporting the fact
    date: str | None = None
    relates_to_sub_question: int | None = None  # index into PlanResult.sub_questions


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanResult(BaseModel):
    """Output of the PLAN + DIVERGE + CHALLENGE nodes."""

    sub_questions: list[str] = Field(default_factory=list)
    primary_queries: list[str] = Field(default_factory=list)
    mechanism_queries: list[str] = Field(default_factory=list)
    contrarian_queries: list[str] = Field(default_factory=list)
    directory_queries: list[str] = Field(default_factory=list)
    target_domains: list[str] = Field(default_factory=list)
    authority_domains: list[str] = Field(default_factory=list)
    what_good_looks_like: str = ""

    # Parallel mode only
    sub_topics: list[SubTopic] = Field(default_factory=list)

    @property
    def all_queries(self) -> list[str]:
        """All queries combined for SEARCH phase."""
        return (
            self.primary_queries
            + self.mechanism_queries
            + self.contrarian_queries
            + self.directory_queries
        )


class SubTopic(BaseModel):
    """A sub-topic for parallel mode decomposition."""

    title: str
    focus: str  # what this agent investigates
    angle: str  # unique search perspective
    sub_questions: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)


# Forward ref resolution
PlanResult.model_rebuild()


# ---------------------------------------------------------------------------
# Reassessment
# ---------------------------------------------------------------------------


class ClaimVerdict(str, Enum):
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    UNVERIFIABLE = "unverifiable"


class VerifiedClaim(BaseModel):
    claim: str
    original_source: str
    verification_source: str | None = None
    verdict: ClaimVerdict = ClaimVerdict.UNVERIFIABLE
    correction: str | None = None
    impact: str | None = None


class ReassessResult(BaseModel):
    """Output of the REASSESS node."""

    # Framing checks (questions 1-3)
    framing_wrong_question: bool = False
    new_framing_emerged: bool = False
    missing_angle: bool = False
    framing_notes: str = ""

    # Claims checks (questions 4-6)
    load_bearing_claims: list[str] = Field(default_factory=list)
    conflation_claims: list[str] = Field(default_factory=list)
    convenience_bias_claims: list[str] = Field(default_factory=list)

    # Actions
    refinement_needed: bool = False
    refinement_queries: list[str] = Field(default_factory=list)
    verify_claims: list[str] = Field(default_factory=list)
    verify_queries: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class GateResult(BaseModel):
    """Result of a code gate between pipeline stages."""

    passed: bool
    reason: str
    action: str | None = None  # e.g., "expand_queries", "retry_search", "targeted_search"
    metadata: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fetch errors
# ---------------------------------------------------------------------------


class FetchErrorType(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    EXTRACTION = "extraction"
    UNKNOWN = "unknown"


class FetchError(BaseModel):
    """Typed error for fetch failures. Never None — always a typed error."""

    url: str
    error_type: FetchErrorType
    message: str
    elapsed_seconds: float
    retry_count: int = 0
    status_code: int | None = None


class FetchResult(BaseModel):
    """Result of a fetch + extract operation."""

    url: str
    success: bool
    content: str | None = None  # markdown if success
    error: FetchError | None = None
    elapsed_seconds: float = 0.0
    extracted_links: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Quality check (pre-synthesis go/no-go)
# ---------------------------------------------------------------------------


class QualityCheckResult(BaseModel):
    """Output of the pre-synthesis quality check. Cheap LLM call to decide
    whether findings are sufficient to produce a useful synthesis."""

    go: bool
    confidence: float = 0.5  # 0.0–1.0
    reasoning: str = ""
    critical_gaps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Quota tracking
# ---------------------------------------------------------------------------


class APIQuota(BaseModel):
    """Per-API quota tracking (persisted to JSON)."""

    api_name: str
    daily_count: int = 0
    monthly_count: int = 0
    total_count: int = 0  # lifetime, useful for one-time credit APIs
    monthly_limit: int | None = None  # null = no limit
    one_time_limit: int | None = None  # for APIs like Serper (2,500 one-time)
    last_reset_day: str | None = None  # YYYY-MM-DD
    last_reset_month: str | None = None  # YYYY-MM
    consecutive_failures: int = 0
    circuit_open_until: str | None = None  # ISO timestamp; null = closed


class QuotaState(BaseModel):
    """Top-level quota state persisted to ~/.config/research-pipeline/quotas.json."""

    quotas: dict[str, APIQuota] = Field(default_factory=dict)
    updated_at: str | None = None
