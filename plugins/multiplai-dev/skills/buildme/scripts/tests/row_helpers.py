"""Helpers for the stop-and-resume row tests.

Each row of the "Stop and resume states" table in
plan-buildme-discard-safety-2026-10-13.md has a test that stops a step at
the row's point, resumes, and either compares the result with a run that
never stopped (`assert_same_as_unstopped`) or checks that a refusal left the
repo untouched (`repo_state`). `run_killed_at` stops a step as SIGKILL would.

The comparison rules are fixed by the plan (Decided 10): they leave out or
rewrite only what legitimately differs between a resumed run and an
unstopped one: commit shas, timestamps, earlier fix attempts, step heads,
logs, wip ref suffixes and each run's own temporary directory. A row test
that finds any other difference is a finding, not a reason to widen them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from build_pipeline import git_ops
from build_pipeline.git_ops import BOOKKEEPING_EXCLUDES

# Decided 10: keys that legitimately differ between a resumed run and an unstopped one.
DROP_KEYS = {
    "at",
    "since",
    "budget",
    "earlier_fixes",
    "step_heads",
}  # and every key ending in "_at"
HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
WIP_SUFFIX = re.compile(
    r"(refs/buildme/wip/[A-Za-z0-9._/-]*?)-[0-9]+(?![A-Za-z0-9._/-])"
)
NOT_COMPARED = (
    "build-progress.md",
    "pr-sections/*",
)  # logs: presence compared, content not
JSON_NORMALIZED = (
    ".build-state.json",
    ".findings-state.json",
    ".findings-r*-state.json",
    ".board.json",
    "reviews/block-*-round-*.json",
)
BOOKKEEPING = (
    ".build-state.json",
    ".findings-state.json",
    ".findings-r*-state.json",
    ".board.json",
    "build-progress.md",
    "pr-sections/*",
)


def _git(root: Path | str, *a: str, text: bool = True) -> Any:
    return subprocess.run(
        ["git", *a], cwd=root, check=True, capture_output=True, text=text
    ).stdout


def _match(rel: str, pats: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, "*/" + p) for p in pats)


def _tokens(root: Path) -> dict[str, str]:
    """Name each commit by its place on the first-parent history, never by its sha."""
    first = _git(root, "rev-list", "--first-parent", "--reverse", "HEAD").split()
    tok = {sha: f"<commit {i}>" for i, sha in enumerate(first)}
    for sha in _git(root, "rev-list", "--all").split():
        tok.setdefault(sha, "<other commit>")
    return tok


def _norm_text(s: str, root: Path, tok: dict[str, str]) -> str:
    """Rewrite the run's own directory, wip ref suffixes and commit shas."""
    s = s.replace(str(root), "<root>")
    s = WIP_SUFFIX.sub(r"\1", s)

    def sub(m: re.Match) -> str:
        hits = {t for sha, t in tok.items() if sha.startswith(m.group(0))}
        return hits.pop() if len(hits) == 1 else m.group(0)

    return HEX.sub(sub, s)


def _norm_json(v: Any, root: Path, tok: dict[str, str]) -> Any:
    if isinstance(v, dict):
        return {
            k: _norm_json(x, root, tok)
            for k, x in v.items()
            if k not in DROP_KEYS and not k.endswith("_at")
        }
    if isinstance(v, list):
        return [_norm_json(x, root, tok) for x in v]
    return _norm_text(v, root, tok) if isinstance(v, str) else v


def _content(rel: str, data: bytes, root: Path, tok: dict[str, str]) -> Any:
    if _match(rel, NOT_COMPARED):
        return "<not compared>"
    if _match(rel, JSON_NORMALIZED):
        value = json.loads(data)
        verification = value.get("verification") if isinstance(value, dict) else None
        if verification and verification.get("passed"):
            from build_pipeline.gates import verification_inputs

            inputs = verification["metadata"]["inputs"]
            project = root / rel.split("specs/", 1)[0]
            # The opaque digest includes fixture-specific absolute paths. Verify
            # it against this checkout before normalizing it for row comparison.
            actual = verification_inputs(
                inputs["command"],
                project,
                parallel=inputs["parallel"],
                plan_revision=inputs["plan_revision"],
            )
            assert inputs == actual, "saved verification must describe this checkout"
            inputs["content"] = "<verified checkout content>"
        return _norm_json(value, root, tok)
    try:
        return _norm_text(data.decode(), root, tok)
    except UnicodeDecodeError:
        return data


def repo_snapshot(root: Path | str, extra_files: Iterable[str] = ()) -> dict[str, Any]:
    """What a resumed run is compared on: every first-parent commit's subject and
    files, the working tree, the index and the status, and the refs under
    refs/buildme/ other than refs/buildme/wip/, each rewritten as Decided 10 says.
    *extra_files* (globs relative to *root*) are left out everywhere."""
    root = Path(root)
    tok = _tokens(root)
    extra_files = tuple(extra_files)

    def skip(rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, x) for x in extra_files)

    history = []
    for sha in _git(root, "rev-list", "--first-parent", "--reverse", "HEAD").split():
        files = {}
        for line in _git(root, "ls-tree", "-r", sha).splitlines():
            meta, rel = line.split("\t", 1)
            if not skip(rel):
                files[rel] = _content(
                    rel,
                    _git(root, "cat-file", "blob", meta.split()[2], text=False),
                    root,
                    tok,
                )
        history.append((_git(root, "log", "-1", "--format=%s", sha).strip(), files))
    work = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if ".git" in p.relative_to(root).parts or not p.is_file() or skip(rel):
            continue
        work[rel] = _content(rel, p.read_bytes(), root, tok)
    index = {}
    for line in _git(root, "ls-files", "-s").splitlines():
        meta, rel = line.split("\t", 1)
        mode, blob, stage = meta.split()
        if not skip(rel):
            index[(rel, stage)] = (
                mode,
                _content(
                    rel, _git(root, "cat-file", "blob", blob, text=False), root, tok
                ),
            )
    refs = [
        r
        for r in _git(
            root, "for-each-ref", "--format=%(refname)", "refs/buildme/"
        ).split()
        if not r.startswith("refs/buildme/wip/")
    ]
    status = _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        ".",
        *BOOKKEEPING_EXCLUDES,
    )
    return {
        "history": history,
        "worktree": work,
        "index": index,
        "status": status,
        "refs": refs,
    }


def assert_same_as_unstopped(
    stopped_root: Path | str, clean_root: Path | str, extra_files: Iterable[str] = ()
) -> None:
    """Assert a resumed run's repo matches a run of the same scenario that
    never stopped. Both arguments are repo roots, not project directories."""
    extra_files = tuple(extra_files)
    a, b = (
        repo_snapshot(stopped_root, extra_files),
        repo_snapshot(clean_root, extra_files),
    )
    for k in a:
        assert a[k] == b[k], k


def repo_state(root: Path | str) -> dict[str, Any]:
    """For a refusal: HEAD, every ref with its sha, the status and index, and
    every file's bytes, leaving out only buildme's bookkeeping files, which a
    refusal may rewrite (the checkpoint, build-progress.md)."""
    root = Path(root)
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if (
            ".git" in p.relative_to(root).parts
            or not p.is_file()
            or _match(rel, BOOKKEEPING)
        ):
            continue
        files[rel] = p.read_bytes()
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "refs": _git(root, "for-each-ref"),
        "status": _git(
            root,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".",
            *BOOKKEEPING_EXCLUDES,
        ),
        "index": _git(root, "ls-files", "-s"),
        "files": files,
    }


def run_killed_at(git_command: str, fn: Callable[[], Any]) -> None:
    """Run fn() in a forked child that exits with status 137 at the first git
    call whose argv[1] is *git_command*, as SIGKILL would."""
    pid = os.fork()
    if pid == 0:
        real = git_ops._run

        def run(argv, *a, **k):
            if argv[1:2] == [git_command]:
                os._exit(137)
            return real(argv, *a, **k)

        git_ops._run = run
        try:
            fn()
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 137, (
        "the child never reached the kill point"
    )
