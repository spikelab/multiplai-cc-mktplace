"""Build orchestrator — main pipeline for /buildme.

A build starts from a change whose proposal, use cases and requirements
pm-spec marked ready (`multiplai_core.specs.is_ready`); anything else is
refused with `SPEC:NOT_READY` (exit 2) before a file is written. Then:
bootstrap → research → codebase analysis → design, unknowns, tasks, rubric →
design audit → plan review → size gate → review → TDD → docs → respec →
archive → publish. A parent change pm-spec split into children is an epic,
handed to `epic.phase` before any of this.

The SKILL.md wrapper handles interactive phases (plan review).
This module handles the non-interactive pipeline sequencing.
"""

from __future__ import annotations

from build_pipeline.budget import BudgetExceededError

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from . import board, git_ops, readiness, review_rounds, walkthroughs
from . import budget as budget_mod
from .change_manager import ChangeManager
from .config import BuildConfig
from .models import BoardColumn, BuildPhase
from .progress import ProgressWriter
from .readiness import SpecIssuesFound
from .state import BuildState, SpecGenState, completed_state_path

log = logging.getLogger(__name__)


async def run_orchestrator(config: BuildConfig, args) -> int:
    """Main entry point for the build orchestrator."""
    budget_mod.reset()
    budget_mod.configure(max_tokens=config.budget_max_tokens, max_usd=config.budget_max_usd)
    # An epic: pm-spec split this change and its `.change.yaml` lists the
    # children. Read in the caller's checkout, before any worktree re-binding:
    # the children build from the commit that holds their ready specs there.
    from .epic import phase as epic_phase
    if epic_phase.is_epic(config):
        return await epic_phase.run_epic_from_specs(config, args)
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

    # buildme builds only from specs pm-spec marked ready (rows S10, S11, S13):
    # checked before anything is written, so a refusal changes nothing.
    rc = _refuse_unless_ready(config, state_path)
    if rc is not None:
        return rc

    # Nothing to resume, but this change already published: ask before
    # building it again (exit 3, writing nothing). With --force, build it anew
    # on the next free branch, skipping the published builds' branches.
    if not state_path.exists():
        completed = _completed_builds(config)
        if completed and not getattr(args, "force", False):
            return _refuse_published_rerun(config, completed)
        if completed:
            config.published_branches = tuple(
                st.branch for _, _, st in completed if st.branch)
            log.info("FORCE new build of published change=%s skipping branches=%s",
                     config.change_name, ",".join(config.published_branches) or "-")

    progress = ProgressWriter(config.progress_file_path())

    # A spec-only handoff reuses its revision-bound planning evidence and spend.
    completed = completed_state_path(state_path)
    if not state_path.exists() and completed.exists():
        planned = BuildState.load(completed)
        if planned.completion_kind == "spec-only":
            if planned.branch and git_ops.current_branch(config.project_dir) != planned.branch:
                raise git_ops.GitLifecycleError("Spec-only handoff must run on its recorded build branch")
            config.pipeline_branch = planned.branch or ""
            budget_mod.get_budget().load_state(planned.budget, accept_unknown_spend=config.accept_unknown_spend)
            planned.checkpoint(state_path)

    # Resume or create state
    if state_path.exists():
        state = BuildState.load(state_path)
        # The spend saved so far carries into this run, whatever phase it
        # resumes in; every checkpoint below saves the running total.
        budget_mod.get_budget().load_state(state.budget, accept_unknown_spend=config.accept_unknown_spend)
        log.info("Resumed build: %s (phase=%s)", config.change_name, state.phase)
    else:
        state = BuildState(
            change_name=config.change_name,
            tier=config.tier,
            state_file=str(state_path),
        )

    if state.phase == BuildPhase.COMPLETE:
        print("RESULT:SUCCESS:historical", flush=True)
        return 0
    cm = ChangeManager(config.specs_dir)

    # A named input file that is gone stops the build (exit 2) only while the
    # phase that reads it is still ahead; a resume uses the change dir's copy.
    if args is not None:
        from .intake import check_input_files
        change_dirs = [config.change_dir] if config.change_name else []
        if state_path.exists():
            change_dirs.append(state_path.parent)
        error = check_input_files(args, state, change_dirs)
        if error:
            print(f"ERROR: {error}", file=sys.stderr, flush=True)
            return 2

    # Set the moment the build's outcome is decided as success (spec-only done,
    # or publish resolved). From then on `.build-state.json` may be renamed to
    # `.build-state.complete.json`, so a late exception (stdout) must NOT be
    # recorded as Cancelled — the card's real column (e.g. In Review) already
    # stands.
    build_succeeded = False
    # Whether this run has started its section of build-progress.md. The
    # progress file lives in the build's worktree, which bootstrap may create,
    # so the section starts right after bootstrap (or at once when bootstrap is
    # behind the build).
    progress_started = False

    def start_progress() -> None:
        nonlocal progress_started
        if not progress_started:
            progress.initialize(config.change_name, config.tier)
            budget_mod.warn_into(progress)
            progress_started = True

    try:
        # Phase: Bootstrap
        if not state.is_phase_complete(BuildPhase.BOOTSTRAP):
            log.info("START phase=BOOTSTRAP change=%s tier=%s", config.change_name, config.tier)
            from .intake import ConfigNotCommittedError
            try:
                await _run_bootstrap(config, state, cm, state_path, args=args)
            except ConfigNotCommittedError as e:
                log.error("FAILED phase=BOOTSTRAP reason=config-not-committed")
                print(f"ERROR: {e}", file=sys.stderr, flush=True)
                return 1
            # Bootstrap may have re-bound config.project_dir to the build's own
            # worktree — every path derived earlier is now stale.
            state_path = config.state_file_path()
            progress = ProgressWriter(config.progress_file_path())
            cm = ChangeManager(config.specs_dir)
            start_progress()
            log.info("DONE phase=BOOTSTRAP")
            print("PHASE:BOOTSTRAP:COMPLETE", flush=True)
            # First board record of the run: the card is being shaped.
            board.record(config, state, BuildPhase.BOOTSTRAP, progress=progress)
        start_progress()

        # Phase: Research
        if not state.tdd and not state.is_phase_complete(BuildPhase.RESEARCH):
            if config.skip_research:
                log.info("SKIP phase=RESEARCH reason=--skip-research")
                log.info("DONE phase=RESEARCH")
                state.advance_to(BuildPhase.RESEARCH, state_path)
            else:
                log.info("START phase=RESEARCH")
                from .intake import run_research_phase
                rc = await run_research_phase(config, state, args, state_path, progress)
                if rc is not None:
                    return rc
            if not state.is_phase_complete(BuildPhase.RESEARCH):
                state.advance_to(BuildPhase.RESEARCH, state_path)
            print("PHASE:RESEARCH:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.RESEARCH, progress=progress)

        # Phase: Codebase Analysis — read the repo that already exists so the
        # design extends it instead of inventing a parallel structure. Runs
        # before spec generation because design.md is its only consumer.
        # Best-effort: an analysis failure costs grounding, never the build. The
        # spec-extract step after it ends the build only when spec-extract may
        # still write into specs/registry/ (`_run_spec_extract_step`).
        if not state.tdd and not state.is_phase_complete(BuildPhase.CODEBASE_ANALYSIS):
            log.info("START phase=CODEBASE_ANALYSIS")
            await _run_codebase_analysis_phase(config, state, progress)
            # Save the analysis's report path before the step, which may return 1
            # or be stopped: a re-run then skips the analysis (row S8).
            state.checkpoint(state_path)
            rc = _run_spec_extract_step(config, state, progress)
            if rc is not None:
                return rc
            state.checkpoint(state_path)
            state.advance_to(BuildPhase.CODEBASE_ANALYSIS, state_path)
            log.info("DONE phase=CODEBASE_ANALYSIS")
            print("PHASE:CODEBASE_ANALYSIS:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.CODEBASE_ANALYSIS, progress=progress)

        # Phase: Spec Generation — buildme's half of the plan: design, unknowns,
        # tasks and rubric, from the ready proposal, use cases and requirements.
        if not state.tdd and not state.is_phase_complete(BuildPhase.SPEC_GENERATION):
            log.info("START phase=SPEC_GENERATION")
            from .spec_generator import run_spec_generator
            result = await run_spec_generator(config, args, standalone=False)
            if result != 0:
                log.error("FAIL phase=SPEC_GENERATION exit_code=%d", result)
                # Reload first, for the reason given below: a re-run keeps the
                # artifacts in its own worktree and needs their saved flags.
                if state_path.exists():
                    state = BuildState.load(state_path)
                state.fail(config.project_dir, state_path)
                board.record_failure(
                    config, state, "spec generation failed", progress=progress,
                )
                return result
            # Reload state from disk — run_spec_generator loaded its own copy
            # and checkpointed the spec_gen sub-state (completed artifacts, the
            # audit/gate completion flags). Advancing our stale copy would
            # write those back as null.
            if state_path.exists():
                state = BuildState.load(state_path)
            # Only ever forwards: advance_to assigns unconditionally, and the
            # reloaded copy may already sit at a later phase.
            if not state.is_phase_complete(BuildPhase.SPEC_GENERATION):
                state.advance_to(BuildPhase.SPEC_GENERATION, state_path)
            # Planning lands as its own commit so the branch history reads
            # specs → planning → implementation rather than one lump.
            # No-op when the pipeline does not own a branch (--no-worktree).
            git_ops.commit_stage(
                config,
                f"docs(specs): {config.change_name} — design, tasks",
                _spec_paths(config),
            )
            log.info("DONE phase=SPEC_GENERATION")
            print("PHASE:SPEC_GENERATION:COMPLETE", flush=True)
            board.record(config, state, BuildPhase.SPEC_GENERATION, progress=progress)

        # Reconcile edited plans before required validation. Completed
        # implementation is kept only while its recorded planning inputs agree.
        from .spec_generator import reconcile_plan, _generate_all_artifacts
        if state.phase != BuildPhase.PUBLISH:
            old_revision = state.spec_gen.revision if state.spec_gen else ""
            reconcile_plan(config, state)
            assert state.spec_gen is not None
            if state.tdd and not old_revision:
                raise RuntimeError("Legacy implementation has no planning revision; preserve it and start a reviewed build in a clean checkout")
            if state.tdd and old_revision and old_revision != state.spec_gen.revision:
                raise RuntimeError("Planning inputs changed after implementation; start a new build to preserve completed work")
            await _generate_all_artifacts(cm, config.change_dir, config, state)
            state.checkpoint(state_path)

        # A plan-review repair can invalidate the earlier design audit. The
        # owner returns only once both outcomes describe the final revision.
        if state.phase != BuildPhase.PUBLISH:
            from .spec_generator import validate_plan
            await validate_plan(config, state, state_path)
            for phase in (BuildPhase.DESIGN_AUDIT, BuildPhase.PLAN_REVIEW):
                if not state.is_phase_complete(phase):
                    state.advance_to(phase, state_path)
                print(f"PHASE:{phase.name}:COMPLETE", flush=True)
                board.record(config, state, phase, progress=progress)

        # Reassess size after every plan rewrite. A plan that should be split
        # stops here (exit 3) with split-proposal.md, until --accept-size.
        if state.phase != BuildPhase.PUBLISH:
            rc = _run_size_gate(
                config, state, state_path, progress,
                accept_size=bool(getattr(args, "accept_size", False)),
            )
            if rc is not None:
                return rc

        if state.phase != BuildPhase.PUBLISH:
            from .spec_generator import assert_plan_current
            assert_plan_current(config, state)

        # Stop if --spec-only. The checkpoint is kept, renamed, at the last spec
        # phase: not a published build, so the next `build` of the change runs
        # as usual.
        if config.spec_only:
            log.info("DONE pipeline=spec-only")
            build_succeeded = True
            state.completion_kind = "spec-only"
            done = state.complete(state_path)
            progress.log_phase("COMPLETE", f"Specs only. Checkpoint kept at {done}")
            print("RESULT:SUCCESS:spec-only", flush=True)
            return 0

        # Phase: Review (handled by SKILL.md wrapper — just advance state)
        if not state.is_phase_complete(BuildPhase.REVIEW):
            if config.auto:
                log.info("SKIP phase=REVIEW reason=--auto")
            else:
                _print_review_context_paths(config)
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
            # standalone=False: this is a sub-phase, so the engine must not
            # complete the checkpoint — the reload below depends on the file
            # still being there.
            result = await run_tdd_engine(config, args, standalone=False)
            # Reload state from disk — tdd_engine wrote its own updates (block status, TDD sub-state)
            # and our in-memory copy is stale. Without this, advance_to() overwrites tdd state with null.
            if state_path.exists():
                state = BuildState.load(state_path)
            issues = readiness.open_issues(config.change_dir)
            if result != 0 and issues is not None:
                # SPEC_IMPACT: contradicts under respec.halt_on_contradiction
                # wrote spec-issues.md (row S12): stop for `pm-spec revise`.
                log.error("STOP phase=TDD_BUILD reason=spec_issues path=%s", issues)
                return readiness.refuse_issues(issues)
            if result != 0:
                log.error("FAIL phase=TDD_BUILD exit_code=%d", result)
                print(f"ERROR:TDD build failed (exit {result})", file=sys.stderr, flush=True)
                state.fail(config.project_dir, state_path)
                board.record_failure(config, state, "TDD build failed", progress=progress)
                return result
            state.advance_to(BuildPhase.TDD_BUILD, state_path)
            log.info("DONE phase=TDD_BUILD")
            print("PHASE:TDD_BUILD:COMPLETE", flush=True)

        # Phase: Docs update — README/CHANGELOG/docs catch up with the code the
        # build just wrote, so the documentation lands in the same PR as the
        # change it describes instead of being left for the reviewer to write.
        # A failed writer blocks completion because it may leave partial edits.
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
            except BudgetExceededError:
                raise
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
        if state.phase != BuildPhase.PUBLISH:
            from .spec_generator import assert_plan_current
            assert_plan_current(config, state)
        state.advance_to(BuildPhase.PUBLISH, state_path)
        if config.auto:
            if config.change_dir.exists():
                log.info("START phase=ARCHIVE reason=--auto")
                # The checkpoint travels with the move (commit_stage's
                # bookkeeping excludes keep it out of the archive commit).
                change_path = config.change_dir
                registry_paths = [config.specs_dir / "registry" / path.name
                                  for path in (change_path / "requirements").glob("*.md")]
                archive_dest = cm.archive_change(change_path)
                log.info("DONE phase=ARCHIVE dest=%s", archive_dest)
                git_ops.commit_stage(
                    config,
                    f"chore(specs): archive {config.change_name}",
                    [str(path.relative_to(config.project_dir))
                     for path in [change_path, archive_dest, *registry_paths]],
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

        config.change_dir = publish_docs_dir
        from .spec_generator import assert_plan_current
        assert_plan_current(config, state, archived=config.auto)

        # All content-writing preparation precedes the shared final gate.
        from .gates import final_verification, verification_inputs
        revision = state.spec_gen.revision if state.spec_gen else ""
        try:
            current = verification_inputs(config.test_command, config.project_dir,
                                          parallel=config.parallel_tests, plan_revision=revision)
        except (OSError, RuntimeError, ValueError):
            current = None
        verified = (state.verification or {}).get("passed", False) and current is not None and (
            (state.verification or {}).get("metadata", {}).get("inputs") == current)
        if not verified:
            sections = [p for p in [_worktree_section(state, publish_docs_dir)] if p is not None]
            sections += _pre_publish_sections(config, state, publish_docs_dir)
            state.publish_sections = [str(p) for p in sections]
            if state.branch and not git_ops.tree_is_clean(config.project_dir):
                raise RuntimeError("Uncommitted build inputs remain; commit only the intended changes before final verification")
            gate = final_verification(config.test_command, config.project_dir,
                                      parallel=config.parallel_tests, plan_revision=revision)
            state.verification = gate.model_dump()
            state.checkpoint(state_path)
            if not gate.passed:
                progress.log_phase("VERIFY", gate.reason)
                print(f"RESULT:FAILED:verification:{gate.reason}", flush=True)
                return 1

        # Phase: Publish (push branch + open draft PR). Non-fatal by
        # construction — a failure leaves the branch and worktree intact with
        # the exact manual commands in build-progress.md, and the build still
        # reports success for the code it produced. A failed publish keeps
        # `.build-state.json`, so a re-run adopts this worktree/branch and
        # retries instead of colliding on the branch.
        published = _run_publish(
            config, state, progress, state_path=state_path, docs_dir=publish_docs_dir,
        )
        state.publication = "published" if published and state.branch and config.git.push else ("skipped" if published else "failed")
        state.phase = BuildPhase.COMPLETE
        build_succeeded = True
        if published:
            # Kept at COMPLETE as `.build-state.complete.json`: never resumed,
            # and a re-run of the change asks first (`_completed_builds`).
            done = state.complete(state_path)
            progress.log_phase(
                "COMPLETE", f"Published: {state.pr_url or state.branch}. Checkpoint kept at {done}",
            )
        else:
            # Leave a resumable PUBLISH checkpoint (pr_url and branch already
            # persisted by _run_publish / advance_to above).
            state.phase = BuildPhase.PUBLISH
            state.checkpoint(state_path)
        print("VERIFICATION:passed", flush=True)
        print(f"PUBLICATION:{state.publication}", flush=True)
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

    except budget_mod.BudgetExceededError as e:
        if state_path.exists():
            state = BuildState.load(state_path)
        state.checkpoint(state_path)
        progress.log_phase("BUDGET", f"STOPPED: {e}\n{e.diagnosis}")
        return 1
    except SpecIssuesFound as found:
        # A requirement the build cannot follow goes back to pm-spec (rows
        # S11, S12): not a failure, so the state is not marked FAILED and a
        # re-run after `pm-spec revise` resumes here.
        log.error("STOP pipeline change=%s reason=spec_issues step=%s", config.change_name, found.step)
        if state_path.exists():
            BuildState.load(state_path).checkpoint(state_path)
        return readiness.stop_on_issues(config.change_dir, found, change=config.change_name)
    except (SystemExit, KeyboardInterrupt, asyncio.CancelledError) as e:
        # A stop (SIGTERM/SIGHUP via git_ops._exit_on_termination, Ctrl-C) is
        # not a failure: write it down, save the spend so far, and let it end
        # the process. The state is not marked FAILED, so resume behaves as
        # after any stop.
        _record_interrupt(progress if progress_started else None, state_path, e,
                          completed=build_succeeded)
        raise
    except Exception as e:
        log.error("FAIL pipeline change=%s error=%s", config.change_name, e, exc_info=True)
        print(f"ERROR:{e}", file=sys.stderr, flush=True)
        if state_path.exists():
            BuildState.load(state_path).checkpoint(state_path)
        # Cancelled only if nothing survives to resume from; otherwise the card
        # stays in its last column and a re-run picks it up there. Once the
        # build succeeded `.build-state.json` may have become
        # `.build-state.complete.json` — never Cancelled then.
        if not build_succeeded:
            board.record_failure(config, state, f"pipeline error: {e}", progress=progress)
        return 1
    finally:
        # What this run spent, by step, at the end of every run that started
        # its progress section (success, failure, a stop, an exit 3).
        if progress_started:
            try:
                progress.log_phase("BUDGET", budget_mod.get_budget().diagnosis(advice=False))
            except OSError as e:
                log.warning("Could not write the spend summary to the progress file: %s", e)


def _refuse_unless_ready(config: BuildConfig, state_path: Path) -> int | None:
    """Refuse a build whose specs are not ready, before anything is written.

    The specs are the change dir the checkpoint is in (the archive's, once an
    `--auto` archive moved it), else the change's dir. Not ready
    (`multiplai_core.specs.is_ready`) prints `SPEC:NOT_READY:<reason>`, exit 2
    (rows S10, S13). The advice names `pm-spec resume <name>`, which checks
    specs already on disk and marks them ready without rewriting them, when
    the specs were never marked and a proposal and requirements are on disk,
    unless a checkpoint from before buildme stopped generating them is still
    short of their generation (item 21). An open `spec_issues` prints
    `SPEC:ISSUES:<path>`, exit 4 (rows S11, S12). A first run that will branch
    a worktree from HEAD refuses a change dir with uncommitted changes, since
    the worktree would not hold them. A finished build's checkpoint is not
    checked: its run only reports the result.
    """
    from multiplai_core.specs import CHANGE_META_FILE, is_ready, read_change_meta

    from . import readiness

    prior = None
    if state_path.exists():
        try:
            prior = BuildState.load(state_path)
        except Exception as e:  # noqa: BLE001 — an unreadable checkpoint is reported where it is loaded
            log.warning("Could not read %s before the specs check: %s", state_path, e)
        if prior is not None and prior.phase == BuildPhase.COMPLETE:
            return None
    change_dir = state_path.parent if state_path.exists() else config.change_dir
    ready, reason = is_ready(change_dir)
    if not ready:
        try:
            marked = (change_dir / CHANGE_META_FILE).is_file() and \
                read_change_meta(change_dir).get("spec_status") is not None
        except (OSError, UnicodeError, ValueError):
            marked = False
        on_disk = (change_dir / "proposal.md").is_file() and any((change_dir / "requirements").glob("*.md"))
        early = prior is not None and prior.tdd is None and not prior.is_phase_complete(BuildPhase.SPEC_GENERATION)
        log.error("REFUSED change=%s reason=spec-not-ready detail=%s", config.change_name, reason)
        return readiness.refuse_not_ready(config.change_name, reason,
                                          resume=not marked and on_disk and not early)
    issues = readiness.open_issues(change_dir)
    if issues is not None:
        log.error("REFUSED change=%s reason=spec-issues file=%s", config.change_name, issues)
        return readiness.refuse_issues(issues)
    if prior is None and config.git.worktree:
        try:
            dirty = readiness.uncommitted(config.project_dir, change_dir)
        except git_ops.GitLifecycleError as e:
            # Not a checkout git can read: bootstrap's preflight says why.
            log.warning("Could not check %s for uncommitted changes: %s", change_dir, e)
            dirty = []
        if dirty:
            log.error("REFUSED change=%s reason=specs-uncommitted paths=%s", config.change_name, dirty)
            return readiness.refuse_not_ready(
                config.change_name,
                f"{change_dir} has uncommitted changes ({', '.join(dirty[:5])}"
                f"{', ...' if len(dirty) > 5 else ''}); the build's worktree branches from HEAD, "
                f"which does not hold them",
                resume=True)
    return None


def _record_interrupt(progress, state_path: Path, exc: BaseException, *, completed: bool) -> None:
    """Write a stop down: `## INTERRUPTED by <SIG>` in the progress file, a
    WARNING log line, and the checkpoint re-saved with the spend so far.

    The checkpoint is re-read from disk first: the TDD engine and the spec
    generator checkpoint their own copies, and writing this function's caller's
    copy would put stale block state back. Never raises — the stop must go on.
    """
    sig = git_ops.stop_signal_name(exc)
    log.warning("INTERRUPTED by %s", sig)
    if progress is not None:
        try:
            progress.log_phase(f"INTERRUPTED by {sig}")
        except OSError as e:
            log.warning("Could not write the stop to the progress file: %s", e)
    if completed or not state_path.exists():
        return
    try:
        BuildState.load(state_path).checkpoint(state_path)
    except Exception as e:  # noqa: BLE001 — a stop must not turn into a crash
        log.warning("Could not save the checkpoint on %s: %s", sig, e)


async def _run_docs_update_phase(
    config: BuildConfig, state: BuildState, progress: ProgressWriter,
) -> None:
    """Run the docs agent, commit what it wrote, and surface the freshness warning.

    Failed writers stop completion because they may leave partial edits.
    """
    from .llm_steps.docs_steps import run_docs_update

    try:
        files, gate = await run_docs_update(config, state)
    except BudgetExceededError:
        raise
    except Exception as docs_err:
        log.warning("Docs update failed: %s", docs_err)
        print(f"PHASE:DOCS_UPDATE:FAILED:{docs_err}", flush=True)
        _progress_note(progress, "DOCS_UPDATE", f"FAILED: {docs_err}")
        raise

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
    except BudgetExceededError:
        raise
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
    logged — for a project with no source files yet.
    """
    if (state.spec_gen is not None and state.spec_gen.spec_extract_sha
            and state.spec_gen.codebase_analysis_path):
        # Saved by the spec-extract step, which runs after this analysis: an
        # earlier process already analysed the code and saved where the report is,
        # and a re-run of the step must not pay for the analysis again. One that
        # failed saved no report, and is asked again.
        log.info("SKIP phase=CODEBASE_ANALYSIS reason=analysed-before-spec-extract report=%s",
                 state.spec_gen.codebase_analysis_path)
        return
    from .code_analysis import NOT_ANALYSED_HEADING

    saved = state.spec_gen.codebase_analysis_path if state.spec_gen is not None else None
    if (saved and Path(saved) == config.codebase_analysis_path and Path(saved).is_file()
            and not Path(saved).read_text(errors="replace").startswith(NOT_ANALYSED_HEADING)):
        # A FAILED build re-run in its own worktree keeps the change dir, the
        # report in it and the design written against that report: analysing
        # the tree again, which now holds the failed run's commits, would pay
        # twice for a report the kept design does not match. The not-analysed
        # report analysed nothing and asks for a commit and a re-run, so that
        # re-run analyses.
        log.info("SKIP phase=CODEBASE_ANALYSIS reason=report-already-written report=%s", saved)
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
    except BudgetExceededError:
        raise
    except Exception as analysis_err:  # non-fatal: grounding, not correctness
        log.warning("Codebase analysis failed (non-fatal): %s", analysis_err)
        return

    output = config.codebase_analysis_path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(analysis)
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    state.spec_gen.codebase_analysis_path = str(output)
    if analysis.startswith(NOT_ANALYSED_HEADING):
        log.info("SKIP phase=CODEBASE_ANALYSIS reason=no-git-commit dir=%s report=%s", config.project_dir, output)
        progress.log_phase("CODEBASE_ANALYSIS", "skipped — the project has no git commit to read the code at; "
                           f"{output} says so and lists its source files")
        return
    log.info("Wrote codebase analysis: %s (%d chars)", output, len(analysis))
    progress.log_phase("CODEBASE_ANALYSIS", f"Codebase analysis written to {output}")


SPEC_EXTRACT_EXIT_LOCKED = 5


def _spec_extract_skill_dir() -> Path:
    """The spec-extract skill's script directory, ``<skills>/spec-extract/scripts``,
    found as `_pr_skill_dir` finds the pr skill's."""
    return Path(__file__).resolve().parents[3] / "spec-extract" / "scripts"


def _spec_extract_argv(config: BuildConfig, sha: str) -> list[str]:
    """`spec_extract_pipeline run --tree <sha> ... --apply` for the project.

    spec-extract puts `--repo`'s own prefix in front of `--path`
    (`target.tree_root`), so a project below the top of its checkout passes the
    top as `--repo` and its own directory as `--path`: the path is never doubled.
    """
    project = Path(config.project_dir).resolve()
    top = git_ops.repo_root(project)
    top = top.resolve() if top else project
    argv = ["uv", "run", "--directory", str(_spec_extract_skill_dir()), "python", "-m",
            "spec_extract_pipeline", "run", "--repo", str(top), "--tree", sha]
    if top != project and top in project.parents:
        argv += ["--path", project.relative_to(top).as_posix()]
    # The cap exactly as configured: `repr` keeps every digit a float holds.
    cap = float(config.spec_extract_max_usd)
    max_usd = str(int(cap)) if cap.is_integer() else repr(cap)
    # Absolute: `uv run --directory` runs spec-extract in its own scripts directory.
    return argv + ["--max-usd", max_usd, "--trust-repo",
                   "--apply", "--specs-dir", str(Path(config.specs_dir).resolve())]


def _spec_extract_last_line(res: git_ops.GitResult) -> str:
    lines = (res.stderr or res.stdout or "").strip().splitlines()
    return lines[-1][:300] if lines else "no output"


def _epic_child(config: BuildConfig) -> bool:
    """True for an epic's child build: `--epic-child` in its command, which a
    hand re-run of the child's saved command keeps (#347), or the coordinator's
    `CHILD_ENV`, which a child started before the flag existed has instead."""
    from .epic import CHILD_ENV

    return config.epic_child or os.environ.get(CHILD_ENV) == "1"


def _run_spec_extract_step(
    config: BuildConfig, state: BuildState, progress: ProgressWriter,
) -> int | None:
    """Run spec-extract on the project before the plan is written, and commit the
    registry files it applies (inside CODEBASE_ANALYSIS, after the analysis).

    None lets the phase complete; an int is the build's exit code, and the phase
    is not advanced, so a re-run repeats the step. The commit read is saved in
    `state.spec_gen.spec_extract_sha` before the launch, and a resume always
    reads that commit, so it reaches the spec-extract run folder the stopped run
    paid into. spec-extract's spend is outside buildme's `BuildBudget`, bounded
    by `spec_extract_max_usd`; its `SPEND:` line is added to buildme's
    (`git_ops.SPEND_REPORTING`), not to an epic's `epic.max_usd`. Skipped in an
    epic's child build.
    """
    from . import sdk, stop_record

    registry = Path(config.specs_dir) / "registry"
    saved = state.spec_gen.spec_extract_sha if state.spec_gen else None
    reason = None
    if _epic_child(config):
        # Siblings at one commit would race for spec-extract's lock and each
        # commit a registry baseline, so an epic's children build without one.
        reason = "epic-child"
    elif not config.spec_extract:
        reason = "spec_extract-false"
    elif not sdk._repo_is_trusted():
        reason = "repo-not-trusted"
    elif saved is None and any(registry.glob("*.md")):
        reason = "registry-not-empty"
    elif saved is None:
        saved = git_ops.rev_parse_head(config.project_dir)
        if saved is None:
            reason = "no-git-commit"
        else:
            if state.spec_gen is None:
                state.spec_gen = SpecGenState()
            state.spec_gen.spec_extract_sha = saved
            state.checkpoint(config.state_file_path())
    if reason is not None:
        log.info("SKIP phase=CODEBASE_ANALYSIS step=spec-extract reason=%s", reason)
        return None

    log.info("START phase=CODEBASE_ANALYSIS step=spec-extract sha=%s", saved)
    progress.log_phase("CODEBASE_ANALYSIS", f"spec-extract at {saved[:12]}")
    res = git_ops._run_spec_extract_skill(_spec_extract_argv(config, saved), cwd=config.project_dir)
    if res.returncode == 0:
        rel = Path(os.path.relpath(registry, config.project_dir)).as_posix()
        git_ops.commit_stage(config, f"docs(specs): baseline registry from spec-extract at {saved[:12]}",
                             [rel])
        applied = next((line.split()[1] for line in reversed(res.stdout.splitlines())
                        if line.startswith("SPEC_EXTRACT:APPLIED ") and len(line.split()) > 1), "0")
        # `commit_stage` commits nothing without a pipeline branch (`--no-worktree`),
        # and a failed commit is only logged: say when the files are left uncommitted.
        left = stop_record.others_work(registry, None) if registry.is_dir() else None
        if left is None:
            print(f"SPEC_EXTRACT:BASELINE {applied}", flush=True)
            progress.log_phase("CODEBASE_ANALYSIS", f"spec-extract applied {applied} registry file(s)")
        else:
            print(f"SPEC_EXTRACT:BASELINE {applied} uncommitted", flush=True)
            log.warning("spec-extract's registry files are not committed: %s", left)
            progress.log_phase("CODEBASE_ANALYSIS", f"spec-extract applied {applied} registry file(s), "
                               f"left uncommitted in {rel}")
        return None
    last = _spec_extract_last_line(res)
    if res.returncode == SPEC_EXTRACT_EXIT_LOCKED:
        print(f"SPEC_EXTRACT:RUNNING {last}", flush=True)
        log.warning("spec-extract is still running for this target: %s", last)
        return 1
    dirty = stop_record.others_work(registry, None) if registry.is_dir() else None
    if dirty is None:
        print(f"SPEC_EXTRACT:NOT_RUN exit={res.returncode} {last}", flush=True)
        log.warning("spec-extract did not run (exit %s): %s; the build goes on without a baseline",
                    res.returncode, last)
        return None
    print(f"ERROR: spec-extract exited {res.returncode} and {dirty}. Stash or commit them, or set "
          "`spec_extract: false` in specs/config.yaml, and re-run.", file=sys.stderr, flush=True)
    return 1


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


async def _run_bootstrap(
    config: BuildConfig, state: BuildState, cm: ChangeManager, state_path: Path,
    *, args=None,
) -> None:
    """Set up the build's worktree/branch, project skeleton, specs/, config.yaml.

    The worktree is created FIRST, before specs/ exists, so the change's
    artifacts are born inside the branch. It re-binds ``config.project_dir``,
    which invalidates ``cm`` and ``state_path`` — both are rebuilt here and in
    the caller.
    """
    import subprocess

    from . import intake, stop_record

    previous_change_dir = config.change_dir if config.change_name else None
    # A FAILED build, or a re-run of one that stopped before its research
    # finished (pointer back AT BOOTSTRAP, with the record this bootstrap
    # saves below), keeps its own worktree: its branch may hold commits,
    # which `_setup_worktree` refuses.
    kept = bool(config.git.worktree
                and (state.phase == BuildPhase.FAILED
                     or (state.phase == BuildPhase.BOOTSTRAP and state.failed_stop is not None))
                and _inside_own_worktree(config, state)
                and _keeps_failed_worktree(config, state))
    if kept:
        state_path = config.state_file_path()
        cm = ChangeManager(config.specs_dir)
    elif config.git.worktree:
        # The worktree branches from HEAD, so an uncommitted config.yaml would
        # silently not exist inside it — refuse instead of building without it.
        # Only the caller's source repo is checked: a build resumed at
        # BOOTSTRAP, or a FAILED one `_keeps_failed_worktree` sends here,
        # runs bootstrap inside its own worktree after
        # _rebind_to_existing_worktree, where the uncommitted config.yaml is
        # the stub bootstrap wrote itself and the user has nothing to commit.
        if (not _inside_own_worktree(config, state)
                and intake.config_yaml_uncommitted(config.project_dir)):
            raise intake.ConfigNotCommittedError(intake.CONFIG_NOT_COMMITTED)
        # With --base-ref (an epic's child) it branches from that commit
        # instead, where a committed but unpushed config.yaml is missing too.
        if (config.base_ref and not _inside_own_worktree(config, state)
                and intake.config_yaml_off_ref(config.project_dir, config.base_ref)):
            raise intake.ConfigNotCommittedError(intake.CONFIG_NOT_ON_BASE)
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
        if previous_change_dir is not None:
            intake.carry_over_inputs(previous_change_dir, config.change_dir)

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

    # What a kept re-run's bootstrap leaves is the build's own (the check
    # above passed, then bootstrap wrote its files): a stop before the
    # research finishes resumes in place against this record. Any other
    # bootstrap starts a tree that no failure record describes.
    state.failed_stop = stop_record.record(config.project_dir) if kept else None
    state.bootstrap_done = True
    state.advance_to(BuildPhase.BOOTSTRAP, state_path)


def _run_size_gate(
    config: BuildConfig, state: BuildState, state_path: Path,
    progress: ProgressWriter, *, accept_size: bool,
) -> int | None:
    """Stop a plan that should be split, after plan review, until --accept-size.

    An acceptance is saved as ``state.size_accepted``, so a retry without the
    flag goes on instead of posting the proposal again.

    Re-assesses the final tasks.md with the plan review's own parse and
    thresholds (``split_assessment``). On ``should_split`` — or when an earlier
    run already stopped here — writes ``split-proposal.md``, sets
    ``state.split_required``, prints ``SPLIT:REQUIRED:<path>`` and returns 3.
    The wrapper posts the proposal on the ticket (or shows it) and stops.
    """
    from .intake import MARKER_SPLIT_REQUIRED, WAITING_EXIT
    from .llm_steps.plan_review_steps import render_split_proposal, split_assessment

    log.info("START phase=SIZE_GATE")
    _, assessment = split_assessment(config, config.change_dir)
    should_split = bool(assessment is not None and assessment.should_split)
    if not should_split and not state.split_required:
        log.info("DONE phase=SIZE_GATE split=false")
        return None
    if accept_size or state.size_accepted:
        state.split_required = False
        state.size_accepted = True
        state.checkpoint(state_path)
        log.info("DONE phase=SIZE_GATE split=%s accepted=true", should_split)
        _progress_note(progress, "SIZE_GATE", "Plan size accepted with --accept-size")
        return None

    path = config.change_dir / "split-proposal.md"
    if assessment is not None and should_split:
        path.write_text(render_split_proposal(assessment, config.change_name))
    state.split_required = True
    state.checkpoint(state_path)
    log.info("DONE phase=SIZE_GATE split=true proposal=%s", path)
    _progress_note(progress, "SIZE_GATE",
                   f"Plan should be split; proposal in {path}. Re-run with --accept-size to build it as one PR.")
    print(f"{MARKER_SPLIT_REQUIRED}:{path}", flush=True)
    return WAITING_EXIT


def _inside_own_worktree(config: BuildConfig, state: BuildState) -> bool:
    """True when the config is already bound inside this build's worktree."""
    if not state.worktree_path:
        return False
    try:
        config.project_dir.resolve().relative_to(Path(state.worktree_path).resolve())
    except ValueError:
        return False
    return True


def _keeps_failed_worktree(config: BuildConfig, state: BuildState) -> bool:
    """Whether a FAILED build re-run inside its own worktree keeps it. A
    re-run stopped before its research finished (pointer AT BOOTSTRAP) is
    checked the same way, against the record its bootstrap saved.

    A re-run (by hand, or an epic coordinator's relaunch of a failed child
    with the same argv) keeps the build's branch and worktree, with every
    commit on it: `_setup_worktree` would refuse that branch once it holds a
    commit. The re-run commits whatever is in the tree on whatever is checked
    out, so it stops (raises ``git_ops.GitLifecycleError``, touching nothing)
    when the worktree is on another branch, or holds an uncommitted change
    the failed run did not leave (`state.failed_stop`, `stop_record`).

    False sends the re-run to `_setup_worktree`, as every FAILED re-run went
    before the failure record existed: a FAILED checkpoint with no record
    (written before it existed, or git could not be asked), whose branch has
    no commit of its own and whose tree holds uncommitted changes, gets a new
    ``-2`` worktree and leaves this one as it is. With a commit,
    `_setup_worktree` would refuse the branch, so it stops here instead.
    """
    from . import stop_record

    worktree = state.worktree_path
    on = git_ops.current_branch(config.project_dir)
    if state.branch and on != state.branch:
        where = "a detached HEAD" if on in ("", "HEAD") else f"branch '{on}'"
        raise git_ops.GitLifecycleError(
            f"Did not re-run the failed build in {worktree}: it is on {where}, not the "
            f"build's branch '{state.branch}', and the re-run commits on what is checked "
            f"out; switch back (`git -C {worktree} switch {state.branch}`), then re-run")
    left = stop_record.others_work(config.project_dir, state.failed_stop)
    if left and state.failed_stop is None and state.branch:
        base = config.base_ref or git_ops.default_branch(config.project_dir)
        if git_ops.unmerged_commit_count(config.project_dir, state.branch, base) == 0:
            log.info("RESUME failed build has no failure record and no commit: a new "
                     "worktree, %s left as it is (%s)", worktree, left)
            return False
    if left:
        raise git_ops.GitLifecycleError(f"Did not re-run the failed build in {worktree}: {left}")
    config.pipeline_branch = state.branch or on
    return True


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
    # With --base-ref (an epic's child), an empty branch left by an attempt
    # killed before its first commit sits at that ref, which the local default
    # branch may not have reached: count its commits against the ref.
    base = config.base_ref or git_ops.default_branch(repo)
    branch, dest = git_ops.resolve_new_branch(
        repo, config.change_name, base, taken=config.published_branches)
    git_ops.create_worktree(repo, branch, dest, start_point=config.base_ref or None)

    state.source_repo = str(repo)
    state.branch = branch
    state.worktree_path = str(dest)
    state.base_sha = git_ops.rev_parse_head(dest)
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
    the worktrees root for this change's worktree(s) and adopt the one whose
    state file was checkpointed last.
    """
    if not config.git.worktree or not config.change_name:
        return
    if config.state_file_path().exists():
        # In-place state: a --no-worktree run, or one already re-bound or
        # started inside the build's own worktree, whose branch it still owns
        # (without it, commit_stage skips every spec-stage commit).
        if not config.pipeline_branch:
            try:
                here = BuildState.load(config.state_file_path())
            except Exception as e:  # corrupt checkpoint — run_orchestrator reports it
                log.warning("Ignoring unreadable state at %s: %s", config.state_file_path(), e)
                return
            if _inside_own_worktree(config, here):
                config.pipeline_branch = here.branch or git_ops.current_branch(config.project_dir)
        return

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
    found: list[tuple[str, Path, BuildState]] = []
    for dest in candidates:
        probe = BuildConfig(
            project_dir=dest / offset, change_name=config.change_name,
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
        found.append((prior.updated_at, dest, prior))
    if not found:
        return
    # A build moved to a new `-2` worktree (a stop AT BOOTSTRAP, or a FAILED
    # checkpoint with no record and no commit) leaves the old one's checkpoint
    # behind, and `buildme-x` sorts before `buildme-x-2`: the build lives
    # where it last checkpointed. A tie keeps the sorted order.
    _, dest, prior = max(found, key=lambda f: f[0])
    config.rebind_project_dir(dest / offset)
    config.pipeline_branch = prior.branch or git_ops.current_branch(dest)
    log.info(
        "RESUME re-bound to existing worktree %s (branch=%s) — not creating a new one",
        dest, config.pipeline_branch,
    )
    print(f"WORKTREE:{dest}", flush=True)


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


def _completed_state_paths(config: BuildConfig) -> list[Path]:
    """The change's `.build-state.complete.json` files under *config*'s
    project: in specs/changes/<name>/ and each specs/archive/<date>-<name>/."""
    if not config.change_name:
        return []
    from .change_manager import archived_change_dirs
    active = completed_state_path(config.state_file_path())
    paths = [active] if active.exists() else []
    paths += [d / active.name
              for d in archived_change_dirs(config.specs_dir, config.change_name)
              if (d / active.name).exists()]
    return paths


def _completed_builds(config: BuildConfig) -> list[tuple[str, Path, BuildState]]:
    """This change's published builds, oldest first: ``(updated_at, path, state)``.

    Looks where resume looks (`_rebind_to_existing_worktree`,
    `_existing_state_path`): the project itself, and each
    ``buildme-<change>[-N]`` worktree under the worktrees root. Only a
    completed checkpoint at phase COMPLETE counts; a ``--spec-only`` or
    standalone ``tdd`` completion is not a published build.
    """
    if not config.change_name:
        return []
    projects = [config]
    repo = git_ops.repo_root(config.project_dir) if config.git.worktree else None
    if repo is not None:
        root = git_ops.worktrees_root(repo)
        if root.is_dir():
            offset = _project_offset(config.project_dir, repo)
            prefix = git_ops.worktree_dir_name(config.change_name)
            for dest in sorted(p for p in root.iterdir() if p.is_dir() and (
                    p.name == prefix or p.name.startswith(f"{prefix}-"))):
                probe = BuildConfig(project_dir=dest / offset,
                                    change_name=config.change_name)
                probe.specs_dir = dest / offset / "specs"
                projects.append(probe)
    found: dict[Path, tuple[str, Path, BuildState]] = {}
    for project in projects:
        for path in _completed_state_paths(project):
            key = path.resolve()
            if key in found:
                continue
            try:
                done = BuildState.load(path)
            except BudgetExceededError:
                raise
            except Exception as e:  # unreadable — not evidence of a published build
                log.warning("Ignoring unreadable completed state at %s: %s", path, e)
                continue
            if done.phase == BuildPhase.COMPLETE:
                found[key] = (done.updated_at, path, done)
    return sorted(found.values(), key=lambda f: f[0])


def _refuse_published_rerun(config: BuildConfig, completed: list[tuple[str, Path, BuildState]]) -> int:
    """Exit 3 with `BUILD:COMPLETE:<path>`: the change already published, and
    building it again is the user's call (`--force`). Writes nothing."""
    _, path, done = completed[-1]
    log.info("REFUSE re-run of published change=%s completed=%s branch=%s pr=%s",
             config.change_name, path, done.branch, done.pr_url)
    print(f"BUILD:COMPLETE:{path}", flush=True)
    print(f"{config.change_name} already published on branch {done.branch or '(none recorded)'}"
          f", PR {done.pr_url or '(none recorded)'}. Re-run with --force to start a new build "
          f"of it on a new branch.", file=sys.stderr, flush=True)
    return 3


def _spec_paths(config: BuildConfig) -> list[str]:
    """Explicit pathspec for the change's spec directory, relative to the
    worktree root. Never a whole-tree stage."""
    try:
        return [str(config.change_dir.relative_to(config.project_dir)),
                str((config.specs_dir / "config.yaml").relative_to(config.project_dir))]
    except ValueError:
        return []


# --- PR body hook -----------------------------------------------------------
#
# A contributor adds one section to the PR body without touching the pr skill.
# It takes (config, state, docs_dir), may write a file under
# `_pr_sections_dir(docs_dir)` whose first line is a Markdown heading, and
# returns that path, or None for no section. It runs after the last block and
# the docs/respec phases, right before publish hands the change to the pr
# skill, and each path it returns is passed as `--section`. A contributor that
# raises is logged and skipped; publish goes on. `walkthroughs.pr_section`
# reports the walkthroughs under docs/walkthroughs/ whose cited lines the build
# moved (and regenerates them when `regenerate_walkthroughs` is on).
def unfixed_findings_section(config: BuildConfig, state: BuildState, docs_dir: Path) -> Path | None:
    """The change's `unfixed-findings.md`, when the TDD engine wrote one: the
    accepted review findings a block that passed kept unfixed (its fix on pass
    was not kept, or not attempted). The file starts with its own heading."""
    path = Path(docs_dir) / review_rounds.UNFIXED_FINDINGS_FILE
    return path if path.is_file() else None


PRE_PUBLISH_CONTRIBUTORS: list[Callable[[BuildConfig, BuildState, Path], Path | None]] = [
    walkthroughs.pr_section,
    unfixed_findings_section,
]


def _pr_sections_dir(docs_dir: Path) -> Path:
    """Where section files for the PR body go. Never committed: the directory
    is in `git_ops.BOOKKEEPING_EXCLUDES`."""
    return docs_dir / "pr-sections"


def _pre_publish_sections(config: BuildConfig, state: BuildState, docs_dir: Path) -> list[Path]:
    """Run each of `PRE_PUBLISH_CONTRIBUTORS` and return the section files they
    wrote, in order. See the comment above `PRE_PUBLISH_CONTRIBUTORS`."""
    paths: list[Path] = []
    for contributor in PRE_PUBLISH_CONTRIBUTORS:
        name = getattr(contributor, "__name__", repr(contributor))
        from .gates import verification_inputs
        before = verification_inputs(config.test_command, config.project_dir)
        try:
            path = contributor(config, state, docs_dir)
        except BudgetExceededError:
            raise
        except Exception as e:
            after = verification_inputs(config.test_command, config.project_dir)
            if before != after:
                raise RuntimeError(f"Failed contributor {name} changed build inputs") from e
            log.warning("pre-publish contributor %s failed without changing inputs: %s", name, e)
            continue
        if path is not None:
            paths.append(Path(path))
    return paths


def _pr_skill_dir() -> Path:
    """The pr skill's script directory: ``<skills>/pr/scripts``. ``<skills>`` is
    ``parents[3]`` of this file (build_pipeline → scripts → buildme → skills),
    which is the ``skills/`` directory both in the repo and in an installed
    copy of the plugin, since that copy holds the whole plugin tree."""
    return Path(__file__).resolve().parents[3] / "pr" / "scripts"


def _pr_base_branch(repo: Path, base_ref: str) -> str | None:
    """The branch the PR targets when the build branched from *base_ref*
    (`build --base-ref`) and that commit is not on the default branch (item
    12): the branch *base_ref* names, else the one origin branch holding it.
    None when *base_ref* is empty or on the default branch, and None with a
    warning when no single origin branch holds it (the PR then targets the
    default branch)."""
    if not base_ref:
        return None
    default = git_ops.default_branch(repo)
    for tip in (f"refs/remotes/origin/{default}", f"refs/heads/{default}"):
        if git_ops._run(["git", "merge-base", "--is-ancestor", base_ref, tip], cwd=repo).returncode == 0:
            return None
    named = git_ops._run(["git", "rev-parse", "--symbolic-full-name", base_ref], cwd=repo).stdout.strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/"):
        if named.startswith(prefix):
            return named[len(prefix):]
    held = git_ops._run(["git", "for-each-ref", "--contains", base_ref, "--format=%(refname)",
                         "refs/remotes/origin/"], cwd=repo)
    branches = [r[len("refs/remotes/origin/"):] for r in held.stdout.split()
                if r != "refs/remotes/origin/HEAD"]
    if len(branches) == 1:
        return branches[0]
    log.warning("--base-ref %s is not on %s and %s origin branch holds it: the PR targets %s",
                base_ref, default, "more than one" if branches else "no", default)
    return None


def _pr_open_argv(
    project_dir: Path, change_dir: Path, branch: str, title: str, *,
    draft: bool = True, sections: list[Path] | None = None, base: str | None = None,
) -> list[str]:
    """``pr_pipeline open`` for a branch this pipeline already pushed.

    ``--repo`` is the project dir inside the build's worktree, not the
    worktree root: a nested project (monorepo subpackage) keeps its ``pr:``
    block in its own ``specs/config.yaml``, which the skill reads under
    ``--repo``; it finds the repo root itself for the templates and git.

    Run as a subprocess (``uv run --directory``), not imported, so buildme
    does not depend on uv resolving a path source between two workspace
    members inside an installed copy of the plugin."""
    argv = [
        "uv", "run", "--directory", str(_pr_skill_dir()),
        "python", "-m", "pr_pipeline", "open",
        "--repo", str(project_dir), "--change-dir", str(change_dir),
        "--head", branch, "--title", title, "--no-push",
    ]
    if base:
        argv += ["--base", base]
    # Always say which: git.pr is buildme's choice, and the skill's own
    # pr.draft setting must not override it.
    argv.append("--draft" if draft else "--ready")
    for section in sections or []:
        argv += ["--section", str(section)]
    return argv


def _worktree_section(state: BuildState, docs_dir: Path) -> Path | None:
    """The PR body's note that the build's worktree was left in place."""
    try:
        directory = _pr_sections_dir(docs_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "worktree.md"
        path.write_text(
            "## Worktree\n\nOpened by the buildme pipeline. The build's worktree is left in "
            f"place at `{state.worktree_path}` — removing it is the calling session's decision.\n"
        )
        return path
    except OSError as e:
        log.warning("Could not write the worktree section for the PR body: %s", e)
        return None


def _run_publish(
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    state_path: Path | None = None,
    docs_dir: Path | None = None,
) -> bool:
    """Push the build's branch and open its PR through the pr skill
    (`pr_pipeline open --no-push`), whose body follows the repo's PR template.
    Returns False when something was left for a human to finish (never
    raises, never fails the build).

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
    docs_dir = docs_dir or config.change_dir
    title = f"buildme: {config.change_name}"

    if not config.git.push:
        log.info("SKIP phase=PUBLISH reason=--no-push branch=%s", state.branch)
        print(f"PHASE:PUBLISH:SKIPPED:{state.branch}", flush=True)
        return True

    sections = [Path(p) for p in state.publish_sections]
    # An epic's child opens its PR against the default branch. Its --base-ref is
    # the local commit that holds the epic's ready specs, which no origin branch
    # holds until a sibling built on it pushes its own branch, or origin's
    # default sha.
    base = None if _epic_child(config) else _pr_base_branch(worktree, config.base_ref)
    pr_argv = _pr_open_argv(
        config.project_dir, docs_dir, state.branch, title, draft=draft, sections=sections,
        base=base,
    )
    manual = git_ops.manual_finish_commands(
        worktree, state.branch, title, include_pr=config.git.pr != "none", draft=draft,
        pr_skill_argv=pr_argv, base=base,
    )

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

    from .gates import verification_inputs
    expected = (state.verification or {}).get("metadata", {}).get("inputs")
    actual = verification_inputs(config.test_command, config.project_dir,
                                 parallel=config.parallel_tests,
                                 plan_revision=state.spec_gen.revision if state.spec_gen else "")
    if not (state.verification or {}).get("passed") or expected != actual:
        raise RuntimeError("Publication refused: final verification is missing or stale")
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

    # The pr skill reads the change's blocks and updated documents from the
    # checkpoint in docs_dir, so it must hold this state.
    if state_path is not None:
        state.checkpoint(state_path)
    url, diagnoses, res = git_ops.open_pr_with_skill(worktree, pr_argv)
    for line in diagnoses if res.ok else []:
        log.info("PR_DIAGNOSIS from the pr skill: %s", line)
    if not res.ok:
        if diagnoses:
            why = "; ".join(diagnoses)[:800]
        else:
            # No PR_DIAGNOSIS line: the skill died first (an import error, uv failing). Its
            # output is all there is, and a traceback's error is its last line, so keep the end.
            why = (res.stderr or res.stdout).strip()
            why = why if len(why) <= 800 else "…" + why[-800:]
        _publish_diagnosis(
            progress,
            f"the pr skill (`pr_pipeline open`) failed (exit {res.returncode}): "
            f"{why}. The branch is pushed — only the PR is missing.",
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
    # A one-line marker: a reason quoting multi-line output (a traceback, git's push
    # report) is folded onto it, or the lines after the first would carry no prefix.
    one_line = " | ".join(part.strip() for part in reason.splitlines() if part.strip())
    print(f"PUBLISH_DIAGNOSIS:{one_line}", flush=True)
