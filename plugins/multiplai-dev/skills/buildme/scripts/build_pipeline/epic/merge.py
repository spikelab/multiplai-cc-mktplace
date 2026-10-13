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

from .. import git_ops
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
    """True when *head* is *reviewed* with one or more merges of
    `origin/<default>` on top and nothing else: the merge step's own pushed
    merge commits, which are merged, not reviewed again (E.S8, E.S13)."""
    commit = head
    for _ in range(50):
        commit = merge_parent(w, commit, default)
        if commit is None:
            return False
        if commit == reviewed:
            return True
    return False


def conflicted(w: Path) -> list[str] | None:
    return _lines(git(w, "diff", "--name-only", "-z", "--diff-filter=U"))


def is_lockfile(path: str) -> bool:
    return Path(path).name in LOCKFILES


def may_resolve(w: Path, path: str) -> bool:
    """Item 40.3: a lockfile, a CHANGELOG, or a file whose every conflicting
    line is a version field."""
    name = Path(path).name
    if is_lockfile(path) or name.upper().startswith("CHANGELOG"):
        return True
    try:
        text = (w / path).read_text()
    except (OSError, UnicodeDecodeError):
        return False
    inside = found = False
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            inside = found = True
        elif line.startswith(">>>>>>> "):
            inside = False
        elif inside and not _MARKER.match(line) and line.strip() and "version" not in line.lower():
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


def unstaged(w: Path) -> list[str] | None:
    """Every path changed in *w*'s tree but not staged, and every untracked
    path, buildme's bookkeeping left out; None when git cannot list them."""
    changed = _lines(git(w, "diff", "--name-only", "-z", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES))
    new = _lines(git(w, "ls-files", "--others", "--exclude-standard", "-z", "--", ".",
                     *git_ops.BOOKKEEPING_EXCLUDES))
    return None if changed is None or new is None else list(dict.fromkeys(changed + new))


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
