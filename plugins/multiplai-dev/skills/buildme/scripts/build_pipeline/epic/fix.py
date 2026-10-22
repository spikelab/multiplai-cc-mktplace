"""What the coordinator runs to fix a reviewed child PR
(plan-buildme-epic-mode-2026-10-12.md, items 32-34).

A review of a child's head with blockers (open Code findings at HIGH or
MEDIUM, `review.blockers`) runs the fix loop, as two subprocesses with a pair
of records each:

1. `post`: `write_decisions(<review_dir>)` chooses what to post (what
   `--auto` posts, kept to what the epic blocks on: Code findings at
   `review.BLOCKING_SEVERITIES`), then `uv run --directory <review scripts>
   python -m review_pipeline post <review_dir> --decisions
   <review_dir>/buildme-decisions.json`, cwd the
   child's project directory. `post` skips a finding already on the PR, so a
   second post is safe.
2. `fix`: `python -m build_pipeline findings --pr <n> --change <epic>-<id>
   --project-dir <P> --auto --trust-repo --max-rounds <M> --severities
   HIGH,MEDIUM --code-only [--max-usd <left>]`, cwd `<P>`, the child's worktree joined
   with the project's offset. `<M>` is every round in the change's
   `.findings-state.json`, finished or not, plus one (`rounds_started`), so
   `findings`, which counts its own rounds from its state, runs one round and
   stops, and a decision follows every review
   (plan-review-rounds-and-finders-2026-10-15.md, amended by Spike on
   2026-10-15). A round an earlier fixer left open is finished first.
   `findings` resumes itself, so an interrupted one is run again with the
   `--max-rounds` it was launched with (`saved_rounds`); `findings` exits
   128 + the signal on SIGTERM or SIGHUP, which is a stop like a kill, not a
   failure. `--severities` and `--code-only` make its
   re-reviews post, so its rounds act on, only what the epic blocks on.
   `<left>` is `epic.max_usd` less the epic's spend at the launch and less
   what the fixers still running were given and have not yet spent on their
   PRs' reviews, rounded to 6 decimals (none without `epic.max_usd`; while
   they hold all of it, the fixer waits for them): `findings` stops before a
   round once its own spend and its reviews' have reached it and a fetch
   found something to act on, exit 0 with `FINDINGS:BUDGET:<rounds>:<usd>`.
   The coordinator takes that as the epic's budget reached when the epic's
   spend is at `epic.max_usd`; under it, the fixer used up only its share,
   and it runs again once the others end.

Then the coordinator's review step reads the PR's head again and reuses the
fixer's last review.

The final fix (plan-review-rounds-and-finders-2026-10-15.md, item 2): when
the review's recorded decision (`review.decide`) is `final_fix`, the same
`findings` runs with the same `<M>` and `--no-review`, so its fresh round
fetches the blockers just posted, fixes, pushes and replies, and nothing
reviews it. Any relaunch keeps the `--max-rounds` it was launched with
(`saved_rounds`): recomputed after its round started or finished, it would
run, and pay for, another. Its result keeps `final_head`, the HEAD it left, which the merge step takes as
the PR's approved head. A final fix whose fetch finds no new comment (the
blockers were already on the PR, so `post` skipped them) ends
`FINDINGS:DONE` with no round (`acted_on_nothing`); the PR still goes to the
merge step, as the decision chose no further review, and its issue says no
fix ran on what it lists. The steps that run these are `Coordinator._fix` and
`Coordinator._fix_pending` in `coordinator.py`.
"""

from __future__ import annotations

import json
import logging
import re
import signal
from pathlib import Path

from ..findings import decisions
from ..findings.cli import ResolveError, change_dir_for, change_name_for
from ..findings.decisions import DECISIONS_FILE
from ..findings.loop import STATE_FILE
from . import review

log = logging.getLogger(__name__)

# What `findings` prints when a step fails or refuses (`findings/loop.py`).
FAILED = re.compile(r"^FINDINGS:FAILED:([^:\n]*):(.*)$", re.MULTILINE)


def write_decisions(review_dir: str | Path) -> Path:
    """32.1: `findings.decisions.write_decisions`, keeping only the Code
    findings at the severities the epic blocks on (`review.blockers`), so the
    fixer acts on nothing the epic would merge with."""
    return decisions.write_decisions(review_dir, severities=review.BLOCKING_SEVERITIES, code_only=True)


def post_argv(review_dir: str) -> list[str]:
    return review._skill("post", review_dir, "--decisions", str(Path(review_dir) / DECISIONS_FILE))


def _state_rounds(project: Path, change: str) -> list:
    """The rounds in *change*'s `<change dir>/.findings-state.json`. The
    change directory is the one `findings` resolves, under the normalised
    change name. Empty when there is no change directory or no state file,
    or the file holds no list of rounds."""
    try:
        path = change_dir_for(project / "specs", change_name_for(change, "")) / STATE_FILE
    except (ResolveError, ValueError):
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return []
    found = doc.get("findings") if isinstance(doc, dict) else None
    rounds = found.get("rounds") if isinstance(found, dict) else None
    return rounds if isinstance(rounds, list) else []


def rounds_done(project: Path, change: str) -> int:
    """The fix rounds `findings` has finished for *change*: the rounds in its
    state (`_state_rounds`) with `review` among their finished steps."""
    return sum(1 for r in _state_rounds(project, change) if isinstance(r, dict)
               and isinstance(r.get("finished"), list) and "review" in r["finished"])


def rounds_started(project: Path, change: str) -> int:
    """Every round in *change*'s findings state, finished or not. `findings`
    resumes an open round first and then stops once this many rounds reach
    its `--max-rounds`, so a fresh round needs `--max-rounds` one more."""
    return len(_state_rounds(project, change))


# A stop, which the next run resumes, not a failure: `findings` turns SIGTERM
# and SIGHUP into SystemExit(128 + signal) (`build_pipeline/__main__.py`), and
# `uv run`, which runs the review and the post, reports a child killed by
# SIGKILL, SIGTERM or SIGHUP as 128 + signal.
STOPPED_EXITS = frozenset(128 + s for s in (signal.SIGHUP, signal.SIGKILL, signal.SIGTERM))

# What `findings` prints when it exits 0 (`findings/loop.py`): no new comment
# to act on, or its own round cap reached.
ENDED = re.compile(r"^FINDINGS:(?:DONE|MAX_ROUNDS):\d+$", re.MULTILINE)
# What it prints when it exits 0 at its `--max-usd` ceiling: a stop, not an end.
BUDGET = re.compile(r"^FINDINGS:BUDGET:\d+:[0-9.]+$", re.MULTILINE)


def stopped(code: int) -> bool:
    """Whether exit *code* is a fixer, review or post stopped by a signal:
    killed (< 0) or one of STOPPED_EXITS."""
    return code < 0 or code in STOPPED_EXITS


def ended(text: str) -> str | None:
    """The last `FINDINGS:DONE:<n>` or `FINDINGS:MAX_ROUNDS:<n>` line of a
    fixer's output; None without one."""
    hits = ENDED.findall(text)
    return hits[-1] if hits else None


def acted_on_nothing(text: str) -> bool:
    """Whether a fixer's output ends `FINDINGS:DONE`: its fetch found no new
    comment to act on, so it ran no round. A final fix that ran its round
    ends `FINDINGS:MAX_ROUNDS`, as its `--max-rounds` allows only that one."""
    end = ended(text)
    return end is not None and end.startswith("FINDINGS:DONE:")


def budget_stopped(text: str) -> bool:
    """Whether a fixer's output ends at its `--max-usd` ceiling: its last
    `FINDINGS:` end line is `FINDINGS:BUDGET`."""
    ends = [m for m in re.finditer(r"^FINDINGS:(?:DONE|MAX_ROUNDS|BUDGET):.*$", text, re.MULTILINE)]
    return bool(ends) and BUDGET.match(ends[-1].group(0)) is not None


def fix_argv(session_id: str, number: int, change: str, project: Path, max_rounds: int,
             max_usd: float | None = None, *, final: bool = False,
             accept_unknown_spend: bool = False) -> list[str]:
    """*final*: the final fix (`--no-review`), whose round gets no review."""
    argv = ["python", "-m", "build_pipeline", "--session-id", session_id, "findings", "--pr", str(number),
            "--change", change, "--project-dir", str(project), "--auto", "--trust-repo",
            "--max-rounds", str(max_rounds), "--severities", ",".join(review.BLOCKING_SEVERITIES),
            "--code-only", ESCALATION_OFF]
    if accept_unknown_spend:
        argv.append("--accept-unknown-spend")
    if final:
        argv.append(FINAL_FLAG)
    if max_usd is not None:
        argv += ["--max-usd", f"{max(max_usd, 0.0):.6f}"]
    return argv


FINAL_FLAG = "--no-review"
# Every fixer: its `--max-rounds` allows one round at a time, so each round
# would otherwise be "the last allowed round" and run the escalated fix.
ESCALATION_OFF = "--no-escalation"


def is_final(attempt_argv: list[str]) -> bool:
    """Whether a `fix` record's argv is a final fix's."""
    return FINAL_FLAG in attempt_argv


def pr_of(attempt_argv: list[str]) -> str:
    """The PR number a `fix` record was launched for."""
    return attempt_argv[attempt_argv.index("--pr") + 1]


def saved_rounds(attempt_argv: list[str]) -> int:
    """The `--max-rounds` a `fix` record was launched with."""
    return int(attempt_argv[attempt_argv.index("--max-rounds") + 1])


def failed_reason(text: str) -> str | None:
    """`the fixer failed at <step>: <reason>` from the last `FINDINGS:FAILED:` line; None without one."""
    hits = FAILED.findall(text)
    if not hits:
        return None
    step, reason = hits[-1]
    return f"the fixer failed at {step}: {reason.strip()}"
