"""The two pieces of buildme this skill uses, imported from the sibling skill.

`build_pipeline.readiness.write_issues` is the one writer of
`spec-issues.md`, and `build_pipeline.epic.store.epic_dir_for` says where a
change's epic records live. buildme's scripts (`../../buildme/scripts`) are a
uv workspace member, and uv refuses a member that is also a `path` source,
while `workspace = true` cannot resolve in an installed plugin. So they are
not a declared dependency: this module puts that directory on `sys.path`
when the package is not already importable. Both modules need only
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
