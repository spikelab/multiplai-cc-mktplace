"""Pipeline models.

These are the pipeline's own shapes, richer than the `findings.json` v1
contract (`export.py` maps them onto it). Two rules are enforced here, at parse
time, so no stage has to remember them:

- A finding cites at least one line range (`citations` has `min_length=1`). A
  finder that returns an uncited finding fails structured parsing.
- `Finding.id` is computed from `(file, line_start, claim)` with the v1 rule.
  Whatever id a model puts in its output is overwritten.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["HIGH", "MEDIUM", "LOW"]
SEVERITIES: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")

# Stage names in run order. `ReviewState.stage` holds the last one completed.
STAGES: tuple[str, ...] = (
    "target", "find", "verify", "merge", "repeats", "assess", "export", "render", "done",
)
# Stages a checkpoint written before 0.22 can name. Both ran after verify, so
# a resumed review of that age continues with merge.
_REMOVED_STAGES = {"prescribe": "verify", "check_fix": "verify"}


def finding_id(file: str, line_start: int, claim: str) -> str:
    """The v1 finding id: first 10 hex of sha1(f"{file}\\0{line_start}\\0{claim}").

    Reimplemented rather than imported from the viewer skill; `test_models.py`
    pins both to the same value for a fixed input.
    """
    return hashlib.sha1(f"{file}\0{line_start}\0{claim}".encode("utf-8")).hexdigest()[:10]


def lower_severity(severity: str) -> str:
    """One step down; LOW stays LOW."""
    i = SEVERITIES.index(severity)
    return SEVERITIES[min(i + 1, len(SEVERITIES) - 1)]


class _Model(BaseModel):
    # Models answer in JSON; an unknown key is ignored rather than failing the
    # whole parse. Export builds the v1 dicts field by field, so nothing
    # unknown ever reaches findings.json.
    model_config = ConfigDict(extra="ignore")


class Citation(_Model):
    path: str  # a repo-relative path, or an http(s) URL for a web source
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str  # exact text expected at those lines

    @property
    def is_web(self) -> bool:
        """A web source. The gates do not check it, and it never anchors a finding."""
        return self.path.lower().startswith(("http://", "https://"))

    @model_validator(mode="after")
    def _ordered(self) -> "Citation":
        if self.line_end < self.line_start:
            raise ValueError(f"line_end {self.line_end} is before line_start {self.line_start}")
        return self


class Finding(_Model):
    id: str = ""
    claim: str
    severity: Severity
    dimension: str = ""
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    failure_scenario: str
    citations: list[Citation] = Field(min_length=1)
    finder: str = ""
    # Every finder that reported this defect: the first one, then any whose
    # finding was dropped as a word-for-word copy or merged into this one.
    finders: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compute_id(self) -> "Finding":
        self.id = finding_id(self.file, self.line_start, self.claim)
        return self

    def with_location(self, **changes) -> "Finding":
        """A copy with changed fields and the id recomputed from them."""
        return Finding.model_validate({**self.model_dump(), **changes})


NEED_CAUSES: tuple[str, ...] = ("no-access", "lookup-failed", "unreachable")


class NeedAsk(_Model):
    """What an agent says it could not get: the `needs` items in its answer.

    An unknown `cause` becomes `no-access` rather than failing the parse: a
    need is a request to a person, and losing it over a label would hide it.
    """
    what: str
    cause: str = "no-access"
    command: str = ""

    @field_validator("cause", mode="before")
    @classmethod
    def _known_cause(cls, value) -> str:
        return value if value in NEED_CAUSES else "no-access"


class Need(BaseModel):
    """Information the review needed and could not get, and how a person would get it.

    `blocks` is a finding id, or "review" for what the finders and the
    pipeline's own lookups could not check. `command` is one read-only shell
    command, or "" when none is known or `need_gate` blanked it. The pipeline
    never runs it.
    """
    what: str
    blocks: str = "review"
    cause: Literal["no-access", "lookup-failed", "unreachable"] = "no-access"
    command: str = ""
    source: Literal["verifier", "finder", "pipeline"] = "pipeline"


class Verdict(_Model):
    finding_id: str = ""
    status: Literal["confirmed", "refuted", "unverifiable"]
    reason: str
    citations: list[Citation] = Field(default_factory=list)  # the verifier's own re-reads
    # What correct behaviour looks like, in one sentence, without proposing a
    # code change. Exported on the finding as `expected_behaviour`.
    expected_behaviour: str = ""
    # What the verifier could not read, each with a command a person could run.
    needs: list[NeedAsk] = Field(default_factory=list)


class DuplicateSet(_Model):
    finding_ids: list[str]
    reason: str = ""


class GateResult(BaseModel):
    passed: bool
    reason: str = ""
    action: str = ""  # "keep" | "reject" | "downgrade" — what the caller does on failure


class TargetInfo(BaseModel):
    repo_path: str
    remote_url: str | None = None
    base_sha: str
    head_sha: str
    commits: list[tuple[str, str]] = Field(default_factory=list)  # (sha, subject)
    files: list[str] = Field(default_factory=list)
    diff_path: str = ""
    slug: str
    label: str = ""
    kind: Literal["branch", "pr", "range", "tree"] = "range"
    ref: str = ""  # the branch name, PR number or range as given; for a tree, the path or "."
    pr: int | None = None
    tickets: list[str] = Field(default_factory=list)
    deployed_in: str | None = None
    # What the author says the change does: the PR title and body for --pr,
    # otherwise empty. Shown to the agents as untrusted text.
    title: str = ""
    description: str = ""
    # The branch the change merges into, and GitHub's rules on it
    # (`gh api repos/{owner}/{repo}/rules/branches/<base_ref>`), so the agents
    # can tell whether CI actually blocks a merge. `[]` means GitHub reports no
    # rules; `None` means they could not be read (no gh, not GitHub, API error).
    base_ref: str = ""
    branch_rules: list[dict] | None = None
    # Tree targets only: (file, reason) for each file at head left out of the
    # review (binary, too large, a lockfile, generated or vendored).
    skipped: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def is_tree(self) -> bool:
        """A whole tree or a directory at one commit, not a change: there is no diff and no commits."""
        return self.kind == "tree"


class Rejected(BaseModel):
    finding: Finding
    reason: str
    stage: str = "find"


class FinderResult(BaseModel):
    """One finder's normalised findings, before dedupe and the gates."""
    findings: list[Finding] = Field(default_factory=list)
    error: str = ""  # set when the finder failed; its findings are then empty
    needs: list[NeedAsk] = Field(default_factory=list)  # what it could not check


class Merged(BaseModel):
    """A finding folded into another that describes the same defect."""
    finding: Finding
    into: str  # the id of the finding it was merged into
    reason: str


class Interval(BaseModel):
    """One stretch of running time; `ended_at` is empty until it ends."""
    started_at: str
    ended_at: str = ""
class AgentCheck(BaseModel):
    """What one agent call was given, what it did, and what it concluded.

    Built only from what `run_agent` already returns: tool names and inputs,
    never tool results or file contents. `findings` (finders) and `verdict`
    (verifiers) hold the same kinds of agent text `findings.json` holds.
    """
    stage: str  # find | verify | merge
    subject: str  # the dimension, the finding id, or the merge group_key
    given: list[str] = Field(default_factory=list)  # labels of the prompt's input blocks
    calls: list[dict] = Field(default_factory=list)  # {tool, target, detail}
    outcome: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    started_at: str = ""
    ended_at: str = ""
    error: str = ""  # empty on success
    # Finders: every finding returned, before dedupe and gates, each with its
    # citations marked `gate` and `seen`, and its `fate`.
    findings: list[dict] = Field(default_factory=list)
    # Verifiers: status, reason, citations (marked), and whether verdict_gate lowered it.
    verdict: dict | None = None


class GateCheck(BaseModel):
    finding_id: str
    gate: str  # finding_gate | verdict_gate
    passed: bool
    rule: str = ""  # which rule fired (gates.reason_kind); empty when passed


class Repeat(BaseModel):
    """A shown finding that describes the same defect as one the person rejected in an earlier round."""
    rejected_id: str
    reason: str
    round: str  # head_sha of the earlier round the rejected finding came from
    note: str = ""  # the person's note on that rejection
    by: Literal["id", "agent"] = "agent"  # matched on the same id in Python, or by the repeats agent


AssessLabel = Literal["useful", "still-open", "low-value", "repeat"]


class Assessment(BaseModel):
    """How a shown finding relates to the rest of the review and to earlier rounds.

    Never changes a verdict or a severity, and never removes a finding.
    """
    label: AssessLabel
    reason: str = ""
    earlier_id: str = ""  # the earlier round's finding it repeats or is still open from
    earlier_round: str = ""  # head_sha of that round
    earlier_decision: str = ""  # the person's decision on it: accept, reject, defer, or "" for none
    earlier_note: str = ""


class ReviewState(BaseModel):
    target: TargetInfo
    stage: str = "target"  # last stage completed
    findings: list[Finding] = Field(default_factory=list)
    verdicts: dict[str, Verdict] = Field(default_factory=dict)  # by finding id
    rejected: list[Rejected] = Field(default_factory=list)
    merged: list[Merged] = Field(default_factory=list)
    # The merge agent's answer per group of overlapping findings, keyed by the
    # group's sorted ids, so a resumed merge stage asks no group twice.
    merge_answers: dict[str, list[DuplicateSet]] = Field(default_factory=dict)
    # Each finder's result by dimension, stored as it returns, so a budget
    # stop during find keeps it and a resumed find runs only the rest.
    finder_results: dict[str, FinderResult] = Field(default_factory=dict)
    # Shown findings that repeat a finding rejected in an earlier round, by id,
    # stored as the repeats stage finds them; `repeats_checked` lists the ids
    # already sent to the repeats agent, so a resume does not ask again.
    repeats: dict[str, Repeat] = Field(default_factory=dict)
    repeats_checked: list[str] = Field(default_factory=list)
    # The assess stage's label per shown finding, by id. `assess_answer` is the
    # agent's answer, stored as it arrives (empty when the call failed), so a
    # resume does not ask twice.
    assessments: dict[str, Assessment] = Field(default_factory=dict)
    assess_answer: "AssessOutput | None" = None
    original_severity: dict[str, str] = Field(default_factory=dict)  # lowered findings only
    errors: list[str] = Field(default_factory=list)  # agent failures, shown in the review header
    # Information the review could not get, each with a command for a person.
    needs: list[Need] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)
    # When the run, each stage and each finder ran (`timings.py`). Each resume
    # adds an interval, so wall time leaves out the time between runs.
    timings: dict[str, list[Interval]] = Field(default_factory=dict)
    # The model, effort and concurrency the last run used, per stage.
    run_config: dict = Field(default_factory=dict)
    # How many times the budget circuit breaker stopped this review.
    budget_stops: int = 0
    # The record of what was checked: one entry per agent call, in the order
    # they returned, and one per gate result. Empty in checkpoints written
    # before 0.26, which still load.
    checks: list[AgentCheck] = Field(default_factory=list)
    gate_checks: list[GateCheck] = Field(default_factory=list)

    @field_validator("stage", mode="before")
    @classmethod
    def _removed_stage(cls, value: str) -> str:
        return _REMOVED_STAGES.get(value, value)

    def past(self, stage: str) -> bool:
        """Whether *stage* has already completed."""
        return STAGES.index(self.stage) >= STAGES.index(stage)


# --- structured-output envelopes the agents return ---------------------------


class FinderOutput(_Model):
    findings: list[Finding] = Field(default_factory=list)
    needs: list[NeedAsk] = Field(default_factory=list)


class MergeOutput(_Model):
    duplicate_sets: list[DuplicateSet] = Field(default_factory=list)


class RepeatMatch(_Model):
    id: str
    rejected_id: str
    reason: str = ""


class RepeatsOutput(_Model):
    matches: list[RepeatMatch] = Field(default_factory=list)


class AssessItem(_Model):
    id: str
    label: str
    reason: str = ""
    earlier_id: str = ""


class AssessOutput(_Model):
    assessments: list[AssessItem] = Field(default_factory=list)
    duplicate_sets: list[DuplicateSet] = Field(default_factory=list)


ReviewState.model_rebuild()
