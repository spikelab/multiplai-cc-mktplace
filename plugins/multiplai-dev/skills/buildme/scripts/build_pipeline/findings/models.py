"""Data the findings stage passes between its steps.

`PRComment` is one comment read from the PR. Its `body` is untrusted text:
nothing reaches a model except `fenced_body()`, which defangs it and wraps it
in an `<untrusted-content>` fence. `Triage` is what the triage agent answers
for one allowed comment, and `TriageRecord` pairs the two for
`findings-r<N>.md`.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from multiplai_core.untrusted import fence
from pydantic import BaseModel, Field


class Verdict(str, Enum):
    """What a comment is, decided by the triage agent."""

    CODE_FIX = "code_fix"  # a defect in the code; fixed with a failing test first
    SPEC_CHANGE = "spec_change"  # the requirement itself is wrong or missing
    QUESTION = "question"  # asks something; answered, nothing changed
    NOT_DEFECT = "not_defect"  # the code is right as it is; the reason is replied


class PRComment(BaseModel):
    """One comment on the PR: a review-thread comment or a top-level comment."""

    id: int  # GitHub's numeric id (`databaseId` for a review comment);
    # made up and negative for a review_body (`fetch.review_body_id`)
    # In a review thread, a top-level (issue) comment, or part of a review's
    # own body (no thread: answered like a top-level comment).
    kind: Literal["review", "issue", "review_body"]
    author: str
    is_bot: bool = False
    body: str = ""  # untrusted
    url: str = ""
    created_at: str = ""
    path: str | None = None
    line: int | None = None
    thread_id: str | None = None  # GraphQL id of the review thread
    reply_to: int | None = None  # the thread's first comment: where a reply goes
    outdated: bool = False
    finding_id: str | None = None  # from the review's hidden marker, when present
    review_id: int | None = None  # for a review_body comment: the review's id
    # Set by fetch.prepare():
    allowed: bool = False
    injection: bool = False

    @property
    def key(self) -> str:
        """The row this comment belongs to in `findings-r<N>.md`: one row per
        review finding across rounds, else one per comment."""
        return f"finding:{self.finding_id}" if self.finding_id else f"comment:{self.id}"

    @property
    def label(self) -> str:
        """How the comment is named to a reader when it has no finding id
        (a review-body comment's `id` is made up; see `fetch.review_body_id`)."""
        if self.kind == "review_body":
            return f"review {self.review_id}"
        return f"comment {self.id}"

    @property
    def location(self) -> str:
        if self.path and self.line:
            return f"{self.path}:{self.line}"
        return self.path or "(PR conversation)"

    def fenced_body(self) -> str:
        """The body, defanged and fenced, labelled with the comment's URL."""
        lines = fence(self.body, source=self.url or self.label)
        return "\n".join(lines) if lines else "(empty comment)"


class Triage(BaseModel):
    """The triage agent's structured answer for one comment."""

    verdict: Verdict
    reason: str = Field(description="One paragraph: why this verdict.")
    locations: list[str] = Field(
        default_factory=list,
        description="Affected `path:line` or `path:start-end` ranges, relative to the project.",
    )
    failing_test: str = Field(
        default="",
        description="For code_fix: the behaviour a failing test must show. Empty otherwise.",
    )
    spec_conflict: bool = Field(
        default=False,
        description="For code_fix: true when the fix contradicts a requirement in the "
        "change's specs, so the spec must change first.",
    )
    requirement: str = Field(
        default="",
        description="For spec_change, or a code_fix with spec_conflict: the requirement "
        "that must change (`<capability>: <requirement name>`), when known.",
    )


class TriageRecord(BaseModel):
    """One comment as listed in `findings-r<N>.md`."""

    round: int
    comment: PRComment
    triage: Triage | None = None  # None: not allowed, flagged, or not triaged
    note: str = ""  # why there is no triage, when there is none

    @property
    def acted_on(self) -> bool:
        return self.triage is not None


class SpecIssue(BaseModel):
    """One requirements change a finding asks for, recorded as a `## ` section
    of the change's `spec-issues.md` for `pm-spec revise`, which makes the
    edit. buildme never edits requirements."""

    finding_key: str
    heading: str  # the section's `## ` heading
    issues_file: str  # spec-issues.md, relative to the project
    command: str  # the pm-spec command that makes the edit
    requirement: str = ""  # as triage named it: `<capability>: <requirement name>`
    requirements_file: str = (
        ""  # `specs.md#<cap>` or `requirements/<cap>.md`, when the change has it
    )
    defers_fix: bool = False  # a code_fix whose fix waits on this change
    appended: bool = True  # False: the file already held this finding's section
    # A deferred code fix (`defers_fix`): its comment, and the `.change.yaml`
    # `spec_revision` when it was deferred. Once `pm-spec revise` has cleared
    # `spec_issues` and moved the revision, a later round triages the comment
    # again, once, and saves the revision it read as `retried_at_revision`.
    deferred_comment_id: int | None = None
    deferred_at_revision: str | None = None
    retried_at_revision: str | None = None
