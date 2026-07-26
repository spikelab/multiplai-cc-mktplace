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
import sys
from pathlib import Path

from . import git_ops
from .change_manager import ChangeManager
from .config import BuildConfig
from .models import BuildPhase
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

    state_path = config.state_file_path()
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

        # Phase: Spec Generation
        if not state.is_phase_complete(BuildPhase.SPEC_GENERATION) and not config.mode == "only":
            log.info("START phase=SPEC_GENERATION")
            from .spec_generator import run_spec_generator
            result = await run_spec_generator(config, args)
            if result != 0:
                log.error("FAIL phase=SPEC_GENERATION exit_code=%d", result)
                state.advance_to(BuildPhase.FAILED, state_path)
                return result
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

        # Phase: Design Audit (best-effort — failures don't block the build)
        if not state.is_phase_complete(BuildPhase.DESIGN_AUDIT):
            log.info("START phase=DESIGN_AUDIT")
            from .llm_steps.spec_steps import run_design_audit
            try:
                gaps = await run_design_audit(config.change_dir, config)
                log.info("DONE phase=DESIGN_AUDIT gaps=%d", len(gaps) if gaps else 0)
            except Exception as audit_err:
                log.warning("Design audit LLM call failed (non-fatal): %s", audit_err)
            state.advance_to(BuildPhase.DESIGN_AUDIT, state_path)
            print("PHASE:DESIGN_AUDIT:COMPLETE", flush=True)

        # Phase: Prototype — a cheap artifact that proves the shape before the
        # expensive TDD build. Phase failure is never build failure.
        if not state.is_phase_complete(BuildPhase.PROTOTYPE):
            await _run_prototype_phase(config, state, progress, state_path)
            state.advance_to(BuildPhase.PROTOTYPE, state_path)
            print("PHASE:PROTOTYPE:COMPLETE", flush=True)

        # Stop if --spec-only
        if config.spec_only:
            log.info("DONE pipeline=spec-only")
            state.cleanup(state_path)
            print("RESULT:SUCCESS:spec-only", flush=True)
            return 0

        # Phase: Review (handled by SKILL.md wrapper — just advance state)
        if not state.is_phase_complete(BuildPhase.REVIEW):
            if config.auto:
                log.info("SKIP phase=REVIEW reason=--auto")
            else:
                # The explainer document goes first, above every other artifact:
                # reading it is the anti-slop step of the whole checkpoint —
                # it is where a dependency's real edge cases are stated before
                # anything is built on top of them.
                if config.unknowns_path.exists():
                    print(f"REVIEW:READ_FIRST:{config.unknowns_path}", flush=True)
                _print_prototype_review_paths(config)
                log.info("DONE phase=REVIEW")
            state.advance_to(BuildPhase.REVIEW, state_path)
            print("PHASE:REVIEW:COMPLETE", flush=True)

        # Phase: TDD Build
        if not state.is_phase_complete(BuildPhase.TDD_BUILD):
            log.info("START phase=TDD_BUILD")
            from .tdd_engine import run_tdd_engine
            result = await run_tdd_engine(config, args)
            # Reload state from disk — tdd_engine wrote its own updates (block status, TDD sub-state)
            # and our in-memory copy is stale. Without this, advance_to() overwrites tdd state with null.
            if state_path.exists():
                state = BuildState.load(state_path)
            if result != 0:
                log.error("FAIL phase=TDD_BUILD exit_code=%d", result)
                state.advance_to(BuildPhase.FAILED, state_path)
                return result
            state.advance_to(BuildPhase.TDD_BUILD, state_path)
            log.info("DONE phase=TDD_BUILD")
            print("PHASE:TDD_BUILD:COMPLETE", flush=True)

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
            log.info("START phase=ARCHIVE reason=--auto")
            # Clean state before archive moves the directory it lives in
            state.cleanup(state_path)
            archive_dest = cm.archive_change(config.change_dir)
            log.info("DONE phase=ARCHIVE dest=%s", archive_dest)
            git_ops.commit_stage(
                config,
                f"chore(specs): archive {config.change_name}",
                ["specs"],
            )
            print("PHASE:ARCHIVE:COMPLETE", flush=True)
        else:
            state.cleanup(state_path)
            log.info(
                "Archive skipped (manual). Run `buildme archive --change %s` when ready.",
                config.change_name,
            )
            print(f"PHASE:ARCHIVE:PENDING:{config.change_name}", flush=True)

        # Phase: Publish (push branch + open draft PR). Non-fatal by
        # construction — a failure leaves the branch and worktree intact with
        # the exact manual commands in build-progress.md, and the build still
        # reports success for the code it produced.
        published = _run_publish(config, state, progress)
        state.phase = BuildPhase.COMPLETE
        if published:
            # Only clear the progress file when there is nothing left for a
            # human to do — an unpushed branch's diagnosis must survive.
            progress.cleanup()
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

    # One regeneration pass of design.md/tasks.md from the notes.
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    if state.spec_gen.prototype_done:
        log.info("SKIP phase=PROTOTYPE_FEEDBACK reason=recorded-complete-in-state")
        return
    try:
        regenerated = await apply_prototype_findings(config)
        log.info("DONE phase=PROTOTYPE_FEEDBACK regenerated=%d", regenerated)
    except Exception as feedback_err:  # non-fatal
        log.warning("Prototype feedback pass failed (non-fatal): %s", feedback_err)
        _log_prototype_diagnosis(progress, config, str(feedback_err))
        return
    state.spec_gen.prototype_done = True
    state.checkpoint(state_path)


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
    base = git_ops.default_branch(repo)
    branch, dest = git_ops.resolve_new_branch(repo, config.change_name, base)
    git_ops.create_worktree(repo, branch, dest)

    state.source_repo = str(repo)
    state.branch = branch
    state.worktree_path = str(dest)
    config.rebind_project_dir(dest)
    config.pipeline_branch = branch
    state.state_file = str(config.state_file_path())
    log.info("Build re-bound to worktree %s on branch %s", dest, branch)
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
            mode=config.mode, project_dir=dest, change_name=config.change_name,
        )
        probe.specs_dir = dest / "specs"
        if not probe.state_file_path().exists():
            continue
        try:
            prior = BuildState.load(probe.state_file_path())
        except Exception as e:  # corrupt checkpoint — leave it for a fresh run
            log.warning("Ignoring unreadable state at %s: %s", probe.state_file_path(), e)
            continue
        config.rebind_project_dir(dest)
        config.pipeline_branch = prior.branch or git_ops.current_branch(dest)
        log.info(
            "RESUME re-bound to existing worktree %s (branch=%s) — not creating a new one",
            dest, config.pipeline_branch,
        )
        print(f"WORKTREE:{dest}", flush=True)
        return


def _spec_paths(config: BuildConfig) -> list[str]:
    """Explicit pathspec for the change's spec directory, relative to the
    worktree root. Never a whole-tree stage."""
    try:
        return [str(config.change_dir.relative_to(config.project_dir))]
    except ValueError:
        return []


def _pr_title_body(config: BuildConfig, state: BuildState) -> tuple[str, str]:
    """PR title from the change name; body from proposal.md's Why, the block
    list, and links to whichever companion artifacts the build produced."""
    title = f"buildme: {config.change_name}"
    parts: list[str] = []

    why = _proposal_why(config)
    if why:
        parts.append(f"## Why\n\n{why}")

    if state.tdd and state.tdd.blocks:
        lines = "\n".join(
            f"- Block {b.number}: {b.name} — {b.status.value}" for b in state.tdd.blocks
        )
        parts.append(f"## Blocks\n\n{lines}")

    change_rel = _spec_paths(config)
    base = change_rel[0] if change_rel else f"specs/changes/{config.change_name}"
    links = [
        f"- `{base}/{name}`"
        for name in ("unknowns.md", "prototype/NOTES.md", "implementation-notes.md", "respec.md")
        if (config.change_dir / name).exists()
    ]
    if links:
        parts.append("## Companion artifacts\n\n" + "\n".join(links))

    parts.append(
        "Opened by the buildme pipeline. The build's worktree is left in place at "
        f"`{state.worktree_path}` — removing it is the calling session's decision."
    )
    return title, "\n\n".join(parts)


def _proposal_why(config: BuildConfig) -> str:
    proposal = config.change_dir / "proposal.md"
    if not proposal.exists():
        return ""
    text = proposal.read_text()
    import re as _re
    m = _re.search(r"^##\s+Why\s*$(.*?)(?=^##\s|\Z)", text, _re.MULTILINE | _re.DOTALL)
    return m.group(1).strip() if m else ""


def _run_publish(config: BuildConfig, state: BuildState, progress: ProgressWriter) -> bool:
    """Push the build's branch and open its PR. Returns False when something
    was left for a human to finish (never raises, never fails the build).

    Skipped entirely when the pipeline does not own a branch: push/PR only
    ever act on a branch this pipeline created, which is what keeps
    ``--no-worktree`` identical to the pre-git-lifecycle behavior.
    """
    if not state.branch or not state.worktree_path:
        log.info("SKIP phase=PUBLISH reason=no-pipeline-branch")
        return True

    worktree = Path(state.worktree_path)
    draft = config.git.pr == "draft"
    title, body = _pr_title_body(config, state)
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
    log.info("PR opened: %s", url)
    if url:
        print(f"PR:{url}", flush=True)
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
