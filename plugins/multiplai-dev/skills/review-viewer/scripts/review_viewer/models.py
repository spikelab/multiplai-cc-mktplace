"""The `findings.json` v1 contract, the `checks.json` v1 contract, the
`summary.json` v1 contract, and the mailbox rows.

These pydantic models are the source of truth. `export-schema` writes them to
`schema/findings.v1.schema.json`, `schema/checks.v1.schema.json` and
`schema/summary.v1.schema.json`, which are committed; producers (the review skill, the session writing a
summary) validate against those files. Every model forbids unknown keys, so a
producer that drifts fails validation instead of losing data silently.

Line numbers are 1-based and refer to the file at `head_sha`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
SCHEMA_PATH = SCHEMA_DIR / "findings.v1.schema.json"
SUMMARY_SCHEMA_PATH = SCHEMA_DIR / "summary.v1.schema.json"
CHECKS_SCHEMA_PATH = SCHEMA_DIR / "checks.v1.schema.json"

SHA_PATTERN = r"^[0-9a-f]{40}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(_Strict):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str


# `Premise` and `Fix` describe the fix that review wrote for a confirmed
# finding before multiplai-dev 0.22. Review no longer writes one; the models
# stay so a findings file written then still opens. The page does not show it.
class Premise(_Strict):
    statement: str
    kind: Literal["in_repo", "external"]
    citation: Citation | None = None


class Fix(_Strict):
    description: str
    patch_sketch: str | None = None
    premises: list[Premise] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Need(_Strict):
    """Information the review could not get, and how a person would get it.

    `blocks` is a finding id, or "review" for a gap in the review as a whole.
    `command` was written by a model or by the pipeline; the page shows it to
    be read before running, and nothing runs it. `where` says where to look
    when one command does not get it (a console page, a dashboard, a team);
    files before multiplai-dev 0.32 have none.
    """

    what: str
    blocks: str
    cause: Literal["no-access", "lookup-failed", "unreachable"]
    command: str = ""
    where: str = ""
    source: Literal["verifier", "finder", "pipeline"]


class Assessment(_Strict):
    """The review's own judgement of a finding against the rest of the review
    and earlier rounds (multiplai-dev 0.27+). Older files have none.

    `repeat`: the same defect as a finding the person rejected in an earlier
    round (`earlier_*` say which, with the decision and note). `still-open`:
    the same defect as an earlier finding that was accepted or not decided.
    `low-value`: true, but not worth acting on, for the reason given.
    `important` (multiplai-dev 0.71+): a trim suggestion about a test that must
    stay, given by the verify stage, not the assess agent.
    `useful`: everything else. A label never removes a finding."""

    label: Literal["useful", "still-open", "low-value", "repeat", "important"]
    reason: str = ""
    earlier_id: str | None = None
    earlier_round: str | None = None  # head_sha of the earlier round
    earlier_decision: Literal["accept", "reject", "defer"] | None = None
    earlier_note: str | None = None


class Absorbed(_Strict):
    """A finding the review merged into this one, at its merge or assess stage: its id and claim."""

    id: str = Field(pattern=r"^[0-9a-f]{10}$")
    claim: str


# `Finding.paths` as the schema states it: optional, but never null when present.
PATH_IDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "uniqueItems": True,
}
# `Finding.lineage` as the schema states it: optional, but never null when present.
LINEAGE_SCHEMA = {"type": "string", "pattern": r"^[0-9a-f]{10}$"}
# `Finding.defect_class` and `Finding.other_locations` as the schema states them: optional, but never
# null or empty when present. Each location is `path:line` or `path:start-end`.
DEFECT_CLASS_SCHEMA = {"type": "string", "minLength": 1}
OTHER_LOCATIONS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {"type": "string", "pattern": r"^.+:\d+(-\d+)?$"},
}


class Finding(_Strict):
    id: str = Field(pattern=r"^[0-9a-f]{10}$")
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    # `unverified` (0.112+): a LOW finding no verifier was asked about.
    status: Literal["confirmed", "unverifiable", "unverified", "refuted", "rejected"]
    claim: str
    # The review's plain-language title (at most 12 words) and what happens, with an example
    # (multiplai-dev 0.113+); absent when the write stage gave up on the finding.
    title: str | None = None
    what_happens: str | None = None
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    failure_scenario: str
    citations: list[Citation] = Field(min_length=1)
    verdict_reason: str | None = None
    # One sentence from the verifier on what correct behaviour looks like,
    # without proposing code. None for refuted and rejected findings.
    expected_behaviour: str | None = None
    fix: Fix | None = None  # older files only; see Premise
    # What the review could not read to settle this finding. Absent in older files.
    needs: list[Need] = Field(default_factory=list)
    # The lines the verifier itself read to reach its verdict. Absent in
    # files written before multiplai-dev 0.26.
    verifier_citations: list[Citation] | None = None
    assessment: Assessment | None = None
    # What the finding is about, as the verifier labelled it (multiplai-dev
    # 0.32+). Absent in older files; the page then guesses from the path.
    topic: (
        Literal[
            "code",
            "tests",
            "docs",
            "config",
            "infra",
            "data",
            "security",
            "performance",
            "process",
        ]
        | None
    ) = None
    # What breaks in production if the change is merged as is, as the verifier
    # rated it (multiplai-dev 0.32+). Absent on refuted and rejected findings
    # and in older files. A `critical` review lists only the two `breaks-*`.
    impact: (
        Literal["breaks-users", "breaks-business", "correctness-only", "hygiene"] | None
    ) = None
    # The section the review lists the finding in (multiplai-dev 0.71+): `trim`
    # for what only the trim finder reported, else from the topic. Absent in
    # older files; readers then use the topic.
    section: Literal["code", "tests", "docs", "trim"] | None = None
    # The ids of the mapped paths (review's `paths.json`) the finding lies on, in that file's
    # order (multiplai-dev 0.82+). Absent when it lies on none; never null or empty.
    paths: Annotated[list[str] | None, WithJsonSchema(PATH_IDS_SCHEMA)] = None
    # The findings merged into this one, also through a finding merged into it in
    # turn (multiplai-dev 0.94+). Absent when there are none, and in older files.
    absorbed: list[Absorbed] = Field(default_factory=list)
    # The id of this finding's first appearance in an earlier round, through a
    # `still-open` or `repeat` assessment (multiplai-dev 0.94+). Absent when it is
    # the finding's own id, and in older files.
    lineage: Annotated[str | None, WithJsonSchema(LINEAGE_SCHEMA)] = None
    # The kind of defect in a few words, and every other place at head with the
    # same defect (multiplai-dev 0.95+). Absent when the finder named none, and in
    # older files.
    defect_class: Annotated[str | None, WithJsonSchema(DEFECT_CLASS_SCHEMA)] = None
    other_locations: Annotated[
        list[str] | None, WithJsonSchema(OTHER_LOCATIONS_SCHEMA)
    ] = None

    @field_validator("paths")
    @classmethod
    def _unique_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not v for v in value) or len(set(value)) != len(value):
            raise ValueError("paths must be distinct, non-empty path ids")
        return value


class Target(_Strict):
    slug: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    label: str
    repo_path: str
    remote_url: str | None = None
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    files_changed: list[str]


class RunTokens(_Strict):
    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cache_read: int = Field(ge=0)
    cache_write: int = Field(ge=0)
    total: int = Field(ge=0)


class RunStage(_Strict):
    """One row of the per-stage table: a finder (`find:<dimension>`), `verify` or `merge`."""

    name: str
    stage: str
    calls: int = Field(ge=0)
    tokens: RunTokens
    cost_usd: float = Field(ge=0)
    wall_seconds: float = Field(ge=0)
    model: str  # the configured model, or "session default"
    effort: str


class RunCounts(_Strict):
    found: int = Field(ge=0)
    rejected: int = Field(ge=0)  # by the gates
    refuted: int = Field(ge=0)
    unverifiable: int = Field(ge=0)
    # LOW findings no verifier was asked about; absent in files written before multiplai-dev 0.112.
    unverified: int | None = Field(default=None, ge=0)
    merged: int = Field(ge=0)
    # Labelled by the assess stage; absent in files written before multiplai-dev 0.28.
    repeats: int | None = Field(default=None, ge=0)
    low_value: int | None = Field(default=None, ge=0)


class Run(_Strict):
    """What one review cost and how long it ran, from the review's final state.

    Cost and tokens are what the SDK returned; a call that returned no usage
    counts as 0 and adds a line to the review's errors.
    """

    started_at: datetime | None = None
    ended_at: datetime | None = None
    wall_seconds: float = Field(
        ge=0
    )  # running time only: the gap before a resume is left out
    calls: int = Field(ge=0)
    tokens: RunTokens
    cost_usd: float = Field(ge=0)
    max_usd: float | None = None  # the budget ceiling; None = no ceiling
    stopped_by_budget: (
        bool  # the ceiling stopped the review at least once before it finished
    )
    stages: list[RunStage]
    counts: RunCounts
    errors: int = Field(ge=0)  # lines in the review's error list


class EarlierStatus(_Strict):
    """What became of a finding an earlier round listed and nobody rejected, at this round's head
    (multiplai-dev 0.94+).

    `round` is the head of the round that listed it (its citations are at that
    commit); `citations` are at this round's head and back the status. A finding
    no agent could assess is `cannot_verify`, never `resolved`; `error` says why
    when every recheck call failed.
    """

    earlier_id: str = Field(pattern=r"^[0-9a-f]{10}$")
    lineage: str = Field(pattern=r"^[0-9a-f]{10}$")
    round: str
    status: Literal["resolved", "still_present", "partially_resolved", "cannot_verify"]
    reason: str = ""
    citations: list[Citation] = Field(default_factory=list)
    error: str = ""


class FindingsFile(_Strict):
    """One review's findings, for review-viewer and the other readers of `findings.json`.

    Additive changes keep the version: a new optional field is added under
    `schema_version` 1, and a file written before it still validates.
    """

    schema_version: Literal[1]
    generated_at: datetime
    producer: str
    target: Target
    findings: list[Finding]
    # Every need of the review, the findings' and its own. Absent in older files.
    needs: list[Need] = Field(default_factory=list)
    run: Run | None = None  # absent in files written before multiplai-dev 0.28
    # The mode the review ran in (multiplai-dev 0.32+); absent means `full`.
    mode: Literal["full", "critical"] | None = None
    # The status of each finding the last round left open (multiplai-dev 0.94+).
    # Absent when there are none, and in older files.
    earlier: list[EarlierStatus] = Field(default_factory=list)


# --- checks.json v1 ------------------------------------------------------------------
#
# Written by the review pipeline beside findings.json: every agent it ran, what
# each was given, every tool call it made (inputs only, never results), and what
# it concluded. Optional: a review without it opens without the Checked tab.


class MarkedCitation(_Strict):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    gate: Literal["pass", "fail", "web"]  # citation_gate at head; a URL is not checked
    seen: Literal[
        "read", "searched", "diff", "prompt", "fetched", "not-seen"
    ]  # how the agent came to the lines


class ToolCallRecord(_Strict):
    tool: str
    target: str  # a repo-relative path, a search pattern, a URL or a query
    detail: str = ""


class CheckedFinding(_Strict):
    id: str = Field(pattern=r"^[0-9a-f]{10}$")
    claim: str
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    failure_scenario: str
    citations: list[MarkedCitation]
    fate: Literal["kept", "deduped", "merged", "rejected"]
    into: str | None = None  # deduped or merged into this id
    rule: str | None = None  # the gate rule that rejected it


class CheckedVerdict(_Strict):
    status: Literal["confirmed", "refuted", "unverifiable"]
    reason: str
    citations: list[MarkedCitation]
    lowered: bool  # verdict_gate turned a confirmation into unverifiable


class ConvertedItemEntry(_Strict):
    """One item of a piece or path finder's converted report (review 0.82+): what kind it was."""

    kind: Literal["defect", "advice"]
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    finding_id: str | None = (
        None  # None when the item had no location to stand a finding on
    )


class AgentEntry(_Strict):
    stage: str = Field(min_length=1)  # find, verify, merge
    subject: str  # the dimension, the finding id, or the merge group's ids
    given: list[str]
    calls: list[ToolCallRecord]
    outcome: str
    turns: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    started_at: str
    ended_at: str
    error: str = ""
    findings: list[CheckedFinding] = Field(default_factory=list)
    verdict: CheckedVerdict | None = None
    items: list[ConvertedItemEntry] | None = (
        None  # a convert call's (`<key> convert`), review 0.82+
    )


class GateEntry(_Strict):
    finding_id: str
    gate: Literal["finding_gate", "verdict_gate", "earlier_status_gate"]
    passed: bool
    rule: str = ""


class CoverageEntry(_Strict):
    """What a review stage could not cover, and why (review's `checks.add_coverage`)."""

    stage: Literal["paths", "find", "conventions"]
    subject: str
    level: Literal["info", "error"]
    reason: str
    detail: str = ""


class ChecksFile(_Strict):
    schema_version: Literal[1]
    generated_at: datetime
    producer: str
    target: Target
    agents: list[AgentEntry]
    gates: list[GateEntry]
    coverage: list[CoverageEntry] | None = None  # review 0.82+; absent in older files


def load_checks(path: str | Path) -> ChecksFile:
    """Parse and validate a checks file. Raises pydantic.ValidationError."""
    return ChecksFile.model_validate_json(Path(path).read_text(encoding="utf-8"))


def checks_schema_text() -> str:
    """The JSON Schema for ChecksFile, in its committed form."""
    return _schema_text(ChecksFile, "checks.v1.schema.json")


def finding_id(file: str, line_start: int, claim: str) -> str:
    """Stable id for a finding: survives re-renders of the same review."""
    raw = f"{file}\0{line_start}\0{claim}".encode()
    return hashlib.sha1(raw).hexdigest()[:10]


def findings_digest(ff: FindingsFile) -> str:
    """Identifies one version of a findings file's content."""
    return hashlib.sha256(ff.model_dump_json().encode("utf-8")).hexdigest()


def load_findings(path: str | Path) -> FindingsFile:
    """Parse and validate a findings file. Raises pydantic.ValidationError."""
    text = Path(path).read_text(encoding="utf-8")
    return FindingsFile.model_validate_json(text)


def _schema_text(model: type[BaseModel], schema_id: str) -> str:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = schema_id
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def schema_text() -> str:
    """The JSON Schema for FindingsFile, in its committed form."""
    return _schema_text(FindingsFile, "findings.v1.schema.json")


# --- summary.json v1 --------------------------------------------------------------
#
# Written by the Claude Code session that runs the skill (the server never
# calls a model), checked by `summary put`, rendered by the page's Summary tab.
# It holds verdicts and the risk inputs only, never an explanation of the code:
# that is the codebase-walkthrough page the Full walkthrough row links.

AssessmentTopic = Literal["commits", "tests", "size", "design", "risk", "other"]


# A badge shows its title on one line, so `summary put` takes titles up to
# this long. The model still loads the 60 characters files written before 0.22
# could use; the published schema advertises this limit, the one put enforces.
ASSESSMENT_TITLE_MAX = 32


class SummaryAssessment(_Strict):
    """The session's judgment on one quality question, shown as a badge beside
    the measured ones. `commits` (does anything say why the change is made?)
    and `tests` (would a test fail if the changed behaviour broke?) are
    required once the summary is complete, and judged by the rubric in
    SKILL.md. `size` and `risk` are accepted in files written before 0.22 but
    rejected by `summary put`: size is measured and risk is the header
    score."""

    topic: AssessmentTopic
    verdict: Literal["good", "note", "concern"]
    title: str = Field(
        min_length=1,
        max_length=60,
        json_schema_extra={"maxLength": ASSESSMENT_TITLE_MAX},
    )
    detail_md: str


class RiskInput(_Strict):
    """The session's two judgments the risk score needs; the page applies the
    rules (see logic.js riskLevel). `tier` is how critical the most critical
    changed code is, from the table in SKILL.md: 0 docs, tests and tooling;
    1 one feature; 2 a shared module or public interface; 3 auth, money, data
    deletion or migration, shared infra, production deploy config. A repo's
    `.review-risk.toml`, read at the base commit, can raise it for the files
    it matches, never lower it."""

    tier: int = Field(ge=0, le=3)
    tier_why: str = Field(min_length=1)
    revertable: bool
    revert_why: str = Field(min_length=1)


class Summary(_Strict):
    schema_version: Literal[1]
    generated_at: datetime
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    assessments: list[SummaryAssessment] = Field(default_factory=list)
    risk: RiskInput | None = None
    complete: bool


def summary_schema_text() -> str:
    """The JSON Schema for Summary, in its committed form."""
    return _schema_text(Summary, "summary.v1.schema.json")


# --- mailbox rows ------------------------------------------------------------


class Anchor(_Strict):
    """Lines a question is about. `side: base` numbers lines at base_sha, for
    a question about lines the change deletes; `head` (the default) at head_sha."""

    path: str
    side: Literal["head", "base"] = "head"
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


# A `share` row's text: the finding as markdown, edited in the page's dialog.
SHARE_TEXT_MAX = 20_000
# A Slack recipient as typed: a person, a @handle or a #channel. The session
# resolves it; this only keeps it to one short line.
SHARE_RECIPIENT_RE = r"^[^\x00-\x1f\x7f]{1,100}$"


class InboxRow(_Strict):
    v: Literal[1] = 1
    id: str
    ts: str
    target: str
    kind: Literal["question", "decision", "share"]
    finding_id: str | None = None
    anchor: Anchor | None = None
    text: str
    decision: Literal["accept", "reject", "defer"] | None = None
    # Sent by a block's light-bulb button: explain exactly `anchor`. The page
    # shows the answer in a strip above that block, not in the thread.
    explain: bool = False
    # kind "share" only: the page's Send button asks the session to post
    # `text`, unchanged, to the PR (`where` "pr" or "line") or to Slack
    # (`where` is the recipient). Pressing Send is the confirmation.
    to: Literal["github", "slack"] | None = None
    where: str | None = Field(default=None, pattern=SHARE_RECIPIENT_RE)
    # kind "question" only: the Full walkthrough dialog's Confirm. The server
    # fixes `text`; the session runs the command `summary status` prints.
    full_walkthrough: bool = False


class OutboxRow(_Strict):
    v: Literal[1] = 1
    reply_to: str
    ts: str
    text: str
    done: bool


class Decision(_Strict):
    decision: Literal["accept", "reject", "defer"]
    note: str = ""
    ts: str
