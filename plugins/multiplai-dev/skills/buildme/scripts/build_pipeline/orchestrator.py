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

from .change_manager import ChangeManager
from .config import BuildConfig
from .models import BuildPhase
from .progress import ProgressWriter
from .state import BuildState

log = logging.getLogger(__name__)


async def run_orchestrator(config: BuildConfig, args) -> int:
    """Main entry point for the build orchestrator."""
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

        # Phase: Archive
        # In --auto mode, archive immediately (merge delta specs → main registry,
        # move change to archive/). Otherwise, leave the change in place so the
        # user can review before running `buildme archive --change <name>`.
        state.advance_to(BuildPhase.COMPLETE, state_path)
        if config.auto:
            log.info("START phase=ARCHIVE reason=--auto")
            # Clean state/progress before archive moves the directory
            state.cleanup(state_path)
            progress.cleanup()
            archive_dest = cm.archive_change(config.change_dir)
            log.info("DONE phase=ARCHIVE dest=%s", archive_dest)
            print("PHASE:ARCHIVE:COMPLETE", flush=True)
            print("RESULT:SUCCESS", flush=True)
        else:
            state.cleanup(state_path)
            progress.cleanup()
            log.info(
                "Archive skipped (manual). Run `buildme archive --change %s` when ready.",
                config.change_name,
            )
            print(f"PHASE:ARCHIVE:PENDING:{config.change_name}", flush=True)
            print("RESULT:SUCCESS", flush=True)
        log.info("DONE pipeline=complete change=%s", config.change_name)
        return 0

    except Exception as e:
        log.error("FAIL pipeline change=%s error=%s", config.change_name, e, exc_info=True)
        print(f"ERROR:{e}", file=sys.stderr, flush=True)
        return 1


async def _run_bootstrap(
    config: BuildConfig, state: BuildState, cm: ChangeManager, state_path: Path,
) -> None:
    """Initialize git, project skeleton, specs/ dir, and config.yaml."""
    import subprocess

    project = config.project_dir

    # Git init if needed
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
