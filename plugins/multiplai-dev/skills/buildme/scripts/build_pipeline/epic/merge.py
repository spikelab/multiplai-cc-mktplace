"""What the coordinator's merge step reads and runs (plan-buildme-epic-mode-2026-10-12.md,
PR E, items 35-42). The step itself is `Coordinator._merge` in `coordinator.py`.

Every git command here runs on the child's worktree root `<W>` (its
`WORKTREE:` path), never on its project directory `<P>` (item 36): `git merge
--abort` acts on the whole worktree, so the check before it must cover the
whole worktree too. Only `run_merge_agent` writes, through an agent.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import tomllib
from difflib import SequenceMatcher
from pathlib import Path
from typing import NamedTuple

import yaml

from .. import git_ops, stop_record
from ..config import discover_test_command, parse_parallel
from ..gates import parallel_command
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
# A marker a conflict always leaves: `=======` alone is also a heading's underline.
_LEFT = re.compile(r"^(?:<{7}|>{7}|\|{7})(?: |$)", re.MULTILINE)
# The CHANGELOGs 40.3 lets the step resolve: by name, never a source file such as `changelog.py`.
_CHANGELOG_SUFFIXES = frozenset({"", ".md", ".markdown", ".rst", ".txt", ".adoc"})
# The tables of a TOML or INI file whose `version` is the package's own (not a dependency's).
_PACKAGE_TABLES = frozenset({"project", "package", "tool.poetry", "workspace.package", "metadata"})
_TABLE = re.compile(r"^\s*(\[\[?)\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
# JSON: a string, or a punctuation mark that opens, closes or separates.
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{}\[\]:,]')
# Where a JSON file keeps the package's own `version`, as the keys of its open containers (`_own`):
# the top level, a marketplace manifest's `metadata` and each of its `plugins[]`. Any other is a
# component's, such as a devcontainer feature's node version or a dependency's.
_OWN_JSON = frozenset({(None,), (None, '"metadata"'), (None, '"plugins"', None)})
# The YAML files whose unindented `version` is the package's own. In another, such as
# docker-compose.yml or dependabot.yml, it is the file format's.
_OWN_YAML = frozenset({"pubspec.yaml", "Chart.yaml", "package.yaml", "galaxy.yml", "galaxy.yaml"})
# A version field and nothing else on the line: `"version": "1.2.0",`, `version = "1.2.0"`,
# `__version__ = "1.2"`, `version: 1.2.0`. Not any line that mentions "version".
_VERSION_FIELD = re.compile(r"""^\s*(["']?)(?:__)?version(?:__)?\1\s*[:=]\s*(["']?)v?(?P<v>\d[\w.+-]*)\2\s*,?\s*$""",
                            re.IGNORECASE)
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


# `_only_resolutions`' answers by worktree and commits: a commit never changes, so neither does its
# answer, and the coordinator asks again for the same PR head on every pass.
_ONLY: dict[tuple[str, str, str], bool] = {}
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _only_resolutions(w: Path, commit: str, first: str) -> bool:
    """*commit*, a merge onto *first*, differs from git's own merge of its
    parents only in what 40.3 lets the step resolve (`not_resolutions`).
    Kept for commits named by their sha, unless git could not tell."""
    key = (str(w), commit, first)
    if key in _ONLY:
        return _ONLY[key]
    found = not_resolutions(w, first, f"{commit}^2", commit)
    if found is not None and _SHA.fullmatch(commit) and _SHA.fullmatch(first):
        if len(_ONLY) >= 1024:
            _ONLY.clear()
        _ONLY[key] = found == []
    return found == []


class Plan(NamedTuple):
    """What merging *second* into *first* leaves the merge step (item 40.3)."""
    files: list[str]          # what the merge agent resolves, in this order: conflicted files it may resolve;
    #                           version files both sides raised, which git merges cleanly when both wrote the
    #                           same version; with those, the CHANGELOGs and lockfiles both sides changed and
    #                           each CHANGELOG only *first* changed whose added lines name a version it raised
    refused: list[str]        # conflicted files it may not resolve


class _Merge(NamedTuple):
    tree: str                 # git's own merge (`git merge-tree`)
    conflicted: list[str]
    raised: list[str]
    plan: Plan
    sides: dict[str, tuple[str, str, str] | None]       # of each file in `plan.files`


def plan(w: Path, first: str, second: str) -> Plan | None:
    """What merging *second* into *first* leaves the merge agent, read with
    `git merge-tree`, which changes nothing; None when git cannot tell."""
    m = _merge(w, first, second)
    return None if m is None else m.plan


def agent_ahead(w: Path, default: str) -> bool:
    """Whether merging `origin/<default>` into HEAD would launch the merge
    agent (or git cannot tell). A conflict the step may not resolve parks
    before any call, so it is not one."""
    p = plan(w, "HEAD", f"refs/remotes/origin/{default}")
    return p is None or (bool(p.files) and not p.refused)


def _merge(w: Path, first: str, second: str) -> _Merge | None:
    res = git(w, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", first, second)
    base = git(w, "merge-base", first, second)
    if res.returncode not in (0, 1) or not base.ok or not base.stdout.strip():
        return None
    tree, *paths = res.stdout.split("\0")
    ours = _lines(git(w, "diff", "--name-only", "-z", "--no-renames", base.stdout.split()[0], first))
    theirs = _lines(git(w, "diff", "--name-only", "-z", "--no-renames", base.stdout.split()[0], second))
    if not tree.strip() or ours is None or theirs is None:
        return None
    base_sha, conflicted, both = base.stdout.split()[0], list(dict.fromkeys(p for p in paths if p)), set(theirs)
    both_changed = [p for p in ours if p in both]
    sides: dict[str, tuple[str, str, str] | None] = {}

    def side(p: str) -> tuple[str, str, str] | None:
        if p not in sides:
            sides[p] = _sides(w, base_sha, first, second, p)
        return sides[p]

    refused = [p for p in conflicted if not _resolvable(p, side(p))]
    raised = [p for p in both_changed if p not in refused and not _special(p)
              and (s := side(p)) is not None and _raised(p, *s)]
    extra = [p for p in both_changed if raised and _special(p) and p not in refused and side(p) is not None]
    # The child's raised versions: a CHANGELOG only the child changed whose added lines name one must move to the
    # version the agent writes (finding 54632ff87f).
    mine = {v for p in raised for v in _normal(p, side(p)[1])[1].values()} - \
        {v for p in raised for v in _normal(p, side(p)[0])[1].values()}
    extra += [p for p in ours if p not in both and _is_changelog(p) and (s := side(p)) is not None
              and any(v in mine for x in _added(s[0], s[1]) for v in _VERSION_TEXT.findall(x))]
    files = list(dict.fromkeys([*(p for p in conflicted if p not in refused), *raised, *extra]))
    return _Merge(tree.strip(), conflicted, raised, Plan(files, refused), {p: side(p) for p in files})


def not_resolutions(w: Path, first: str, second: str, result: str) -> list[str] | None:
    """The files where *result* (a commit or tree), a merge of *second* into
    *first*, holds more than 40.3 lets the step write; None when git cannot
    tell. Every file it differs from git's own merge in, every conflicted
    file and every version file both sides raised is checked against its
    three sides (base, *first*, *second*), never against git's conflict
    markers, which trim lines both sides share:
    - a file `plan` does not give the agent (a refused conflict, a binary or
      deleted side included) must be git's own merge;
    - a version file must be git's merge with its version values aside, and
      each version value the side that changed it, or, where both did, the
      next after *second*'s, raising the part *first* raised (`_versions`);
    - a CHANGELOG must be *second*'s, every line kept in its order, with the
      lines *first* added in theirs, *first*'s version in them replaced by
      the one written for it (`_entries`); one git merged cleanly is checked
      too when those lines name a version that was replaced;
    - a lockfile must load, and hold only keys and values of *first*'s or
      *second*'s copy, or a version written in a version file, and each
      pinned package must be one side's whole, under its name (`_lockfile`)."""
    m = _merge(w, first, second)
    differ = None if m is None else _lines(git(w, "diff", "--name-only", "-z", "--no-renames", m.tree, result))
    if m is None or differ is None:
        return None
    check = list(dict.fromkeys([*differ, *m.conflicted, *m.raised]))
    allowed = {p: (m.sides.get(p), _blob(w, result, p)) for p in check if p in m.plan.files}
    bad, wrote = [p for p in check if p not in allowed], {}
    for p, (s, new) in allowed.items():
        if not _special(p):
            ok, wrote[p] = _versions(p, *s, new) if s and new is not None else (False, {})
            if not ok:
                bad.append(p)
    for p in m.plan.files:              # the child's entry still under its old version, where git left it
        if (_is_changelog(p) and p not in allowed and (s := m.sides.get(p))
                and any(v in _renames(p, wrote) for x in _added(s[0], s[1]) for v in _VERSION_TEXT.findall(x))):
            check.append(p)
            allowed[p] = (s, _blob(w, result, p))
    written = set().union(*_renames("", wrote).values())
    for p, (s, new) in allowed.items():
        if is_lockfile(p) and not (s and new is not None and _lockfile(p, s[1], s[2], new, written)):
            bad.append(p)
        elif _is_changelog(p) and not (s and new is not None and _entries(*s, new, _renames(p, wrote))):
            bad.append(p)
    return [p for p in check if p in bad]


def _renames(changelog: str, wrote: dict[str, dict[str, set[str]]]) -> dict[str, set[str]]:
    """Each version value replaced, with the values written for it, in the
    version files *wrote* holds (`_versions`) under *changelog*'s directory:
    its package's, not another's at the same version; in all of them when
    none is there (or *changelog* is "")."""
    top = Path(changelog).parent.as_posix()
    mine = [r for f, r in wrote.items() if top == "." or f.startswith(top + "/")] if changelog else []
    found: dict[str, set[str]] = {}
    for renames in mine or wrote.values():
        for old, values in renames.items():
            found.setdefault(old, set()).update(values)
    return found


def _blob(w: Path, tree: str, path: str) -> str | None:
    """*path*'s text in *tree*; None when it is not there or not text."""
    try:
        res = git_ops._run(["git", "cat-file", "blob", f"{tree}:{path}"], cwd=w)
    except UnicodeDecodeError:
        return None
    return res.stdout if res.ok and "\0" not in res.stdout else None


def _sides(w: Path, base: str, first: str, second: str, path: str) -> tuple[str, str, str] | None:
    """*path*'s text at *base*, *first* and *second*: base "" when neither had
    it (both added it); None when *first* or *second* lacks it (a deletion)
    or any side is not text."""
    o, t, b = _blob(w, first, path), _blob(w, second, path), _blob(w, base, path)
    if o is None or t is None or (b is None and git(w, "cat-file", "-e", f"{base}:{path}").ok):
        return None
    return ("" if b is None else b, o, t)


def _special(path: str) -> bool:
    return is_lockfile(path) or _is_changelog(path)


def _resolvable(path: str, sides: tuple[str, str, str] | None) -> bool:
    """A conflicted *path* the step may resolve (40.3): a lockfile or a
    CHANGELOG, or a file whose conflicts are all in the package's own version
    fields: git merges it cleanly with those values aside. Never one with a
    deleted or binary side."""
    if sides is None:
        return False
    if _special(path):
        return True
    b, o, t = (_normal(path, x)[0] for x in sides)
    return _merge3(o, b, t) is not None


# --- version files ---------------------------------------------------------

# Stands in for a version value while a file is merged without them: no NUL, which git reads as binary.
_HOLE = "\x01"


def _normal(path: str, text: str) -> tuple[list[str], dict[int, str]]:
    """*text*'s lines with each own version value (`_own`) replaced by
    `_HOLE`, and those values by line."""
    lines = text.splitlines()
    own = _own(path, lines)
    for i in own:
        m = _VERSION_FIELD.match(lines[i])
        lines[i] = lines[i][:m.start("v")] + _HOLE + lines[i][m.end("v"):]
    return lines, own


def _merge3(ours: list[str], base: list[str], theirs: list[str]) -> list[str] | None:
    """`git merge-file` of three texts' lines; None on a conflict or an error."""
    with tempfile.TemporaryDirectory() as d:
        names = []
        for name, lines in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(d) / name).write_text("".join(x + "\n" for x in lines))
            names.append(name)
        res = git_ops._run(["git", "merge-file", "-p", *names], cwd=d, errors="replace")
    return res.stdout.splitlines() if res.returncode == 0 else None


def _matched(a: list[str], b: list[str]) -> dict[int, int]:
    """Each line of *a* that `difflib` matches to a line of *b*."""
    return {blk.a + k: blk.b + k for blk in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
            for k in range(blk.size)}


def _raised(path: str, b: str, o: str, t: str) -> bool:
    """Both sides changed one of *path*'s own version values."""
    no, vo = _normal(path, o)
    if not vo:
        return False
    (nb, vb), (nt, vt) = (_normal(path, x) for x in (b, t))
    to_b, to_t = _matched(no, nb), _matched(no, nt)
    return any(i in to_b and i in to_t and vb.get(to_b[i]) not in (None, v)
               and vt.get(to_t[i]) not in (None, vb[to_b[i]]) for i, v in vo.items())


def _versions(path: str, b: str, o: str, t: str, r: str) -> tuple[bool, dict[str, set[str]]]:
    """*r* is git's merge of *path* with its version values aside, and each
    value is what `_expected` says; with each value *r* wrote that neither
    side has, under *o*'s value it replaces."""
    (nb, vb), (no, vo), (nt, vt), (nr, vr) = (_normal(path, x) for x in (b, o, t, r))
    if _merge3(no, nb, nt) != nr:
        return False, {}
    to_b, to_o, to_t = _matched(nr, nb), _matched(nr, no), _matched(nr, nt)
    renames: dict[str, set[str]] = {}
    for i, value in vr.items():
        ours, theirs = vo.get(to_o.get(i, -1)), vt.get(to_t.get(i, -1))
        want = _expected(vb.get(to_b.get(i, -1)), ours, theirs)
        if want is None or value != want:
            return False, {}
        if want not in (ours, theirs):          # both raised it, so *ours* is a value (`_expected`)
            renames.setdefault(ours, set()).add(want)
    return True, renames


def _expected(b: str | None, o: str | None, t: str | None) -> str | None:
    """A version value of the merge: the side's that changed it; where both
    changed it, the next after *t*'s (`_next`); None when there is none."""
    if o is None or t is None:
        return t if o is None else o
    if o == b:
        return t
    if t == b:
        return o
    if b is None:
        return t if o == t else None
    return _next(b, o, t)


def _next(b: str, o: str, t: str) -> str | None:
    """*t* with the part *o* raised from *b* raised by one and every part
    after it 0: base 0.64.0, ours 0.65.0, theirs 0.64.1 gives 0.65.0, and
    theirs 0.65.0 gives 0.66.0. None unless all three are dotted numbers
    and *o* raised *b*."""
    parts = [[int(x) for x in v.split(".")] if re.fullmatch(r"\d+(?:\.\d+)*", v) else None for v in (b, o, t)]
    if None in parts:
        return None
    n = max(len(p) for p in parts)
    bp, op, tp = (p + [0] * (n - len(p)) for p in parts)
    i = next((k for k in range(n) if op[k] != bp[k]), None)
    if i is None or op[i] < bp[i]:
        return None
    return ".".join(str(x) for x in (tp[:i] + [tp[i] + 1] + [0] * (n - i - 1))[:max(len(parts[2]), i + 1)])


# --- CHANGELOGs -------------------------------------------------------------

_VERSION_TEXT = re.compile(r"\d+(?:\.\d+)+")
# An interleaving check past this many cells parks rather than hold the run (judgment).
_MAX_CELLS = 4_000_000


def _added(b: str, o: str) -> list[str]:
    """The lines *o* added to *b*, in their order. Blank lines do not count."""
    bl, ol = ([x for x in text.splitlines() if x.strip()] for text in (b, o))
    return [x for tag, _, _, j1, j2 in SequenceMatcher(None, bl, ol, autojunk=False).get_opcodes()
            if tag in ("insert", "replace") for x in ol[j1:j2]]


def _entries(b: str, o: str, t: str, r: str, renames: dict[str, set[str]]) -> bool:
    """*r* keeps both CHANGELOGs in full: every line of *t* in its order and,
    among them, the lines *o* added to *b* in their order, each version in
    *renames* replaced by one written for it and nothing else changed, and
    nothing more. Blank lines do not count."""
    kept = [x for x in r.splitlines() if x.strip()]
    return _interleaves(kept, [x for x in t.splitlines() if x.strip()], _added(b, o), renames)


def _interleaves(lines: list[str], theirs: list[str], ours: list[str], renames: dict[str, set[str]]) -> bool:
    """*lines* are *theirs* and *ours* merged, each in its order, every line
    of both kept once and nothing added; a line of *ours* matches with each
    version in *renames* replaced by one written for it. Time: len(theirs)
    x len(ours)."""
    if len(lines) != len(theirs) + len(ours) or len(theirs) * len(ours) > _MAX_CELLS:
        return False
    shapes = [(_VERSION_TEXT.sub(_HOLE, x), _VERSION_TEXT.findall(x)) for x in lines]
    wants = [(_VERSION_TEXT.sub(_HOLE, x), [renames.get(v, {v}) for v in _VERSION_TEXT.findall(x)]) for x in ours]

    def fits(i: int, k: int) -> bool:           # ours[i] may stand as lines[k]
        (form, values), (want, allowed) = shapes[k], wants[i]
        return form == want and all(v in a for v, a in zip(values, allowed))

    row = [True] * (len(ours) + 1)              # row[i]: lines[:j + i] merge theirs[:j] and ours[:i]
    for i in range(1, len(ours) + 1):
        row[i] = row[i - 1] and fits(i - 1, i - 1)
    for j in range(1, len(theirs) + 1):
        row[0] = row[0] and theirs[j - 1] == lines[j - 1]
        for i in range(1, len(ours) + 1):
            row[i] = (row[i] and theirs[j - 1] == lines[i + j - 1]) or (row[i - 1] and fits(i - 1, i + j - 1))
        if not any(row):
            return False
    return row[-1]


# --- lockfiles --------------------------------------------------------------

_TOML_LOCKS = frozenset({"uv.lock", "poetry.lock", "Cargo.lock"})
_JSON_LOCKS = frozenset({"package-lock.json", "Pipfile.lock", "composer.lock", "Package.resolved"})
# The keys that pin a package's content: a hash, or a git revision (uv.lock's `source.git`,
# poetry.lock's `resolved_reference`, Package.resolved's `revision`).
_HASH_KEYS = frozenset({"hash", "hashes", "checksum", "integrity", "shasum", "sha256", "sha512",
                        "git", "resolved_reference", "revision"})
_PINNED_TEXT = re.compile(r"^\s*(?:integrity|checksum|resolved)\b", re.MULTILINE)
# Where a lockfile keeps a hash of its manifest, which regenerating it rewrites: a hex digest.
_MANIFEST_HASH = {"poetry.lock": ("metadata", "content-hash"), "composer.lock": ("content-hash",),
                  "Pipfile.lock": ("_meta", "hash", "sha256"), "Package.resolved": ("originHash",)}
_DIGEST = re.compile(r"[0-9a-f]{32,128}")


def _lockfile(path: str, o: str, t: str, r: str, written: set[str]) -> bool:
    """*r*, a regenerated lockfile. One read as data (`_load`) loads with no
    key twice; every key in it, and every value with the key it is under,
    is one of *o* or *t*, in any place (regenerating moves entries and drops
    the losing side's; a JSON comma moves with them), or is a `version` in
    *written*, or its manifest hash, a hex digest; and each package it pins
    (`_units`) is a whole package of *o* or *t* under the same name: never
    one side's version with the other's hash, nor one package's entry under
    another's name. Another holds only lines of *o* or *t* or a version
    field holding a version in *written*, and each yarn.lock paragraph that
    pins a package is one side's."""
    name = Path(path).name
    try:
        loaded = [_load(name, x) for x in (o, t, r)]
    except (ValueError, yaml.YAMLError):         # tomllib's and json's errors are ValueErrors
        return False
    if name in _TOML_LOCKS | _JSON_LOCKS | {"pnpm-lock.yaml"}:
        digest = [_digest(name, x) for x in loaded][2]
        if digest is not None and not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
            return False
        known = _items(loaded[0]) | _items(loaded[1]) | {("=", "version", _canon(v)) for v in written}
        units = {_canon(x) for d in loaded[:2] for x in _units(name, d)}
        return _items(loaded[2]) <= known and all(_canon(x) in units for x in _units(name, loaded[2]))
    known = set(o.splitlines()) | set(t.splitlines())
    if any(line not in known and not ((m := _VERSION_FIELD.match(line)) and m["v"] in written)
           for line in r.splitlines()):
        return False
    if name == "yarn.lock":                      # one package per paragraph
        blocks = {b.strip() for x in (o, t) for b in x.split("\n\n")}
        return all(b.strip() in blocks for b in r.split("\n\n") if _PINNED_TEXT.search(b))
    return True


def _load(name: str, text: str):
    """*text* loaded, for a lockfile format read here; None for another."""
    if name in _TOML_LOCKS:
        return tomllib.loads(text)
    if name in _JSON_LOCKS:
        return json.loads(text, object_pairs_hook=_unique)
    if name == "pnpm-lock.yaml":
        return yaml.load(text, Loader=_UniqueKeys)
    return None


def _unique(pairs: list[tuple[str, object]]) -> dict:
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("a key appears twice")
    return dict(pairs)


class _UniqueKeys(yaml.SafeLoader):
    """yaml's safe loader, refusing a key written twice, which it would
    otherwise keep the last of."""


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    keys = [_canon(loader.construct_object(k, deep=True)) for k, _ in node.value]
    if len(set(keys)) != len(keys):
        raise yaml.constructor.ConstructorError(None, None, "a key appears twice", node.start_mark)
    return loader.construct_mapping(node, deep=True)


_UniqueKeys.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _digest(name: str, data):
    """Takes *name*'s manifest hash (`_MANIFEST_HASH`) out of *data* and
    returns it; None when it has none."""
    path = _MANIFEST_HASH.get(name, ())
    for key in path[:-1]:
        data = data.get(key) if isinstance(data, dict) else None
    return data.pop(path[-1], None) if path and isinstance(data, dict) else None


def _items(data) -> set[tuple]:
    """Every key in *data*, and every value with the key it is under."""
    found: set[tuple] = set()

    def walk(x, key) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                found.add(("key", _canon(k)))
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        else:
            found.add(("=", key, _canon(x)))

    walk(data, None)
    return found


def _units(name: str, data) -> list:
    """What *data* pins as one package: each mapping `_pinned` finds, with
    the keys it sits under, which name the package in package-lock.json and
    Pipfile.lock; in pnpm's lock, which names a package by a key, each entry
    of `packages` and `snapshots`; in Package.resolved, which pins by
    `revision`, each pin whole, its name with its version and revision."""
    found: list = _pinned(data)
    if name == "pnpm-lock.yaml" and isinstance(data, dict):
        found += [{k: v} for part in ("packages", "snapshots") if isinstance(data.get(part), dict)
                  for k, v in data[part].items()]
    if name == "Package.resolved" and isinstance(data, dict):
        top = data.get("object") if isinstance(data.get("object"), dict) else data     # version 1 nests them
        found += top["pins"] if isinstance(top.get("pins"), list) else []
    return found


def _pinned(data) -> list[tuple[tuple, dict]]:
    """Each mapping but the top one with a `version` and a pin at or below
    it (a hash key, or Cargo.lock's `git+` source) that no mapping below it
    with a `version` claims: one locked package, with the keys of the
    mappings above it (a list adds none: regenerating reorders it). The top
    one's `version` is the lockfile format's. A package's `metadata` is not
    searched: uv.lock keeps there what the project asks for (`requires-dist`,
    whose `git` is a request, not a revision), not what it locked."""
    found: list[tuple[tuple, dict]] = []

    def walk(x, at: tuple) -> bool:              # True: x holds a pin no package below claims
        if isinstance(x, dict):
            held = [walk(v, (*at, k)) for k, v in x.items() if not (k == "metadata" and "version" in x)]
            pins = any(k in _HASH_KEYS for k in x) or str(x.get("source", "")).startswith("git+")   # Cargo.lock
            if (any(held) or pins) and "version" in x and at:
                found.append((at, x))
                return False
            return any(held) or pins
        return any([walk(v, at) for v in x]) if isinstance(x, list) else False

    walk(data, ())
    return found


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)


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


def is_lockfile(path: str) -> bool:
    return Path(path).name in LOCKFILES


def _is_changelog(path: str) -> bool:
    p = Path(path)
    return p.stem.upper() == "CHANGELOG" and p.suffix.lower() in _CHANGELOG_SUFFIXES


def _own(path: str, lines: list[str]) -> dict[int, str]:
    """The package's own version fields among *lines*, by line, with their
    values: in a JSON file the top-level one, or a marketplace manifest's
    `metadata.version` or `plugins[].version` (`_OWN_JSON`), in a package
    table (or none) of a TOML or INI file, unindented elsewhere (in YAML,
    only in a package manifest, `_OWN_YAML`). A dependency's version
    requirement, or a nested component's version, is not one. One pass."""
    suffix, found = Path(path).suffix.lower(), {}
    stack: list[str | None] = []             # JSON: the key each open container is under
    key = last = table = None
    for i, line in enumerate(lines):
        m = _VERSION_FIELD.match(line)
        if m and (tuple(stack) in _OWN_JSON if suffix == ".json"
                  else (table is None or (table[0] == "[" and table[1] in _PACKAGE_TABLES))
                  if suffix in (".toml", ".cfg", ".ini")
                  else line == line.lstrip() and (suffix not in (".yml", ".yaml") or Path(path).name in _OWN_YAML)):
            found[i] = m["v"]
        if suffix == ".json":
            for tok in _TOKEN.findall(line):
                if tok[0] == '"':
                    last = tok
                elif tok == ":":
                    key = last
                elif tok in "{[":
                    stack.append(key)
                    key = None
                elif tok in "}]":
                    stack = stack[:-1]
                    key = None
                else:
                    key = None
        elif (t := _TABLE.match(line)) and suffix in (".toml", ".cfg", ".ini"):
            table = (t.group(1), t.group(2))
    return found



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


def outside_merge(w: Path, conflicted_files: list[str]) -> list[str] | None:
    """During a merge, the files other than *conflicted_files* that are
    changed and not staged, or untracked: the tests would run with them and
    the merge commit would leave them out. None when git cannot list them."""
    staged = index(w)
    left = None if staged is None else changed_since(w, staged)
    return None if left is None else [p for p in left if p not in conflicted_files]


def abort_names(stderr: str) -> list[str]:
    """The files a refused abort of the merge names."""
    return list(dict.fromkeys(a or b.strip() for a, b in _NAMED.findall(stderr)))


def abort_hint(w: Path) -> str:
    """What a person runs before re-running, when a merge is left in *w*."""
    return f"git -C {w} merge --abort, then re-run"


def undo_hint(w: Path, files: list[str]) -> str:
    """What a person runs before re-running, when the merge agent changed
    *files*, which git did not leave conflicted: `git merge --abort` refuses
    over an unstaged edit to a file the merge changed, and keeps one to a
    file it did not, which would keep the child parked (E.S26). An edit is
    put back from the index; a new file is removed."""
    if not files:
        return abort_hint(w)
    new = set(_lines(git(w, "ls-files", "--others", "--exclude-standard", "-z", "--", *files)) or [])
    steps = [f"git -C {w} checkout -- {' '.join(shlex.quote(f) for f in files if f not in new)}"
             if any(f not in new for f in files) else "",
             f"rm -- {' '.join(shlex.quote(str(w / f)) for f in files if f in new)}" if new else ""]
    return f"{', then '.join(x for x in steps if x)}, then {abort_hint(w)}"


def _tdd(p: Path) -> dict:
    """`tdd:` from `<P>/specs/config.yaml`, empty when there is none to read."""
    path = Path(p) / "specs" / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text()) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        data = None
    tdd = data.get("tdd") if isinstance(data, dict) else None
    return tdd if isinstance(tdd, dict) else {}


def test_command(p: Path) -> str | None:
    """Item 40.4: `tdd.test_command` from `<P>/specs/config.yaml`, else the first
    command `config.discover_test_command(<P>)` finds; None when neither has one."""
    command = _tdd(p).get("test_command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    found = discover_test_command(p)
    return found[0] if found else None


def suite_command(p: Path) -> str | None:
    """What 40.4 runs: `test_command(p)` with `-n auto` when `tdd.parallel`
    allows it and pytest-xdist imports in the project's environment
    (`gates.parallel_command`). Runs the project's interpreter: the merge step
    vouches for the repo, as it does for the tests themselves."""
    command = test_command(p)
    if command is None:
        return None
    return parallel_command(command, Path(p), parse_parallel(_tdd(p).get("parallel", "auto")))


async def run_merge_agent(config, w: Path, files: list[str], branch_name: str, default: str) -> AgentResult:
    """Item 40.3: one agent call (step `merge`) that resolves *files* in *w*.
    The coordinator vouches for the repo here as it does for every build,
    review and fixer it launches (`--trust-repo`): without it, `agent_call`
    would raise `RepoTrustError` mid-merge in a run started without the flag."""
    prompt = MERGE_PROMPT.format(default=default, branch=branch_name,
                                 files="\n".join(f"- `{f}`" for f in files))
    before = os.environ.get("BUILDME_TRUST_REPO")
    os.environ["BUILDME_TRUST_REPO"] = "1"
    try:
        return await agent_call(
            prompt,
            allowed_tools=MERGE_TOOLS,
            model=config.model_for('merge'),
            effort=config.effort_for('merge'),
            max_turns=MERGE_MAX_TURNS,
            cwd=str(w),
            budget_label="merge",
        )
    finally:                                     # the trust is this call's only, not the process's
        if before is None:
            os.environ.pop("BUILDME_TRUST_REPO", None)
        else:
            os.environ["BUILDME_TRUST_REPO"] = before
