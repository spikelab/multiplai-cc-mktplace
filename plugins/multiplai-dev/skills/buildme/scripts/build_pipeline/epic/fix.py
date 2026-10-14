"""What the coordinator runs to fix a reviewed child PR
(plan-buildme-epic-mode-2026-10-12.md, items 32-34).

A review of a child's head with blockers (open Code findings at HIGH or
MEDIUM, `review.blockers`) runs the fix loop, as two subprocesses with a pair
of records each:

1. `post`: `write_decisions(<review_dir>)` chooses what to post (what
   `--auto` posts, at the severities the epic blocks on,
   `review.BLOCKING_SEVERITIES`), then `uv run --directory <review scripts> python -m review_pipeline
   post <review_dir> --decisions <review_dir>/buildme-decisions.json`, cwd the
   child's project directory. `post` skips a finding already on the PR, so a
   second post is safe.
2. `fix`: `python -m build_pipeline findings --pr <n> --change <epic>-<id>
   --project-dir <P> --auto --trust-repo --max-rounds <M> --severities HIGH,MEDIUM
   [--max-usd <left>]`, cwd `<P>`, the
   child's worktree joined with the project's offset. `<M>` is the rounds in
   the change's `.findings-state.json` whose review finished plus the review
   rounds the PR has left (`epic.max_review_rounds` less `pr_round`), so
   `findings`, which counts its own rounds from its state, stops where the
   epic's cap is. A round still open (its review not yet in the ledger, so not
   in `pr_round`) is one of the rounds left: `findings` finishes it first.
   `findings` resumes itself, so an interrupted one is run again with the same
   command; `findings` exits 128 + the signal on SIGTERM or SIGHUP, which is a
   stop like a kill, not a failure. `--severities` makes its re-reviews post,
   so its rounds act on, only what the epic blocks on. `<left>` is
   `epic.max_usd` less the epic's spend at the launch (none without
   `epic.max_usd`): `findings` stops before a round once its own spend and its
   reviews' have reached it, exit 0 with `FINDINGS:BUDGET:<rounds>:<usd>`,
   and the coordinator takes that as the epic's budget reached.

Then the coordinator's review step reads the PR's head again and reuses the
fixer's last review. The steps that run these are `Coordinator._fix` and
`Coordinator._fix_pending` in `coordinator.py`.
"""

from __future__ import annotations

import json
import logging
import re
import signal
from pathlib import Path

from ..findings.cli import ResolveError, change_dir_for, change_name_for
from ..findings import decisions
from ..findings.decisions import DECISIONS_FILE
from ..findings.loop import STATE_FILE
from . import review

log = logging.getLogger(__name__)

# What `findings` prints when a step fails or refuses (`findings/loop.py`).
FAILED = re.compile(r"^FINDINGS:FAILED:([^:\n]*):(.*)$", re.MULTILINE)


def write_decisions(review_dir: str | Path) -> Path:
    """32.1: `findings.decisions.write_decisions`, keeping only the severities
    the epic blocks on, so the fixer acts on nothing the epic would merge with."""
    return decisions.write_decisions(review_dir, severities=review.BLOCKING_SEVERITIES)


def post_argv(review_dir: str) -> list[str]:
    return review._skill("post", review_dir, "--decisions", str(Path(review_dir) / DECISIONS_FILE))


def rounds_done(project: Path, change: str) -> int:
    """The fix rounds `findings` has finished for *change*: the rounds in its
    `<change dir>/.findings-state.json` with `review` among their finished
    steps. The change directory is the one `findings` resolves, under the
    normalised change name. 0 when there is no change directory or no state
    file, or the file holds no list of rounds."""
    try:
        path = change_dir_for(project / "specs", change_name_for(change, "")) / STATE_FILE
    except (ResolveError, ValueError):
        return 0
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return 0
    found = doc.get("findings") if isinstance(doc, dict) else None
    rounds = found.get("rounds") if isinstance(found, dict) else None
    if not isinstance(rounds, list):
        return 0
    return sum(1 for r in rounds if isinstance(r, dict) and isinstance(r.get("finished"), list)
               and "review" in r["finished"])


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


def budget_stopped(text: str) -> bool:
    """Whether a fixer's output ends at its `--max-usd` ceiling: its last
    `FINDINGS:` end line is `FINDINGS:BUDGET`."""
    ends = [m for m in re.finditer(r"^FINDINGS:(?:DONE|MAX_ROUNDS|BUDGET):.*$", text, re.MULTILINE)]
    return bool(ends) and BUDGET.match(ends[-1].group(0)) is not None


def fix_argv(session_id: str, number: int, change: str, project: Path, max_rounds: int,
             max_usd: float | None = None) -> list[str]:
    argv = ["python", "-m", "build_pipeline", "--session-id", session_id, "findings", "--pr", str(number),
            "--change", change, "--project-dir", str(project), "--auto", "--trust-repo",
            "--max-rounds", str(max_rounds), "--severities", ",".join(review.BLOCKING_SEVERITIES)]
    if max_usd is not None:
        argv += ["--max-usd", f"{max(max_usd, 0.0):.6f}"]
    return argv


def failed_reason(text: str) -> str | None:
    """`the fixer failed at <step>: <reason>` from the last `FINDINGS:FAILED:` line; None without one."""
    hits = FAILED.findall(text)
    if not hits:
        return None
    step, reason = hits[-1]
    return f"the fixer failed at {step}: {reason.strip()}"
