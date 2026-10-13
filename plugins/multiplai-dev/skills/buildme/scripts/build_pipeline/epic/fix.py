"""What the coordinator runs to fix a reviewed child PR
(plan-buildme-epic-mode-2026-10-12.md, items 32-34).

A review of a child's head with blockers (open Code findings at HIGH or
MEDIUM, `review.blockers`) runs the fix loop, as two subprocesses with a pair
of records each:

1. `post`: `findings.decisions.write_decisions(<review_dir>)` chooses what to
   post, then `uv run --directory <review scripts> python -m review_pipeline
   post <review_dir> --decisions <review_dir>/buildme-decisions.json`, cwd the
   child's project directory. `post` skips a finding already on the PR, so a
   second post is safe.
2. `fix`: `python -m build_pipeline findings --pr <n> --change <epic>-<id>
   --project-dir <P> --auto --trust-repo --max-rounds <M>`, cwd `<P>`, the
   child's worktree joined with the project's offset. `<M>` is the rounds in
   the change's `.findings-state.json` plus the review rounds the PR has left
   (`epic.max_review_rounds` less `pr_round`), so `findings`, which counts
   its own rounds from its state, stops where the epic's cap is. `findings`
   resumes itself, so an interrupted one is run again with the same command.

Then the coordinator's review step reads the PR's head again and reuses the
fixer's last review. The steps that run these are `Coordinator._fix` and
`Coordinator._fix_pending` in `coordinator.py`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..findings.cli import ResolveError, change_dir_for
from ..findings.decisions import DECISIONS_FILE
from ..findings.loop import STATE_FILE
from . import review

log = logging.getLogger(__name__)

# What `findings` prints when a step fails or refuses (`findings/loop.py`).
FAILED = re.compile(r"^FINDINGS:FAILED:([^:\n]*):(.*)$", re.MULTILINE)


def post_argv(review_dir: str) -> list[str]:
    return review._skill("post", review_dir, "--decisions", str(Path(review_dir) / DECISIONS_FILE))


def rounds_done(project: Path, change: str) -> int:
    """The fix rounds `findings` has started for *change*: the rounds in its
    `<change dir>/.findings-state.json`; 0 when there is no change directory
    or no state file, or the file holds no list of rounds."""
    try:
        path = change_dir_for(project / "specs", change) / STATE_FILE
    except ResolveError:
        return 0
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return 0
    rounds = (doc.get("findings") or {}).get("rounds") if isinstance(doc, dict) else None
    return len(rounds) if isinstance(rounds, list) else 0


def fix_argv(session_id: str, number: int, change: str, project: Path, max_rounds: int) -> list[str]:
    return ["python", "-m", "build_pipeline", "--session-id", session_id, "findings", "--pr", str(number),
            "--change", change, "--project-dir", str(project), "--auto", "--trust-repo",
            "--max-rounds", str(max_rounds)]


def failed_reason(text: str) -> str | None:
    """`the fixer failed at <step>: <reason>` from the last `FINDINGS:FAILED:` line; None without one."""
    hits = FAILED.findall(text)
    if not hits:
        return None
    step, reason = hits[-1]
    return f"the fixer failed at {step}: {reason.strip()}"
