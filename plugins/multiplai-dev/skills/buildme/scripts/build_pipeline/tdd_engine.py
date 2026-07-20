"""TDD implementation engine — block-by-block test-first build.

Parses tasks.md into blocks, runs model-adaptive TDD cycles per block,
gates on integration tests and quality reviews after each block, and runs
a final comprehensive review.

Exit codes: 0=success, 1=build failure, 3=agent timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from .change_manager import extract_global_constraints
from .config import BuildConfig
from .gates import (
    _repo_trusted,
    agent_status_gate,
    baseline_test_gate,
    integration_gate,
    red_gate,
    review_iteration_gate,
    review_score_gate,
    run_test_suite,
    wiring_task_gate,
)
from .llm_steps.review_steps import run_code_review
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
    FinalReviewVerdict,
    GateResult,
    ReviewResult,
    TestQualityAudit,
)
from .progress import ProgressWriter
from .prompts.review import FINAL_REVIEW_PROMPT
from .prompts.test_writing import TEST_QUALITY_PROMPT
from .sdk import llm_call_structured
from .state import BuildState, TDDState

log = logging.getLogger(__name__)


def _git_commit_block_phase(config: BuildConfig, phase: str, block: BlockInfo) -> str | None:
    """Commit the block phase's changes in the project repo.

    phase: "test" or "impl" (used for conventional-commit prefix).
    Returns short SHA of the new commit, or None if there was nothing to
    commit or the commit failed (logged as warning — never raises).

    Stages everything EXCEPT buildme's own bookkeeping files (build-progress.md
    and .build-state.json) so they don't leak into the user's per-block commits.
    """
    cwd = str(config.project_dir)
    # Exclude the bookkeeping files via :(exclude) pathspecs, relative to the repo.
    excludes: list[str] = []
    for bookkeeping in (config.progress_file_path(), config.state_file_path()):
        try:
            rel = bookkeeping.relative_to(config.project_dir)
        except ValueError:
            continue  # outside the repo — git add under '.' won't touch it anyway
        excludes.append(f":(exclude){rel}")
    try:
        subprocess.run(
            ["git", "add", "-A", "--", ".", *excludes],
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


def _git_rev_parse_head(config: BuildConfig) -> str | None:
    """Return the project repo's HEAD SHA, or None (not a repo / no commits)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(config.project_dir), capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        log.warning("git rev-parse HEAD failed: %s", proc.stderr.strip())
    except Exception as e:
        log.warning("git rev-parse HEAD failed: %s", e)
    return None


# git's well-known empty-tree object. Used as the diff baseline when the
# project repo has no commits at block start (fresh `git init`): diffing
# against the empty tree makes block 1's committed work visible to the
# reviewer, whereas a None baseline would fall back to `git diff HEAD`,
# which post-commit shows nothing.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


# Cap the diff handed to the reviewer so a pathological block can't blow the
# context window. 150k chars ≈ 40k tokens — far above any sane block diff.
MAX_REVIEW_DIFF_CHARS = 150_000


_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")
MAX_TEST_SCAN_CHARS = 200_000


def _read_block_test_files(config: BuildConfig, block: BlockInfo) -> str:
    """Concatenated source of the test files this block added or modified.

    The quality gate scans real test source, never the test-writer agent's
    prose report — a report contains no `def test_*`, so scanning it makes the
    gate fail every block on "No test functions found". Returns "" when git
    cannot tell us what changed; the caller falls back to the agent output.
    """
    target = block.baseline_commit or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", target],
            cwd=str(config.project_dir), capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.warning("Could not list changed files for block %d: %s",
                        block.number, proc.stderr.strip())
            return ""
        names = proc.stdout.splitlines()
    except Exception as e:
        log.warning("Could not list changed files for block %d: %s", block.number, e)
        return ""

    parts: list[str] = []
    for name in names:
        if not _TEST_FILE_RE.search(name.strip()):
            continue
        path = config.project_dir / name.strip()
        try:
            parts.append(f"# --- {name.strip()} ---\n{path.read_text()}")
        except OSError as e:
            log.warning("Could not read test file %s: %s", name, e)

    content = "\n\n".join(parts)
    return content[:MAX_TEST_SCAN_CHARS]


def _capture_block_diff(config: BuildConfig, block: BlockInfo) -> str:
    """Capture everything the block changed: baseline commit → working tree.

    Blocks commit at phase boundaries (test/impl commits), so diffing from the
    recorded pre-block baseline covers those commits plus any uncommitted
    refactor/review-fix edits. An empty repo at block start records
    EMPTY_TREE_SHA as the baseline (see run_block_tdd). The `git diff HEAD`
    fallback is a last resort for a truly-None baseline (e.g. checkpoints
    written before baselines existed) and misses committed work. Returns ""
    on git failure — never raises.

    Known limitation: brand-new files that are still untracked (created after
    the impl commit, e.g. by a review-fix agent) don't appear until the next
    phase commit tracks them.
    """
    target = block.baseline_commit or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", target],
            cwd=str(config.project_dir), capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.warning(
                "Failed to capture diff for block %d (git diff %s): %s",
                block.number, target, proc.stderr.strip(),
            )
            return ""
        diff = proc.stdout
    except Exception as e:
        log.warning("Failed to capture diff for block %d: %s", block.number, e)
        return ""
    if len(diff) > MAX_REVIEW_DIFF_CHARS:
        diff = diff[:MAX_REVIEW_DIFF_CHARS] + "\n... (diff truncated for review)"
    return diff


EXIT_SUCCESS = 0
EXIT_BUILD_FAILURE = 1
EXIT_AGENT_TIMEOUT = 3

# Circuit breaker: attempts 1-2 run the scoped fix prompt; the final attempt
# escalates — question-the-architecture prompt on config.review_model (when
# set). Exhaustion fails the block with a diagnosis in build-progress.md.
MAX_INTEGRATION_FIX_ATTEMPTS = 3
MAX_REVIEW_ITERATIONS = 3

# Cap for RED/GREEN evidence blobs stored in block state and fed to the
# reviewer — the interesting part of a pytest run is the tail.
MAX_EVIDENCE_CHARS = 4000


def _trim_evidence(output: str) -> str:
    return output[-MAX_EVIDENCE_CHARS:]

# Weak test patterns for Phase A.5 quality check (line-level; per-function
# mock heuristics live in _scan_test_function)
WEAK_TEST_PATTERNS = [
    re.compile(r"assert\s+True\b"),
    re.compile(r"assert\s+\w+\s+is\s+not\s+None\s*$", re.MULTILINE),
    re.compile(r"def\s+test_\w+\s*\([^)]*\)\s*:\s*\n\s*(pass|\.\.\.)\s*$", re.MULTILINE),
    # `assert x.called` (or obj.method.called) alone on a line — verifies the
    # mock was touched, not what the code did.
    re.compile(r"^\s*assert\s+\w+(?:\.\w+)*\.called\s*(?:#.*)?$", re.MULTILINE),
]

# Per-function heuristics (superpowers testing-anti-patterns): a test whose
# only assertions interrogate a mock, or whose mock scaffolding outweighs its
# assertions, is testing the mock — not the behavior.
_TEST_FUNC_RE = re.compile(
    r"^([ \t]*)def\s+(test_\w+)\s*\([^)]*\)\s*(?:->\s*[^:]+)?:", re.MULTILINE
)
_ASSERT_LINE_RE = re.compile(r"^\s*assert\s")
_MOCK_ASSERT_RE = re.compile(
    r"^\s*(?:assert\s+.*\.(?:called|call_count|call_args)\b"
    r"|\w+(?:\.\w+)*\.assert_(?:called|any_call|has_calls|not_called)\w*\()"
)
_MOCK_SETUP_RE = re.compile(
    r"MagicMock\(|(?<!\w)Mock\(|(?<!\w)patch[.(]|\.return_value|\.side_effect"
)


def _iter_test_functions(content: str):
    """Yield (name, body) for each test function; body ends at the next line
    indented at or below the def's level."""
    matches = list(_TEST_FUNC_RE.finditer(content))
    for i, m in enumerate(matches):
        indent = len(m.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body_lines = []
        for line in content[m.end():end].splitlines():
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            body_lines.append(line)
        yield m.group(2), "\n".join(body_lines)


def _scan_test_function(name: str, body: str) -> list[str]:
    """Mechanical mock anti-pattern findings for one test function."""
    lines = body.splitlines()
    mock_asserts = [ln for ln in lines if _MOCK_ASSERT_RE.match(ln)]
    real_asserts = [
        ln for ln in lines
        if _ASSERT_LINE_RE.match(ln) and not _MOCK_ASSERT_RE.match(ln)
    ]
    setup_lines = [ln for ln in lines if _MOCK_SETUP_RE.search(ln)]

    findings = []
    if mock_asserts and not real_asserts:
        findings.append(
            f"{name}: mock-assertion-only — every assertion interrogates a mock; "
            f"assert an observable outcome of the code under test"
        )
    total_asserts = len(mock_asserts) + len(real_asserts)
    if total_asserts and len(setup_lines) > total_asserts:
        findings.append(
            f"{name}: mock-setup-dominant — {len(setup_lines)} mock-setup lines vs "
            f"{total_asserts} assertion(s); the test mostly configures mocks"
        )
    return findings


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

        # Parse the Interfaces section: `- Produces: <sig>` / `- Consumes: <sig>`
        # bullet lines under an `Interfaces:` header (until the first
        # non-bullet, non-blank line).
        produces: list[str] = []
        consumes: list[str] = []
        interfaces_match = re.search(r"^Interfaces:\s*$", body, re.MULTILINE)
        if interfaces_match:
            for line in body[interfaces_match.end():].splitlines():
                bullet = re.match(r"^-\s+(Produces|Consumes)\b[^:]*:\s*(.+)$", line)
                if bullet:
                    value = bullet.group(2).strip()
                    if value and value.lower() != "(none)":
                        (produces if bullet.group(1) == "Produces" else consumes).append(value)
                elif line.strip():
                    break

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
            produces=produces,
            consumes=consumes,
        ))

    return blocks


def _global_constraints_text(config: BuildConfig) -> str:
    """The design doc's Global Constraints section ("" when absent)."""
    if not config.design_path.exists():
        return ""
    return extract_global_constraints(config.design_path.read_text())


def assemble_context(
    block: BlockInfo,
    config: BuildConfig,
    role: str,
    blocks: list[BlockInfo] | None = None,
) -> str:
    """Build the context bundle for an agent prompt.

    Includes: block info, global constraints, cross-block interfaces, design
    doc, specs, rubric, memory files, reference docs. The role parameter
    ("test_writer", "implementer", "refactorer") controls which files are
    included. ``blocks`` (the full block list) threads earlier blocks'
    Produces signatures into this block's context.
    """
    parts: list[str] = []

    # Block info
    parts.append(f"# Block {block.number}: {block.name}")
    parts.append(block.description)
    if block.satisfies:
        parts.append(f"Satisfies: {', '.join(block.satisfies)}")

    # Global constraints — verbatim, ahead of everything else the agent reads
    constraints = _global_constraints_text(config)
    if constraints:
        parts.append(
            f"\n## Global Constraints (project-wide, non-negotiable)\n{constraints}"
        )

    # This block's interface contract
    if block.produces or block.consumes:
        iface_lines = [f"- Produces: {sig}" for sig in block.produces]
        iface_lines += [f"- Consumes: {sig}" for sig in block.consumes]
        parts.append("\n## Interfaces (this block)\n" + "\n".join(iface_lines))

    # Earlier blocks' Produces signatures — dependent blocks call these
    # exactly as written, never re-derived from memory.
    if blocks:
        prior = [
            f"- Block {b.number} ({b.name}) produces: {sig}"
            for b in blocks if b.number < block.number
            for sig in b.produces
        ]
        if prior:
            parts.append(
                "\n## Interfaces from earlier blocks (use these signatures verbatim)\n"
                + "\n".join(prior)
            )

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

    # Per-function mock heuristics (mock-assertion-only, mock-setup-dominant)
    for func_name, body in _iter_test_functions(test_files_content):
        weak_findings.extend(_scan_test_function(func_name, body))

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


async def _audit_test_quality(test_files_content: str, contracts: str, config: BuildConfig) -> TestQualityAudit:
    """LLM adjudication of the static weak-test scan (TEST_QUALITY_PROMPT)."""
    prompt = TEST_QUALITY_PROMPT.format(
        test_files=test_files_content,
        contracts=contracts or "(none provided)",
    )
    return await llm_call_structured(prompt, TestQualityAudit, model=config.model, max_retries=1)


async def _enforce_test_quality(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    specs: str,
    context: str,
    block_idx: int,
    test_files_content: str,
) -> bool:
    """Enforced test-quality gate: static scan, LLM-adjudicated, no advisory path.

    Static scan fail → the LLM auditor adjudicates (the regex scan is coarse;
    the auditor confirms or overturns). Confirmed weak → one test-writer retry
    carrying the findings, then re-scan (+ re-audit). Still weak → block
    FAILED. Auditor errors also fail the block — an unverifiable gate must
    not silently pass.
    """
    scan = run_test_quality_check(test_files_content, specs, config)
    if scan.passed:
        return True

    log.warning(
        "Static weak-test scan failed for block %s (%s) — LLM auditor adjudicating",
        block.name, scan.reason,
    )
    try:
        audit = await _audit_test_quality(test_files_content, specs, config)
    except Exception as e:
        log.error("FAIL block=%d name=%s phase=TEST_QUALITY_AUDIT error=%s",
                  block.number, block.name, e)
        progress.log_agent("TestQualityAuditor", block.name, f"ERROR: {e}")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    if audit.passed:
        log.info("Test-quality auditor overturned the static scan for block %s", block.name)
        progress.log_agent("TestQualityAuditor", block.name, "OVERTURNED static scan — tests OK")
        return True

    findings = audit.findings_text() or scan.reason
    log.warning("Test-quality auditor confirmed weak tests for block %s — one test-writer retry",
                block.name)
    progress.log_agent("TestQualityAuditor", block.name, "CONFIRMED weak tests — retrying test writer")
    retry = await run_test_writer(
        block_name=block.name,
        block_description=(
            f"{block.description}\n\n"
            f"## Test Quality Failure — rewrite the weak tests\n"
            f"A quality audit confirmed these tests are too weak to gate an "
            f"implementation:\n{findings}\n\n"
            f"Rewrite them so every test asserts a meaningful behavioral outcome."
        ),
        specs=specs,
        context_bundle=context,
        test_command=config.test_command,
        model=config.model,
        cwd=str(config.project_dir),
    )
    if not retry.success:
        block.timed_out = retry.timed_out
        log.error("FAIL block=%d name=%s phase=TEST_QUALITY_RETRY error=%s",
                  block.number, block.name, retry.error)
        progress.log_agent("TestWriter", block.name, "TIMEOUT" if retry.timed_out else "FAILED")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False
    sha = _git_commit_block_phase(config, "test", block)
    if sha:
        block.test_commit = sha
        state.checkpoint(config.state_file_path())

    retry_source = _read_block_test_files(config, block) or retry.output
    rescan = run_test_quality_check(retry_source, specs, config)
    if rescan.passed:
        return True
    try:
        re_audit = await _audit_test_quality(retry_source, specs, config)
    except Exception as e:
        log.error("FAIL block=%d name=%s phase=TEST_QUALITY_REAUDIT error=%s",
                  block.number, block.name, e)
        progress.log_agent("TestQualityAuditor", block.name, f"ERROR: {e}")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False
    if re_audit.passed:
        return True

    log.error("FAIL block=%d name=%s phase=TEST_QUALITY reason=still-weak-after-retry",
              block.number, block.name)
    progress.log_agent("TestQualityAuditor", block.name, "STILL WEAK after retry — block failed")
    state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
    return False


async def _enforce_red_gate(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    specs: str,
    context: str,
    block_idx: int,
) -> bool:
    """Prove the block's tests fail for the right reason before implementing.

    Runs the suite and applies red_gate. On failure: one test-writer retry
    carrying the gate's reason; a second failure marks the block FAILED.
    On pass: stores trimmed RED output as evidence in block state and the
    progress log. Returns True when RED is confirmed (or unverifiable
    because no test command is configured).
    """
    if not config.test_command:
        log.info("RED gate skipped for block %s: no test command configured", block.name)
        return True

    for attempt in (1, 2):
        exit_code, output = run_test_suite(config.test_command, config.project_dir)
        gate = red_gate(output, exit_code)
        if gate.passed:
            block.red_evidence = _trim_evidence(output)
            state.checkpoint(config.state_file_path())
            log.info("RED confirmed block=%d name=%s (%s)", block.number, block.name, gate.reason)
            progress.log_agent("RedGate", block.name, "RED CONFIRMED")
            progress.log_evidence("RED", block.name, block.red_evidence)
            return True

        if attempt == 1:
            log.warning(
                "RED gate failed for block %s (action=%s): %s — one test-writer retry",
                block.name, gate.action, gate.reason,
            )
            progress.log_agent("RedGate", block.name, f"FAILED ({gate.action}) — retrying test writer")
            retry = await run_test_writer(
                block_name=block.name,
                block_description=(
                    f"{block.description}\n\n"
                    f"## RED Gate Failure — fix the tests\n"
                    f"The previously written tests did not fail for the right reason:\n"
                    f"{gate.reason}\n\n"
                    f"Required action: {gate.action}. Rework the test files so they "
                    f"fail because the behavior is unimplemented (assertion/"
                    f"NotImplementedError/missing attribute), not because the test "
                    f"files themselves are broken, and not pass trivially."
                ),
                specs=specs,
                context_bundle=context,
                test_command=config.test_command,
                model=config.model,
                cwd=str(config.project_dir),
            )
            if not retry.success:
                block.timed_out = retry.timed_out
                log.error("FAIL block=%d name=%s phase=RED_GATE_RETRY error=%s",
                          block.number, block.name, retry.error)
                progress.log_agent("TestWriter", block.name, "TIMEOUT" if retry.timed_out else "FAILED")
                state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
                return False
            sha = _git_commit_block_phase(config, "test", block)
            if sha:
                block.test_commit = sha
                state.checkpoint(config.state_file_path())

    log.error("FAIL block=%d name=%s phase=RED_GATE reason=%s",
              block.number, block.name, gate.reason)
    progress.log_agent("RedGate", block.name, f"FAILED after retry: {gate.reason}")
    state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
    return False


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
    # Index by list position, not block.number - 1: LLM-generated tasks.md
    # numbering isn't guaranteed contiguous-from-1. The block being run is
    # always the one at state.tdd.current_block (see run_tdd_engine's loop).
    block_idx = state.tdd.current_block if state.tdd else 0
    total = len(state.tdd.blocks) if state.tdd else 0
    cwd = str(config.project_dir)

    # Record the pre-block diff baseline (stamped once at block START —
    # survives resume via state). The quality review diffs the working tree
    # against this SHA to review the block's actual changes. Stamp ONLY when
    # the block is genuinely starting (PENDING/TESTING, i.e. before any
    # phase commit is guaranteed): resuming a mid-block checkpoint written
    # before baselines existed (IMPLEMENTING/REVIEWING with
    # baseline_commit=None) must NOT stamp the current HEAD — HEAD already
    # contains the block's own test/impl commits, and stamping it would hide
    # them from the reviewer. Such blocks keep baseline=None and
    # _capture_block_diff uses its documented `git diff HEAD` fallback
    # (misses committed work — the best available for pre-baseline
    # checkpoints).
    if block.baseline_commit is None and block.status in (
        BlockStatus.PENDING,
        BlockStatus.TESTING,
    ):
        baseline = _git_rev_parse_head(config)
        # Empty repo (no commits yet) → baseline against the empty tree so the
        # reviewer sees block 1's work; a None baseline would diff `HEAD`,
        # which after the block's own commits shows nothing.
        block.baseline_commit = baseline or EMPTY_TREE_SHA
        state.checkpoint(config.state_file_path())
    elif block.baseline_commit is None:
        log.warning(
            "Block %d resumed at %s with no baseline_commit (pre-baseline "
            "checkpoint) — review diff falls back to `git diff HEAD` and "
            "misses the block's committed work",
            block.number, block.status.value,
        )

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

    context = assemble_context(block, config, "test_writer", blocks=state.tdd.blocks if state.tdd else None)

    progress.log_agent("TestWriter", block.name, "STARTED")
    test_result = await run_test_writer(
        block_name=block.name,
        block_description=block.description,
        specs=specs,
        context_bundle=context,
        test_command=config.test_command,
        model=config.model,
        cwd=cwd,
    )

    if not test_result.success:
        # agent_call degrades a timeout to a failed result (timed_out=True)
        # rather than raising — propagate that so the orchestrator returns
        # EXIT_AGENT_TIMEOUT instead of a generic build failure.
        block.timed_out = test_result.timed_out
        reason = "timeout" if test_result.timed_out else "error"
        log.error("FAIL block=%d name=%s phase=TEST_WRITE reason=%s error=%s",
                  block.number, block.name, reason, test_result.error)
        progress.log_agent("TestWriter", block.name, "TIMEOUT" if test_result.timed_out else "FAILED")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    # The agent's own STATUS slot: NEEDS_CONTEXT/BLOCKED is it saying the work
    # was not doable as specified — stop the block, surface its reason.
    status = agent_status_gate(test_result.output, "TestWriter")
    if not status.passed:
        log.error("FAIL block=%d name=%s phase=TEST_WRITE reason=agent_status status=%s",
                  block.number, block.name, status.metadata.get("status"))
        progress.log_agent("TestWriter", block.name, str(status.metadata.get("status")))
        progress.log_diagnosis(block.name, status.reason)
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    log.info("DONE block=%d name=%s phase=TEST_WRITE", block.number, block.name)
    progress.log_agent("TestWriter", block.name, "COMPLETE")

    test_sha = _git_commit_block_phase(config, "test", block)
    if test_sha:
        block.test_commit = test_sha
        state.checkpoint(config.state_file_path())

    # --- Phase A.5: Test quality enforcement (static scan + LLM adjudication) ---
    # Scan the test source the agent actually wrote — its report is prose and
    # contains no test functions. Fall back to the report only when git can't
    # tell us what changed (non-git project).
    test_source = _read_block_test_files(config, block) or test_result.output
    quality_ok = await _enforce_test_quality(
        block, config, state, progress, specs, context, block_idx, test_source,
    )
    if not quality_ok:
        return False

    # --- Phase A.6: RED gate — prove tests fail before implementing ---
    red_ok = await _enforce_red_gate(block, config, state, progress, specs, context, block_idx)
    if not red_ok:
        return False

    # --- Phase B: Implement ---
    log.info("START block=%d name=%s phase=IMPLEMENT", block.number, block.name)
    state.mark_block_status(block_idx, BlockStatus.IMPLEMENTING, config.state_file_path())
    progress.log_block(block.number, total, block.name, "IMPLEMENTING")

    impl_context = assemble_context(block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None)
    progress.log_agent("Implementer", block.name, "STARTED")
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

    if not impl_result.success:
        block.timed_out = impl_result.timed_out
        reason = "timeout" if impl_result.timed_out else "error"
        log.error("FAIL block=%d name=%s phase=IMPLEMENT reason=%s error=%s",
                  block.number, block.name, reason, impl_result.error)
        progress.log_agent("Implementer", block.name, "TIMEOUT" if impl_result.timed_out else "FAILED")
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    impl_status = agent_status_gate(impl_result.output, "Implementer")
    if not impl_status.passed:
        log.error("FAIL block=%d name=%s phase=IMPLEMENT reason=agent_status status=%s",
                  block.number, block.name, impl_status.metadata.get("status"))
        progress.log_agent("Implementer", block.name, str(impl_status.metadata.get("status")))
        progress.log_diagnosis(block.name, impl_status.reason)
        state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
        return False

    log.info("DONE block=%d name=%s phase=IMPLEMENT turns=%d elapsed=%.0fs",
             block.number, block.name, impl_result.turns_used, impl_result.elapsed_seconds)
    progress.log_agent("Implementer", block.name, "COMPLETE")

    impl_sha = _git_commit_block_phase(config, "impl", block)
    if impl_sha:
        block.impl_commit = impl_sha
        state.checkpoint(config.state_file_path())

    # --- Phase C: Refactor (standard tier only) ---
    if config.refactor_phase:
        log.info("START block=%d name=%s phase=REFACTOR", block.number, block.name)
        refactor_context = assemble_context(block, config, "refactorer", blocks=state.tdd.blocks if state.tdd else None)
        progress.log_agent("Refactorer", block.name, "STARTED")
        refactor_result = await run_refactorer(
            block_name=block.name,
            block_description=block.description,
            context_bundle=refactor_context,
            test_command=config.test_command,
            model=config.model,
            cwd=cwd,
        )
        # Refactor is non-fatal: a failed or timed-out refactor leaves the
        # passing implementation intact, so log and move on.
        if not refactor_result.success:
            reason = "timeout" if refactor_result.timed_out else refactor_result.error
            log.warning("FAIL block=%d name=%s phase=REFACTOR reason=%s (non-fatal)", block.number, block.name, reason)
            progress.log_agent("Refactorer", block.name, "TIMEOUT" if refactor_result.timed_out else "FAILED")
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
    # Index by list position (see run_block_tdd) — block.number may be non-contiguous.
    block_idx = state.tdd.current_block if state.tdd else 0
    total = len(state.tdd.blocks) if state.tdd else 0

    # --- Integration gate ---
    log.info("START block=%d name=%s phase=INTEGRATION_GATE", block.number, block.name)
    gate = integration_gate(config.test_command, config.project_dir)
    if not gate.passed:
        log.warning("Integration gate failed after block %s: %s", block.name, gate.reason)
        # Circuit breaker: scoped fixes first, then one architecture-questioning
        # escalation, then FAILED with a diagnosis — never an endless fix loop.
        last_fix_output = ""
        for attempt in range(MAX_INTEGRATION_FIX_ATTEMPTS):
            escalated = attempt == MAX_INTEGRATION_FIX_ATTEMPTS - 1
            fix_model = (config.review_model or config.model) if escalated else config.model
            log.info("Integration fix attempt %d/%d%s", attempt + 1,
                     MAX_INTEGRATION_FIX_ATTEMPTS, " (escalated)" if escalated else "")
            if escalated:
                progress.log_agent("IntegrationFixer", block.name,
                                   "ESCALATED — questioning the architecture")
            context = assemble_context(block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None)
            fix_result = await run_integration_fix(
                failure_output=gate.metadata.get("stderr", "") + gate.metadata.get("stdout", ""),
                test_command=config.test_command,
                context_bundle=context,
                escalate=escalated,
                model=fix_model,
                cwd=str(config.project_dir),
            )
            # A timed-out or failed fix leaves success=False; retry until attempts run out.
            if fix_result.success:
                last_fix_output = fix_result.output
                gate = integration_gate(config.test_command, config.project_dir)
                if gate.passed:
                    break

        if not gate.passed:
            log.error("Integration gate still failing after %d fix attempts for block %s",
                      MAX_INTEGRATION_FIX_ATTEMPTS, block.name)
            diagnosis = (
                f"Integration gate exhausted after {MAX_INTEGRATION_FIX_ATTEMPTS} fix "
                f"attempts (final attempt escalated).\n"
                f"Gate: {gate.reason}\n"
                f"Last fix agent report:\n{last_fix_output or '(no fix agent completed)'}"
            )
            progress.log_diagnosis(block.name, diagnosis)
            state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
            return False

    # GREEN evidence: the suite passing with the implementation in place —
    # the counterpart to the RED evidence captured before implementation.
    block.green_evidence = _trim_evidence(gate.metadata.get("stdout", "") or gate.reason)
    state.checkpoint(config.state_file_path())
    progress.log_evidence("GREEN", block.name, block.green_evidence)

    # --- Quality review loop ---
    state.mark_block_status(block_idx, BlockStatus.REVIEWING, config.state_file_path())
    progress.log_block(block.number, total, block.name, "REVIEWING")

    review_passed = False
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
            review_passed = True
            break

        log.info("Review iteration %d failed for block %s: %s", iteration + 1, block.name, score_gate.reason)
        if iteration + 1 >= MAX_REVIEW_ITERATIONS:
            break  # no fix agent after the last review — its work would go unreviewed
        # Spawn fix agent for the failing dimensions
        context = assemble_context(block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None)
        fix = await run_implementer(
            block_name=block.name,
            block_description=f"Fix review issues: {score_gate.reason}",
            failing_tests=score_gate.reason,
            context_bundle=context,
            test_command=config.test_command,
            prompt_style=config.implementer_prompt_style,
            model=config.model,
            cwd=str(config.project_dir),
        )
        if not fix.success and fix.timed_out:
            log.warning("Fix agent timed out during review iteration %d", iteration + 1)

    if not review_passed:
        if config.lenient_review:
            # Accept-and-continue (pre-0.4 behavior), explicitly opted into.
            log.warning(
                "Review exhausted for block %s after %d iterations — accepting "
                "(--lenient-review)", block.name, MAX_REVIEW_ITERATIONS,
            )
            progress.log_block(block.number, total, block.name,
                               "COMPLETE (review below threshold, --lenient-review)")
        else:
            log.error("FAIL block=%d name=%s phase=REVIEW reason=review-exhausted "
                      "after %d iterations", block.number, block.name, MAX_REVIEW_ITERATIONS)
            progress.log_block(block.number, total, block.name, "FAILED — review exhausted")
            print(f"BLOCK:{block.number}/{total}:{block.name}:REVIEW_EXHAUSTED")
            state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
            return False

    state.mark_block_status(block_idx, BlockStatus.DONE, config.state_file_path())
    log.info("DONE block=%d/%d name=%s", block.number, total, block.name)
    progress.log_block(block.number, total, block.name, "COMPLETE")
    print(f"BLOCK:{block.number}/{total}:{block.name}:COMPLETE")
    return True


async def _run_quality_review(block: BlockInfo, config: BuildConfig) -> ReviewResult:
    """Run an evidence-based quality review of the block's implementation.

    Reviews the block's ACTUAL diff (pre-block baseline → working tree), with
    the project's coding standards pushed into the reviewer's context, via
    llm_steps.review_steps.run_code_review (which honors config.review_model).

    Propagates LLMCallError / LLMCallTimeoutError on failure. Callers must
    handle the exception and fail the block — silently fabricating passing
    scores is worse than a loud failure (the old fallback was a real bug).
    """
    rubric = ""
    if config.rubric_path.exists():
        rubric = config.rubric_path.read_text()

    diff = _capture_block_diff(config, block)
    if not diff:
        log.warning(
            "No diff captured for block %d (%s) — reviewer sees spec context only",
            block.number, block.name,
        )

    spec_context = f"Block {block.number}: {block.name}\n{block.description}"
    if block.satisfies:
        spec_context += f"\nSatisfies: {', '.join(block.satisfies)}"
        # The satisfied capabilities' spec scenarios, verbatim — the reviewer
        # judges Missing/Extra/Misunderstood against these, not a paraphrase.
        req_dir = config.change_dir / "requirements"
        for cap in block.satisfies:
            req_file = req_dir / f"{cap}.md"
            if req_file.exists():
                spec_context += f"\n\n### Requirement: {cap}\n{req_file.read_text()}"

    # Global constraints are review criteria too — a block that violates one
    # is non-compliant even when its local behavior is correct.
    constraints = _global_constraints_text(config)
    if constraints:
        spec_context += f"\n\n### Global Constraints (project-wide)\n{constraints}"

    # RED/GREEN evidence reaches the reviewer as claims to verify, not truth.
    report_parts = []
    if block.red_evidence:
        report_parts.append(f"### RED evidence (tests failing before implementation)\n"
                            f"```\n{block.red_evidence}\n```")
    if block.green_evidence:
        report_parts.append(f"### GREEN evidence (suite after implementation)\n"
                            f"```\n{block.green_evidence}\n```")

    return await run_code_review(
        diff,
        rubric,
        config,
        spec_context=spec_context,
        standards=config.standards_text(),
        implementer_report="\n\n".join(report_parts),
    )


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
            # Only report a timeout when the block actually timed out; every
            # failure path sets status=FAILED, so the status alone can't tell
            # a timeout from an ordinary build failure.
            if block.timed_out:
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
        final_review = await _run_final_review(config, state)

        if final_review is not None and not final_review.passed:
            # Fail closed on both an unverifiable review (error) and a genuine
            # FAILED verdict — a build the reviewer would not trust is not
            # done. Not marked done — a resume re-runs the review.
            label = ("ERROR" if final_review.action == "final_review_error"
                     else "FAILED")
            log.error("FAIL phase=FINAL_REVIEW result=%s reason=%s",
                      label.lower(), final_review.reason[:200])
            progress.log_phase("FINAL_REVIEW", f"{label}: {final_review.reason}")
            if not config.lenient_review:
                return EXIT_BUILD_FAILURE
            log.warning("--lenient-review: continuing despite final-review %s",
                        label.lower())
            state.tdd.final_review_done = True
            state.checkpoint(state_path)
        else:
            state.tdd.final_review_done = True
            state.checkpoint(state_path)
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


def _capture_full_build_diff(config: BuildConfig, state: BuildState) -> str:
    """Whole-build diff for the final review: first block's pre-build baseline
    → working tree, capped like the per-block review diff. Returns "" on git
    failure — never raises."""
    baseline = None
    if state.tdd and state.tdd.blocks:
        baseline = state.tdd.blocks[0].baseline_commit
    target = baseline or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", target],
            cwd=str(config.project_dir), capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.warning("Failed to capture full build diff (git diff %s): %s",
                        target, proc.stderr.strip())
            return ""
        diff = proc.stdout
    except Exception as e:
        log.warning("Failed to capture full build diff: %s", e)
        return ""
    if len(diff) > MAX_REVIEW_DIFF_CHARS:
        diff = diff[:MAX_REVIEW_DIFF_CHARS] + "\n... (diff truncated for review)"
    return diff


async def _run_final_review(config: BuildConfig, state: BuildState) -> GateResult | None:
    """Final comprehensive review over the full build diff.

    Structured verdict (FinalReviewVerdict via llm_call_structured) — no
    string-matching on free text. Fails closed: an exception yields
    passed=False with action="final_review_error", surfaced to the caller —
    never a fabricated pass.
    """
    rubric = ""
    if config.rubric_path.exists():
        rubric = config.rubric_path.read_text()
    if not rubric:
        return GateResult(passed=True, reason="No rubric — skipping final review")

    diff = _capture_full_build_diff(config, state)
    if not diff:
        log.warning("No full-build diff captured — final review sees rubric only")
    prompt = FINAL_REVIEW_PROMPT.format(
        diff=diff or "(no diff captured)",
        rubric=rubric,
    )
    model = getattr(config, "review_model", None) or config.model
    try:
        verdict = await llm_call_structured(prompt, FinalReviewVerdict, model=model, max_retries=1)
    except Exception as e:
        log.error("Final review errored (failing closed): %s", e)
        return GateResult(
            passed=False,
            reason=f"Final review errored: {e}",
            action="final_review_error",
        )
    reason = verdict.summary
    if verdict.issues:
        reason += " Issues: " + "; ".join(verdict.issues)
    return GateResult(
        passed=verdict.passed,
        reason=reason[:500],
        metadata={"issues": verdict.issues},
    )


def _detect_entry_point(config: BuildConfig) -> tuple[str, list[str], dict | None] | None:
    """Find a runnable entry point to smoke-test: (label, argv, env) or None.

    Covers python packages (<pkg>/__main__.py at root or under src/, run as
    `python -m <pkg>`), loose scripts (main.py/app.py/__main__.py), and node
    projects (package.json "main"). env is a full environment mapping when
    the run needs one (src-layout PYTHONPATH), else None (inherit).
    """
    project_dir = config.project_dir
    for base in (project_dir, project_dir / "src"):
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*/__main__.py")):
            pkg = p.parent.name
            if pkg.startswith(".") or pkg in ("tests", "test"):
                continue
            env = None
            if base != project_dir:
                env = dict(os.environ)
                existing = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = str(base) + (os.pathsep + existing if existing else "")
            return f"python -m {pkg}", [sys.executable, "-m", pkg, "--help"], env
    for name in ("main.py", "app.py", "__main__.py"):
        if (project_dir / name).exists():
            return f"python {name}", [sys.executable, name, "--help"], None
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text())
        except (ValueError, OSError):
            data = {}
        main = data.get("main")
        if isinstance(main, str) and (project_dir / main).exists():
            return f"node {main}", ["node", main, "--help"], None
    return None


def _verify_entry_point(config: BuildConfig) -> GateResult:
    """Smoke-run the project's entry point (app-type projects).

    Reports real evidence: the detected entry point is actually executed
    (`--help`) under the repo-trust guard with a 60s timeout. Library
    projects (no entry point) pass by assumption; anything found but not
    executable is reported unverified (passed=False — stays warn-level at
    the call site) rather than fabricated as "passed".
    """
    entry = _detect_entry_point(config)
    if entry is None:
        return GateResult(passed=True, reason="No explicit entry point (library project assumed)")
    label, argv, env = entry

    if not _repo_trusted():
        return GateResult(
            passed=False,
            reason=f"Entry point found ({label}) but not smoke-run: repo not "
                   f"trusted (set --trust-repo or BUILDME_TRUST_REPO=1)",
        )
    try:
        proc = subprocess.run(
            argv, cwd=str(config.project_dir), env=env,
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            passed=False,
            reason=f"Entry point smoke run timed out after 60s: {label}",
        )
    except (FileNotFoundError, OSError) as e:
        return GateResult(
            passed=False,
            reason=f"Entry point smoke run could not start ({label}): {e}",
        )
    output_tail = (proc.stdout + proc.stderr)[-500:]
    if proc.returncode == 0:
        return GateResult(
            passed=True,
            reason=f"Entry point smoke run passed: {label}",
            metadata={"output": output_tail},
        )
    return GateResult(
        passed=False,
        reason=f"Entry point smoke run failed (exit {proc.returncode}): {label}",
        metadata={"output": output_tail},
    )
