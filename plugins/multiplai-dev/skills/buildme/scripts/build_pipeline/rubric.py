"""Rubric module — detects change type and generates evaluation rubric.

The rubric is the final spec artifact, generated after tasks.md.
It's used by code review gates to score implementations.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .prompts.rubric_prompts import RUBRIC_PROMPT
from .sdk import llm_call

log = logging.getLogger(__name__)

# Keywords mapped to change types
_FRONTEND_KEYWORDS = re.compile(
    r"\b(react|vue|angular|css|html|component|ui|ux|button|form|page|"
    r"frontend|browser|dom|tailwind|stylesheet)\b",
    re.IGNORECASE,
)
_BACKEND_KEYWORDS = re.compile(
    r"\b(api|endpoint|database|db|migration|model|schema|server|"
    r"backend|rest|graphql|queue|worker|celery|cron)\b",
    re.IGNORECASE,
)
_INFRA_KEYWORDS = re.compile(
    r"\b(terraform|docker|kubernetes|k8s|ci|cd|pipeline|deploy|"
    r"infra|infrastructure|monitoring|logging|helm|ansible)\b",
    re.IGNORECASE,
)


def detect_change_type(change_dir: Path) -> str:
    """Detect change type from artifact contents.

    Reads proposal, specs, and design to classify as:
    frontend, backend, fullstack, or infra.

    Returns:
        One of: "frontend", "backend", "fullstack", "infra"
    """
    text = _gather_text(change_dir)
    if not text:
        return "backend"  # safe default

    frontend_hits = len(_FRONTEND_KEYWORDS.findall(text))
    backend_hits = len(_BACKEND_KEYWORDS.findall(text))
    infra_hits = len(_INFRA_KEYWORDS.findall(text))

    log.debug(
        "Change type detection: frontend=%d backend=%d infra=%d",
        frontend_hits, backend_hits, infra_hits,
    )

    # Infra dominates if it has the most hits
    if infra_hits > frontend_hits and infra_hits > backend_hits:
        return "infra"

    # Both frontend and backend present = fullstack
    if frontend_hits > 0 and backend_hits > 0:
        # Only fullstack if both have meaningful presence
        if frontend_hits >= 3 and backend_hits >= 3:
            return "fullstack"
        return "frontend" if frontend_hits > backend_hits else "backend"

    if frontend_hits > backend_hits:
        return "frontend"
    if backend_hits > 0:
        return "backend"

    return "backend"


async def generate_rubric(change_dir: Path, config) -> str:
    """Generate rubric.md tailored to the change type.

    Args:
        change_dir: Path to the change directory containing artifacts
        config: BuildConfig for model selection

    Returns:
        Generated rubric markdown content.
    """
    change_type = detect_change_type(change_dir)

    # Gather spec summaries
    spec_summaries = _gather_spec_summaries(change_dir)
    tasks_summary = _read_file(change_dir / "tasks.md")

    prompt = RUBRIC_PROMPT.format(
        change_type=change_type,
        spec_summaries=spec_summaries,
        tasks_summary=tasks_summary,
    )

    log.info("Generating rubric for change_type=%s", change_type)
    result = await llm_call(prompt, model=config.model)
    log.info("Rubric generated (%d chars)", len(result))
    return result


def _gather_text(change_dir: Path) -> str:
    """Gather text from all artifacts for keyword analysis."""
    parts = []
    for filename in ["proposal.md", "design.md", "tasks.md"]:
        path = change_dir / filename
        if path.exists():
            parts.append(path.read_text())

    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        for spec_file in sorted(specs_dir.glob("*/spec.md")):
            parts.append(spec_file.read_text())

    return " ".join(parts)


def _gather_spec_summaries(change_dir: Path) -> str:
    """Gather first few lines of each spec for rubric context."""
    specs_dir = change_dir / "specs"
    if not specs_dir.exists():
        return "(no specs)"
    parts = []
    for spec_file in sorted(specs_dir.glob("*/spec.md")):
        cap_name = spec_file.parent.name
        content = spec_file.read_text()
        # Take first 500 chars as summary
        summary = content[:500] + ("..." if len(content) > 500 else "")
        parts.append(f"### {cap_name}\n{summary}")
    return "\n\n".join(parts) if parts else "(no specs)"


def _read_file(path: Path) -> str:
    """Read a file, returning placeholder if missing."""
    if path.exists():
        return path.read_text()
    return "(not available)"
