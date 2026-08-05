#!/usr/bin/env python3
"""Drift-guard: one environment, and nothing that could quietly become a second.

Why this gate exists, concretely. On 2026-08-04 this repo carried four `.venv`
directories totalling 915MB. Two were legitimate (buildme, deep-research, each
with its own project). Two were hand-made — `plugins/multiplai-context/.venv`
(30MB) and `plugins/multiplai-media/skills/screen-demo/.venv` (229MB, created
by a since-retired `bootstrap.sh`). All four were gitignored, so `git status`
was clean and nobody noticed for months.

The consolidation onto a single uv workspace removes the *reason* to make a
stray venv, and uv itself removes the *ability* for anything declared as a
workspace member: invoked from anywhere inside a member directory, uv walks up
to the workspace root and uses the one environment there. It cannot create a
local one.

That leaves exactly two holes, and this file closes them:

  1. A `pyproject.toml` that is NOT a declared member. uv does not warn about
     this — it silently gives that directory its own `.venv`. Checked by
     `check_members`, and it works in CI because a pyproject.toml is committed.

  2. A `.venv` outside the workspace root, however it got there (`python -m
     venv`, `uv venv`, a bootstrap script). Checked by `check_no_stray_venvs`.
     This one only works run locally as a pre-commit hook: stray venvs are
     gitignored, so CI on a fresh clone will never see one.

Nothing can prevent someone typing `python -m venv .venv` in a skill directory.
The point of check 2 is to turn that from a silent multi-hundred-megabyte
accretion into a failure at the next commit.

Exit 0 clean, 1 on any finding. Deterministic and offline, like the repo's
other gates.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"

# An actual PEP 723 block: `# /// script` on its own line, a `# dependencies`
# key, and a closing `# ///`. Deliberately stricter than a substring search.
_PEP723_BLOCK = re.compile(
    r"^# /// script\s*$\n(?:#.*\n)*?^#\s*dependencies\s*=(?:#.*\n|.*\n)*?^# ///\s*$",
    re.M,
)


def _declared_members() -> set[Path]:
    """Workspace member directories, as absolute paths."""
    meta = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
    members = meta.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    return {(REPO_ROOT / m).resolve() for m in members}


def _found_pyprojects() -> list[Path]:
    """Every pyproject.toml under plugins/, excluding anything inside a venv."""
    return sorted(
        p for p in REPO_ROOT.glob("plugins/**/pyproject.toml")
        if ".venv" not in p.parts
    )


def check_members() -> list[str]:
    """Every pyproject.toml under plugins/ must be a declared workspace member.

    An undeclared one is not a style problem: uv gives it its own environment,
    which is precisely the state this repo is moving away from.
    """
    declared = _declared_members()
    return [
        f"{p.parent.relative_to(REPO_ROOT)} has a pyproject.toml but is not in "
        f"[tool.uv.workspace] members — uv will give it its own .venv. "
        f"Add it to pyproject.toml, or delete the file."
        for p in _found_pyprojects()
        if p.parent.resolve() not in declared
    ]


def check_members_exist() -> list[str]:
    """Every declared member must actually exist and carry a pyproject.toml.

    Guards the opposite drift: a member left in the list after its directory is
    renamed or removed makes `uv lock` fail with a much less obvious message.
    """
    problems = []
    for m in sorted(_declared_members()):
        rel = m.relative_to(REPO_ROOT)
        if not m.is_dir():
            problems.append(f"workspace member {rel} does not exist")
        elif not (m / "pyproject.toml").is_file():
            problems.append(f"workspace member {rel} has no pyproject.toml")
    return problems


def check_no_stray_venvs() -> list[str]:
    """There is one environment, at the workspace root. Any other is a bug.

    Only meaningful when run locally — these are gitignored, so a CI checkout
    never has them. That is why this gate belongs in pre-commit, not just CI.
    """
    return [
        f"stray virtualenv at {v.relative_to(REPO_ROOT)} "
        f"({sum(f.stat().st_size for f in v.rglob('*') if f.is_file()) // 1_000_000}MB) — "
        f"the workspace has one environment at the repo root. Delete it; if its "
        f"scripts need dependencies, declare them in a member pyproject.toml."
        for v in sorted(REPO_ROOT.glob("plugins/**/.venv"))
        if v.is_dir()
    ]


def check_no_nested_locks() -> list[str]:
    """One workspace, one uv.lock — at the repo root, and nowhere else.

    This is not tidiness. Two locks under `plugins/` survived the workspace
    consolidation and went stale, and `uv` cannot regenerate them: run `uv lock`
    from a member directory and it walks up to the workspace root, resolves the
    whole graph and rewrites the *root* lock, leaving the nested one untouched.
    So a nested lock is frozen at whatever it held the day it was orphaned, and
    nothing — not uv, not Dependabot, not CI — can move it.

    The cost landed on users. An installed plugin is a copy of the plugin
    subtree with no workspace root above it, so `uv run --project <member-dir>`
    finds the nested lock and resolves from it. Both orphans still pinned
    cryptography 49.0.0 (CVE-2026-69247, high) months after the root lock had
    moved to 50.0.0; deleting them drops the resolve straight to 50.0.0.

    Nothing in the previous gate suite had an opinion about lockfile *location*,
    which is exactly why this went unnoticed.
    """
    return [
        f"nested lockfile at {p.relative_to(REPO_ROOT)} — the workspace has one "
        f"uv.lock at the repo root. `uv lock` from a member directory rewrites "
        f"the root lock, never this one, so it can only ever go stale; an "
        f"installed plugin would then resolve from it. Delete it."
        for p in sorted(REPO_ROOT.glob("plugins/**/uv.lock"))
        if ".venv" not in p.parts
    ]


def check_no_pep723() -> list[str]:
    """No script may reintroduce a PEP 723 dependency block.

    PEP 723 is the right tool for a standalone single-file script, and the
    wrong one here: `uv run` re-resolves those dependencies on every
    invocation, which is what made the UserPromptSubmit hooks take 12-68s and
    time out. Dependencies belong in a member pyproject.toml.
    """
    problems = []
    for p in sorted(REPO_ROOT.glob("plugins/**/*.py")):
        if ".venv" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        # A real block only — anchored at line start, opened and closed, with a
        # dependencies key inside. A substring search for "# /// script" also
        # matches test assertions and fixtures that quote the marker (there are
        # four such files), which would make this gate cry wolf until someone
        # disabled it.
        if _PEP723_BLOCK.search(text):
            problems.append(
                f"{p.relative_to(REPO_ROOT)} carries a PEP 723 dependency block — "
                f"declare it in the nearest member pyproject.toml instead"
            )
    return problems


CHECKS = (
    ("undeclared workspace members", check_members),
    ("missing workspace members", check_members_exist),
    ("stray virtualenvs", check_no_stray_venvs),
    ("nested lockfiles", check_no_nested_locks),
    ("PEP 723 dependency blocks", check_no_pep723),
)


def main() -> int:
    failed = False
    for label, check in CHECKS:
        problems = check()
        if problems:
            failed = True
            print(f"✗ {label}:", file=sys.stderr)
            for p in problems:
                print(f"    {p}", file=sys.stderr)
        else:
            print(f"✓ {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
