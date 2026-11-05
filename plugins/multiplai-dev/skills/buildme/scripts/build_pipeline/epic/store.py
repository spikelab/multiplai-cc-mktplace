"""The epic directory: where it is, and the file in it (items 5 and 7).

`<data_dir>/changes/<repo>/<branch>/buildme/` (`multiplai_core.runs.change_folder`),
where `<branch>` is the parent change's feature branch (`<TICKET>-<change>`)
and `<repo>` is origin's repository name, as the review skill names its
`reviews/<repo>/` folders (`repo_folder`). The branch exists before the epic's
first file is written, so the folder never needs renaming.

Before multiplai-dev 0.156 the folder was `<data_dir>/buildme/epics/<owner>-<repo>--<epic>/`
(`legacy_epic_dir`, from the review skill's slug rule: `origin_owner_repo`,
`slug_repo_name`, `sanitize_slug` in `review_pipeline/target.py`, copied here
and pinned by `tests/test_epic_store.py`). `epic_dir_for` keeps using an
existing old folder until `python -m build_pipeline move-epics` moves it and
leaves a symlink in its place, so one epic's records never split in two.

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
from datetime import UTC, datetime
from pathlib import Path

from multiplai_core import checkpoint, runs
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
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
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
    path = path.removesuffix(".git")
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
    log.info(
        "no origin remote in %s with an owner and a name: the epic directory uses the "
        "directory name %s",
        repo,
        repo.name,
    )
    return repo.name


def sanitize_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return slug or "target"


def legacy_epic_dir(repo: Path, change_name: str) -> Path:
    """`<data_dir>/buildme/epics/<owner>-<repo>--<change>/`: where epics lived
    before multiplai-dev 0.156."""
    repo = Path(repo)
    return (
        get_paths().data_dir
        / "buildme"
        / "epics"
        / sanitize_slug(f"{slug_repo_name(repo)}--{change_name}")
    )


def repo_folder(repo: Path) -> str:
    """The `<repo>` part of the change folder: origin's repository name, else the
    directory name, as the review skill's `checkout_repo_part` names `reviews/<repo>/`."""
    repo = Path(repo)
    pair = origin_owner_repo(_remote_url(repo)) if repo.is_dir() else None
    return runs.folder_name(pair[1] if pair else repo.name)


class AmbiguousEpic(Exception):
    """Several change folders of one repository hold an epic of this name."""


def _epics_named(repo: Path, change_name: str) -> list[Path]:
    """Every `<data_dir>/changes/<repo>/*/buildme/` whose `epic.json` names *change_name*."""
    root = runs.changes_root() / repo_folder(repo)
    found = []
    for d in sorted(root.glob(f"*/buildme/{EPIC_FILE}")):
        try:
            name = load_epic(d.parent).name
        except EpicFileError:
            continue
        if name == change_name:
            found.append(d.parent)
    return found


def epic_dir_for(repo: Path, change_name: str, branch: str | None = None) -> Path:
    """Where the epic *change_name* of *repo* keeps its records (module docstring).

    With *branch* (the run that creates or runs the epic knows it), its change
    folder's `buildme/`, unless that does not exist yet and the old folder does:
    then the old one, until it is moved. Without *branch* (status, watch,
    preflight, the viewer), the one change folder holding an epic of that name;
    with none, the old folder. Raises AmbiguousEpic when two branches of the
    repository hold an epic of that name."""
    repo = Path(repo)
    legacy = legacy_epic_dir(repo, change_name)
    if branch:
        new = runs.change_folder(repo_folder(repo), branch) / "buildme"
        if not new.exists() and legacy.is_dir() and not legacy.is_symlink():
            return legacy
        return new
    found = _epics_named(repo, change_name)
    if len(found) > 1:
        raise AmbiguousEpic(
            f"several branches hold an epic {change_name}: "
            + ", ".join(str(d) for d in found)
            + "; name the feature branch"
        )
    return found[0] if found else legacy


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
        raise EpicFileError(
            f"{path} cannot be read ({type(e).__name__}); fix or remove it by hand"
        ) from None


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
