"""The run's saved state: `<change_dir>/.pm-spec/state.json`.

The run folder `.pm-spec/` sits inside the change dir and holds a `.gitignore`
of `*`, so nothing in it is ever committed with the change: `state.json`,
`progress.md` (append-only, for a person), `run.lock` and, while a rewrite is
being published, `publication.json`.

Saved with `multiplai_core.checkpoint.save_model` (a temp file, then
`os.replace`), so a kill mid-save leaves the previous state whole.

What each field is for, by the plan's stop-and-resume rows:

- `interview_waiting` / `interview_done`: the ticket interview's wait.
- `split`: the components answer (row S5), saved before any of its files is
  written; `single` is one component, kept in the change dir.
  `proposal_inputs` is `epic.proposal_hash` when it was saved: a resume
  before stage 3 that finds the proposal, the ticket or the interview changed
  since asks for the components again (`epic.reopen_if_proposal_changed`).
  A state saved before 0.15.2 has `""`.
- `artifacts`: per file written (the proposal, the use cases, each
  `specs.md`), the hash of its inputs and of the text it wrote. Informational:
  a file on disk is kept whatever its record says (rows S9, S11).
- `steps`: one `StepOutcome` per stage, `proposal`, `components`,
  `prototype`, `tickets` and `specs` (rows S1-S7), and per checked step with a
  one-rewrite rule (`consistency`, `revise`). A review stop is `passed` once
  pushed, `skipped` under `--auto`; `tickets` is `pending` at the ticket wait.
  A run started before the stages existed gets each of them `skipped`, reason
  "before the staged flow" (row S14).
- `component_checks`: one saved result per component of the component check
  (rows S1-S10 of its table), with the digest of the files it read.
- `checked_digests`: row S40, what the last consistency check read, so the
  next one reads in full only what changed.
- `handoff`: row S8, the revision the ready record was written at, whether its
  commit landed and whether the branch was pushed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from multiplai_core import checkpoint
from pydantic import BaseModel, ConfigDict, Field

STATE_FILE = "state.json"
PROGRESS_FILE = "progress.md"


class Unreadable(Exception):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            f"{path} cannot be read as a pm-spec state; nothing was written. "
            "Move it aside to start the change's run again."
        )


class Options(BaseModel):
    """What the run was started with, kept so a resume runs the same way."""

    model_config = ConfigDict(extra="forbid")

    interview_mode: Literal["self", "user", "ticket"] = "user"
    auto: bool = False
    prototype: Literal["auto", "true", "false"] = "auto"
    # False (`--no-epic-split`): the components answer is one component.
    epic_split: bool = True
    ticket_ref: str = ""
    ticket_url: str = ""
    ticket_children: list[str] = Field(default_factory=list)


class ContextFile(BaseModel):
    name: str
    content: str


class ArtifactRecord(BaseModel):
    inputs: str
    output: str | None = None


class StepOutcome(BaseModel):
    """One checked step. `revision` is the step's input revision when it was
    recorded. `repair_findings` are the findings its one rewrite was asked to
    fix, numbered, with a status each once a recheck ran. `rewrite_published`
    is set in the checkpoint that publishes the rewrite. `blocked_revision` is
    the revision a block on findings was recorded at: a resume at another
    revision means the user edited the files, and the step gets its rewrite back.
    `blocked_dependencies` is `layout.dependency_digest` at that block: an edit
    to a component's `depends_on` or to `components.md` alone also counts as
    an edit (row S58); a state saved before 0.14.0 has none, and only the
    revision is compared. `reviewed_blocks` counts the checks after the specs
    review that blocked, for the round cap (`consistency.refuse_at_round_cap`)."""

    model_config = ConfigDict(extra="forbid")

    revision: str = ""
    outcome: Literal["pending", "passed", "blocked", "skipped"] = "pending"
    reason: str = ""
    repair_attempts: int = 0
    repair_findings: list[dict] = Field(default_factory=list)
    rewrite_published: bool = False
    blocking_findings: list[dict] = Field(default_factory=list)
    advisory_findings: list[dict] = Field(default_factory=list)
    blocked_revision: str | None = None
    blocked_dependencies: str | None = None
    source: str = ""
    reviewed_blocks: int = 0


class ComponentCheck(BaseModel):
    """One component's saved component-check result. `digest` is the sha256
    over the files it read (`component_check.digest`); a result whose digest
    still matches is reused with no call. `findings` and `prior_assessments`
    are the agent's answer as it gave them."""

    model_config = ConfigDict(extra="forbid")

    digest: str
    findings: list[dict] = Field(default_factory=list)
    prior_assessments: list[dict] = Field(default_factory=list)
    cost_usd: float = 0.0


class EpicChild(BaseModel):
    id: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    plan: str = ""
    ticket_ref: str = ""


class SplitRecord(BaseModel):
    inputs: str
    single: bool
    proposal_inputs: str = ""
    reason: str = ""
    children: list[EpicChild] = Field(default_factory=list)


class Handoff(BaseModel):
    revision: str
    committed: bool = False
    commit: str | None = None
    # The feature branch was pushed after the commit (or there was nothing to
    # push: a build's worktree). A state saved before pm-spec pushed reads False.
    pushed: bool = False


class RunState(BaseModel):
    # A field this version does not know is refused, not dropped: re-saving
    # would otherwise lose what a newer version saved.
    model_config = ConfigDict(extra="forbid")

    change: str
    options: Options = Field(default_factory=Options)
    parent: str | None = None
    intake_done: bool = False
    # `--interview-summary` and `--context-files` as given to `new`, so a
    # resume that writes the interview later still has them.
    summary: str = ""
    contexts: list[ContextFile] = Field(default_factory=list)
    interview_waiting: bool = False
    interview_done: bool = False
    split: SplitRecord | None = None
    artifacts: dict[str, ArtifactRecord] = Field(default_factory=dict)
    steps: dict[str, StepOutcome] = Field(default_factory=dict)
    handoff: Handoff | None = None
    budget: dict | None = None
    # A change whose specs pm-spec did not write, taken as they stand: only
    # the consistency check and the handoff run on it.
    adopted: bool = False
    # Consistency findings a person accepted with `resume --accept`: each saved
    # finding with its `reason` and `accepted_at`. Kept across checks, so a
    # later check is told not to raise them again.
    accepted_findings: list[dict] = Field(default_factory=list)
    # The sha256 of each document the last finished consistency check read
    # (`consistency.document_digests`: the proposal, the use cases, each plan
    # and each spec file, by path). The next check reads in full only the spec
    # files changed since, and the files they cite. Cleared before each check
    # and saved with its outcome, so a check stopped before its outcome was
    # saved leaves none, and the next check reads everything (row S40). None
    # (no check yet, or a state saved before 0.15.0): a full check.
    checked_digests: dict[str, str] | None = None
    # The component check's result per component id (the change name for a
    # change with no components), each saved as it arrives.
    component_checks: dict[str, ComponentCheck] = Field(default_factory=dict)

    def step(self, name: str) -> StepOutcome:
        return self.steps.setdefault(name, StepOutcome())


def ensure_run_dir(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    ignore = run_dir / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("*\n")


def load(run_dir: Path) -> RunState | None:
    path = run_dir / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return None
    state = checkpoint.load_model(path, RunState)
    if state is None:
        raise Unreadable(path)
    return state


def save(state: RunState, run_dir: Path, *, ledger: bool = True) -> None:
    """Save *state*, with the current run's spend unless *ledger* is False (a
    child change's first state, which starts with a ledger of its own)."""
    from . import sdk

    if ledger:
        state.budget = sdk.ledger().to_state()
    ensure_run_dir(run_dir)
    checkpoint.save_model(state, run_dir, STATE_FILE)
