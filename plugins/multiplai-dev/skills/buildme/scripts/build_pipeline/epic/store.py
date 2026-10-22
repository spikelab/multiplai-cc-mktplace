"""The epic directory: where it is, and the file in it (items 5 and 7).

`<data_dir>/buildme/epics/<owner>-<repo>--<epic>/`, where `<epic>` is the
parent change's name and `<owner>-<repo>` comes from the review skill's
slug rule (`origin_owner_repo`, `slug_repo_name`, `sanitize_slug` in
`review_pipeline/target.py`), copied here and pinned by `tests/test_epic_store.py`.

`epic.json` (`Epic`) holds the children as the parent's and the children's
`.change.yaml` list them (`phase.run_epic_from_specs` sets them on every
run) and each child's status. Saved with `multiplai_core.checkpoint.save_model`,
so a kill leaves the old or the new file, never half of one. A file saved
before pm-spec fed the epic, with keys this model no longer has (the split's
`state`, `hashes`, plans, a child's `repo`), still loads: pydantic ignores
the extra keys.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from multiplai_core import checkpoint
from multiplai_core.paths import get_paths
from pydantic import BaseModel, Field

from .. import git_ops
from ..stop_record import StepStop

log = logging.getLogger(__name__)

EPIC_FILE = "epic.json"


class EpicFileError(Exception):
    """An epic file cannot be read, or fails the checks."""


class EpicChild(BaseModel):
    """One child: its id (its change is `<epic>-<id>`) and the ids of the
    children it depends on, from its `.change.yaml` `depends_on`."""

    id: str
    depends_on: list[str] = Field(default_factory=list)


class ChildStatus(BaseModel):
    """A child's status, rebuilt from the records at each start (PR B1):
    `pending`, `running`, `built` (PR open), `reviewing` (its PR's review
    runs, PR C), `waiting` (a dependency's PR is open), `merged`, `parked`,
    `skipped` (a dependency parked), `failed` or `unknown` (a `gh`, `git` or
    `ps` read about a stopped child failed). `pr_round` (the PR's review
    rounds, item 23) and `conflicts` (its last mergeability read) are cached
    here; the review ledger and `gh` are the truth. PR E: `merge_stop`, the
    tree of the child's worktree when its merge step last stopped (item 39),
    and `merge_park`, why its merge step parked it, which keeps it parked."""

    status: str = "pending"
    pr: str | None = None
    reason: str = ""
    pr_round: int | None = None
    conflicts: bool | None = None
    merge_stop: StepStop | None = None
    merge_park: str = ""


class Epic(BaseModel):
    name: str
    children: list[EpicChild] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    child_status: dict[str, ChildStatus] = Field(default_factory=dict)
    # PR B2: what the epic has spent, rebuilt from the records and the review
    # ledger at each save; incomplete when a spend could not be read.
    spent_usd: float = 0.0
    spend_complete: bool = True


# --- the directory name (copied from review_pipeline/target.py) ---------------

def origin_owner_repo(url: str | None) -> tuple[str, str] | None:
    """`(owner, repo)` from a remote URL (the last two path parts, `.git` dropped), else None."""
    if not url:
        return None
    path = url.strip().rstrip("/")
    if path.endswith(".git"):
        path = path[:-len(".git")]
    parts = [p for p in re.split(r"[/:]", path) if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


class GitUnreadable(Exception):
    """git timed out or did not start, so it said nothing about the checkout."""


def _git(argv: list[str], cwd: Path) -> git_ops.GitResult:
    """`git_ops._run`, raising GitUnreadable for its timeout (124) and for a
    git that did not start (127), which say nothing about *cwd*."""
    proc = git_ops._run(argv, cwd=cwd)
    if proc.returncode in (124, 127):
        raise GitUnreadable(f"{proc.command} in {cwd}: {proc.stderr.strip()}")
    return proc


def _remote_url(repo: Path) -> str | None:
    proc = _git(["git", "remote", "get-url", "origin"], cwd=repo)
    url = proc.stdout.strip()
    return url if proc.returncode == 0 and url else None


def slug_repo_name(repo: Path) -> str:
    """`<owner>-<repo>` from the `origin` remote; the directory name without one."""
    pair = origin_owner_repo(_remote_url(repo)) if repo.is_dir() else None
    if pair:
        return "-".join(pair)
    log.info("no origin remote in %s with an owner and a name: the epic directory uses the "
             "directory name %s", repo, repo.name)
    return repo.name


def sanitize_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return slug or "target"


def epic_dir_for(repo: Path, change_name: str) -> Path:
    """`<data_dir>/buildme/epics/<owner>-<repo>--<change>/`."""
    repo = Path(repo)
    return get_paths().data_dir / "buildme" / "epics" / sanitize_slug(f"{slug_repo_name(repo)}--{change_name}")


# --- epic.json -----------------------------------------------------------------

def load_epic(epic_dir: Path) -> Epic:
    """The saved epic. Raises EpicFileError when epic.json is missing or unparsable;
    the file is never rewritten for that."""
    path = Path(epic_dir) / EPIC_FILE
    if not path.is_file():
        raise EpicFileError(f"{path} is missing")
    try:
        return Epic.model_validate_json(path.read_text())
    except (OSError, ValueError) as e:
        raise EpicFileError(f"{path} cannot be read ({type(e).__name__}); fix or remove it by hand") from None


def save_epic(epic_dir: Path, epic: Epic) -> Path:
    return checkpoint.save_model(epic, epic_dir, EPIC_FILE)


def _write_atomic(path: Path, text: str) -> None:
    """Write through a temp file and os.replace, so a stop never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
