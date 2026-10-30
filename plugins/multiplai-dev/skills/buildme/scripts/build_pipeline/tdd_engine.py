"""TDD implementation engine — block-by-block test-first build.

Parses tasks.md into blocks, runs model-adaptive TDD cycles per block,
gates on integration tests and quality reviews after each block, and runs
a final comprehensive review.

Exit codes: 0=success, 1=build failure, 4=agent timeout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from . import board, git_ops, review_rounds, stop_record
from . import budget as budget_mod
from .budget import BudgetExceededError
from .change_manager import (
    extract_global_constraints,
    requirement_sources,
    requirement_texts,
)
from .config import BuildConfig
from .gates import (
    TEST_RUNS_DIR,
    RunnerFailed,
    _repo_trusted,
    agent_status_gate,
    baseline_test_gate,
    failing_tests,
    failure_log_path,
    final_verification,
    integration_gate,
    parse_diff_paths,
    parse_handoff_note,
    parse_implementation_note,
    parse_recurring,
    parse_searched,
    red_expected_note,
    red_gate,
    review_iteration_gate,
    review_score_gate,
    run_test_suite,
    unchanged_tests_gate,
    verification_inputs,
    wiring_task_gate,
)
from .llm_steps.respec_steps import append_implementation_note
from .llm_steps.review_steps import run_code_review
from .llm_steps.tdd_steps import (
    run_implementer,
    run_integration_fix,
    run_mutation_test_writer,
    run_refactor_all,
    run_refactorer,
    run_test_writer,
)
from .models import (
    BlockInfo,
    BlockStatus,
    BuildPhase,
    EndSuiteState,
    FindingAdjudication,
    FixOnPassState,
    GateResult,
    HandoffNote,
    MutationResult,
    ReviewFinding,
    ReviewResult,
    TestQualityAudit,
)
from .mutation import (
    changed_source_files,
    mutation_applies,
    render_report,
    run_mutation,
    score_text,
)
from .narrow_tests import (
    agent_test_command,
    agent_test_command_multi,
    narrow_parts,
    narrow_test_command,
    narrow_test_command_multi,
    runner_of,
)
from .progress import ProgressWriter
from .prompts.implementation import (
    REVIEW_FIX_ESCALATION,
    REVIEW_FIX_LOCATIONS,
    REVIEW_FIX_SELF_CHECK,
)
from .prompts.review import (
    ADJUDICATION_ROUND_MEMORY,
    ADJUDICATION_SCOPE_FIELD,
    ADJUDICATION_SCOPE_SECTION,
    FINDING_ADJUDICATION_PROMPT,
)
from .prompts.test_writing import DRAFTS_SECTION, TEST_QUALITY_PROMPT
from .sdk import llm_call_structured
from .state import BuildState, TDDState

log = logging.getLogger(__name__)


# When each block started in this process, for `_tdd_event`'s elapsed time.
_BLOCK_STARTED: dict[tuple[str, int], float] = {}
# The `TDD:` line's phase for the status a block fails from.
_FAIL_PHASES = {
    BlockStatus.TESTING: "TEST_WRITE",
    BlockStatus.IMPLEMENTING: "IMPLEMENT",
    BlockStatus.REVIEWING: "REVIEW",
}


def _tdd_event(
    config: BuildConfig,
    state: BuildState,
    block: BlockInfo,
    phase: str,
    status: str,
    detail: str = "",
) -> None:
    """Print one `TDD: <change> block=N/M phase=<PHASE> <START|DONE|FAIL>
    elapsed=<s>` line on stdout and append it to the progress file, so a
    build's progress shows outside the daily log. Elapsed counts from the
    block's start in this process (from its first event on a resume)."""
    total = len(state.tdd.blocks) if state.tdd else 0
    started = _BLOCK_STARTED.setdefault(
        (config.change_name, block.number), time.monotonic()
    )
    line = (
        f"TDD: {config.change_name} block={block.number}/{total} phase={phase} "
        f"{status} elapsed={time.monotonic() - started:.0f}"
        + (f" {detail}" if detail else "")
    )
    print(line, flush=True)
    try:
        ProgressWriter(config.progress_file_path()).log_line(line)
    except OSError as e:
        log.warning("Could not append to the progress file: %s", e)


def _mark_block(
    state: BuildState,
    block_idx: int,
    status: BlockStatus,
    config: BuildConfig,
) -> None:
    """Set a block's status, checkpoint, and refresh the board card.

    Every block status is In Development (board.py's docstring says why —
    notably REVIEWING is an in-process review with no pushed branch, so it is
    not In Review). The board write still happens on each change so
    `.board.json` stays current when the TDD engine runs standalone; it emits a
    `BOARD:` line only when the card actually moves columns.
    """
    if status == BlockStatus.FAILED and state.tdd and block_idx < len(state.tdd.blocks):
        block = state.tdd.blocks[block_idx]
        _tdd_event(
            config, state, block, _FAIL_PHASES.get(block.status, "BLOCK"), "FAIL"
        )
    state.mark_block_status(block_idx, status, config.state_file_path())
    board.record(
        config,
        state,
        BuildPhase.TDD_BUILD,
        block_status=status,
        note=f"block {block_idx + 1} {status.value}",
    )


def _git_commit_block_phase(
    config: BuildConfig, phase: str, block: BlockInfo
) -> str | None:
    """Commit the block phase's changes in the project repo.

    phase: "test", "impl", "refactor" or "fix" (used for the conventional-commit
    prefix). Returns the new SHA or None for no changes; commit failures raise.
    """
    return git_ops.commit_tree(
        config.project_dir,
        f"{phase}(block-{block.number}): {block.name}",
        f"block={block.number} phase={phase}",
    )


def _test_command_since(config: BuildConfig, base: str | None) -> str:
    """`test_command` narrowed to the test files changed since *base* and the
    tests named after the source files changed since it (`narrow_tests`), or
    the whole `test_command` when it cannot narrow it. With
    `tdd.narrow_commands` each directory's command is narrowed in it."""
    if config.narrow_commands:
        return narrow_test_command_multi(
            config.narrow_commands,
            config.project_dir,
            base,
            config.narrow_tests,
            config.test_command,
        )
    return narrow_test_command(
        config.test_command, config.project_dir, base, enabled=config.narrow_tests
    )


def _agent_test_command_since(config: BuildConfig, base: str | None) -> str:
    """`_test_command_since`, plus how to add the test files an agent writes
    and the tests named after the modules it changes, which the command,
    fixed before it starts, cannot list."""
    if config.narrow_commands:
        return agent_test_command_multi(
            config.narrow_commands,
            config.project_dir,
            base,
            config.narrow_tests,
            config.test_command,
        )
    return agent_test_command(
        config.test_command, config.project_dir, base, enabled=config.narrow_tests
    )


def _block_test_command(config: BuildConfig, block: BlockInfo) -> str:
    """The test command for the block's RED check: `_test_command_since` the
    block's start. The gates that decide whether work is done keep the whole
    `test_command` under `tdd.whole_suite: per_block` (`_block_gate`)."""
    return _test_command_since(config, block.baseline_commit)


def _block_agent_test_command(config: BuildConfig, block: BlockInfo) -> str:
    """What the block's agents (test writer, implementer, refactorer,
    integration and review fixers) are given as their test command."""
    return _agent_test_command_since(config, block.baseline_commit)


def _change_base(state: BuildState) -> str | None:
    """The commit the build started from: the first block's start."""
    if state.tdd and state.tdd.blocks:
        return state.tdd.blocks[0].baseline_commit
    return None


def _change_test_command(config: BuildConfig, state: BuildState) -> str:
    """`_test_command_since` the start of the build: every test file the
    change touched and the tests named after every module it changed."""
    return _test_command_since(config, _change_base(state))


def _change_agent_test_command(config: BuildConfig, state: BuildState) -> str:
    """What an agent working on the whole change is given as its test command
    (the whole-change refactorer, the mutation test writer, the end fixer)."""
    return _agent_test_command_since(config, _change_base(state))


# git's well-known empty-tree object. Used as the diff baseline when the
# project repo has no commits at block start (fresh `git init`): diffing
# against the empty tree makes block 1's committed work visible to the
# reviewer, whereas a None baseline would fall back to `git diff HEAD`,
# which post-commit shows nothing.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


# Cap the diff handed to the reviewer so a pathological block can't blow the
# context window. 150k chars ≈ 40k tokens — far above any sane block diff.
MAX_REVIEW_DIFF_CHARS = 150_000


# The test files the weak-test scan reads: Python (`_TEST_FILE_RE`) and
# JS/TS (`_JS_TEST_FILE_RE`, the shapes `narrow_tests._NODE_TEST_RE` names,
# plus anything under `__tests__/`). The scan counts each file's tests by its
# language (`_count_tests`); its weak patterns and mock heuristics are
# Python-only and run on the Python files alone. Swift and Go are not read:
# a scan with no counter for them would fail every block on "no test
# functions found".
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")
_JS_TEST_FILE_RE = re.compile(
    r"(^|/)[^/]+\.(test|spec)\.[cm]?[jt]sx?$|(^|/)__tests__/.+\.[cm]?[jt]sx?$"
)
# The header `_read_block_test_files` writes before each file's source.
_TEST_FILE_HEADER_RE = re.compile(r"^# --- (.+) ---$", re.MULTILINE)
_PY_TEST_DEF_RE = re.compile(r"def\s+test_\w+")
# `it(` / `test(`, `.each(...)(` and `.concurrent(`; not `it.skip`, `xit` or
# `test.todo`.
_JS_TEST_CALL_RE = re.compile(
    r"^\s*(it|test)(\.each\(.*?\)|\.concurrent)?\s*\(", re.MULTILINE
)

# Language-agnostic, used only for integrity hashing. The integrity gate does
# not parse test source — it only needs to know which paths are tests — so it
# must cover every language buildme runs on, or the gate silently no-ops (empty
# `before` → "not checked") on a Swift or TypeScript repo.
_TEST_PATH_RE = re.compile(
    r"(^|/)("
    r"test_[^/]+\.py|[^/]+_test\.py|conftest\.py"  # Python
    r"|tests\.py"  # Django: <app>/tests.py
    r"|[^/]+_test\.go"  # Go
    r"|[^/]+Tests?\.swift"  # Swift
    r"|[^/]+\.(test|spec)\.[cm]?[jt]sx?"  # JS/TS
    r")$"
    r"|(^|/)(tests?|__tests__|Tests)/"  # anything under a tests dir
)
MAX_TEST_SCAN_CHARS = 200_000


def _read_block_test_files(config: BuildConfig, block: BlockInfo) -> str:
    """Concatenated source of the test files this block added or modified.

    The quality gate scans real test source, never the test-writer agent's
    prose report — a report contains no `def test_*`, so scanning it makes the
    gate fail every block on "No test functions found". Returns "" when git
    cannot tell us what changed; the caller falls back to the agent output.
    """
    names = _list_block_test_files(config, block)
    if names is None:
        return ""

    parts: list[str] = []
    for name in names:
        if not (
            _TEST_FILE_RE.search(name.strip()) or _JS_TEST_FILE_RE.search(name.strip())
        ):
            continue
        path = config.project_dir / name
        try:
            parts.append(f"# --- {name.strip()} ---\n{path.read_text()}")
        except OSError as e:
            log.warning("Could not read test file %s: %s", name, e)

    content = "\n\n".join(parts)
    return content[:MAX_TEST_SCAN_CHARS]


def _list_block_test_files(config: BuildConfig, block: BlockInfo) -> list[str] | None:
    """Project-relative paths of the test files this block added or modified.

    Returns None when git could not be asked (non-zero exit, timeout, not a
    repo) — distinct from `[]`, which means "asked, and this block touched no
    test files". Collapsing the two made a transient git failure look like the
    agent had deleted every test file; see `_snapshot_test_files`.
    """
    target = block.baseline_commit or "HEAD"
    try:
        proc = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--relative",
                "-z",
                target,
                "--",
                ".",
                *git_ops.BOOKKEEPING_EXCLUDES,
            ],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.warning(
                "Could not list changed files for block %d: %s",
                block.number,
                proc.stderr.strip(),
            )
            return None
        names = proc.stdout.split("\0")
        untracked = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
                *git_ops.BOOKKEEPING_EXCLUDES,
            ],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if untracked.returncode:
            return None
        names += untracked.stdout.split("\0")
    except Exception as e:
        log.warning("Could not list changed files for block %d: %s", block.number, e)
        return None
    return [n for n in names if n and _TEST_PATH_RE.search(n)]


def _snapshot_test_files(
    config: BuildConfig, block: BlockInfo
) -> dict[str, str] | None:
    """{project-relative path: sha256} for the block's test files, right now.

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
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("Could not hash test file %s: %s", name, e)
            return None
    return snapshot


# What the Claude CLI's error says when it stops an agent at its turn cap.
TURN_LIMIT_ERROR = "Reached maximum number of turns"


def _hit_turn_limit(result) -> bool:
    return (
        not result.success
        and not result.timed_out
        and TURN_LIMIT_ERROR in (result.error or "")
    )


def _uncommitted_test_files(config: BuildConfig) -> list[str] | None:
    """Project-relative paths of the test files changed since HEAD or not yet
    tracked, that are on disk. None when git could not be asked.

    `_list_block_test_files` diffs tracked files only, so it misses the new
    test files an agent wrote and never committed."""
    # buildme's own files and the specs are never the agent's tests, even
    # under a change named `test`.
    pathspec = ["--", ".", *git_ops.BOOKKEEPING_EXCLUDES]
    try:
        pathspec.append(
            f":(exclude){config.specs_dir.resolve().relative_to(config.project_dir.resolve()).as_posix()}"
        )
    except ValueError:
        pass
    names: set[str] = set()
    for argv in (
        ["git", "diff", "--name-only", "--relative", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            proc = subprocess.run(
                [*argv, *pathspec],
                cwd=str(config.project_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            log.warning("Could not list uncommitted test files: %s", e)
            return None
        if proc.returncode != 0:
            log.warning(
                "Could not list uncommitted test files: %s", proc.stderr.strip()
            )
            return None
        names |= {n.strip() for n in proc.stdout.splitlines() if n.strip()}
    return sorted(
        n
        for n in names
        if _TEST_PATH_RE.search(n) and (config.project_dir / n).is_file()
    )


def _kept_after_turn_limit(
    config: BuildConfig, block: BlockInfo, result, progress: ProgressWriter
) -> list[str]:
    """The test files a test-writer *result* that stopped at the turn cap left
    on disk, which then go on to the gates as if it had finished; [] for any
    other result."""
    kept = (_uncommitted_test_files(config) or []) if _hit_turn_limit(result) else []
    if kept:
        log.warning(
            "WARN block=%d phase=TEST_WRITE reason=turn_limit kept_files=%d",
            block.number,
            len(kept),
        )
        progress.log_agent(
            "TestWriter",
            block.name,
            f"ran out of turns — its {len(kept)} test file(s) go to the quality and RED gates",
        )
    return kept


def _drafts_section(config: BuildConfig, files: list[str]) -> str:
    """DRAFTS_SECTION and one line per file: new and its line count, or the
    lines `git diff --numstat` counts added and removed since HEAD."""
    lines = []
    for name in files:
        proc = subprocess.run(
            ["git", "diff", "--numstat", "--relative", "HEAD", "--", name],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        counts = proc.stdout.split()[:2] if proc.returncode == 0 else []
        if len(counts) == 2:
            lines.append(f"- `{name}` (+{counts[0]} -{counts[1]})")
        else:
            try:
                n = len(
                    (config.project_dir / name).read_text(errors="replace").splitlines()
                )
                lines.append(f"- `{name}` (new file, {n} lines)")
            except OSError:
                lines.append(f"- `{name}`")
    return DRAFTS_SECTION + "\n".join(lines)


def _unreported_tests(block: BlockInfo) -> str:
    """What the implementer is given as the failing tests when there is no
    test-writer report (it stopped at the turn cap, or an earlier run wrote the
    tests): the RED gate's run of them."""
    return (
        "No test-writer report is available: the test writer stopped at the turn cap before it "
        "reported, or an earlier run wrote these tests. They are committed, and this is how they "
        f"failed in the RED gate:\n{block.red_evidence}"
    )


def _capture_block_diff(config: BuildConfig, block: BlockInfo) -> str:
    """Capture everything the block changed: baseline commit → working tree.

    Blocks commit at phase boundaries (test/impl commits), so diffing from the
    recorded pre-block baseline covers those commits plus any uncommitted
    refactor/review-fix edits. An empty repo at block start records
    EMPTY_TREE_SHA as the baseline (see run_block_tdd). The `git diff HEAD`
    fallback is a last resort for a truly-None baseline (e.g. checkpoints
    written before baselines existed) and misses committed work. Returns ""
    on git failure — never raises.

    Files that are still untracked (created after the last phase commit, e.g.
    by a refactor or fix agent) are added as new-file diffs
    (`_untracked_diff`): the next commit takes them with the block, so the
    reviewer must see them before it does.

    The block's review round records (`review_rounds`) are left out: they are
    committed with the block, and are the review's output, not its input.
    """
    target = block.baseline_commit or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", "--relative", target, *_without_review_records(config)],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.warning(
                "Failed to capture diff for block %d (git diff %s): %s",
                block.number,
                target,
                proc.stderr.strip(),
            )
            return ""
        diff = proc.stdout + _untracked_diff(config)
    except Exception as e:
        log.warning("Failed to capture diff for block %d: %s", block.number, e)
        return ""
    if len(diff) > MAX_REVIEW_DIFF_CHARS:
        diff = diff[:MAX_REVIEW_DIFF_CHARS] + "\n... (diff truncated for review)"
    return diff


def _without_review_records(config: BuildConfig) -> list[str]:
    """Pathspec args that leave the change dir's review round records out of a
    `git diff`. They are committed with each block, and a diff capped at
    `MAX_REVIEW_DIFF_CHARS` must not spend its budget on them: `specs/` sorts
    before most source directories, so the cap would cut code instead."""
    try:
        records = (
            (config.change_dir / review_rounds.REVIEWS_DIR)
            .resolve()
            .relative_to(Path(config.project_dir).resolve())
        )
    except ValueError:
        return []
    # An exclude-only pathspec keeps the whole-repo scope of a bare `git diff`.
    return ["--", f":(exclude,literal){records.as_posix()}"]


def _untracked_diff(config: BuildConfig) -> str:
    """New-file diffs for every untracked file the next phase commit would take.

    `git diff <baseline>` shows only tracked paths, but `git_ops.commit_tree`
    runs `git add -A`, so a file an agent created since the last commit is
    committed with the block without ever being reviewed. Read-only: the
    index is not touched. Listed the way `commit_tree` stages: not ignored, and
    not buildme's bookkeeping; the round records stay out as in
    `_without_review_records`. Stops once the output passes
    `MAX_REVIEW_DIFF_CHARS`, since the caller cuts it there anyway. Returns ""
    on git failure."""
    records = _without_review_records(config)[1:]
    try:
        listed = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
                *git_ops.BOOKKEEPING_EXCLUDES,
                *records,
            ],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        log.warning("Could not list untracked files for the review diff: %s", e)
        return ""
    if listed.returncode != 0:
        log.warning(
            "Could not list untracked files for the review diff: %s",
            listed.stderr.strip(),
        )
        return ""
    parts: list[str] = []
    size = 0
    for name in sorted(n for n in listed.stdout.split("\0") if n):
        if size > MAX_REVIEW_DIFF_CHARS:
            break
        try:
            # --no-index exits 1 when the files differ, which they always do here.
            proc = subprocess.run(
                ["git", "diff", "--no-index", "--", os.devnull, name],
                cwd=str(config.project_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as e:
            log.warning("Could not diff untracked file %s: %s", name, e)
            continue
        if proc.returncode > 1:
            log.warning(
                "Could not diff untracked file %s: %s", name, proc.stderr.strip()
            )
            continue
        parts.append(proc.stdout)
        size += len(proc.stdout)
    return "".join(parts)


EXIT_SUCCESS = 0
EXIT_BUILD_FAILURE = 1
# 4, not 3: `build` passes this code through, and 3 is intake.WAITING_EXIT
# ("waiting, re-run to resume") — a timeout must never read as waiting.
EXIT_AGENT_TIMEOUT = 4

# Circuit breaker: attempts 1-2 run the scoped fix prompt; the final attempt
# escalates — question-the-architecture prompt on
# config.model_for("integration_fix_escalated"), the review model when one is
# set. Exhaustion fails the block with a diagnosis in build-progress.md.
MAX_INTEGRATION_FIX_ATTEMPTS = 3
MAX_REVIEW_ITERATIONS = 3

# Cap for RED/GREEN evidence blobs stored in block state and fed to the
# reviewer — the interesting part of a pytest run is the tail.
MAX_EVIDENCE_CHARS = 4000


def _trim_evidence(output: str) -> str:
    return output[-MAX_EVIDENCE_CHARS:]


# Notes for later blocks (`carry_block_notes`). Starting values, not measured:
# the notes-forward benchmark records the real sizes.
HANDOFF_NOTE_MAX_CHARS = 1500
HANDOFF_SECTION_MAX_CHARS = 12000
HANDOFF_HEADING = (
    "## Notes from earlier blocks (written by their agents; "
    "check them against the code before relying on them)"
)


# Weak test patterns for Phase A.5 quality check (line-level; per-function
# mock heuristics live in _scan_test_function)
WEAK_TEST_PATTERNS = [
    re.compile(r"assert\s+True\b"),
    re.compile(r"assert\s+\w+\s+is\s+not\s+None\s*$", re.MULTILINE),
    re.compile(
        r"def\s+test_\w+\s*\([^)]*\)\s*:\s*\n\s*(pass|\.\.\.)\s*$", re.MULTILINE
    ),
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
        for line in content[m.end() : end].splitlines():
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            body_lines.append(line)
        yield m.group(2), "\n".join(body_lines)


def _scan_test_function(name: str, body: str) -> list[str]:
    """Mechanical mock anti-pattern findings for one test function."""
    lines = body.splitlines()
    mock_asserts = [ln for ln in lines if _MOCK_ASSERT_RE.match(ln)]
    real_asserts = [
        ln
        for ln in lines
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
            for line in body[interfaces_match.end() :].splitlines():
                bullet = re.match(r"^-\s+(Produces|Consumes)\b[^:]*:\s*(.+)$", line)
                if bullet:
                    value = bullet.group(2).strip()
                    if value and value.lower() != "(none)":
                        (
                            produces if bullet.group(1) == "Produces" else consumes
                        ).append(value)
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
                description = body[: satisfies_match.start()].strip()
            else:
                description = body.strip()

        blocks.append(
            BlockInfo(
                number=number,
                name=name,
                description=description,
                satisfies=satisfies,
                produces=produces,
                consumes=consumes,
            )
        )

    return blocks


def _global_constraints_text(config: BuildConfig) -> str:
    """The design doc's Global Constraints section ("" when absent)."""
    if not config.design_path.exists():
        return ""
    return extract_global_constraints(config.design_path.read_text())


PROTOTYPE_RULE = (
    "When this block writes UI or API code, match the prototype: read the files "
    "below and copy what they show (screens, layout, names, endpoint signatures, "
    "output formats). They show only what things look like; the requirements say "
    "how they behave."
)


def _handoff_section(block: BlockInfo, blocks: list[BlockInfo]) -> str:
    """Earlier blocks' notes for later blocks, in block order, under
    `HANDOFF_SECTION_MAX_CHARS`: the oldest blocks' notes are left out first,
    and the section names them. "" when no earlier block has a note."""
    chunks = []
    for b in blocks:
        if b.number >= block.number or not b.handoff_notes:
            continue
        body = "\n\n".join(
            f"From the {n.role}"
            + (f" (round {n.round})" if n.round else "")
            + f":\n{n.text}"
            for n in b.handoff_notes
        )
        chunks.append(
            (f"Block {b.number} ({b.name})", f"### Block {b.number}: {b.name}\n{body}")
        )
    if not chunks:
        return ""
    left_out = []
    while (
        len(chunks) > 1 and sum(len(c) for _, c in chunks) > HANDOFF_SECTION_MAX_CHARS
    ):
        left_out.append(chunks.pop(0)[0])
    if left_out:
        log.info(
            "Handoff notes left out for block %d: %s", block.number, ", ".join(left_out)
        )
    lines = [HANDOFF_HEADING]
    if left_out:
        lines.append(
            f"Left out, over the {HANDOFF_SECTION_MAX_CHARS}-character limit: "
            f"the notes of {', '.join(left_out)}."
        )
    lines += [c for _, c in chunks]
    return "\n\n".join(lines)


def prototype_section(config: BuildConfig) -> str:
    """`## Prototype`: `PROTOTYPE_RULE` and the files under
    `change_root(change_dir)/prototype/`, relative to the project when they
    are inside it; "" when there are none."""
    from multiplai_core.specs import change_root

    proto = change_root(Path(config.change_dir)) / "prototype"
    if not proto.is_dir():
        return ""
    files = sorted(p for p in proto.rglob("*") if p.is_file())
    if not files:
        return ""

    def shown(p: Path) -> str:
        try:
            return p.relative_to(config.project_dir).as_posix()
        except ValueError:
            return str(p)

    return (
        "\n## Prototype\n"
        + PROTOTYPE_RULE
        + "\n\n"
        + "\n".join(f"- {shown(p)}" for p in files)
    )


def assemble_context(
    block: BlockInfo,
    config: BuildConfig,
    role: str,
    blocks: list[BlockInfo] | None = None,
) -> str:
    """Build the context bundle for an agent prompt.

    Includes: block info, global constraints, cross-block interfaces, design
    doc, specs, the prototype's files (`prototype_section`), rubric, memory
    files, reference docs. The role parameter
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
            for b in blocks
            if b.number < block.number
            for sig in b.produces
        ]
        if prior:
            parts.append(
                "\n## Interfaces from earlier blocks (use these signatures verbatim)\n"
                + "\n".join(prior)
            )

    # What earlier blocks' agents wrote for later blocks (`carry_block_notes`).
    # The implementer role only (the review-fix, integration-fix and fix-on-pass
    # agents get it too): the test writer stays black-box, and the refactorer
    # changes no behaviour.
    if config.carry_block_notes and role == "implementer" and blocks:
        handoff = _handoff_section(block, blocks)
        if handoff:
            parts.append("\n" + handoff)

    # Design document
    if config.design_path.exists():
        parts.append(f"\n## Design Document\n{config.design_path.read_text()}")

    # Requirements (BDD scenarios — one section per capability, from
    # specs.md or requirements/<cap>.md).
    # The test_writer already receives these verbatim through its dedicated
    # ``specs`` slot (built by the caller in run_block_tdd / the quality+RED
    # retries), so including them here too would ship the full requirements
    # set TWICE inside a single prompt — pure intra-call duplication that the
    # prompt cache cannot dedupe. Implementer/refactorer/reviewer get them
    # only via this bundle, so keep them for every role except test_writer.
    if role != "test_writer":
        for rel, text in requirement_sources(config.change_dir).items():
            parts.append(f"\n## Requirements: {rel}\n{text}")

    # The prototype pm-spec made (a component's: its change's): the agents
    # that write the code and its tests match what it shows of a UI or API.
    if role in ("test_writer", "implementer"):
        prototype = prototype_section(config)
        if prototype:
            parts.append(prototype)

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

    # Checked facts about the existing code the block's tasks name (`block_facts:
    # true`, off by default). Re-checked at the current HEAD, because earlier
    # blocks have committed changes since the codebase analysis wrote them.
    if config.block_facts and role in ("test_writer", "implementer"):
        from .code_analysis import FACTS_FILE, block_facts_section

        facts = block_facts_section(
            block,
            config.codebase_analysis_path.parent / FACTS_FILE,
            config.project_dir,
        )
        if facts:
            parts.append("\n" + facts)

    # Signatures of the existing Python code the block names, read from the
    # working tree now. The test writer calls that code; without them it spent
    # most of its turns opening the files.
    if role == "test_writer":
        from .signatures import signatures_section

        sigs = signatures_section(block, config.project_dir)
        if sigs:
            parts.append("\n" + sigs)

    # Rubric (for reviewers — include for all so agents know quality bar)
    if config.rubric_path.exists():
        parts.append(f"\n## Evaluation Rubric\n{config.rubric_path.read_text()}")

    # Project context
    if config.project_description:
        parts.append(f"\n## Project Context\n{config.project_description}")

    # Memory files (technical preferences, etc.)
    memory_dir = config.config_dir / "memory"
    for mem_file in (
        config.core_memory_files
        + config.stack_memory_files
        + config.additional_memory_files
    ):
        mem_path = memory_dir / mem_file
        if mem_path.exists():
            parts.append(f"\n## Memory: {mem_file}\n{mem_path.read_text()}")

    # Stack reference docs
    for ref_doc in config.stack_reference_docs():
        parts.append(f"\n## Reference: {ref_doc.name}\n{ref_doc.read_text()}")

    return "\n\n".join(parts)


def _count_tests(content: str) -> tuple[str, int]:
    """(the Python source in *content*, its test count). *content* is
    `_read_block_test_files`' output: each file's source after a
    `# --- <name> ---` header, counted by its language — `def test_` in
    Python, `it(`/`test(` calls in JS/TS (`_JS_TEST_CALL_RE`). Without
    headers (the test writer's report) it is all read as Python."""
    headers = list(_TEST_FILE_HEADER_RE.finditer(content))
    if not headers:
        return content, len(_PY_TEST_DEF_RE.findall(content))
    python: list[str] = [content[: headers[0].start()]]
    total = 0
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(content)
        body = content[header.end() : end]
        if _JS_TEST_FILE_RE.search(header.group(1)):
            total += len(_JS_TEST_CALL_RE.findall(body))
        else:
            python.append(content[header.start() : end])
            total += len(_PY_TEST_DEF_RE.findall(body))
    return "".join(python), total


def run_test_quality_check(
    test_files_content: str, contracts: str, config: BuildConfig
) -> GateResult:
    """Phase A.5: Static scan for weak test patterns.

    Scans test file content for anti-patterns like `assert True`, `assert x is not None`
    as sole assertion, and empty test bodies. Returns a GateResult.
    """
    if not config.gates.test_quality_enabled:
        return GateResult(passed=True, reason="Test quality check disabled")

    python, total_tests = _count_tests(test_files_content)
    weak_findings: list[str] = []
    for pattern in WEAK_TEST_PATTERNS:
        matches = pattern.findall(python)
        for m in matches:
            weak_findings.append(f"Weak pattern found: {m.strip()[:80]}")

    # Per-function mock heuristics (mock-assertion-only, mock-setup-dominant)
    for func_name, body in _iter_test_functions(python):
        weak_findings.extend(_scan_test_function(func_name, body))

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


async def _audit_test_quality(
    test_files_content: str, contracts: str, config: BuildConfig
) -> TestQualityAudit:
    """LLM adjudication of the static weak-test scan (TEST_QUALITY_PROMPT)."""
    prompt = TEST_QUALITY_PROMPT.format(
        test_files=test_files_content,
        contracts=contracts or "(none provided)",
    )
    return await llm_call_structured(
        prompt,
        TestQualityAudit,
        model=config.model_for("test_quality_audit"),
        effort=config.effort_for("test_quality_audit"),
        max_retries=1,
        budget_label="test_quality_audit",
    )


def _finding_lines(findings: list[ReviewFinding]) -> str:
    """Open findings, by id, for the adjudicator."""
    return (
        "\n".join(
            f"- {f.finding_id} [{f.severity}] {f.claim}"
            + (f" ({f.file_path}:{f.line})" if f.file_path else "")
            for f in findings
        )
        or "(none)"
    )


def _rejected_lines(rejected: list[dict]) -> str:
    """Earlier-rejected findings, by id, each with the judge's reason."""
    return (
        "\n".join(
            f"- {r.get('finding_id')} [{r.get('severity', '')}] {r.get('claim', '')}"
            + (f" ({r['file_path']}:{r.get('line')})" if r.get("file_path") else "")
            + f" — rejected: {r.get('judge_reason') or '(no reason recorded)'}"
            for r in rejected
        )
        or "(none)"
    )


def _round_memory_text(memory: review_rounds.RoundMemory) -> str:
    """FINDING_ADJUDICATION_PROMPT's `{round_memory}` for a round from 2 on."""
    scoped = memory.scope_diff is not None
    return ADJUDICATION_ROUND_MEMORY.format(
        open_findings=_finding_lines(memory.open),
        rejected_findings=_rejected_lines(memory.rejected),
        scope_section=(
            ADJUDICATION_SCOPE_SECTION.format(
                scope_diff=memory.scope_diff or "(no changes since the previous review)"
            )
            if scoped
            else ""
        ),
        scope_title=" and scope" if scoped else "",
        scope_field=ADJUDICATION_SCOPE_FIELD if scoped else "",
        scope_example=', "in_fix_scope": true' if scoped else "",
    )


# What only the adjudicator sets on a finding; a reviewer's value is cleared.
_JUDGE_FIELDS = {
    "finding_id": "",
    "duplicate_of": None,
    "in_fix_scope": None,
    "earlier_judge_reason": "",
}


async def _adjudicate_review_findings(
    review: ReviewResult,
    block: BlockInfo,
    config: BuildConfig,
    diff: str,
    build_context: str = "",
    memory: review_rounds.RoundMemory | None = None,
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

    *memory* (from round 2 of a block on) adds the open findings, the
    earlier-rejected ones and the scope diff to the prompt, and each accepted
    finding gets the verdict's `duplicate_of` (only an id the memory knows)
    and `in_fix_scope`. Missing values mean "not a duplicate" and "in scope",
    the same fail-open direction.
    """
    candidates = [
        f.model_copy(update=_JUDGE_FIELDS) for f in review.findings_or_derived()
    ]
    if not candidates:
        return review.model_copy(update={"findings": []})
    if not config.adjudicate_findings:
        # Off means "do not act on unadjudicated findings", NOT "apply them
        # blind" — the invariant is that nothing reaches a fix agent unjudged.
        log.info(
            "Finding adjudication disabled — %d finding(s) dropped, not applied",
            len(candidates),
        )
        return review.model_copy(
            update={"findings": [], "rejected_findings": candidates}
        )

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
        round_memory=_round_memory_text(memory) if memory else "",
    )
    try:
        adjudication = await llm_call_structured(
            prompt,
            FindingAdjudication,
            model=config.model_for("adjudication"),
            effort=config.effort_for("adjudication"),
            max_retries=1,
            budget_label="adjudication",
        )
    except BudgetExceededError:
        raise
    except Exception as e:
        log.warning(
            "Finding adjudication failed for block %d (%s) — keeping all %d findings",
            block.number,
            e,
            len(candidates),
        )
        return review.model_copy(update={"findings": candidates})

    accepted_idx = adjudication.accepted_indices(len(candidates))
    reasons = {v.index: v.reason for v in adjudication.verdicts}
    verdicts = {v.index: v for v in adjudication.verdicts}
    open_ids = {f.finding_id for f in memory.open} if memory else set()
    earlier = {r.get("finding_id"): r for r in memory.rejected} if memory else {}
    accepted, rejected = [], []
    for i, finding in enumerate(candidates):
        if i in accepted_idx:
            v = verdicts.get(i)
            if memory and v is not None:
                dup = (
                    v.duplicate_of
                    if v.duplicate_of in open_ids | set(earlier)
                    else None
                )
                finding = finding.model_copy(
                    update={
                        "duplicate_of": dup,
                        "in_fix_scope": v.in_fix_scope
                        if memory.scope_diff is not None
                        else None,
                        "earlier_judge_reason": (
                            earlier[dup].get("judge_reason") or ""
                            if dup in earlier
                            else ""
                        ),
                    }
                )
            accepted.append(finding)
        else:
            rejected.append(
                finding.model_copy(
                    update={
                        "evidence": f"{finding.evidence}\nREJECTED: {reasons.get(i, '')}".strip()
                    }
                )
            )
    log.info(
        "Adjudication block=%d: %d/%d findings accepted, %d rejected",
        block.number,
        len(accepted),
        len(candidates),
        len(rejected),
    )
    return review.model_copy(
        update={"findings": accepted, "rejected_findings": rejected}
    )


def _record_audit(
    block: BlockInfo, progress: ProgressWriter, audit: TestQualityAudit, verdict: str
) -> None:
    """Write the test-quality auditor's verdict, reason and findings to the
    progress file, and its reason to the log."""
    log.info(
        "Test-quality audit for block %s (%s): %s",
        block.name,
        verdict,
        audit.reason or "(no reason given)",
    )
    progress.log_diagnosis(
        block.name,
        f"Test-quality audit ({verdict}): {audit.reason or '(no reason given)'}\n"
        f"{audit.findings_text()}",
    )


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
        block.name,
        scan.reason,
    )
    try:
        audit = await _audit_test_quality(test_files_content, specs, config)
    except BudgetExceededError:
        raise
    except Exception as e:
        log.error(
            "FAIL block=%d name=%s phase=TEST_QUALITY_AUDIT error=%s",
            block.number,
            block.name,
            e,
        )
        block.resumable = True
        progress.log_agent("TestQualityAuditor", block.name, f"ERROR: {e}")
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    _record_audit(block, progress, audit, "OVERTURNED" if audit.passed else "CONFIRMED")
    if audit.passed:
        log.info(
            "Test-quality auditor overturned the static scan for block %s", block.name
        )
        progress.log_agent(
            "TestQualityAuditor", block.name, "OVERTURNED static scan — tests OK"
        )
        return True

    findings = audit.findings_text() or scan.reason
    log.warning(
        "Test-quality auditor confirmed weak tests for block %s — one test-writer retry",
        block.name,
    )
    progress.log_agent(
        "TestQualityAuditor", block.name, "CONFIRMED weak tests — retrying test writer"
    )
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
        test_command=_block_agent_test_command(config, block),
        model=config.model_for("test_writer"),
        effort=config.effort_for("test_writer"),
        max_turns=config.test_writer_max_turns,
        cwd=str(config.project_dir),
    )
    if not retry.success and not _kept_after_turn_limit(config, block, retry, progress):
        block.timed_out = retry.timed_out
        block.resumable = True
        log.error(
            "FAIL block=%d name=%s phase=TEST_QUALITY_RETRY error=%s",
            block.number,
            block.name,
            retry.error,
        )
        progress.log_agent(
            "TestWriter", block.name, "TIMEOUT" if retry.timed_out else "FAILED"
        )
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
    except BudgetExceededError:
        raise
    except Exception as e:
        log.error(
            "FAIL block=%d name=%s phase=TEST_QUALITY_REAUDIT error=%s",
            block.number,
            block.name,
            e,
        )
        block.resumable = True
        progress.log_agent("TestQualityAuditor", block.name, f"ERROR: {e}")
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
    _record_audit(
        block, progress, re_audit, "OVERTURNED" if re_audit.passed else "STILL WEAK"
    )
    if re_audit.passed:
        return True

    log.error(
        "FAIL block=%d name=%s phase=TEST_QUALITY reason=still-weak-after-retry",
        block.number,
        block.name,
    )
    progress.log_agent(
        "TestQualityAuditor", block.name, "STILL WEAK after retry — block failed"
    )
    _mark_block(state, block_idx, BlockStatus.FAILED, config)
    return False


def _red_run(
    config: BuildConfig, block: BlockInfo, expected: list[str] | None
) -> tuple[GateResult, int | None, str, str, str | None]:
    """Run the block's RED check and judge it with `red_gate`: the gate's
    result, and the exit code, output, command and runner the RED evidence
    is read from.

    With `tdd.narrow_commands`, each directory's part runs on its own and
    `red_gate` judges each part that fails or holds one of the block's test
    files (a part that passes holding none, such as the tests named after a
    changed module, has nothing to prove). The first rejection decides; with
    none, the first judged part is the evidence, and it is "unverified" when
    any judged part is. When no part is judged, the first part's passing run
    is judged and rejected."""
    parts = (
        narrow_parts(
            config.narrow_commands,
            config.project_dir,
            block.baseline_commit,
            enabled=config.narrow_tests,
        )
        if config.narrow_commands
        else None
    )
    if not parts:
        command = _block_test_command(config, block)
        exit_code, output = run_test_suite(
            command,
            config.project_dir,
            config.test_timeout,
            failure_log_path(config.change_dir, f"block-{block.number}-red"),
        )
        runner = runner_of(command, config.project_dir)
        gate = red_gate(
            output,
            exit_code,
            runner=runner,
            expected_tests=expected,
            test_command=command,
            project_dir=config.project_dir,
        )
        return gate, exit_code, output, command, runner
    runs = []
    for part in parts:
        exit_code, output = run_test_suite(
            part.shell,
            config.project_dir,
            config.test_timeout,
            failure_log_path(config.change_dir, f"block-{block.number}-red"),
        )
        prefix = "" if part.dir in ("", ".") else part.dir.rstrip("/") + "/"
        holds = any(t.startswith(prefix) for t in expected or [])
        gate = red_gate(
            output,
            exit_code,
            runner=part.runner,
            expected_tests=expected,
            test_command=part.shell,
            project_dir=config.project_dir,
        )
        log.info(
            "RED part dir=%s exit=%s judged=%s: %s",
            part.dir,
            exit_code,
            exit_code != 0 or holds,
            gate.reason,
        )
        runs.append((part, exit_code, output, gate, exit_code != 0 or holds))
    judged = [r for r in runs if r[4]] or runs[:1]
    rejected = next((r for r in judged if not r[3].passed), None)
    part, exit_code, _, gate, _ = rejected or judged[0]
    if rejected is None and any(
        r[3].metadata.get("classification") == "unverified" for r in judged
    ):
        gate = next(
            r[3] for r in judged if r[3].metadata.get("classification") == "unverified"
        )
    output = "\n".join(f"$ {r[0].shell}\n{r[2]}" for r in runs)
    return gate, exit_code, output, part.shell, part.runner


async def _enforce_red_gate(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    specs: str,
    context: str,
    block_idx: int,
    test_report: str | None = None,
) -> bool:
    """Prove the block's tests fail for the right reason before implementing.

    Runs the suite and applies red_gate. On failure: one test-writer retry
    carrying the gate's reason; a second failure marks the block FAILED.
    On pass: stores trimmed RED output as evidence in block state and the
    progress log. Returns True when RED is confirmed (or unverifiable
    because no test command is configured).

    *test_report* is the test writer's report (None when no test writer ran
    in this process); after a retry, the retry's report replaces it. On a
    pass its `RED_EXPECTED:` slot and the check against the run
    (`red_expected_note`) become `block.red_expected` and the first lines of
    `block.red_evidence`, set together so one checkpoint saves both. It
    changes no verdict.
    """
    if not config.test_command:
        log.info(
            "RED gate skipped for block %s: no test command configured", block.name
        )
        return True

    for attempt in (1, 2):
        # The block's own test files are always in the narrow set, and an
        # empty set falls back to the whole command, so RED cannot pass on a
        # run that selected nothing.
        expected = _list_block_test_files(config, block)
        gate, exit_code, output, command, runner = _red_run(config, block, expected)
        if gate.passed:
            block.red_expected = red_expected_note(
                test_report,
                output,
                exit_code,
                runner=runner,
                expected_tests=expected,
                test_command=command,
                project_dir=config.project_dir,
            )
            block.red_evidence = (
                block.red_expected + "\n" + _trim_evidence(gate.reason + "\n" + output)
            )
            # Freeze the bar the moment it is proven to be a bar. Everything
            # after this point — the implement phase and every review-fix
            # iteration — can write to these files, so this hash map is the
            # only later evidence of whether they did.
            # Missing or empty changed-test evidence cannot establish RED.
            snapshot = _snapshot_test_files(config, block)
            if snapshot is None or not snapshot:
                progress.log_agent(
                    "RedGate", block.name, "FAILED: changed test evidence unavailable"
                )
                _mark_block(state, block_idx, BlockStatus.FAILED, config)
                return False
            block.test_file_hashes = snapshot
            log.info(
                "Test integrity: snapshotted %d test file(s) for block %d",
                len(block.test_file_hashes),
                block.number,
            )
            state.checkpoint(config.state_file_path())
            log.info(
                "RED confirmed block=%d name=%s (%s)",
                block.number,
                block.name,
                gate.reason,
            )
            progress.log_agent("RedGate", block.name, "RED CONFIRMED")
            _tdd_event(config, state, block, "RED", "DONE")
            progress.log_evidence("RED", block.name, block.red_evidence)
            return True

        if attempt == 1:
            log.warning(
                "RED gate failed for block %s (action=%s): %s — one test-writer retry",
                block.name,
                gate.action,
                gate.reason,
            )
            progress.log_agent(
                "RedGate", block.name, f"FAILED ({gate.action}) — retrying test writer"
            )
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
                test_command=_block_agent_test_command(config, block),
                model=config.model_for("test_writer"),
                effort=config.effort_for("test_writer"),
                max_turns=config.test_writer_max_turns,
                cwd=str(config.project_dir),
            )
            if not retry.success and not _kept_after_turn_limit(
                config, block, retry, progress
            ):
                block.timed_out = retry.timed_out
                block.resumable = True
                log.error(
                    "FAIL block=%d name=%s phase=RED_GATE_RETRY error=%s",
                    block.number,
                    block.name,
                    retry.error,
                )
                progress.log_agent(
                    "TestWriter", block.name, "TIMEOUT" if retry.timed_out else "FAILED"
                )
                _mark_block(state, block_idx, BlockStatus.FAILED, config)
                return False
            test_report = retry.output
            sha = _git_commit_block_phase(config, "test", block)
            if sha:
                block.test_commit = sha
                state.checkpoint(config.state_file_path())

    log.error(
        "FAIL block=%d name=%s phase=RED_GATE reason=%s",
        block.number,
        block.name,
        gate.reason,
    )
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
        output,
        block_number=block.number,
        block_name=block.name,
        role=role,
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
            block.number,
            role,
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
                block.number,
                role,
                note.spec_impact,
            )
            progress.log_spec_impact(block.name, role, note.spec_impact, note.surprises)
        else:
            log.warning(
                "SPEC_IMPACT: contradicts reported by %s on block %d (%s): %s",
                role,
                block.number,
                block.name,
                note.surprises,
            )
            progress.log_spec_impact(block.name, role, note.spec_impact, note.surprises)

    if not note.contradicts:
        return True

    if not config.gates.respec_halt_on_contradiction:
        return True

    # The requirements go back to pm-spec: the issue is written to
    # spec-issues.md and `.change.yaml` `spec_issues` set, and the build
    # stops with SPEC:ISSUES (exit 4) once the engine returns (row S12).
    from .readiness import pm_spec_change, write_issues

    satisfies = ", ".join(block.satisfies) or "(no Satisfies line)"
    try:
        issues = write_issues(
            config.change_dir,
            [
                (
                    f"Block {block.number} ({block.name}): {role} SPEC_IMPACT: contradicts",
                    f"Satisfies: {satisfies}\n\n{note.surprises.strip()}\n\n"
                    f"Raised by buildme's TDD build, under respec.halt_on_contradiction.",
                )
            ],
            change=config.change_name,
        )
    except OSError as e:
        log.warning("Could not write the spec issue for block %d: %s", block.number, e)
        issues = None
    diagnosis = (
        f"Build stopped: the {role} reported SPEC_IMPACT: contradicts on block "
        f"{block.number} ({block.name}) and respec.halt_on_contradiction is on.\n"
        f"The block could only be built by doing something the spec/design does "
        f"not say (or says otherwise), so the spec is decided by pm-spec "
        f"rather than steered around in code.\n\n"
        f"Reported surprise:\n{note.surprises}\n\n"
        f"Next: `/multiplai-pm:pm-spec revise {pm_spec_change(config.change_name)}` reads "
        f"{issues or 'spec-issues.md'}, or set "
        f"`respec: {{halt_on_contradiction: false}}` in specs/config.yaml to let "
        f"the build continue and collect the delta into respec.md instead."
    )
    log.error(
        "FAIL block=%d name=%s reason=spec_contradiction role=%s",
        block.number,
        block.name,
        role,
    )
    progress.log_diagnosis(block.name, diagnosis)
    return False


def _record_handoff_note(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    role: str,
    round_: int,
    output: str,
) -> None:
    """Save an agent's `NOTES_FOR_LATER_BLOCKS:` section on the block and
    checkpoint (`carry_block_notes` only). One note per (role, round): a re-run
    of the same agent replaces its note, so a resume never doubles it. A note
    over `HANDOFF_NOTE_MAX_CHARS` keeps its end. A missing section is logged,
    never fatal."""
    if not config.carry_block_notes:
        return
    text = parse_handoff_note(output)
    if text is None:
        log.info(
            "No notes for later blocks block=%d role=%s round=%d",
            block.number,
            role,
            round_,
        )
        return
    if len(text) > HANDOFF_NOTE_MAX_CHARS:
        log.info(
            "Handoff note cut block=%d role=%s from %d to %d characters",
            block.number,
            role,
            len(text),
            HANDOFF_NOTE_MAX_CHARS,
        )
        text = text[-HANDOFF_NOTE_MAX_CHARS:]
    note = HandoffNote(role=role, round=round_, text=text)
    keys = [(n.role, n.round) for n in block.handoff_notes]
    if (role, round_) in keys:
        block.handoff_notes[keys.index((role, round_))] = note
    else:
        block.handoff_notes.append(note)
    state.checkpoint(config.state_file_path())


def _impl_done(config: BuildConfig, block: BlockInfo) -> bool:
    """An earlier run of *block* committed its tests and its implementation,
    both on HEAD. The refactor runs when HEAD is the implementation commit;
    a later commit means the block went past it (`run_block_tdd`)."""
    return bool(
        block.test_commit
        and block.impl_commit
        and git_ops.is_ancestor_of_head(config.project_dir, block.test_commit)
        and git_ops.is_ancestor_of_head(config.project_dir, block.impl_commit)
    )


def _tests_done(config: BuildConfig, block: BlockInfo) -> str | None:
    """How much of Phase A an earlier run of *block* finished: "red" when its
    test commit is on HEAD, the RED gate passed and the test files are as the
    gate left them; "committed" when the test commit is on HEAD but the RED
    gate has not passed on these files; None otherwise."""
    if not block.test_commit or not git_ops.is_ancestor_of_head(
        config.project_dir, block.test_commit
    ):
        return None
    if (
        block.red_evidence
        and _snapshot_test_files(config, block) == block.test_file_hashes
    ):
        return "red"
    return "committed"


async def _write_block_tests(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    specs: str,
    context: str,
    block_idx: int,
    cwd: str,
) -> tuple[bool, str | None]:
    """Phase A: run the test writer and commit its tests. Returns whether the
    block goes on, and the agent's report (None when it ran out of turns and
    left none)."""
    # Test files an earlier attempt left uncommitted are the agent's own
    # drafts: say so, or it re-reads the code and audits them from scratch.
    description = block.description
    drafts = _uncommitted_test_files(config)
    if drafts:
        log.info("block=%d phase=TEST_WRITE drafts=%d", block.number, len(drafts))
        description += "\n\n" + _drafts_section(config, drafts)

    progress.log_agent("TestWriter", block.name, "STARTED")
    test_result = await run_test_writer(
        block_name=block.name,
        block_description=description,
        specs=specs,
        context_bundle=context,
        test_command=_block_agent_test_command(config, block),
        model=config.model_for("test_writer"),
        effort=config.effort_for("test_writer"),
        max_turns=config.test_writer_max_turns,
        cwd=cwd,
    )

    # An agent stopped at its turn cap may have finished its tests: the files
    # on disk go to the quality and RED gates, which decide.
    kept = _kept_after_turn_limit(config, block, test_result, progress)
    if not test_result.success and not kept:
        # agent_call degrades a timeout to a failed result (timed_out=True)
        # rather than raising — propagate that so the orchestrator returns
        # EXIT_AGENT_TIMEOUT instead of a generic build failure.
        block.timed_out = test_result.timed_out
        block.resumable = True
        reason = "timeout" if test_result.timed_out else "error"
        log.error(
            "FAIL block=%d name=%s phase=TEST_WRITE reason=%s error=%s",
            block.number,
            block.name,
            reason,
            test_result.error,
        )
        progress.log_agent(
            "TestWriter", block.name, "TIMEOUT" if test_result.timed_out else "FAILED"
        )
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False, None

    if kept:
        report = (
            "SURPRISES: The test writer ran out of turns and did not report; its test files "
            f"({', '.join(kept)}) went to the quality and RED gates.\nSPEC_IMPACT: none"
        )
    else:
        # The agent's own STATUS slot: NEEDS_CONTEXT/BLOCKED is it saying the work
        # was not doable as specified — stop the block, surface its reason.
        status = agent_status_gate(test_result.output, "TestWriter")
        if not status.passed:
            log.error(
                "FAIL block=%d name=%s phase=TEST_WRITE reason=agent_status status=%s",
                block.number,
                block.name,
                status.metadata.get("status"),
            )
            progress.log_agent(
                "TestWriter", block.name, str(status.metadata.get("status"))
            )
            progress.log_diagnosis(block.name, status.reason)
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False, None

        log.info("DONE block=%d name=%s phase=TEST_WRITE", block.number, block.name)
        progress.log_agent("TestWriter", block.name, "COMPLETE")
        report = test_result.output

    if not _record_implementation_note(
        block, config, state, progress, "test_writer", report
    ):
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False, None

    test_sha = _git_commit_block_phase(config, "test", block)
    if test_sha:
        block.test_commit = test_sha
        state.checkpoint(config.state_file_path())

    return True, None if kept else test_result.output


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
    # Why the last attempt stopped says nothing about this one: a block that
    # timed out and now fails its RED gate is a build failure, not a timeout.
    block.timed_out = False
    block.resumable = False

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
            block.number,
            block.status.value,
        )

    # A block resumed after its implementation was committed (G33) starts at
    # the first step with no commit: the test writer, the quality and RED
    # gates and the implementer are not run again.
    if _impl_done(config, block):
        head = git_ops.rev_parse_head(config.project_dir)
        refactor = config.refactor_phase and head == block.impl_commit
        log.info(
            "SKIP block=%d phase=TEST_WRITE,IMPLEMENT reason=impl-committed next=%s",
            block.number,
            "REFACTOR" if refactor else "REVIEW",
        )
        progress.log_agent(
            "Implementer",
            block.name,
            f"SKIPPED — tests and implementation committed at "
            f"{block.test_commit[:8]} and {block.impl_commit[:8]}",
        )
        if not refactor:
            return True
        _mark_block(state, block_idx, BlockStatus.IMPLEMENTING, config)
        return await _refactor_block(
            block, config, state, progress, block_idx, block.impl_commit
        )

    # --- Phase A: Write tests ---
    if block.status == BlockStatus.PENDING:
        _mark_block(state, block_idx, BlockStatus.TESTING, config)
        progress.log_block(block.number, total, block.name, "TESTING")

    log.info(
        "START block=%d/%d name=%s phase=TEST_WRITE", block.number, total, block.name
    )
    _BLOCK_STARTED[(config.change_name, block.number)] = time.monotonic()
    _tdd_event(config, state, block, "TEST_WRITE", "START")

    specs = ""
    for cap, text in requirement_texts(config.change_dir).items():
        specs += f"\n### {cap}.md\n{text}"

    # A resumed block whose tests an earlier run committed does not write them
    # again: "red" skips to the implementer, "committed" re-runs the gates.
    done = _tests_done(config, block)
    context = (
        ""
        if done == "red"
        else assemble_context(
            block, config, "test_writer", blocks=state.tdd.blocks if state.tdd else None
        )
    )
    if done:
        log.info(
            "SKIP block=%d phase=TEST_WRITE reason=tests-committed%s",
            block.number,
            "" if done == "red" else " gates=rerun",
        )
        progress.log_agent(
            "TestWriter",
            block.name,
            f"SKIPPED — tests committed at {block.test_commit[:8]}"
            + ("" if done == "red" else "; re-running the quality and RED gates"),
        )
        test_report = None
    else:
        ok, test_report = await _write_block_tests(
            block, config, state, progress, specs, context, block_idx, cwd
        )
        if not ok:
            return False

    if done != "red":
        # --- Phase A.5: Test quality enforcement (static scan + LLM adjudication) ---
        # Scan the test source the agent actually wrote — its report is prose and
        # contains no test functions. Fall back to the report only when git can't
        # tell us what changed (non-git project).
        test_source = _read_block_test_files(config, block) or test_report or ""
        quality_ok = await _enforce_test_quality(
            block,
            config,
            state,
            progress,
            specs,
            context,
            block_idx,
            test_source,
        )
        if not quality_ok:
            return False

        # --- Phase A.6: RED gate — prove tests fail before implementing ---
        red_ok = await _enforce_red_gate(
            block,
            config,
            state,
            progress,
            specs,
            context,
            block_idx,
            test_report=test_report,
        )
        if not red_ok:
            return False
    failing_tests = test_report if test_report is not None else _unreported_tests(block)

    # --- Phase B: Implement ---
    log.info("START block=%d name=%s phase=IMPLEMENT", block.number, block.name)
    _tdd_event(config, state, block, "IMPLEMENT", "START")
    _mark_block(state, block_idx, BlockStatus.IMPLEMENTING, config)
    progress.log_block(block.number, total, block.name, "IMPLEMENTING")

    impl_context = assemble_context(
        block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None
    )
    progress.log_agent("Implementer", block.name, "STARTED")
    impl_result = await run_implementer(
        block_name=block.name,
        block_description=block.description,
        failing_tests=failing_tests,
        context_bundle=impl_context,
        test_command=_block_agent_test_command(config, block),
        prompt_style=config.implementer_prompt_style,
        model=config.model_for("implementer"),
        effort=config.effort_for("implementer"),
        cwd=cwd,
        carry_notes=config.carry_block_notes,
    )

    if not impl_result.success:
        block.timed_out = impl_result.timed_out
        block.resumable = True
        reason = "timeout" if impl_result.timed_out else "error"
        log.error(
            "FAIL block=%d name=%s phase=IMPLEMENT reason=%s error=%s",
            block.number,
            block.name,
            reason,
            impl_result.error,
        )
        progress.log_agent(
            "Implementer", block.name, "TIMEOUT" if impl_result.timed_out else "FAILED"
        )
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    impl_status = agent_status_gate(impl_result.output, "Implementer")
    if not impl_status.passed:
        log.error(
            "FAIL block=%d name=%s phase=IMPLEMENT reason=agent_status status=%s",
            block.number,
            block.name,
            impl_status.metadata.get("status"),
        )
        progress.log_agent(
            "Implementer", block.name, str(impl_status.metadata.get("status"))
        )
        progress.log_diagnosis(block.name, impl_status.reason)
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False

    log.info(
        "DONE block=%d name=%s phase=IMPLEMENT turns=%d elapsed=%.0fs",
        block.number,
        block.name,
        impl_result.turns_used,
        impl_result.elapsed_seconds,
    )
    progress.log_agent("Implementer", block.name, "COMPLETE")
    _tdd_event(config, state, block, "IMPLEMENT", "DONE")

    # Kept in state so the reviewer sees the implementer's own account as a
    # claim to verify, and so the test-integrity gate can find a declared
    # TEST CHANGE REQUIRED reason after a resume.
    block.implementer_report = _trim_evidence(impl_result.output)
    state.checkpoint(config.state_file_path())

    if not _record_implementation_note(
        block,
        config,
        state,
        progress,
        "implementer",
        impl_result.output,
    ):
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
    _record_handoff_note(block, config, state, "implementer", 0, impl_result.output)

    impl_sha = _git_commit_block_phase(config, "impl", block)
    if impl_sha:
        block.impl_commit = impl_sha
        state.checkpoint(config.state_file_path())

    # --- Phase C: Refactor (every tier) ---
    if config.refactor_phase:
        return await _refactor_block(
            block, config, state, progress, block_idx, impl_sha
        )
    return True


async def _refactor_block(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    block_idx: int,
    impl_sha: str | None,
) -> bool:
    """Phase C of `run_block_tdd`: the refactor, verified, its diff discarded
    on failure. False when the block fails."""
    cwd = str(config.project_dir)
    log.info("START block=%d name=%s phase=REFACTOR", block.number, block.name)
    _tdd_event(config, state, block, "REFACTOR", "START")
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

    refactor_context = assemble_context(
        block, config, "refactorer", blocks=state.tdd.blocks if state.tdd else None
    )
    progress.log_agent("Refactorer", block.name, "STARTED")
    refactor_result = await run_refactorer(
        block_name=block.name,
        block_description=block.description,
        context_bundle=refactor_context,
        test_command=_block_agent_test_command(config, block),
        model=config.model_for("refactor"),
        effort=config.effort_for("refactor"),
        cwd=cwd,
    )
    # Refactor is non-fatal: a failed or timed-out refactor leaves the
    # passing implementation intact, so log and move on.
    if not refactor_result.success:
        reason = "timeout" if refactor_result.timed_out else refactor_result.error
        log.warning(
            "FAIL block=%d name=%s phase=REFACTOR reason=%s (non-fatal)",
            block.number,
            block.name,
            reason,
        )
        progress.log_agent(
            "Refactorer",
            block.name,
            "TIMEOUT" if refactor_result.timed_out else "FAILED",
        )
        _tdd_event(
            config,
            state,
            block,
            "REFACTOR",
            "FAIL",
            "reason=timeout" if refactor_result.timed_out else "reason=error",
        )
        if not rewind_to or not git_ops.discard_to(
            config.project_dir, rewind_to, "failed block refactor"
        ):
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
        if not _block_tests_gate(config, block).passed:
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
    elif not _verify_block_refactor(
        block, config, state, progress, rewind_to, pre_refactor_hashes
    ):
        if (
            not rewind_to
            or git_ops.rev_parse_head(config.project_dir) != rewind_to
            or not git_ops.tree_is_clean(config.project_dir)
            or not _block_tests_gate(config, block).passed
        ):
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
    else:
        log.info("DONE block=%d name=%s phase=REFACTOR", block.number, block.name)
        progress.log_agent("Refactorer", block.name, "COMPLETE")
        _tdd_event(config, state, block, "REFACTOR", "DONE")
        if not _record_implementation_note(
            block,
            config,
            state,
            progress,
            "refactorer",
            refactor_result.output,
        ):
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
        refactor_sha = _git_commit_block_phase(config, "refactor", block)
        if refactor_sha:
            block.refactor_commit = refactor_sha
            state.checkpoint(config.state_file_path())

    return True


def _block_tests_gate(
    config: BuildConfig, block: BlockInfo, failure_log: Path | None = None
) -> GateResult:
    """`integration_gate` on the block's own tests (`_block_test_command`),
    which is the whole `test_command` when it cannot be narrowed. Without
    `tdd.parallel`: that rewrite is for the whole suite."""
    return integration_gate(
        _block_test_command(config, block),
        config.project_dir,
        timeout=config.test_timeout,
        failure_log=failure_log,
    )


# Blocks whose narrowed gate fell back to the whole command under
# `tdd.whole_suite: end`, warned about once each in this process.
_WHOLE_FALLBACK_WARNED: set[tuple[str, int]] = set()


def _defers_whole_suite(config: BuildConfig) -> bool:
    return config.whole_suite == "end"


def _block_gate_detail(config: BuildConfig, block: BlockInfo) -> str:
    """The `TDD:` detail for a block gate's start: nothing under `per_block`,
    `block-tests` under `end`, and `whole-suite (could not narrow)` when the
    block's command falls back to the whole one."""
    if not _defers_whole_suite(config):
        return ""
    if _block_test_command(config, block) == config.test_command:
        return "whole-suite (could not narrow)"
    return "block-tests"


def _block_gate(config: BuildConfig, block: BlockInfo, label: str) -> GateResult:
    """The test run that decides a block step's work is green. Under
    `tdd.whole_suite: per_block` it is the whole `test_command`; under `end`
    it is the block's own tests (`_block_tests_gate`), and the whole command
    runs once after the last block (`_run_end_suite`). A failed run writes
    its output to `.buildme-test-runs/<stamp>-block-N-<label>.log`."""
    failure_log = failure_log_path(config.change_dir, f"block-{block.number}-{label}")
    if _defers_whole_suite(config):
        if _block_test_command(config, block) != config.test_command:
            return _block_tests_gate(config, block, failure_log)
        key = (config.change_name, block.number)
        if key not in _WHOLE_FALLBACK_WARNED:
            _WHOLE_FALLBACK_WARNED.add(key)
            log.warning(
                "tdd.whole_suite is end but block %d (%s) could not be narrowed — "
                "its gates run the whole test command",
                block.number,
                block.name,
            )
    return integration_gate(
        config.test_command,
        config.project_dir,
        parallel=config.parallel_tests,
        timeout=config.test_timeout,
        failure_log=failure_log,
    )


def _output_note(gate: GateResult) -> str:
    """` (output: <path>)` when the gate saved a failed run's output."""
    path = gate.metadata.get("output_file")
    return f" (output: {path})" if path else ""


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

    1. The block's own tests are re-run (`_block_tests_gate`). The whole
       suite is not: `integration_gate` runs it on this same tree a moment
       later, and throws the refactor away first when it is red
       (`_discard_refactor_for_gate`), so a second whole run here would
       catch nothing that one does not.
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
    gate = _block_tests_gate(config, block)
    detail = "" if gate.passed else f"tests failed after refactor: {gate.reason}"

    if gate.passed:
        after = _snapshot_test_files(config, block)
        if before is None or after is None:
            # Same reading as everywhere else: an unavailable snapshot means
            # "not checked", never "every test file was deleted".
            log.warning(
                "Test integrity could not run after refactor for block %d: "
                "test file list unavailable",
                block.number,
            )
            progress.log_agent("TestIntegrity", block.name, "NOT CHECKED (refactor)")
            detail = "test integrity evidence unavailable"
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
            block.number,
            block.name,
            detail,
        )
        progress.log_agent("Refactorer", block.name, "REVERT FAILED — see log")
        return False

    reverted = git_ops.discard_to(
        config.project_dir,
        rewind_to,
        f"refactor of block {block.number}",
    )
    log.warning(
        "Refactor of block %d (%s) %s: %s",
        block.number,
        block.name,
        "discarded" if reverted else "failed verification but could not be discarded",
        detail,
    )
    progress.log_agent(
        "Refactorer",
        block.name,
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
    report: str | Sequence[str],
) -> bool:
    """Apply `unchanged_tests_gate` at one of the two writable windows.

    *window* is "implement" or "review-fix N" — both are checked, because the
    review-fix agent IS the implementer, so passing the first check only means
    the tests survived until the first review, not until the block is done.

    *report* is the output of the agents that wrote during THIS window only
    (in the implement window, a list: the implementer's report and each
    integration fixer's, parsed one by one so a bare marker at the end of one
    cannot take the next one's first line as its reason), not the accumulated
    `block.implementer_report`. Scanning the accumulation would
    let one `TEST CHANGE REQUIRED:` declared during implement silently
    authorize every later review-fix mutation for the rest of the block — the
    exact hole the per-window re-baseline exists to close.

    A flagged (declared) change passes but is recorded on the block, so
    `_run_quality_review` can hand it to the reviewer as an unverified claim.
    """
    after = _snapshot_test_files(config, block)
    if after is None:
        progress.log_agent(
            "TestIntegrity", block.name, f"FAILED: evidence unavailable ({window})"
        )
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
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
        log.warning(
            "Test files changed during %s for block %d with a declared reason: %s",
            window,
            block.number,
            gate.reason,
        )
        progress.log_agent(
            "TestIntegrity",
            block.name,
            f"FLAGGED ({window}) — declared change, sent to reviewer",
        )
        return True
    if gate.passed:
        log.debug(
            "Test integrity OK after %s for block %d (%s)",
            window,
            block.number,
            gate.reason,
        )
        return True

    log.error(
        "FAIL block=%d name=%s phase=TEST_INTEGRITY window=%s reason=%s",
        block.number,
        block.name,
        window,
        gate.reason,
    )
    progress.log_agent("TestIntegrity", block.name, f"FAILED ({window})")
    progress.log_diagnosis(block.name, gate.reason)
    state.mark_block_status(block_idx, BlockStatus.FAILED, config.state_file_path())
    return False


def _discard_refactor_for_gate(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
) -> bool:
    """Throw the block's refactor commit away after a red integration gate.
    True when it was discarded and the gate should run again: the block has
    a refactor commit on top of its impl commit, HEAD is still the refactor
    commit and the tree is clean, so nothing but the refactor is dropped."""
    if (
        not block.refactor_commit
        or not block.impl_commit
        or git_ops.rev_parse_head(config.project_dir) != block.refactor_commit
        or not git_ops.tree_is_clean(config.project_dir)
    ):
        return False
    if not git_ops.discard_to(
        config.project_dir, block.impl_commit, f"refactor of block {block.number}"
    ):
        return False
    log.warning(
        "Refactor of block %d (%s) discarded: integration gate red after it",
        block.number,
        block.name,
    )
    suite = "block tests" if _defers_whole_suite(config) else "whole suite"
    progress.log_agent(
        "Refactorer", block.name, f"REVERTED — {suite} red after refactor"
    )
    block.refactor_commit = None
    state.checkpoint(config.state_file_path())
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

    # Every integration fixer writes inside the implement window, so its report
    # is where its TEST CHANGE REQUIRED declaration would appear. Kept whole
    # and apart from `block.implementer_report`, whose trim to the last
    # MAX_EVIDENCE_CHARS could otherwise cut the implementer's own declaration.
    fix_reports: list[str] = []

    # --- Integration gate ---
    log.info("START block=%d name=%s phase=INTEGRATION_GATE", block.number, block.name)
    _tdd_event(
        config,
        state,
        block,
        "INTEGRATION_GATE",
        "START",
        _block_gate_detail(config, block),
    )
    gate = _block_gate(config, block, "integration")
    # Under per_block the refactor check ran only the block's tests, so this
    # is the first whole run since the refactor; under `tdd.whole_suite: end`
    # it runs the block tests again. Either way a red gate throws the refactor
    # away and runs again before any fix agent is spent on it.
    if not gate.passed and _discard_refactor_for_gate(block, config, state, progress):
        gate = _block_gate(config, block, "integration")
    _tdd_event(
        config,
        state,
        block,
        "INTEGRATION_GATE",
        "DONE" if gate.passed else "FAIL",
        "" if gate.passed else "fixing",
    )
    if not gate.passed:
        log.warning(
            "Integration gate failed after block %s: %s%s",
            block.name,
            gate.reason,
            _output_note(gate),
        )
        # Circuit breaker: scoped fixes first, then one architecture-questioning
        # escalation, then FAILED with a diagnosis — never an endless fix loop.
        last_fix_output = ""
        fix_completed = False
        for attempt in range(MAX_INTEGRATION_FIX_ATTEMPTS):
            escalated = attempt == MAX_INTEGRATION_FIX_ATTEMPTS - 1
            fix_step = "integration_fix_escalated" if escalated else "integration_fix"
            fix_model = config.model_for(fix_step)
            log.info(
                "Integration fix attempt %d/%d%s",
                attempt + 1,
                MAX_INTEGRATION_FIX_ATTEMPTS,
                " (escalated)" if escalated else "",
            )
            if escalated:
                progress.log_agent(
                    "IntegrationFixer",
                    block.name,
                    "ESCALATED — questioning the architecture",
                )
            context = assemble_context(
                block,
                config,
                "implementer",
                blocks=state.tdd.blocks if state.tdd else None,
            )
            fix_result = await run_integration_fix(
                failure_output=gate.metadata.get("output", gate.reason),
                failing_tests=failing_tests(gate.metadata.get("output", "")),
                test_command=_block_agent_test_command(config, block),
                context_bundle=context,
                escalate=escalated,
                model=fix_model,
                effort=config.effort_for(fix_step),
                cwd=str(config.project_dir),
            )
            if fix_result.output:
                fix_reports.append(fix_result.output)
            # A timed-out or failed fix leaves success=False; retry until attempts run out.
            if fix_result.success:
                fix_completed = True
                last_fix_output = fix_result.output
                gate = _block_gate(config, block, f"integration-fix-{attempt + 1}")
                if gate.passed:
                    break

        if not gate.passed:
            log.error(
                "Integration gate still failing after %d fix attempts for block %s",
                MAX_INTEGRATION_FIX_ATTEMPTS,
                block.name,
            )
            diagnosis = (
                f"Integration gate exhausted after {MAX_INTEGRATION_FIX_ATTEMPTS} fix "
                f"attempts (final attempt escalated).\n"
                f"Gate: {gate.reason}{_output_note(gate)}\n"
                f"Last fix agent report:\n{last_fix_output or '(no fix agent completed)'}"
            )
            progress.log_diagnosis(block.name, diagnosis)
            # No fix agent completed: the failure was never worked on.
            block.resumable = not fix_completed
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False

    # --- Test-integrity gate (window 1: the implement phase) ---
    # Checked BEFORE GREEN evidence is accepted. A green suite proves nothing
    # if the suite is no longer the one that went red.
    if not _enforce_test_integrity(
        block,
        config,
        state,
        progress,
        block_idx,
        "implement",
        [block.implementer_report, *fix_reports],
    ):
        return False

    # GREEN evidence: the suite passing with the implementation in place —
    # the counterpart to the RED evidence captured before implementation.
    block.green_evidence = _trim_evidence(
        gate.metadata.get("output", "") or gate.reason
    )
    state.checkpoint(config.state_file_path())
    progress.log_evidence("GREEN", block.name, block.green_evidence)

    # --- Quality review loop ---
    _mark_block(state, block_idx, BlockStatus.REVIEWING, config)
    progress.log_block(block.number, total, block.name, "REVIEWING")

    review_passed = False
    fixes_run = fixes_completed = 0
    round_path: Path | None = None
    recurring: str | None = None
    for iteration in range(MAX_REVIEW_ITERATIONS):
        iter_gate = review_iteration_gate(iteration, MAX_REVIEW_ITERATIONS)
        if not iter_gate.passed:
            log.warning("Review loop exhausted for block %s", block.name)
            break

        # A runaway review/fix loop is the classic budget sink: each iteration
        # is a full panel review PLUS a full implementer run. Check before
        # spending, so the stop lands on a boundary rather than mid-call.
        try:
            budget_mod.check(
                phase=f"review iteration {iteration + 1} of block {block.number}"
            )
        except BudgetExceededError as e:
            log.error(
                "FAIL block=%d name=%s phase=REVIEW reason=budget_exhausted",
                block.number,
                block.name,
            )
            progress.log_diagnosis(block.name, f"{e}\n{e.diagnosis}")
            block.resumable = True
            state.mark_block_status(
                block_idx, BlockStatus.FAILED, config.state_file_path()
            )
            return False

        # What the block's earlier review rounds left open, read from the
        # round records on disk (so a resumed loop picks it up too). Round 1
        # has no earlier record and runs exactly as it always has.
        open_, earlier_rejected, last = review_rounds.open_state(
            config.change_dir, block.number
        )

        # Run review (via llm_call_structured). Propagates SDK failures —
        # no silent fallback to fabricated passing scores.
        try:
            review = await _run_quality_review(
                block, config, **({"open_findings": open_} if open_ else {})
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            log.error(
                "FAIL block=%d name=%s phase=REVIEW iteration=%d error=%s",
                block.number,
                block.name,
                iteration + 1,
                e,
            )
            progress.log_review(block.name, iteration + 1, 0.0, False)
            block.resumable = True
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False

        # Reviewers propose; the orchestrator disposes. THE core invariant:
        # no reviewer suggestion may reach a fix agent unadjudicated.
        memory = (
            None
            if last is None
            else review_rounds.RoundMemory(
                open=open_,
                rejected=earlier_rejected,
                scope_diff=_scope_diff(config, last),
            )
        )
        review = await _adjudicate_review_findings(
            review,
            block,
            config,
            _capture_block_diff(config, block),
            build_context=_global_constraints_text(config),
            memory=memory,
        )
        # From round 2: restatements of open findings merge into them. New
        # findings outside the last fix's reach are labelled in the round
        # record and fixed like the rest. The fix agent, and the fix on pass,
        # get the open and new findings; every one of them was accepted by
        # the adjudicator.
        plan = (
            None
            if memory is None
            else review_rounds.plan_round(
                review.findings, open_, review.prior_assessments
            )
        )
        block.review_scores = (
            review
            if plan is None
            else review.model_copy(update={"findings": plan.to_fix})
        )
        block.review_iterations = iteration + 1

        score_gate = review_score_gate(review, config.review_gate)
        progress.log_review(
            block.name, iteration + 1, review.weighted_average, score_gate.passed
        )
        round_path = _record_review_round(config, block, review, score_gate, plan=plan)
        _tdd_event(
            config,
            state,
            block,
            "REVIEW",
            "DONE" if score_gate.passed else "FAIL",
            f"round={iteration + 1} findings={len(review.findings)}",
        )

        if score_gate.passed:
            review_passed = True
            break

        log.info(
            "Review iteration %d failed for block %s: %s",
            iteration + 1,
            block.name,
            score_gate.reason,
        )
        if iteration + 1 >= MAX_REVIEW_ITERATIONS:
            break  # no fix agent after the last review — its work would go unreviewed
        # Spawn fix agent for the failing dimensions PLUS the findings that
        # survived adjudication. Rejected findings are absent by construction.
        fix_brief = score_gate.reason
        if plan is None:
            new_findings = review.findings
        else:
            new_findings = [review.findings[i] for i in plan.new_open]
            open_text = review_rounds.open_findings_text(plan)
            if open_text:
                fix_brief += f"\n\n## Findings still open from earlier rounds (fix these)\n{open_text}"
        accepted_text = ReviewResult(scores=[], findings=new_findings).findings_text()
        if accepted_text:
            fix_brief += (
                f"\n\n## Adjudicated findings (accepted — fix these)\n{accepted_text}"
            )
        if any(f.other_locations for f in block.review_scores.findings):
            fix_brief += "\n\n" + REVIEW_FIX_LOCATIONS
        # The last fix (its review is the loop's last) questions the approach,
        # on the review model, with every finding of every round in front of
        # it — as the integration gate's last fix does. Counted per process.
        escalated = iteration == MAX_REVIEW_ITERATIONS - 2
        fix_step = "review_fix_escalated" if escalated else "review_fix"
        fix_model = config.model_for(fix_step)
        description = _review_fix_description(fix_brief)
        if escalated:
            progress.log_agent(
                "ReviewFix", block.name, "ESCALATED — questioning the approach"
            )
            description = (
                REVIEW_FIX_ESCALATION.format(
                    findings=review_rounds.history_text(config.change_dir, block.number)
                )
                + "\n"
                + description
            )
        review_rounds.update_round(round_path, fix_brief=description)
        context = assemble_context(
            block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None
        )
        fix = await run_implementer(
            block_name=block.name,
            block_description=description,
            failing_tests=fix_brief,
            context_bundle=context,
            test_command=_block_agent_test_command(config, block),
            prompt_style=config.implementer_prompt_style,
            model=fix_model,
            effort=config.effort_for(fix_step),
            cwd=str(config.project_dir),
            carry_notes=config.carry_block_notes,
        )
        if escalated:
            recurring = parse_recurring(fix.output or "")
            _record_fix(round_path, block, fix, escalated=True, recurring=recurring)
        else:
            _record_fix(round_path, block, fix)
        fixes_run += 1
        fixes_completed += bool(fix.success)
        if not fix.success:
            block.resumable = True
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
        gate = _block_gate(config, block, f"review-fix-{iteration + 1}")
        if not gate.passed:
            progress.log_diagnosis(
                block.name,
                gate.metadata.get("output", gate.reason) + _output_note(gate),
            )
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
        if fix.output:
            # The fix agent is an implementer too — its report is where a
            # TEST CHANGE REQUIRED declaration for this window would appear.
            block.implementer_report = _trim_evidence(
                f"{block.implementer_report}\n\n[review-fix {iteration + 1}]\n{fix.output}"
            )
            _record_handoff_note(
                block, config, state, "review-fix", iteration + 1, fix.output
            )

        # --- Test-integrity gate (window 2: this review-fix iteration) ---
        # Checked every iteration, not just once: the fix agent has the same
        # unrestricted write access as the original implementer, and it runs
        # AFTER the tests were quality-audited.
        # Only THIS iteration's fix output counts as a declaration — see
        # `_enforce_test_integrity`'s *report* contract.
        if not _enforce_test_integrity(
            block,
            config,
            state,
            progress,
            block_idx,
            f"review-fix {iteration + 1}",
            fix.output or "",
        ):
            return False
        # Committed per fix, so the round record can name the fix's commit
        # and the next round's record its own head.
        review_rounds.amend_fix(
            round_path, commit=_git_commit_block_phase(config, "fix", block)
        )

    if not review_passed:
        if config.lenient_review:
            # Accept-and-continue (pre-0.4 behavior), explicitly opted into.
            log.warning(
                "Review exhausted for block %s after %d iterations — accepting "
                "(--lenient-review)",
                block.name,
                MAX_REVIEW_ITERATIONS,
            )
            progress.log_block(
                block.number,
                total,
                block.name,
                "COMPLETE (review below threshold, --lenient-review)",
            )
        else:
            log.error(
                "FAIL block=%d name=%s phase=REVIEW reason=review-exhausted "
                "after %d iterations",
                block.number,
                block.name,
                MAX_REVIEW_ITERATIONS,
            )
            progress.log_block(
                block.number, total, block.name, "FAILED — review exhausted"
            )
            progress.log_diagnosis(
                block.name, _review_exhausted_diagnosis(config, block, recurring)
            )
            print(f"BLOCK:{block.number}/{total}:{block.name}:REVIEW_EXHAUSTED")
            # Every review fix errored: the findings were never worked on.
            block.resumable = fixes_run > 0 and fixes_completed == 0
            _mark_block(state, block_idx, BlockStatus.FAILED, config)
            return False
    elif config.fix_on_pass:
        if not await _fix_on_pass(
            block, config, state, progress, block_idx, round_path
        ):
            return False

    return _finish_block(block, config, state, progress, block_idx, total)


def _finish_block(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    block_idx: int,
    total: int,
) -> bool:
    """Commit the block's last edits and mark it DONE."""
    # The review-fix agents' edits are part of this block. Committed here, a
    # later block's discard back to its own start commit cannot drop them,
    # and its first commit does not carry them.
    _git_commit_block_phase(config, "fix", block)
    block.fix_pass = None
    if _defers_whole_suite(config) and state.tdd is not None:
        # Saved by `_mark_block`'s checkpoint: a block gated on its own tests
        # leaves the whole run owed, whatever the setting is on resume.
        state.tdd.whole_suite_deferred = True
    _mark_block(state, block_idx, BlockStatus.DONE, config)
    log.info("DONE block=%d/%d name=%s", block.number, total, block.name)
    progress.log_block(block.number, total, block.name, "COMPLETE")
    _tdd_event(config, state, block, "BLOCK", "DONE")
    print(f"BLOCK:{block.number}/{total}:{block.name}:COMPLETE")
    return True


def _record_review_round(
    config: BuildConfig,
    block: BlockInfo,
    review: ReviewResult,
    gate: GateResult,
    kind: str = "review",
    plan: review_rounds.RoundPlan | None = None,
) -> Path | None:
    """Write this round's record under the change dir's `reviews/`. With a
    *plan* (round 2 on), its open list, assessments and the labels of the
    findings outside the last fix's reach go in the record too."""
    memory = (
        {}
        if plan is None
        else {
            "carried_open": plan.carried,
            "new_open": plan.new_open,
            "outside": plan.outside,
            "prior_assessments": plan.assessments,
        }
    )
    head = git_ops.rev_parse_head(config.project_dir)
    reviewed, unknown = _reviewed_revision(config, head)
    return review_rounds.record_round(
        config.change_dir,
        block,
        head_sha=head,
        reviewed_sha=reviewed,
        reviewed_sha_unknown=unknown,
        review=review,
        gate_passed=gate.passed,
        gate_reason=gate.reason,
        weighted_average=review.weighted_average_with(config.review_gate),
        kind=kind,
        **memory,
    )


def _reviewed_revision(
    config: BuildConfig, head: str | None
) -> tuple[str | None, str | None]:
    """(*head*, None) when *head* holds the code the reviewer just read:
    nothing in the project differs from it but buildme's bookkeeping and the
    round records (which the reviewer's diff leaves out too), a submodule the
    parent's `ignore` setting hides included, and git is told to skip
    checking no file, in the project or in a submodule. Otherwise (None, why)."""
    if not head:
        return None, "git could not read HEAD"
    try:
        change = (
            (config.change_dir / review_rounds.REVIEWS_DIR)
            .resolve()
            .relative_to(Path(config.project_dir).resolve())
        )
    except ValueError:
        change = None
    unchecked = git_ops.unchecked_files(config.project_dir, ignore=change)
    if unchecked is None:
        return None, "git could not list the index"
    if unchecked:
        return None, (
            "git is told not to check "
            + ", ".join(unchecked[:5])
            + (" and others" if len(unchecked) > 5 else "")
            + " (assume-unchanged or skip-worktree), so an edit there would not show"
        )
    if not git_ops.tree_is_clean(
        config.project_dir, ignore=change, every_submodule=True
    ):
        return None, (
            "uncommitted changes in the project (or git status failed): "
            "HEAD is not the tree the reviewer read"
        )
    return head, None


def _scope_diff(config: BuildConfig, last: dict) -> str | None:
    """`git diff <last reviewed_sha> HEAD`: every change committed since the
    previous review ran, the round records left out as in the review diff.
    None when either end is not a commit whose tree its review read (the last
    record names none: an uncommitted tree, or a record written before
    0.96.6; or the tree under review now is not HEAD) or git cannot give the
    diff."""
    sha = last.get("reviewed_sha")
    if not sha:
        return None
    current, why = _reviewed_revision(
        config, git_ops.rev_parse_head(config.project_dir)
    )
    if current is None:
        log.info("No review scope diff: the tree reviewed now is not HEAD (%s)", why)
        return None
    try:
        proc = subprocess.run(
            [
                "git",
                "diff",
                "--relative",
                sha,
                "HEAD",
                *_without_review_records(config),
            ],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        log.warning("Could not diff %s..HEAD for the review scope: %s", sha[:8], e)
        return None
    if proc.returncode != 0:
        log.warning(
            "Could not diff %s..HEAD for the review scope: %s",
            sha[:8],
            proc.stderr.strip(),
        )
        return None
    diff = proc.stdout
    if len(diff) > MAX_REVIEW_DIFF_CHARS:
        diff = diff[:MAX_REVIEW_DIFF_CHARS] + "\n... (diff truncated)"
    return diff


def _review_exhausted_diagnosis(
    config: BuildConfig, block: BlockInfo, recurring: str | None
) -> str:
    """Why the review loop ran out: the findings still present or partly
    resolved in two or more round records, and the last fix's RECURRING:
    answer when it gave one."""
    ids = review_rounds.recurring_ids(config.change_dir, block.number)
    lines = [
        f"Review exhausted after {MAX_REVIEW_ITERATIONS} iterations.",
        "Findings still present or partly resolved in two or more rounds: "
        + (", ".join(ids) if ids else "none"),
    ]
    if recurring is not None:
        lines.append(f"RECURRING (from the last fix): {recurring}")
    return "\n".join(lines)


def _review_fix_description(brief: str) -> str:
    """The block description a review-fix agent gets: the brief, then the
    instructions to look for the same mistake and re-read its own diff."""
    return f"Fix review issues: {brief}\n\n{REVIEW_FIX_SELF_CHECK}"


def _record_fix(round_path: Path | str | None, block: BlockInfo, fix, **extra) -> None:
    """Put a returned fix agent's report and `SEARCHED:` list in the round
    record. A missing section is recorded as `searched: null` with a warning:
    code cannot check that a search happened, so it records the claim rather
    than gating on it."""
    searched = parse_searched(fix.output or "")
    if searched is None and fix.success:
        log.warning(
            "Review fix for block %d (%s) reported no SEARCHED: section — "
            "recorded as searched: null",
            block.number,
            block.name,
        )
    review_rounds.update_round(
        round_path,
        fix={
            "report": fix.output,
            "success": fix.success,
            "timed_out": fix.timed_out,
            "commit": None,
            "searched": searched,
            **extra,
        },
    )


def _discard_keeping_records(config: BuildConfig, sha: str, label: str) -> bool:
    """`git_ops.discard_to`, keeping every review round record: the rollback
    is of the fix, not of the record that says what happened to it."""
    saved = review_rounds.snapshot(config.change_dir)
    ok = git_ops.discard_to(config.project_dir, sha, label)
    review_rounds.restore(saved)
    return ok


def _records_dir_in_project(config: BuildConfig) -> Path | None:
    """The round records' directory relative to the project: the pass writes
    there itself, and `_discard_keeping_records` puts the records back."""
    try:
        return (
            review_rounds.reviews_dir(config.change_dir)
            .resolve()
            .relative_to(Path(config.project_dir).resolve())
        )
    except ValueError:
        return None


def _record_fix_pass_stop(config: BuildConfig, fp: FixOnPassState) -> None:
    """Record the tree as the pass leaves it on reaching its next step
    (`stop_record.record`): HEAD and every uncommitted path with its content.
    A resume rolls back only this (`_others_work_in_fix_pass`). When git
    cannot be asked, no record is kept, and a resume then refuses any
    uncommitted change."""
    stop = stop_record.record(
        config.project_dir, ignore=_records_dir_in_project(config)
    )
    fp.stop_head = stop.head if stop else None
    fp.stop_edits = stop.edits if stop else None
    fp.stop_index = stop.index if stop else None


def _others_work_in_fix_pass(config: BuildConfig, fp: FixOnPassState) -> str | None:
    """Why a resumed fix on pass must not roll the tree back, or None:
    `stop_record.others_work` against the pass's record. The rollback
    discards back to the checkpoint, so everything it throws away must be
    the stopped pass's own, and HEAD must be where the pass stopped. Killed
    while the fix agent ran, the pass has no record, so it refuses any
    uncommitted change and HEAD must be at the checkpoint. The round records
    are the pass's own and are not checked."""
    recorded = fp.step != "fix_agent" and fp.stop_edits is not None and fp.stop_head
    stop = (
        stop_record.StepStop(
            head=fp.stop_head, edits=fp.stop_edits, index=fp.stop_index
        )
        if recorded
        else None
    )
    return stop_record.others_work(
        config.project_dir,
        stop,
        ignore=_records_dir_in_project(config),
        head=fp.stop_head if recorded else fp.checkpoint,
    )


def _restore_block(block: BlockInfo, fp: FixOnPassState) -> None:
    """Put back the block state the fix on pass started from."""
    block.test_file_hashes = dict(fp.test_file_hashes)
    block.test_change_claims = list(fp.test_change_claims)
    block.implementer_report = fp.implementer_report
    if fp.review is not None:
        block.review_scores = fp.review


async def _fix_on_pass(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    block_idx: int,
    round_path: Path | None,
) -> bool:
    """Fix the accepted Critical/Major/Minor findings of a block that passed.

    One pass, outside `MAX_REVIEW_ITERATIONS`: checkpoint (HEAD, clean tree)
    → budget check → fix agent on those findings only → suite +
    `_enforce_test_integrity` → one more review and adjudication. The fix is
    kept only when the suite is green and `review_score_gate` still passes;
    otherwise the tree goes back to the checkpoint. Findings left unfixed go
    to `unfixed-findings.md`. That review is given the open findings like any
    later round, and `plan_round` keeps each one it does not call resolved. Returns False only when a rollback was refused,
    which leaves an unverified fix in the tree.
    """
    review = block.review_scores
    findings = review_rounds.fixable_on_pass(review.findings) if review else []
    if not findings:
        return True

    # The checkpoint must be a faithful snapshot: commit what the block has
    # not committed yet (the commit `_finish_block` would make), then check.
    _git_commit_block_phase(config, "fix", block)
    checkpoint = git_ops.rev_parse_head(config.project_dir)
    if checkpoint is None or not git_ops.tree_is_clean(config.project_dir):
        return _skip_fix_on_pass(
            config,
            block,
            progress,
            findings,
            "not attempted (no clean commit to roll back to)",
        )
    budget_mod.check(phase=f"fix on pass of block {block.number}")

    block.fix_pass = FixOnPassState(
        checkpoint=checkpoint,
        findings=findings,
        round_path=str(round_path) if round_path else None,
        review=review,
        test_file_hashes=dict(block.test_file_hashes),
        test_change_claims=list(block.test_change_claims),
        implementer_report=block.implementer_report,
    )
    state.checkpoint(config.state_file_path())
    return await _run_fix_on_pass(block, config, state, progress, block_idx)


def _skip_fix_on_pass(
    config: BuildConfig,
    block: BlockInfo,
    progress: ProgressWriter,
    findings: list,
    status: str,
) -> bool:
    log.warning("Fix on pass of block %d (%s): %s", block.number, block.name, status)
    progress.log_agent("ReviewFix", block.name, f"FIX ON PASS {status}")
    review_rounds.append_unfixed(config.change_dir, block, findings, status)
    return True


async def _run_fix_on_pass(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    block_idx: int,
) -> bool:
    """The fix on pass from its fix agent on, for `block.fix_pass`. Also the
    resume path, after `_resume_fix_on_pass` rolled the tree back."""
    fp = block.fix_pass
    state_path = config.state_file_path()
    accepted = ReviewResult(scores=[], findings=fp.findings).findings_text()
    brief = (
        "The block passed review. The judge accepted these findings and they are "
        "still open: fix them, and change nothing else."
        f"\n\n## Adjudicated findings (accepted — fix these)\n{accepted}"
    )
    review_rounds.update_round(fp.round_path, fix_brief=_review_fix_description(brief))
    progress.log_agent(
        "ReviewFix", block.name, f"FIX ON PASS — {len(fp.findings)} finding(s)"
    )

    context = assemble_context(
        block, config, "implementer", blocks=state.tdd.blocks if state.tdd else None
    )
    fix = await run_implementer(
        block_name=block.name,
        block_description=_review_fix_description(brief),
        failing_tests=brief,
        context_bundle=context,
        test_command=_block_agent_test_command(config, block),
        prompt_style=config.implementer_prompt_style,
        model=config.model_for("review_fix"),
        effort=config.effort_for("review_fix"),
        cwd=str(config.project_dir),
    )
    _record_fix(fp.round_path, block, fix, pass_kind="fix_on_pass")
    if fix.output:
        block.implementer_report = _trim_evidence(
            f"{block.implementer_report}\n\n[fix on pass]\n{fix.output}"
        )
    _record_fix_pass_stop(config, fp)
    fp.step = "tests"
    state.checkpoint(state_path)

    reason = ""
    if not fix.success:
        reason = "the fix agent " + ("timed out" if fix.timed_out else "failed")
    else:
        gate = _block_gate(config, block, "fix-on-pass")
        if not gate.passed:
            reason = f"tests failed after the fix: {gate.reason}{_output_note(gate)}"
        elif not _enforce_test_integrity(
            block, config, state, progress, block_idx, "fix on pass", fix.output or ""
        ):
            reason = "test files changed during the fix"

    new_review: ReviewResult | None = None
    plan: review_rounds.RoundPlan | None = None
    memory: review_rounds.RoundMemory | None = None
    if not reason:
        _record_fix_pass_stop(config, fp)
        fp.step = "review"
        state.checkpoint(state_path)
        # The findings the block's review rounds left open, with their ids
        # from the round records, as the review loop reads them. Only when the
        # latest review record is the one the pass started from: with that
        # record missing (not written, or unreadable), an older one would not
        # hold what the pass worked on, so no memory is used and every finding
        # of the pass stays open below.
        open_, earlier_rejected, last = review_rounds.open_state(
            config.change_dir, block.number
        )
        if (
            last is None
            or fp.round_path is None
            or Path(fp.round_path).name
            != (f"block-{block.number:02d}-round-{last.get('round')}.json")
        ):
            open_, last = [], None
        try:
            new_review = await _run_quality_review(
                block, config, **({"open_findings": open_} if open_ else {})
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            log.warning(
                "Review after the fix on pass of block %d failed: %s", block.number, e
            )
            reason = f"the review after the fix failed: {e}"
        else:
            # The fix is not committed yet, so `_scope_diff` (last head to
            # HEAD) would not show it: the scope is unknown, which leaves
            # every finding in scope.
            memory = (
                None
                if last is None
                else review_rounds.RoundMemory(
                    open=open_, rejected=earlier_rejected, scope_diff=None
                )
            )
            new_review = await _adjudicate_review_findings(
                new_review,
                block,
                config,
                _capture_block_diff(config, block),
                build_context=_global_constraints_text(config),
                memory=memory,
            )
            if memory is not None:
                plan = review_rounds.plan_round(
                    new_review.findings, open_, new_review.prior_assessments
                )
            else:
                # No round record to match the review's statuses to: missing
                # history is not a resolution, so what the pass worked on
                # stays open, in the record as in the block and the report.
                plan = review_rounds.RoundPlan(
                    assessments=[],
                    carried=list(fp.findings),
                    new_open=list(range(len(new_review.findings))),
                )
                plan.to_fix = plan.carried + list(new_review.findings)
            gate = review_score_gate(new_review, config.review_gate)
            progress.log_review(
                block.name,
                block.review_iterations + 1,
                new_review.weighted_average,
                gate.passed,
            )
            _record_review_round(
                config, block, new_review, gate, kind="fix_on_pass_review", plan=plan
            )
            if not gate.passed:
                reason = f"the block no longer passes review: {gate.reason}"

    if not reason and new_review is not None:
        sha = _git_commit_block_phase(config, "fix", block)
        review_rounds.amend_fix(fp.round_path, commit=sha, kept=True)
        still = [
            (
                f,
                plan.status(f.finding_id)
                if memory is not None
                else "no round record to check it against",
            )
            for f in plan.carried
        ]
        raised = [new_review.findings[i] for i in plan.new_open]
        block.review_scores = new_review.model_copy(update={"findings": plan.to_fix})
        block.fix_on_pass_kept = True
        for f, status in still:
            if review_rounds.fixable_on_pass([f]):
                review_rounds.append_unfixed(
                    config.change_dir,
                    block,
                    [f],
                    f"still open after the fix on pass ({status}; one fix pass per block)",
                )
        review_rounds.append_unfixed(
            config.change_dir,
            block,
            review_rounds.fixable_on_pass(raised),
            "not attempted (raised by the review after the kept fix; one fix pass per block)",
        )
        log.info(
            "Fix on pass of block %d kept (%s)", block.number, (sha or "no commit")[:8]
        )
        progress.log_agent("ReviewFix", block.name, "FIX ON PASS KEPT")
        return True

    review_rounds.amend_fix(fp.round_path, kept=False, not_kept_reason=reason)
    if not _discard_keeping_records(
        config, fp.checkpoint, f"fix on pass of block {block.number}"
    ):
        diagnosis = (
            f"The fix on pass of block {block.number} was not kept ({reason}) and "
            f"could not be rolled back to {fp.checkpoint[:8]}: the tree holds an "
            "unverified fix."
        )
        log.error(
            "FAIL block=%d name=%s phase=FIX_ON_PASS reason=rollback-refused",
            block.number,
            block.name,
        )
        progress.log_diagnosis(block.name, diagnosis)
        block.resumable = False
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
    _restore_block(block, fp)
    review_rounds.append_unfixed(
        config.change_dir, block, fp.findings, f"fix not kept: {reason}"
    )
    log.warning("Fix on pass of block %d rolled back: %s", block.number, reason)
    progress.log_agent("ReviewFix", block.name, f"FIX ON PASS ROLLED BACK — {reason}")
    return True


async def _resume_fix_on_pass(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
) -> bool:
    """A build stopped during a block's fix on pass: roll back to its
    checkpoint and run the pass again, so a half-applied fix is never kept."""
    block_idx = state.tdd.current_block if state.tdd else 0
    total = len(state.tdd.blocks) if state.tdd else 0
    fp = block.fix_pass
    assert fp is not None, "Resuming a fix requires its saved recovery record"
    refusal = _others_work_in_fix_pass(config, fp)
    if refusal:
        log.error(
            "FAIL block=%d name=%s phase=FIX_ON_PASS reason=resume-refused: %s",
            block.number,
            block.name,
            refusal,
        )
        progress.log_diagnosis(
            block.name,
            f"Did not resume the fix on pass of block {block.number}, and rolled nothing "
            f"back: {refusal}. Then run the build again.",
        )
        block.resumable = True
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
    log.warning(
        "Block %d stopped during its fix on pass (step %s): rolling back to %s "
        "and running the pass again",
        block.number,
        fp.step,
        fp.checkpoint[:8],
    )
    progress.log_agent(
        "ReviewFix",
        block.name,
        f"RESUMED at {fp.step} — rolling back to {fp.checkpoint[:8]}",
    )
    if not _discard_keeping_records(
        config, fp.checkpoint, f"fix on pass of block {block.number}"
    ):
        progress.log_diagnosis(
            block.name,
            f"Could not roll the unfinished fix on pass back to {fp.checkpoint[:8]}; "
            "the tree may hold a half-applied fix.",
        )
        block.resumable = False
        _mark_block(state, block_idx, BlockStatus.FAILED, config)
        return False
    _restore_block(block, fp)
    fp.step = "fix_agent"
    fp.stop_head = fp.stop_edits = fp.stop_index = None
    state.checkpoint(config.state_file_path())
    if not await _run_fix_on_pass(block, config, state, progress, block_idx):
        return False
    return _finish_block(block, config, state, progress, block_idx, total)


async def _run_quality_review(
    block: BlockInfo,
    config: BuildConfig,
    open_findings: list[ReviewFinding] | None = None,
) -> ReviewResult:
    """Run an evidence-based quality review of the block's implementation.

    Reviews the block's ACTUAL diff (pre-block baseline → working tree), with
    the project's coding standards pushed into the reviewer's context, via
    llm_steps.review_steps.run_code_review (which runs on config.model_for("review")).

    Propagates LLMCallError / LLMCallTimeoutError on failure. Callers must
    handle the exception and fail the block — silently fabricating passing
    scores is worse than a loud failure (the old fallback was a real bug).

    *open_findings* (round 2 of a block on) are listed after the RED/GREEN
    evidence, for the reviewer to give each a status. Nothing there tells the
    reviewer what to leave out ("Review prompts stay unbiased").
    """
    rubric = ""
    if config.rubric_path.exists():
        rubric = config.rubric_path.read_text()

    diff = _capture_block_diff(config, block)
    if not diff:
        log.warning(
            "No diff captured for block %d (%s) — reviewer sees spec context only",
            block.number,
            block.name,
        )

    spec_context = f"Block {block.number}: {block.name}\n{block.description}"
    if block.satisfies:
        spec_context += f"\nSatisfies: {', '.join(block.satisfies)}"
        # The satisfied capabilities' spec scenarios, verbatim — the reviewer
        # judges Missing/Extra/Misunderstood against these, not a paraphrase.
        texts = requirement_texts(config.change_dir)
        for cap in block.satisfies:
            if cap in texts:
                spec_context += f"\n\n### Requirement: {cap}\n{texts[cap]}"

    # Global constraints are review criteria too — a block that violates one
    # is non-compliant even when its local behavior is correct.
    constraints = _global_constraints_text(config)
    if constraints:
        spec_context += f"\n\n### Global Constraints (project-wide)\n{constraints}"

    # RED/GREEN evidence reaches the reviewer as claims to verify, not truth.
    report_parts = []
    if block.red_evidence:
        report_parts.append(
            f"### RED evidence (tests failing before implementation)\n"
            f"```\n{block.red_evidence}\n```"
        )
    if block.green_evidence:
        report_parts.append(
            f"### GREEN evidence (suite after implementation)\n"
            f"```\n{block.green_evidence}\n```"
        )
    if open_findings:
        report_parts.append(
            "### Findings still open from earlier rounds\n"
            + "\n".join(
                f"- {f.finding_id} [{f.severity}] {f.claim}"
                + (f" ({f.file_path}:{f.line})" if f.file_path else "")
                for f in open_findings
            )
        )
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
        # The conventions chain is scoped to the files this block touched, so a
        # sibling package's CLAUDE.md never reaches the reviewer as a rule.
        standards=config.standards_text(parse_diff_paths(diff)),
        implementer_report="\n\n".join(report_parts),
    )


def _stop_on_runner_failure(
    block: BlockInfo,
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    failed: RunnerFailed,
) -> None:
    """Stop *block* after the test runner failed `RUNNER_ATTEMPTS` times with
    no test results (row S30): FAILED and `resumable`, so a re-run resumes the
    block and runs the test command again, and no fix agent is spent on a
    failure no test reported. The diagnosis names the saved outputs."""
    log.error(
        "STOP block=%d name=%s reason=test-runner-failed: %s",
        block.number,
        block.name,
        failed,
    )
    progress.log_diagnosis(block.name, f"Stopped: {failed}")
    block.resumable = True
    idx = state.tdd.current_block if state.tdd else 0
    _mark_block(state, idx, BlockStatus.FAILED, config)


async def run_tdd_engine(config: BuildConfig, args, *, standalone: bool = True) -> int:
    """Run TDD and retain current spend if a provider or process interrupts it."""
    try:
        return await _run_tdd_engine(config, args, standalone=standalone)
    except BaseException:
        path = config.state_file_path()
        if path.exists():
            # The last atomic checkpoint owns block progress; attach the latest
            # incurred usage without treating the interrupted operation as done.
            saved = BuildState.load(path)
            saved.checkpoint(path)
        raise


async def _run_tdd_engine(config: BuildConfig, args, *, standalone: bool = True) -> int:
    """Main entry point for the TDD engine.

    Orchestrates: parse blocks → baseline gate → per-block TDD → whole-change
    refactor → entry-point check.

    *standalone* says who owns the build's lifecycle. True (the
    `python -m build_pipeline tdd` entry point) means the engine IS the build:
    it starts its own section of the progress file, and on success renames the
    checkpoint to `.build-state.complete.json` with the phase still at
    TDD_BUILD (`BuildState.complete`). That is not a published build, so a
    later `build` of the change runs as usual. False (the orchestrator's
    TDD_BUILD sub-phase, and each findings round) means the caller owns phase
    advancement and the checkpoint, and the engine must leave it in place:
    removing it here made the orchestrator's post-engine reload
    (`if state_path.exists(): state = BuildState.load(state_path)`) fall
    through to a stale in-memory copy with `tdd is None`, so a crash in
    DOCS_UPDATE/RESPEC/PUBLISH resumed at TDD_BUILD with no block state and
    re-ran the whole build.
    """
    state_path = config.state_file_path()
    progress = ProgressWriter(config.progress_file_path())

    # Ceilings first, so every call below is accounted against them.
    if standalone:
        budget_mod.reset()
    budget_mod.configure(
        max_tokens=config.budget_max_tokens,
        max_usd=config.budget_max_usd,
    )
    budget_mod.warn_into(progress)

    # Load or create state
    if state_path.exists():
        state = BuildState.load(state_path)
        # A resumed build inherits what the earlier run already spent —
        # a fresh budget on resume would make the ceiling unenforceable.
        if standalone:
            budget_mod.get_budget().load_state(
                state.budget, accept_unknown_spend=config.accept_unknown_spend
            )
        log.info(
            "START phase=TDD_ENGINE resumed=true block=%d spent=%d tokens",
            state.tdd.current_block if state.tdd else 0,
            budget_mod.get_budget().total_tokens,
        )
    else:
        state = BuildState(
            change_name=config.change_name,
            tier=config.tier,
            state_file=str(state_path),
            phase=BuildPhase.TDD_BUILD,
        )

    # Initialize TDD state from tasks.md if missing (fresh start or orchestrator pre-wrote state)
    if state.tdd is None:
        blocks = parse_blocks(config.tasks_path)
        if not blocks:
            log.error(
                "FAIL phase=TDD_ENGINE reason=no-blocks-found path=%s",
                config.tasks_path,
            )
            return EXIT_BUILD_FAILURE
        log.info("START phase=TDD_ENGINE blocks=%d tier=%s", len(blocks), config.tier)
        state.tdd = TDDState(blocks=blocks)
        state.checkpoint(state_path)

    # Allow --block to override starting position
    start_block = getattr(args, "block", None)
    if start_block is not None and state.tdd:
        state.tdd.current_block = start_block - 1  # 0-indexed

    total_blocks = len(state.tdd.blocks) if state.tdd else 0
    if standalone:
        progress.initialize(config.change_name, config.tier, total_blocks)
    else:
        # The caller started this run's section of the progress file.
        progress.log_phase("TDD_BUILD", f"Blocks: {total_blocks}")
    # The card is In Development for the whole engine run (a no-op emission
    # when the orchestrator already put it there).
    board.record(
        config,
        state,
        BuildPhase.TDD_BUILD,
        progress=progress,
        note=f"{total_blocks} block(s)",
    )

    # The standalone `tdd` command checks what a paid call needs first (item
    # 33); under `build` the orchestrator already did.
    if standalone:
        from . import preflight

        failed = preflight.run(config, publishes=False, target=None)
        if failed:
            preflight.report(failed)
            return EXIT_BUILD_FAILURE

    # --- Baseline test gate ---
    if state.tdd and not state.tdd.baseline_tests_pass:
        progress.log_phase("BASELINE", "Running existing test suite")
        try:
            gate = baseline_test_gate(
                config.test_command,
                config.project_dir,
                parallel=config.parallel_tests,
                timeout=config.test_timeout,
                failure_log=failure_log_path(config.change_dir, "baseline"),
            )
        except RunnerFailed as failed:
            log.error("STOP phase=BASELINE reason=test-runner-failed: %s", failed)
            progress.log_phase("BASELINE", f"STOPPED: {failed}")
            return EXIT_BUILD_FAILURE
        if not gate.passed:
            log.error(
                "Baseline test gate failed: %s%s", gate.reason, _output_note(gate)
            )
            progress.log_phase("BASELINE", f"FAILED: {gate.reason}{_output_note(gate)}")
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
            log.error(
                "FAIL phase=TDD_ENGINE reason=budget_exhausted block=%d", block.number
            )
            progress.log_phase("BUDGET", f"STOPPED: {e}\n{e.diagnosis}")
            state.checkpoint(state_path)
            return EXIT_BUILD_FAILURE

        log.info("Starting block %d/%d: %s", block.number, total_blocks, block.name)

        # A build that stopped during this block's fix on pass: the block had
        # already passed review, so only the pass runs again.
        if block.fix_pass is not None:
            if not await _resume_fix_on_pass(block, config, state, progress):
                return EXIT_BUILD_FAILURE
            state.advance_block(state_path)
            continue

        try:
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
            review_ok = await _run_integration_and_review(
                block, config, state, progress
            )
        except RunnerFailed as failed:
            _stop_on_runner_failure(block, config, state, progress, failed)
            return EXIT_BUILD_FAILURE
        if not review_ok:
            return EXIT_BUILD_FAILURE

        state.advance_block(state_path)

    # --- Whole-change refactor (one conservative pass, after every block) ---
    # Runs here and not per block on purpose: cross-block duplication is only
    # visible once every block exists. Whole-change review is not buildme's
    # job: /multiplai-dev:review runs on the PR.
    if not await _run_refactor_all(config, state, progress):
        return EXIT_BUILD_FAILURE

    # --- End-of-build whole run (`tdd.whole_suite: end`) ---
    # Before mutation testing: cosmic-ray's restore point must be a green,
    # committed tree.
    if not await _run_end_suite(config, state, progress, standalone):
        return EXIT_BUILD_FAILURE

    # --- Mutation testing (Python only; cosmic-ray over the changed lines) ---
    # After the whole-change refactor so it measures the code that ships. A
    # step that must see the finished, mutation-tested build goes after this.
    if not await _run_mutation(config, state, progress):
        return EXIT_BUILD_FAILURE

    # --- Entry point verification ---
    if config.gates.e2e_test_entry_point_check and not state.tdd.e2e_done:
        log.info("START phase=E2E_CHECK")
        progress.log_phase("E2E_CHECK", "Verifying entry point")
        e2e_gate = _verify_entry_point(config)
        state.tdd.e2e_done = True
        state.checkpoint(state_path)
        if not e2e_gate.passed:
            log.warning(
                "DONE phase=E2E_CHECK result=warning reason=%s", e2e_gate.reason
            )
            progress.log_phase("E2E_CHECK", f"WARNING: {e2e_gate.reason}")
        else:
            log.info("DONE phase=E2E_CHECK result=passed")
            progress.log_phase("E2E_CHECK", "PASSED")

    if standalone and not _verified_now(config, state):
        gate = final_verification(
            config.test_command,
            config.project_dir,
            parallel=config.parallel_tests,
            timeout=config.test_timeout,
            plan_revision=state.spec_gen.revision if state.spec_gen else "",
            failure_log=failure_log_path(config.change_dir, "final-verification"),
        )
        state.verification = gate.model_dump()
        state.checkpoint(state_path)
        if not gate.passed:
            progress.log_phase("VERIFY", gate.reason + _output_note(gate))
            return EXIT_BUILD_FAILURE

    # Success. Persist the finished TDD sub-state; the phase pointer stays at
    # TDD_BUILD (for the orchestrator to advance, or as a standalone run's
    # completed phase).
    state.checkpoint(state_path)
    # Report the spend even when nothing stopped: a build that finished at 95%
    # of its ceiling is the one worth knowing about before the next run.
    spend = budget_mod.get_budget()
    log.info(
        "DONE phase=TDD_ENGINE blocks=%d tokens=%d cost_usd=%.2f calls=%d",
        total_blocks,
        spend.total_tokens,
        spend.cost_usd,
        spend.calls,
    )
    progress.log_phase("BUDGET", spend.diagnosis())
    progress.log_phase("COMPLETE", _completion_line(state.tdd.blocks))
    if standalone:
        state.complete(state_path)
    return EXIT_SUCCESS


def _verified_now(config: BuildConfig, state: BuildState) -> bool:
    """True when `state.verification` passed on the inputs the tree has now
    (the orchestrator's test before publish), so the final check would run
    the same suite on the same tree again."""
    saved = state.verification or {}
    if not saved.get("passed"):
        return False
    try:
        current = verification_inputs(
            config.test_command,
            config.project_dir,
            parallel=config.parallel_tests,
            plan_revision=state.spec_gen.revision if state.spec_gen else "",
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return saved.get("metadata", {}).get("inputs") == current


def _completion_line(blocks: list[BlockInfo]) -> str:
    """The COMPLETE line of build-progress.md. Past the block loop a block
    that is not done failed and was skipped (`findings.code.skip_block`), so
    it is named rather than counted as implemented."""
    skipped = [b for b in blocks if b.status != BlockStatus.DONE]
    if not skipped:
        return f"All {len(blocks)} blocks implemented successfully"
    names = "; ".join(
        f"block {b.number} ({b.name}): {b.status.value}, skipped" for b in skipped
    )
    return (
        f"{len(blocks) - len(skipped)} of {len(blocks)} blocks implemented. "
        f"Not implemented: {names}"
    )


def capture_build_diff(config: BuildConfig, state: BuildState) -> str:
    """Whole-build diff: first block's pre-build baseline → working tree,
    capped like the per-block review diff. Returns "" on git
    failure — never raises."""
    baseline = None
    if state.tdd and state.tdd.blocks:
        baseline = state.tdd.blocks[0].baseline_commit
    target = baseline or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", "--relative", target, *_without_review_records(config)],
            cwd=str(config.project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.warning(
                "Failed to capture full build diff (git diff %s): %s",
                target,
                proc.stderr.strip(),
            )
            return ""
        diff = proc.stdout + _untracked_diff(config)
    except Exception as e:
        log.warning("Failed to capture full build diff: %s", e)
        return ""
    if len(diff) > MAX_REVIEW_DIFF_CHARS:
        diff = diff[:MAX_REVIEW_DIFF_CHARS] + "\n... (diff truncated for review)"
    return diff


def _snapshot_all_test_files(
    config: BuildConfig, state: BuildState
) -> dict[str, str] | None:
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
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
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
    # downstream re-runs the suite. Whose
    # edits are dirty is unknowable from here, and unknown is never safe to
    # destroy — so: no reset, no checkpoint, no refactor pass.
    pre_gate = _refactor_all_gate(config, state, "refactor-all-before")
    if not pre_gate.passed:
        progress.log_phase(
            "REFACTOR_ALL",
            f"FAILED — suite red before refactor: {pre_gate.reason}"
            + _output_note(pre_gate),
        )
        state.checkpoint(config.state_file_path())
        return False

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
            "checkpoint commit failed with uncommitted work in the tree"
        )
        progress.log_phase(
            "REFACTOR_ALL", "SKIPPED — could not checkpoint the pending tree"
        )
        state.tdd.refactor_all_done = True
        state.checkpoint(config.state_file_path())
        return True
    before = _snapshot_all_test_files(config, state)

    log.info("START phase=REFACTOR_ALL diff_chars=%d", len(diff))
    progress.log_phase("REFACTOR_ALL", "Simplifying across blocks")
    result = await run_refactor_all(
        diff=diff,
        design=config.design_path.read_text()
        if config.design_path.exists()
        else "(no implementation-plan.md)",
        rubric=config.rubric_path.read_text()
        if config.rubric_path.exists()
        else "(no rubric.md)",
        test_command=_change_agent_test_command(config, state),
        model=config.model_for("refactor_all"),
        effort=config.effort_for("refactor_all"),
        cwd=str(config.project_dir),
    )

    if not result.success:
        # The agent may have written files before it failed or timed out.
        # Nothing verified them, and the step is marked done below, so a
        # resume never re-runs it: left in the tree, they would make MUTATION
        # (which needs a committed tree) refuse on every resume. So they get
        # the same discard as a pass that failed verification.
        reason = "timeout" if result.timed_out else result.error
        log.warning("FAIL phase=REFACTOR_ALL reason=%s (non-fatal)", reason)
        progress.log_phase(
            "REFACTOR_ALL", "TIMEOUT" if result.timed_out else f"FAILED: {reason}"
        )
        gate = None
        detail = f"refactor agent did not finish: {reason}"
    else:
        gate = _refactor_all_gate(config, state, "refactor-all-after")
        detail = (
            ""
            if gate.passed
            else f"tests failed after refactor: {gate.reason}{_output_note(gate)}"
        )
    if gate is not None and gate.passed:
        after = _snapshot_all_test_files(config, state)
        if before is None or after is None:
            log.warning(
                "Test integrity could not run after the whole-change "
                "refactor: test file list unavailable"
            )
            progress.log_phase("REFACTOR_ALL", "test integrity NOT CHECKED")
            detail = "test integrity evidence unavailable"
        else:
            # No report argument, deliberately: no escape hatch in this window.
            tests_gate = unchanged_tests_gate(before, after, "")
            if not tests_gate.passed:
                detail = f"test files changed during refactor: {tests_gate.reason}"

    if detail:
        if rewind_to and git_ops.discard_to(
            config.project_dir, rewind_to, "whole-change refactor"
        ):
            log.warning("DONE phase=REFACTOR_ALL result=discarded reason=%s", detail)
            progress.log_phase("REFACTOR_ALL", f"REVERTED — {detail}")
            if not _refactor_all_gate(config, state, "refactor-all-discarded").passed:
                return False
        else:
            # The refactor's edits failed verification AND could not be
            # discarded — they stay in the tree, and nothing downstream
            # re-runs the suite. Say so
            # loudly rather than let the build reach COMPLETE looking clean.
            log.error(
                "DONE phase=REFACTOR_ALL result=unverified-and-not-discarded "
                "reason=%s — UNVERIFIED refactor edits remain in the tree "
                "and will ship unless reviewed",
                detail,
            )
            progress.log_phase("REFACTOR_ALL", f"REVERT FAILED — {detail}")
            state.checkpoint(config.state_file_path())
            return False
    else:
        sha = git_ops.commit_tree(
            config.project_dir,
            "refactor: simplify across blocks",
            "whole-change refactor",
        )
        if sha:
            # So the end-of-build run can throw this commit away first when
            # the whole suite is red right after it (`_run_end_suite`).
            state.tdd.refactor_all_commit = sha
            state.checkpoint(config.state_file_path())
        log.info(
            "DONE phase=REFACTOR_ALL result=committed sha=%s",
            sha[:8] if sha else "(no changes)",
        )
        progress.log_phase(
            "REFACTOR_ALL", "COMMITTED" if sha else "PASSED (no changes needed)"
        )

    state.tdd.refactor_all_done = True
    state.checkpoint(config.state_file_path())
    return True


def _refactor_all_gate(
    config: BuildConfig, state: BuildState, label: str
) -> GateResult:
    """The whole-change refactor's test run: the whole `test_command`, or,
    when the end-of-build run is owed (`_end_suite_needed`), the tests of
    everything the change touched (`_change_test_command`)."""
    failure_log = failure_log_path(config.change_dir, label)
    if (
        _end_suite_needed(config, state)
        and _change_test_command(config, state) != config.test_command
    ):
        return integration_gate(
            _change_test_command(config, state),
            config.project_dir,
            timeout=config.test_timeout,
            failure_log=failure_log,
        )
    return integration_gate(
        config.test_command,
        config.project_dir,
        parallel=config.parallel_tests,
        timeout=config.test_timeout,
        failure_log=failure_log,
    )


def _end_suite_needed(config: BuildConfig, state: BuildState) -> bool:
    """True when the whole test command runs once after the last block:
    `tdd.whole_suite: end`, or a block already finished under it."""
    return _defers_whole_suite(config) or bool(
        state.tdd and state.tdd.whole_suite_deferred
    )


def _end_event(
    config: BuildConfig, progress: ProgressWriter, status: str, detail: str = ""
) -> None:
    """`_tdd_event` for the end-of-build run: `block=end phase=END_SUITE`."""
    started = _BLOCK_STARTED.setdefault((config.change_name, 0), time.monotonic())
    line = (
        f"TDD: {config.change_name} block=end phase=END_SUITE {status} "
        f"elapsed={time.monotonic() - started:.0f}" + (f" {detail}" if detail else "")
    )
    print(line, flush=True)
    try:
        progress.log_line(line)
    except OSError as e:
        log.warning("Could not append to the progress file: %s", e)


def _content_now(config: BuildConfig) -> str | None:
    """The tree's `verification_inputs` content hash, or None when Git could
    not be read."""
    try:
        return verification_inputs(
            config.test_command, config.project_dir, parallel=config.parallel_tests
        )["content"]
    except (OSError, RuntimeError, ValueError) as e:
        log.warning("Could not fingerprint the tree for the end-of-build run: %s", e)
        return None


def _refuse_end_suite(
    config: BuildConfig, progress: ProgressWriter, since: str | None
) -> bool:
    """Refuse to start or resume the end run over uncommitted changes it
    cannot tell from the user's. Changes nothing; returns False."""
    status = git_ops._run(
        [
            "git",
            "status",
            "--porcelain",
            "-z",
            "--",
            ".",
            *git_ops.BOOKKEEPING_EXCLUDES,
        ],
        cwd=config.project_dir,
    )
    names = sorted(
        {entry[3:] for entry in status.stdout.split("\0") if len(entry) > 3}
        if status.ok
        else set()
    )
    paths = ", ".join(names) or "the project"
    message = (
        f"END_SUITE: did not resume — uncommitted changes in {paths} since "
        f"{(since or '')[:8]}; buildme cannot tell its stopped fixer's edits from "
        "yours. Commit them to keep them or discard them, then run the build again."
    )
    log.error(message)
    print(message, flush=True)
    progress.log_phase("END_SUITE", message)
    return False


def _start_end_suite(config: BuildConfig, state: BuildState) -> EndSuiteState:
    """Save a fresh checkpoint for the end run at HEAD, keeping *attempts*
    from the run in flight (if any), and checkpoint the state."""
    tdd = state.tdd
    previous = tdd.end_suite
    snapshot = _snapshot_all_test_files(config, state)
    tdd.end_suite = EndSuiteState(
        checkpoint=git_ops.rev_parse_head(config.project_dir) or "",
        attempts=previous.attempts if previous else 0,
        test_file_hashes=snapshot,
    )
    state.checkpoint(config.state_file_path())
    return tdd.end_suite


def _end_suite_gate(
    config: BuildConfig, state: BuildState, label: str, standalone: bool
) -> GateResult:
    """One whole run of `test_command`. In a standalone run it is
    `final_verification`, saved to `state.verification`, so the final check
    after the engine reuses it when nothing changed since."""
    failure_log = failure_log_path(config.change_dir, label)
    if standalone:
        gate = final_verification(
            config.test_command,
            config.project_dir,
            parallel=config.parallel_tests,
            timeout=config.test_timeout,
            plan_revision=state.spec_gen.revision if state.spec_gen else "",
            failure_log=failure_log,
        )
        state.verification = gate.model_dump()
        return gate
    return integration_gate(
        config.test_command,
        config.project_dir,
        parallel=config.parallel_tests,
        timeout=config.test_timeout,
        failure_log=failure_log,
    )


def _write_fix_attempts_diff(config: BuildConfig, checkpoint: str) -> str | None:
    """`git diff <checkpoint>` of the tree the end fixers left, new files
    included, to `.buildme-test-runs/<stamp>-end-fix-attempts.diff`. Returns
    the path, or None when it could not be written."""
    root = config.project_dir
    pathspec = ["--", ".", *git_ops.BOOKKEEPING_EXCLUDES]
    tracked = git_ops._run(["git", "diff", "--binary", checkpoint, *pathspec], cwd=root)
    untracked = git_ops._run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", *pathspec], cwd=root
    )
    if not tracked.ok or not untracked.ok:
        log.warning(
            "Could not read the end fixers' diff: %s",
            (tracked.stderr or untracked.stderr).strip(),
        )
        return None
    parts = [tracked.stdout]
    for name in filter(None, untracked.stdout.split("\0")):
        # Exit 1 means "differs", which a new file always does.
        new = git_ops._run(
            ["git", "diff", "--binary", "--no-index", "--", "/dev/null", name], cwd=root
        )
        parts.append(new.stdout)
    path = failure_log_path(config.change_dir, "end-fix-attempts").with_suffix(".diff")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(parts), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write the end fixers' diff to %s: %s", path, e)
        return None
    return str(path)


def _end_suite_exhausted(
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    gate: GateResult,
    reason: str,
) -> bool:
    """Save the fixers' diff, put the tree back to the end run's checkpoint,
    record the failure and return False."""
    es = state.tdd.end_suite
    diff_file = _write_fix_attempts_diff(config, es.checkpoint)
    discarded = git_ops.discard_to(
        config.project_dir, es.checkpoint, "end-of-build fix attempts"
    )
    es.exhausted = True
    es.after_fix_content = None
    es.failure_file = gate.metadata.get("output_file")
    state.checkpoint(config.state_file_path())
    diagnosis = (
        f"End-of-build test run failed: {reason}\n"
        f"Gate: {gate.reason}\n"
        f"Last run's output: {es.failure_file or '(not saved)'}\n"
        f"Fix attempts' diff: {diff_file or '(not saved)'}\n"
        + (
            f"The tree is back at {es.checkpoint[:8]}."
            if discarded
            else f"The fix attempts could NOT be discarded back to "
            f"{es.checkpoint[:8]}: inspect the tree."
        )
    )
    log.error("FAIL phase=END_SUITE reason=%s", reason)
    progress.log_diagnosis("end of build", diagnosis)
    _end_event(config, progress, "FAIL", f"attempts={es.attempts}" + _output_note(gate))
    return False


async def _run_end_suite(
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
    standalone: bool,
) -> bool:
    """Run the whole `test_command` once after the last block, when
    `tdd.whole_suite: end` left it owed (`_end_suite_needed`).

    Green: done. Red: when HEAD is the whole-change refactor commit, throw it
    away and run again; then up to MAX_INTEGRATION_FIX_ATTEMPTS fix attempts
    (the last one escalated), each followed by a whole run. A fix that turns
    the suite green is checked by `unchanged_tests_gate` and committed. If the
    attempts run out, the fixers' diff and the last run's output are saved,
    the tree goes back to where the run started, and this returns False.

    Resume (rows S6-S12 of plan-buildme-full-suite-once-2026-10-28.md) never
    commits, discards or tests uncommitted edits it cannot prove are the
    stopped fixer's (`EndSuiteState.after_fix_content`).
    """
    tdd = state.tdd
    if tdd is None or tdd.end_suite_done or not _end_suite_needed(config, state):
        return True
    state_path = config.state_file_path()
    es = tdd.end_suite
    head = git_ops.rev_parse_head(config.project_dir)
    clean = git_ops.tree_is_clean(config.project_dir)
    fixed = (
        es is not None
        and not es.exhausted
        and es.after_fix_content is not None
        and _content_now(config) == es.after_fix_content
    )
    if fixed:
        pass  # S8/S9: the stopped fixer's tree; test it, as the loop would.
    elif not clean:
        return _refuse_end_suite(config, progress, es.checkpoint if es else head)
    else:
        if es is not None and es.exhausted:
            message = (
                f"END_SUITE: previous run exhausted {es.attempts} fix attempts "
                f"(output: {es.failure_file}); starting again from {(head or '')[:8]}"
            )
            log.warning(message)
            print(message, flush=True)
            progress.log_phase("END_SUITE", message)
            tdd.end_suite = None
        if tdd.end_suite is None or tdd.end_suite.checkpoint != head:
            _start_end_suite(config, state)
    es = tdd.end_suite

    _end_event(config, progress, "START", f"attempts={es.attempts}")
    progress.log_phase("END_SUITE", "Running the whole test command")
    gate = _end_suite_gate(config, state, f"end-suite-{es.attempts}", standalone)
    if (
        not gate.passed
        and not fixed
        and tdd.refactor_all_commit
        and head == tdd.refactor_all_commit
    ):
        parent = git_ops._run(
            ["git", "rev-parse", f"{tdd.refactor_all_commit}^"], cwd=config.project_dir
        )
        if parent.ok and git_ops.discard_to(
            config.project_dir, parent.stdout.strip(), "whole-change refactor"
        ):
            log.warning(
                "Whole-change refactor discarded: the whole suite is red after it"
            )
            progress.log_phase(
                "REFACTOR_ALL", "REVERTED — whole suite red after it (end of build)"
            )
            tdd.refactor_all_commit = None
            es = _start_end_suite(config, state)
            gate = _end_suite_gate(
                config, state, f"end-suite-{es.attempts}", standalone
            )

    while not gate.passed:
        log.warning(
            "End-of-build test run failed: %s%s", gate.reason, _output_note(gate)
        )
        if es.attempts >= MAX_INTEGRATION_FIX_ATTEMPTS:
            return _end_suite_exhausted(
                config,
                state,
                progress,
                gate,
                f"still red after {es.attempts} fix attempts",
            )
        try:
            budget_mod.check(phase="end-of-build fix")
        except BudgetExceededError as e:
            log.error("FAIL phase=END_SUITE reason=budget_exhausted")
            progress.log_phase("BUDGET", f"STOPPED: {e}\n{e.diagnosis}")
            state.checkpoint(state_path)
            return False
        es.attempts += 1
        es.after_fix_content = None
        state.checkpoint(state_path)
        escalated = es.attempts == MAX_INTEGRATION_FIX_ATTEMPTS
        fix_step = "integration_fix_escalated" if escalated else "integration_fix"
        fix_model = config.model_for(fix_step)
        progress.log_agent(
            "IntegrationFixer",
            "end of build",
            f"attempt {es.attempts}/{MAX_INTEGRATION_FIX_ATTEMPTS}"
            + (" — ESCALATED" if escalated else ""),
        )
        diff = capture_build_diff(config, state)
        fix_result = await run_integration_fix(
            failure_output=gate.metadata.get("output", gate.reason),
            failing_tests=failing_tests(gate.metadata.get("output", "")),
            test_command=_change_agent_test_command(config, state),
            context_bundle=(
                "## The change's diff (every block, from the start of the build)\n"
                + (diff or "(diff unavailable)")
            ),
            escalate=escalated,
            model=fix_model,
            effort=config.effort_for(fix_step),
            cwd=str(config.project_dir),
        )
        if fix_result.output:
            es.fix_reports.append(fix_result.output)
        es.after_fix_content = _content_now(config)
        state.checkpoint(state_path)
        if fix_result.success:
            gate = _end_suite_gate(
                config, state, f"end-suite-{es.attempts}", standalone
            )
        fixed = True

    if fixed:
        after = _snapshot_all_test_files(config, state)
        if es.test_file_hashes is None or after is None:
            return _end_suite_exhausted(
                config, state, progress, gate, "test integrity evidence unavailable"
            )
        tests_gate = unchanged_tests_gate(es.test_file_hashes, after, es.fix_reports)
        if not tests_gate.passed:
            return _end_suite_exhausted(
                config, state, progress, gate, tests_gate.reason
            )
        claims = tests_gate.metadata.get("claims") or []
        if claims:
            es.test_change_claims = list(claims)
            progress.log_phase(
                "END_SUITE",
                f"{tests_gate.reason}\n" + "\n".join(f"- {c}" for c in claims),
            )
        git_ops.commit_tree(
            config.project_dir, "fix: end-of-build test suite", "end-of-build fix"
        )
        if standalone:
            # The commit moved HEAD: the final check runs again on it.
            state.verification = None
    tdd.end_suite_done = True
    tdd.end_suite = None
    state.checkpoint(state_path)
    progress.log_phase("END_SUITE", "PASSED")
    _end_event(config, progress, "DONE")
    return True


def _snapshot_source_files(config: BuildConfig) -> dict[str, str] | None:
    """{project-relative path: sha256} for every non-test file the survivor
    round must leave alone: every tracked file, plus every untracked file not
    ignored by git (a new source module, a root pytest.ini that would change
    how the second run is scored), minus anything `_TEST_PATH_RE` calls a
    test, buildme's own bookkeeping, and the caches running the suite writes
    (`_is_tool_cache`). None when git could not list them — "not checked",
    never "everything was deleted" (same contract as `_snapshot_test_files`)."""
    names: set[str] = set()
    for argv in (
        ["git", "ls-files", "-z"],
        ["git", "ls-files", "-z", "-o", "--exclude-standard"],
    ):
        try:
            proc = subprocess.run(
                argv,
                cwd=str(config.project_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Could not list source files: %s", e)
            return None
        if proc.returncode != 0:
            log.warning("Could not list source files: %s", proc.stderr.strip())
            return None
        for name in proc.stdout.split("\0"):
            if not name or _is_tool_cache(name):
                continue
            if (
                _TEST_PATH_RE.search(name)
                or Path(name).name in _BOOKKEEPING_NAMES
                or TEST_RUNS_DIR in Path(name).parts
            ):
                continue
            names.add(name)
    snapshot: dict[str, str] = {}
    for name in sorted(names):
        try:
            snapshot[name] = hashlib.sha256(
                (config.project_dir / name).read_bytes()
            ).hexdigest()
        except OSError:
            # A tracked file already gone is recorded as absent; if it
            # reappears (or vanishes) later, the comparison sees it.
            continue
    return snapshot


# buildme's own files, by basename — the source snapshot must not count them
# as edits (they change on every checkpoint). Mirrors git_ops.BOOKKEEPING_EXCLUDES.
_BOOKKEEPING_NAMES = frozenset(
    {".build-state.json", ".board.json", "build-progress.md"}
)

# What running the suite or installing the project writes into a tree whose
# .gitignore does not cover it: caches, coverage and junit reports, and an
# install's *.egg-info. Tracked paths are skipped too: block commits run
# `git add -A`, so such a tree already tracks these, and every suite run
# rewrites them (a .pyc whenever cosmic-ray has touched the source, a report
# with a fresh timestamp each time).
_TOOL_CACHE_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        "htmlcov",
        ".eggs",
    }
)
_SUITE_REPORT_NAMES = frozenset(
    {
        "coverage.xml",
        "coverage.json",
        "coverage.lcov",
        "lcov.info",
    }
)


def _is_tool_cache(name: str) -> bool:
    path = Path(name)
    lowered = path.name.lower()
    return (
        any(
            part in _TOOL_CACHE_DIRS or part.endswith(".egg-info")
            for part in path.parts[:-1]
        )
        or path.suffix in (".pyc", ".pyo")
        or path.name == ".coverage"
        or path.name.startswith(".coverage.")
        or lowered in _SUITE_REPORT_NAMES
        or (lowered.startswith("junit") and path.suffix.lower() == ".xml")
    )


def _edits_the_suite_does_not_rewrite(
    config: BuildConfig,
    rewind_to: str,
    before: dict[str, str],
    changed: list[str],
) -> tuple[list[str] | None, GateResult | None]:
    """Which of the *changed* non-test files the agent edited, as opposed to
    files its own suite runs wrote (a test.db, a *.log, a .benchmarks/ entry,
    an HTML report — new, or tracked because a block commit swept it in).

    Puts each changed file back as it was at *rewind_to* (a file that did not
    exist there is deleted), runs the suite once, and keeps the files the
    suite did not write again: those still match the pre-round snapshot.
    A file the full suite writes to the same bytes every run (a golden JSON,
    a test.db built from fixed fixtures) matches it too, so each committed
    file kept that way gets the agent's version back for one more suite run:
    the ones that run rewrites are the suite's output, and go back to the
    checkpoint. Returns the first suite run's gate result too, so the caller
    does not run the suite again. The list is None when the check could not
    be made; a file that cannot be put back is counted as edited."""
    root = config.project_dir
    try:
        proc = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--name-only", rewind_to, "--", *changed],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Could not list the checkpoint's files: %s", e)
        return None, None
    if proc.returncode != 0:
        log.warning("Could not list the checkpoint's files: %s", proc.stderr.strip())
        return None, None
    in_checkpoint = {name for name in proc.stdout.split("\0") if name}
    # The agent's version of each committed file, for the second run below.
    # None: the agent deleted it. A file that cannot be read is left out.
    agent_versions: dict[str, bytes | None] = {}
    for name in in_checkpoint:
        try:
            agent_versions[name] = (root / name).read_bytes()
        except FileNotFoundError:
            agent_versions[name] = None
        except OSError as e:
            log.warning("Could not read %s: %s", name, e)
    unrestorable: list[str] = []
    for name in changed:
        if name in in_checkpoint:
            continue
        if name in before:
            # Present before the round but not committed: no copy to put back.
            unrestorable.append(name)
            continue
        try:
            (root / name).unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not remove %s: %s", name, e)
            unrestorable.append(name)
    if in_checkpoint:
        try:
            proc = subprocess.run(
                ["git", "checkout", rewind_to, "--", *sorted(in_checkpoint)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Could not restore the round's non-test files: %s", e)
            return None, None
        if proc.returncode != 0:
            log.warning(
                "Could not restore the round's non-test files: %s", proc.stderr.strip()
            )
            return None, None
    gate = integration_gate(
        config.test_command,
        root,
        parallel=config.parallel_tests,
        timeout=config.test_timeout,
    )
    rerun = _snapshot_source_files(config)
    if rerun is None:
        return None, None
    edited = sorted(
        set(unrestorable)
        | {
            name
            for name in changed
            if name not in unrestorable and rerun.get(name) == before.get(name)
        }
    )
    suite_written = _rewritten_by_the_suite(
        config,
        rewind_to,
        {
            name: agent_versions[name]
            for name in edited
            if name in agent_versions and name not in unrestorable
        },
    )
    if suite_written is None:
        return None, None
    return [name for name in edited if name not in suite_written], gate


def _rewritten_by_the_suite(
    config: BuildConfig,
    rewind_to: str,
    agent_versions: dict[str, bytes | None],
) -> set[str] | None:
    """Which of these committed files the suite writes: each gets the agent's
    version back (None: deleted), the suite runs once, and a file that run
    changed is the suite's output. Those go back to *rewind_to*; the others
    keep the agent's version, which the caller discards with the round.
    None when the check could not be made."""
    if not agent_versions:
        return set()
    root = config.project_dir
    put_back: dict[str, str | None] = {}
    for name, data in agent_versions.items():
        try:
            if data is None:
                (root / name).unlink(missing_ok=True)
                put_back[name] = None
            else:
                (root / name).write_bytes(data)
                put_back[name] = hashlib.sha256(data).hexdigest()
        except OSError as e:
            log.warning("Could not put back the agent's %s: %s", name, e)
    if not put_back:
        return set()
    integration_gate(
        config.test_command,
        root,
        parallel=config.parallel_tests,
        timeout=config.test_timeout,
    )
    rerun = _snapshot_source_files(config)
    if rerun is None:
        return None
    written = {name for name, digest in put_back.items() if rerun.get(name) != digest}
    if written:
        try:
            proc = subprocess.run(
                ["git", "checkout", rewind_to, "--", *sorted(written)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Could not restore the suite's output files: %s", e)
            return None
        if proc.returncode != 0:
            log.warning(
                "Could not restore the suite's output files: %s", proc.stderr.strip()
            )
            return None
    return written


def _format_survivors(config: BuildConfig, result: MutationResult) -> str:
    """The survivor list the mutation test writer sees: location, operator,
    the line as it is now, and cosmic-ray's diff (capped)."""
    parts = []
    for s in result.survivors:
        try:
            source_lines = (config.project_dir / s.path).read_text().splitlines()
            line = (
                source_lines[s.line - 1].strip()
                if 0 < s.line <= len(source_lines)
                else ""
            )
        except OSError:
            line = ""
        diff = s.diff.strip()
        if len(diff) > 1500:
            diff = diff[:1500] + "\n... (diff truncated)"
        parts.append(
            f"### {s.path}:{s.line} — {s.operator}\n"
            f"Line now: `{line}`\n```diff\n{diff}\n```"
        )
    return "\n\n".join(parts)


def _write_mutation_report(config: BuildConfig, text: str) -> str | None:
    """Write `specs/changes/<name>/mutation.md` and commit it. Returns the
    commit sha (None when nothing was committed — logged by git_ops)."""
    path = config.change_dir / "mutation.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)
        return None
    return git_ops.commit_tree(
        config.project_dir, "test(mutation): record mutation score", "mutation report"
    )


class _RoundOutcome(NamedTuple):
    """What the survivor round did. `discard_failed` means the round's edits
    are still in the tree — the caller must fail the step and commit nothing,
    or the next whole-tree commit would sweep them onto the build branch."""

    note: str
    source_edited: bool = False
    sha: str | None = None
    discard_failed: bool = False


async def _run_mutation_round(
    config: BuildConfig,
    first: MutationResult,
    rewind_to: str,
    agent_test_command: str,
) -> _RoundOutcome:
    """The one survivor round.

    `source_edited` means the agent changed a non-test file — the round is
    discarded and the caller fails the step. A red suite or a failed agent
    discards the round and keeps the first-run result (sha None); an agent
    stopped at its turn cap is not a failed one, and goes through the same
    checks as a finished round. Any discard that git refuses sets
    `discard_failed`.
    """

    def _discard(
        note_ok: str, note_failed: str, *, source_edited: bool = False
    ) -> _RoundOutcome:
        if git_ops.discard_to(config.project_dir, rewind_to, "mutation survivor round"):
            return _RoundOutcome(note_ok, source_edited=source_edited)
        return _RoundOutcome(
            note_failed + f" The round's edits could NOT be discarded back to "
            f"{rewind_to[:8]}, so nothing was committed — inspect the tree.",
            source_edited=source_edited,
            discard_failed=True,
        )

    before = _snapshot_source_files(config)
    branch = git_ops.current_branch(config.project_dir)
    result = await run_mutation_test_writer(
        survivors=_format_survivors(config, first),
        test_command=agent_test_command,
        model=config.model_for("mutation_test_writer"),
        effort=config.effort_for("mutation_test_writer"),
        max_turns=config.mutation_test_writer_max_turns,
        cwd=str(config.project_dir),
    )

    # The agent holds Bash, so it may have committed its own work. Its commits
    # go back into the tree so every check below sees what it changed, and
    # buildme commits (or discards) the round itself. A HEAD that cannot be
    # brought back to the checkpoint leaves nothing safe to commit or reset.
    # The branch is compared even when the sha did not move: a switch to a new
    # branch at the same commit would put buildme's commits on that branch.
    head = git_ops.rev_parse_head(config.project_dir)
    on_branch = git_ops.current_branch(config.project_dir)
    if on_branch != branch:
        return _RoundOutcome(
            f"The test writer left branch {branch or '(unknown)'} for "
            f"{on_branch or 'a branch git could not read'}, so nothing was "
            "committed — inspect the tree.",
            discard_failed=True,
        )
    if head != rewind_to and not git_ops.unwind_commits_to(
        config.project_dir, rewind_to, "mutation survivor round"
    ):
        where = f"to {head[:8]}" if head else "somewhere git could not read"
        return _RoundOutcome(
            f"The test writer moved HEAD from {rewind_to[:8]} {where}, and it could not be "
            "brought back, so nothing was committed — inspect the tree.",
            discard_failed=True,
        )
    # An agent stopped at its turn cap gets the same checks as a finished one.
    capped = _hit_turn_limit(result)
    if not result.success and not capped:
        reason = "timeout" if result.timed_out else (result.error or "agent failed")
        return _discard(
            f"The test writer failed ({reason}); its edits were discarded and the "
            "first-run score stands.",
            f"The test writer failed ({reason}); its edits were NOT discarded.",
        )

    after = _snapshot_source_files(config)
    gate = None
    if before is None or after is None:
        edited: list[str] | None = None
    else:
        edited = sorted(
            name
            for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        )
        if edited:
            edited, gate = _edits_the_suite_does_not_rewrite(
                config, rewind_to, before, edited
            )
    if edited is None or edited:
        detail = (
            "the source snapshot could not be taken"
            if edited is None
            else "non-test files changed: " + ", ".join(edited[:20])
        )
        return _discard(
            f"The test writer's round was discarded: {detail}.",
            f"The test writer's round must be discarded: {detail}.",
            source_edited=True,
        )

    if gate is None:
        gate = integration_gate(
            config.test_command,
            config.project_dir,
            parallel=config.parallel_tests,
            timeout=config.test_timeout,
        )
    if not gate.passed:
        return _discard(
            f"The new tests left the suite red ({gate.reason[:300]}); the round was "
            "discarded and the first-run score stands.",
            f"The new tests left the suite red ({gate.reason[:300]}); the round was "
            "NOT discarded.",
        )

    sha = git_ops.commit_tree(
        config.project_dir,
        "test(mutation): kill surviving mutants",
        "mutation survivor round",
    )
    if sha is None:
        if git_ops.tree_is_clean(config.project_dir):
            return _RoundOutcome(
                "The test writer changed no files; the first-run score stands."
            )
        return _discard(
            "The round's tests could not be committed and were discarded; "
            "the first-run score stands.",
            "The round's tests could not be committed.",
        )
    if capped:
        return _RoundOutcome(
            "The test writer ran out of turns; its tests passed the round's checks "
            f"and were kept, committed as {sha[:8]} "
            "(`test(mutation): kill surviving mutants`).",
            sha=sha,
        )
    return _RoundOutcome(
        f"The test writer added or edited tests, committed as {sha[:8]} "
        "(`test(mutation): kill surviving mutants`).",
        sha=sha,
    )


async def _run_mutation(
    config: BuildConfig,
    state: BuildState,
    progress: ProgressWriter,
) -> bool:
    """The MUTATION sub-step: cosmic-ray over the lines the build changed.

    Returns False when the build must stop — score below `mutation.min_score`,
    a tool that could not produce a score, a survivor round that edited source,
    or the budget breaker. Every False leaves `mutation_done` unset, so a
    resume runs the step again. A skip (not Python, disabled, nothing changed)
    sets the flag and returns True.
    """
    if not state.tdd or state.tdd.mutation_done:
        return True
    state_path = config.state_file_path()

    def _skip(reason: str) -> bool:
        log.info("SKIP phase=MUTATION reason=%s", reason)
        progress.log_phase("MUTATION", f"SKIPPED — {reason}")
        state.tdd.mutation_done = True
        state.checkpoint(state_path)
        return True

    def _fail(reason: str) -> bool:
        log.error("FAIL phase=MUTATION reason=%s", reason)
        progress.log_phase("MUTATION", f"FAILED: {reason}")
        state.checkpoint(state_path)
        return False

    applies, why_not = mutation_applies(config)
    if not applies:
        return _skip(why_not)
    baseline = state.tdd.blocks[0].baseline_commit if state.tdd.blocks else None
    if not baseline:
        return _skip(
            "no pre-build baseline commit recorded, so the changed lines are unknown"
        )

    # A committed tree is the restore point for cosmic-ray's in-place edits
    # and for a discarded survivor round. Every verified path into this step
    # leaves the tree committed: the whole-change refactor checkpoints the
    # review fixes and commits or discards its own pass. So uncommitted edits
    # here are ones nothing verified — a refactor whose verification failed
    # or whose agent died mid-edit, and whose discard git refused; a refactor
    # skipped on a red suite or a failed checkpoint; or a previous mutation
    # round that could not be discarded. Committing them would put
    # them on the build branch and score mutants against them; resetting
    # would destroy work nobody has looked at. So stop and leave them.
    if not git_ops.tree_is_clean(config.project_dir):
        return _fail(
            "the tree has uncommitted changes that buildme did not verify (see the "
            "REFACTOR_ALL entry above, or a previous MUTATION failure) — refusing to "
            "commit them or mutation-test them. Review them, commit or discard them, "
            "then resume the build"
        )
    rewind_to = git_ops.rev_parse_head(config.project_dir)
    if rewind_to is None:
        return _fail("could not read HEAD to restore to before mutation testing")

    paths = changed_source_files(config.project_dir, baseline)
    if paths is None:
        return _fail("could not list the files changed since the pre-build baseline")
    if not paths:
        return _skip("no non-test Python file the test suite imports changed")

    log.info("START phase=MUTATION files=%d", len(paths))
    progress.log_phase("MUTATION", f"Mutating {len(paths)} changed Python file(s)")
    first = run_mutation(config, baseline, paths)
    if not first.ok:
        return _fail(f"cosmic-ray did not produce a score: {first.error}")
    log.info(
        "MUTATION first_run score=%s survivors=%d",
        score_text(first),
        len(first.survivors),
    )

    final = first
    round_note = "No survivors — no round was needed."
    if first.survivors:
        try:
            budget_mod.check(phase="mutation survivor round")
        except BudgetExceededError as e:
            log.error("FAIL phase=MUTATION reason=budget_exhausted")
            progress.log_phase("BUDGET", f"STOPPED: {e}\n{e.diagnosis}")
            state.checkpoint(state_path)
            return False
        progress.log_agent(
            "MutationTestWriter",
            "survivors",
            f"{len(first.survivors)} surviving mutant(s)",
        )
        outcome = await _run_mutation_round(
            config, first, rewind_to, _change_agent_test_command(config, state)
        )
        round_note = outcome.note
        if outcome.discard_failed:
            # No report: writing it means a whole-tree commit, which would put
            # the round's red tests or source edits onto the build branch.
            return _fail(round_note)
        if outcome.source_edited:
            _write_mutation_report(
                config,
                render_report(
                    first=first,
                    final=first,
                    round_note=round_note,
                    min_score=config.mutation.min_score,
                    passed=False,
                ),
            )
            return _fail(round_note)
        if outcome.sha:
            second = run_mutation(config, baseline, paths)
            if second.discard_failed:
                return _fail(
                    f"the second cosmic-ray run left the tree changed and "
                    f"it could not be discarded: {second.error}"
                )
            if second.ok:
                final = second
            else:
                round_note += (
                    f" The second cosmic-ray run failed ({second.error}); "
                    "the first-run score stands."
                )

    # Every path above leaves the tree committed or discarded. If it is not,
    # the report's whole-tree commit would carry whatever is there onto the
    # build branch, so stop instead.
    if not git_ops.tree_is_clean(config.project_dir):
        return _fail(
            "the tree has uncommitted changes after mutation testing — "
            "refusing to commit the report on top of them"
        )

    # No score with no errors means no mutant was generated on the changed
    # lines: nothing to judge, so the step passes. No score with errors means
    # every mutant errored, so cosmic-ray could not judge the tests at all.
    if final.score is None and final.errors:
        return _fail(
            f"cosmic-ray did not produce a score: all {final.errors} "
            "mutant(s) on the changed lines errored and none could be judged"
        )

    min_score = config.mutation.min_score
    passed = final.score is None or final.score >= min_score
    _write_mutation_report(
        config,
        render_report(
            first=first,
            final=final,
            round_note=round_note,
            min_score=min_score,
            passed=passed,
        ),
    )

    if not passed:
        return _fail(
            f"mutation score {score_text(final)} is below "
            f"mutation.min_score {min_score:.2f} — see "
            f"{config.change_dir / 'mutation.md'}"
        )
    state.tdd.mutation_done = True
    state.checkpoint(state_path)
    log.info("DONE phase=MUTATION result=passed score=%s", score_text(final))
    progress.log_phase("MUTATION", f"PASSED — score {score_text(final)}")
    return True


def _detect_entry_point(
    config: BuildConfig,
) -> tuple[str, list[str], dict | None] | None:
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
                env["PYTHONPATH"] = str(base) + (
                    os.pathsep + existing if existing else ""
                )
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
        return GateResult(
            passed=True, reason="No explicit entry point (library project assumed)"
        )
    label, argv, env = entry

    if not _repo_trusted():
        return GateResult(
            passed=False,
            reason=f"Entry point found ({label}) but not smoke-run: repo not "
            f"trusted (set --trust-repo or BUILDME_TRUST_REPO=1)",
        )
    try:
        proc = subprocess.run(
            argv,
            cwd=str(config.project_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
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
