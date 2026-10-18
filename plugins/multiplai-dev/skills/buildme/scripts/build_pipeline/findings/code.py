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
body, and is defanged and flattened to one line per task. Each fix block asks
the fixer to look for the same mistake elsewhere and to say where it looked
in a `SEARCHED:` section, which the reply quotes (`searched_text`).

Once the blocks pass, one read-only agent checks the round's own commits
(`run_self_check`). A problem it lists becomes one more block, after the last
fix, and its report is kept as `findings-r<N>-self-check.md`.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

from multiplai_core.untrusted import defang, fence

from .. import budget, git_ops
from ..config import BuildConfig
from ..gates import parse_searched
from ..llm_steps.spec_steps import AUDITOR_MAX_TURNS, AUDITOR_TOOLS
from ..models import BlockInfo, BlockStatus
from ..prompts.findings import SELF_CHECK_PROMPT
from ..sdk import agent_call
from ..state import BuildState
from .models import TriageRecord, Verdict

log = logging.getLogger(__name__)

Engine = Callable[..., Awaitable[int]]


def tasks_file_name(round_n: int) -> str:
    return f"findings-r{round_n}-tasks.md"


def engine_state_name(round_n: int) -> str:
    return f".findings-r{round_n}-state.json"


def self_check_file_name(round_n: int) -> str:
    return f"findings-r{round_n}-self-check.md"


def code_fixes(records: list[TriageRecord]) -> list[TriageRecord]:
    """The records fixed with a failing test first, in comment order."""
    return [r for r in records if r.triage and r.triage.verdict == Verdict.CODE_FIX]


def _line(text: str, limit: int = 1200) -> str:
    """Model-written text as one defanged line (a task is one line)."""
    return " ".join(defang(text or "", limit).split())


def block_ident(record: TriageRecord) -> str:
    c = record.comment
    return f"finding {c.finding_id}" if c.finding_id else c.label


def render_tasks(round_n: int, records: list[TriageRecord],
                 self_check: dict | None = None) -> tuple[str, dict[int, str]]:
    """(`findings-r<N>-tasks.md` text, block number → the finding's row key).
    When *self_check* (`FindingsRound.self_check`) names a block, the check's
    report is that block, after the last fix, keyed `block:<k>`."""
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
            f"- [ ] {i}.2 Change the code so that test passes. "
            f"Why it is a defect: {_line(t.reason)} Code involved: {where}",
            f"- [ ] {i}.3 Search the same function, its other branches, its callers and the "
            "rest of the PR's diff for the same mistake, and fix each one you find; change "
            "nothing else. Close your report with a `SEARCHED:` section, after the other "
            "slots and before any test-change declaration: one line per finding "
            "saying where you looked and what you found.",
            "",
        ]
    k = (self_check or {}).get("block")
    if k:
        report = _line(self_check.get("report", ""), 4000)
        blocks[k] = f"block:{k}"
        out += [
            f"## {k}. Fix what the check of this round's fixes found",
            "",
            f"- [ ] {k}.1 Write a failing test that shows each problem the check found: {report}",
            f"- [ ] {k}.2 Change the code so those tests pass, and nothing else.",
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


def write_tasks(change_dir: Path, round_n: int, records: list[TriageRecord],
                self_check: dict | None = None) -> tuple[Path, dict[int, str]]:
    text, blocks = render_tasks(round_n, records, self_check)
    path = Path(change_dir) / tasks_file_name(round_n)
    path.write_text(text)
    return path, blocks


def write_self_check(change_dir: Path, round_n: int, self_check: dict | None) -> Path | None:
    """`findings-r<N>-self-check.md` from the saved check, or None (nothing
    written) when the check found no problem or has not run."""
    report = (self_check or {}).get("report") or ""
    if not report:
        return None
    path = Path(change_dir) / self_check_file_name(round_n)
    path.write_text(f"# Check of round {round_n}'s fixes\n\n"
                    f"Commits checked: up to `{self_check.get('head') or ''}`\n\n"
                    f"{report.strip()}\n")
    return path


# The engine appends each review-fix agent's report, and the fix on a passed
# block's, to the implementer's (`tdd_engine`, `[review-fix <n>]`,
# `[fix on pass]`). Those agents end with their own `SEARCHED:` section, about
# the block reviewer's findings, not the PR comment the fixer was given.
_LATER_AGENT = re.compile(r"\n\[(review-fix (\d+)|fix on pass)\]\n")


def searched_text(report: str, first_later: str | None = None) -> str:
    """The fixer's own `SEARCHED:` section (the last one before any later
    agent's part) of a block's implementer report, as one line
    (`gates.parse_searched`'s entries joined), or "" with a warning when it has
    none. *first_later* is the marker of the first later agent that ran on the
    block (`review-fix 1`, or `fix on pass` with no review fix), None when none
    ran. The engine keeps only the report's last `MAX_EVIDENCE_CHARS`: in a
    report that long, unless the first marker left is *first_later*'s, a later
    agent's output may have cut the fixer's part away, and "" is safer than
    another agent's search. With no later agent, the trim kept the end of the
    fixer's own output, where its section is. Recorded, never checked: code
    cannot tell whether a search happened."""
    from ..tdd_engine import MAX_EVIDENCE_CHARS

    report = report or ""
    first = _LATER_AGENT.search(report)
    lost = (first_later is not None and len(report) >= MAX_EVIDENCE_CHARS
            and (first is None or first.group(1) != first_later))
    entries = None if lost else parse_searched(report[:first.start()] if first else report)
    if entries is None:
        log.warning("the fixer's report has no SEARCHED: section")
        return ""
    return "; ".join(entries)


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
            # A review fix ran when a second review did.
            first_later = ("review-fix 1" if b.review_iterations > 1
                           else "fix on pass" if b.fix_on_pass_kept else None)
            out[key] = {"test": b.test_commit or "", "impl": b.impl_commit,
                        "status": b.status.value,
                        "searched": searched_text(b.implementer_report, first_later)}
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
    # The budget is one module-level counter, and `--auto` runs several rounds
    # in one process. Each engine run is held to the ceiling on this round's
    # own engine spend: clear the counter here, and the engine restores the
    # spend the round's checkpoint holds when there is one (a resumed round),
    # or starts from zero (a new round). Triage's and spec's agent calls are
    # not counted against it: no checkpoint records them, so a round resumed
    # in a new process could not count them either, and the ceiling would
    # again depend on how many rounds ran in the process.
    budget.reset()
    log.info("START findings_code round=%d blocks=%d tasks=%s state=%s",
             round_n, len(blocks), rc.tasks_path, rc.state_file_path())
    code = await engine(rc, SimpleNamespace(block=None), standalone=False)
    fixes = fixes_from_state(rc.state_file_path(), blocks)
    log.info("DONE findings_code round=%d exit=%d fixed=%d", round_n, code, len(fixes))
    return CodeResult(exit_code=code, blocks=blocks, fixes=fixes)


def add_block(state_path: Path, tasks_path: Path, number: int) -> None:
    """Append block *number* of the tasks file to the round's engine
    checkpoint as not started, unless it is there: the engine reads the tasks
    file only when its checkpoint has no blocks. Keeps the checkpoint's spend,
    as `restart_block` does. Does nothing before the engine has a checkpoint
    (its first run reads the whole tasks file)."""
    if not Path(state_path).exists():
        return
    state = BuildState.load(state_path)
    if state.tdd is None or any(b.number == number for b in state.tdd.blocks):
        return
    from ..tdd_engine import parse_blocks
    block = next((b for b in parse_blocks(Path(tasks_path)) if b.number == number), None)
    if block is None:
        raise ValueError(f"block {number} is not in {tasks_path}")
    state.tdd.blocks.append(block)
    state.checkpoint(state_path, keep_budget=True)


# The check's answer ends with one of two marker lines (`SELF_CHECK_PROMPT`):
# `NO PROBLEMS` (any wording that starts so), or `PROBLEMS:` and the list.
# Its output joins the text of every turn, so narration may come first: the
# last marker decides. A marker starts in a line's first column, with at most
# a heading `#` or emphasis around it, so a list item ("- No problems are
# handled for b=0", "- Problem: mul() overflows") or an item's indented
# continuation is never one.
_CHECK_MARKER = re.compile(
    r"^(?:#+[ \t]*)?[*_`]*(?:(?P<none>no problems?\b)|problems[*_`]*[ \t]*:)",
    re.IGNORECASE | re.MULTILINE)

# A list that says only "none" or "nothing", optionally "found", with
# punctuation at most.
_NONE = re.compile(r"[^\w\n]*(?:none|nothing)(?:[ \t]+found)?[^\w\n]*", re.IGNORECASE)

_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S", re.MULTILINE)


def _check_report(text: str) -> str:
    """The problems a check answer lists, or "" for none. An answer with no
    marker, or with list items after its `NO PROBLEMS`, is kept whole: the
    agent wrote something, and dropping it could drop a problem."""
    last = None
    for last in _CHECK_MARKER.finditer(text):
        pass
    if last is None:
        return text
    if last.group("none"):
        return text if _LIST_ITEM.search(text, last.end()) else ""
    listed = text[last.end():].strip()
    if _NONE.fullmatch(listed):
        return ""
    return listed or text


# Enough of the round's diff to judge; the agent opens the rest with Read.
MAX_SELF_CHECK_DIFF_CHARS = 150_000


async def run_self_check(config: BuildConfig, diff: str) -> str:
    """One read-only agent (`AUDITOR_TOOLS`) over the round's fix commits.
    Returns the problems it lists, or "" when it found none. Raises when the
    agent failed, so the code step stops and a resume asks again."""
    if len(diff) > MAX_SELF_CHECK_DIFF_CHARS:
        diff = diff[:MAX_SELF_CHECK_DIFF_CHARS] + "\n... (diff truncated)"
    prompt = SELF_CHECK_PROMPT.format(
        change_name=config.change_name,
        fenced_diff="\n".join(fence(diff, "git diff of this round's fix commits")) or "(empty diff)",
    )
    result = await agent_call(
        prompt,
        allowed_tools=AUDITOR_TOOLS,
        model=config.model_for('findings_self_check'),
        effort=config.effort_for('findings_self_check'),
        max_turns=AUDITOR_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="findings_self_check",
    )
    if not result.success:
        raise RuntimeError(f"the self-check agent failed: {' '.join((result.error or '').split())[:300]}")
    return _check_report((result.output or "").strip())


def failed_block(state_path: Path) -> BlockInfo | None:
    """The block the engine stopped on, when it stopped because that block
    failed: its RED gate, implementation or review. None when it stopped for
    anything else (an agent call that timed out or errored, the budget, a gate
    before or after the blocks), which a re-run may get past."""
    if not state_path.exists():
        return None
    tdd = BuildState.load(state_path).tdd
    if not tdd or tdd.current_block >= len(tdd.blocks):
        return None
    block = tdd.blocks[tdd.current_block]
    if block.status != BlockStatus.FAILED or block.timed_out or block.resumable:
        return None
    return block


def stopped_block(state_path: Path) -> BlockInfo | None:
    """The block the engine was in when it last stopped, by any cause (a
    resumable stop, a kill), or None when it stopped outside the blocks."""
    if not state_path.exists():
        return None
    tdd = BuildState.load(state_path).tdd
    if not tdd or tdd.current_block >= len(tdd.blocks):
        return None
    block = tdd.blocks[tdd.current_block]
    return None if block.status == BlockStatus.DONE else block


def restart_block(state_path: Path, number: int) -> None:
    """Set block *number* in the round's engine checkpoint back to not
    started, after its work was discarded: the engine then stamps a new
    start commit and keeps no commit that left the branch. A fix on pass in
    flight is dropped with them: its checkpoint left the branch too, so the
    engine builds the block again instead of rolling back to it, and the
    test-change claims of the discarded attempt describe no test that is
    left. The spend the checkpoint holds is kept: this may run in a new
    process, whose budget is empty, and the resumed engine restores its
    budget from it."""
    state = BuildState.load(state_path)
    for b in (state.tdd.blocks if state.tdd else []):
        if b.number == number:
            b.status = BlockStatus.PENDING
            b.baseline_commit = b.test_commit = b.impl_commit = b.refactor_commit = None
            b.timed_out = b.resumable = False
            b.fix_pass = None
            b.test_change_claims = []
    state.checkpoint(state_path, keep_budget=True)


def skip_block(state_path: Path, number: int) -> bool:
    """Move the round's engine checkpoint past failed block *number*, so the
    next engine run goes on with the blocks after it. False when the
    checkpoint is not stopped on that block. Keeps the checkpoint's spend,
    as `restart_block` does."""
    state = BuildState.load(state_path)
    tdd = state.tdd
    if not tdd or tdd.current_block >= len(tdd.blocks) or tdd.blocks[tdd.current_block].number != number:
        return False
    tdd.current_block += 1
    state.checkpoint(state_path, keep_budget=True)
    return True


def stopped_after_blocks(state_path: Path) -> str | None:
    """Why the engine stopped after its block loop, or None when it stopped
    before or inside it (or there is no checkpoint). Past the loop, every
    block is done or was failed, answered "not fixed" and skipped; what
    stopped the engine is a whole-change step it runs after the blocks: a
    refactor pass, which stops only on the budget, or mutation testing. Each
    done block's fix has by then passed its own RED, test-hash and review
    gates and is committed."""
    if not state_path.exists():
        return None
    tdd = BuildState.load(state_path).tdd
    if not tdd or not tdd.blocks or tdd.current_block < len(tdd.blocks):
        return None
    if not tdd.refactor_all_done:
        return "the budget stopped the whole-change refactor"
    if not tdd.mutation_done:
        return ("mutation testing failed (a score below mutation.min_score, or cosmic-ray "
                "could not score the changed lines; the progress log says which)")
    return "a whole-change check failed"


def baseline_red(state_path: Path) -> bool:
    """True when the engine stopped at its baseline gate: the suite failed
    before any block ran."""
    if not state_path.exists():
        return False
    tdd = BuildState.load(state_path).tdd
    return bool(tdd) and not tdd.baseline_tests_pass


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
