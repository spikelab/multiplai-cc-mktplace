"""Fix each `code_fix` finding through the TDD engine a build uses.

The round's code fixes become `findings-r<N>-tasks.md`, one block per finding
in the `tasks.md` format `tdd_engine.parse_blocks` reads, whose first task is
the failing test that shows the defect. `run_tdd_engine` then runs those
blocks with every gate a build runs (RED, test hashing, refactor discard,
per-block review), pointed at the round's own files through three
`BuildConfig` overrides: `change_dir` (the change, archived or not),
`tasks_file` and `state_file` (`.findings-r<N>-state.json`). The build's own
`tasks.md` and `.build-state.json` are never read or written.

The block text comes from the triage agent's answer, never from the comment
body, and is defanged and flattened to one line per task.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

from multiplai_core.untrusted import defang

from .. import git_ops
from ..config import BuildConfig
from ..models import BlockInfo, BlockStatus
from ..state import BuildState
from .models import TriageRecord, Verdict

log = logging.getLogger(__name__)

Engine = Callable[..., Awaitable[int]]


def tasks_file_name(round_n: int) -> str:
    return f"findings-r{round_n}-tasks.md"


def engine_state_name(round_n: int) -> str:
    return f".findings-r{round_n}-state.json"


def code_fixes(records: list[TriageRecord]) -> list[TriageRecord]:
    """The records fixed with a failing test first, in comment order."""
    return [r for r in records if r.triage and r.triage.verdict == Verdict.CODE_FIX]


def _line(text: str, limit: int = 1200) -> str:
    """Model-written text as one defanged line (a task is one line)."""
    return " ".join(defang(text or "", limit).split())


def block_ident(record: TriageRecord) -> str:
    c = record.comment
    return f"finding {c.finding_id}" if c.finding_id else c.label


def render_tasks(round_n: int, records: list[TriageRecord]) -> tuple[str, dict[int, str]]:
    """(`findings-r<N>-tasks.md` text, block number → the finding's row key)."""
    out = [
        f"# Review findings, round {round_n}: code fixes",
        "",
        "One block per review comment triaged as a code defect. Each block starts "
        "with the failing test that shows the defect.",
        "",
    ]
    blocks: dict[int, str] = {}
    for i, r in enumerate(code_fixes(records), start=1):
        t = r.triage
        assert t is not None
        blocks[i] = r.comment.key
        failing = _line(t.failing_test) or _line(t.reason)
        where = ", ".join(_line(loc, 200) for loc in t.locations) or "see the reason"
        out += [
            f"## {i}. Fix {block_ident(r)}",
            "",
            f"- [ ] {i}.1 Write a failing test that shows the defect: {failing}",
            f"- [ ] {i}.2 Change the code so that test passes, and nothing else. "
            f"Why it is a defect: {_line(t.reason)} Code involved: {where}",
            "",
        ]
    return "\n".join(out), blocks


@dataclass
class CodeResult:
    exit_code: int
    blocks: dict[int, str] = field(default_factory=dict)       # block number → row key
    fixes: dict[str, dict[str, str]] = field(default_factory=dict)  # row key → {test, impl}


def round_config(config: BuildConfig, change_dir: Path, round_n: int) -> BuildConfig:
    """A copy of *config* whose engine paths are the round's own."""
    rc = copy.copy(config)
    rc.change_dir = change_dir
    rc.tasks_file = tasks_file_name(round_n)
    rc.state_file = engine_state_name(round_n)
    return rc


def write_tasks(change_dir: Path, round_n: int, records: list[TriageRecord]) -> tuple[Path, dict[int, str]]:
    text, blocks = render_tasks(round_n, records)
    path = Path(change_dir) / tasks_file_name(round_n)
    path.write_text(text)
    return path, blocks


def fixes_from_state(state_path: Path, blocks: dict[int, str]) -> dict[str, dict[str, str]]:
    """Row key → the block's test and impl commits, from the round's engine
    checkpoint. A block that is not done, or has no impl commit, is left out
    (not fixed): a failed block's commits are discarded (`failed_block`)."""
    if not state_path.exists():
        return {}
    state = BuildState.load(state_path)
    out: dict[str, dict[str, str]] = {}
    for b in (state.tdd.blocks if state.tdd else []):
        key = blocks.get(b.number)
        if key and b.impl_commit and b.status == BlockStatus.DONE:
            out[key] = {"test": b.test_commit or "", "impl": b.impl_commit,
                        "status": b.status.value}
    return out


async def run_code_step(config: BuildConfig, change_dir: Path, round_n: int,
                        records: list[TriageRecord], *, blocks: dict[int, str] | None = None,
                        engine: Engine | None = None) -> CodeResult:
    """Run the round's code-fix blocks through `run_tdd_engine` (standalone=False,
    so the round's checkpoint is kept for a resume and the fixing commits are
    read back from it). The tasks file must already exist; *blocks* is what
    `write_tasks` returned for it. With no code fix, nothing runs."""
    if blocks is None:
        _, blocks = render_tasks(round_n, records)
    if not blocks:
        return CodeResult(exit_code=0)
    if engine is None:
        from ..tdd_engine import run_tdd_engine as engine
    rc = round_config(config, change_dir, round_n)
    log.info("START findings_code round=%d blocks=%d tasks=%s state=%s",
             round_n, len(blocks), rc.tasks_path, rc.state_file_path())
    code = await engine(rc, SimpleNamespace(block=None), standalone=False)
    fixes = fixes_from_state(rc.state_file_path(), blocks)
    log.info("DONE findings_code round=%d exit=%d fixed=%d", round_n, code, len(fixes))
    return CodeResult(exit_code=code, blocks=blocks, fixes=fixes)


def failed_block(state_path: Path) -> BlockInfo | None:
    """The block the engine stopped on, when it stopped because that block
    failed: its RED gate, implementation or review. None when it stopped for
    anything else (an agent timeout, the budget, a gate before or after the
    blocks), which a re-run may get past."""
    if not state_path.exists():
        return None
    tdd = BuildState.load(state_path).tdd
    if not tdd or tdd.current_block >= len(tdd.blocks):
        return None
    block = tdd.blocks[tdd.current_block]
    if block.status != BlockStatus.FAILED or block.timed_out:
        return None
    return block


def skip_block(state_path: Path, number: int) -> bool:
    """Move the round's engine checkpoint past failed block *number*, so the
    next engine run goes on with the blocks after it. False when the
    checkpoint is not stopped on that block."""
    state = BuildState.load(state_path)
    tdd = state.tdd
    if not tdd or tdd.current_block >= len(tdd.blocks) or tdd.blocks[tdd.current_block].number != number:
        return False
    state.advance_block(state_path)
    return True


def commit_round_files(project_dir: Path, round_n: int, paths: list[Path]) -> str | None:
    """Commit the round's own records (`findings-r<N>.md`, the tasks file)
    before the engine runs, so its whole-tree block commits start clean."""
    existing = [str(p) for p in paths if Path(p).exists()]
    if not existing:
        return None
    return git_ops.commit_paths(
        Path(project_dir), f"chore(findings-r{round_n}): record the round's findings",
        existing + git_ops.BOOKKEEPING_EXCLUDES,
    )
