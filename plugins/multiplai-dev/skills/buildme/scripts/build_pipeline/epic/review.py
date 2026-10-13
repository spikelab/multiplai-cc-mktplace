"""What the coordinator reads to review a child's PR
(plan-buildme-epic-mode-2026-10-12.md, items 21-24).

The review is the review skill's own `python -m review_pipeline review`, run
as a subprocess through `uv run --directory <review scripts>`, as the findings
stage runs it (`findings/loop.py`), because the skills' projects reach their
dependencies through uv path sources. buildme does not import
`review_pipeline`: it reads the review's ledger, `<data_dir>/reviews/ledger.jsonl`,
as a file. Each review appends a `started` line and, when it ends, a
`finished` line with `slug` (`<owner>-<repo>--pr-<n>`), `pr`, `head_sha`,
`base_sha`, `target_dir`, `round`, `stopped` (None for a complete review)
and `run` (`cost_usd`). A review stopped by a signal writes no `finished`
line.

The steps that run it are `Coordinator._review` in `coordinator.py`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

LEDGER = ("reviews", "ledger.jsonl")
REVIEW_WAIT = 60.0          # seconds between asks while another review of the PR runs
RUNNING_NOW = "is running now"
TAIL = 400                  # characters of a failed review's output kept as the park reason
BLOCKING_SEVERITIES = ("HIGH", "MEDIUM")
# What `review` prints (exit 4) when a review of this target and head started
# and never ended: `review_pipeline/__main__.py`, `InterruptedRun`.
INTERRUPTED = re.compile(r"interrupted run found; continue with: python -m review_pipeline resume (.+)$",
                         re.MULTILINE)


def review_skill_dir() -> Path:
    from ..findings.loop import _review_skill_dir

    return _review_skill_dir()


def _skill(*args: str) -> list[str]:
    return ["uv", "run", "--directory", str(review_skill_dir()), "python", "-m", "review_pipeline", *args]


def review_argv(worktree: Path, number: int) -> list[str]:
    return _skill("review", "--repo", str(worktree), "--pr", str(number), "--trust-repo")


def resume_argv(target_dir: str) -> list[str]:
    return _skill("resume", target_dir, "--trust-repo")


def ledger_lines() -> list[dict]:
    """Every readable line of the review ledger, in order; none when it is missing."""
    from multiplai_core.paths import get_paths

    path = Path(get_paths().data_dir).joinpath(*LEDGER)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("cannot read the review ledger %s: %s", path, e)
        return []
    out = []
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _complete(line: dict, repo_slug: str, number: int) -> bool:
    """A `finished` line of a complete review of PR *number* in this repo."""
    return (line.get("event") == "finished" and line.get("stopped") is None and line.get("pr") == number
            and str(line.get("slug") or "").startswith(repo_slug + "--"))


def pr_round(lines: list[dict], repo_slug: str, number: int) -> int:
    """Item 23: the number of distinct heads a complete review of this PR covered."""
    return len({line.get("head_sha") for line in lines if _complete(line, repo_slug, number)})


def finished_line(lines: list[dict], repo_slug: str, number: int, head: str) -> dict | None:
    """The last complete review of this PR at *head*, else None (item 24)."""
    hits = [line for line in lines if _complete(line, repo_slug, number) and line.get("head_sha") == head]
    return hits[-1] if hits else None


def conflicts(view: dict) -> bool | None:
    """From `gh pr view --json mergeable,mergeStateStatus`: True for a
    conflict, False when GitHub says it merges, None while it does not know.
    Recorded only: a conflict never blocks a review."""
    if view.get("mergeable") == "CONFLICTING" or view.get("mergeStateStatus") == "DIRTY":
        return True
    if view.get("mergeable") == "MERGEABLE":
        return False
    return None


def blockers(review_dir: Path | str) -> list[str] | None:
    """The open Code findings at HIGH or MEDIUM in `<review_dir>/findings.json`,
    one line each; None when the file cannot be read. "Open" is what the
    review lists (`findings.decisions._listed`, a copy of the review skill's
    rule)."""
    from ..findings.decisions import _listed, _section

    path = Path(review_dir) / "findings.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        findings, mode = doc["findings"], doc.get("mode")
        return [f"{f['severity']} {f['id']} {f.get('file') or '?'}:{f.get('line_start') or '?'} "
                f"{' '.join(str(f.get('claim') or '').split())[:160]}"
                for f in findings
                if _listed(f, mode) and _section(f) == "code" and f.get("severity") in BLOCKING_SEVERITIES]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("cannot read %s: %s", path, e)
        return None


def log_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def tail(text: str) -> str:
    return " ".join(text.split())[-TAIL:]


def findings_path(text: str) -> str | None:
    """The path on the last `findings: <path>` line a review printed."""
    paths = [line[len("findings: "):].strip() for line in text.splitlines() if line.startswith("findings: ")]
    return paths[-1] if paths else None
