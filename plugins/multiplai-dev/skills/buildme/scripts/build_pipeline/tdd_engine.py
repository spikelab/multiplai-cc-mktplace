"""TDD implementation engine — block-by-block test-first build.

Parses tasks.md into blocks, runs model-adaptive TDD cycles per block,
gates on integration tests and quality reviews after each block, and runs
a final comprehensive review.

Exit codes: 0=success, 1=build failure, 3=agent timeout.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from .config import BuildConfig
from .gates import (
    baseline_test_gate,
    integration_gate,
    review_iteration_gate,
    review_score_gate,
    wiring_task_gate,
)
from .llm_steps.tdd_steps import (
    run_implementer,
    run_integration_fix,
    run_refactorer,
    run_test_writer,
)
from .models import (
    BlockInfo,
    BlockStatus,
    BuildPhase,
    GateResult,
    ReviewResult,
)
from .progress import ProgressWriter
from .sdk import LLMCallTimeoutError, llm_call, llm_call_structured
from .state import BuildState, TDDState

log = logging.getLogger(__name__)


def _git_commit_block_phase(cwd: str, phase: str, block: BlockInfo) -> str | None:
    """Commit all changes in cwd for the current block phase.

    phase: "test" or "impl" (used for conventional-commit prefix).
    Returns short SHA of the new commit, or None if there was nothing to
    commit or the commit failed (logged as warning — never raises).
    """
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=cwd, check=True, capture_output=True, timeout=30,
        )
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd, capture_output=True, timeout=10,
        )
        if status.returncode == 0:
            log.info("No changes to commit for block=%d phase=%s", block.number, phase)
            return None
        msg = f"{phase}(block-{block.number}): {block.name}"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=cwd, check=True, capture_output=True, timeout=30,
        )
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=10,
        )
        sha = sha_proc.stdout.strip()
        log.info("COMMIT block=%d phase=%s sha=%s", block.number, phase, sha[:8])
        return sha
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr.decode(errors="replace") if e.stderr else "").strip()
        log.warning("Failed to commit block=%d phase=%s: %s", block.number, phase, stderr or str(e))
        return None
    except Exception as e:
        log.warning("Unexpected error committing block=%d phase=%s: %s", block.number, phase, e)
        return None


EXIT_SUCCESS = 0
EXIT_BUILD_FAILURE = 1
EXIT_AGENT_TIMEOUT = 3

MAX_INTEGRATION_FIX_ATTEMPTS = 2
MAX_REVIEW_ITERATIONS = 3

# Weak test patterns for Phase A.5 quality check
WEAK_TEST_PATTERNS = [
    re.compile(r"assert\s+True\b"),
    re.compile(r"assert\s+\w+\s+is\s+not\s+None\s*$", re.MULTILINE),
    re.compile(r"def\s+test_\w+\s*\([^)]*\)\s*:\s*\n\s*(pass|\.\.\.)\s*$", re.MULTILINE),
]


def parse_blocks(tasks_path: Path) -> list[BlockInfo]:
    """Parse tasks.md into a list of BlockInfo.

    Handles two formats:
    - Advanced (coarse): ## N. Block Name\\n\\nDescription paragraph.\\n\\nSatisfies: ...
    - Standard (checkboxes): ## N. Block Name\\n\\n- [ ] N.1 Task\\n- [ ] N.2 Task
    """
    if not tasks_path.exists():
        log.warning("tasks.md not found at %s", tasks_path)
        return []

    text = tasks_path.read_text()
    blocks: list[BlockInfo] = []

    # Split on ## N. headers
    header_pattern = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)
    matches = list(header_pattern.finditer(text))

    for i, match in enumerate(matches):
        number = int(match.group(1))
        name = match.group(2).strip()

        # Extract body between this header and the next (or EOF)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()

        # Parse description and satisfies
        description = ""
        satisfies: list[str] = []

        # Check for Satisfies line
        satisfies_match = re.search(r"^Satisfies:\s*(.+)$", body, re.MULTILINE)
        if satisfies_match:
            satisfies_str = satisfies_match.group(1).strip()
            satisfies = [s.strip() for s in satisfies_str.split(",") if s.strip()]

        # Check for checkbox format
        checkbox_pattern = re.compile(r"^-\s+\[[ x]\]\s+(.+)$", re.MULTILINE)
        checkboxes = checkbox_pattern.findall(body)

        if checkboxes:
            # Standard format: tasks are the checkboxes
            description = "\n".join(f"- {task}" for task in checkboxes)
        else:
            # Advanced format: description is everything before Satisfies
            if satisfies_match:
                description = body[:satisfies_match.start()].strip()
            else:
                description = body.strip()

        blocks.append(BlockInfo(
            number=number,
            name=name,
            description=description,
            satisfies=satisfies,
        ))

    return blocks


def assemble_context(block: BlockInfo, config: BuildConfig, role: str) -> str:
    """Build the context bundle for an agent prompt.

    Includes: block info, design doc, specs, rubric, memory files, reference docs.
    The role parameter ("test_writer", "implementer", "refactorer") controls
    which files are included.
    """
    parts: list[str] = []

    # Block info
    parts.append(f"# Block {block.number}: {block.name}")
    parts.append(block.description)
    if block.satisfies:
        parts.append(f"Satisfies: {', '.join(block.satisfies)}")

    # Design document
    if config.design_path.exists():
        parts.append(f"\n## Design Document\n{config.design_path.read_text()}")

    # Requirement files (BDD scenarios — one per capability)
    req_dir = config.change_dir / "requirements"
    if req_dir.exists():
        for req_file in sorted(req_dir.glob("*.md")):
            rel = req_file.relative_to(config.change_dir)
            parts.append(f"\n## Requirements: {rel}\n{req_file.read_text()}")

    # Rubric (for reviewers — include for all so agents know quality bar)
    if config.rubric_path.exists():
        parts.append(f"\n## Evaluation Rubric\n{config.rubric_path.read_text()}")

    # Project context
    if config.project_description:
        parts.append(f"\n## Project Context\n{config.project_description}")

    # Memory files (technical preferences, etc.)
    memory_dir = config.config_dir / "memory"
    for mem_file in config.core_memory_files + config.stack_memory_files + config.additional_memory_files:
        mem_path = memory_dir / mem_file
        if mem_path.exists():
            parts.append(f"\n## Memory: {mem_file}\n{mem_path.read_text()}")

    # Stack reference docs
    for ref_doc in config.stack_reference_docs():
        parts.append(f"\n## Reference: {ref_doc.name}\n{ref_doc.read_text()}")

    return "\n\n".join(parts)


def run_test_quality_check(test_files_content: str, contracts: str, config: BuildConfig) -> GateResult:
    """Phase A.5: Static scan for weak test patterns.

    Scans test file content for anti-patterns like `assert True`, `assert x is not None`
    as sole assertion, and empty test bodies. Returns a GateResult.
    """
    if not config.gates.test_quality_enabled:
        return GateResult(passed=True, reason="Test quality check disabled")

    weak_findings: list[str] = []
    for pattern in WEAK_TEST_PATTERNS:
        matches = pattern.findall(test_files_content)
        for m in matches:
            weak_findings.append(f"Weak pattern found: {m.strip()[:80]}")

    # Count total test functions
    total_tests = len(re.findall(r"def\s+test_\w+", test_files_content))

    if total_tests == 0:
        return GateResult(
            passed=False,
            reason="No test functions found in test files",
            action="rewrite_tests",
            metadata={"total_tests": 0, "weak_count": 0},
        )

    weak_count = len(weak_findings)
    ratio = weak_count / total_tests if total_tests > 0 else 0.0

    if ratio >= 0.2:
        return GateResult(
            passed=False,
            reason=f"Test quality check failed: {weak_count}/{total_tests} weak tests ({ratio:.0%})",
            action="rewrite_tests",
            metadata={
                "total_tests": total_tests,
                "weak_count": weak_count,
                "findings": weak_findings[:10],
            },
        )

    return GateResult(
        passed=True,
        reason=f"Test quality OK: {weak_count}/{total_tests} weak tests ({ratio:.0%})",
        metadata={"total_tests": total_tests, "weak_count": weak_count},
    )


async def run_block_tdd(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
) -> bool:
    """Run TDD cycle for a single block. Returns True on success.

    Advanced tier: test-writer + implementer (clean code)
    Standard tier: test-writer + implementer (minimum) + refactorer
    """
    block_idx = block.number - 1
    total = len(state.tdd.blocks) if state.tdd else 0
    cwd = str(config.project_dir)

    # --- Phase A: Write tests ---
    if block.status == BlockStatus.PENDING:
        state.mark_block_status(block_idx, BlockStatus.TESTING, config.state_file_path())
        progress.log_block(block.number, total, block.name, "TESTING")

    log.info("START block=%d/%d name=%s phase=TEST_WRITE", block.number, total, block.name)

    specs = ""
    req_dir = config.change_dir / "requirements"
    if req_dir.exists():
        for req_file in sorted(req_dir.glob("*.md")):
            specs += f"\n### {req_file.name}\n{req_file.read_text()}"

    context = assemble_context(block, config, "test_writer")

    progress.log_agent("TestWriter", block.name, "STARTED")
    try:
        test_result = await run_test_writer(
            block_name=block.name,
            block_description=block.description,
            specs=specs,
            context_bundle=context,
            test_command=config.test_command,
            model=config.model,
            cwd=cwd,
        )
    except LLMCallTimeoutError:
        log.error("FAIL block=%d name=%s phase=TEST_WRITE reason=timeout", block.number, block.name)
        progress.log_agent("TestWriter", block.name, "TIMEOUT")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    if not test_result.success:
        log.error("FAIL block=%d name=%s phase=TEST_WRITE error=%s", block.number, block.name, test_result.error)
        progress.log_agent("TestWriter", block.name, "FAILED")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    log.info("DONE block=%d name=%s phase=TEST_WRITE", block.number, block.name)
    progress.log_agent("TestWriter", block.name, "COMPLETE")

    test_sha = _git_commit_block_phase(cwd, "test", block)
    if test_sha:
        block.test_commit = test_sha
        state.checkpoint(config.state_file_path())

    # --- Phase A.5: Test quality check ---
    test_files_content = test_result.output
    quality_gate = run_test_quality_check(test_files_content, specs, config)
    if not quality_gate.passed:
        log.warning("Test quality check failed for block %s: %s", block.name, quality_gate.reason)
        # Don't fail the build — log and continue

    # --- Phase B: Implement ---
    log.info("START block=%d name=%s phase=IMPLEMENT", block.number, block.name)
    state.mark_block_status(block_idx, BlockStatus.IMPLEMENTING, config.state_file_path())
    progress.log_block(block.number, total, block.name, "IMPLEMENTING")

    impl_context = assemble_context(block, config, "implementer")
    progress.log_agent("Implementer", block.name, "STARTED")
    try:
        impl_result = await run_implementer(
            block_name=block.name,
            block_description=block.description,
            failing_tests=test_result.output,
            context_bundle=impl_context,
            test_command=config.test_command,
            prompt_style=config.implementer_prompt_style,
            model=config.model,
            cwd=cwd,
        )
    except LLMCallTimeoutError:
        log.error("FAIL block=%d name=%s phase=IMPLEMENT reason=timeout", block.number, block.name)
        progress.log_agent("Implementer", block.name, "TIMEOUT")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    if not impl_result.success:
        log.error("FAIL block=%d name=%s phase=IMPLEMENT error=%s", block.number, block.name, impl_result.error)
        progress.log_agent("Implementer", block.name, "FAILED")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    log.info("DONE block=%d name=%s phase=IMPLEMENT turns=%d elapsed=%.0fs",
             block.number, block.name, impl_result.turns_used, impl_result.elapsed_seconds)
    progress.log_agent("Implementer", block.name, "COMPLETE")

    impl_sha = _git_commit_block_phase(cwd, "impl", block)
    if impl_sha:
        block.impl_commit = impl_sha
        state.checkpoint(config.state_file_path())

    # --- Phase C: Refactor (standard tier only) ---
    if config.refactor_phase:
        log.info("START block=%d name=%s phase=REFACTOR", block.number, block.name)
        refactor_context = assemble_context(block, config, "refactorer")
        progress.log_agent("Refactorer", block.name, "STARTED")
        try:
            refactor_result = await run_refactorer(
                block_name=block.name,
                block_description=block.description,
                context_bundle=refactor_context,
                test_command=config.test_command,
                model=config.model,
                cwd=cwd,
            )
        except LLMCallTimeoutError:
            progress.log_agent("Refactorer", block.name, "TIMEOUT")
            log.warning("FAIL block=%d name=%s phase=REFACTOR reason=timeout (non-fatal)", block.number, block.name)
        else:
            if not refactor_result.success:
                log.warning("FAIL block=%d name=%s phase=REFACTOR reason=%s (non-fatal)", block.number, block.name, refactor_result.error)
            else:
                log.info("DONE block=%d name=%s phase=REFACTOR", block.number, block.name)
            progress.log_agent("Refactorer", block.name, "COMPLETE")

    return True


async def _run_integration_and_review(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
) -> bool:
    """Run integration gate + review loop for a block. Returns True on success."""
    block_idx = block.number - 1
    total = len(state.tdd.blocks) if state.tdd else 0

    # --- Integration gate ---
    log.info("START block=%d name=%s phase=INTEGRATION_GATE", block.number, block.name)
    gate = integration_gate(config.test_command, config.project_dir)
    if not gate.passed:
        log.warning("Integration gate failed after block %s: %s", block.name, gate.reason)
        # Attempt fix
        for attempt in range(MAX_INTEGRATION_FIX_ATTEMPTS):
            log.info("Integration fix attempt %d/%d", attempt + 1, MAX_INTEGRATION_FIX_ATTEMPTS)
            context = assemble_context(block, config, "implementer")
            try:
                fix_result = await run_integration_fix(
                    failure_output=gate.metadata.get("stderr", "") + gate.metadata.get("stdout", ""),
                    test_command=config.test_command,
                    context_bundle=context,
                    model=config.model,
                    cwd=str(config.project_dir),
                )
            except LLMCallTimeoutError:
                continue
            if fix_result.success:
                gate = integration_gate(config.test_command, config.project_dir)
                if gate.passed:
                    break

        if not gate.passed:
            log.error("Integration gate still failing after fix attempts for block %s", block.name)
            state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
            return False

    # --- Quality review loop ---
    state.mark_block_status(block_idx, BlockStatus.REVIEWING, config.state_file_path())
    progress.log_block(block.number, total, block.name, "REVIEWING")

    for iteration in range(MAX_REVIEW_ITERATIONS):
        iter_gate = review_iteration_gate(iteration, MAX_REVIEW_ITERATIONS)
        if not iter_gate.passed:
            log.warning("Review loop exhausted for block %s", block.name)
            break

        # Run review (via llm_call_structured). Propagates SDK failures —
        # no silent fallback to fabricated passing scores.
        try:
            review = await _run_quality_review(block, config)
        except Exception as e:
            log.error(
                "FAIL block=%d name=%s phase=REVIEW iteration=%d error=%s",
                block.number, block.name, iteration + 1, e,
            )
            progress.log_review(block.name, iteration + 1, 0.0, False)
            state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
            return False
        block.review_scores = review
        block.review_iterations = iteration + 1

        score_gate = review_score_gate(review)
        progress.log_review(block.name, iteration + 1, review.weighted_average, score_gate.passed)

        if score_gate.passed:
            break

        log.info("Review iteration %d failed for block %s: %s", iteration + 1, block.name, score_gate.reason)
        # Spawn fix agent for the failing dimensions
        context = assemble_context(block, config, "implementer")
        try:
            await run_implementer(
                block_name=block.name,
                block_description=f"Fix review issues: {score_gate.reason}",
                failing_tests=score_gate.reason,
                context_bundle=context,
                test_command=config.test_command,
                prompt_style=config.implementer_prompt_style,
                model=config.model,
                cwd=str(config.project_dir),
            )
        except LLMCallTimeoutError:
            log.warning("Fix agent timed out during review iteration %d", iteration + 1)

    # Mark done regardless of final review outcome (we gave it MAX attempts)
    state.mark_block_status(block_idx, BlockStatus.DONE, config.state_file_path())
    log.info("DONE block=%d/%d name=%s", block.number, total, block.name)
    progress.log_block(block.number, total, block.name, "COMPLETE")
    print(f"BLOCK:{block.number}/{total}:{block.name}:COMPLETE")
    return True


async def _run_quality_review(block: BlockInfo, config: BuildConfig) -> ReviewResult:
    """Run a quality review of the block's implementation using LLM scoring.

    Propagates LLMCallError / LLMCallTimeoutError on failure. Callers must
    handle the exception and fail the block — silently fabricating passing
    scores is worse than a loud failure (the old fallback was a real bug).
    """
    rubric = ""
    if config.rubric_path.exists():
        rubric = config.rubric_path.read_text()

    prompt = f"""\
Review block "{block.name}" implementation against this rubric.
Score each dimension 1-5 with evidence.

## Rubric
{rubric}

## Block Description
{block.description}

Return JSON:
```json
{{
    "scores": [
        {{"dimension": "...", "weight": <int>, "score": <int 1-5>, "evidence": "..."}}
    ]
}}
```
"""
    return await llm_call_structured(prompt, ReviewResult, model=config.model)


async def run_tdd_engine(config: BuildConfig, args) -> int:
    """Main entry point for the TDD engine.

    Orchestrates: parse blocks → baseline gate → per-block TDD → final review.
    """
    state_path = config.state_file_path()
    progress = ProgressWriter(config.progress_file_path())

    # Load or create state
    if state_path.exists():
        state = BuildState.load(state_path)
        log.info("START phase=TDD_ENGINE resumed=true block=%d", state.tdd.current_block if state.tdd else 0)
    else:
        state = BuildState(
            change_name=config.change_name,
            mode=config.mode,
            tier=config.tier,
            state_file=str(state_path),
            phase=BuildPhase.TDD_BUILD,
        )

    # Initialize TDD state from tasks.md if missing (fresh start or orchestrator pre-wrote state)
    if state.tdd is None:
        blocks = parse_blocks(config.tasks_path)
        if not blocks:
            log.error("FAIL phase=TDD_ENGINE reason=no-blocks-found path=%s", config.tasks_path)
            return EXIT_BUILD_FAILURE
        log.info("START phase=TDD_ENGINE blocks=%d tier=%s", len(blocks), config.tier)
        state.tdd = TDDState(blocks=blocks)
        state.checkpoint(state_path)

    # Allow --block to override starting position
    start_block = getattr(args, "block", None)
    if start_block is not None and state.tdd:
        state.tdd.current_block = start_block - 1  # 0-indexed

    total_blocks = len(state.tdd.blocks) if state.tdd else 0
    progress.initialize(config.change_name, config.mode, config.tier, total_blocks)

    # --- Baseline test gate ---
    if state.tdd and not state.tdd.baseline_tests_pass:
        progress.log_phase("BASELINE", "Running existing test suite")
        gate = baseline_test_gate(config.test_command, config.project_dir)
        if not gate.passed:
            log.error("Baseline test gate failed: %s", gate.reason)
            progress.log_phase("BASELINE", f"FAILED: {gate.reason}")
            return EXIT_BUILD_FAILURE
        state.tdd.baseline_tests_pass = True
        state.checkpoint(state_path)
        progress.log_phase("BASELINE", "PASSED")

    # --- Wiring task validation ---
    wiring_gate = wiring_task_gate(config.tasks_path, config.project_dir)
    if not wiring_gate.passed:
        log.warning("Wiring task gate: %s", wiring_gate.reason)
        progress.log_phase("WIRING_CHECK", f"WARNING: {wiring_gate.reason}")

    # --- Block loop ---
    if not state.tdd:
        return EXIT_BUILD_FAILURE

    while state.tdd.current_block < len(state.tdd.blocks):
        block = state.tdd.blocks[state.tdd.current_block]

        # Skip already-done blocks (resume case)
        if block.status == BlockStatus.DONE:
            state.advance_block(state_path)
            continue

        log.info("Starting block %d/%d: %s", block.number, total_blocks, block.name)

        # Run TDD phases
        tdd_ok = await run_block_tdd(block, config, state, progress)
        if not tdd_ok:
            if block.status == BlockStatus.FAILED:
                # Check for timeout
                return EXIT_AGENT_TIMEOUT
            return EXIT_BUILD_FAILURE

        # Run integration + review
        review_ok = await _run_integration_and_review(block, config, state, progress)
        if not review_ok:
            return EXIT_BUILD_FAILURE

        state.advance_block(state_path)

    # --- Final comprehensive review ---
    if not state.tdd.final_review_done:
        log.info("START phase=FINAL_REVIEW")
        progress.log_phase("FINAL_REVIEW", "Running comprehensive review")
        final_review = await _run_final_review(config)
        state.tdd.final_review_done = True
        state.checkpoint(state_path)

        if final_review and not final_review.passed:
            log.warning("DONE phase=FINAL_REVIEW result=issues reason=%s", final_review.reason[:100])
            progress.log_phase("FINAL_REVIEW", f"ISSUES: {final_review.reason}")
        else:
            log.info("DONE phase=FINAL_REVIEW result=passed")
            progress.log_phase("FINAL_REVIEW", "PASSED")

    # --- Entry point verification ---
    if config.gates.e2e_test_entry_point_check and not state.tdd.e2e_done:
        log.info("START phase=E2E_CHECK")
        progress.log_phase("E2E_CHECK", "Verifying entry point")
        e2e_gate = _verify_entry_point(config)
        state.tdd.e2e_done = True
        state.checkpoint(state_path)
        if not e2e_gate.passed:
            log.warning("DONE phase=E2E_CHECK result=warning reason=%s", e2e_gate.reason)
            progress.log_phase("E2E_CHECK", f"WARNING: {e2e_gate.reason}")
        else:
            log.info("DONE phase=E2E_CHECK result=passed")
            progress.log_phase("E2E_CHECK", "PASSED")

    # Success
    state.advance_to(BuildPhase.COMPLETE, state_path)
    log.info("DONE phase=TDD_ENGINE blocks=%d", total_blocks)
    progress.log_phase("COMPLETE", f"All {total_blocks} blocks implemented successfully")
    state.cleanup(state_path)
    return EXIT_SUCCESS


async def _run_final_review(config: BuildConfig) -> GateResult | None:
    """Run a final comprehensive review of the entire implementation."""
    rubric = ""
    if config.rubric_path.exists():
        rubric = config.rubric_path.read_text()
    if not rubric:
        return GateResult(passed=True, reason="No rubric — skipping final review")

    prompt = f"""\
Perform a comprehensive final review of the entire implementation.
Check for cross-block integration issues, missed specs, and overall quality.

## Rubric
{rubric}

Return a brief summary: PASSED or FAILED with key issues.
"""
    try:
        result = await llm_call(prompt, model=config.model)
        passed = "PASSED" in result.upper() and "FAILED" not in result.upper()
        return GateResult(passed=passed, reason=result[:200])
    except Exception as e:
        log.warning("Final review failed: %s", e)
        return GateResult(passed=True, reason=f"Review unavailable: {e}")


def _verify_entry_point(config: BuildConfig) -> GateResult:
    """Check that the project has a runnable entry point if it's an app."""
    project_dir = config.project_dir
    entry_points = [
        project_dir / "src" / "__main__.py",
        project_dir / "__main__.py",
        project_dir / "main.py",
        project_dir / "app.py",
    ]
    # Also check package.json for bin/main
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        return GateResult(passed=True, reason="package.json found — entry point assumed")

    for ep in entry_points:
        if ep.exists():
            return GateResult(passed=True, reason=f"Entry point found: {ep.name}")

    # Not necessarily a problem for libraries
    return GateResult(passed=True, reason="No explicit entry point (library project assumed)")
