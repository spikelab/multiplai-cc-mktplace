"""The epic directory: where it is, and the files in it (items 5 and 7).

`<data_dir>/buildme/epics/<owner>-<repo>--<epic>/`, where `<epic>` is the
parent build's change name and `<owner>-<repo>` comes from the review skill's
slug rule (`origin_owner_repo`, `slug_repo_name`, `sanitize_slug` in
`review_pipeline/target.py`), copied here and pinned by `tests/test_epic_split.py`.

- `epic.json` (`Epic`): the children with their plans, and `state`
  (`proposed`). Saved with
  `multiplai_core.checkpoint.save_model`, so a kill leaves the old or the new
  file, never half of one.
- `split.md`: the children, what each depends on, and which run in parallel.
- `plans/<id>.md`: one plan per child.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from multiplai_core import checkpoint
from multiplai_core.paths import get_paths
from pydantic import BaseModel, Field

from .split import EpicChild, waves

log = logging.getLogger(__name__)

EPIC_FILE = "epic.json"
SPLIT_FILE = "split.md"
PLANS_DIR = "plans"
TABLE_HEADER = ["id", "title", "depends on", "repo"]
NONE = "—"


class EpicFileError(Exception):
    """An epic file cannot be read, or fails the checks."""


class Epic(BaseModel):
    name: str
    state: Literal["proposed"] = "proposed"
    reason: str = ""
    children: list[EpicChild] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


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


def _remote_url(repo: Path) -> str | None:
    proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo,
                          capture_output=True, text=True, check=False)
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


# --- split.md and plans/ ----------------------------------------------------------

def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_split(epic: Epic) -> str:
    lines = [f"# Epic split: {epic.name}", ""]
    if epic.reason:
        lines += [epic.reason.strip(), ""]
    lines += ["| " + " | ".join(TABLE_HEADER) + " |", "|---|---|---|---|"]
    for c in epic.children:
        deps = ", ".join(c.depends_on) or NONE
        lines.append(f"| {_cell(c.id)} | {_cell(c.title)} | {_cell(deps)} | {_cell(c.repo or NONE)} |")
    lines += ["", "## Order", ""]
    for i, wave in enumerate(waves(epic.children), 1):
        how = "in parallel" if len(wave) > 1 else "alone"
        lines.append(f"{i}. {', '.join(wave)} ({how})")
    lines += ["", f"Each child's plan is `{PLANS_DIR}/<id>.md`.", ""]
    return "\n".join(lines)


def plan_path(epic_dir: Path, child_id: str) -> Path:
    return Path(epic_dir) / PLANS_DIR / f"{child_id}.md"


def write_split(epic_dir: Path, epic: Epic) -> None:
    (Path(epic_dir) / SPLIT_FILE).write_text(render_split(epic))


def write_plan(epic_dir: Path, child: EpicChild) -> None:
    path = plan_path(epic_dir, child.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(child.plan)


def write_files(epic_dir: Path, epic: Epic) -> None:
    """Render split.md and every plan from *epic* (epic.json is saved first, by the caller)."""
    write_split(epic_dir, epic)
    for c in epic.children:
        write_plan(epic_dir, c)
