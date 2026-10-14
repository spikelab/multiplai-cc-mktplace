"""What the coordinator's merge step reads and runs (plan-buildme-epic-mode-2026-10-12.md,
PR E, items 35-42). The step itself is `Coordinator._merge` in `coordinator.py`.

Every git command here runs on the child's worktree root `<W>` (its
`WORKTREE:` path), never on its project directory `<P>` (item 36): `git merge
--abort` acts on the whole worktree, so the check before it must cover the
whole worktree too. Only `run_merge_agent` writes, through an agent.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import git_ops, stop_record
from ..config import discover_test_command
from ..models import AgentResult
from ..prompts.epic_merge import MERGE_PROMPT
from ..sdk import agent_call

MERGE_TOOLS = ["Read", "Edit", "Bash"]
MERGE_MAX_TURNS = 40
# The lockfiles the merge agent may regenerate (item 40.3).
LOCKFILES = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "Cargo.lock", "Gemfile.lock", "go.sum", "Package.resolved", "composer.lock",
})
_MARKER = re.compile(r"^(?:<{7}|={7}|>{7}|\|{7})(?: |$)", re.MULTILINE)
# A version field and nothing else on the line: `"version": "1.2.0",`, `version = "1.2.0"`,
# `__version__ = "1.2"`, `version: 1.2.0`. Not any line that mentions "version".
_VERSION_FIELD = re.compile(r"""^\s*(["']?)(?:__)?version(?:__)?\1\s*[:=]\s*(["']?)v?\d[\w.+-]*\2\s*,?\s*$""",
                            re.IGNORECASE)
# The files a refused abort of the merge names (git 2.43).
_NAMED = re.compile(r"^error: Entry '([^']+)' not uptodate|^\t(\S.*)$", re.MULTILINE)


def git(w: Path, *args: str) -> git_ops.GitResult:
    return git_ops._run(["git", *args], cwd=w)


def _lines(res: git_ops.GitResult) -> list[str] | None:
    return [p for p in res.stdout.split("\0") if p] if res.ok else None


def merging(w: Path) -> bool:
    """A merge is in progress in *w*: it has a `MERGE_HEAD`."""
    return git(w, "rev-parse", "-q", "--verify", "MERGE_HEAD").ok


def branch(w: Path) -> str | None:
    res = git(w, "symbolic-ref", "--quiet", "--short", "HEAD")
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def sha(w: Path, ref: str) -> str | None:
    res = git(w, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def _ancestor(w: Path, a: str, b: str) -> bool:
    return git(w, "merge-base", "--is-ancestor", a, b).returncode == 0


def merge_parent(w: Path, commit: str, default: str) -> str | None:
    """*commit*'s first parent when *commit* merges `origin/<default>` into it:
    two parents, the second an ancestor of `refs/remotes/origin/<default>`."""
    res = git(w, "rev-list", "--parents", "-n", "1", commit)
    parents = res.stdout.split()[1:] if res.ok else []
    if len(parents) == 2 and _ancestor(w, parents[1], f"refs/remotes/origin/{default}"):
        return parents[0]
    return None


def unpushed_merge(w: Path, reviewed: str | None, branch_name: str, default: str) -> bool:
    """Item 37's shape: HEAD merges `origin/<default>` into the last clean
    reviewed head or into `origin/<branch>`, and is not on `origin/<branch>`."""
    head = git_ops.rev_parse_head(w)
    first = merge_parent(w, head, default) if head else None
    origin = sha(w, f"refs/remotes/origin/{branch_name}")
    if first is None or first not in (reviewed, origin):
        return False
    return not (origin and _ancestor(w, head, origin))


def merges_onto(w: Path, head: str, reviewed: str, default: str) -> bool:
    """True when *head* is *reviewed* with merges of `origin/<default>` on top,
    each holding only what the step itself may push (`_only_resolutions`): not
    reviewed again (E.S8, E.S13). A person's merge with other edits is."""
    commit = head
    for _ in range(50):
        parent = merge_parent(w, commit, default)
        if parent is None or not _only_resolutions(w, commit, parent):
            return False
        if parent == reviewed:
            return True
        commit = parent
    return False


def _only_resolutions(w: Path, commit: str, first: str) -> bool:
    """*commit*, a merge onto *first*, differs from git's own merge of its
    parents only in lockfiles and in conflicted files 40.3 lets the step resolve."""
    auto = git(w, "merge-tree", "--write-tree", "--name-only", "--no-messages", first, f"{commit}^2")
    tree, *conflicted = auto.stdout.splitlines() if auto.returncode in (0, 1) and auto.stdout.strip() else [""]
    differ = _lines(git(w, "diff", "--name-only", "-z", "--no-renames", tree, commit)) if tree else None
    return differ is not None and all(is_lockfile(p) or (p in conflicted and _resolvable(
        p, git(w, "cat-file", "blob", f"{tree}:{p}").stdout)) for p in differ)


def not_reviewed(w: Path, reviewed: str | None, default: str) -> str | None:
    """Why HEAD in *w* is neither the reviewed head nor it plus merges
    `merges_onto` accepts (E.S17); `others_work`'s `origin_ref` passes any
    commit on `origin/<branch>`, an older one too."""
    head = git_ops.rev_parse_head(w)
    if reviewed is None or head == reviewed or (head and merges_onto(w, head, reviewed, default)):
        return None
    return (f"HEAD {(head or 'unknown')[:12]} in {w} is not the reviewed head {reviewed[:12]} "
            f"or a merge of origin/{default} onto it, so it holds commits the review did not see")


def ready(w: Path, reviewed: str | None, origin_ref: str, default: str) -> str | None:
    """40.1: why `<W>` may not be merged into, or None. No `ignore`: ignoring
    the change directory would let an abort run over a user's spec edit."""
    return (stop_record.others_work(w, None, head=reviewed, origin_ref=origin_ref)
            or not_reviewed(w, reviewed, default))


def conflicted(w: Path) -> list[str] | None:
    return _lines(git(w, "diff", "--name-only", "-z", "--diff-filter=U"))


def is_lockfile(path: str) -> bool:
    return Path(path).name in LOCKFILES


def may_resolve(w: Path, path: str) -> bool:
    """Item 40.3: a lockfile, a CHANGELOG, or a file whose every conflicting
    line is a version field."""
    if is_lockfile(path) or _is_changelog(path):
        return True
    try:
        return _resolvable(path, (w / path).read_text())
    except (OSError, UnicodeDecodeError):
        return False


def _is_changelog(path: str) -> bool:
    return Path(path).stem.upper() == "CHANGELOG"


def _resolvable(path: str, text: str) -> bool:
    """*text*, the conflicted content of *path*, is one 40.3 lets the step resolve."""
    if is_lockfile(path) or _is_changelog(path):
        return True
    inside = found = False
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            inside = found = True
        elif line.startswith(">>>>>>> "):
            inside = False
        elif inside and not _MARKER.match(line) and line.strip() and not _VERSION_FIELD.match(line):
            return False
    return found


def unresolved(w: Path, files: list[str]) -> list[str]:
    """The files that still hold a conflict marker line, or cannot be read."""
    left = []
    for f in files:
        try:
            if _MARKER.search((w / f).read_text()):
                left.append(f)
        except (OSError, UnicodeDecodeError):
            left.append(f)
    return left


def index(w: Path) -> dict[str, list[str]] | None:
    """*w*'s index, path to its entries (mode, blob and stage), buildme's
    bookkeeping left out; None when git cannot list it."""
    entries = _lines(git(w, "ls-files", "--stage", "-z", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES))
    if entries is None:
        return None
    found: dict[str, list[str]] = {}
    for entry in entries:
        meta, _, path = entry.partition("\t")
        found.setdefault(path, []).append(meta)
    return found


def changed_since(w: Path, before: dict[str, list[str]]) -> list[str] | None:
    """Every path staged since the index was *before*, changed and not staged,
    or untracked, bookkeeping left out; None when git cannot list them."""
    now = index(w)
    changed = _lines(git(w, "diff", "--name-only", "-z", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES))
    new = _lines(git(w, "ls-files", "--others", "--exclude-standard", "-z", "--", ".",
                     *git_ops.BOOKKEEPING_EXCLUDES))
    if now is None or changed is None or new is None:
        return None
    staged = [p for p in sorted(now.keys() | before.keys()) if now.get(p) != before.get(p)]
    return list(dict.fromkeys(staged + changed + new))


def abort_names(stderr: str) -> list[str]:
    """The files a refused abort of the merge names."""
    return list(dict.fromkeys(a or b.strip() for a, b in _NAMED.findall(stderr)))


def abort_hint(w: Path) -> str:
    """What a person runs before re-running, when a merge is left in *w*."""
    return f"git -C {w} merge --abort, then re-run"


def test_command(p: Path) -> str | None:
    """Item 40.4: `tdd.test_command` from `<P>/specs/config.yaml`, else the first
    command `config.discover_test_command(<P>)` finds; None when neither has one."""
    path = Path(p) / "specs" / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text()) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        data = None
    tdd = data.get("tdd") if isinstance(data, dict) else None
    command = tdd.get("test_command") if isinstance(tdd, dict) else None
    if isinstance(command, str) and command.strip():
        return command.strip()
    found = discover_test_command(p)
    return found[0] if found else None


async def run_merge_agent(config, w: Path, files: list[str], branch_name: str, default: str) -> AgentResult:
    """Item 40.3: one agent call (step `merge`) that resolves *files* in *w*."""
    prompt = MERGE_PROMPT.format(default=default, branch=branch_name,
                                 files="\n".join(f"- `{f}`" for f in files))
    return await agent_call(
        prompt,
        allowed_tools=MERGE_TOOLS,
        model=config.model_for('merge'),
        max_turns=MERGE_MAX_TURNS,
        cwd=str(w),
        budget_label="merge",
    )
