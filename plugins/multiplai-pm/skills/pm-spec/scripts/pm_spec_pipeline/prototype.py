"""The prototype, the first step of stage 3: what the change will look like.

It runs after the components are approved and before the tickets and the
specs, in the change's `prototype/`. Files already there (the PM's own, or an
earlier run's) are kept and no agent runs. Otherwise one agent with `Read`,
`Write`, `Glob` and `Grep` makes whatever helps people picture the change:
screens copied from the current frontend, endpoint signatures, sample output,
or anything else. It shows only what things look like, with no behaviour and
no runnable code; the behaviour goes in the specs, which are reviewed together
with it. Its `NOTES.md` says what each file shows and the questions it raised.

Every path the agent reports writing is checked against `prototype/` in code,
and `prototype_gate` checks the result (one retry): a file besides `NOTES.md`,
and `NOTES.md`; `STATUS: BLOCKED` or `NEEDS_CONTEXT` fails without a retry.
`--no-prototype` / `prototype: {enabled: false}` turns the step off. Once the
step has passed or been skipped it never runs again for the change.

Files in `prototype/` are the PM's only when no agent has run. After a failed
agent run (`steps.prototype` blocked, row S15) the next run re-checks them with
`prototype_gate`: passed (a person fixed them), they are kept; `STATUS:
BLOCKED` or `NEEDS_CONTEXT`, it fails again; otherwise the agent runs again.

Copied from buildme's `llm_steps/prototype_steps.py` and `gates.py`
(`parse_prototype_notes`, `prototype_gate`), then adapted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import artifacts, layout, sdk
from . import findings as F
from .prompts import UNTRUSTED_NOTICE
from .prompts.prototype import PROTOTYPE_PROMPT
from .run import EXIT_FAILED, Run, Stop

log = logging.getLogger(__name__)

NAME = "prototype"
DIR = "prototype"
PROTOTYPE_TOOLS = ["Read", "Write", "Glob", "Grep"]
MAX_PROTOTYPE_ATTEMPTS = 2

_STATUS_RE = re.compile(
    r"^\s*(?:[-*]\s*|#{1,6}\s*|\*\*)?STATUS\*{0,2}\s*:\s*\**\s*([A-Za-z_]+)",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class Gate:
    passed: bool
    reason: str
    action: str = ""
    artifacts: list[str] = field(default_factory=list)


def existing_files(prototype_dir: Path) -> list[Path]:
    if not prototype_dir.is_dir():
        return []
    return [p for p in sorted(prototype_dir.rglob("*")) if p.is_file()]


def prototype_gate(prototype_dir: Path) -> Gate:
    notes_path = prototype_dir / "NOTES.md"
    if not prototype_dir.is_dir():
        return Gate(False, f"Prototype directory not created: {prototype_dir}", "retry")
    if not notes_path.is_file():
        return Gate(False, f"Prototype NOTES.md missing in {prototype_dir}", "retry")
    notes_text = notes_path.read_text()
    m = _STATUS_RE.search(notes_text)
    word = m.group(1).upper() if m else ""
    if word in ("BLOCKED", "NEEDS_CONTEXT"):
        return Gate(
            False,
            f"Prototype agent reported STATUS: {word}. Its notes:\n{notes_text[-1500:]}",
            "blocked",
        )
    found = [str(p) for p in existing_files(prototype_dir) if p != notes_path]
    if not found:
        return Gate(False, "Prototype produced no file besides NOTES.md", "retry")
    return Gate(
        True, f"Prototype produced {len(found)} file(s) and NOTES.md", "", found
    )


def files_outside(files_changed: list[str], prototype_dir: Path) -> list[str]:
    root = prototype_dir.resolve()
    outside = []
    for entry in files_changed or []:
        path = Path(entry)
        resolved = (path if path.is_absolute() else prototype_dir / path).resolve()
        if not resolved.is_relative_to(root):
            outside.append(str(resolved))
    return outside


def prototype_prompt(run: Run, prototype_dir: Path) -> str:
    change_dir = run.config.change_dir
    return PROTOTYPE_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        change_name=run.config.change_name,
        project_dir=run.config.project_dir,
        proposal_content=artifacts.proposal_text(run) or "(not available)",
        use_cases_content=artifacts.use_cases_text(run) or "(not available)",
        plans_content=layout.plans_text(change_dir),
        prototype_dir=prototype_dir,
    )


async def run_prototype(run: Run) -> Gate:
    prototype_dir = run.config.change_dir / DIR
    prototype_dir.mkdir(parents=True, exist_ok=True)
    prompt = prototype_prompt(run, prototype_dir)
    gate = Gate(False, "Prototype never ran")
    for attempt in range(1, MAX_PROTOTYPE_ATTEMPTS + 1):
        log.info("START step=PROTOTYPE attempt=%d/%d", attempt, MAX_PROTOTYPE_ATTEMPTS)
        try:
            answer = await sdk.agent_text(
                prompt,
                config=run.config,
                step="prototype",
                label="prototype",
                tools=PROTOTYPE_TOOLS,
                cwd=str(prototype_dir),
            )
        except sdk.CallFailed as e:
            return Gate(False, f"Prototype writer failed: {e}")
        if outside := files_outside(answer.files_changed, prototype_dir):
            return Gate(
                False,
                f"Prototype agent wrote outside {prototype_dir}: {outside}",
                "boundary",
            )
        gate = prototype_gate(prototype_dir)
        if gate.passed or gate.action == "blocked":
            return gate
        log.warning("Prototype gate failed (attempt %d): %s", attempt, gate.reason)
    return gate


def _fail(run: Run, reason: str) -> None:
    F.record_error(run, NAME, reason)
    print(f"ERROR: prototype: {reason}", flush=True)
    raise Stop(EXIT_FAILED, reason)


async def run_prototype_stage(run: Run) -> None:
    step = run.state.steps.get(NAME)
    if step and step.outcome in ("passed", "skipped"):
        return
    prototype_dir = run.config.change_dir / DIR
    if step and step.outcome == "blocked":
        # Row S15: an agent ran and failed; what is in prototype/ is its, not the PM's.
        gate = prototype_gate(prototype_dir)
        if gate.passed:
            F.record_passed(run, NAME, "", f"Checked again: {gate.reason}")
            run.progress("PROTOTYPE_PASSED", gate.reason)
            return
        if gate.action == "blocked":
            _fail(run, gate.reason)
    elif files := existing_files(prototype_dir):
        F.record_passed(
            run, NAME, "", f"Kept the {len(files)} file(s) already in prototype/"
        )
        log.info("SKIP phase=PROTOTYPE reason=prototype/ already holds files")
        return
    if run.state.options.prototype == "false":
        reason = "disabled (--no-prototype / prototype.enabled: false)"
        log.info("SKIP phase=PROTOTYPE reason=%s", reason)
        F.record_skipped(run, NAME, "", reason)
        return
    print("PHASE:PROTOTYPE", flush=True)
    gate = await run_prototype(run)
    if not gate.passed:
        _fail(run, gate.reason)
    F.record_passed(run, NAME, "", gate.reason)
    run.progress("PROTOTYPE_PASSED", gate.reason)
