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
from collections.abc import Sequence
from typing import Literal

from code_facts import Dropped as FactDropped
from code_facts import Fact, FilePurpose, Term
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import TRIM_DIMENSION

Severity = Literal["HIGH", "MEDIUM", "LOW"]
SEVERITIES: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")

# Stage names in run order. `ReviewState.stage` holds the last one completed.
STAGES: tuple[str, ...] = (
    "target",
    "explain",
    "paths",
    "find",
    "verify",
    "recheck",
    "merge",
    "repeats",
    "assess",
    "write",
    "export",
    "render",
    "done",
)
# Stages a checkpoint written before 0.22 can name. Both ran after verify, so
# a resumed review of that age continues with merge.
_REMOVED_STAGES = {"prescribe": "verify", "check_fix": "verify"}


def finding_id(file: str, line_start: int, claim: str) -> str:
    """The v1 finding id: first 10 hex of sha1(f"{file}\\0{line_start}\\0{claim}").

    Reimplemented rather than imported from the viewer skill; `test_models.py`
    pins both to the same value for a fixed input.
    """
    return hashlib.sha1(f"{file}\0{line_start}\0{claim}".encode()).hexdigest()[:10]


def lower_severity(severity: str) -> str:
    """One step down; LOW stays LOW."""
    i = SEVERITIES.index(severity)
    return SEVERITIES[min(i + 1, len(SEVERITIES) - 1)]


def locations_list(value) -> list[str]:
    """A finder's `other_locations` as a list of stripped strings: None is [], one string is a list of one.

    A parse failure would lose the whole finding, so any JSON value is read as
    best it can be, and never raises: a value that is neither a string nor a
    list (false, a number, an object) is [], and in a list only the strings
    are kept, so an object's keys never become places.
    `gates.locations_gate` judges each location afterwards.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = [v.strip() for v in value if isinstance(v, str)]
    return [v for v in out if v]


class _Model(BaseModel):
    # Models answer in JSON; an unknown key is ignored rather than failing the
    # whole parse. Export builds the v1 dicts field by field, so nothing
    # unknown ever reaches findings.json.
    model_config = ConfigDict(extra="ignore")


class Citation(_Model):
    # A repo-relative path, an http(s) URL for a web source, or a path under
    # `.review-deps/<name>@<commit12>/` for a line of a git dependency.
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str  # exact text expected at those lines

    @property
    def is_web(self) -> bool:
        """A web source. The gates do not check it, and it never anchors a finding."""
        return self.path.lower().startswith(("http://", "https://"))

    @property
    def is_dependency(self) -> bool:
        """A line of a git dependency (`deps.py`). The gates check it, but it never anchors a finding."""
        return self.path.startswith(DEPS_DIR + "/")

    @model_validator(mode="after")
    def _ordered(self) -> Citation:
        if self.line_end < self.line_start:
            raise ValueError(
                f"line_end {self.line_end} is before line_start {self.line_start}"
            )
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
    # The ids of the mapped paths the finding lies on (`paths_io.assign_paths`, after merge), in
    # `paths.json` order; None when it lies on none or the review mapped no path.
    paths: list[str] | None = None
    # The kind of defect in a few words, and every other place with the same
    # defect as `path:line` or `path:start-end`, as the finder named them
    # (buildme's `ReviewFinding` names). `gates.locations_gate` drops a
    # location that is not at head; merge takes the union of a set's locations.
    defect_class: str = ""
    other_locations: list[str] = Field(default_factory=list)
    # An incremental round copies a finding an earlier round left open on a file whose
    # patch has not changed: the kept round's directory name (`rounds/<NN>-<head12>`).
    carried_from: str | None = None

    @field_validator("defect_class", mode="before")
    @classmethod
    def _class_text(cls, value):
        return "" if value is None else str(value).strip()

    @field_validator("other_locations", mode="before")
    @classmethod
    def _locations_list(cls, value):
        """One location written as a string, or a location as an object, still parses."""
        return locations_list(value)

    @model_validator(mode="after")
    def _compute_id(self) -> Finding:
        self.id = finding_id(self.file, self.line_start, self.claim)
        return self

    def with_location(self, **changes) -> Finding:
        """A copy with changed fields and the id recomputed from them."""
        return Finding.model_validate({**self.model_dump(), **changes})


def is_trim(finding: Finding) -> bool:
    """Whether only the trim finder reported *finding*."""
    names = finding.finders or [finding.finder]
    return all(name == TRIM_DIMENSION for name in names)


NEED_CAUSES: tuple[str, ...] = ("no-access", "lookup-failed", "unreachable")
# What a finding is about, as the verifier labels it. The page shows it as a badge.
TOPICS: tuple[str, ...] = (
    "code",
    "tests",
    "docs",
    "config",
    "infra",
    "data",
    "security",
    "performance",
    "process",
)
# What goes wrong in production if the change is merged as it is, as the
# verifier rates it. `--mode critical` lists only CRITICAL_IMPACTS.
# DEPENDS_ON_DATA: default settings reach the path, and the harm happens only
# when production holds data the verifier cannot read; its reason names that data.
DEPENDS_ON_DATA = "depends-on-data"
IMPACTS: tuple[str, ...] = (
    "breaks-users",
    "breaks-business",
    DEPENDS_ON_DATA,
    "correctness-only",
    "hygiene",
)
CRITICAL_IMPACTS: tuple[str, ...] = ("breaks-users", "breaks-business", DEPENDS_ON_DATA)
MODES: tuple[str, ...] = ("full", "critical")
# The section a finding is listed in. `trim` holds what only the trim finder
# reported; otherwise by topic, and any other topic, or none, is "code".
SECTIONS: tuple[str, ...] = ("code", "tests", "docs", "trim")


def section_of(topic: str | None, finders: Sequence[str] = ()) -> str:
    """The finding's section. Exported as `section`, so readers of findings.json need not repeat this."""
    if finders and all(name == TRIM_DIMENSION for name in finders):
        return "trim"
    return topic if topic in ("tests", "docs") else "code"


class NeedAsk(_Model):
    """What an agent says it could not get: the `needs` items in its answer.

    An unknown `cause` becomes `no-access` rather than failing the parse: a
    need is a request to a person, and losing it over a label would hide it.
    """

    what: str
    cause: str = "no-access"
    command: str = ""
    where: str = ""

    @field_validator("cause", mode="before")
    @classmethod
    def _known_cause(cls, value) -> str:
        return value if value in NEED_CAUSES else "no-access"


# Where the snapshot holds the reviewed repository's git dependencies
# (`deps.py`): `<snapshot>/.review-deps/<name>@<commit[:12]>/`.
DEPS_DIR = ".review-deps"


class DepEntry(BaseModel):
    """One git dependency a uv.lock at head pins, and what `deps.materialize` did with it.

    `cache` is the bare clone the gates re-read a citation of it from; `status`
    `ok` means its source is in the snapshot under `folder`.
    """

    name: str
    url: str
    commit: str
    cache: str = ""
    status: Literal["ok", "skipped", "failed"]
    reason: str = ""

    @property
    def folder(self) -> str:
        return f"{self.name}@{self.commit[:12]}"


class Need(BaseModel):
    """Information the review needed and could not get, and how a person would get it.

    `blocks` is a finding id, or "review" for what the finders and the
    pipeline's own lookups could not check. `command` is one read-only shell
    command, or "" when none is known or `need_gate` blanked it. The pipeline
    never runs it. `where` says where a person would look when one command
    does not get it: a console page, a dashboard, a file outside the repo, or
    the team that owns it; "" when the agent named none.
    """

    what: str
    blocks: str = "review"
    cause: Literal["no-access", "lookup-failed", "unreachable"] = "no-access"
    command: str = ""
    where: str = ""
    source: Literal["verifier", "finder", "pipeline"] = "pipeline"


class NeedCommand(BaseModel):
    """One command the review asks a person to run, and every need it answers (`export.need_commands`).

    `id` is `N1`, `N2`, … in the order the command first appears in the needs.
    `why` is the shortest `what` of the needs grouped under it. `blocks` is the
    sorted finding ids it bears on, or `["review"]` when it bears on none.
    """

    id: str
    command: str
    why: str
    blocks: list[str]
    where: str = ""
    whats: list[str]


class AnswerMatch(BaseModel):
    """What one saved output, `answers/<id>.txt`, bears on (`answers.py`), saved once matched.

    `sha256` is the output's hash when matched, `head_sha` the review's head then. `before` is
    each finding's verdict status before the output was applied ("" when it had none), so the
    command can say what changed and rewrite only those findings. `note` is what the match call
    said the output shows that no finding covers.
    """

    sha256: str
    head_sha: str
    finding_ids: list[str] = Field(default_factory=list)
    before: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class Verdict(_Model):
    finding_id: str = ""
    # `unverified` is made in code for a LOW finding no verifier was asked about
    # (`stages/verify.py`); a verifier never answers it (`VerifierAnswer`).
    status: Literal["confirmed", "refuted", "unverifiable", "unverified"]
    reason: str
    citations: list[Citation] = Field(
        default_factory=list
    )  # the verifier's own re-reads
    # What correct behaviour looks like, in one sentence, without proposing a
    # code change. Exported on the finding as `expected_behaviour`.
    expected_behaviour: str = ""
    # What the verifier could not read, each with a command a person could run.
    needs: list[NeedAsk] = Field(default_factory=list)
    # What the finding is about, one of TOPICS; "" when the verifier gave none
    # or one not in the list. Exported on the finding as `topic`.
    topic: str = ""
    # What breaks in production if merged as is, one of IMPACTS; "" for a
    # refuted finding and in checkpoints written before 0.32.
    impact: str = ""
    # The severity the verifier gives the consequence it verified; None for a
    # refuted finding, an answer without one, and checkpoints written before
    # 0.132. The verify stage sets the finding's severity from it
    # (`stages/verify.verified_severity`).
    severity: Severity | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _known_topic(cls, value) -> str:
        value = str(value or "").strip().lower()
        return value if value in TOPICS else ""

    @field_validator("impact", mode="before")
    @classmethod
    def _known_impact(cls, value) -> str:
        value = str(value or "").strip().lower()
        return value if value in IMPACTS else ""

    @field_validator("severity", mode="before")
    @classmethod
    def _known_severity(cls, value) -> str | None:
        value = str(value or "").strip().upper()
        return value if value in SEVERITIES else None


class VerifierAnswer(Verdict):
    """The verifier's answer as parsed: `impact` and `topic` are required unless refuted.

    A missing or unknown impact or topic fails the parse, so
    `agent_call_structured` re-asks; the stage stores a plain `Verdict`. The
    topic decides a finding's section and whether its impact is capped, so a
    blanked one would put a test gap in Code with a `breaks-*` impact.
    """

    @model_validator(mode="after")
    def _impact_required(self) -> VerifierAnswer:
        if self.status == "unverified":
            raise ValueError("status must be confirmed, refuted or unverifiable")
        if self.status != "refuted" and not self.impact:
            raise ValueError(
                f"impact is required unless status is refuted: one of {', '.join(IMPACTS)}"
            )
        if self.status != "refuted" and not self.topic:
            raise ValueError(
                f"topic is required unless status is refuted: one of {', '.join(TOPICS)}"
            )
        return self


class DuplicateSet(_Model):
    finding_ids: list[str]
    reason: str = ""


class GateResult(BaseModel):
    passed: bool
    reason: str = ""
    action: str = (
        ""  # "keep" | "reject" | "downgrade" — what the caller does on failure
    )


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
    ref: str = (
        ""  # the branch name, PR number or range as given; for a tree, the path or "."
    )
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
    # Change targets only: the `--exclude` globs, and the changed paths they
    # left out of `files` and of the diff.
    excluded: list[str] = Field(default_factory=list)
    excluded_files: list[str] = Field(default_factory=list)
    # The git dependencies whose source this run extracted into the snapshot
    # (`deps.ensure`, set again on every run and resume); the gates re-read a
    # citation of one from its `cache` at its `commit`.
    deps: list[DepEntry] = Field(default_factory=list)

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
    # Its label when the assess stage merged it, kept so a `still-open` label still counts for the survivor.
    assessment: Assessment | None = None


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
    given: list[str] = Field(
        default_factory=list
    )  # labels of the prompt's input blocks
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
    # A convert call (`<key> convert`): each item's kind, severity and finding id (`ConvertedItem`).
    items: list[dict] | None = None


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
    by: Literal["id", "agent"] = (
        "agent"  # matched on the same id in Python, or by the repeats agent
    )


class EarlierDecision(BaseModel):
    """One entry of an `--earlier-decisions` file: a finding an earlier reviewer of the same
    change (buildme's block review) rejected, or accepted and left unfixed, with its reason."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    file: str = ""
    line_start: int | None = None
    line_end: int | None = None
    claim: str = Field(min_length=1)
    decision: Literal["rejected", "accepted-unfixed"]
    reason: str = ""


class EarlierDecisionMatch(BaseModel):
    """A shown finding that describes the same defect as an `EarlierDecision`: its id,
    decision and reason, why the two are the same (`match_reason`), and how it was matched."""

    id: str
    decision: Literal["rejected", "accepted-unfixed"]
    reason: str = ""
    match_reason: str = ""
    by: Literal["id", "agent"] = "agent"


# `important` is not the assess agent's: the verify stage gives it to a
# confirmed trim finding about a test that must stay (`stages/verify.important_assessments`).
AssessLabel = Literal["useful", "still-open", "low-value", "repeat", "important"]


class Assessment(BaseModel):
    """How a shown finding relates to the rest of the review and to earlier rounds.

    Never changes a verdict or a severity, and never removes a finding.
    """

    label: AssessLabel
    reason: str = ""
    earlier_id: str = ""  # the earlier round's finding it repeats or is still open from
    earlier_round: str = ""  # head_sha of that round
    earlier_decision: str = (
        ""  # the person's decision on it: accept, reject, defer, or "" for none
    )
    earlier_note: str = ""


# What became of an earlier round's open finding, worst first: the order the
# assess stage's reconcile uses to pick the final status. buildme's
# `PRIOR_STATUSES` holds the same words best first; copied, not imported
# (buildme does not import this package), and `test_recheck.py` pins the two.
EARLIER_STATUSES: tuple[str, ...] = (
    "still_present",
    "partially_resolved",
    "cannot_verify",
    "resolved",
)
EarlierStatusWord = Literal[
    "still_present", "partially_resolved", "cannot_verify", "resolved"
]


class EarlierStatus(BaseModel):
    """The status of a finding an earlier round listed and nobody rejected, at this round's head.

    Given by the recheck stage (`stages/recheck.py`), checked by `earlier_status_gate`,
    and made final by the assess stage's reconcile. `round` is the head of the round
    the finding was listed in, the commit its citations are at; `citations` are at
    this round's head. `error` is set when every recheck try failed.
    """

    earlier_id: str
    lineage: str
    round: str
    status: EarlierStatusWord
    reason: str = ""
    citations: list[Citation] = Field(default_factory=list)
    error: str = ""


class ExplainedFacts(BaseModel):
    """The explain stage's checked facts (`code_facts`), as `facts.json` holds them."""

    facts: list[Fact] = Field(default_factory=list)
    files: list[FilePurpose] = Field(default_factory=list)
    interfaces: list[Fact] = Field(default_factory=list)
    gotchas: list[Fact] = Field(default_factory=list)
    conventions: list[Fact] = Field(default_factory=list)
    terms: list[Term] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)
    dropped: list[FactDropped] = Field(default_factory=list)


class Piece(BaseModel):
    """One piece of a change review's diff, which every dimension finder reviews (`chunks.split_pieces`).

    Files only, in piece order (head paths; a deleted file's base path); the
    finder's diff is rebuilt from the stored `diff.patch` when its prompt is
    built (`chunks.piece_diff_text`), which keeps the state small.
    """

    id: int  # 1-based, contiguous
    group: (
        str  # the package or skill directory, "" for the root, "meta" for a meta piece
    )
    meta: bool = False
    files: list[str] = Field(default_factory=list)
    omitted: list[str] = Field(
        default_factory=list
    )  # lockfiles, whose text no piece holds (meta only)
    binary: list[str] = Field(
        default_factory=list
    )  # files in `files` whose change is binary
    chars: int = 0  # characters of diff, headers included; binary files count 0
    # A half made when a finder hit the turn limit on piece `split_from` (`chunks.split_piece`); it runs only
    # the dimensions in `split_for`. The piece it was cut from lists its halves' ids in `split_into`.
    split_from: int | None = None
    split_into: list[int] = Field(default_factory=list)
    split_for: list[str] = Field(default_factory=list)
    # A half of a one-file piece: the [first, end) indices of the file's hunks it holds; None for whole files.
    hunks: list[int] | None = None


class ConvertedItem(BaseModel):
    """One item of a converted report, as `checks.json` records it: its kind is recorded nowhere else."""

    kind: Literal["defect", "advice"]
    severity: Severity
    finding_id: str | None = (
        None  # None when the item named no location a finding could stand on
    )


class FinderReport(BaseModel):
    """A piece or path finder's plain-text report, stored as soon as it returns, and what convert made of it."""

    key: str  # "callers@3" | "path@p7" ("piece@3" in checkpoints written before multiplai-dev 0.106)
    text: str
    converted: bool = False
    convert_error: str | None = (
        None  # the last convert call failed; `resume` asks it again
    )
    items: list[ConvertedItem] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    needs: list[NeedAsk] = Field(
        default_factory=list
    )  # what the report says it could not check


class CoverageRecord(BaseModel):
    """What a stage could not cover, or covered only in part, and why (`checks.add_coverage`).

    `error` records also go to `state.errors`; `info` records (normal scoping, a
    doc left out for the budget) only to `checks.json` and the report's gap line.
    """

    stage: Literal["paths", "find", "verify", "conventions"]
    subject: str  # "p3", "review_cmd at a.py:2", "piece@2", "docs/gone.md"
    level: Literal["info", "error"]
    reason: str  # a closed vocabulary per stage: see checks.ERROR_LINES
    detail: str = ""


class WriteAnswer(_Model):
    """A shown finding in plain words (`stages/write.py`): a title of at most 12 words, and what
    happens, with one example. The finding's `claim` is never changed; this is shown above it."""

    title: str
    what_happens: str


class WriteItem(WriteAnswer):
    id: str


class WriteOutput(_Model):
    findings: list[WriteItem] = Field(default_factory=list)


class RunStart(BaseModel):
    """One run of the pipeline on this round: a review or a resume (`orchestrator.run_state`)."""

    producer: str  # "multiplai-dev:review 0.111.0"
    session_id: str = ""
    started_at: str = ""


class Scope(BaseModel):
    """What this round reviews, decided once by `prepare` (`incremental.decide`) and kept by `resume`.

    `incremental` re-reviews only `reviewed_files`: the change's files whose patch differs from the
    one in round `since_round`'s `diff.patch`, or that are new to the change. The findings that round
    left open on the other files are copied into this round (`carried_ids`). `full` reviews every
    file; `reason` says which condition ruled out `incremental`.
    """

    kind: Literal["full", "incremental"] = "full"
    since_round: str = ""  # the kept round's directory name
    since_number: int = 0  # its position among the kept rounds, 1-based
    since_head: str = ""
    since_base: str = ""
    reviewed_files: list[str] = Field(default_factory=list)
    total_files: int = 0
    carried_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class ReviewState(BaseModel):
    target: TargetInfo
    stage: str = "target"  # last stage completed
    # `full` lists every section; `critical` only findings whose impact is in
    # CRITICAL_IMPACTS. Set when the review starts; `resume` keeps it.
    mode: Literal["full", "critical"] = "full"
    # A tree review run with `--trim` (a change review reads `ReviewConfig.trim`
    # instead). Set when the review starts; `resume` keeps it.
    trim: bool = False
    # The extra finders' switch, which nothing reads since the piece finders replaced them
    # (multiplai-dev 0.82); kept so checkpoints that carry it still load.
    extra_finders: bool = False
    # The paths stage (`stages/paths.py`). `paths_enabled` is a tree review's `--paths` (a change
    # review always maps paths); `resume` keeps it. `paths_status` says what the stage did, and
    # `path_ids` are the ids in `paths.json`, in file order. A checkpoint saved at or past find
    # before the stage existed keeps "not-run", which every reader of paths takes as no paths.
    paths_enabled: bool = False
    paths_status: Literal["not-run", "skipped", "ok", "empty", "failed"] = "not-run"
    paths_failure: str | None = None  # why a failed or empty mapping has no paths
    path_ids: list[str] = Field(default_factory=list)
    # A change review's pieces (`chunks.split_pieces`), computed once by `prepare` and reused by
    # `resume`; None for a tree review and in checkpoints written before pieces existed.
    pieces: list[Piece] | None = None
    # The finder dimensions a change review runs on every piece (`review.yaml` `dimensions`), set by
    # `prepare` and kept by `resume`; None in checkpoints written before multiplai-dev 0.106 (read as the
    # default set, `stages/find.DEFAULT_CHANGE_DIMENSIONS`).
    dimensions: list[str] | None = None
    # The piece and path finders' reports by key (`stages/find.run_reported`). A fresh round
    # starts empty; `resume` converts what is stored and asks only for what is missing.
    finder_reports: dict[str, FinderReport] = Field(default_factory=dict)
    # What a stage could not cover (`checks.add_coverage`), exported as `checks.json`'s `coverage`.
    coverage: list[CoverageRecord] = Field(default_factory=list)
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
    # The explain stage (`explain: true` only): each explore agent's answer by
    # task key, stored as it returns so a resume asks none twice, and the
    # facts that passed the citation check. None when explain did not run.
    explain_answers: dict[str, dict] = Field(default_factory=dict)
    facts: ExplainedFacts | None = None
    # Shown findings that repeat a finding rejected in an earlier round, by id,
    # stored as the repeats stage finds them; `repeats_checked` lists the ids
    # already sent to the repeats agent, so a resume does not ask again.
    repeats: dict[str, Repeat] = Field(default_factory=dict)
    repeats_checked: list[str] = Field(default_factory=list)
    # Shown findings that describe the same defect as an entry of the `--earlier-decisions` file
    # (a decision buildme's block review made), by finding id, stored by the repeats stage;
    # `earlier_checked` lists the ids already sent to its match call, so a resume does not ask again.
    # A match is not a repeat: the finding stays listed.
    earlier_decisions: dict[str, EarlierDecisionMatch] = Field(default_factory=dict)
    earlier_checked: list[str] = Field(default_factory=list)
    # The assess stage's label per shown finding, by id. `assess_answer` is the
    # agent's answer, stored as it arrives (empty when the call failed), so a
    # resume does not ask twice.
    assessments: dict[str, Assessment] = Field(default_factory=dict)
    assess_answer: AssessOutput | None = None
    # The recheck stage's status per open earlier finding, by earlier id, stored as
    # each answer arrives (the first one per id stands), so a resume asks only for
    # the rest. `earlier_status` is the final list the assess stage's reconcile
    # writes; `lineage` maps a finding id to the id of its first appearance.
    recheck_answers: dict[str, EarlierStatus] = Field(default_factory=dict)
    earlier_status: list[EarlierStatus] = Field(default_factory=list)
    lineage: dict[str, str] = Field(default_factory=dict)
    original_severity: dict[str, str] = Field(
        default_factory=dict
    )  # lowered findings only
    errors: list[str] = Field(
        default_factory=list
    )  # agent failures, shown in the review header
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
    # The write stage's answer per shown finding, by id, stored as each call returns, so a resume
    # asks only for the rest; `write_failed` lists the ids whose answer failed `write_gate` twice,
    # which are never asked again and fall back to their claim.
    write_answers: dict[str, WriteAnswer] = Field(default_factory=dict)
    write_failed: list[str] = Field(default_factory=list)
    # One entry per run of this round, review or resume, appended when it starts and
    # never rewritten; review-viewer's export finds the sessions' transcripts by it.
    # Empty in checkpoints written before 0.111.
    runs: list[RunStart] = Field(default_factory=list)
    # This round's scope (`Scope`): None in a tree, dir or range review's state, and in checkpoints
    # written before 0.115, which reviewed every file.
    scope: Scope | None = None
    # The `answers` command (0.124+): each saved output's match by its id (`N1`, …), the hashes of
    # the outputs already applied, and the findings whose text it rewrote since their verdict was
    # last dropped. Empty in a review it never ran on.
    answer_matches: dict[str, AnswerMatch] = Field(default_factory=dict)
    answers_applied: list[str] = Field(default_factory=list)
    answers_rewritten: list[str] = Field(default_factory=list)

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


class ConvertItem(_Model):
    kind: Literal["defect", "advice"] = "defect"
    severity: Severity | None = None
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    claim: str
    failure_scenario: str = ""
    citations: list[Citation] = Field(default_factory=list)
    defect_class: str = ""
    other_locations: list[str] = Field(default_factory=list)
    pre_existing: bool = False  # the report marks it `Pre-existing: yes`

    @field_validator("defect_class", mode="before")
    @classmethod
    def _class_text(cls, value):
        return "" if value is None else str(value).strip()

    @field_validator("other_locations", mode="before")
    @classmethod
    def _locations_list(cls, value):
        return locations_list(value)

    @field_validator("pre_existing", mode="before")
    @classmethod
    def _pre_existing_flag(cls, value):
        return bool(value) if value is not None else False


class ConvertOutput(_Model):
    """The convert call's answer: one item per defect or advice in the report, and what it could not check."""

    items: list[ConvertItem] = Field(default_factory=list)
    needs: list[NeedAsk] = Field(default_factory=list)


class AnswerMatchItem(_Model):
    id: str
    finding_ids: list[str] = Field(default_factory=list)
    note: str = ""


class AnswerMatchOutput(_Model):
    """The `answers` match call's answer: per saved output, the findings it bears on."""

    outputs: list[AnswerMatchItem] = Field(default_factory=list)


class MergeOutput(_Model):
    duplicate_sets: list[DuplicateSet] = Field(default_factory=list)


class RepeatMatch(_Model):
    id: str
    rejected_id: str
    reason: str = ""


class RepeatsOutput(_Model):
    matches: list[RepeatMatch] = Field(default_factory=list)


class EarlierMatch(_Model):
    id: str
    decision_id: str
    reason: str = ""


class EarlierMatchesOutput(_Model):
    matches: list[EarlierMatch] = Field(default_factory=list)


def worst_status(*statuses: str) -> str:
    """The worst of *statuses* in `EARLIER_STATUSES` order."""
    return min(statuses, key=EARLIER_STATUSES.index)


class RecheckOutput(_Model):
    """The recheck agent's answer about one earlier finding."""

    status: EarlierStatusWord
    reason: str = ""
    citations: list[Citation] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def _known_status(cls, value):
        """An unknown status is `cannot_verify`, never `resolved` (buildme's `PriorAssessment` rule):
        an agent that answers in its own words must not close a finding, nor fail the parse."""
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        return text if text in EARLIER_STATUSES else "cannot_verify"


class AssessItem(_Model):
    id: str
    label: str
    reason: str = ""
    earlier_id: str = ""


class AssessOutput(_Model):
    assessments: list[AssessItem] = Field(default_factory=list)
    duplicate_sets: list[DuplicateSet] = Field(default_factory=list)


Merged.model_rebuild()
ReviewState.model_rebuild()
