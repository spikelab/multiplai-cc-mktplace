"""The source of the reviewed repository's git dependencies, inside the snapshot.

A finding often turns on what a dependency does with a value the change
passes it. The agents read the snapshot only, so `materialize` extracts each
git-sourced package that a `uv.lock` at head pins, at that pinned commit, into
`<snapshot>/.review-deps/<name>@<commit[:12]>/`. The gates re-read a
dependency citation from the clone cache's git objects, never from the
extracted files.

Registry, `path` and `editable` sources, and other ecosystems' lock files, are
not read.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass

from multiplai_core.citations import GIT_ARGV, git_env

from .models import TargetInfo
from .target import git

log = logging.getLogger(__name__)

LOCK_FILE = "uv.lock"
_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Dep:
    name: str
    url: str  # without the `?branch=…` query and the `#<commit>` fragment
    commit: str  # 40 hex


def _parse_source(value: str) -> tuple[str, str]:
    """(url, commit) from a uv.lock git source, `<url>[?<query>]#<commit>`."""
    url, _, commit = value.partition("#")
    return url.split("?", 1)[0], commit


def locked_git_deps(target: TargetInfo) -> list[Dep]:
    """One `Dep` per git-sourced package in every uv.lock at head, by (url, commit) once."""
    listing = git(target.repo_path, "ls-tree", "-r", "--name-only", "-z", target.head_sha, check=False)
    paths = [p for p in listing.stdout.split("\0") if p.rsplit("/", 1)[-1] == LOCK_FILE]
    seen: set[tuple[str, str]] = set()
    out: list[Dep] = []
    for path in sorted(paths):
        shown = git(target.repo_path, "show", f"{target.head_sha}:{path}", check=False)
        if shown.returncode != 0:
            continue
        try:
            data = tomllib.loads(shown.stdout)
        except tomllib.TOMLDecodeError as e:
            log.info("%s is not TOML; its dependencies are not read: %s", path, e)
            continue
        for package in data.get("package") or []:
            source = package.get("source") if isinstance(package, dict) else None
            if not isinstance(source, dict) or not isinstance(source.get("git"), str):
                continue
            url, commit = _parse_source(source["git"])
            name = str(package.get("name") or "")
            if not _COMMIT.fullmatch(commit):
                log.info("%s: %s pins %r, not a 40-hex commit; skipped", path, name, commit)
                continue
            if not name or (url, commit) in seen:
                continue
            seen.add((url, commit))
            out.append(Dep(name, url, commit))
    return out
