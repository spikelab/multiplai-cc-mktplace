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

from .models import GateResult, ReviewResult

log = logging.getLogger(__name__)


def _repo_trusted() -> bool:
    """Mirror of sdk._repo_is_trusted (kept dependency-free here). Gates that run
    the repo's own test_command / conftest.py must not execute against a repo the
    user has not vouched for — the test command is arbitrary argv and pytest runs
    any conftest.py in the tree at collection time (CWE-94)."""
    return os.environ.get("BUILDME_TRUST_REPO", "").strip().lower() in ("1", "true", "yes")


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
        return GateResult(passed=True, reason="All tests pass")
    return GateResult(
        passed=False,
        reason=f"Tests failing (exit {result.returncode})",
        action="spawn_fix_agent",
        metadata={"stderr": result.stderr[-1000:], "stdout": result.stdout[-500:]},
    )


def review_score_gate(review: ReviewResult) -> GateResult:
    """Check if review scores meet threshold (weighted avg >= 3.5, no dim at 1)."""
    avg = review.weighted_average
    failing = review.failing_dimensions

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
