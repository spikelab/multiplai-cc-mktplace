"""Build state management with checkpointing and resume.

Hierarchical state: orchestrator → spec generation → TDD → per-block.
Serialized to JSON after every significant transition.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, model_validator

from . import budget
from .findings.models import PRComment, SpecChange, Triage
from .models import BlockInfo, BlockStatus, BuildPhase
from .stop_record import StepStop  # noqa: F401  (moved; `from .state import StepStop` keeps working)
from .stop_record import record as record_stop

log = logging.getLogger(__name__)


class StepOutcome(BaseModel):
    revision: str
    outcome: Literal["passed", "blocked", "advisory", "skipped"]
    reason: str = ""
    repair_attempts: int = 0
    blocking_findings: str = ""


class SpecGenState(BaseModel):
    """State for the spec generator sub-pipeline."""
    revision: str = ""
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    stale_artifacts: list[str] = Field(default_factory=list)
    steps: dict[str, StepOutcome] = Field(default_factory=dict)
    expected_capabilities: list[str] = Field(default_factory=list)
    requirements_source: str = ""
    requirements_members: dict[str, str] = Field(default_factory=dict)
    completed_artifacts: list[str] = Field(default_factory=list)
    research_path: str | None = None
    codebase_analysis_path: str | None = None
    # The commit buildme's spec-extract step reads, saved before the launch so a
    # resume reaches the same spec-extract run folder (the plan's rows S1, S5)
    # and runs even when the registry now holds the files that run applied (S3).
    spec_extract_sha: str | None = None
    @model_validator(mode="before")
    @classmethod
    def migrate_repair_admission(cls, value):
        # Legacy done booleans cannot prove a revision passed. A spent repair
        # allowance, however, must survive upgrading or interruption.
        if isinstance(value, dict):
            value = dict(value)
            steps = dict(value.get("steps", {}))
            for name in ("tasks_audit", "design_audit", "plan_review"):
                if value.pop(name + "_regen_done", False):
                    existing = steps.get(name)
                    step = existing.model_dump() if isinstance(existing, StepOutcome) else dict(existing or {})
                    steps[name] = {"revision": "", "outcome": "blocked",
                                   "reason": "Legacy repair already admitted; validation required",
                                   **step, "repair_attempts": max(1, step.get("repair_attempts", 0))}
            value["steps"] = steps
        return value

    def repair_attempts(self, name: str) -> int:
        step = self.steps.get(name)
        return step.repair_attempts if step else 0

    def admit_repair(self, name: str) -> None:
        if self.repair_attempts(name):
            raise RuntimeError(f"{name} repair already attempted; resolve blocking findings before resuming")
        self.record_step(name, "blocked", "Repair admitted; validation pending")
        self.steps[name].repair_attempts = 1

    def step_revision(self, name: str) -> str:
        keys = {
            "explainers": {"proposal.md", "design.md", "config.yaml", "references"},
            "tasks_audit": {"tasks.md", "design.md", "proposal.md", "use-cases.md", "unknowns.md", "config.yaml", "references", "granularity"},
            "prototype": {"proposal.md", "design.md"},
            "design_audit": {"proposal.md", "design.md", "tasks.md", "config.yaml", "references"},
        }.get(name)
        if keys is None:
            return self.revision
        inputs = {k: v for k, v in self.artifact_hashes.items() if k in keys or k.startswith("requirements/")}
        return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()

    def step_passed(self, name: str) -> bool:
        step = self.steps.get(name)
        accepted = {"passed", "skipped", "advisory"} if name in {"prototype", "explainers"} else {"passed"}
        return bool(step and step.revision == self.step_revision(name) and step.outcome in accepted)

    def record_step(self, name: str, outcome: Literal["passed", "blocked", "advisory", "skipped"] = "passed", reason: str = "", *, blocking_findings: str = "") -> None:
        self.steps[name] = StepOutcome(revision=self.step_revision(name), outcome=outcome, reason=reason,
                                       repair_attempts=self.repair_attempts(name), blocking_findings=blocking_findings)



class TDDState(BaseModel):
    """State for the TDD engine sub-pipeline."""
    blocks: list[BlockInfo] = Field(default_factory=list)
    current_block: int = 0
    baseline_tests_pass: bool = False
    # The whole-change refactor pass runs after the block loop and commits its
    # own result, so nothing on disk records that it happened — a resume would
    # otherwise re-run it (and pay for it) every time. Old checkpoints default
    # to False, which re-runs a pass that is idempotent by construction.
    refactor_all_done: bool = False
    # The MUTATION sub-step (cosmic-ray over the build's changed lines) runs
    # after the whole-change refactor and commits its own survivor-round tests
    # and mutation.md, so nothing on disk says it passed — without the flag a
    # resume would re-run every mutant. Set only when the score met
    # `mutation.min_score` (or the step was skipped); a failed step stays False
    # so a resume runs it again. Old checkpoints default to False.
    mutation_done: bool = False
    e2e_done: bool = False


FINDINGS_STEPS = ("fetch", "triage", "spec", "code", "publish", "review")


class FindingsRound(BaseModel):
    """One round of `python -m build_pipeline findings`. Each step in
    `FINDINGS_STEPS` is added to `started` before it runs and to `finished`
    after, so a resume restarts at the first unfinished step."""

    number: int
    since: str | None = None                   # top-level comments after this are new
    fetched_at: str | None = None              # when this round's fetch started: the next round's `since`
    started: list[str] = Field(default_factory=list)
    finished: list[str] = Field(default_factory=list)
    step_heads: dict[str, str] = Field(default_factory=dict)  # step → HEAD when it started
    comments: list[PRComment] = Field(default_factory=list)   # new this round, as fetched
    triaged: dict[int, Triage] = Field(default_factory=dict)  # comment id → verdict
    delta_path: str | None = None
    spec_changes: list[SpecChange] = Field(default_factory=list)
    spec_commit: str | None = None
    tasks_path: str | None = None
    engine_state_path: str | None = None
    blocks: dict[int, str] = Field(default_factory=dict)      # block number → row key
    fixes: dict[str, dict[str, str]] = Field(default_factory=dict)  # row key → commits
    # Why the TDD engine stopped after its block loop (a whole-change step
    # after the blocks failed). Legacy diagnostic only: failed required checks
    # now stop the round before publication.
    code_check_failed: str | None = None
    # HEAD right after the code step's records commit, before any fix: the
    # base of the diff the self-check reads. Set once, never overwritten.
    code_base: str | None = None
    # The self-check of the round's fix commits, saved once it answered:
    # {"head": HEAD it read up to, "report": the problems ("" for none),
    # "block": the block number the report became (None for none)}. A saved
    # check never runs again. None until then, and in older checkpoints.
    self_check: dict | None = None
    pushed_head: str | None = None
    body_updated: bool = False
    replies_sent: list[str] = Field(default_factory=list)     # reply keys, in order sent
    review_run_dir: str | None = None
    review_waiting: bool = False               # told the human to accept and post
    review_finished_at: str | None = None
    # step → what the tree held when that step last stopped. Removed when
    # the step starts again, so a step killed before it could record its
    # stop has none. Empty in checkpoints written before the field existed.
    stops: dict[str, StepStop] = Field(default_factory=dict)

    @property
    def seen_ids(self) -> set[int]:
        return {c.id for c in self.comments}


class FindingsState(BaseModel):
    """The findings rounds on one PR. Saved on its own, as a `BuildState`
    with only this field set, in `<change>/.findings-state.json`: the build's
    `.build-state.json` is never recreated."""

    pr: int
    repo: str
    branch: str
    change_dir: str
    rounds: list[FindingsRound] = Field(default_factory=list)

    def seen_ids(self) -> set[int]:
        out: set[int] = set()
        for r in self.rounds:
            out |= r.seen_ids
        return out


COMPLETED_SUFFIX = ".complete.json"


def completed_state_path(state_path: Path) -> Path:
    """Where a succeeded run keeps *state_path*: ``.build-state.json`` →
    ``.build-state.complete.json``."""
    stem = state_path.name[:-len(".json")] if state_path.name.endswith(".json") else state_path.name
    return state_path.with_name(stem + COMPLETED_SUFFIX)


class BuildResult(BaseModel):
    kind: Literal["pending", "spec-only", "verified-code", "historical"]
    verification: dict | None
    publication: str


class BuildState(BaseModel):
    """Root state for the entire build pipeline."""

    schema_version: int = 1
    verification: dict | None = None
    publication: str = "pending"
    completion_kind: Literal["pending", "spec-only", "verified-code"] = "pending"
    publish_sections: list[str] = Field(default_factory=list)

    @property
    def result(self) -> BuildResult:
        verified = bool(self.verification and self.verification.get("passed"))
        kind = "verified-code" if verified else (
            "spec-only" if self.completion_kind == "spec-only"
            else "historical" if self.phase == BuildPhase.COMPLETE else "pending")
        return BuildResult(kind=kind, verification=self.verification, publication=self.publication)

    # Identity
    change_name: str
    mode: str  # scratch | brief | only
    tier: str  # advanced | standard

    # Phase tracking
    phase: BuildPhase = BuildPhase.INIT

    # Orchestrator state
    bootstrap_done: bool = False
    interview_summary: str | None = None
    research_path: str | None = None

    # Intake (--ticket-ref / --ticket-url / --plan-file). Recorded so a resume
    # and the wrapper know what the build was started from. All default to
    # None, so checkpoints written before intake existed still load.
    ticket_ref: str | None = None
    ticket_url: str | None = None
    plan_file: str | None = None
    # `interview: ticket` asked its questions on the ticket and is waiting for
    # replies. The pointer sits at INTERVIEW_DONE, which is_phase_complete
    # does not count as complete, so a resume re-enters the interview.
    # Default False for old checkpoints.
    interview_waiting: bool = False
    # The plan should be split and the build stopped after plan review; a
    # re-run stops the same way until --accept-size. Default False.
    split_required: bool = False
    # --accept-size was given once for this build. A retry without the flag
    # (a resume, or a FAILED build re-running every phase) keeps the
    # acceptance instead of stopping again. Default False.
    size_accepted: bool = False
    # Epic mode: the epic directory EPIC_SPLIT wrote (`<data_dir>/buildme/
    # epics/<owner>-<repo>--<change>/`), saved in the same checkpoint that
    # completes the phase. Set means the build is an epic: every later run
    # reads epic.json and never reaches spec generation. Default None.
    epic_dir: str | None = None
    # EPIC_SPLIT answered one change. A retry of a FAILED build (which re-runs
    # every phase) skips the step instead of asking again, since a second
    # answer of several would turn a change already being built into an
    # epic. Default False; a skip and `load` (row A.S14) set it too.
    epic_single: bool = False

    # Git lifecycle — set when the pipeline creates its own worktree/branch.
    # Persisted so a resume RE-BINDS to the existing worktree instead of
    # creating a second one. All four default to None, so a .build-state.json
    # written before the git lifecycle existed still loads.
    worktree_path: str | None = None
    branch: str | None = None
    source_repo: str | None = None
    pr_url: str | None = None
    # The commit the build's branch started from (the worktree's HEAD when it
    # was created). The stale-walkthrough check reads it to tell a walkthrough
    # this build made stale from one that was stale before it. None in older
    # checkpoints and for --no-worktree builds.
    base_sha: str | None = None
    # What the build's tree held when the build last failed (`fail`): HEAD and
    # every uncommitted path with its content. A re-run of the FAILED build
    # reuses its worktree and refuses an uncommitted change this record does
    # not hold (`orchestrator._run_bootstrap`). None in older checkpoints and
    # when git could not be asked; the re-run then refuses any uncommitted
    # change. The re-run's bootstrap records its tree here again once it is
    # done, so a stop before the interview finishes (pointer AT BOOTSTRAP)
    # resumes in place too; any other bootstrap clears it.
    failed_stop: StepStop | None = None

    # Documentation files the DOCS_UPDATE phase reported writing (empty when it
    # updated nothing). Persisted because the PR body is written later — after
    # the archive move, and possibly in a *resumed* process — so an in-memory
    # value would silently drop the "docs updated" line on any resume. Old
    # checkpoints default to [].
    docs_impact: list[str] = Field(default_factory=list)

    # Sub-pipeline state
    spec_gen: SpecGenState | None = None
    tdd: TDDState | None = None
    # Review-comment rounds after the build (`python -m build_pipeline
    # findings`). Not a BuildPhase: they track their own steps. None in every
    # build checkpoint, including those written before the field existed.
    findings: FindingsState | None = None

    # Cumulative spend, snapshotted on every checkpoint. A resumed build
    # restores it (see run_tdd_engine) — otherwise resume would hand a runaway
    # build a fresh budget and the ceiling would never bind.
    budget: dict = Field(default_factory=dict)

    # Checkpointing
    state_file: str = ""
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def checkpoint(self, path: Path | None = None, *, keep_budget: bool = False) -> None:
        """Serialize state to disk, atomically.

        The `budget` field is replaced with this process's spend, unless
        *keep_budget*: a caller that edits a checkpoint it loaded, outside the
        run that owns it, passes it so the spend the file holds survives.

        The checkpoint is the build's only crash-recovery record, and it is
        rewritten after every block phase — so a truncated write is a real
        failure mode, and one there is no recovery from: `BuildState.load`
        raises on a half-written file and the orchestrator has nothing else to
        resume from. Write to a temp file in the SAME directory (so `os.replace`
        stays within one filesystem and is therefore atomic) and rename over the
        target. A crash then leaves either the previous checkpoint or the new
        one, never a partial one; a serialization failure leaves the previous
        checkpoint intact and no stray temp file.
        """
        target = path or Path(self.state_file)
        if not target.name:
            return
        if not keep_budget:
            self.budget = budget.get_budget().to_state()
        self.updated_at = datetime.now(timezone.utc).isoformat()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(self.model_dump_json(indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        log.debug("State checkpointed to %s", target)

    @classmethod
    def load(cls, path: Path) -> BuildState:
        """Load state from a JSON file."""
        data = json.loads(path.read_text())
        if data.get("schema_version", 0) > 1:
            raise ValueError("Checkpoint version is newer than this Buildme; upgrade before resuming")
        state = cls.model_validate(data)
        if "epic_single" not in data and state.epic_dir is None and (
                state.phase == BuildPhase.FAILED or state.is_phase_complete(BuildPhase.EPIC_SPLIT)):
            state.epic_single = True
        log.info("Resumed state from %s (phase=%s)", path, state.phase)
        return state

    def advance_to(self, phase: BuildPhase, path: Path | None = None) -> None:
        """Advance to a new phase and checkpoint."""
        self.phase = phase
        self.checkpoint(path)
        log.info("Advanced to phase: %s", phase.value)

    def fail(self, project_dir: Path | str, path: Path | None = None) -> None:
        """Move to FAILED and checkpoint, recording first what the tree in
        *project_dir* holds (`failed_stop`, `stop_record.record`)."""
        self.failed_stop = record_stop(project_dir)
        self.advance_to(BuildPhase.FAILED, path)

    def complete(self, path: Path | None = None) -> Path:
        """Keep the checkpoint of a run that succeeded, under its completed name.

        Writes the state as it is now (phase and spend), then renames
        ``.build-state.json`` to ``.build-state.complete.json`` in the same
        directory, atomically. Resume looks only for ``.build-state.json``, so
        the completed file is never resumed; it stays as the record of what the
        build did and spent, and lets a re-run of a published build ask first
        (``orchestrator._completed_builds``). Returns the completed file's path.
        """
        target = path or Path(self.state_file)
        self.checkpoint(target)
        done = completed_state_path(target)
        os.replace(target, done)
        log.info("Checkpoint kept as %s (phase=%s)", done, self.phase)
        return done

    def is_phase_complete(self, phase: BuildPhase) -> bool:
        """Check if a phase has already been completed (for resume).

        FAILED is not a completion state — if the build failed, no phases
        count as complete so the pipeline retries from the beginning.
        """
        if self.phase == BuildPhase.FAILED:
            return False
        phase_order = list(BuildPhase)
        return phase_order.index(self.phase) > phase_order.index(phase)

    # --- TDD helpers ---

    def current_block(self) -> BlockInfo | None:
        if self.tdd and self.tdd.current_block < len(self.tdd.blocks):
            return self.tdd.blocks[self.tdd.current_block]
        return None

    def advance_block(self, path: Path | None = None) -> None:
        if self.tdd:
            self.tdd.current_block += 1
            self.checkpoint(path)

    def mark_block_status(self, block_idx: int, status: BlockStatus, path: Path | None = None) -> None:
        if self.tdd and block_idx < len(self.tdd.blocks):
            self.tdd.blocks[block_idx].status = status
            self.checkpoint(path)

    def all_blocks_done(self) -> bool:
        if not self.tdd:
            return False
        return all(b.status == BlockStatus.DONE for b in self.tdd.blocks)
