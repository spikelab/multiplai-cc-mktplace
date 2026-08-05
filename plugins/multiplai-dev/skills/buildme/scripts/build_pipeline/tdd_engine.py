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

import hashlib

from . import budget as budget_mod
from .budget import BudgetExceededError
from . import board
from . import git_ops
from .change_manager import extract_global_constraints
from .config import BuildConfig
from .gates import (
    _repo_trusted,
    agent_status_gate,
    baseline_test_gate,
    integration_gate,
    parse_implementation_note,
    red_gate,
    review_iteration_gate,
    review_score_gate,
    run_test_suite,
    unchanged_tests_gate,
    wiring_task_gate,
)
from .llm_steps.respec_steps import append_implementation_note
from .llm_steps.review_steps import run_code_review
from .llm_steps.tdd_steps import (
    run_implementer,
    run_integration_fix,
    run_refactor_all,
    run_refactorer,
    run_test_writer,
)
from .models import (
    BlockInfo,
    BlockStatus,
    BuildPhase,
    FinalReviewVerdict,
    FindingAdjudication,
    GateResult,
    ReviewResult,
    TestQualityAudit,
)
from .progress import ProgressWriter
from .prompts.review import FINAL_REVIEW_PROMPT, FINDING_ADJUDICATION_PROMPT
from .prompts.test_writing import TEST_QUALITY_PROMPT
from .sdk import llm_call_structured
from .state import BuildState, TDDState

log = logging.getLogger(__name__)


def _mark_block(
    state: BuildState, block_idx: int, status: BlockStatus, config: BuildConfig,
) -> None:
    """Set a block's status, checkpoint, and refresh the board card.

    Every block status is In Development (board.py's docstring says why —
    notably REVIEWING is an in-process review with no pushed branch, so it is
    not In Review). The board write still happens on each change so
    `.board.json` stays current when the TDD engine runs standalone; it emits a
    `BOARD:` line only when the card actually moves columns.
    """
    state.mark_block_status(block_idx, status, config.state_file_path())
    board.record(config, state, BuildPhase.TDD_BUILD, block_status=status,
                 note=f"block {block_idx + 1} {status.value}")


def _git_commit_block_phase(config: BuildConfig, phase: str, block: BlockInfo) -> str | None:
    """Commit the block phase's changes in the project repo.

    phase: "test", "impl" or "refactor" (used for the conventional-commit
    prefix). Returns the new commit's SHA, or None if there was nothing to
    commit or the commit failed (logged as a warning — never raises).
    """
    return git_ops.commit_tree(
        config.project_dir,
        f"{phase}(block-{block.number}): {block.name}",
        f"block={block.number} phase={phase}",
    )


# git's well-known empty-tree object. Used as the diff baseline when the
# project repo has no commits at block start (fresh `git init`): diffing
# against the empty tree makes block 1's committed work visible to the
# reviewer, whereas a None baseline would fall back to `git diff HEAD`,
# which post-commit shows nothing.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


# Cap the diff handed to the reviewer so a pathological block can't blow the
# context window. 150k chars ≈ 40k tokens — far above any sane block diff.
MAX_REVIEW_DIFF_CHARS = 150_000


# Python-only, on purpose: this regex feeds the weak-test *scan*, which looks
# for `def test_*`. Widening it to Swift/TS would hand non-Python source to a
# Python-shaped scan and fail every block on "no test functions found".
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")

# Language-agnostic, used only for integrity hashing. The integrity gate does
# not parse test source — it only needs to know which paths are tests — so it
# must cover every language buildme runs on, or the gate silently no-ops (empty
# `before` → "not checked") on a Swift or TypeScript repo.
_TEST_PATH_RE = re.compile(
    r"(^|/)("
    r"test_[^/]+\.py|[^/]+_test\.py|conftest\.py"        # Python
    r"|[^/]+_test\.go"                                    # Go
    r"|[^/]+Tests?\.swift"                                # Swift
    r"|[^/]+\.(test|spec)\.[cm]?[jt]sx?"                  # JS/TS
    r")$"
    r"|(^|/)(tests?|__tests__|Tests)/"                    # anything under a tests dir
)
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


def _list_block_test_files(config: BuildConfig, block: BlockInfo) -> list[str] | None:
    """Repo-relative paths of the test files this block added or modified.

    Returns None when git could not be asked (non-zero exit, timeout, not a
    repo) — distinct from `[]`, which means "asked, and this block touched no
    test files". Collapsing the two made a transient git failure look like the
    agent had deleted every test file; see `_snapshot_test_files`.
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
            return None
        names = proc.stdout.splitlines()
    except Exception as e:
        log.warning("Could not list changed files for block %d: %s", block.number, e)
        return None
    return [n.strip() for n in names if _TEST_PATH_RE.search(n.strip())]


def _snapshot_test_files(config: BuildConfig, block: BlockInfo) -> dict[str, str] | None:
    """{repo-relative path: sha256} for the block's test files, right now.

    Taken the moment the RED gate passes and re-taken at each later checkpoint;
    comparing the two is how `unchanged_tests_gate` sees a moved bar. Content
    hashing rather than mtime because a git checkout or a formatter run moves
    mtimes without changing what the test asserts.

    A file that vanished between listing and reading is simply omitted, which
    `unchanged_tests_gate` reads as a deletion — the correct interpretation.

    Returns None when the file list itself could not be obtained. That is NOT a
    deletion: an unavailable `after` snapshot compared against a populated
    `before` would report every test file as deleted and fail the block on a git
    hiccup. The caller treats None as "gate could not run".
    """
    names = _list_block_test_files(config, block)
    if names is None:
        return None
    snapshot: dict[str, str] = {}
    for name in names:
        path = config.project_dir / name
        try:
            snapshot[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as e:
            log.debug("Could not hash test file %s: %s", name, e)
    return snapshot


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

    # Requirement files (BDD scenarios — one per capability).
    # The test_writer already receives these verbatim through its dedicated
    # ``specs`` slot (built by the caller in run_block_tdd / the quality+RED
    # retries), so including them here too would ship the full requirements
    # set TWICE inside a single prompt — pure intra-call duplication that the
    # prompt cache cannot dedupe. Implementer/refactorer/reviewer get them
    # only via this bundle, so keep them for every role except test_writer.
    req_dir = config.change_dir / "requirements"
    if role != "test_writer" and req_dir.exists():
        for req_file in sorted(req_dir.glob("*.md")):
            rel = req_file.relative_to(config.change_dir)
            parts.append(f"\n## Requirements: {rel}\n{req_file.read_text()}")

    # Unknowns — the explainer document for dependencies new to this project.
    # test_writer ONLY: the payoff of the B1 gate is that the documented edge
    # cases (empty/malformed/oversized/concurrent/offline) arrive as tests. The
    # implementer gets them indirectly through those tests, and the refactorer
    # has no use for them — shipping the document to every role would just
    # inflate prompts.
    if role == "test_writer" and config.unknowns_path.exists():
        parts.append(
            "\n## Unknowns — dependencies new to this project\n"
            "Write a test for every edge case below that this block touches.\n\n"
            + config.unknowns_path.read_text()
        )

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
    return await llm_call_structured(prompt, TestQualityAudit, model=config.model,
                                     effort=config.review_effort, max_retries=1,
                                     budget_label="test_quality_audit")


async def _adjudicate_review_findings(
    review: ReviewResult,
    block: BlockInfo,
    config: BuildConfig,
    diff: str,
    build_context: str = "",
) -> ReviewResult:
    """Accept or reject each reviewer finding before anything acts on it.

    Mirrors `_audit_test_quality`: a cheap coarse signal (there, a regex scan;
    here, a fresh-context reviewer) is confirmed or overturned by a judge with
    more context before the pipeline spends work on it.

    The asymmetry is deliberate. Reviewers run in *fresh* contexts because that
    is what makes them catch errors the implementer's own context hides — and
    it is also why roughly a quarter of what they propose is wrong: they cannot
    see the decisions the build already made. The adjudicator runs on the main
    model WITH that context. Reviewers propose; the orchestrator disposes.

    Returns a copy of *review* whose `findings` are the accepted ones and whose
    `rejected_findings` hold the rest. The gate and the fix loop read only
    `findings`, so a rejected finding cannot reach a fix agent.

    Failure is fail-OPEN, deliberately: if the adjudicator errors, every
    finding stays accepted. The alternative — dropping findings when the judge
    is unavailable — would silently weaken review exactly when something is
    already going wrong.
    """
    candidates = review.findings_or_derived()
    if not candidates:
        return review.model_copy(update={"findings": []})
    if not config.adjudicate_findings:
        # Off means "do not act on unadjudicated findings", NOT "apply them
        # blind" — the invariant is that nothing reaches a fix agent unjudged.
        log.info("Finding adjudication disabled — %d finding(s) dropped, not applied",
                 len(candidates))
        return review.model_copy(update={"findings": [], "rejected_findings": candidates})

    findings_text = "\n".join(
        f"[{i}] ({f.severity}, confidence {f.confidence:.2f}"
        + (f", raised by {len(f.reviewers)} reviewers" if len(f.reviewers) > 1 else "")
        + f") {f.claim}"
        + (f"\n     location: {f.file_path}:{f.line}" if f.file_path else "")
        + (f"\n     evidence: {f.evidence}" if f.evidence else "")
        for i, f in enumerate(candidates)
    )
    block_context = f"Block {block.number}: {block.name}\n{block.description}"
    prompt = FINDING_ADJUDICATION_PROMPT.format(
        block_context=block_context,
        diff=diff or "(no diff captured)",
        build_context=build_context or "(no additional build context)",
        findings=findings_text,
    )
    try:
        adjudication = await llm_call_structured(
            prompt, FindingAdjudication, model=config.model,
            effort=config.review_effort, max_retries=1,
            budget_label="adjudication",
        )
    except Exception as e:
        log.warning(
            "Finding adjudication failed for block %d (%s) — keeping all %d findings",
            block.number, e, len(candidates),
        )
        return review.model_copy(update={"findings": candidates})

    accepted_idx = adjudication.accepted_indices(len(candidates))
    reasons = {v.index: v.reason for v in adjudication.verdicts}
    accepted, rejected = [], []
    for i, finding in enumerate(candidates):
        if i in accepted_idx:
            accepted.append(finding)
        else:
            rejected.append(finding.model_copy(
                update={"evidence": f"{finding.evidence}\nREJECTED: {reasons.get(i, '')}".strip()}
            ))
    log.info(
        "Adjudication block=%d: %d/%d findings accepted, %d rejected",
        block.number, len(accepted), len(candidates), len(rejected),
    )
    return review.model_copy(update={"findings": accepted, "rejected_findings": rejected})


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
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
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
        effort=config.agent_effort,
        cwd=str(config.project_dir),
    )
    if not retry.success:
        block.timed_out = retry.timed_out
        log.error("FAIL block=%d name=%s phase=TEST_QUALITY_RETRY error=%s",
                  block.number, block.name, retry.error)
        progress.log_agent("TestWriter", block.name, "TIMEOUT" if retry.timed_out else "FAILED")
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
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
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
    if re_audit.passed:
        return True

    log.error("FAIL block=%d name=%s phase=TEST_QUALITY reason=still-weak-after-retry",
              block.number, block.name)
    progress.log_agent("TestQualityAuditor", block.name, "STILL WEAK after retry — block failed")
    _mark_block(state, block_idx, BlockStatus.FAILED, config)
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
            # Freeze the bar the moment it is proven to be a bar. Everything
            # after this point — the implement phase and every review-fix
            # iteration — can write to these files, so this hash map is the
            # only later evidence of whether they did.
            # None (git unavailable) degrades to {}, which `unchanged_tests_gate`
            # reads as "no snapshot → gate could not run". Storing None would
            # violate the model's dict contract and crash the len() below.
            block.test_file_hashes = _snapshot_test_files(config, block) or {}
            if not block.test_file_hashes:
                log.warning("Test integrity: no snapshot for block %d — the gate will "
                            "report 'not checked' for this block", block.number)
            log.info("Test integrity: snapshotted %d test file(s) for block %d",
                     len(block.test_file_hashes), block.number)
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
                effort=config.agent_effort,
                cwd=str(config.project_dir),
            )
            if not retry.success:
                block.timed_out = retry.timed_out
                log.error("FAIL block=%d name=%s phase=RED_GATE_RETRY error=%s",
                          block.number, block.name, retry.error)
                progress.log_agent("TestWriter", block.name, "TIMEOUT" if retry.timed_out else "FAILED")
                _mark_block(state, block_idx, BlockStatus.FAILED, config)
                return False
            sha = _git_commit_block_phase(config, "test", block)
            if sha:
                block.test_commit = sha
                state.checkpoint(config.state_file_path())

    log.error("FAIL block=%d name=%s phase=RED_GATE reason=%s",
              block.number, block.name, gate.reason)
    progress.log_agent("RedGate", block.name, f"FAILED after retry: {gate.reason}")
    _mark_block(state, block_idx, BlockStatus.FAILED, config)
    return False


def _record_implementation_note(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    role: str,
    output: str,
) -> bool:
    """Capture an agent's SURPRISES:/SPEC_IMPACT: slots.

    The note is persisted on the block (surviving resume) and appended to
    implementation-notes.md immediately, so an interrupted build still leaves
    the learning on disk for the respec step. A `contradicts` note is logged
    and written to build-progress.md as a warning.

    Returns False only when the build must stop: a contradiction under
    `respec: {halt_on_contradiction: true}`. Recording never fails the build
    on its own.
    """
    note = parse_implementation_note(
        output, block_number=block.number, block_name=block.name, role=role,
    )
    if note is None:
        return True

    # A resumed mid-block run re-parses the same agent report: recording the
    # identical note (same role/block/surprises/spec_impact) again would
    # duplicate it on the block, in implementation-notes.md, and ultimately in
    # the respec prompt. Skip the recording — but never the contradiction
    # handling below, so a resume cannot sneak past a configured halt.
    if note in block.notes:
        log.info(
            "Implementation note already recorded block=%d role=%s — skipping duplicate",
            block.number, role,
        )
    else:
        block.notes.append(note)
        state.checkpoint(config.state_file_path())
        try:
            append_implementation_note(config.change_dir, note)
        except OSError as e:
            # The note is on the block/state and run_respec_audit merges state
            # notes back in, so nothing is lost; failing a green block over
            # the markdown copy is not worth it.
            log.warning("Could not append implementation note to disk: %s", e)

        if not note.contradicts:
            log.info(
                "Implementation note block=%d role=%s spec_impact=%s",
                block.number, role, note.spec_impact,
            )
            progress.log_spec_impact(block.name, role, note.spec_impact, note.surprises)
        else:
            log.warning(
                "SPEC_IMPACT: contradicts reported by %s on block %d (%s): %s",
                role, block.number, block.name, note.surprises,
            )
            progress.log_spec_impact(block.name, role, note.spec_impact, note.surprises)

    if not note.contradicts:
        return True

    if not config.gates.respec_halt_on_contradiction:
        return True

    diagnosis = (
        f"Build stopped: the {role} reported SPEC_IMPACT: contradicts on block "
        f"{block.number} ({block.name}) and respec.halt_on_contradiction is on.\n"
        f"The block could only be built by doing something the spec/design does "
        f"not say (or says otherwise), so the spec is decided in conversation "
        f"rather than steered around in code.\n\n"
        f"Reported surprise:\n{note.surprises}\n\n"
        f"Next: reshape the requirements/design for this block, or set "
        f"`respec: {{halt_on_contradiction: false}}` in specs/config.yaml to let "
        f"the build continue and collect the delta into respec.md instead."
    )
    log.error("FAIL block=%d name=%s reason=spec_contradiction role=%s",
              block.number, block.name, role)
    progress.log_diagnosis(block.name, diagnosis)
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
        baseline = git_ops.rev_parse_head(config.project_dir)
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
        _mark_block(state, block_idx, BlockStatus.TESTING, config)
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
        effort=config.agent_effort,
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
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    # The agent's own STATUS slot: NEEDS_CONTEXT/BLOCKED is it saying the work
    # was not doable as specified — stop the block, surface its reason.
    status = agent_status_gate(test_result.output, "TestWriter")
    if not status.passed:
        log.error("FAIL block=%d name=%s phase=TEST_WRITE reason=agent_status status=%s",
                  block.number, block.name, status.metadata.get("status"))
        progress.log_agent("TestWriter", block.name, str(status.metadata.get("status")))
        progress.log_diagnosis(block.name, status.reason)
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    log.info("DONE block=%d name=%s phase=TEST_WRITE", block.number, block.name)
    progress.log_agent("TestWriter", block.name, "COMPLETE")

    if not _record_implementation_note(
        block, config, state, progress, "test_writer", test_result.output,
    ):
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

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
    _mark_block(state, block_idx, BlockStatus.IMPLEMENTING, config)
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
        effort=config.agent_effort,
        cwd=cwd,
    )

    if not impl_result.success:
        block.timed_out = impl_result.timed_out
        reason = "timeout" if impl_result.timed_out else "error"
        log.error("FAIL block=%d name=%s phase=IMPLEMENT reason=%s error=%s",
                  block.number, block.name, reason, impl_result.error)
        progress.log_agent("Implementer", block.name, "TIMEOUT" if impl_result.timed_out else "FAILED")
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    impl_status = agent_status_gate(impl_result.output, "Implementer")
    if not impl_status.passed:
        log.error("FAIL block=%d name=%s phase=IMPLEMENT reason=agent_status status=%s",
                  block.number, block.name, impl_status.metadata.get("status"))
        progress.log_agent("Implementer", block.name, str(impl_status.metadata.get("status")))
        progress.log_diagnosis(block.name, impl_status.reason)
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    log.info("DONE block=%d name=%s phase=IMPLEMENT turns=%d elapsed=%.0fs",
             block.number, block.name, impl_result.turns_used, impl_result.elapsed_seconds)
    progress.log_agent("Implementer", block.name, "COMPLETE")

    # Kept in state so the reviewer sees the implementer's own account as a
    # claim to verify, and so the test-integrity gate can find a declared
    # TEST CHANGE REQUIRED reason after a resume.
    block.implementer_report = _trim_evidence(impl_result.output)
    state.checkpoint(config.state_file_path())

    if not _record_implementation_note(
        block, config, state, progress, "implementer", impl_result.output,
    ):
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    impl_sha = _git_commit_block_phase(config, "impl", block)
    if impl_sha:
        block.impl_commit = impl_sha
        state.checkpoint(config.state_file_path())

    # --- Phase C: Refactor (every tier) ---
    if config.refactor_phase:
        log.info("START block=%d name=%s phase=REFACTOR", block.number, block.name)
        # The point to rewind to if the refactor turns out not to be
        # behavior-preserving. The impl commit above is the normal answer; the
        # HEAD fallback only applies when there was genuinely nothing to commit
        # and the tree is clean, so a reset cannot eat uncommitted work.
        rewind_to = impl_sha
        if rewind_to is None and git_ops.tree_is_clean(config.project_dir):
            rewind_to = git_ops.rev_parse_head(config.project_dir)

        # The integrity baseline for this window is the tree as the implementer
        # left it — NOT `block.test_file_hashes`, which was frozen at the RED
        # gate and is only re-baselined by `_enforce_test_integrity` AFTER this
        # phase. An implementer that legitimately changed a test (the
        # `TEST CHANGE REQUIRED:` path, committed just above) would otherwise
        # be blamed on the refactorer, discarding a good refactor every time.
        # The whole-change pass snapshots its own window the same way.
        pre_refactor_hashes = _snapshot_test_files(config, block)

        refactor_context = assemble_context(block, config, "refactorer", blocks=state.tdd.blocks if state.tdd else None)
        progress.log_agent("Refactorer", block.name, "STARTED")
        refactor_result = await run_refactorer(
            block_name=block.name,
            block_description=block.description,
            context_bundle=refactor_context,
            test_command=config.test_command,
            model=config.model,
            effort=config.agent_effort,
            cwd=cwd,
        )
        # Refactor is non-fatal: a failed or timed-out refactor leaves the
        # passing implementation intact, so log and move on.
        if not refactor_result.success:
            reason = "timeout" if refactor_result.timed_out else refactor_result.error
            log.warning("FAIL block=%d name=%s phase=REFACTOR reason=%s (non-fatal)", block.number, block.name, reason)
            progress.log_agent("Refactorer", block.name, "TIMEOUT" if refactor_result.timed_out else "FAILED")
        elif not _verify_block_refactor(block, config, state, progress, rewind_to,
                                        pre_refactor_hashes):
            # Verification failed and the diff was discarded (or could not be):
            # the block keeps the implementation it had. Already logged.
            pass
        else:
            log.info("DONE block=%d name=%s phase=REFACTOR", block.number, block.name)
            progress.log_agent("Refactorer", block.name, "COMPLETE")
            if not _record_implementation_note(
                block, config, state, progress, "refactorer", refactor_result.output,
            ):
                _mark_block(state, block_idx, BlockStatus.FAILED, config)
                return False
            refactor_sha = _git_commit_block_phase(config, "refactor", block)
            if refactor_sha:
                block.refactor_commit = refactor_sha
                state.checkpoint(config.state_file_path())

    return True


def _verify_block_refactor(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    rewind_to: str | None,
    before: dict[str, str] | None,
) -> bool:
    """Prove the block's refactor was behavior-preserving, or throw it away.

    Two checks, both of which the refactorer could otherwise satisfy by moving
    the bar instead of clearing it:

    1. The suite is re-run through `integration_gate` — the same call that
       produces this block's GREEN evidence a moment later — so "still green"
       means green by the same measurement.
    2. `unchanged_tests_gate` is re-applied against *before* — the snapshot
       taken when the refactor window opened (after the impl commit), so a
       test the implementer legitimately changed is not blamed on the
       refactorer — with **no report**, so the `TEST CHANGE REQUIRED:` escape
       hatch is unavailable in this window by construction. The hatch exists
       for an implementer that discovers a genuinely wrong test; a refactorer
       is defined as behavior-preserving, so it has no case for touching the
       contract it is being measured against.

    A failure discards the refactor diff and returns False. It never fails the
    block: the implementation that was green before the refactor is still there
    afterwards, which is the whole point of committing impl first.
    """
    gate = integration_gate(config.test_command, config.project_dir)
    detail = "" if gate.passed else f"tests failed after refactor: {gate.reason}"

    if gate.passed:
        after = _snapshot_test_files(config, block)
        if before is None or after is None:
            # Same reading as everywhere else: an unavailable snapshot means
            # "not checked", never "every test file was deleted".
            log.warning("Test integrity could not run after refactor for block %d: "
                        "test file list unavailable", block.number)
            progress.log_agent("TestIntegrity", block.name, "NOT CHECKED (refactor)")
        else:
            # No report argument, deliberately — see the docstring.
            tests_gate = unchanged_tests_gate(before, after, "")
            if not tests_gate.passed:
                detail = f"test files changed during refactor: {tests_gate.reason}"

    if not detail:
        return True

    if rewind_to is None:
        log.warning(
            "Refactor of block %d (%s) failed verification and could NOT be "
            "discarded (no commit to rewind to): %s",
            block.number, block.name, detail,
        )
        progress.log_agent("Refactorer", block.name, "REVERT FAILED — see log")
        return False

    reverted = git_ops.discard_to(
        config.project_dir, rewind_to, f"refactor of block {block.number}",
    )
    log.warning(
        "Refactor of block %d (%s) %s: %s",
        block.number, block.name,
        "discarded" if reverted else "failed verification but could not be discarded",
        detail,
    )
    progress.log_agent(
        "Refactorer", block.name,
        "REVERTED — refactor did not verify" if reverted else "REVERT FAILED — see log",
    )
    return False


def _enforce_test_integrity(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    block_idx: int,
    window: str,
    report: str,
) -> bool:
    """Apply `unchanged_tests_gate` at one of the two writable windows.

    *window* is "implement" or "review-fix N" — both are checked, because the
    review-fix agent IS the implementer, so passing the first check only means
    the tests survived until the first review, not until the block is done.

    *report* is the output of the agent that wrote during THIS window only, not
    the accumulated `block.implementer_report`. Scanning the accumulation would
    let one `TEST CHANGE REQUIRED:` declared during implement silently
    authorize every later review-fix mutation for the rest of the block — the
    exact hole the per-window re-baseline exists to close.

    A flagged (declared) change passes but is recorded on the block, so
    `_run_quality_review` can hand it to the reviewer as an unverified claim.
    """
    after = _snapshot_test_files(config, block)
    if after is None:
        # Git could not be asked. Comparing a populated `before` against an
        # absent `after` would report every test file as deleted and fail the
        # block on a git hiccup, so report the gate as unrun instead.
        log.warning("Test integrity could not run after %s for block %d: "
                    "test file list unavailable", window, block.number)
        progress.log_agent("TestIntegrity", block.name, f"NOT CHECKED ({window})")
        return True
    gate = unchanged_tests_gate(block.test_file_hashes, after, report)
    if gate.metadata.get("flagged"):
        for claim in gate.metadata.get("claims", []):
            if claim not in block.test_change_claims:
                block.test_change_claims.append(claim)
        # Re-baseline to the declared state so the NEXT window measures from
        # here — otherwise one declared change would excuse every later silent
        # one for the rest of the block.
        block.test_file_hashes = after
        state.checkpoint(config.state_file_path())
        log.warning("Test files changed during %s for block %d with a declared reason: %s",
                    window, block.number, gate.reason)
        progress.log_agent("TestIntegrity", block.name,
                           f"FLAGGED ({window}) — declared change, sent to reviewer")
        return True
    if gate.passed:
        log.debug("Test integrity OK after %s for block %d (%s)",
                  window, block.number, gate.reason)
        return True

    log.error("FAIL block=%d name=%s phase=TEST_INTEGRITY window=%s reason=%s",
              block.number, block.name, window, gate.reason)
    progress.log_agent("TestIntegrity", block.name, f"FAILED ({window})")
    progress.log_diagnosis(block.name, gate.reason)
    state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
    return False


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
                effort=config.agent_effort,
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
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False

    # --- Test-integrity gate (window 1: the implement phase) ---
    # Checked BEFORE GREEN evidence is accepted. A green suite proves nothing
    # if the suite is no longer the one that went red.
    if not _enforce_test_integrity(block, config, state, progress, block_idx,
                                   "implement", block.implementer_report):
        return False

    # GREEN evidence: the suite passing with the implementation in place —
    # the counterpart to the RED evidence captured before implementation.
    block.green_evidence = _trim_evidence(gate.metadata.get("stdout", "") or gate.reason)
    state.checkpoint(config.state_file_path())
    progress.log_evidence("GREEN", block.name, block.green_evidence)

    # --- Quality review loop ---
    _mark_block(state, block_idx, BlockStatus.REVIEWING, config)
    progress.log_block(block.number, total, block.name, "REVIEWING")

    review_passed = False
    for iteration in range(MAX_REVIEW_ITERATIONS):
        iter_gate = review_iteration_gate(iteration, MAX_REVIEW_ITERATIONS)
        if not iter_gate.passed:
            log.warning("Review loop exhausted for block %s", block.name)
            break

        # A runaway review/fix loop is the classic budget sink: each iteration
        # is a full panel review PLUS a full implementer run. Check before
        # spending, so the stop lands on a boundary rather than mid-call.
        try:
            budget_mod.check(phase=f"review iteration {iteration + 1} of block {block.number}")
        except BudgetExceededError as e:
            log.error("FAIL block=%d name=%s phase=REVIEW reason=budget_exhausted",
                      block.number, block.name)
            progress.log_diagnosis(block.name, f"{e}\n{e.diagnosis}")
            state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
            return False

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
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False

        # Reviewers propose; the orchestrator disposes. THE core invariant:
        # no reviewer suggestion may reach a fix agent unadjudicated.
        review = await _adjudicate_review_findings(
            review, block, config, _capture_block_diff(config, block),
            build_context=_global_constraints_text(config),
        )
        block.review_scores = review
        block.review_iterations = iteration + 1

        score_gate = review_score_gate(review, config.review_gate)
        progress.log_review(block.name, iteration + 1, review.weighted_average, score_gate.passed)

        if score_gate.passed:
            review_passed = True
            break

        log.info("Review iteration %d failed for block %s: %s", iteration + 1, block.name, score_gate.reason)
        if iteration + 1 >= MAX_REVIEW_ITERATIONS:
            break  # no fix agent after the last review — its work would go unreviewed
        # Spawn fix agent for the failing dimensions PLUS the findings that
        # survived adjudication. Rejected findings are absent by construction.
        accepted_text = review.findings_text()
        fix_brief = score_gate.reason
        if accepted_text:
            fix_brief += f"\n\n## Adjudicated findings (accepted — fix these)\n{accepted_text}"
        context = assemble_context(block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None)
        fix = await run_implementer(
            block_name=block.name,
            block_description=f"Fix review issues: {fix_brief}",
            failing_tests=fix_brief,
            context_bundle=context,
            test_command=config.test_command,
            prompt_style=config.implementer_prompt_style,
            model=config.model,
            effort=config.agent_effort,
            cwd=str(config.project_dir),
        )
        if not fix.success and fix.timed_out:
            log.warning("Fix agent timed out during review iteration %d", iteration + 1)
        if fix.output:
            # The fix agent is an implementer too — its report is where a
            # TEST CHANGE REQUIRED declaration for this window would appear.
            block.implementer_report = _trim_evidence(
                f"{block.implementer_report}\n\n[review-fix {iteration + 1}]\n{fix.output}"
            )

        # --- Test-integrity gate (window 2: this review-fix iteration) ---
        # Checked every iteration, not just once: the fix agent has the same
        # unrestricted write access as the original implementer, and it runs
        # AFTER the tests were quality-audited.
        # Only THIS iteration's fix output counts as a declaration — see
        # `_enforce_test_integrity`'s *report* contract.
        if not _enforce_test_integrity(
            block, config, state, progress, block_idx,
            f"review-fix {iteration + 1}", fix.output or "",
        ):
            return False

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
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False

    _mark_block(state, block_idx, BlockStatus.DONE, config)
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
    if block.test_change_claims:
        # Same treatment as RED/GREEN: the implementer said the test had to
        # change, and that assertion is exactly what needs checking. A green
        # suite is not evidence here — it is the thing under suspicion.
        claims = "\n".join(f"- {c}" for c in block.test_change_claims)
        report_parts.append(
            "### DECLARED TEST CHANGES (unverified claims — verify against the diff)\n"
            "The implementer modified test files after they passed the RED gate and "
            "declared these reasons. Check the diff: does each change preserve what "
            "the test was asserting, or does it weaken the bar the implementation "
            "had to clear?\n"
            f"{claims}"
        )

    return await run_code_review(
        diff,
        rubric,
        config,
        spec_context=spec_context,
        standards=config.standards_text(),
        implementer_report="\n\n".join(report_parts),
    )


async def run_tdd_engine(config: BuildConfig, args, *, standalone: bool = True) -> int:
    """Main entry point for the TDD engine.

    Orchestrates: parse blocks → baseline gate → per-block TDD → final review.

    *standalone* says who owns the build's lifecycle. True (the
    `python -m build_pipeline tdd` entry point) means the engine IS the build:
    on success it advances the checkpoint to COMPLETE and deletes it. False
    (the orchestrator's TDD_BUILD sub-phase) means the orchestrator owns phase
    advancement and cleanup, and the engine must leave the checkpoint alone —
    deleting it here made the orchestrator's post-engine reload
    (`if state_path.exists(): state = BuildState.load(state_path)`) fall
    through to a stale in-memory copy with `tdd is None`, so a crash in
    DOCS_UPDATE/RESPEC/PUBLISH resumed at TDD_BUILD with no block state and
    re-ran the whole build; and a crash in the few lines between the advance
    and the cleanup left a checkpoint at COMPLETE, silently skipping every
    remaining phase.
    """
    state_path = config.state_file_path()
    progress = ProgressWriter(config.progress_file_path())

    # Ceilings first, so every call below is accounted against them.
    budget_mod.configure(
        max_tokens=config.budget_max_tokens,
        max_usd=config.budget_max_usd,
    )

    # Load or create state
    if state_path.exists():
        state = BuildState.load(state_path)
        # A resumed build inherits what the earlier run already spent —
        # a fresh budget on resume would make the ceiling unenforceable.
        budget_mod.get_budget().load_state(state.budget)
        log.info("START phase=TDD_ENGINE resumed=true block=%d spent=%d tokens",
                 state.tdd.current_block if state.tdd else 0,
                 budget_mod.get_budget().total_tokens)
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
    # The card is In Development for the whole engine run (a no-op emission
    # when the orchestrator already put it there).
    board.record(config, state, BuildPhase.TDD_BUILD, progress=progress,
                 note=f"{total_blocks} block(s)")

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

        # Block boundary is the cheapest place to stop: nothing is half-done,
        # and the state file already holds the spend so a resume with a raised
        # ceiling picks up here rather than restarting.
        try:
            budget_mod.check(phase=f"block {block.number} of {total_blocks}")
        except BudgetExceededError as e:
            log.error("FAIL phase=TDD_ENGINE reason=budget_exhausted block=%d", block.number)
            progress.log_phase("BUDGET", f"STOPPED: {e}\n{e.diagnosis}")
            state.checkpoint(state_path)
            return EXIT_BUILD_FAILURE

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

    # --- Whole-change refactor (one conservative pass, before final review) ---
    # Runs here and not per block on purpose: cross-block duplication is only
    # visible once every block exists, and the final review should grade the
    # code that ships, not a version the refactor is about to change.
    if not await _run_refactor_all(config, state, progress):
        return EXIT_BUILD_FAILURE

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
    if standalone:
        state.advance_to(BuildPhase.COMPLETE, state_path)
    else:
        # Persist the finished TDD sub-state so the orchestrator's reload sees
        # it; the phase pointer stays at TDD_BUILD for the orchestrator to
        # advance.
        state.checkpoint(state_path)
    # Report the spend even when nothing stopped: a build that finished at 95%
    # of its ceiling is the one worth knowing about before the next run.
    spend = budget_mod.get_budget()
    log.info("DONE phase=TDD_ENGINE blocks=%d tokens=%d cost_usd=%.2f calls=%d",
             total_blocks, spend.total_tokens, spend.cost_usd, spend.calls)
    progress.log_phase("BUDGET", spend.diagnosis())
    progress.log_phase("COMPLETE", f"All {total_blocks} blocks implemented successfully")
    if standalone:
        state.cleanup(state_path)
    return EXIT_SUCCESS


def capture_build_diff(config: BuildConfig, state: BuildState) -> str:
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


def _snapshot_all_test_files(config: BuildConfig, state: BuildState) -> dict[str, str] | None:
    """{repo-relative path: sha256} for every test file the build touched.

    The union of each block's snapshot, so the whole-change refactor is
    measured against the same hashes the per-block windows used. Returns None
    the moment any block's list is unavailable — the invariant that an
    unobtainable snapshot means "not checked", never "everything was deleted",
    holds here too.
    """
    if not state.tdd or not state.tdd.blocks:
        return {}
    merged: dict[str, str] = {}
    for block in state.tdd.blocks:
        snapshot = _snapshot_test_files(config, block)
        if snapshot is None:
            return None
        merged.update(snapshot)
    return merged


async def _run_refactor_all(
    config: BuildConfig, state: BuildState, progress: ProgressWriter,
) -> bool:
    """One conservative refactor pass over the whole change.

    Returns False only when the budget breaker stopped the build — every other
    outcome (no diff, agent failure, failed verification) is non-fatal and
    returns True, because a refactor must never turn a green build red.

    Safety harness, identical in shape to the per-block one: the pending tree
    is proven green and then committed first so there is a faithful point to
    rewind to (a tree that is red, or that cannot be checkpointed, skips the
    pass — never a checkpoint of breakage, never a reset over uncommitted
    work), the full suite must be green afterwards, and `unchanged_tests_gate`
    is re-applied with no report — the `TEST CHANGE REQUIRED:` escape hatch
    does not exist in a refactor window.
    """
    if not state.tdd or state.tdd.refactor_all_done:
        return True

    # A build-sized agent run is worth a boundary check, same as a block.
    try:
        budget_mod.check(phase="whole-change refactor")
    except BudgetExceededError as e:
        log.error("FAIL phase=REFACTOR_ALL reason=budget_exhausted")
        progress.log_phase("BUDGET", f"STOPPED: {e}\n{e.diagnosis}")
        state.checkpoint(config.state_file_path())
        return False

    diff = capture_build_diff(config, state)
    if not diff:
        log.info("SKIP phase=REFACTOR_ALL reason=no-diff-captured")
        state.tdd.refactor_all_done = True
        state.checkpoint(config.state_file_path())
        return True

    # Prove the tree green BEFORE checkpointing it as the rewind baseline.
    # Every block ended green, so a red suite here means the tree holds
    # unverified edits — a prior whole-change pass that crashed after its
    # agent wrote files (resume re-enters with refactor_all_done still
    # False), or a review fix that broke the suite after the last
    # integration run. Committing them would bake the breakage into the
    # exact commit a failed verification later restores, and nothing
    # downstream re-runs the suite (the final review is LLM-graded). Whose
    # edits are dirty is unknowable from here, and unknown is never safe to
    # destroy — so: no reset, no checkpoint, no refactor pass.
    pre_gate = integration_gate(config.test_command, config.project_dir)
    if not pre_gate.passed:
        log.error(
            "SKIP phase=REFACTOR_ALL reason=suite-red-before-refactor: %s — "
            "the tree holds unverified edits (crashed refactor pass or a "
            "breaking review fix); NOT checkpointing them as a rewind "
            "baseline. The suite is RED — review before shipping.",
            pre_gate.reason,
        )
        progress.log_phase("REFACTOR_ALL",
                           f"SKIPPED — suite red before refactor: {pre_gate.reason}")
        state.tdd.refactor_all_done = True
        state.checkpoint(config.state_file_path())
        return True

    # Review-fix edits are left uncommitted by the block loop, so "the last
    # commit" is only a faithful pre-refactor snapshot once they are in it.
    # Without this, discarding a failed refactor would take the build's own
    # review fixes with it.
    rewind_to = git_ops.commit_tree(
        config.project_dir,
        "chore: checkpoint before whole-change refactor",
        "pre-refactor checkpoint",
    )
    if rewind_to is None and git_ops.tree_is_clean(config.project_dir):
        # Nothing to commit and the tree is clean: HEAD already holds
        # everything, so it is a faithful snapshot (same guard as the
        # per-block rewind).
        rewind_to = git_ops.rev_parse_head(config.project_dir)
    if rewind_to is None:
        # `git_ops.commit_tree` returns None for "commit failed" too (failing
        # hook, missing identity) — with work still in the tree, possibly all
        # of it staged. Trusting HEAD then would let a failed verification
        # `reset --hard` + `clean -fd` over the uncommitted work and destroy
        # it. The rewind target is unknown, and unknown is never safe to
        # reset — skip the pass rather than run an agent with no way back.
        log.warning(
            "SKIP phase=REFACTOR_ALL reason=no-rewind-target: pre-refactor "
            "checkpoint commit failed with uncommitted work in the tree")
        progress.log_phase("REFACTOR_ALL",
                           "SKIPPED — could not checkpoint the pending tree")
        state.tdd.refactor_all_done = True
        state.checkpoint(config.state_file_path())
        return True
    before = _snapshot_all_test_files(config, state)

    log.info("START phase=REFACTOR_ALL diff_chars=%d", len(diff))
    progress.log_phase("REFACTOR_ALL", "Simplifying across blocks")
    result = await run_refactor_all(
        diff=diff,
        design=config.design_path.read_text() if config.design_path.exists() else "(no design.md)",
        rubric=config.rubric_path.read_text() if config.rubric_path.exists() else "(no rubric.md)",
        test_command=config.test_command,
        model=config.model,
        effort=config.agent_effort,
        cwd=str(config.project_dir),
    )

    if not result.success:
        reason = "timeout" if result.timed_out else result.error
        log.warning("FAIL phase=REFACTOR_ALL reason=%s (non-fatal)", reason)
        progress.log_phase("REFACTOR_ALL",
                           "TIMEOUT" if result.timed_out else f"FAILED: {reason}")
        state.tdd.refactor_all_done = True
        state.checkpoint(config.state_file_path())
        return True

    gate = integration_gate(config.test_command, config.project_dir)
    detail = "" if gate.passed else f"tests failed after refactor: {gate.reason}"
    if gate.passed:
        after = _snapshot_all_test_files(config, state)
        if before is None or after is None:
            log.warning("Test integrity could not run after the whole-change "
                        "refactor: test file list unavailable")
            progress.log_phase("REFACTOR_ALL", "test integrity NOT CHECKED")
        else:
            # No report argument, deliberately: no escape hatch in this window.
            tests_gate = unchanged_tests_gate(before, after, "")
            if not tests_gate.passed:
                detail = f"test files changed during refactor: {tests_gate.reason}"

    if detail:
        if rewind_to and git_ops.discard_to(config.project_dir, rewind_to, "whole-change refactor"):
            log.warning("DONE phase=REFACTOR_ALL result=discarded reason=%s", detail)
            progress.log_phase("REFACTOR_ALL", f"REVERTED — {detail}")
        else:
            # The refactor's edits failed verification AND could not be
            # discarded — they stay in the tree, and nothing downstream
            # re-runs the suite (the final review is LLM-graded). Say so
            # loudly rather than let the build reach COMPLETE looking clean.
            log.error("DONE phase=REFACTOR_ALL result=unverified-and-not-discarded "
                      "reason=%s — UNVERIFIED refactor edits remain in the tree "
                      "and will ship unless reviewed", detail)
            progress.log_phase("REFACTOR_ALL", f"REVERT FAILED — {detail}")
    else:
        sha = git_ops.commit_tree(
            config.project_dir, "refactor: simplify across blocks",
            "whole-change refactor",
        )
        log.info("DONE phase=REFACTOR_ALL result=committed sha=%s",
                 sha[:8] if sha else "(no changes)")
        progress.log_phase("REFACTOR_ALL",
                           "COMMITTED" if sha else "PASSED (no changes needed)")

    state.tdd.refactor_all_done = True
    state.checkpoint(config.state_file_path())
    return True


def _build_trajectory_text(state: BuildState) -> str:
    """Per-block history for the final review's trajectory judgment.

    The cumulative diff alone shows the destination, not the path. Whether a
    block needed three review iterations, or declared a test change, or ran a
    panel that disagreed with itself, is the signal that separates "arrived
    somewhere fine" from "drifted there" — and it is only visible here.
    """
    if not state.tdd or not state.tdd.blocks:
        return "(no per-block trajectory recorded)"
    lines: list[str] = []
    for b in state.tdd.blocks:
        parts = [f"- Block {b.number} ({b.name}): {b.status.value}"]
        if b.review_iterations:
            parts.append(f"{b.review_iterations} review iteration(s)")
        if b.review_scores is not None:
            parts.append(f"final weighted score {b.review_scores.weighted_average:.1f}")
            if b.review_scores.rejected_findings:
                parts.append(
                    f"{len(b.review_scores.rejected_findings)} finding(s) rejected by the orchestrator"
                )
        if b.test_change_claims:
            parts.append(
                "TEST FILES CHANGED after RED, declared reason(s): "
                + "; ".join(b.test_change_claims)
            )
        lines.append(" — ".join(parts))
    return "\n".join(lines)


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

    diff = capture_build_diff(config, state)
    if not diff:
        log.warning("No full-build diff captured — final review sees rubric only")
    prompt = FINAL_REVIEW_PROMPT.format(
        diff=diff or "(no diff captured)",
        rubric=rubric,
        trajectory=_build_trajectory_text(state),
    )
    model = getattr(config, "review_model", None) or config.model
    try:
        verdict = await llm_call_structured(prompt, FinalReviewVerdict, model=model,
                                            effort=config.review_effort, max_retries=1,
                                            budget_label="final_review")
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
