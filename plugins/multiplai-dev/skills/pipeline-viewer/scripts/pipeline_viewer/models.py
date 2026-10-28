"""The shapes the server sends and the mailbox rows it writes.

`ChangeStatus` is what `status.change_status` returns and what `/api/change`
and `/api/changes` send; `API.md` restates it for the page. Every model here
is output only: built from files and git, never parsed from a request.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from viewer_kit.people import Person

StopState = Literal[
    "not_reached",
    "waiting",
    "acknowledged",
    "approved",
    "decided_by_agent",
    "rework_requested",
]
RowKind = Literal["ask", "comment", "ack", "approve", "rework", "timeout"]
RecordKind = Literal["comment", "ack", "approve", "rework", "decided"]

STOP_LABELS = {
    1: "proposal",
    2: "use cases and components",
    3: "specs",
    4: "implementation plan",
    5: "pull request",
}
# Stops a person can approve here. Stop 5 is approved on the pull request.
APPROVABLE_STOPS = (1, 2, 3, 4)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reviewer(_Out):
    name: str
    role: Literal["pm", "engineer"]
    # False when the name is in config but not (yet) in people.json.
    invited: bool = True


class DocRef(_Out):
    """One document of a stop, relative to the change dir."""

    path: str
    # True: the stop's approval covers it. False: shown for reading only
    # (`tasks.md` at stop 4).
    approve: bool = True
    exists: bool = True


class Record(_Out):
    """A decision commit on the change's branch (`record.py` writes them)."""

    sha: str
    kind: RecordKind
    stop: int
    by: str
    at: str
    subject: str
    revision: str | None = None
    reason: str | None = None


class StopStatus(_Out):
    n: int
    label: str
    state: StopState
    reviewers: list[Reviewer] = Field(default_factory=list)
    # Names still expected to act: every reviewer of a role that has not
    # approved, while the stop is waiting or acknowledged.
    waiting_on: list[str] = Field(default_factory=list)
    waiting_since: str | None = None
    # When a reminder goes out if nobody acknowledged (`ack_timeout_minutes`).
    ack_by: str | None = None
    # When the agent decides alone; null once acknowledged (an
    # acknowledgement holds the wait), and for stop 5.
    decide_at: str | None = None
    acknowledged_by: list[str] = Field(default_factory=list)
    approved_by: list[str] = Field(default_factory=list)
    decided_by: str | None = None
    rework_by: list[str] = Field(default_factory=list)
    # What an approval names in `Revision:`: a hash of the stop's committed
    # documents (stops 1-3), buildme's plan revision (stop 4).
    revision: str | None = None
    docs: list[DocRef] = Field(default_factory=list)
    records: list[Record] = Field(default_factory=list)
    pr_url: str | None = None


class PmSpecStatus(_Out):
    steps: dict[str, str] = Field(default_factory=dict)
    interview_waiting: bool = False
    spec_status: str | None = None
    handoff_revision: str | None = None
    # `.pm-spec/run.lock` is held: pm-spec is running on this change now.
    running: bool = False
    progress_tail: list[str] = Field(default_factory=list)


class BlockRow(_Out):
    number: int
    name: str
    status: str
    review_iterations: int = 0
    timed_out: bool = False
    # A fix-on-pass is in flight for this block.
    fix_pass: bool = False
    test_commit: str | None = None
    impl_commit: str | None = None
    refactor_commit: str | None = None
    red_evidence: str = ""
    green_evidence: str = ""
    review_rounds: list[str] = Field(default_factory=list)


class BuildStatus(_Out):
    phase: str | None = None
    complete: bool = False
    started_at: str | None = None
    updated_at: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    pr_url: str | None = None
    failed_stop: bool = False
    spent_usd: float | None = None
    max_usd: float | None = None
    blocks: list[BlockRow] = Field(default_factory=list)
    blocks_done: int = 0
    blocks_total: int = 0
    elapsed_s: float | None = None
    board: dict | None = None
    progress_tail: list[str] = Field(default_factory=list)


class EpicChildRow(_Out):
    id: str
    status: str
    pr: str | int | None = None
    rounds: int = 0
    reason: str = ""


class EpicAgent(_Out):
    n: int
    kind: str
    child: str
    started_at: str | None = None
    exit_code: int | None = None
    finished: bool = False


class EpicStatus(_Out):
    run: str
    children: list[EpicChildRow] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    spent_usd: float | None = None
    agents: list[EpicAgent] = Field(default_factory=list)


class ChangeStatus(_Out):
    name: str
    branch: str | None = None
    # The stop the change is at: the first reached stop not yet approved or
    # decided, else the last reached one; null before stop 1.
    current_stop: int | None = None
    waiting_on: list[str] = Field(default_factory=list)
    stops: list[StopStatus] = Field(default_factory=list)
    pm_spec: PmSpecStatus | None = None
    build: BuildStatus | None = None
    epic: EpicStatus | None = None
    # The branch's checkout has commits its upstream lacks (a failed push).
    unpushed: bool = False
    # Where review writes go; false when no checkout holds the branch.
    writable: bool = False


class InboxRow(_Out):
    """A row the server (or `deadlines`) appends for the session."""

    v: int = 1
    id: str
    ts: str
    kind: RowKind
    change: str
    stop: int | None = None
    doc: str | None = None
    section: str | None = None
    text: str = ""
    revision: str | None = None
    commit: str | None = None
    author: Person | None = None


class OutboxRow(_Out):
    v: int = 1
    reply_to: str
    ts: str
    text: str
    done: bool = True
    # A commit the answer made (an `ask` that asked for an edit).
    commit: str | None = None
