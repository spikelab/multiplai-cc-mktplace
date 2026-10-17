"""Shell command lines as `SKILL.md` files and Claude Code hooks write them.

This module reads command text and nothing else: it finds the commands of a
`SKILL.md`'s shell fences and of a hooks file, tokenises one command line, and
finds the module a `python -m` names. Turning a command into a discovery entry
is `entries`' job.

Tokenising. A command is split with `shlex` into words and shell operators.
Leading `cd <dir> &&` segments set the working directory; the first other
segment is the command, up to its first operator. Then `${CLAUDE_SKILL_DIR}`
(in a `SKILL.md`), `${CLAUDE_PLUGIN_ROOT}` and `$CLAUDE_PROJECT_DIR` /
`${CLAUDE_PROJECT_DIR}` are expanded to absolute paths under the snapshot root,
and leading launchers are stripped: environment assignments, `sh`, `bash`,
`/bin/sh`, `/bin/bash`, `exec`, `env`, an existing `.sh` file, and `uv run` with
its flags. A relative path is relative to the repository root, the directory a
hook or skill command runs from, unless a `cd` or `uv run --directory` moved it.
The repository root, and whether a file exists, come from the snapshot's
`PyIndex`, so a file in a pruned directory is never a launcher.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shlex
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .pyindex import PyIndex

log = logging.getLogger(__name__)

SHELL_FENCE_INFOS = frozenset({"bash", "sh", "shell", "zsh", "console"})
_LAUNCHERS = frozenset({"sh", "bash", "/bin/sh", "/bin/bash", "exec", "env"})
# The `uv run` options that take a value; every other option is a flag.
_UV_RUN_VALUE_OPTIONS = frozenset({
    "--directory", "--project", "--package", "--with", "--with-editable", "--with-requirements", "--python", "-p",
    "--extra", "--group", "--only-group", "--no-group", "--env-file", "--index", "--default-index", "--index-url",
    "--extra-index-url", "--find-links", "-f", "--config-file", "--cache-dir", "--color", "--exclude-newer",
    "--prerelease", "--resolution", "--index-strategy", "--keyring-provider", "--link-mode", "--refresh-package",
    "--reinstall-package", "--upgrade-package", "-P", "--no-binary-package", "--no-build-package",
    "--python-platform", "--no-extra",
})
_PYTHON_VALUE_OPTIONS = frozenset({"-X", "-W", "--check-hash-based-pycs"})
_INTERPRETER = re.compile(r"(?:.*/)?python(?:3(?:\.\d+)?)?")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_VARIABLE = re.compile(r"\$\{(CLAUDE_[A-Z_]+)\}|\$(CLAUDE_[A-Z_]+)(?![A-Za-z0-9_])")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_OPERATOR_CHARS = frozenset("();<>|&")
_FD_NUMBER = re.compile(r"\d+")
# Whether a command line that failed to tokenise was meant to run `python -m`.
_PYTHON_M_TEXT = re.compile(r"(?:^|[\s/])(?:python(?:3(?:\.\d+)?)?|uv\s+run)\s(?:.*\s)?-m\s")
_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_JSON_COLON = re.compile(r"\s*:\s*")


@dataclass(frozen=True)
class CommandLine:
    """One tokenised command: its words with variables expanded and launchers stripped.

    `search_dirs` are the `PYTHONPATH` directories, then the `cd`, `--directory` and
    `--project` ones, snapshot-relative; directories outside the snapshot are left out.
    `launcher` is the last `.sh` file stripped, snapshot-relative.
    """
    tokens: tuple[str, ...]
    search_dirs: tuple[str, ...]
    launcher: str | None
    cwd: Path
    root: Path

    def snapshot_path(self, token: str) -> str | None:
        """The snapshot-relative path a word names, from the working directory; None outside the snapshot."""
        return _relative(self.root, self.cwd / token)


def command_tokens(command: str, *, source_file: str, root: Path) -> list[str]:
    """The words of `command` as run from `source_file`, variables expanded and launchers stripped.

    Indexes the snapshot on every call; a caller holding a `PyIndex` uses `parse_command_line`.
    Raises ValueError when the command cannot be tokenised (an unclosed quote).
    """
    index = PyIndex.build(root)
    return list(parse_command_line(command, source_file=source_file, root=root, index=index).tokens)


def parse_command_line(command: str, *, source_file: str, root: Path, index: PyIndex) -> CommandLine:
    """`command` as written in `source_file`, a snapshot-relative path, in the snapshot `index` covers.

    Raises ValueError as `command_tokens`.
    """
    root = Path(root).absolute()
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    variables = _variables(root, source_file, index)
    words = [_VARIABLE.sub(lambda m: variables.get(m[1] or m[2], m[0]), word) for word in lexer]
    cwd = root / index.repo_root(source_file)
    moved: list[Path] = []
    for segment, operator in _segments(words):
        if len(segment) == 2 and segment[0] == "cd" and operator == "&&":
            cwd = cwd / segment[1]
            moved.append(cwd)
            continue
        return _strip_launchers(segment, cwd, moved, root, index)
    return CommandLine((), (), None, cwd, root)


def looks_like_python_m(command: str) -> bool:
    """Whether the raw text of a command that could not be tokenised runs `python -m` or `uv run -m`."""
    return _PYTHON_M_TEXT.search(command) is not None


def _segments(words: list[str]) -> list[tuple[list[str], str | None]]:
    """The words between shell operators, each with the operator that ends it.

    A number just before a redirection is its file descriptor (`2>&1`), not a word
    of the command; the lexer has already lost the spacing that would tell `2 >` apart.
    """
    segments: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    for word in words:
        if word and set(word) <= _OPERATOR_CHARS:
            if word[0] in "<>" and current and _FD_NUMBER.fullmatch(current[-1]):
                current.pop()
            segments.append((current, word))
            current = []
        else:
            current.append(word)
    segments.append((current, None))
    return segments


def _strip_launchers(words: list[str], cwd: Path, moved: list[Path], root: Path, index: PyIndex) -> CommandLine:
    """The command left once the leading assignments, launchers and `uv run` options of `words` are stripped."""
    pythonpath: list[Path] = []
    launcher = None
    position = 0
    while position < len(words):
        word = words[position]
        if _ASSIGNMENT.match(word):
            name, _, value = word.partition("=")
            if name == "PYTHONPATH":
                pythonpath += [cwd / part for part in value.split(":") if part]
            position += 1
        elif word in _LAUNCHERS:
            position += 1
        elif word == "uv" and words[position + 1:position + 2] == ["run"]:
            position, cwd = _skip_uv_run_options(words, position + 2, cwd, moved)
        elif (script := _existing_shell_script(word, cwd, root, index)) is not None:
            launcher = script
            position += 1
        else:
            break
    dirs = (_relative(root, d) for d in [*pythonpath, *moved])
    search_dirs = tuple(dict.fromkeys(d for d in dirs if d is not None))
    return CommandLine(tuple(words[position:]), search_dirs, launcher, cwd, root)


def _existing_shell_script(word: str, cwd: Path, root: Path, index: PyIndex) -> str | None:
    """The snapshot-relative path of the `.sh` file `word` names, when discovery walked it."""
    if not word.endswith(".sh"):
        return None
    script = _relative(root, cwd / word)
    return script if script is not None and index.has_file(script) else None


def _skip_uv_run_options(words: list[str], position: int, cwd: Path, moved: list[Path]) -> tuple[int, Path]:
    """The position after `uv run`'s options, and the working directory `--directory` leaves.

    `--directory` and `--project` directories are appended to `moved`. `-m` is not
    skipped: `uv run -m pkg` runs a module as `python -m pkg` does.
    """
    while position < len(words) and words[position].startswith("-") and words[position] != "-m":
        option, has_value, value = words[position].partition("=")
        if option == "--":
            return position + 1, cwd
        if not has_value and option in _UV_RUN_VALUE_OPTIONS and position + 1 < len(words):
            position += 1
            value, has_value = words[position], "="
        if has_value and option in ("--directory", "--project"):
            directory = cwd / value
            moved.append(directory)
            if option == "--directory":
                cwd = directory
        position += 1
    return position, cwd


def _variables(root: Path, source_file: str, index: PyIndex) -> dict[str, str]:
    """The Claude Code variables a command in `source_file` may use, as absolute paths."""
    found = {"CLAUDE_PLUGIN_ROOT": str(root / _plugin_root(index, source_file)),
             "CLAUDE_PROJECT_DIR": str(root / index.repo_root(source_file))}
    if posixpath.basename(source_file) == "SKILL.md":
        found["CLAUDE_SKILL_DIR"] = str(root / posixpath.dirname(source_file))
    return found


def _ancestors(source_file: str) -> Iterator[str]:
    """The directories holding `source_file`, innermost first, down to the snapshot root `""`."""
    directory = posixpath.dirname(source_file)
    while True:
        yield directory
        if not directory:
            return
        directory = posixpath.dirname(directory)


def _plugin_root(index: PyIndex, source_file: str) -> str:
    """The nearest ancestor with `.claude-plugin/plugin.json`, else the parent of the file's directory."""
    for directory in _ancestors(source_file):
        if index.has_file(posixpath.join(directory, ".claude-plugin", "plugin.json")):
            return directory
    return posixpath.dirname(posixpath.dirname(source_file))


def _relative(root: Path, path: Path) -> str | None:
    """`path` relative to `root` after normalisation, POSIX and `""` for the root; None outside it."""
    try:
        relative = Path(os.path.normpath(path)).relative_to(root).as_posix()
    except ValueError:
        return None
    return "" if relative == "." else relative


# --- python -m ------------------------------------------------------------------------------


def python_module(tokens: Sequence[str]) -> tuple[str, list[str]] | None:
    """`(module, arguments)` of `python[3] [options] -m <module> ...`, or of `-m <module> ...` left by a
    launcher; None for any other command.
    """
    rest = _past_interpreter(tokens)
    if len(rest) >= 2 and rest[0] == "-m":
        return rest[1], rest[2:]
    return None


def python_script(tokens: Sequence[str]) -> tuple[str, list[str]] | None:
    """`(script, arguments)` of `python[3] [options] <script>.py ...`, or of a `.py` file run directly; None for
    any other command.
    """
    rest = _past_interpreter(tokens)
    if rest and rest[0].endswith(".py"):
        return rest[0], rest[1:]
    return None


def _past_interpreter(tokens: Sequence[str]) -> list[str]:
    """The words after a leading `python[3]` and its options; all of `tokens` when they start otherwise."""
    rest = list(tokens)
    if rest and _INTERPRETER.fullmatch(rest[0]):
        rest = rest[1:]
        while rest and rest[0].startswith("-") and rest[0] not in ("-m", "-c", "-"):
            rest = rest[2:] if rest[0] in _PYTHON_VALUE_OPTIONS else rest[1:]
    return rest


def first_positional(arguments: Sequence[str]) -> str | None:
    """The first argument that is not an option."""
    return next((a for a in arguments if not a.startswith("-")), None)


def module_search_roots(command: CommandLine, source_file: str, repo_root: str) -> list[str]:
    """Where `python -m` in `command` is looked for, in order: its `PYTHONPATH`, `cd`, `--directory` and
    `--project` directories, the source file's directory and its `scripts/`, the repository root and its `src/`.
    """
    source_dir = posixpath.dirname(source_file)
    roots = [*command.search_dirs, source_dir, posixpath.join(source_dir, "scripts"),
             repo_root, posixpath.join(repo_root, "src")]
    return list(dict.fromkeys(roots))


def resolve_python_m(module: str, search_roots: Sequence[str], index: PyIndex) -> str | None:
    """The file `python -m <module>` runs: `<root>/<module path>/__main__.py` or `<root>/<module path>.py` under
    the first of `search_roots` (snapshot-relative, `""` for the root) that has either; None when none does.
    """
    if not _MODULE.fullmatch(module):
        return None
    parts = module.split(".")
    for base in search_roots:
        stem = posixpath.join(base, *parts)
        for candidate in (posixpath.join(stem, "__main__.py"), f"{stem}.py"):
            if index.has_file(candidate):
                return candidate
    return None


# --- SKILL.md -------------------------------------------------------------------------------


def shell_commands(markdown: str) -> Iterator[tuple[int, str]]:
    """`(line, command)` for each command in a shell-fenced block, `\\` continuations joined onto the first line.

    Blank lines and `#` comments are skipped, as is a `$ ` prompt in a `console` block.
    """
    fence: str | None = None
    console = shell = False
    pending: list[str] = []
    start = 0
    for number, line in enumerate(markdown.splitlines(), start=1):
        if fence is None:
            opening = _FENCE_OPEN.match(line)
            # A backtick fence's info string has no backtick: "```x```" is inline code.
            if opening and not (opening[1][0] == "`" and "`" in opening[2]):
                fence = opening[1]
                info = opening[2].split()
                language = info[0].lower() if info else ""
                shell, console = language in SHELL_FENCE_INFOS, language == "console"
            continue
        closing = _FENCE_CLOSE.match(line)
        if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence):
            if pending:
                yield start, " ".join(pending)
            fence, pending = None, []
            continue
        if not shell:
            continue
        text = line.strip()
        if not pending:
            if console:
                text = text.removeprefix("$ ")
            if not text or text.startswith("#"):
                continue
            start = number
        if text.endswith("\\"):
            pending.append(text[:-1].rstrip())
            continue
        pending.append(text)
        yield start, " ".join(part for part in pending if part)
        pending = []


# --- hooks ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookCommand:
    """A `"type": "command"` hook: `<event>[:<matcher>]`, its command string and that string's line."""
    name: str
    command: str
    line: int


def is_hooks_file(path: str) -> bool:
    """`hooks.json`, or `.claude/settings*.json`."""
    directory, name = posixpath.split(path)
    return name == "hooks.json" or (
        posixpath.basename(directory) == ".claude" and name.startswith("settings") and name.endswith(".json"))


def hook_commands(text: str) -> list[HookCommand]:
    """The command hooks of a hooks or settings document, in document order.

    Hooks of any other type, and malformed hook objects, are skipped. Raises
    ValueError when `text` is not JSON.
    """
    document = json.loads(text)
    events = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(events, dict):
        return []
    lines = _command_value_lines(text)
    found = []
    for event, groups in events.items():
        for group in groups if event and isinstance(groups, list) else []:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            matcher = group.get("matcher")
            name = f"{event}:{matcher}" if isinstance(matcher, str) and matcher else event
            for hook in group["hooks"]:
                command = hook.get("command") if isinstance(hook, dict) and hook.get("type") == "command" else None
                if isinstance(command, str) and command.strip():
                    # json.loads keeps document order, so the occurrences are taken in order.
                    found.append(HookCommand(name, command, lines[command].popleft()))
    return found


def _command_value_lines(text: str) -> dict[str, deque[int]]:
    """The lines of every `"command": "<string>"` value of a JSON text, per decoded string, in text order."""
    lines: dict[str, deque[int]] = {}
    previous: re.Match[str] | None = None
    for match in _JSON_STRING.finditer(text):
        if previous is not None and json.loads(previous[0]) == "command" \
                and _JSON_COLON.fullmatch(text, previous.end(), match.start()):
            lines.setdefault(json.loads(match[0]), deque()).append(text.count("\n", 0, match.start()) + 1)
        previous = match
    return lines
