"""The pieces of buildme this skill uses, imported from the sibling skill.

`build_pipeline.readiness.write_issues` is the one writer of
`spec-issues.md`, `build_pipeline.epic.store` says where a change's epic
records live (`epic_dir_for`, and the folder names `epic_dirs` lists), and
`build_pipeline.epic.causes.cause_of` sorts a child's reason text into a
cause for summaries written before buildme recorded one. buildme's scripts (`../../buildme/scripts`) are a
uv workspace member, and uv refuses a member that is also a `path` source,
while `workspace = true` cannot resolve in an installed plugin. So they are
not a declared dependency: this module puts that directory on `sys.path`
when the package is not already importable. These modules need only
multiplai-core, pydantic and the stdlib, which this package declares. The
directory sits beside this skill in the repo and in an installed copy of the
plugin alike.
"""

from __future__ import annotations

import sys
from pathlib import Path

BUILDME_SCRIPTS = Path(__file__).resolve().parents[3] / "buildme" / "scripts"


def _ensure_path() -> None:
    try:
        import build_pipeline  # noqa: F401
    except ImportError:
        if not BUILDME_SCRIPTS.is_dir():
            raise ImportError(
                f"pipeline-viewer needs buildme's scripts at {BUILDME_SCRIPTS}; "
                "install the whole multiplai-dev plugin"
            ) from None
        sys.path.append(str(BUILDME_SCRIPTS))


def write_issues(
    change_dir: Path, issues: list[tuple[str, str]], *, change: str
) -> Path:
    _ensure_path()
    from build_pipeline.readiness import write_issues as _write

    return _write(change_dir, issues, change=change)


def epic_dir_for(repo: Path, change: str) -> Path:
    _ensure_path()
    from build_pipeline.epic.store import epic_dir_for as _dir

    return _dir(repo, change)


def cause_of(status: str, reason: str) -> str | None:
    """buildme's `epic.causes.cause_of`: the cause of a child from its status
    and reason text, for a summary written before buildme recorded causes."""
    _ensure_path()
    from build_pipeline.epic.causes import cause_of as _cause

    return _cause(status, reason)


def repo_folder(repo: Path) -> str:
    """The `<repo>` of `<data_dir>/changes/<repo>/` and of the run rows' `target.repo`."""
    _ensure_path()
    from build_pipeline.epic.store import repo_folder as _folder

    return _folder(repo)


def legacy_prefix(repo: Path) -> str:
    """`<owner>-<repo>--`: how every old epic folder of *repo* starts
    (`store.legacy_epic_dir`)."""
    _ensure_path()
    from build_pipeline.epic.store import sanitize_slug, slug_repo_name

    return sanitize_slug(slug_repo_name(Path(repo))) + "--"


def epic_dirs(repo: Path, data_dir: Path) -> list[Path]:
    """Every epic folder of *repo* under *data_dir* holding `epic.json`: the
    old `buildme/epics/<owner>-<repo>--<change>/` ones, then the new
    `changes/<repo>/<branch>/buildme/` ones, each sorted. Old folders are not
    moved yet (a symlink left by a move is skipped: its target is listed)."""
    from multiplai_core import runs

    data_dir = Path(data_dir)
    old = sorted(
        d
        for d in (data_dir / "buildme" / "epics").glob(f"{legacy_prefix(repo)}*")
        if (d / "epic.json").is_file() and not d.is_symlink()
    )
    new = sorted(
        f.parent
        for f in (data_dir / runs.CHANGES_DIR / repo_folder(repo)).glob(
            "*/buildme/epic.json"
        )
    )
    return old + new


def scorecard_sources(repo: Path, data_dir: Path | None = None):
    """What `status.scorecard` reads for *repo*: its run rows, its epic
    folders and buildme's `cause_of`. *data_dir* defaults to multiplai's."""
    from multiplai_core import runs
    from multiplai_core.paths import get_paths

    from .status import ScorecardSources

    data_dir = Path(data_dir) if data_dir is not None else Path(get_paths().data_dir)
    return ScorecardSources(
        repo=repo_folder(repo),
        data_dir=data_dir,
        runs_file=data_dir / runs.RUNS_FILE,
        epic_dirs=epic_dirs(repo, data_dir),
        cause_of=cause_of,
    )


def merge_mode(repo: Path, branch: str | None) -> str:
    """How the epic on *branch* merges its children's PRs, as buildme decides
    (`Coordinator.__post_init__`): `epic.merge` in `specs/config.yaml`, else
    `manual` when *branch* is origin's default branch, else `auto`."""
    import yaml

    try:
        data = yaml.safe_load((Path(repo) / "specs" / "config.yaml").read_text()) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        data = {}
    epic = data.get("epic") if isinstance(data, dict) else None
    merge = epic.get("merge") if isinstance(epic, dict) else None
    if merge in ("auto", "manual"):
        return merge
    if merge is not None:
        return "manual"  # buildme's own fallback for a value it does not know
    _ensure_path()
    from build_pipeline.feature_branch import default_name

    return "manual" if branch and branch == default_name(Path(repo)) else "auto"
