"""What the coordinator's merge step reads and runs (plan-buildme-epic-mode-2026-10-12.md,
PR E, items 35-42). The step itself is `Coordinator._merge` in `coordinator.py`.

Every git command here runs on the child's worktree root `<W>` (its
`WORKTREE:` path), never on its project directory `<P>` (item 36): `git merge
--abort` acts on the whole worktree, so the check before it must cover the
whole worktree too. Only `run_merge_agent` writes, through an agent.
"""

from __future__ import annotations

import os
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
# A marker a conflict always leaves: `=======` alone is also a heading's underline.
_LEFT = re.compile(r"^(?:<{7}|>{7}|\|{7})(?: |$)", re.MULTILINE)
# The CHANGELOGs 40.3 lets the step resolve: by name, never a source file such as `changelog.py`.
_CHANGELOG_SUFFIXES = frozenset({"", ".md", ".markdown", ".rst", ".txt", ".adoc"})
# The tables of a TOML or INI file whose `version` is the package's own (not a dependency's).
_PACKAGE_TABLES = frozenset({"project", "package", "tool.poetry", "workspace.package", "metadata"})
_TABLE = re.compile(r"^\s*(\[\[?)\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
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
    """*commit*, a merge onto *first*, differs from git's own merge of its
    parents only in what 40.3 lets the step resolve (`not_resolutions`)."""
    return not_resolutions(w, first, f"{commit}^2", commit) == []


def not_resolutions(w: Path, first: str, second: str, result: str) -> list[str] | None:
    """The files where *result* (a commit or tree) differs from git's own
    merge of *first* and *second* in more than 40.3 lets the step resolve:
    in a conflicted version file, only the conflicting version lines; in a
    conflicted CHANGELOG, only lines from its sides (numbers aside); in a
    lockfile, only lines git's merge held or version fields. None when git
    cannot tell."""
    auto = git(w, "merge-tree", "--write-tree", "--name-only", "--no-messages", first, second)
    if auto.returncode not in (0, 1) or not auto.stdout.strip():
        return None
    tree, *conflicted = auto.stdout.splitlines()
    differ = _lines(git(w, "diff", "--name-only", "-z", "--no-renames", tree, result))
    if differ is None:
        return None
    return [p for p in differ if not _allowed(p, p in conflicted, _blob(w, tree, p), _blob(w, result, p))]


def _blob(w: Path, tree: str, path: str) -> str | None:
    res = git(w, "cat-file", "blob", f"{tree}:{path}")
    return res.stdout if res.ok else None


def _allowed(path: str, conflicted: bool, auto: str | None, new: str | None) -> bool:
    """*new* is a resolution 40.3 allows of *auto*, git's merge of *path*."""
    if auto is None or new is None:
        return False
    parts = _segments(auto)
    if is_lockfile(path):
        known = {x for part in parts for x in ([part] if isinstance(part, str) else part)}
        return all(line in known or _VERSION_FIELD.match(line) for line in new.splitlines())
    if not conflicted:
        return False
    if _is_changelog(path):
        return _fits(parts, new.splitlines(), lambda a, b: _digits(a) == _digits(b),
                     lambda side, line: not line.strip() or _digits(line) in {_digits(x) for x in side})
    return _resolvable(path, auto) and _fits(parts, new.splitlines(), str.__eq__,
                                             lambda side, line: bool(_VERSION_FIELD.match(line)))


def _digits(line: str) -> str:
    return re.sub(r"\d+", "0", line)


def _segments(text: str) -> list[str | list[str]]:
    """*text*, git's merge of a file, as its lines outside conflicts and, for
    each conflict, the lines of all its sides in one list."""
    parts: list[str | list[str]] = []
    side: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("<<<<<<<") and _MARKER.match(line):
            side = []
            parts.append(side)
        elif side is not None and line.startswith(">>>>>>>") and _MARKER.match(line):
            side = None
        elif side is not None and _MARKER.match(line):
            continue
        elif side is not None:
            side.append(line)
        else:
            parts.append(line)
    return parts


def _fits(parts: list[str | list[str]], lines: list[str], same, may) -> bool:
    """*lines* are *parts* with each conflict replaced by one or more lines
    *may* allows for its sides; every other line *same* as git's."""
    reach = {0}
    for part in parts:
        nxt: set[int] = set()
        for j in reach:
            if isinstance(part, str):
                if j < len(lines) and same(part, lines[j]):
                    nxt.add(j + 1)
                continue
            k = j
            while k < len(lines) and may(part, lines[k]):
                k += 1
                nxt.add(k)
        if not nxt:
            return False
        reach = nxt
    return len(lines) in reach


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


def conflicts_ahead(w: Path, default: str) -> bool:
    """Whether merging `origin/<default>` into HEAD would conflict (or git
    cannot tell): `git merge-tree` reads, and leaves `<W>` as it is."""
    return git(w, "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD",
               f"refs/remotes/origin/{default}").returncode != 0


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
    p = Path(path)
    return p.stem.upper() == "CHANGELOG" and p.suffix.lower() in _CHANGELOG_SUFFIXES


def _resolvable(path: str, text: str) -> bool:
    """*text*, the conflicted content of *path*, is one 40.3 lets the step
    resolve: every conflicting line is the package's own version field."""
    if is_lockfile(path) or _is_changelog(path):
        return True
    found, inside, before, cur, first = False, False, [], [], None
    for line in text.splitlines():           # *before*: the lines above, a conflict's first side for it
        if not inside:
            if line.startswith("<<<<<<<") and _MARKER.match(line):
                found, inside, cur, first = True, True, [], None
            else:
                before.append(line)
        elif line.startswith(">>>>>>>") and _MARKER.match(line):
            before += first if first is not None else cur
            inside = False
        elif _MARKER.match(line):            # `|||||||` or `=======`: the next side
            first = cur if first is None else first
            cur = []
        elif line.strip() and not (_VERSION_FIELD.match(line) and _own_version(path, before + cur, line)):
            return False
        else:
            cur.append(line)
    return found


def _own_version(path: str, before: list[str], line: str) -> bool:
    """*line*, a version field after the lines *before*, is the package's own:
    at the top level of a JSON file, in a package table (or none) of a TOML or
    INI file, unindented elsewhere. A dependency's version requirement is not."""
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        depth = 0
        for x in before:
            for ch in _STRING.sub('""', x):
                depth += (ch in "{[") - (ch in "}]")
        return depth == 1
    if suffix in (".toml", ".cfg", ".ini"):
        tables = [m for m in map(_TABLE.match, before) if m]
        return not tables or (tables[-1].group(1) == "[" and tables[-1].group(2) in _PACKAGE_TABLES)
    return line == line.lstrip()


def unresolved(w: Path, files: list[str]) -> list[str]:
    """The files that still hold a conflict marker line, or cannot be read.
    A lone `=======` is not one: it underlines a heading too."""
    left = []
    for f in files:
        try:
            if _LEFT.search((w / f).read_text()):
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
    """Item 40.3: one agent call (step `merge`) that resolves *files* in *w*.
    The coordinator vouches for the repo here as it does for every build,
    review and fixer it launches (`--trust-repo`): without it, `agent_call`
    would raise `RepoTrustError` mid-merge in a run started without the flag."""
    os.environ["BUILDME_TRUST_REPO"] = "1"
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
