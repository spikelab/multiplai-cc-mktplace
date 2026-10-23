"""The summary and the full walkthrough: what the Summary tab shows beside the measured badges.

The Claude Code session writes `summary.json` (the server never calls a model):
its verdicts on the commits and the tests, and the risk inputs. It holds no
explanation of the code. `summary put` checks it against the target the viewer
serves and then replaces `<mailbox>/../summary.json` atomically. The page polls
`/api/targets/<slug>/summary` for it.

The explanation of the code is the codebase-walkthrough HTML page, written only
when the user asks for it from the page. `full_walkthrough` says whether that
page exists for the served commit, in `<mailbox>/full-walkthrough/<head_sha>/`,
and gives the session the one command that writes it there.

The served target is read from `<mailbox>/target.json`, which `serve` writes
when it publishes: the findings file (or the in-memory one for a plain diff),
the PR metadata and the stale-review notice. It holds no secret.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from .gitdata import is_tree_review, tree_pathspec
from .mailbox import Mailbox, atomic_write
from .models import ASSESSMENT_TITLE_MAX, FindingsFile, Summary

# Assessments a summary must carry when it says it is complete.
MUST_ASSESS = ("commits", "tests")


def must_assess(ff: FindingsFile) -> tuple[str, ...]:
    """The assessments a complete summary of this target needs. A tree target
    (a module, or `review --tree`) is not a change: no commit says why, no test
    covers a change, and nothing is merged, so it needs none, and no risk."""
    return () if is_tree_review(ff.target) else MUST_ASSESS


# Topics a new summary may no longer use: size is measured, and risk is the
# header score built from `risk` (models.RiskInput).
RETIRED_TOPICS = {
    "size": "size is measured from git",
    "risk": "risk is scored from the `risk` block",
}
# A badge shows its title on one line (see models.ASSESSMENT_TITLE_MAX).
TITLE_MAX = ASSESSMENT_TITLE_MAX


def summary_path(box: Path) -> Path:
    return Path(box).parent / "summary.json"


def served_path(box: Path) -> Path:
    return Path(box) / "target.json"


@dataclass
class Served:
    findings: FindingsFile
    pr: dict | None = None
    notice: str | None = None
    stats: dict | None = None

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "findings": self.findings.model_dump(mode="json"),
                    "pr": self.pr,
                    "notice": self.notice,
                    "stats": self.stats,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )


def load_served(box: Path) -> Served:
    """The target `serve` wrote for this mailbox. Raises OSError or ValueError."""
    data = json.loads(served_path(box).read_text(encoding="utf-8"))
    return Served(
        FindingsFile.model_validate(data["findings"]),
        data.get("pr"),
        data.get("notice"),
        data.get("stats"),
    )


def load_summary(box: Path) -> Summary | None:
    path = summary_path(box)
    if not path.exists():
        return None
    return Summary.model_validate_json(path.read_text(encoding="utf-8"))


def check(sm: Summary, ff: FindingsFile) -> list[str]:
    """Every reason this summary cannot be shown for this target; empty when it can."""
    t = ff.target
    errors: list[str] = []
    if sm.base_sha != t.base_sha or sm.head_sha != t.head_sha:
        errors.append(
            f"summary: base_sha/head_sha {sm.base_sha[:12]}..{sm.head_sha[:12]} "
            f"are not the served target's {t.base_sha[:12]}..{t.head_sha[:12]}"
        )
    topics = [a.topic for a in sm.assessments]
    for a in sm.assessments:
        if a.topic in RETIRED_TOPICS:
            errors.append(
                f"assessment {a.topic}: not an assessment topic any more "
                f"({RETIRED_TOPICS[a.topic]})"
            )
        if len(a.title) > TITLE_MAX:
            errors.append(
                f"assessment {a.topic}: title is {len(a.title)} characters, over "
                f"{TITLE_MAX}; put the rest in detail_md"
            )
    for topic in sorted({t for t in topics if t != "other" and topics.count(t) > 1}):
        errors.append(f"assessment {topic}: more than one assessment has this topic")
    if sm.complete:
        for topic in must_assess(ff):
            if topic not in topics:
                errors.append(
                    f"summary: complete is true but there is no {topic!r} assessment"
                )
        if sm.risk is None and not is_tree_review(t):
            errors.append("summary: complete is true but there is no 'risk' block")
    return errors


def put(box: Path, text: str, ff: FindingsFile) -> tuple[Summary | None, list[str]]:
    """Parse and check *text*; when it passes, replace the summary file.
    Returns (the summary, []) or (None or the summary, errors)."""
    try:
        sm = Summary.model_validate_json(text)
    except ValueError as exc:
        return None, [f"summary: not a valid summary.json v1:\n{exc}"]
    errors = check(sm, ff)
    if errors:
        return sm, errors
    atomic_write(summary_path(box), sm.model_dump_json(indent=2) + "\n", mode=0o600)
    return sm, []


# --- the full walkthrough --------------------------------------------------------------------

# The walkthrough's spend ceiling for a run the page asks for: its own default
# (codebase-walkthrough SKILL.md), named in the page's dialog.
FULL_WALKTHROUGH_MAX_USD = 50
# A page is whole when its last bytes close the document: a write that stopped
# part-way leaves no `</html>` there, and the page counts as absent.
PAGE_TAIL_BYTES = 1024


def full_walkthrough_dir(box: Path, head_sha: str) -> Path:
    return Path(box) / "full-walkthrough" / head_sha


def full_walkthrough_runs(box: Path) -> Path:
    return Path(box) / "full-walkthrough" / "runs"


def _whole(page: Path) -> bool:
    try:
        with page.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - PAGE_TAIL_BYTES))
            return b"</html>" in fh.read()
    except OSError:
        return False


def pending_request(box: Path, slug: str) -> str | None:
    """The id of this target's `full_walkthrough` row with no final reply, or None."""
    mb = Mailbox(box)
    done: dict[str, bool] = {}
    for row in mb.read_outbox(0)[0]:
        done[row.get("reply_to")] = row.get("done") is True
    for row in mb.read_inbox():
        if (
            row.get("full_walkthrough") is True
            and row.get("target") == slug
            and not done.get(row.get("id"))
        ):
            return row.get("id")
    return None


def full_walkthrough(box: Path, served: Served, pr_number: int | None) -> dict:
    """Whether the full walkthrough page exists for the served commit, and how to write it.

    `state`, decided in this order: `ready` (exactly one whole `*-walkthrough.html` in `dir`),
    `unavailable` (more than one), `requested` (a `full_walkthrough` row has no final reply),
    `absent`. `kind` is `tree`, `pr` or `range`; `what` names the directory or the change;
    `command` is the walkthrough's argv after `python -m walkthrough_pipeline`; `run_dir` is the
    earlier run of this commit under `runs/`, or None (every run slug ends `<sha12>--human`).
    *pr_number* is `server.pr_number_of` for the target: a PR runs `--scope pr --pr N`.
    """
    t = served.findings.target
    box = Path(box)
    root = Path(t.repo_path)
    d = full_walkthrough_dir(box, t.head_sha)
    runs = full_walkthrough_runs(box)
    common = [
        "--trust-repo",
        "--repos",
        str(root),
        "--output",
        str(d),
        "--runs-dir",
        str(runs),
        "--max-usd",
        str(FULL_WALKTHROUGH_MAX_USD),
    ]
    if is_tree_review(t):
        spec = tree_pathspec(t)
        directory = root / spec[1] if spec else root
        kind, what = "tree", str(directory)
        command = ["run", str(directory), "--commit", t.head_sha, *common]
    elif pr_number is not None:
        kind, what = "pr", f"PR #{pr_number}"
        command = ["run", "--scope", "pr", "--pr", str(pr_number), "--repo", str(root)]
        command += ["--expect-head", t.head_sha, *common]
    else:
        kind, what = "range", f"{t.base_sha[:12]}..{t.head_sha[:12]}"
        command = ["run", "--scope", "pr", "--range", f"{t.base_sha}..{t.head_sha}"]
        command += ["--repo", str(root), "--expect-head", t.head_sha, *common]
    pages = sorted(p for p in d.glob("*-walkthrough.html") if p.is_file() and _whole(p))
    run_dirs = (
        sorted(p for p in runs.glob(f"*{t.head_sha[:12]}--human") if p.is_dir())
        if runs.is_dir()
        else []
    )
    out = {
        "state": "absent",
        "reason": None,
        "kind": kind,
        "what": what,
        "commit": t.head_sha,
        "dir": str(d),
        "page": None,
        "run_dir": str(run_dirs[0]) if run_dirs else None,
        "request_id": None,
        "command": command,
    }
    if len(pages) == 1:
        out.update(state="ready", page=str(pages[0]))
    elif len(pages) > 1:
        out.update(state="unavailable", reason=f"several pages in {d}")
    else:
        rid = pending_request(box, t.slug)
        if rid is not None:
            out.update(state="requested", request_id=rid)
    return out


def command_line(fw: dict) -> str:
    """The shell form of `full_walkthrough(...)["command"]`."""
    return shlex.join(fw["command"])
