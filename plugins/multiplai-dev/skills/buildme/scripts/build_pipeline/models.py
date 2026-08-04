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
    # Reads the repo that already exists (architecture, patterns, dependencies)
    # so the design is written against it rather than against a generic stack.
    # Ordinal position matters: state.is_phase_complete compares positions in
    # this enum, so CODEBASE_ANALYSIS sits where it runs — after research,
    # before spec generation consumes its output. Checkpoints written before
    # this phase existed still load (the stored value is a phase name, not an
    # index) and resume at their recorded phase.
    CODEBASE_ANALYSIS = "codebase_analysis"
    SPEC_GENERATION = "spec_generation"
    DESIGN_AUDIT = "design_audit"
    # Cheap shape proof (mockup / sample output / CLI transcript) before the
    # expensive TDD build. Ordinal position matters: state.is_phase_complete
    # compares positions in this enum, so PROTOTYPE sits where it runs — after
    # the design audit, before the human review checkpoint. Checkpoints written
    # before this phase existed still load (the stored value is a phase name,
    # not an index) and resume at their recorded phase.
    PROTOTYPE = "prototype"
    REVIEW = "review"
    TDD_BUILD = "tdd_build"
    # Proposes a spec delta from the implementation notes collected during the
    # build. Sits between TDD_BUILD and PUBLISH because it reads what the
    # build learned; is_phase_complete compares enum positions, so old
    # checkpoints (which never carry "respec") still order correctly.
    RESPEC = "respec"
    # Push the build's own branch + open the PR. Ordinal position matters:
    # state.is_phase_complete compares positions in this enum, so PUBLISH must
    # sit immediately before COMPLETE (and after every producing phase,
    # including RESPEC).
    PUBLISH = "publish"
    COMPLETE = "complete"
    FAILED = "failed"


class BlockStatus(str, Enum):
    PENDING = "pending"
    TESTING = "testing"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"


class BoardColumn(str, Enum):
    """The eleven kanban columns of the dark-factory board.

    Values are the column names exactly as the board displays them — the same
    string lands in `.board.json`'s `column` field and in the
    `BOARD:<change>:<column>` stdout line, so there is one representation and
    no translation table.

    The enum is the full vocabulary of the board, NOT a claim about what the
    pipeline drives. `board.column_for` is the only mapping, and `board.py`'s
    module docstring states which of these columns the pipeline ever sets.
    """
    BACKLOG = "Backlog"
    ACCEPTED = "Accepted"
    SHAPING = "Shaping"
    PLANNING = "Planning"
    IN_DEVELOPMENT = "In Development"
    IN_REVIEW = "In Review"
    TESTING = "Testing"
    READY_FOR_PROD = "Ready for Prod"
    DEPLOYING = "Deploying"
    DEPLOYED = "Deployed"
    CANCELLED = "Cancelled"


class ArtifactStatus(str, Enum):
    DONE = "done"
    READY = "ready"
    BLOCKED = "blocked"


# --- Quality evaluation ---

class ReviewGatePolicy(BaseModel):
    """Thresholds the review gate applies. THE single source of these numbers.

    Previously 3.5 and "score == 1" were hardcoded in `review_score_gate` AND
    duplicated in `ReviewResult.passed`; the two could drift. Both now read
    this policy, and `specs/config.yaml: code_review.gate` overrides it. The
    defaults reproduce the old binary behavior exactly.
    """

    # Weighted average of effective scores below this fails the block.
    min_weighted_average: float = 3.5
    # An effective dimension score at or below this is a hard fail on its own.
    critical_score: float = 1.0
    # The "no information" score confidence pulls toward — see
    # ReviewScore.effective_score. Sitting exactly at the pass threshold means
    # a zero-confidence dimension neither rescues nor sinks a block.
    neutral_score: float = 3.5


class ReviewScore(BaseModel):
    dimension: str
    weight: int = Field(ge=1, le=3)
    score: int = Field(ge=1, le=5)
    evidence: str
    # Graded, not binary: how sure the reviewer is of THIS dimension's score.
    # Defaults to 1.0 so a reviewer that emits no confidence (and every
    # existing fixture) scores exactly as it did before.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def effective_score(self, neutral: float = 3.5) -> float:
        """The score discounted by confidence, toward *neutral*.

        Confidence interpolates between the reviewer's claim and "no
        information": at 1.0 this IS the raw score; at 0.0 it is `neutral`.

        The direction matters. Multiplying score by confidence would make an
        unsure reviewer look *harsher* (score 2 at 40% → 0.8, a hard critical
        fail), which is backwards — low confidence is weak evidence, not
        strong evidence of badness. Shrinking toward neutral instead means an
        unsure bad score merely stops counting.
        """
        return neutral + self.confidence * (self.score - neutral)


class ReviewIssue(BaseModel):
    dimension: str
    severity: str  # Critical, Major, Minor, Note
    description: str
    file_path: str | None = None
    line: int | None = None


class ReviewFinding(BaseModel):
    """One discrete, adjudicable claim from a reviewer.

    Reviews used to emit only scores and prose, which cannot be accepted or
    rejected one at a time. Roughly a quarter of reviewer suggestions are
    wrong, so a finding is a *proposal*: the orchestrator adjudicates it (see
    `_adjudicate_review_findings`) before anything acts on it.
    """

    claim: str
    severity: str = "Minor"  # Critical, Major, Minor, Note
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str = ""
    dimension: str = ""
    file_path: str | None = None
    line: int | None = None
    # Which panel members raised it. Independent confirmation is the strongest
    # signal a panel produces, so it is recorded rather than collapsed away.
    reviewers: list[str] = Field(default_factory=list)

    def dedupe_key(self) -> tuple:
        """Identity for cross-reviewer dedupe: same place, same claim."""
        claim = " ".join(self.claim.lower().split())[:160]
        return (self.file_path or "", self.line or 0, claim)


class FindingVerdict(BaseModel):
    """The orchestrator's accept/reject decision on one finding."""

    index: int
    accepted: bool
    reason: str = ""


class FindingAdjudication(BaseModel):
    """Structured output of the adjudication pass over a review's findings."""

    verdicts: list[FindingVerdict] = Field(default_factory=list)

    def accepted_indices(self, total: int) -> set[int]:
        """Indices judged accepted.

        A finding with no verdict is treated as ACCEPTED. Fail-safe direction:
        the adjudicator silently dropping a finding must not silently discard
        a real defect — an unreviewed finding stays in play.
        """
        judged = {v.index for v in self.verdicts if 0 <= v.index < total}
        rejected = {v.index for v in self.verdicts if not v.accepted and 0 <= v.index < total}
        unjudged = set(range(total)) - judged
        return (judged - rejected) | unjudged


class ReviewResult(BaseModel):
    scores: list[ReviewScore]
    issues: list[ReviewIssue] = Field(default_factory=list)
    # Discrete adjudicable claims. Reviewers that emit only `issues` get these
    # derived from them (see `findings_or_derived`), so nothing is lost.
    findings: list[ReviewFinding] = Field(default_factory=list)
    # Strengths-first review: what the diff genuinely does well.
    strengths: list[str] = Field(default_factory=list)
    # Spec-compliance verdict (two-verdict review, ported from the superpowers
    # task reviewer): spec behavior absent from the diff, implementation beyond
    # the spec, and implementation that got a scenario's meaning wrong.
    missing: list[str] = Field(default_factory=list)
    extra: list[str] = Field(default_factory=list)
    misunderstood: list[str] = Field(default_factory=list)
    # Findings the orchestrator rejected, kept for the record. Never fed to a
    # fix agent — that is the point of adjudication.
    rejected_findings: list[ReviewFinding] = Field(default_factory=list)
    # Reviewer labels that produced this result (>1 after a panel merge).
    panel: list[str] = Field(default_factory=list)

    def weighted_average_with(self, policy: ReviewGatePolicy | None = None) -> float:
        """Confidence-discounted weighted average (see ReviewScore.effective_score)."""
        if not self.scores:
            return 0.0
        pol = policy or ReviewGatePolicy()
        total_weight = sum(s.weight for s in self.scores)
        if total_weight == 0:
            return 0.0
        return sum(
            s.effective_score(pol.neutral_score) * s.weight for s in self.scores
        ) / total_weight

    @property
    def weighted_average(self) -> float:
        """Default-policy weighted average. Identical to the pre-graded value
        whenever every score carries the default confidence of 1.0."""
        return self.weighted_average_with(None)

    @property
    def spec_compliant(self) -> bool:
        """Clean-or-minor spec verdict. Missing or misunderstood spec behavior
        means the block cannot be trusted; `extra` alone is scope creep the
        dimension scores already price in."""
        return not self.missing and not self.misunderstood

    def passed_with(self, policy: ReviewGatePolicy | None = None) -> bool:
        """Both verdicts must hold: spec compliance AND the score threshold.

        Takes the policy explicitly because `specs/config.yaml:
        code_review.gate` can move these thresholds. Callers that have a
        `BuildConfig` MUST pass `config.review_gate` — otherwise this reports
        the default-policy verdict while `review_score_gate` (which decides the
        block's fate) applies the configured one, and the two disagree exactly
        when someone bothered to configure them.
        """
        pol = policy or ReviewGatePolicy()
        return (
            self.spec_compliant
            and self.weighted_average_with(pol) >= pol.min_weighted_average
            and not self.failing_dimensions_with(pol)
        )

    @property
    def passed(self) -> bool:
        """The DEFAULT-policy verdict. `review_score_gate` is authoritative.

        Convenience for call sites with no config in hand (tests, ad-hoc
        inspection). Prefer `passed_with(config.review_gate)`.
        """
        return self.passed_with(None)

    def failing_dimensions_with(self, policy: ReviewGatePolicy | None = None) -> list[str]:
        pol = policy or ReviewGatePolicy()
        return [
            s.dimension
            for s in self.scores
            if s.effective_score(pol.neutral_score) <= pol.critical_score
        ]

    @property
    def failing_dimensions(self) -> list[str]:
        return self.failing_dimensions_with(None)

    def findings_or_derived(self) -> list[ReviewFinding]:
        """Findings to adjudicate — falling back to `issues` when empty.

        A reviewer running an older prompt (or a model that ignored the
        findings slot) still produces `issues`; deriving findings from them
        keeps adjudication total rather than silently reviewing nothing.
        """
        if self.findings:
            return list(self.findings)
        return [
            ReviewFinding(
                claim=i.description,
                severity=i.severity,
                dimension=i.dimension,
                file_path=i.file_path,
                line=i.line,
                # Derived, not stated: mid confidence rather than a fabricated
                # high one.
                confidence=0.5,
            )
            for i in self.issues
        ]

    def findings_text(self) -> str:
        """Accepted findings rendered for a fix agent's prompt.

        Reads `self.findings`, which is only the ACCEPTED set because
        `_adjudicate_review_findings` writes the adjudicated survivors back into
        it (rejects move to `rejected_findings`). That write-back is load-bearing
        and this method is why: without it, an unjudged finding would reach a fix
        agent. Don't "fix" this to read a separate accepted list without moving
        the write-back too.
        """
        return "\n".join(
            f"- [{f.severity}] {f.claim}"
            + (f" ({f.file_path}:{f.line})" if f.file_path else "")
            + (f"\n  evidence: {f.evidence}" if f.evidence else "")
            for f in self.findings
        )


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


# --- Implementation notes (spec ↔ implementation loop) ---

SPEC_IMPACT_LEVELS = ("none", "clarify", "contradicts")


class ImplementationNote(BaseModel):
    """One agent's report of what did not match the spec/design.

    Parsed from the agent's REQUIRED `SURPRISES:` / `SPEC_IMPACT:` slots
    (gates.parse_implementation_note). Notes accumulate on the block (so they
    survive resume) and are appended to implementation-notes.md as the build
    runs, which is what the respec step reads at the end of the build.
    """
    block_number: int
    block_name: str
    role: str  # test_writer | implementer | refactorer
    surprises: str = ""
    spec_impact: str = "none"  # none | clarify | contradicts

    @property
    def contradicts(self) -> bool:
        """The block could only be built by doing something the spec does not
        say (or says otherwise) — the loudest signal for the respec step."""
        return self.spec_impact == "contradicts"


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
    # Set only when the block's refactor pass survived verification (suite
    # re-run + test-integrity gate). A discarded refactor leaves this None,
    # which is how the trajectory can tell "no refactor landed" from "one did".
    refactor_commit: str | None = None
    review_scores: ReviewResult | None = None
    review_iterations: int = 0
    # Red-green proof captured by the engine (trimmed suite output): RED is
    # stored when the RED gate confirms the block's tests fail before
    # implementation; GREEN when the integration gate passes after it. Both
    # feed the reviewer (as evidence to verify, not trust) and build-progress.md.
    red_evidence: str = ""
    green_evidence: str = ""
    # {test file path: sha256} snapshotted the moment the RED gate passed.
    # The test files stay writable for the whole implement phase AND for every
    # review-fix iteration, so this is the only way to tell afterwards whether
    # the bar moved. Persisted so it survives resume. Empty = not snapshotted
    # (pre-integrity checkpoints, or no test command configured).
    test_file_hashes: dict[str, str] = Field(default_factory=dict)
    # The implementer's own report, kept so the reviewer and the test-integrity
    # gate (its TEST CHANGE REQUIRED escape hatch) can both consult it.
    implementer_report: str = ""
    # Test-integrity claims the implementer declared. Passed to the reviewer as
    # unverified assertions, exactly like RED/GREEN evidence.
    test_change_claims: list[str] = Field(default_factory=list)
    # Surprises the block's agents reported (SURPRISES:/SPEC_IMPACT: slots).
    # Persisted here so a resumed build keeps the learning it already
    # collected; also appended to implementation-notes.md as they arrive.
    notes: list[ImplementationNote] = Field(default_factory=list)


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
