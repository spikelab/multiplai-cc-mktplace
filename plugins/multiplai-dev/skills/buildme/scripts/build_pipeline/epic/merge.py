"""What the coordinator's merge step reads and runs (plan-buildme-epic-mode-2026-10-12.md,
PR E, items 35-42). The step itself is `Coordinator._merge` in `coordinator.py`.

Every git command here runs on the child's worktree root `<W>` (its
`WORKTREE:` path), never on its project directory `<P>` (item 36): `git merge
--abort` acts on the whole worktree, so the check before it must cover the
whole worktree too. Until item 40.3 lands, the step resolves no conflict:
any conflict parks the child.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import git_ops, stop_record
from ..config import discover_test_command

# The files a refused abort of the merge names (git 2.43).
_NAMED = re.compile(r"^error: Entry '([^']+)' not uptodate|^\t(\S.*)$", re.MULTILINE)


def git(w: Path, *args: str) -> git_ops.GitResult:
    """Undecodable output (a non-UTF-8 file name) is replaced, never raised."""
    return git_ops._run(["git", *args], cwd=w, errors="replace")


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
    reviewed head or into `origin/<branch>`, and is not on `origin/<branch>`.
    It must also be the reviewed head plus merges `merges_onto` accepts: a
    person's merge with other edits, or one onto a head origin's branch has
    since left behind (E.S7), goes to 40.1, which parks it."""
    head = git_ops.rev_parse_head(w)
    first = merge_parent(w, head, default) if head else None
    origin = sha(w, f"refs/remotes/origin/{branch_name}")
    if first is None or first not in (reviewed, origin) or reviewed is None:
        return False
    return not (origin and _ancestor(w, head, origin)) and merges_onto(w, head, reviewed, default)


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
    """*commit*, a merge onto *first*, is git's own merge of its parents
    (`not_resolutions`)."""
    return not_resolutions(w, first, f"{commit}^2", commit) == []


def not_resolutions(w: Path, first: str, second: str, result: str) -> list[str] | None:
    """The files where *result* (a commit or tree) differs from git's own
    merge of *first* and *second*. Until item 40.3 lands, the step resolves
    no conflict, so every such file is one it did not merge; a conflicted
    file always differs, as git's merge of it holds the markers. None when
    git cannot tell."""
    auto = git(w, "merge-tree", "--write-tree", "--name-only", "--no-messages", first, second)
    if auto.returncode not in (0, 1) or not auto.stdout.strip():
        return None
    tree = auto.stdout.splitlines()[0]
    return _lines(git(w, "diff", "--name-only", "-z", "--no-renames", tree, result))


def not_reviewed(w: Path, reviewed: str | None, default: str) -> str | None:
    """Why HEAD in *w* is neither the reviewed head nor it plus merges
    `merges_onto` accepts (E.S17), with what a person does about it;
    `others_work`'s `origin_ref` passes any commit on `origin/<branch>`, an
    older one too (E.S25)."""
    head = git_ops.rev_parse_head(w)
    if reviewed is None or head == reviewed or (head and merges_onto(w, head, reviewed, default)):
        return None
    at = f"HEAD {(head or 'unknown')[:12]} in {w}"
    if sha(w, reviewed) is None:
        return (f"{at} is not the reviewed head {reviewed[:12]}, which {w} has not fetched; "
                f"git -C {w} fetch origin, then re-run")
    if head and _ancestor(w, head, reviewed):
        return (f"{at} is behind the reviewed head {reviewed[:12]}, pushed from elsewhere; "
                f"git -C {w} merge --ff-only {reviewed}, then re-run")
    if not (head and _ancestor(w, reviewed, head)):
        return (f"{at} does not hold the reviewed head {reviewed[:12]}: origin's branch moved; "
                f"{move_hint(w, reviewed)}")
    return (f"{at} is not the reviewed head {reviewed[:12]} or a merge of origin/{default} onto it, "
            "so it holds commits the review did not see; push them to be reviewed, then re-run")


def move_hint(w: Path, to: str) -> str:
    """What a person runs when `<W>` holds only a merge of the step's onto a
    head origin's branch has left behind (E.S7)."""
    return (f"git -C {w} fetch origin, then git -C {w} reset --keep {to} if HEAD holds only "
            "the merge step's merge, then re-run")


def ready(w: Path, reviewed: str | None, origin_ref: str, default: str) -> str | None:
    """40.1: why `<W>` may not be merged into, or None. No `ignore`: ignoring
    the change directory would let an abort run over a user's spec edit.
    Uncommitted changes first, then HEAD (`not_reviewed` says why it is not
    the reviewed head better than `others_work` does)."""
    return (stop_record.others_work(w, None) or not_reviewed(w, reviewed, default)
            or stop_record.others_work(w, None, head=reviewed, origin_ref=origin_ref))


def conflicted(w: Path) -> list[str] | None:
    return _lines(git(w, "diff", "--name-only", "-z", "--diff-filter=U"))


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

