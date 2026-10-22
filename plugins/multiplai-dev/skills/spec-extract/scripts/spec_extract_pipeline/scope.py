"""The scope step: which Python files a run reads, their public symbols, and the units.

Python is enforced here, in code (plan, Decided 17). The scope is every `*.py`
file at the target's commit (under `--path` for `--tree`; for `--pr`, the `.py`
files the PR changed that exist at its head, read whole), less what review's
`tree_files` skips: binary files, files over 200,000 characters, files with
`linguist-generated` or `linguist-vendored` set, and submodules. Review also
skips lockfiles by name; none of its lockfile names ends in `.py`, so that rule
never applies here. A symbolic link is skipped too: its blob is the link's
target path, not code.

`ast` lists each module's public top-level functions and classes and the
public methods of its public classes ("public": the name does not start with
`_`). A file `ast` cannot parse is not analysed. Test files (`test_*.py`,
`*_test.py`, anything under a `tests/` directory) are evidence only: they go to
the explore agents, so later steps can cite them, but their symbols are never
specified.

The result is saved in state as `scope` and never recomputed once `scope` is
in `steps_done` (row R26): a resumed `--pr` run whose base branch moved, or
whose PR merged, keeps the files it started with.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import warnings
from pathlib import PurePosixPath

from code_facts import Scope, Unit, partition
from multiplai_core.citations import GIT_ARGV, git_env
from pydantic import BaseModel, ConfigDict

from . import target as target_mod
from .target import Target

log = logging.getLogger(__name__)

# Lines per explore unit: buildme's codebase analysis uses the same (`code_analysis.py`, UNIT_LINES).
UNIT_LINES = 6000
# More units than this is refused with a hint to narrow the target. Judgment (plan, Unverified).
DEFAULT_MAX_UNITS = 20
# Review's `TREE_MAX_CHARS`.
MAX_CHARS = 200_000
GENERATED_ATTRS = ("linguist-generated", "linguist-vendored")
# git's own test for a binary blob: a NUL byte in the first 8,000 bytes.
BINARY_PROBE = 8000

MODE_LINK = "120000"
MODE_SUBMODULE = "160000"


class ScopeRefused(Exception):
    """The scope cannot be explored as it stands (exit 2)."""

    def __init__(self, message: str, line: str = ""):
        super().__init__(message)
        self.line = line  # a `SPEC_EXTRACT:` line for stdout, or ""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Symbol(_Model):
    path: str
    name: str  # `func`, `Class`, or `Class.method`
    kind: str  # "function", "class" or "method"
    line: int


class Skipped(_Model):
    path: str
    reason: str


class TargetScope(_Model):
    """What the run reads. Every list is in path order; `units` in `partition`'s order."""

    files: list[str]  # the Python files the agents read, test files included
    symbols: list[Symbol]  # public symbols of the files that are not tests
    skipped: list[Skipped]  # left out by review's rules, or a symbolic link
    unparsed: list[Skipped]  # `ast` could not parse them: not analysed
    units: list[Unit]


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or "tests" in p.parts[:-1]
    )


def line_count(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def public_symbols(path: str, tree: ast.Module) -> list[Symbol]:
    out: list[Symbol] = []
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and not node.name.startswith("_"):
            out.append(
                Symbol(path=path, name=node.name, kind="function", line=node.lineno)
            )
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            out.append(
                Symbol(path=path, name=node.name, kind="class", line=node.lineno)
            )
            for item in node.body:
                if isinstance(
                    item, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and not item.name.startswith("_"):
                    out.append(
                        Symbol(
                            path=path,
                            name=f"{node.name}.{item.name}",
                            kind="method",
                            line=item.lineno,
                        )
                    )
    return out


def _parse(data: bytes) -> ast.Module | str:
    """The module, or why `ast` could not parse it."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "ignore"
            )  # SyntaxWarning for odd escapes is not our business
            return ast.parse(data)
    except (SyntaxError, ValueError, UnicodeDecodeError) as e:
        where = f" (line {e.lineno})" if isinstance(e, SyntaxError) and e.lineno else ""
        first = (str(getattr(e, "msg", "") or e).splitlines() or [type(e).__name__])[0]
        return f"ast could not parse it: {first}{where}"


def _check_attrs(repo: str, head: str, paths: list[str]) -> dict[str, str]:
    """{path: attribute} for paths with linguist-generated or -vendored set at *head*
    (review's `_check_attrs`). An old git without `check-attr --source` skips the check."""
    if not paths:
        return {}
    proc = subprocess.run(
        [
            *GIT_ARGV,
            "-C",
            repo,
            "check-attr",
            "-z",
            "--stdin",
            "--source",
            head,
            *GENERATED_ATTRS,
        ],
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        env=git_env(),
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        log.warning(
            "git check-attr --source failed; not skipping generated files: %s",
            proc.stderr.strip()[:300],
        )
        return {}
    parts = proc.stdout.split("\0")
    out: dict[str, str] = {}
    for i in range(0, len(parts) - 2, 3):
        path, attr, value = parts[i], parts[i + 1], parts[i + 2]
        if value in ("set", "true") and path not in out:
            out[path] = attr
    return out


def changed_files(repo: str, base_sha: str, head_sha: str) -> set[str]:
    """The paths `git diff --name-only --no-renames` names between the PR's base and head."""
    raw = target_mod._git_bytes(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--name-only",
        "--no-renames",
        "-z",
        base_sha,
        head_sha,
    )
    return {os.fsdecode(p) for p in raw.split(b"\0") if p}


def _under(path: str, top: str) -> bool:
    return not top or path == top or path.startswith(top.rstrip("/") + "/")


def compute(target: Target) -> TargetScope:
    """The scope of *target* at its head commit. Reads git only."""
    repo, head = target.repo, str(target.head_sha)
    entries = [
        (mode, oid, path)
        for mode, _kind, oid, path in target_mod._tree_entries(repo, head)
        if path.endswith(".py")
    ]
    if target.kind == "tree" and target.path:
        entries = [e for e in entries if _under(e[2], target.path)]
    elif target.kind == "pr":
        changed = changed_files(repo, str(target.base_sha), head)
        entries = [e for e in entries if e[2] in changed]
    entries.sort(key=lambda e: e[2])

    skipped: list[Skipped] = []
    blobs_wanted: list[tuple[str, str]] = []
    for mode, oid, path in entries:
        if mode == MODE_SUBMODULE:
            skipped.append(Skipped(path=path, reason="submodule"))
        elif mode == MODE_LINK:
            skipped.append(Skipped(path=path, reason="symbolic link"))
        else:
            blobs_wanted.append((oid, path))
    blobs = target_mod._blob_bytes(repo, [oid for oid, _ in blobs_wanted])
    generated = _check_attrs(repo, head, [p for _, p in blobs_wanted])

    files: list[str] = []
    symbols: list[Symbol] = []
    unparsed: list[Skipped] = []
    lines: dict[str, int] = {}
    for oid, path in blobs_wanted:
        data = blobs[oid]
        if b"\0" in data[:BINARY_PROBE]:
            skipped.append(Skipped(path=path, reason="binary"))
        elif (
            len(data) > MAX_CHARS
            and len(data.decode("utf-8", errors="replace")) > MAX_CHARS
        ):
            skipped.append(Skipped(path=path, reason=f"over {MAX_CHARS} characters"))
        elif path in generated:
            skipped.append(Skipped(path=path, reason=f"{generated[path]} is set"))
        else:
            parsed = _parse(data)
            if isinstance(parsed, str):
                unparsed.append(Skipped(path=path, reason=parsed))
                continue
            files.append(path)
            lines[path] = line_count(data)
            if not is_test_file(path):
                symbols.extend(public_symbols(path, parsed))
    skipped.sort(key=lambda s: s.path)
    units = partition(Scope.whole_files([(p, lines[p]) for p in files]), UNIT_LINES)
    return TargetScope(
        files=files, symbols=symbols, skipped=skipped, unparsed=unparsed, units=units
    )


def check(scope: TargetScope, max_units: int) -> None:
    """Refuse a scope with no Python file to read, or more units than *max_units*."""
    if not scope.units:
        raise ScopeRefused(
            f"no Python file to read in this target ({len(scope.skipped)} skipped, "
            f"{len(scope.unparsed)} that ast could not parse)",
            line="SPEC_EXTRACT:NO_PYTHON",
        )
    if len(scope.units) > max_units:
        raise ScopeRefused(
            f"this target splits into {len(scope.units)} explore units, more than --max-units "
            f"{max_units}. Narrow it with --path <dir>, or raise --max-units"
        )


def plan_text(target: Target, scope: TargetScope, describe: str) -> str:
    """`plan.txt`: the units and the number of calls a run would make. No timestamp."""
    n = len(scope.units)
    s = len(
        {(x.path, x.name) for x in scope.symbols}
    )  # a name defined twice is one symbol
    lines = [
        f"spec-extract plan: {describe}",
        "",
        f"Explore: {n} unit(s), one agent run each (a unit whose run fails is asked once more, "
        f"so at most {2 * n} agent runs; an answer that does not parse is sent back once to be "
        f"reformatted, in one turn with no tools).",
    ]
    for u in scope.units:
        lines.append(
            f"- {u.id}: {len(u.files)} file(s), {u.lines} lines: {', '.join(u.files)}"
        )
    lines += [
        "",
        f"Public symbols to specify: {s}.",
        f"Write: one call per capability; the group step (one call) decides the capabilities, "
        f"at most 40 symbols each, so at least {-(-s // 40)} write call(s).",
        f"Skipped: {len(scope.skipped)} file(s). Not analysed (ast could not parse): "
        f"{len(scope.unparsed)} file(s).",
    ]
    return "\n".join(lines) + "\n"
