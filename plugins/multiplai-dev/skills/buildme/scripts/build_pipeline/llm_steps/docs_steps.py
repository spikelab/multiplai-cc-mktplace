"""The documentation phase — README/CHANGELOG/docs land in the build's own PR.

One agent runs after the TDD build (BuildPhase.DOCS_UPDATE) with the whole
build diff, the implementation notes, and Read/Write/Edit/Glob/Grep inside the
project. It discovers the project's own documents (`README*`, `CHANGELOG*`,
`docs/**`), updates the ones the diff made stale, and closes its report with a
REQUIRED `DOCS_IMPACT:` slot naming what it wrote.

A failed writer blocks completion because it may have left partial edits.
Documentation freshness remains advisory after a successful writer.
"""

from __future__ import annotations

from build_pipeline.budget import BudgetExceededError

import logging
from pathlib import Path

from ..gates import docs_freshness_gate, parse_docs_impact
from ..models import GateResult
from ..prompts.docs_update import DOCS_UPDATE_PROMPT
from ..sdk import agent_call

log = logging.getLogger(__name__)

DOCS_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
NOTES_FILENAME = "implementation-notes.md"


async def run_docs_update(config, state=None) -> tuple[list[str], GateResult]:
    """Update the project's documentation from the build diff.

    Returns ``(files the agent reported writing, the freshness gate's verdict)``.
    The gate never fails (see `gates.docs_freshness_gate`); the caller prints its
    warning and continues either way.
    """
    from ..tdd_engine import capture_build_diff

    diff = capture_build_diff(config, state) if state is not None else ""
    prompt = DOCS_UPDATE_PROMPT.format(
        change_name=getattr(config, "change_name", "") or config.project_dir.name,
        project_dir=config.project_dir,
        diff=diff or "(no diff could be captured for this build)",
        notes=_read_notes(config),
    )

    agent = await agent_call(
        prompt, allowed_tools=DOCS_TOOLS,
        model=config.model_for("docs_update"), effort=config.effort_for("docs_update"),
        cwd=str(config.project_dir), budget_label="docs_update",
    )
    if not agent.success:
        raise RuntimeError(f"Documentation writer failed: {agent.error or agent.output}")
    output = agent.output or ""

    files = parse_docs_impact(output)
    gate = docs_freshness_gate(diff, files, Path(config.project_dir))
    log.info(
        "DONE step=DOCS_UPDATE files=%s gate=%s",
        files if files else "none", gate.action or "ok",
    )
    return list(files or []), gate


def _read_notes(config) -> str:
    """The build's implementation notes, or a stated absence."""
    path = Path(config.change_dir) / NOTES_FILENAME
    if not path.exists():
        return "(no implementation notes were recorded during this build)"
    try:
        return path.read_text()
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
        return "(implementation notes could not be read)"
