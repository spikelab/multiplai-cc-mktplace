"""Build orchestrator — main pipeline for /buildme.

Three modes:
  - scratch: interview → research → specs → design audit → review → build
  - brief: load docs → interview → research → specs → design audit → review → build
  - only: verify specs → research check → build

The SKILL.md wrapper handles interactive phases (interview, plan review).
This module handles the non-interactive pipeline sequencing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from . import board, git_ops
from .change_manager import ChangeManager
from .config import BuildConfig
from .models import BoardColumn, BuildPhase
from .progress import ProgressWriter
from .state import BuildState, SpecGenState

log = logging.getLogger(__name__)


async def run_orchestrator(config: BuildConfig, args) -> int:
    """Main entry point for the build orchestrator."""
    # Resume re-binding, before anything derives a path from the config: a
    # previous run's state file lives INSIDE its worktree, so a plain
    # config.state_file_path() lookup against the source repo would miss it
    # and bootstrap would create a second worktree.
    _rebind_to_existing_worktree(config)

    # The checkpoint normally lives at specs/changes/<name>/.build-state.json,
    # but after an --auto archive with publish still pending it has traveled
    # with the change dir into specs/archive/ — _existing_state_path finds it
    # in either place.
    state_path = _existing_state_path(config) or config.state_file_path()
    progress = ProgressWriter(config.progress_file_path())

    # Resume or create state
    if state_path.exists():
        state = BuildState.load(state_path)
        log.info("Resumed build: %s (phase=%s)", config.change_name, state.phase)
    else:
        state = BuildState(
            change_name=config.change_name,
            mode=config.mode,
            tier=config.tier,
            state_file=str(state_path),
        )

    cm = ChangeManager(config.specs_dir)

    # Set the moment the build's outcome is decided as success (spec-only done,
    # or publish resolved). From then on the checkpoint is deliberately gone,
    # so a late exception (progress cleanup, stdout) must NOT be recorded as
    # Cancelled — the card's real column (e.g. In Review) already stands.
    build_succeeded = False

    try:
        # Phase: Bootstrap
        if not state.is_phase_complete(BuildPhase.BOOTSTRAP) and config.mode != "only":
            log.info("START phase=BOOTSTRAP change=%s mode=%s tier=%s", config.change_name, config.mode, config.tier)
            await _run_bootstrap(config, state, cm, state_path)
            # Bootstrap may have re-bound config.project_dir to the build's own
            # worktree — every path derived earlier is now stale.
            state_path = config.state_file_path()
            progress = ProgressWriter(config.progress_file_path())
            cm = ChangeManager(config.specs_dir)
            log.info("DONE phase=BOOTSTRAP")
            print("PHASE:BOOTSTRAP:COMPLETE", flush=True)
            # First board record of the run: the card is being shaped. Not
            # recorded any earlier — at INIT the change directory does not
            # exist yet, and creating it would suppress .change.yaml.
            board.record(config, state, BuildPhase.BOOTSTRAP, progress=progress)

        # Phase: Interview (handled by SKILL.md wrapper — we receive summary)
        if not state.is_phase_complete(BuildPhase.INTERVIEW_DONE) and config.mode != "only":
            interview_summary = getattr(args, "interview_summary", "")

            # Load context files (--context-files) and prepend to interview summary
            context_files = getattr(args, "context_files", []) or []
            if context_files:
                context_parts = []
                for cf_path in context_files:
                    cf = Path(cf_path)
                    if cf.exists():
                        content = cf.read_text()
                        context_parts.append(f"--- Context: {cf.name} ---\n{content}")
                        log.info("Loaded context file: %s (%d chars)", cf_path, len(content))
                    else:
                        log.warning("Context file not found: %s", cf_path)
                if context_parts:
                    loaded = "\n\n".join(context_parts)
                    interview_summary = f"{loaded}\n\n--- Interview Summary ---\n{interview_summary}" if interview_summary else loaded

            if interview_summary:
                log.info("START phase=INTERVIEW summary_len=%d", len(interview_summary))
                state.interview_summary = interview_summary
                state.advance_to(BuildPhase.INTERVIEW_DONE, state_path)
                log.info("DONE phase=INTERVIEW")
                print("PHASE:INTERVIEW:COMPLETE", flush=True)
                board.record(config, state, BuildPhase.INTERVIEW_DONE, progress=progress)
            elif not config.auto:
                log.warning("SKIP phase=INTERVIEW reason=no-summary-provided")
                state.advance_to(BuildPhase.INTERVIEW_DONE, state_path)

        # Phase: Research
        if not state.is_phase_complete(BuildPhase.RESEARCH):
            if config.skip_research:
                log.info("SKIP phase=RESEARCH reason=--skip-research")
                state.advance_to(BuildPhase.RESEARCH, state_path)
            elif config.mode == "only":
                log.info("START phase=RESEARCH_CHECK")
                await _run_research_check(config, state, cm, state_path)
            else:
                log.info("START phase=RESEARCH")
                research_path = getattr(args, "research_path", "")
                if research_path:
                    state.research_path = research_path
                state.advance_to(BuildPhase.RESEARCH, state_path)
            log.info("DONE phase=RESEARCH")
            print("PHASE:RESEARCH:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.RESEARCH, progress=progress)

        # Phase: Codebase Analysis — read the repo that already exists so the
        # design extends it instead of inventing a parallel structure. Runs
        # before spec generation because design.md is its only consumer.
        # Best-effort: a failure here costs grounding, never the build.
        if not state.is_phase_complete(BuildPhase.CODEBASE_ANALYSIS):
            log.info("START phase=CODEBASE_ANALYSIS")
            await _run_codebase_analysis_phase(config, state, progress)
            state.checkpoint(state_path)
            state.advance_to(BuildPhase.CODEBASE_ANALYSIS, state_path)
            log.info("DONE phase=CODEBASE_ANALYSIS")
            print("PHASE:CODEBASE_ANALYSIS:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.CODEBASE_ANALYSIS, progress=progress)

        # Phase: Spec Generation
        if not state.is_phase_complete(BuildPhase.SPEC_GENERATION) and not config.mode == "only":
            log.info("START phase=SPEC_GENERATION")
            from .spec_generator import run_spec_generator
            result = await run_spec_generator(config, args)
            if result != 0:
                log.error("FAIL phase=SPEC_GENERATION exit_code=%d", result)
                state.advance_to(BuildPhase.FAILED, state_path)
                board.record_failure(
                    config, state, "spec generation failed", progress=progress,
                )
                return result
            # Reload state from disk — run_spec_generator loaded its own copy
            # and checkpointed the spec_gen sub-state (completed artifacts, the
            # audit/gate completion flags). Advancing our stale copy would
            # write those back as null, and the design audit below would then
            # regenerate a second time because design_audit_regen_done was
            # lost. Same reason the TDD phase reloads.
            if state_path.exists():
                state = BuildState.load(state_path)
            # Only ever forwards. The reloaded copy may already sit at a LATER
            # phase than this one — run_spec_generator advances to DESIGN_AUDIT
            # before running its audit stage — and advance_to assigns
            # unconditionally, so an unguarded call here would rewind the
            # persisted pointer to SPEC_GENERATION. That re-opens the phases in
            # between (the DESIGN_AUDIT block below would re-enter and pay
            # another audit call) and leaves a checkpoint that lies about where
            # the build is.
            if not state.is_phase_complete(BuildPhase.SPEC_GENERATION):
                state.advance_to(BuildPhase.SPEC_GENERATION, state_path)
            # Shaping/planning lands as its own commit so the branch history
            # reads shaping → planning → implementation rather than one lump.
            # No-op when the pipeline does not own a branch (--no-worktree).
            git_ops.commit_stage(
                config,
                f"docs(specs): {config.change_name} — proposal, requirements, design, tasks",
                _spec_paths(config),
            )
            log.info("DONE phase=SPEC_GENERATION")
            print("PHASE:SPEC_GENERATION:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.SPEC_GENERATION, progress=progress)

        # Phase: Design Audit (best-effort — failures don't block the build).
        # The stage runs the audit AND folds critical/major gaps back into
        # design.md/tasks.md with one regeneration pass; the pass is recorded
        # in spec_gen.design_audit_regen_done so it happens at most once per
        # build no matter which call site reaches it first.
        if not state.is_phase_complete(BuildPhase.DESIGN_AUDIT):
            log.info("START phase=DESIGN_AUDIT")
            from .spec_generator import run_design_audit_stage
            gaps = await run_design_audit_stage(
                config.change_dir, config, state, state_path,
            )
            log.info("DONE phase=DESIGN_AUDIT gaps=%d", len(gaps) if gaps else 0)
            state.advance_to(BuildPhase.DESIGN_AUDIT, state_path)
            print("PHASE:DESIGN_AUDIT:COMPLETE", flush=True)
            # Spec'ing is done being shaped; the card is now being planned.
            board.record(config, state, BuildPhase.DESIGN_AUDIT, progress=progress)

        # Phase: Prototype — a cheap artifact that proves the shape before the
        # expensive TDD build. Phase failure is never build failure.
        if not state.is_phase_complete(BuildPhase.PROTOTYPE):
            await _run_prototype_phase(config, state, progress, state_path)
            state.advance_to(BuildPhase.PROTOTYPE, state_path)
            print("PHASE:PROTOTYPE:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.PROTOTYPE, progress=progress)

        # Stop if --spec-only
        if config.spec_only:
            log.info("DONE pipeline=spec-only")
            build_succeeded = True
            state.cleanup(state_path)
            print("RESULT:SUCCESS:spec-only", flush=True)
            return 0

        # Phase: Review (handled by SKILL.md wrapper — just advance state)
        if not state.is_phase_complete(BuildPhase.REVIEW):
            if config.auto:
                log.info("SKIP phase=REVIEW reason=--auto")
            else:
                _print_review_context_paths(config)
                _print_prototype_review_paths(config)
                log.info("DONE phase=REVIEW")
            state.advance_to(BuildPhase.REVIEW, state_path)
            print("PHASE:REVIEW:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.REVIEW, progress=progress)

        # Phase: TDD Build
        if not state.is_phase_complete(BuildPhase.TDD_BUILD):
            log.info("START phase=TDD_BUILD")
            # Recorded BEFORE the engine runs — the card is in development for
            # the whole build, not only once it succeeds.
            board.record(config, state, BuildPhase.TDD_BUILD, progress=progress)
            from .tdd_engine import run_tdd_engine
            result = await run_tdd_engine(config, args)
            # Reload state from disk — tdd_engine wrote its own updates (block status, TDD sub-state)
            # and our in-memory copy is stale. Without this, advance_to() overwrites tdd state with null.
            if state_path.exists():
                state = BuildState.load(state_path)
            if result != 0:
                log.error("FAIL phase=TDD_BUILD exit_code=%d", result)
                state.advance_to(BuildPhase.FAILED, state_path)
                board.record_failure(config, state, "TDD build failed", progress=progress)
                return result
            state.advance_to(BuildPhase.TDD_BUILD, state_path)
            log.info("DONE phase=TDD_BUILD")
            print("PHASE:TDD_BUILD:COMPLETE", flush=True)

        # Phase: Docs update — README/CHANGELOG/docs catch up with the code the
        # build just wrote, so the documentation lands in the same PR as the
        # change it describes instead of being left for the reviewer to write.
        # Always on (no flag, no config toggle) and non-fatal, like RESPEC: the
        # code already exists by now, so a documentation failure must not turn a
        # finished build into a failed one.
        if not state.is_phase_complete(BuildPhase.DOCS_UPDATE):
            log.info("START phase=DOCS_UPDATE")
            await _run_docs_update_phase(config, state, progress)
            state.advance_to(BuildPhase.DOCS_UPDATE, state_path)
            print("PHASE:DOCS_UPDATE:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.DOCS_UPDATE, progress=progress)

        # Phase: Respec (proposal only — never edits the specs; non-fatal)
        # Reads implementation-notes.md (written as the build ran) and writes
        # respec.md next to it, before the archive move carries both along.
        if not state.is_phase_complete(BuildPhase.RESPEC):
            log.info("START phase=RESPEC")
            from .llm_steps.respec_steps import run_respec_audit
            try:
                respec_path = await run_respec_audit(config, state)
                if respec_path:
                    log.info("DONE phase=RESPEC proposal=%s", respec_path)
                    progress.log_phase(
                        "RESPEC", f"Proposed spec delta written to {respec_path}",
                    )
                else:
                    log.warning("Respec audit produced no proposal (non-fatal)")
            except Exception as respec_err:
                log.warning("Respec audit failed (non-fatal): %s", respec_err)
            # Commit whatever the post-spec-generation phases left inside
            # specs/changes/<name> (prototype/, implementation-notes.md,
            # respec.md). Without --auto there is no archive commit to carry
            # them, so the pushed branch would otherwise be missing them.
            # Explicit pathspec; no-op when the pipeline owns no branch.
            git_ops.commit_stage(
                config,
                f"docs(specs): {config.change_name} — build companion artifacts",
                _spec_paths(config),
            )
            state.advance_to(BuildPhase.RESPEC, state_path)
            print("PHASE:RESPEC:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.RESPEC, progress=progress)

        # Phase: Archive
        # In --auto mode, archive immediately (merge delta specs → main registry,
        # move change to archive/). Otherwise, leave the change in place so the
        # user can review before running `buildme archive --change <name>`.
        #
        # PUBLISH runs AFTER the archive move, so the pushed branch carries the
        # archived layout and the move itself is a committed change rather than
        # an uncommitted rename sitting in the worktree.
        state.advance_to(BuildPhase.PUBLISH, state_path)
        if config.auto:
            if config.change_dir.exists():
                log.info("START phase=ARCHIVE reason=--auto")
                # The checkpoint travels with the move (commit_stage's
                # bookkeeping excludes keep it out of the archive commit).
                archive_dest = cm.archive_change(config.change_dir)
                log.info("DONE phase=ARCHIVE dest=%s", archive_dest)
                git_ops.commit_stage(
                    config,
                    f"chore(specs): archive {config.change_name}",
                    ["specs"],
                )
                print("PHASE:ARCHIVE:COMPLETE", flush=True)
            else:
                # Resumed after a publish failure: a previous attempt already
                # archived — the checkpoint's location is the archived dir.
                archive_dest = state_path.parent
                log.info("SKIP phase=ARCHIVE reason=already-archived dest=%s", archive_dest)
            # Re-point the checkpoint at where the move put it. It stays alive
            # until publish is done — a failed push/PR must leave something a
            # re-run can resume from.
            state_path = archive_dest / state_path.name
            state.state_file = str(state_path)
            publish_docs_dir = archive_dest
        else:
            log.info(
                "Archive skipped (manual). Run `buildme archive --change %s` when ready.",
                config.change_name,
            )
            print(f"PHASE:ARCHIVE:PENDING:{config.change_name}", flush=True)
            publish_docs_dir = config.change_dir

        # Phase: Publish (push branch + open draft PR). Non-fatal by
        # construction — a failure leaves the branch and worktree intact with
        # the exact manual commands in build-progress.md, and the build still
        # reports success for the code it produced. Runs BEFORE state.cleanup:
        # a failed publish keeps the checkpoint, so a re-run adopts this
        # worktree/branch and retries instead of colliding on the branch.
        published = _run_publish(
            config, state, progress, state_path=state_path, docs_dir=publish_docs_dir,
        )
        state.phase = BuildPhase.COMPLETE
        build_succeeded = True
        if published:
            state.cleanup(state_path)
            # Only clear the progress file when there is nothing left for a
            # human to do — an unpushed branch's diagnosis must survive.
            progress.cleanup()
        else:
            # Leave a resumable PUBLISH checkpoint (pr_url and branch already
            # persisted by _run_publish / advance_to above).
            state.phase = BuildPhase.PUBLISH
            state.checkpoint(state_path)
        print("RESULT:SUCCESS", flush=True)
        if state.worktree_path:
            print(f"WORKTREE:{state.worktree_path}", flush=True)
            log.info(
                "Worktree left in place at %s (branch %s) — deletion is the "
                "calling session's decision, from the workspace root.",
                state.worktree_path, state.branch,
            )
        log.info("DONE pipeline=complete change=%s", config.change_name)
        return 0

    except Exception as e:
        log.error("FAIL pipeline change=%s error=%s", config.change_name, e, exc_info=True)
        print(f"ERROR:{e}", file=sys.stderr, flush=True)
        # Cancelled only if nothing survives to resume from; otherwise the card
        # stays in its last column and a re-run picks it up there. Once the
        # build succeeded the missing checkpoint is the normal post-success
        # cleanup, not an unrecoverable failure — never Cancelled then.
        if not build_succeeded:
            board.record_failure(config, state, f"pipeline error: {e}", progress=progress)
        return 1


def prototype_decision(config) -> tuple[bool, str]:
    """Whether to run the prototype stage, and the reason either way.

    "false" / "true" come from `--no-prototype` / `--prototype` or
    `prototype: {enabled: ...}` in specs/config.yaml; "auto" defers to
    gates.prototype_required (frontend/fullstack, or a user-visible output
    format). The reason is always logged, so a skip is a recorded decision
    rather than a silent absence.
    """
    mode = getattr(config, "prototype_mode", "auto")
    if mode == "false":
        return False, "disabled (--no-prototype / config.yaml prototype.enabled: false)"
    if mode == "true":
        return True, "forced on (--prototype / config.yaml prototype.enabled: true)"

    from .gates import prototype_required

    if prototype_required(config.change_dir):
        return True, "auto: change has a UI or a user-visible output format"
    return False, "auto: no UI and no user-visible output format in proposal/design"


async def _run_prototype_phase(
    config: BuildConfig, state: BuildState, progress: ProgressWriter, state_path: Path,
) -> None:
    """Run the prototype stage and feed its notes back into design/tasks.

    Never raises: a failed prototype fails the phase, writes a diagnosis to
    build-progress.md, and lets the build continue.
    """
    # Resume guard, BEFORE the expensive agent run: prototype_done is
    # checkpointed after the feedback pass, so a crash between that checkpoint
    # and advance_to(PROTOTYPE) would otherwise re-run the agent here and then
    # skip the feedback pass — discarding whatever the fresh run disproved.
    # The artifact already exists on disk and its findings were applied; skip
    # the whole re-run.
    if state.spec_gen and state.spec_gen.prototype_done:
        log.info("SKIP phase=PROTOTYPE reason=recorded-complete-in-state")
        return

    should_run, reason = prototype_decision(config)
    if not should_run:
        log.info("SKIP phase=PROTOTYPE reason=%s", reason)
        print(f"PHASE:PROTOTYPE:SKIPPED:{reason}", flush=True)
        return

    log.info("START phase=PROTOTYPE reason=%s", reason)
    from .llm_steps.prototype_steps import apply_prototype_findings, run_prototype

    try:
        result = await run_prototype(config)
    except Exception as proto_err:  # non-fatal by design
        log.warning("Prototype stage failed (non-fatal): %s", proto_err)
        _log_prototype_diagnosis(progress, config, str(proto_err))
        return

    if not result.passed:
        log.warning("FAIL phase=PROTOTYPE reason=%s", result.reason)
        _log_prototype_diagnosis(progress, config, result.reason)
        return

    log.info("DONE phase=PROTOTYPE — %s", result.reason)

    # One regeneration pass of design.md/tasks.md from the notes. (A completed
    # feedback pass never reaches here — the guard at the top of this function
    # skips the whole phase when prototype_done is recorded.)
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    try:
        regenerated = await apply_prototype_findings(config)
        log.info("DONE phase=PROTOTYPE_FEEDBACK regenerated=%d", regenerated)
    except Exception as feedback_err:  # non-fatal
        log.warning("Prototype feedback pass failed (non-fatal): %s", feedback_err)
        _log_prototype_diagnosis(progress, config, str(feedback_err))
        return
    state.spec_gen.prototype_done = True
    state.checkpoint(state_path)


async def _run_docs_update_phase(
    config: BuildConfig, state: BuildState, progress: ProgressWriter,
) -> None:
    """Run the docs agent, commit what it wrote, and surface the freshness warning.

    Never raises: the build's code is already on disk when this runs, so a
    documentation failure is the phase's failure and never the build's.
    """
    from .llm_steps.docs_steps import run_docs_update

    try:
        files, gate = await run_docs_update(config, state)
    except Exception as docs_err:  # non-fatal by design
        log.warning("Docs update failed (non-fatal): %s", docs_err)
        print(f"PHASE:DOCS_UPDATE:FAILED:{docs_err}", flush=True)
        _progress_note(progress, "DOCS_UPDATE", f"FAILED (non-fatal): {docs_err}")
        return

    staged, dropped = _docs_paths(config, files)
    # Only paths that survived validation reach the state — the reported list
    # is agent output, and a hallucinated path must not surface in the PR body
    # as a document this build updated.
    state.docs_impact = staged
    # Checkpointed here rather than relying on the caller's advance_to: the PR
    # body is written much later, possibly in a resumed process, and an
    # in-memory value would silently drop the "docs updated" section.
    state.checkpoint(config.state_file_path())
    if dropped:
        shown = ", ".join(dropped[:10]) + (" …" if len(dropped) > 10 else "")
        log.warning(
            "DOCS_UPDATE reported %d path(s) that are not files in the project "
            "and were NOT committed: %s", len(dropped), shown,
        )
        print(f"DOCS_WARNING:reported paths not found in the project, dropped: {shown}",
              flush=True)
        _progress_note(
            progress, "DOCS_UPDATE",
            f"WARNING: {len(dropped)} reported path(s) dropped (not files in the "
            f"project): {shown}",
        )
    if staged:
        log.info("DONE phase=DOCS_UPDATE files=%d", len(staged))
        _progress_note(progress, "DOCS_UPDATE", "Updated: " + ", ".join(staged))
        # Its own commit, with an explicit pathspec — the docs are a separate
        # readable step in the branch history, not a lump with the code.
        git_ops.commit_stage(
            config,
            f"docs({config.change_name}): update documentation",
            staged,
        )
    else:
        log.info("DONE phase=DOCS_UPDATE files=0")
        _progress_note(progress, "DOCS_UPDATE", "No documentation needed updating")

    _warn_on_unreported_writes(config, staged, progress)

    if gate.action == "docs_may_be_stale":
        log.warning("DOCS_UPDATE warning: %s", gate.reason)
        print(f"DOCS_WARNING:{gate.reason}", flush=True)
        _progress_note(progress, "DOCS_UPDATE", f"WARNING: {gate.reason}")


def _warn_on_unreported_writes(
    config: BuildConfig, staged: list[str], progress: ProgressWriter,
) -> None:
    """Name any file the docs agent changed but did not report.

    The agent holds `Write`/`Edit` over the whole project, and only the paths
    it *reports* are staged. Anything else it touched — a source file it edited
    by accident, a document it forgot to name — would otherwise sit uncommitted
    in the worktree: absent from the PR, absent from the diff a reviewer reads,
    and gone when the worktree is removed. This does not stage it (the commit
    stays explicit-path, which is the property that makes an agent's self-report
    safe to act on); it makes the discrepancy visible.

    Never raises, and never fails the phase — the build's code is already
    committed by now.
    """
    changed = _uncommitted_paths(config)
    if changed is None:
        return
    staged_set = set(staged)
    unreported = [p for p in changed if p not in staged_set]
    if not unreported:
        return
    shown = ", ".join(unreported[:10]) + (" …" if len(unreported) > 10 else "")
    log.warning(
        "DOCS_UPDATE changed %d file(s) it did not report and which were "
        "therefore NOT committed: %s", len(unreported), shown,
    )
    print(f"DOCS_WARNING:unreported changes left uncommitted: {shown}", flush=True)
    _progress_note(
        progress, "DOCS_UPDATE",
        f"WARNING: {len(unreported)} unreported change(s) left uncommitted: {shown}",
    )


def _uncommitted_paths(config: BuildConfig) -> list[str] | None:
    """Repo-relative paths with uncommitted changes, excluding buildme's own.

    `specs/` is excluded because everything under it is the pipeline's paperwork
    (the state file, the board card, the change's artifacts), committed by other
    phases on their own schedule. `build-progress.md` is the pipeline's scratch.

    Returns None when git could not be asked — "unknown" must not be reported as
    "the agent wrote nothing".
    """
    import subprocess

    try:
        rel_specs = str(config.specs_dir.relative_to(config.project_dir))
    except ValueError:
        rel_specs = "specs"
    excludes = [f":(exclude){rel_specs}", ":(exclude)build-progress.md"]
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", *excludes],
            cwd=str(config.project_dir), capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log.warning("Could not check for unreported docs writes: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("Could not check for unreported docs writes: %s", proc.stderr.strip())
        return None
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # Renames are reported as "old -> new"; the new path is the one on disk.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and path not in paths:
            paths.append(path)
    return paths


def _docs_paths(config: BuildConfig, files: list[str]) -> tuple[list[str], list[str]]:
    """Split the agent's reported paths into ``(staged, dropped)``.

    ``staged`` holds pathspecs (relative to the project dir) for the documents
    the agent wrote: only paths that resolve to an existing file **inside** the
    project survive. This list comes from an agent's report and is the one
    place it becomes ``git add`` argv, so anything outside the project — or any
    pathspec-magic string that is not a real file — lands in ``dropped``
    (as reported, for naming back to the user) rather than being staged.
    """
    root = Path(config.project_dir).resolve()
    staged: list[str] = []
    dropped: list[str] = []
    for entry in files or []:
        candidate = Path(entry)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            log.warning(
                "Ignoring reported docs path (outside the project, or not a file): %s",
                entry,
            )
            dropped.append(entry)
            continue
        rel = str(resolved.relative_to(root))
        if rel not in staged:
            staged.append(rel)
    return staged, dropped


def _progress_note(progress: ProgressWriter, phase: str, text: str) -> None:
    """Write a progress line; a progress-file failure never breaks a phase."""
    try:
        progress.log_phase(phase, text)
    except OSError as e:
        log.warning("Could not write %s note to progress file: %s", phase, e)


def _log_prototype_diagnosis(progress: ProgressWriter, config: BuildConfig, reason: str) -> None:
    """Record why the prototype phase failed where the user will read it."""
    print(f"PHASE:PROTOTYPE:FAILED:{reason}", flush=True)
    try:
        progress.log_diagnosis(
            "prototype",
            f"Prototype stage did not produce a usable artifact.\n"
            f"Reason: {reason}\n"
            f"Expected under: {config.prototype_dir}\n"
            f"The build continues — the prototype is an aid, not a dependency.",
        )
    except OSError as e:
        log.warning("Could not write prototype diagnosis to progress file: %s", e)


# Source extensions that make a directory "a codebase worth analyzing". A
# project whose only files are specs/, a README and a .gitignore has nothing
# for the explore agents to read, and three agents reporting "(new project)"
# is pure spend.
_SOURCE_SUFFIXES = frozenset({
    ".py", ".swift", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
    ".kt", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".m",
    ".mm", ".scala", ".ex", ".exs", ".sh", ".sql", ".vue", ".svelte",
})

# Directories never worth walking when deciding "does this project have code".
_SOURCE_SCAN_SKIP = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".worktrees",
    "specs", ".build", "dist", "build", ".mypy_cache", ".pytest_cache",
    "target", ".tox", ".next",
})


def _has_source_files(project_dir: Path) -> bool:
    """Whether the project already contains source code to analyze.

    Walks the tree, skipping vendor/build/spec directories, and stops at the
    first source file — a bootstrapped-but-empty project must not pay for
    three explore agents.
    """
    if not project_dir.is_dir():
        return False
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SOURCE_SCAN_SKIP and not d.startswith(".")]
        for name in files:
            if Path(name).suffix in _SOURCE_SUFFIXES:
                return True
    return False


async def _run_codebase_analysis_phase(
    config: BuildConfig, state: BuildState, progress: ProgressWriter,
) -> None:
    """Analyze the existing codebase and record the report for spec generation.

    Never raises: the design falls back to "(new project)" when this produces
    nothing, which is exactly the pre-phase behavior. Skipped — with the reason
    logged — in `only` mode (no spec generation to feed) and for a project with
    no source files yet.
    """
    if config.mode == "only":
        log.info("SKIP phase=CODEBASE_ANALYSIS reason=only-mode-generates-no-specs")
        return
    if not _has_source_files(config.project_dir):
        log.info(
            "SKIP phase=CODEBASE_ANALYSIS reason=no-source-files-yet dir=%s",
            config.project_dir,
        )
        progress.log_phase(
            "CODEBASE_ANALYSIS", "skipped — no source files yet (new project)",
        )
        return

    from .llm_steps.spec_steps import run_codebase_analysis

    try:
        analysis = await run_codebase_analysis(config.project_dir, config)
    except Exception as analysis_err:  # non-fatal: grounding, not correctness
        log.warning("Codebase analysis failed (non-fatal): %s", analysis_err)
        return

    output = config.codebase_analysis_path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(analysis)
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    state.spec_gen.codebase_analysis_path = str(output)
    log.info("Wrote codebase analysis: %s (%d chars)", output, len(analysis))
    progress.log_phase("CODEBASE_ANALYSIS", f"Codebase analysis written to {output}")


def _print_review_context_paths(config: BuildConfig) -> None:
    """Print the documents the review checkpoint must be read against, in order.

    The explainer goes first, above every other artifact: reading it is the
    anti-slop step of the whole checkpoint — it is where a dependency's real
    edge cases are stated before anything is built on top of them. The
    codebase analysis follows, because it is what the design was written
    against: "why does it extend that module" is answered there, not in
    design.md.
    """
    if config.unknowns_path.exists():
        print(f"REVIEW:READ_FIRST:{config.unknowns_path}", flush=True)
    if config.codebase_analysis_path.exists():
        print(f"REVIEW:CODEBASE_ANALYSIS:{config.codebase_analysis_path}", flush=True)


def _print_prototype_review_paths(config: BuildConfig) -> None:
    """Print the prototype's `file://` path at the non-auto review checkpoint.

    The pipeline runs inside a container whose localhost is not the user's, so
    a served URL would be unreachable; the shared filesystem mount is the
    channel that always works.
    """
    from .llm_steps.prototype_steps import primary_prototype_artifact

    artifact = primary_prototype_artifact(config.prototype_dir)
    if artifact is None:
        return
    print(f"PROTOTYPE:file://{artifact.resolve()}", flush=True)
    notes = config.prototype_dir / "NOTES.md"
    if notes.exists():
        print(f"PROTOTYPE_NOTES:file://{notes.resolve()}", flush=True)


async def _run_bootstrap(
    config: BuildConfig, state: BuildState, cm: ChangeManager, state_path: Path,
) -> None:
    """Set up the build's worktree/branch, project skeleton, specs/, config.yaml.

    The worktree is created FIRST, before specs/ exists, so the change's
    artifacts are born inside the branch. It re-binds ``config.project_dir``,
    which invalidates ``cm`` and ``state_path`` — both are rebuilt here and in
    the caller.
    """
    import subprocess

    if config.git.worktree:
        _setup_worktree(config, state)
        state_path = config.state_file_path()
        cm = ChangeManager(config.specs_dir)
    else:
        # --no-worktree: the pre-git-lifecycle behavior, including `git init`
        # for a brand-new project directory.
        project = config.project_dir
        if not (project / ".git").exists():
            subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
            log.info("Initialized git repo at %s", project)

    # specs/ init if needed
    if not config.specs_dir.exists():
        cm.init_specs()
        log.info("Initialized specs/ at %s", config.specs_dir)

    # Create change if name is set
    if config.change_name:
        cm.create_change(config.change_name)

    # Write config.yaml if it doesn't exist
    config_yaml = config.specs_dir / "config.yaml"
    if not config_yaml.exists():
        import yaml
        data = {
            "schema": "spec-driven",
            "context": f"Project: {config.project_name}\n{config.project_description}",
            "tdd": {"enabled": True, "test_command": config.test_command},
        }
        config_yaml.write_text(yaml.dump(data, default_flow_style=False))

    state.bootstrap_done = True
    state.advance_to(BuildPhase.BOOTSTRAP, state_path)


def _setup_worktree(config: BuildConfig, state: BuildState) -> None:
    """Create the build's own worktree + branch and re-bind the config to it.

    Refuses (raises ``git_ops.GitLifecycleError``) rather than falling back
    when the source repo is not in a state that can be safely taken over —
    a silent fallback would put the build back on the caller's checkout,
    which is exactly what this phase exists to prevent.
    """
    if not config.change_name:
        log.info("SKIP worktree reason=no-change-name")
        return

    repo = git_ops.preflight(config.project_dir, worktree_requested=True)
    offset = _project_offset(config.project_dir, repo)
    base = git_ops.default_branch(repo)
    branch, dest = git_ops.resolve_new_branch(repo, config.change_name, base)
    git_ops.create_worktree(repo, branch, dest)

    state.source_repo = str(repo)
    state.branch = branch
    state.worktree_path = str(dest)
    # Re-bind to the SAME subdirectory inside the worktree, not its root — a
    # nested project (monorepo subpackage) must keep building where it lives.
    config.rebind_project_dir(dest / offset)
    config.pipeline_branch = branch
    state.state_file = str(config.state_file_path())
    log.info("Build re-bound to %s on branch %s", config.project_dir, branch)
    print(f"WORKTREE:{dest}", flush=True)
    print(f"BRANCH:{branch}", flush=True)


def _rebind_to_existing_worktree(config: BuildConfig) -> None:
    """Re-bind to a previous run's worktree so a resume never makes a second one.

    A build's ``.build-state.json`` lives inside its worktree, so on resume the
    default lookup (source repo → specs/changes/<name>/) finds nothing. Search
    the worktrees root for this change's worktree(s) and adopt the first one
    that carries a state file.
    """
    if not config.git.worktree or not config.change_name:
        return
    if config.state_file_path().exists():
        return  # in-place state (--no-worktree run, or already re-bound)

    repo = git_ops.repo_root(config.project_dir)
    if repo is None:
        return
    offset = _project_offset(config.project_dir, repo)
    root = git_ops.worktrees_root(repo)
    if not root.is_dir():
        return

    prefix = git_ops.worktree_dir_name(config.change_name)
    candidates = sorted(
        p for p in root.iterdir()
        if p.is_dir() and (p.name == prefix or p.name.startswith(f"{prefix}-"))
    )
    for dest in candidates:
        probe = BuildConfig(
            mode=config.mode, project_dir=dest / offset, change_name=config.change_name,
        )
        probe.specs_dir = dest / offset / "specs"
        probe_state = _existing_state_path(probe)
        if probe_state is None:
            continue
        try:
            prior = BuildState.load(probe_state)
        except Exception as e:  # corrupt checkpoint — leave it for a fresh run
            log.warning("Ignoring unreadable state at %s: %s", probe_state, e)
            continue
        config.rebind_project_dir(dest / offset)
        config.pipeline_branch = prior.branch or git_ops.current_branch(dest)
        log.info(
            "RESUME re-bound to existing worktree %s (branch=%s) — not creating a new one",
            dest, config.pipeline_branch,
        )
        print(f"WORKTREE:{dest}", flush=True)
        return


def _project_offset(project_dir: Path, repo: Path) -> Path:
    """``project_dir`` relative to its repo root (``Path('.')`` at the root).

    A nested project (monorepo subpackage) must keep building at the SAME
    subdirectory inside the build's worktree — re-binding to the worktree root
    would silently relocate the build (specs/, commits, test discovery) to the
    enclosing repo's root. The offset is exact (both paths come from git), so
    re-deriving it is correct rather than a guess; the ValueError branch is a
    refusal for the should-be-impossible case, in this module's
    refuse-rather-than-guess spirit.
    """
    try:
        return project_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        raise git_ops.GitLifecycleError(
            f"{project_dir} is not inside its own repo root {repo} — cannot "
            f"derive where the build should live inside the worktree. Refusing "
            f"to relocate the build to the worktree root."
        )


def _existing_state_path(config: BuildConfig) -> Path | None:
    """The change's on-disk checkpoint, wherever it currently lives.

    Normally ``specs/changes/<name>/.build-state.json``. After an ``--auto``
    archive with publish still pending, the checkpoint has traveled with the
    change dir into ``specs/archive/<date>-<name>/`` — without finding it
    there, a re-run after a publish failure would restart from scratch and
    hard-fail on the branch collision.
    """
    active = config.state_file_path()
    if active.exists():
        return active
    if not config.change_name:
        return None
    from .change_manager import archived_change_dirs
    # Anchored to exactly YYYY-MM-DD-<slug> — a loose `*-<slug>` glob would
    # also adopt another change's checkpoint (`foo` matching `…-bar-foo`).
    archived = [
        d / active.name
        for d in archived_change_dirs(config.specs_dir, config.change_name)
        if (d / active.name).exists()
    ]
    return archived[-1] if archived else None


def _spec_paths(config: BuildConfig) -> list[str]:
    """Explicit pathspec for the change's spec directory, relative to the
    worktree root. Never a whole-tree stage."""
    try:
        return [str(config.change_dir.relative_to(config.project_dir))]
    except ValueError:
        return []


def _pr_title_body(
    config: BuildConfig, state: BuildState, docs_dir: Path | None = None,
) -> tuple[str, str]:
    """PR title from the change name; body from proposal.md's Why, the block
    list, and links to whichever companion artifacts the build produced.

    ``docs_dir`` is where the change's documents live NOW — after an --auto
    archive that is ``specs/archive/<date>-<name>/``, not ``config.change_dir``
    (whose files were all moved, so reading through it would lose the Why and
    every artifact link)."""
    docs_dir = docs_dir or config.change_dir
    title = f"buildme: {config.change_name}"
    parts: list[str] = []

    why = _proposal_why(docs_dir)
    if why:
        parts.append(f"## Why\n\n{why}")

    if state.tdd and state.tdd.blocks:
        lines = "\n".join(
            f"- Block {b.number}: {b.name} — {b.status.value}" for b in state.tdd.blocks
        )
        parts.append(f"## Blocks\n\n{lines}")

    # Named explicitly so a reviewer knows the documentation in this PR was
    # rewritten by the build and needs reading, not assumed to be untouched.
    if state.docs_impact:
        docs = "\n".join(f"- `{name}`" for name in state.docs_impact)
        parts.append(
            "## Documentation\n\nUpdated by the build to match the code in this "
            f"PR:\n\n{docs}"
        )

    try:
        base = str(docs_dir.relative_to(config.project_dir))
    except ValueError:
        base = f"specs/changes/{config.change_name}"
    links = [
        f"- `{base}/{name}`"
        for name in ("unknowns.md", "prototype/NOTES.md", "implementation-notes.md", "respec.md")
        if (docs_dir / name).exists()
    ]
    if links:
        parts.append("## Companion artifacts\n\n" + "\n".join(links))

    parts.append(
        "Opened by the buildme pipeline. The build's worktree is left in place at "
        f"`{state.worktree_path}` — removing it is the calling session's decision."
    )
    return title, "\n\n".join(parts)


def _proposal_why(docs_dir: Path) -> str:
    proposal = docs_dir / "proposal.md"
    if not proposal.exists():
        return ""
    text = proposal.read_text()
    import re as _re
    m = _re.search(r"^##\s+Why\s*$(.*?)(?=^##\s|\Z)", text, _re.MULTILINE | _re.DOTALL)
    return m.group(1).strip() if m else ""


def _run_publish(
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    state_path: Path | None = None,
    docs_dir: Path | None = None,
) -> bool:
    """Push the build's branch and open its PR. Returns False when something
    was left for a human to finish (never raises, never fails the build).

    Skipped entirely when the pipeline does not own a branch: push/PR only
    ever act on a branch this pipeline created, which is what keeps
    ``--no-worktree`` identical to the pre-git-lifecycle behavior.

    ``docs_dir`` is where the change's documents currently live (the archived
    dir after an --auto archive); ``state_path``, when given, is checkpointed
    as soon as the PR URL is known so it survives crashes and re-runs.
    """
    if not state.branch or not state.worktree_path:
        log.info("SKIP phase=PUBLISH reason=no-pipeline-branch")
        return True

    worktree = Path(state.worktree_path)
    draft = config.git.pr == "draft"
    title, body = _pr_title_body(config, state, docs_dir)
    manual = git_ops.manual_finish_commands(
        worktree, state.branch, title, include_pr=config.git.pr != "none", draft=draft,
    )

    if not config.git.push:
        log.info("SKIP phase=PUBLISH reason=--no-push branch=%s", state.branch)
        print(f"PHASE:PUBLISH:SKIPPED:{state.branch}", flush=True)
        return True

    log.info("START phase=PUBLISH branch=%s", state.branch)

    if not git_ops.has_remote(worktree):
        _publish_diagnosis(
            progress,
            f"No 'origin' remote on {worktree} — nothing was pushed and no remote "
            f"was invented. The branch '{state.branch}' and its worktree are intact.",
            manual,
        )
        print("PHASE:PUBLISH:FAILED:no-remote", flush=True)
        return False

    push = git_ops.push_branch(worktree, state.branch)
    if not push.ok:
        _publish_diagnosis(
            progress,
            f"`git push` failed (exit {push.returncode}): "
            f"{(push.stderr or push.stdout).strip()[:800]}",
            manual,
        )
        print("PHASE:PUBLISH:FAILED:push", flush=True)
        return False
    log.info("PUSHED branch=%s", state.branch)
    print(f"PUSHED:{state.branch}", flush=True)

    if config.git.pr == "none":
        print("PHASE:PUBLISH:COMPLETE", flush=True)
        return True

    url, res = git_ops.open_pr(worktree, title, body, draft=draft)
    if not res.ok:
        _publish_diagnosis(
            progress,
            f"`gh pr create` failed (exit {res.returncode}): "
            f"{(res.stderr or res.stdout).strip()[:800]}. The branch is pushed — "
            f"only the PR is missing.",
            manual,
        )
        print("PHASE:PUBLISH:FAILED:pr", flush=True)
        return False

    state.pr_url = url
    # Persist the PR URL immediately — the in-memory object alone would lose
    # it on any crash, and a resume/re-run could never learn a PR exists.
    if state_path is not None:
        state.checkpoint(state_path)
    log.info("PR opened: %s", url)
    if url:
        print(f"PR:{url}", flush=True)
    # In Review becomes real here and nowhere else: the branch is pushed and a
    # PR exists, so a reviewer can `git fetch` and diff it. Recorded from the
    # LIVE state (which now also carries pr_url in its checkpoint until the
    # post-publish cleanup).
    board.record(
        config, state, column=BoardColumn.IN_REVIEW,
        note=f"branch {state.branch} pushed; PR {url or '(url unknown)'}",
        progress=progress,
    )
    print("PHASE:PUBLISH:COMPLETE", flush=True)
    return True


def _publish_diagnosis(progress: ProgressWriter, reason: str, manual: str) -> None:
    """Record a publish failure with the exact commands to finish by hand."""
    text = (
        f"{reason}\n\n"
        "Finish by hand from the workspace root:\n\n"
        "```\n" + manual + "\n```"
    )
    log.warning("PUBLISH failed (non-fatal): %s", reason)
    try:
        progress.log_diagnosis("publish", text)
    except OSError as e:
        log.warning("Could not write publish diagnosis to progress file: %s", e)
    print(f"PUBLISH_DIAGNOSIS:{reason}", flush=True)


async def _run_research_check(
    config: BuildConfig, state: BuildState, cm: ChangeManager, state_path: Path,
) -> None:
    """For build-only mode: check if research.md exists, generate if not."""
    if config.research_path.exists():
        state.research_path = str(config.research_path)
        log.info("Research already exists: %s", config.research_path)
        return

    log.info("No research.md found — will be generated by SKILL.md wrapper via /deep-research")
    # The SKILL.md wrapper handles invoking /deep-research --quick --auto
    # We just note that it's needed
