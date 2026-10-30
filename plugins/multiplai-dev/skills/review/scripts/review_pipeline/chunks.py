"""How a change's diff is cut for the finders that each read one part of it.

`split_pieces` cuts a change review's diff into **pieces** for the piece
finders: whole files only, related files together (one package or skill
directory, a module with its tests), at most `PIECE_MAX_CHARS` characters of
diff each unless one file alone is larger, with release metadata in meta
pieces placed last and lockfile text left out. There is no cap on the number
of pieces.

`split_piece` halves a piece whose finder hit the turn limit: by files, or a
one-file piece at the hunk boundary nearest its middle. `changed_symbols`
names the definitions that existed before the change and that a piece's diff
changes, which `stages/find.split_for_callers` counts to halve a piece for the
callers finder before it runs.

`head_changed_lines` gives the paths stage each file's added lines at head.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Piece
from .target import LOCKFILES


@dataclass
class FileChange:
    path: str  # the head path, or the base path of a deleted file
    added: int
    deleted: int
    text: str = ""  # this file's part of the diff, from its `diff --git` line
    head_path: str | None = None  # None for a deleted file
    base_path: str | None = None  # None for an added file
    status: str = "modified"  # added, modified, deleted, renamed or binary

    @property
    def lines(self) -> int:
        return self.added + self.deleted


def _section_path(header: str, lines: list[str]) -> str:
    """The file a `diff --git` section is about: the new side, or the old one for a deleted file."""
    old = new = ""
    for line in lines:
        if line.startswith("@@"):
            break
        if line.startswith("--- "):
            old = header_path(line[4:].rstrip("\n"))
        elif line.startswith("+++ "):
            new = header_path(line[4:].rstrip("\n"))
    for name, prefix in ((new, "b/"), (old, "a/")):
        if name and name != "/dev/null":
            return name.removeprefix(prefix)
    rest = header[len("diff --git ") :]
    return rest.rsplit(" b/", 1)[-1] if " b/" in rest else rest


_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


def header_path(name: str) -> str:
    """A path as git wrote it in a header, with git's C-style quoting (`"a/caf\\303\\251"`) undone.

    Git ends a `---` or `+++` name that holds a space with a tab, which is not
    part of the name: a name git leaves unquoted never holds a tab. A space at
    either end belongs to the name.
    """
    name = name.removesuffix("\t")
    if not (len(name) >= 2 and name[0] == name[-1] == '"'):
        return name
    body, out, i = name[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(ch in "01234567" for ch in octal):
                out.append(int(octal, 8))
                i += 4
                continue
            out.append(_ESCAPES.get(nxt, ord(nxt)) if ord(nxt) < 128 else 0)
            i += 2
            continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _sides(header: str, lines: list[str]) -> tuple[str | None, str | None, str]:
    """(base path, head path, status) of one `diff --git` section, from its header lines."""
    old = new = None
    rename_from = rename_to = None
    added = deleted = is_binary = False
    for line in lines:
        if line.startswith("@@"):
            break
        text = line.rstrip("\n")
        if text.startswith("--- "):
            old = header_path(text[4:])
        elif text.startswith("+++ "):
            new = header_path(text[4:])
        elif text.startswith("rename from "):
            rename_from = header_path(text[len("rename from ") :])
        elif text.startswith("rename to "):
            rename_to = header_path(text[len("rename to ") :])
        elif text.startswith("new file mode"):
            added = True
        elif text.startswith("deleted file mode"):
            deleted = True
        elif text.startswith("Binary files ") or text == "GIT binary patch":
            is_binary = True

    def strip(name: str | None, prefix: str) -> str | None:
        if name is None or name == "/dev/null":
            return None
        return name.removeprefix(prefix)

    base, head = strip(old, "a/") or rename_from, strip(new, "b/") or rename_to
    if (
        base is None and head is None
    ):  # a mode change or a binary file: only the `diff --git` line names it
        rest = header[len("diff --git ") :]
        name = rest.rsplit(" b/", 1)[-1] if " b/" in rest else rest
        base = None if added else name
        head = None if deleted else name
    if added:
        base = None
    if deleted:
        head = None
    if is_binary:
        status = "binary"
    elif head is None:
        status = "deleted"
    elif base is None:
        status = "added"
    elif base != head:
        status = "renamed"
    else:
        status = "modified"
    return base, head, status


def _split_lines(text: str) -> list[str]:
    """*text* split on `\n` only, each line keeping its newline: how git numbers lines."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def diff_files(diff: str) -> list[FileChange]:
    """Each file in *diff* with its added and deleted lines, in diff order (a binary file counts 0)."""
    sections: list[list[str]] = []
    for line in _split_lines(diff):
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    files = []
    for section in sections:
        added = deleted = 0
        in_hunk = False
        for line in section[1:]:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added += 1
            elif in_hunk and line.startswith("-"):
                deleted += 1
        header = section[0].rstrip("\n")
        base, head, status = _sides(header, section[1:])
        files.append(
            FileChange(
                path=head or base or _section_path(header, section[1:]),
                added=added,
                deleted=deleted,
                text="".join(section),
                head_path=head,
                base_path=base,
                status=status,
            )
        )
    return files


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def head_changed_lines(diff: str) -> dict[str, list[int]]:
    """The head-side line numbers of each file's added lines, under its head path, in diff order.

    A hunk's body is counted from its `@@` lengths, never recognised by its
    first characters, so an added line reading `++ b/x` is a line, not a file
    header. The text is split on `\n` only, which is how git numbers lines. A
    deleted file, a pure deletion and a binary file contribute nothing; a
    renamed file appears under its new path.
    """
    out: dict[str, list[int]] = {}
    lines = diff.split("\n")
    path: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ ") and path is None:
            name = header_path(line[4:].rstrip("\r"))
            path = "" if name == "/dev/null" else name.removeprefix("b/")
        elif (m := _HUNK.match(line)) and path is not None:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            number = int(m.group(2))
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            while (old_left > 0 or new_left > 0) and i < len(lines):
                body = lines[i]
                i += 1
                if body.startswith("\\"):  # "\ No newline at end of file"
                    continue
                if body.startswith("+"):
                    if path:
                        out.setdefault(path, []).append(number)
                    number += 1
                    new_left -= 1
                elif body.startswith("-"):
                    old_left -= 1
                else:
                    number += 1
                    new_left -= 1
                    old_left -= 1
    return out


# --- pieces --------------------------------------------------------------------------------------

# A piece holds at most this many characters of diff (inclusive); one file alone may be larger.
PIECE_MAX_CHARS = 50_000
# A directory holding one of these is a package or skill directory: a group of related files.
GROUP_MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "SKILL.md")
META_NAMES = frozenset(
    {
        "CHANGELOG.md",
        "plugin.json",
        "marketplace.json",
        "VERSION",
        "version.txt",
        ".gitignore",
        ".gitattributes",
    }
)
META_GROUP = "meta"
# A test file's name and the name of the module it tests.
_TEST_NAMES = (
    (re.compile(r"^test_(.+)\.py$"), r"\1.py"),
    (re.compile(r"^(.+)_test\.py$"), r"\1.py"),
    (re.compile(r"^(.+)_test\.go$"), r"\1.go"),
    (re.compile(r"^(.+)\.(?:test|spec)\.([^.]+)$"), r"\1.\2"),
)


def is_lockfile(path: str) -> bool:
    return posixpath.basename(path) in LOCKFILES


def is_meta(path: str) -> bool:
    """Release and repository metadata: changelogs, manifests, version files, licences, `.github/`."""
    name = posixpath.basename(path)
    return (
        name in META_NAMES or name.startswith("LICENSE") or path.startswith(".github/")
    )


def tested_module(path: str) -> str | None:
    """The module file name a test file's name says it tests, or None for a file that is not a test."""
    name = posixpath.basename(path)
    for pattern, module in _TEST_NAMES:
        if pattern.match(name):
            return pattern.sub(module, name)
    return None


def group_of(path: str, tree: frozenset[str]) -> str:
    """The nearest ancestor directory of *path* holding a group marker in *tree*; "" (the root) when none."""
    directory = posixpath.dirname(path)
    while directory:
        if any(f"{directory}/{marker}" in tree for marker in GROUP_MARKERS):
            return directory
        directory = posixpath.dirname(directory)
    return ""


def _common_dirs(a: str, b: str) -> int:
    """How many leading directories two paths share."""
    n = 0
    for x, y in zip(posixpath.dirname(a).split("/"), posixpath.dirname(b).split("/")):
        if not x or x != y:
            break
        n += 1
    return n


def _fill(units: Iterable[list[FileChange]], sizes: dict[str, int]) -> list[list[str]]:
    """Units (a module and its tests, or one file) into piece file lists of at most PIECE_MAX_CHARS.

    A unit goes whole into the open piece when it fits; one larger than the
    budget is split into its files, module first, by the same rule; a file
    larger than the budget is alone in its piece.
    """
    pieces: list[list[str]] = []
    open_files: list[str] = []
    open_chars = 0

    def close() -> None:
        nonlocal open_files, open_chars
        if open_files:
            pieces.append(open_files)
        open_files, open_chars = [], 0

    def add(paths: list[str], chars: int) -> None:
        nonlocal open_chars
        if open_files and open_chars + chars > PIECE_MAX_CHARS:
            close()
        open_files.extend(paths)
        open_chars += chars
        if chars > PIECE_MAX_CHARS:  # an oversized file shares its piece with nothing
            close()

    for unit in units:
        chars = sum(sizes[f.path] for f in unit)
        if chars <= PIECE_MAX_CHARS:
            add([f.path for f in unit], chars)
        else:
            for f in unit:
                add([f.path], sizes[f.path])
    close()
    return pieces


def split_pieces(
    diff: str, *, head_tree: frozenset[str], base_tree: frozenset[str]
) -> list[Piece]:
    """A change review's diff as pieces for the dimension finders, ids 1..K, in a fixed order.

    *head_tree* and *base_tree* are every path at head and at base (one
    `git ls-tree -r --name-only` each), where the group markers are looked up:
    at base for a file the change deletes. A file's size is the length of its
    part of the diff, headers included; a binary file counts 0 and a lockfile
    nothing at all (it is listed in the first meta piece's `omitted`). Group
    pieces are ordered by group and first file; meta pieces come last.
    """
    files = diff_files(diff)
    omitted = [f.path for f in files if is_lockfile(f.path)]
    kept = [f for f in files if not is_lockfile(f.path)]
    sizes = {f.path: 0 if f.status == "binary" else len(f.text) for f in kept}
    binary = {f.path for f in kept if f.status == "binary"}
    meta = sorted((f for f in kept if is_meta(f.path)), key=lambda f: f.path)
    rest = [f for f in kept if not is_meta(f.path)]

    tests = {f.path: tested_module(f.path) for f in rest}
    modules = [f for f in rest if tests[f.path] is None]
    paired: dict[str, list[FileChange]] = {}  # module path -> its tests
    for f in rest:
        wanted = tests[f.path]
        if wanted is None:
            continue
        matches = [m for m in modules if posixpath.basename(m.path) == wanted]
        if matches:
            best = min(matches, key=lambda m: (-_common_dirs(m.path, f.path), m.path))
            paired.setdefault(best.path, []).append(f)
    in_pairs = {t.path for ts in paired.values() for t in ts}

    groups: dict[str, list[list[FileChange]]] = {}
    for f in rest:
        if f.path in in_pairs:
            continue
        tree = base_tree if f.status == "deleted" else head_tree
        unit = [f, *sorted(paired.get(f.path, []), key=lambda t: t.path)]
        groups.setdefault(group_of(f.path, tree), []).append(unit)

    ordered: list[tuple[str, list[str]]] = []
    for group in sorted(groups):
        units = sorted(groups[group], key=lambda u: u[0].path)
        ordered.extend((group, p) for p in _fill(units, sizes))
    ordered.sort(key=lambda gp: (gp[0], gp[1][0]))
    meta_pieces = _fill(([f] for f in meta), sizes)
    if omitted and not meta_pieces:
        meta_pieces = [[]]

    pieces = [
        Piece(
            id=i,
            group=group,
            files=paths,
            binary=[p for p in paths if p in binary],
            chars=sum(sizes[p] for p in paths),
        )
        for i, (group, paths) in enumerate(ordered, 1)
    ]
    for k, paths in enumerate(meta_pieces):
        pieces.append(
            Piece(
                id=len(pieces) + 1,
                group=META_GROUP,
                meta=True,
                files=paths,
                omitted=omitted if k == 0 else [],
                binary=[p for p in paths if p in binary],
                chars=sum(sizes[p] for p in paths),
            )
        )
    return pieces


def file_hunks(text: str) -> tuple[str, list[str]]:
    """One file's part of the diff as its header lines (`diff --git`, `---`, `+++`, ...) and its hunks."""
    header: list[str] = []
    hunks: list[list[str]] = []
    for line in _split_lines(text):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    return "".join(header), ["".join(h) for h in hunks]


def piece_diff_text(diff: str, piece: Piece) -> str:
    """The diff of *piece*'s files, in the piece's order (a lockfile's text is never in it).

    A half of a one-file piece (`piece.hunks`) holds the file's header lines and only its own hunks.
    """
    texts = {f.path: f.text for f in diff_files(diff)}
    if piece.hunks is not None and len(piece.files) == 1:
        header, hunks = file_hunks(texts.get(piece.files[0], ""))
        first, end = piece.hunks
        return header + "".join(hunks[first:end])
    return "".join(texts.get(path, "") for path in piece.files)


def _half(
    piece: Piece, piece_id: int, files: list[str], chars: int, hunks=None
) -> Piece:
    return Piece(
        id=piece_id,
        group=piece.group,
        meta=piece.meta,
        files=files,
        binary=[p for p in piece.binary if p in files],
        chars=chars,
        split_from=piece.id,
        hunks=hunks,
    )


def _file_cut(files: list[str], sizes: dict[str, int]) -> int:
    """Where to cut *files* in two: the boundary nearest half their characters that keeps a module with its
    tests (`tested_module`), or the nearest boundary when every one parts them."""
    total = sum(sizes[f] for f in files)
    names = {posixpath.basename(f): f for f in files}

    def parts_pair(i: int) -> bool:
        left = set(files[:i])
        return any(
            (module := names.get(tested_module(f) or ""))
            and (f in left) != (module in left)
            for f in files
        )

    def distance(i: int) -> int:
        return abs(2 * sum(sizes[f] for f in files[:i]) - total)

    cuts = sorted(range(1, len(files)), key=lambda i: (distance(i), i))
    kept = [i for i in cuts if not parts_pair(i)]
    return (kept or cuts)[0]


def split_piece(piece: Piece, diff: str, next_id: int) -> tuple[Piece, Piece] | None:
    """*piece* in two halves with ids *next_id* and *next_id* + 1, or None when it cannot be split.

    Files are halved by characters, in order (`_file_cut`). A one-file piece is
    halved at the hunk boundary nearest its middle, each half repeating the
    file's header lines; a one-hunk file is not split. A half is never split
    again: it is cut from the stored diff, not from the halves.
    """
    sizes = {
        f.path: 0 if f.status == "binary" else len(f.text) for f in diff_files(diff)
    }
    if len(piece.files) >= 2:
        sizes = {f: sizes.get(f, 0) for f in piece.files}
        i = _file_cut(piece.files, sizes)
        left, right = piece.files[:i], piece.files[i:]
        return (
            _half(piece, next_id, left, sum(sizes[f] for f in left)),
            _half(piece, next_id + 1, right, sum(sizes[f] for f in right)),
        )
    if len(piece.files) != 1:
        return None
    header, hunks = file_hunks(piece_diff_text(diff, piece))
    if len(hunks) < 2:
        return None
    total = sum(len(h) for h in hunks)
    cut = min(
        range(1, len(hunks)),
        key=lambda i: (abs(2 * sum(len(h) for h in hunks[:i]) - total), i),
    )
    path = piece.files[0]
    return (
        _half(
            piece,
            next_id,
            [path],
            len(header) + sum(len(h) for h in hunks[:cut]),
            [0, cut],
        ),
        _half(
            piece,
            next_id + 1,
            [path],
            len(header) + sum(len(h) for h in hunks[cut:]),
            [cut, len(hunks)],
        ),
    )


# --- changed symbols -----------------------------------------------------------------------------

# A definition line: Python `def`/`class`, JS/TS `function`/`class`, Go `func` (with a receiver), Rust
# `fn`, Swift `func`, after any of the usual modifiers. The name is group 1.
_DEFINITION = re.compile(
    r"^\s*(?:(?:export|default|pub|public|private|protected|internal|static|async|override)\s+)*"
    r"(?:def|class|function|func|fn)\s+(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)"
)
# A hunk header and the text git prints after it: the nearest definition line above the hunk.
_HUNK_CONTEXT = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")


def changed_symbols(diff: str) -> set[tuple[str, str]]:
    """(path, name) of each definition that existed before the change and that *diff* changes.

    Only files the change did not add count: their callers are in the rest of
    the repository. A name counts when a removed line defines it (a signature
    changed or a definition deleted) or when a hunk header names it (git puts
    the definition above the hunk there, so a body changed). A definition the
    change adds has its callers in the change itself and does not count.
    """
    out: set[tuple[str, str]] = set()
    for change in diff_files(diff):
        if change.status == "added":
            continue
        for line in _split_lines(change.text):
            text = line.rstrip("\r\n")
            if (header := _HUNK_CONTEXT.match(text)) is not None:
                found = _DEFINITION.match(header.group(1))
            elif text.startswith("-") and not text.startswith("--- "):
                found = _DEFINITION.match(text[1:])
            else:
                continue
            if found is not None:
                out.add((change.path, found.group(1)))
    return out
