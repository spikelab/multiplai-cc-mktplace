"""Quality gates — pure code assertions between pipeline stages.

Each gate returns a GateResult. Failed gates include an action hint
that the pipeline uses to decide recovery strategy.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

from .models import GateResult, ImplementationNote, ReviewResult

log = logging.getLogger(__name__)


def _repo_trusted() -> bool:
    """Mirror of sdk._repo_is_trusted (kept dependency-free here). Gates that run
    the repo's own test_command / conftest.py must not execute against a repo the
    user has not vouched for — the test command is arbitrary argv and pytest runs
    any conftest.py in the tree at collection time (CWE-94)."""
    return os.environ.get("BUILDME_TRUST_REPO", "").strip().lower() in ("1", "true", "yes")


# NOTE: not currently wired into the pipeline. No caller invokes an implementation
# feasibility gate; kept for a future explicit feasibility check.
def feasibility_gate(project_dir: Path, stack: str, dependencies: list[str]) -> GateResult:
    """Check if dependencies can be resolved for the detected stack."""
    if not dependencies:
        return GateResult(passed=True, reason="No dependencies to check")

    if stack in ("pyproject", "python"):
        # Check PyPI availability
        missing = []
        for dep in dependencies:
            pkg = dep.split(">=")[0].split("==")[0].split("[")[0].strip()
            try:
                result = subprocess.run(
                    ["pip", "index", "versions", pkg],
                    capture_output=True, text=True, timeout=30,
                )
            except FileNotFoundError:
                # No `pip` on the PATH (uv-only machine) — can't check PyPI,
                # so skip the gate rather than crashing the orchestrator.
                log.warning("pip not found; skipping PyPI availability gate")
                return GateResult(passed=True, reason="pip unavailable — skipped PyPI check")
            if result.returncode != 0:
                missing.append(pkg)
        if missing:
            return GateResult(
                passed=False, reason=f"Packages not found on PyPI: {missing}",
                action="suggest_alternatives", metadata={"missing": missing},
            )
    elif stack in ("Package", "swift"):
        resolve = subprocess.run(
            ["swift", "package", "resolve"],
            capture_output=True, text=True, cwd=project_dir, timeout=120,
        )
        if resolve.returncode != 0:
            return GateResult(
                passed=False, reason=f"SPM resolve failed: {resolve.stderr[:500]}",
                action="fix_dependencies",
            )
    return GateResult(passed=True, reason=f"Dependencies resolved for {stack}")


def wiring_task_gate(tasks_path: Path, project_dir: Path) -> GateResult:
    """Check that tasks.md has a wiring task if the project is an app."""
    app_markers = [
        project_dir / ".xcodeproj",
        project_dir / "src" / "__main__.py",
        *project_dir.glob("**/__main__.py"),
    ]
    is_app = False
    for marker in app_markers:
        if isinstance(marker, Path) and marker.exists():
            is_app = True
            break
    # Check package.json for entry points
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        import json
        data = json.loads(pkg_json.read_text())
        if "main" in data or "bin" in data or "scripts" in data.get("scripts", {}):
            is_app = True

    if not is_app:
        return GateResult(passed=True, reason="Not detected as app project")

    tasks_text = tasks_path.read_text() if tasks_path.exists() else ""
    wiring_patterns = re.compile(
        r"(wir(?:e|ing)|entry.?point|connect.*into|startup.?sequence|runnable)",
        re.IGNORECASE,
    )
    if wiring_patterns.search(tasks_text):
        return GateResult(passed=True, reason="Wiring task found in tasks.md")

    return GateResult(
        passed=False,
        reason="No entry-point wiring task found. TDD agents will build "
               "mocked units but nothing assembles them into a working app.",
        action="add_wiring_task",
    )


def baseline_test_gate(test_command: str, project_dir: Path) -> GateResult:
    """Run the test suite and check it passes before TDD starts."""
    if not test_command:
        return GateResult(passed=True, reason="No test command configured — skipping baseline")
    if not _repo_trusted():
        return GateResult(
            passed=False,
            reason="Repo not trusted — refusing to run its test_command/conftest.py "
                   "(set --trust-repo or BUILDME_TRUST_REPO=1).",
            action="fix_tests",
        )
    try:
        result = subprocess.run(
            shlex.split(test_command),
            capture_output=True, text=True, cwd=project_dir, timeout=300,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return GateResult(passed=False, reason=f"Test command failed: {e}", action="fix_tests")

    if result.returncode == 0:
        return GateResult(
            passed=True, reason="Baseline tests pass",
            metadata={"stdout": result.stdout[-500:]},
        )
    return GateResult(
        passed=False,
        reason=f"Baseline tests failing (exit {result.returncode})",
        action="fix_tests",
        metadata={"stderr": result.stderr[-1000:], "stdout": result.stdout[-500:]},
    )


def run_test_suite(test_command: str, project_dir: Path, timeout: int = 300) -> tuple[int | None, str]:
    """Run the repo's test suite under the trust guard (shared mechanics for
    the integration and RED gates).

    Returns (exit_code, combined output). exit_code is None when the command
    could not run at all (missing binary, timeout, untrusted repo) — callers
    must treat None as "could not verify", never as pass or fail evidence.
    """
    if not _repo_trusted():
        return None, (
            "Repo not trusted — refusing to run its test_command/conftest.py "
            "(set --trust-repo or BUILDME_TRUST_REPO=1)."
        )
    try:
        result = subprocess.run(
            shlex.split(test_command),
            capture_output=True, text=True, cwd=project_dir, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, f"Test command failed to run: {e}"
    return result.returncode, result.stdout + "\n" + result.stderr


def integration_gate(test_command: str, project_dir: Path) -> GateResult:
    """Run the full test suite after a block completes."""
    if not test_command:
        return GateResult(passed=True, reason="No test command — skipping integration gate")
    if not _repo_trusted():
        return GateResult(
            passed=False,
            reason="Repo not trusted — refusing to run its test_command/conftest.py "
                   "(set --trust-repo or BUILDME_TRUST_REPO=1).",
        )
    try:
        result = subprocess.run(
            shlex.split(test_command),
            capture_output=True, text=True, cwd=project_dir, timeout=300,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return GateResult(passed=False, reason=f"Integration tests failed: {e}")

    if result.returncode == 0:
        return GateResult(
            passed=True, reason="All tests pass",
            metadata={"stdout": result.stdout[-2000:]},
        )
    return GateResult(
        passed=False,
        reason=f"Tests failing (exit {result.returncode})",
        action="spawn_fix_agent",
        metadata={"stderr": result.stderr[-1000:], "stdout": result.stdout[-500:]},
    )


# Failure signatures that prove tests fail "for the right reason": the code
# under test is missing or incomplete, not that the test files themselves are
# broken.
_RED_RIGHT_REASON = re.compile(
    r"(AssertionError|NotImplementedError|"
    r"has no attribute|is not defined|"
    r"^FAILED\b|\bFAILED\b|"
    # Runner-agnostic signatures: Jest/Vitest print a per-file `FAIL src/…`
    # marker (uppercase only — lowercase "fail" appears in ordinary prose);
    # terse runners (pytest -q --tb=no, Jest's "Tests: 1 failed") may emit
    # only a summary count. Zero-count summaries ("0 failed") are not proof.
    r"^FAIL\b|"
    r"(?i:\b[1-9]\d*\s+failed\b))",
    re.MULTILINE,
)

# Signatures of broken test files: pytest could not even collect/parse them.
_RED_BROKEN_TESTS = re.compile(
    r"(SyntaxError|IndentationError|"
    r"error(s)? during collection|collection error|"
    r"errors during collection|ERROR collecting|"
    r"INTERNALERROR)",
    re.IGNORECASE,
)


def red_gate(test_output: str, exit_code: int | None) -> GateResult:
    """RED-phase gate: prove the freshly written tests fail for the right reason.

    Pass iff the suite exits non-zero AND the failure signature is a genuine
    behavioral failure (FAILED / AssertionError / NotImplementedError /
    missing attribute), not a broken test file (collection/syntax error).

    Fail actions:
    - "rewrite_tests": suite passed — the new tests exercise nothing that is
      still unimplemented, so they prove nothing.
    - "fix_tests": tests are broken (collection/syntax error) or the failure
      signature is unrecognizable — repair the test files, don't implement yet.
    """
    tail = test_output[-1000:]
    if exit_code is None:
        return GateResult(
            passed=False,
            reason=f"RED gate could not run the suite: {tail}",
            action="fix_tests",
            metadata={"exit_code": None},
        )
    if exit_code == 0:
        return GateResult(
            passed=False,
            reason="RED gate failed: suite passed BEFORE implementation — the new "
                   "tests exercise nothing unimplemented and prove nothing",
            action="rewrite_tests",
            metadata={"exit_code": 0},
        )
    if _RED_BROKEN_TESTS.search(test_output):
        return GateResult(
            passed=False,
            reason=f"RED gate failed: tests error instead of failing "
                   f"(collection/syntax problem, exit {exit_code}): {tail}",
            action="fix_tests",
            metadata={"exit_code": exit_code},
        )
    if _RED_RIGHT_REASON.search(test_output):
        return GateResult(
            passed=True,
            reason=f"RED confirmed: tests fail for the right reason (exit {exit_code})",
            metadata={"exit_code": exit_code},
        )
    return GateResult(
        passed=False,
        reason=f"RED gate failed: suite exits {exit_code} but with no recognizable "
               f"test-failure signature: {tail}",
        action="fix_tests",
        metadata={"exit_code": exit_code},
    )


_AGENT_STATUS_RE = re.compile(
    r"^\s*(?:[-*]\s*|\*\*)?STATUS:?\*{0,2}\s*[:\-]?\s*"
    r"(DONE_WITH_CONCERNS|DONE|NEEDS_CONTEXT|BLOCKED)\b",
    re.MULTILINE | re.IGNORECASE,
)
_AGENT_STATUS_PROCEED = {"DONE", "DONE_WITH_CONCERNS"}


def parse_agent_status(output: str) -> str | None:
    """The last `STATUS:` slot an agent reported, uppercased ("" → None).

    Agents are instructed to close their report with a REQUIRED STATUS slot.
    The last occurrence wins so a status quoted mid-report (e.g. restating the
    instructions) never outranks the agent's own final verdict.
    """
    matches = _AGENT_STATUS_RE.findall(output or "")
    return matches[-1].upper() if matches else None


def agent_status_gate(output: str, agent_name: str) -> GateResult:
    """Gate an agent's self-reported STATUS slot.

    NEEDS_CONTEXT and BLOCKED are the agent saying it could not do the work —
    the pipeline surfaces the stated reason and fails the block rather than
    proceeding on an admitted non-result. A missing slot is not fatal (the
    deterministic gates are the real verification), but it is reported.
    """
    status = parse_agent_status(output)
    if status is None:
        return GateResult(
            passed=True,
            reason=f"{agent_name} reported no STATUS slot",
            metadata={"status": None},
        )
    if status in _AGENT_STATUS_PROCEED:
        return GateResult(
            passed=True,
            reason=f"{agent_name} reported STATUS: {status}",
            metadata={"status": status},
        )
    return GateResult(
        passed=False,
        reason=f"{agent_name} reported STATUS: {status} — it could not complete "
               f"the work. Agent report:\n{(output or '')[-1500:]}",
        action="escalate_to_human",
        metadata={"status": status},
    )


# --- Prototype-first stage ---

# Words that name a user-visible output format. Deliberately narrower than the
# rubric's backend keyword set: bare "schema" and "model" are ordinary
# database/backend vocabulary, so matching them would make every DB change
# "needs a prototype". The phrases here describe something a person looks at —
# a rendered page, a printed/exported document, a shaped response.
_OUTPUT_FORMAT_RE = re.compile(
    r"\b(report|reports|export|exports|dashboard|mockup|wireframe|"
    r"screenshot|printout|invoice|receipt|spreadsheet|pdf|"
    r"cli output|command.line output|terminal output|console output|"
    r"stdout|sample output|output format|output schema|response schema|"
    r"json schema|rendered (?:page|view|output|document)|"
    r"user.visible|user.facing (?:output|format|document))\b",
    re.IGNORECASE,
)


def prototype_required(change_dir: Path) -> bool:
    """Whether this change should prove its shape with a cheap prototype first.

    True when the change type is frontend or fullstack (there is a UI to look
    at), or when the proposal/design describe a user-visible output format.
    Pure function — reads the artifacts already on disk, makes no LLM call.
    """
    from .rubric import detect_change_type

    change_type = detect_change_type(change_dir)
    if change_type in ("frontend", "fullstack"):
        log.info("Prototype required: change_type=%s", change_type)
        return True

    for filename in ("proposal.md", "design.md"):
        path = change_dir / filename
        if not path.exists():
            continue
        match = _OUTPUT_FORMAT_RE.search(path.read_text())
        if match:
            log.info(
                "Prototype required: %s mentions user-visible output (%r)",
                filename, match.group(0),
            )
            return True

    log.info("Prototype not required: change_type=%s, no output-format mention", change_type)
    return False


# The REQUIRED slots a prototype agent closes its NOTES.md with. Same convention
# as the implementation agents' STATUS/TESTS_RUN/GREEN/FILES slots parsed by
# parse_agent_status.
PROTOTYPE_SLOTS = ("PROVES", "DISPROVES", "OPEN_QUESTIONS", "STATUS")

_SLOT_HEADER_RE = re.compile(
    r"^\s*(?:[-*]\s*|#{1,6}\s*|\*\*)?(" + "|".join(PROTOTYPE_SLOTS) + r")\*{0,2}\s*:",
    re.MULTILINE | re.IGNORECASE,
)

# Slot values that say "nothing here". A slot filled with "none" is a real
# answer (the agent looked and found nothing) but carries no content to act on.
_EMPTY_SLOT_VALUES = {"", "none", "none.", "n/a", "na", "-", "—", "(none)", "nothing", "tbd"}


def parse_prototype_notes(text: str) -> dict[str, str]:
    """Parse a prototype NOTES.md into its REQUIRED slots.

    Returns a dict keyed by lowercased slot name (proves, disproves,
    open_questions, status) with the slot's text, which may span several lines
    up to the next slot header. Missing slots are absent from the dict.
    """
    notes: dict[str, str] = {}
    matches = list(_SLOT_HEADER_RE.finditer(text or ""))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        notes[match.group(1).lower()] = value
    return notes


def slot_has_content(value: str | None) -> bool:
    """Whether a slot carries something actionable ("none" does not)."""
    if value is None:
        return False
    stripped = value.strip().strip("*_` ").lower()
    return stripped not in _EMPTY_SLOT_VALUES


def prototype_gate(prototype_dir: Path) -> GateResult:
    """Gate the prototype stage's output.

    Passes when the directory holds at least one artifact file besides NOTES.md
    (the thing that actually proves the shape) and NOTES.md reports what the
    prototype PROVES plus its OPEN_QUESTIONS. A NOTES.md with no artifact
    beside it is a description of a prototype, not a prototype.
    """
    if not prototype_dir.exists():
        return GateResult(
            passed=False,
            reason=f"Prototype directory not created: {prototype_dir}",
            action="retry_prototype",
        )

    notes_path = prototype_dir / "NOTES.md"
    if not notes_path.exists():
        return GateResult(
            passed=False,
            reason=f"Prototype NOTES.md missing in {prototype_dir}",
            action="retry_prototype",
        )

    notes_text = notes_path.read_text()
    notes = parse_prototype_notes(notes_text)

    # The prompt invites an honest STATUS: NEEDS_CONTEXT / BLOCKED when the
    # proposal and design do not say enough to draw the shape. Checked BEFORE
    # the artifact/slot checks: an agent that could not draw usually leaves
    # only NOTES.md, and retrying or reporting "empty PROVES" would bury its
    # stated blocker under a misleading structural failure. `action` is
    # deliberately not "retry_prototype" — re-asking the same unanswerable
    # question cannot succeed.
    status_value = (notes.get("status") or "").strip().strip("*_` ")
    status_word = status_value.split()[0].upper().rstrip(".,;:") if status_value else ""
    if status_word in ("BLOCKED", "NEEDS_CONTEXT"):
        return GateResult(
            passed=False,
            reason=(
                f"Prototype agent reported STATUS: {status_word} — it could not "
                f"draw the shape from the proposal/design. Its notes:\n"
                f"{notes_text[-1500:]}"
            ),
            action="prototype_blocked",
            metadata={"status": status_word},
        )

    artifacts = [
        p for p in sorted(prototype_dir.rglob("*"))
        if p.is_file() and p != notes_path
    ]
    if not artifacts:
        return GateResult(
            passed=False,
            reason="Prototype produced no artifact file besides NOTES.md — "
                   "nothing to look at, so nothing is proven.",
            action="retry_prototype",
        )

    missing = [s for s in PROTOTYPE_SLOTS if s.lower() not in notes]
    if missing:
        return GateResult(
            passed=False,
            reason=f"Prototype NOTES.md is missing REQUIRED slots: {missing}",
            action="retry_prototype",
            metadata={"artifacts": [str(a) for a in artifacts]},
        )

    # The three slot content rules deliberately differ:
    # - PROVES must carry real content ("none" fails, via slot_has_content):
    #   an artifact that proves nothing about the shape is not a prototype.
    # - OPEN_QUESTIONS must be non-empty but "none" passes: "I looked and
    #   nothing is left to decide" is a real answer; only a blank slot means
    #   the agent never considered the question.
    # - DISPROVES needs only to be present (the `missing` check above): empty
    #   and "none" both mean nothing was disproved — there is nothing to act
    #   on, so nothing further to enforce.
    if not slot_has_content(notes.get("proves")):
        return GateResult(
            passed=False,
            reason="Prototype NOTES.md has an empty PROVES: slot — the artifact "
                   "must state what shape it proves.",
            action="retry_prototype",
            metadata={"artifacts": [str(a) for a in artifacts]},
        )

    if not (notes.get("open_questions") or "").strip():
        return GateResult(
            passed=False,
            reason="Prototype NOTES.md has an empty OPEN_QUESTIONS: slot — write "
                   "the remaining questions, or 'none'.",
            action="retry_prototype",
            metadata={"artifacts": [str(a) for a in artifacts]},
        )

    return GateResult(
        passed=True,
        reason=f"Prototype produced {len(artifacts)} artifact file(s) with complete NOTES.md",
        metadata={
            "artifacts": [str(a) for a in artifacts],
            "status": notes.get("status", ""),
        },
    )


# --- Implementation notes (SURPRISES: / SPEC_IMPACT: slots) ---

# Every REQUIRED slot label an agent report can carry — used as the stop
# boundary when reading the free-text SURPRISES slot.
_REPORT_SLOT_LABELS = "STATUS|TESTS_RUN|GREEN|FILES|TEST_COUNT|SURPRISES|SPEC_IMPACT"

# The colon after a slot label is MANDATORY — every legitimate slot form
# carries one (`SURPRISES: x`, `- **SURPRISES:** x`, `**SURPRISES**: x`).
# Requiring it keeps prose that merely mentions the word ("Surprises were
# minimal this block.", or an echo of the prompt's own coaching sentence)
# from being recorded as a note, and keeps a continuation line that happens
# to open with a slot word ("STATUS quo in the repo differs...") from
# truncating a multiline value. The colon may sit inside or outside the
# closing ** of a bold label.
_SLOT_COLON = r"(?::\*{0,2}|\*{1,2}:)"

_SURPRISES_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?SURPRISES" + _SLOT_COLON + r"[ \t]*"
    r"(.*?)"
    r"(?=^\s*(?:[-*]\s*)?(?:\*\*)?(?:" + _REPORT_SLOT_LABELS + r")"
    + _SLOT_COLON + r"|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)

_SPEC_IMPACT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?SPEC_IMPACT" + _SLOT_COLON + r"\s*"
    r"(none|clarify|contradicts)\b",
    re.MULTILINE | re.IGNORECASE,
)

# Placeholder answers that mean "nothing to report" — treated as an empty
# SURPRISES slot rather than as content. Shares the prototype gate's
# empty-slot vocabulary (_EMPTY_SLOT_VALUES) minus "tbd": in a prototype's
# PROVES: slot "tbd" is a non-answer, but an implementer writing "tbd" under
# SURPRISES is deferring something real, which the respec loop must keep.
_EMPTY_SURPRISES = _EMPTY_SLOT_VALUES - {"tbd"}


def _clean_surprises(raw: str) -> str:
    """Strip fences and the prompt's own angle-bracket placeholder from the
    free-text SURPRISES slot; return "" when it says nothing."""
    lines = [
        ln for ln in (raw or "").strip().splitlines()
        if not ln.strip().startswith("```")
    ]
    text = "\n".join(lines).strip()
    # The agent echoing the template (`SURPRISES: <what did not match ...>`)
    # is not a finding.
    if text.startswith("<") and text.endswith(">"):
        return ""
    return "" if text.lower() in _EMPTY_SURPRISES else text


def parse_implementation_note(
    output: str,
    *,
    block_number: int,
    block_name: str,
    role: str,
) -> ImplementationNote | None:
    """Build an ImplementationNote from an agent's SURPRISES/SPEC_IMPACT slots.

    Returns None when the agent reported nothing worth recording (no slots, or
    "none" on both) — the notes file stays signal-only. The last occurrence of
    each slot wins, so a report that quotes the instructions before answering
    them never outranks the agent's own answer.
    """
    surprises_matches = _SURPRISES_RE.findall(output or "")
    surprises = _clean_surprises(surprises_matches[-1]) if surprises_matches else ""

    impact_matches = _SPEC_IMPACT_RE.findall(output or "")
    spec_impact = impact_matches[-1].lower() if impact_matches else "none"

    if not surprises and spec_impact == "none":
        return None

    return ImplementationNote(
        block_number=block_number,
        block_name=block_name,
        role=role,
        surprises=surprises,
        spec_impact=spec_impact,
    )


def review_score_gate(review: ReviewResult) -> GateResult:
    """Two-verdict gate: spec compliance (nothing missing/misunderstood) AND
    score threshold (weighted avg >= 3.5, no dimension at 1)."""
    avg = review.weighted_average
    failing = review.failing_dimensions

    if not review.spec_compliant:
        parts = []
        if review.missing:
            parts.append(f"missing: {review.missing}")
        if review.misunderstood:
            parts.append(f"misunderstood: {review.misunderstood}")
        return GateResult(
            passed=False,
            reason=f"Spec-compliance verdict failed — {'; '.join(parts)}",
            action="fix_spec_compliance",
            metadata={
                "missing": review.missing,
                "misunderstood": review.misunderstood,
                "extra": review.extra,
                "weighted_average": avg,
            },
        )
    if failing:
        return GateResult(
            passed=False,
            reason=f"Dimension(s) scored 1: {failing}",
            action="fix_critical_dimension",
            metadata={"failing_dimensions": failing, "weighted_average": avg},
        )
    if avg < 3.5:
        return GateResult(
            passed=False,
            reason=f"Weighted average {avg:.1f} < 3.5 threshold",
            action="fix_low_scores",
            metadata={"weighted_average": avg},
        )
    return GateResult(
        passed=True,
        reason=f"Review passed: weighted average {avg:.1f}",
        metadata={"weighted_average": avg},
    )


# --- Unknowns / explainer gate (B1) ---

# A dependency's section is a level-2 heading; its required lists are level-3
# subsections. Matching is by heading text, so the gate reads the same document
# a human reads.
_UNKNOWNS_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_UNKNOWNS_SUBSECTION_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_EDGE_CASES_RE = re.compile(r"edge\s*case", re.IGNORECASE)
_ASSUMPTIONS_RE = re.compile(r"assumption", re.IGNORECASE)
# A filled list item: a bullet or numbered item with real text after it —
# the (?!<) lookahead refuses template placeholders (`- <case>: <what happens>`).
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?!<)(\S.*)$", re.MULTILINE)


def _split_sections(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """(heading, body) pairs for every heading matched by ``pattern``."""
    matches = list(pattern.finditer(text or ""))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1).strip(), text[m.end():end]))
    return out


def _has_filled_list(body: str) -> bool:
    """True when the body carries at least one list item with real content
    (the item pattern itself already refuses `<placeholder>` items)."""
    return _LIST_ITEM_RE.search(body or "") is not None


def _heading_names_dep(heading: str, needle: str) -> bool:
    """Whether a ``## heading`` is THIS dependency's section.

    Whole-token match over package-name characters — `react` must not ride on
    a `## react-query` heading — while still tolerating decoration around the
    name (backticks, a trailing ``— what it is`` clause).
    """
    return re.search(
        r"(?<![A-Za-z0-9._+@/-])" + re.escape(needle) + r"(?![A-Za-z0-9._+@/-])",
        heading.lower(),
    ) is not None


def unknowns_gate(unknowns_text: str, deps) -> GateResult:
    """Structural gate on `unknowns.md` — the explainer must actually explain.

    Fails when a detected dependency has no section at all, or when its
    "Edge cases & failure modes" or "Assumptions we are making" list is empty.
    Those two lists are the whole point of the explainer (the Whisper-silence
    class of surprise), so an empty one is an unwritten explainer, not a
    stylistic nit.

    ``deps`` accepts NewDependency objects or plain name strings. With no
    dependencies the gate passes — "nothing new to this project" is a recorded
    finding, not a failure.
    """
    names = [getattr(d, "name", d) for d in (deps or [])]
    if not names:
        return GateResult(
            passed=True,
            reason="No dependencies new to this project — nothing to explain",
            metadata={"dependencies": []},
        )

    sections = _split_sections(unknowns_text or "", _UNKNOWNS_SECTION_RE)
    findings: list[str] = []
    for name in names:
        needle = str(name).lower()
        match = next(
            (body for heading, body in sections if _heading_names_dep(heading, needle)),
            None,
        )
        if match is None:
            findings.append(f"`{name}`: no `## {name}` section in unknowns.md")
            continue

        subsections = _split_sections(match, _UNKNOWNS_SUBSECTION_RE)
        edge_body = next(
            (b for h, b in subsections if _EDGE_CASES_RE.search(h)), None
        )
        assum_body = next(
            (b for h, b in subsections if _ASSUMPTIONS_RE.search(h)), None
        )
        if edge_body is None:
            findings.append(f"`{name}`: missing an 'Edge cases & failure modes' subsection")
        elif not _has_filled_list(edge_body):
            findings.append(
                f"`{name}`: 'Edge cases & failure modes' list is empty — needs at "
                f"least one bullet naming observed behavior on empty, malformed, "
                f"oversized, concurrent, and offline input"
            )
        if assum_body is None:
            findings.append(f"`{name}`: missing an 'Assumptions we are making' subsection")
        elif not _has_filled_list(assum_body):
            findings.append(
                f"`{name}`: 'Assumptions we are making' list is empty — needs at "
                f"least one falsifiable claim"
            )

    if findings:
        return GateResult(
            passed=False,
            reason="Explainer sections incomplete:\n" + "\n".join(f"- {f}" for f in findings),
            action="regenerate_unknowns",
            metadata={"findings": findings, "dependencies": names},
        )
    return GateResult(
        passed=True,
        reason=f"All {len(names)} dependency explainer(s) complete",
        metadata={"dependencies": names},
    )


def review_iteration_gate(iteration: int, max_iterations: int = 3) -> GateResult:
    """Check if review fix cycles are exhausted."""
    if iteration < max_iterations:
        return GateResult(
            passed=True,
            reason=f"Review iteration {iteration + 1}/{max_iterations}",
        )
    return GateResult(
        passed=False,
        reason=f"Review loop exhausted after {max_iterations} iterations",
        action="halt_build",
    )
