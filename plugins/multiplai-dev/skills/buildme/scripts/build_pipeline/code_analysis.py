"""Checked, cited facts about the project's code, from the shared `code_facts` package.

Two users:

- The codebase analysis phase (`llm_steps/spec_steps.run_codebase_analysis`).
  It explains the project's source files at HEAD, one `code_facts` agent per
  slice, plus one slice of its manifest and configuration files for the
  dependencies it declares. Each source slice is also pointed at the tests
  that import its modules, read only for how tests are written. It keeps a
  fact only when one of its citations is found at that commit
  (`multiplai_core.check_quote`), renders the facts to `codebase-analysis.md`
  with `path:line` links, and writes them to `facts.json` beside it, in the
  format review's explain stage writes (`review/schema/facts.v1.schema.json`).
- The TDD engine's context (`block_facts: true`). `block_facts_section` hands a
  block's test writer and implementer the facts about the files its tasks
  name, each citation re-checked at the worktree's current HEAD, because
  earlier blocks have committed changes since the analysis ran.

Paths are relative to the project directory, which is the agents' working
directory. When the project is a subdirectory of its repo (a monorepo
subpackage), the resolver adds the repo prefix before `git show`.

The model call is the caller's: `code_facts` never retries, so buildme's own
policy (re-run the whole call, `sdk.agent_call_structured`) is the one that runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from code_facts import (
    CallFn,
    Citation,
    ExploreOutput,
    Fact,
    Gated,
    Resolve,
    Scope,
    Span,
    Unit,
    citation_keeper,
    explore_unit,
    gate_answers,
    partition,
    run_tasks,
    unit_key,
)
from multiplai_core.citations import check_quote, file_at

from . import git_ops

log = logging.getLogger(__name__)

FACTS_FILE = "facts.json"
# git's empty tree: the base of a whole-tree `facts.json` (review --tree, walkthroughs).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# Lines per explore unit: codebase-walkthrough's and review's default.
UNIT_LINES = 6000
# Explore agents in flight at once; sdk's own semaphore caps every call at 10.
CONCURRENCY = 4
# An agent reads up to UNIT_LINES lines with `Read`, a few hundred at a time.
MAX_TURNS = 30
ANALYSIS_TOOLS = ["Read", "Glob", "Grep"]
# A file this size or larger is data, not code (walkthrough's snapshot limit).
MAX_FILE_BYTES = 2 * 1024 * 1024
# A file with this few lines needs no fact of its own (walkthrough's coverage rule).
SMALL_FILE_LINES = 30

# Vendored, generated and tool directories: never part of the scope.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".worktrees",
        "specs",
        ".build",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        "target",
        ".tox",
        ".next",
        "vendor",
        "third_party",
        "staticfiles",
        "migrations",
    }
)
GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    "_pb2.py",
    "_pb2_grpc.py",
    ".pb.go",
    ".generated.ts",
)

# Files that declare a project's dependencies, by name. A manifest that is
# itself code (setup.py, Package.swift, mix.exs) is analysed as source.
MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.cfg",
        "Pipfile",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    }
)
_REQUIREMENTS = re.compile(r"requirements[\w.-]*\.txt")
CONFIG_SUFFIXES = frozenset({".toml", ".yaml", ".yml", ".ini", ".cfg"})
# Lock files with a config suffix: generated, and long.
LOCK_NAMES = frozenset({"pnpm-lock.yaml"})
# A config file longer than this is data (fixtures, an API description), not configuration.
MAX_CONFIG_LINES = 1000
# The unit id of the manifest and configuration slice; its facts render under Dependencies.
DEPENDENCY_UNIT_ID = "deps"
DEPENDENCY_NOTE = """
These are the project's manifest and configuration files. In `facts` and `files`, say which
dependencies they declare (with the versions they pin), what configuration they set and where
the code loads it, and which external services the project is set up to talk to.
"""
# Test files a slice is pointed at for test style.
MAX_TEST_EXAMPLES = 2
TEST_STYLE_INTRO = (
    "Test files, read only to learn how this project's tests are written:"
)
TEST_STYLE_NOTE = """
{intro} their layout, fixtures, naming and assertion style. State that in `conventions`, with
citations into them. They are not among the files to explain: cite them in `conventions` only.
{files}
"""

PATH_RULE = (
    "- `path` is the path exactly as listed above, relative to your working directory "
    "(the project root)."
)

HEADINGS = ("Architecture", "Patterns", "Dependencies", "Conventions")
# The first line of the report written when there is no commit to read the code at.
NOT_ANALYSED_HEADING = "# Codebase analysis: not analysed"
# Working-tree source files listed in that report; the rest are counted.
MAX_LISTED_FILES = 100


@dataclass
class ProjectGit:
    """Where the project sits in its repo, and the commit read."""

    root: Path  # the repo root
    prefix: str  # the project dir relative to the root, "" or ending in "/"
    sha: str

    def resolver(self) -> Resolve:
        def resolve(path: str, side: str):
            if side != "head" or not path:
                return None
            return str(self.root), self.sha, self.prefix + path

        return resolve

    def text(self, path: str) -> str | None:
        return file_at(str(self.root), self.sha, self.prefix + path)


def project_git(project_dir: Path) -> ProjectGit | None:
    """The project's repo, prefix and HEAD commit, or None when it has none."""
    root = git_ops.repo_root(project_dir)
    prefix = git_ops.path_prefix(project_dir) if root else None
    sha = git_ops.rev_parse_head(project_dir) if root else None
    if root is None or prefix is None or sha is None:
        return None
    return ProjectGit(root=root, prefix=prefix, sha=sha)


# --- the scope -----------------------------------------------------------------------


def is_test_file(path: str) -> bool:
    """codebase-walkthrough's `--depth overview` rule: under `tests/`, `test_*`, or `tests.py`."""
    p = PurePosixPath(path)
    return "tests" in p.parts[:-1] or p.name.startswith("test_") or p.name == "tests.py"


def _is_source(path: str) -> bool:
    from .orchestrator import _SOURCE_SUFFIXES

    return PurePosixPath(path).suffix in _SOURCE_SUFFIXES


def is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in MANIFEST_NAMES or _REQUIREMENTS.fullmatch(name) is not None


def is_config_file(path: str) -> bool:
    """A manifest, or a file with a config suffix that is not a lock file or a test fixture."""
    p = PurePosixPath(path)
    if p.name in LOCK_NAMES or is_test_file(path) or _is_source(path):
        return False
    return is_manifest(path) or p.suffix in CONFIG_SUFFIXES


def _files_at_head(project_dir: Path, git: ProjectGit, want) -> dict[str, str]:
    """{path: text} of the files at HEAD that *want* accepts, leaving out vendored,
    generated and tool directories, binary files and files over MAX_FILE_BYTES."""
    out: dict[str, str] = {}
    for path in git_ops.files_at(project_dir, git.sha) or []:
        p = PurePosixPath(path)
        if any(part in SKIP_DIRS or part.startswith(".") for part in p.parts[:-1]):
            continue
        if p.name.endswith(GENERATED_SUFFIXES) or not want(path):
            continue
        text = git.text(path)
        if (
            text is None
            or "\0" in text
            or len(text.encode("utf-8", errors="replace")) >= MAX_FILE_BYTES
        ):
            continue
        out[path] = text
    return out


def source_files(project_dir: Path, git: ProjectGit) -> dict[str, str]:
    """{path: text} of the project's source files at its HEAD commit.

    Source means a suffix in the orchestrator's source list; test files,
    vendored, generated and tool directories, binary files and files over
    MAX_FILE_BYTES are left out.
    """
    return _files_at_head(
        project_dir, git, lambda p: _is_source(p) and not is_test_file(p)
    )


def project_tests(project_dir: Path, git: ProjectGit) -> dict[str, str]:
    """{path: text} of the project's test files at HEAD, by `source_files`' other rules."""
    return _files_at_head(project_dir, git, lambda p: _is_source(p) and is_test_file(p))


def config_files(project_dir: Path, git: ProjectGit) -> dict[str, str]:
    """{path: text} of the project's manifests and config files at HEAD, by `source_files`'
    other rules, without lock files and files over MAX_CONFIG_LINES lines."""
    return {
        p: t
        for p, t in _files_at_head(project_dir, git, is_config_file).items()
        if line_count(t) <= MAX_CONFIG_LINES
    }


def working_tree_source_files(project_dir: Path) -> list[str]:
    """The project's source files on disk, by the same rules as `source_files`
    (no test, vendored, generated or tool files), for a project with no commit."""
    out: list[str] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(files):
            path = (Path(root) / name).relative_to(project_dir).as_posix()
            if (
                _is_source(path)
                and not name.endswith(GENERATED_SUFFIXES)
                and not is_test_file(path)
            ):
                out.append(path)
    return out


def no_commit_report(project_dir: Path) -> str:
    """What `codebase-analysis.md` says when the project has source files and no commit
    (a `--no-worktree` build `git init`s without committing): that nothing was
    analysed and why, and which source files exist, so the design does not read
    the project as new."""
    files = working_tree_source_files(project_dir)
    lines = [
        NOT_ANALYSED_HEADING,
        "",
        "The project has no git commit, and the codebase analysis reads the code at a commit so "
        "that every fact it reports can be checked there. Nothing below describes what the code "
        "does. The project is not new: these source files are in its working tree. To get the "
        "analysis, commit them before the build starts.",
        "",
    ]
    lines += [f"- `{p}`" for p in files[:MAX_LISTED_FILES]]
    if len(files) > MAX_LISTED_FILES:
        lines.append(f"- … and {len(files) - MAX_LISTED_FILES} more")
    return "\n".join(lines) + "\n"


def line_count(text: str) -> int:
    return len(text.split("\n")) - (1 if text.endswith("\n") else 0)


_PATH_TOKEN = re.compile(r"[\w./-]+")


def _named_in(text: str, paths: list[str]) -> set[str]:
    """The paths *text* names: the whole path, a file name only one of *paths* has,
    or a partial path (`stages/trace.py`) only one of *paths* ends with."""
    names: dict[str, list[str]] = {}
    for p in paths:
        names.setdefault(PurePosixPath(p).name, []).append(p)
    found = set()
    for p in paths:
        if re.search(rf"(?<![\w./-]){re.escape(p)}(?![\w/-])", text):
            found.add(p)
    for name, owners in names.items():
        if (
            len(owners) == 1
            and "." in name
            and re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w/-])", text)
        ):
            found.add(owners[0])
    for token in {t.rstrip(".").removeprefix("./") for t in _PATH_TOKEN.findall(text)}:
        if "/" in token and re.search(r"\.\w+$", token):
            owners = [
                p
                for p in names.get(PurePosixPath(token).name, [])
                if p == token or p.endswith("/" + token)
            ]
            if len(owners) == 1:
                found.add(owners[0])
    return found


_IMPORT_LINE = re.compile(
    r"^\s*(?:from|import|require|use|using|include|#include|@import)\b.*$|require\(.*\)",
    re.MULTILINE,
)


def _module_word(path: str) -> str:
    p = PurePosixPath(path)
    return (
        p.parent.name
        if p.stem in ("__init__", "index", "mod") and p.parent.name
        else p.stem
    )


def import_words(text: str) -> set[str]:
    """The words on *text*'s import lines."""
    return set(re.findall(r"[\w-]+", " ".join(_IMPORT_LINE.findall(text))))


def inbound_imports(texts: dict[str, str]) -> dict[str, int]:
    """For each file, how many other files have an import line naming its module.

    A text heuristic, not a resolver: a module is named by its file stem (its
    directory for `__init__`/`index`/`mod`), matched as a whole word among the
    words of each other file's import lines. One pass over the words, so a
    large repo costs no more than reading its import lines.
    """
    words = {p: import_words(t) for p, t in texts.items()}
    files_naming: Counter[str] = Counter()
    for ws in words.values():
        files_naming.update(ws)
    counts: dict[str, int] = {}
    for path in texts:
        word = _module_word(path)
        counts[path] = files_naming[word] - (1 if word in words[path] else 0)
    return counts


def spec_text(change_dir: Path | None) -> str:
    """The change's own documents so far: its Markdown files and requirements."""
    if change_dir is None or not change_dir.is_dir():
        return ""
    parts = []
    for f in sorted(change_dir.glob("*.md")) + sorted(
        (change_dir / "requirements").glob("*.md")
    ):
        if f.name == "codebase-analysis.md":
            continue
        try:
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def limit_scope(
    lines: dict[str, int], ranked: list[str], max_units: int
) -> tuple[Scope, list[str]]:
    """(the scope of the longest prefix of *ranked* that fits in *max_units* units, the files left out)."""

    def scope_of(paths: list[str]) -> Scope:
        return Scope.whole_files([(p, lines[p]) for p in sorted(paths)])

    if len(partition(scope_of(ranked), UNIT_LINES)) <= max_units:
        return scope_of(ranked), []
    lo, hi = 0, len(ranked)  # the largest k whose first k files fit
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(partition(scope_of(ranked[:mid]), UNIT_LINES)) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    return scope_of(ranked[:lo]), ranked[lo:]


def rank_files(texts: dict[str, str], named: set[str]) -> list[str]:
    """Files the spec names first, then the most imported, then by path."""
    inbound = inbound_imports(texts)
    return sorted(texts, key=lambda p: (p not in named, -inbound[p], p))


def dependency_slice(lines: dict[str, int]) -> tuple[list[str], list[str]]:
    """(the manifest and config files one slice reads, the ones left out).

    Manifests first, then the files nearest the project root, then by path,
    while they fit in UNIT_LINES lines; the first file always goes in.
    """
    ranked = sorted(
        lines, key=lambda p: (not is_manifest(p), len(PurePosixPath(p).parts), p)
    )
    kept: list[str] = []
    size = 0
    for i, p in enumerate(ranked):
        if kept and size + lines[p] > UNIT_LINES:
            return kept, ranked[i:]
        kept.append(p)
        size += lines[p]
    return kept, []


def tests_for_units(units: list[Unit], tests: dict[str, str]) -> dict[str, list[str]]:
    """{unit id: the test files it is pointed at for test style}.

    Each unit gets up to MAX_TEST_EXAMPLES test files whose import lines name
    its modules, those naming the most first. When no test names a module of
    any unit, the first unit gets the first ones by path, so test style is
    still read once.
    """
    words = {p: import_words(t) for p, t in tests.items()}
    out: dict[str, list[str]] = {}
    for u in units:
        modules = {_module_word(p) for p in u.files}
        scored = sorted((-len(modules & w), p) for p, w in words.items() if modules & w)
        if scored:
            out[u.id] = [p for _, p in scored[:MAX_TEST_EXAMPLES]]
    if not out and units and tests:
        out[units[0].id] = sorted(tests)[:MAX_TEST_EXAMPLES]
    return out


def _test_note(paths: list[str]) -> str:
    if not paths:
        return ""
    return TEST_STYLE_NOTE.format(
        intro=TEST_STYLE_INTRO, files="\n".join(f"- {p}" for p in paths)
    )


# --- the analysis ---------------------------------------------------------------------


@dataclass
class Analysis:
    gated: Gated
    git: ProjectGit
    scope: Scope
    not_analysed: list[str] = field(default_factory=list)
    # Manifest and config files the dependency slice did not read.
    config_not_analysed: list[str] = field(default_factory=list)
    failed: list[tuple[list[str], str]] = field(
        default_factory=list
    )  # (unit files, error)
    uncovered: list[str] = field(default_factory=list)


def uncovered(scope: Scope, facts: list[Fact]) -> list[str]:
    """Files in the scope, over SMALL_FILE_LINES lines, that no surviving fact cites."""
    cited = {c.path for f in facts for c in f.citations}
    return [
        s.path
        for s in scope.spans
        if s.lines > SMALL_FILE_LINES and s.path not in cited
    ]


async def analyse(
    project_dir: Path,
    *,
    call: CallFn,
    max_units: int,
    name: str,
    change_dir: Path | None = None,
) -> Analysis | None:
    """Explain the project's source files at HEAD. None when it is not a git repo with a commit.

    A unit whose call raises is recorded in `failed` and the rest go on, so a
    partial picture still reaches the design.
    """
    git = project_git(project_dir)
    if git is None:
        return None
    texts = source_files(project_dir, git)
    lines = {p: line_count(t) for p, t in texts.items()}
    config_lines = {p: line_count(t) for p, t in config_files(project_dir, git).items()}
    # The dependency slice is one of the max_units; with only one, the source gets it.
    if max_units > 1:
        deps, config_not_analysed = dependency_slice(config_lines)
    else:
        deps, config_not_analysed = [], sorted(config_lines)
    ranked = rank_files(texts, _named_in(spec_text(change_dir), list(texts)))
    scope, not_analysed = limit_scope(
        lines, ranked, max_units - 1 if deps else max_units
    )
    units = partition(scope, UNIT_LINES)
    examples = tests_for_units(units, project_tests(project_dir, git))
    label = name or project_dir.name

    answers: dict[str, dict] = {}
    tasks = [
        (
            unit_key(u),
            explore_unit(
                label,
                u.spans,
                ExploreOutput,
                "standard",
                conventions=True,
                path_rule=PATH_RULE,
                notes=_test_note(examples.get(u.id, [])),
            ),
        )
        for u in units
    ]
    if deps:
        deps_spans = [Span.whole(p, config_lines[p]) for p in deps]
        units.append(
            Unit(
                id=DEPENDENCY_UNIT_ID,
                files=deps,
                lines=sum(config_lines[p] for p in deps),
                spans=deps_spans,
            )
        )
        tasks.append(
            (
                unit_key(units[-1]),
                explore_unit(
                    label,
                    deps_spans,
                    ExploreOutput,
                    "standard",
                    conventions=True,
                    path_rule=PATH_RULE,
                    notes=DEPENDENCY_NOTE,
                ),
            )
        )
        scope = Scope(spans=scope.spans + deps_spans)
    await run_tasks(
        tasks,
        call,
        answers,
        concurrency=CONCURRENCY,
        call_kwargs=dict(
            allowed_tools=ANALYSIS_TOOLS, max_turns=MAX_TURNS, cwd=str(project_dir)
        ),
        errors=[],
        recoverable=(Exception,),
    )
    keep = citation_keeper(git.resolver(), root=project_dir)
    gated = gate_answers(answers, keep, conventions=True)
    by_key = {unit_key(u): u for u in units}
    failed = [
        (by_key[k].files, a["error"])
        for k, a in answers.items()
        if "error" in a and k in by_key
    ]
    return Analysis(
        gated=gated,
        git=git,
        scope=scope,
        not_analysed=not_analysed,
        config_not_analysed=config_not_analysed,
        failed=failed,
        uncovered=uncovered(scope, gated.facts),
    )


# --- codebase-analysis.md -------------------------------------------------------------


def _where(c: Citation) -> str:
    return (
        f"{c.path}:{c.line_start}"
        if c.line_end == c.line_start
        else f"{c.path}:{c.line_start}-{c.line_end}"
    )


def _link(c: Citation, project_dir: Path, md_dir: Path) -> str:
    target = os.path.relpath(project_dir / c.path, md_dir).replace(os.sep, "/")
    return f"[`{_where(c)}`]({target}#L{c.line_start})"


def _bullet(
    claim: str, citations: list[Citation], project_dir: Path, md_dir: Path
) -> str:
    links = ", ".join(_link(c, project_dir, md_dir) for c in citations)
    return f"- {' '.join(claim.split())} ({links})"


def render_markdown(a: Analysis, project_dir: Path, md_dir: Path) -> str:
    """The analysis as Markdown: Architecture, Patterns, Dependencies, Conventions, then what was left out.

    Architecture holds each source file's purpose and the facts about what
    the code does; Patterns the gotchas and the domain terms; Dependencies
    what the manifest and config files declare (dependencies, configuration,
    external services), then the interfaces other code calls; Conventions the
    rules new code must follow, test style included. Each claim is followed by
    its `path:line` links, relative to *md_dir*.
    """
    g = a.gated
    deps_source = unit_key(Unit(id=DEPENDENCY_UNIT_ID, files=[], lines=0)) + "/"
    by_bucket: dict[str, list[Fact]] = {}
    for f in g.facts:
        by_bucket.setdefault(f.source.rsplit("/", 1)[-1], []).append(f)
    sections: dict[str, list[str]] = {h: [] for h in HEADINGS}
    for f in by_bucket.get("file", []) + by_bucket.get("fact", []):
        heading = "Dependencies" if f.source.startswith(deps_source) else "Architecture"
        sections[heading].append(_bullet(f.claim, f.citations, project_dir, md_dir))
    for f in by_bucket.get("gotcha", []):
        sections["Patterns"].append(_bullet(f.claim, f.citations, project_dir, md_dir))
    for t in g.terms:
        sections["Patterns"].append(
            _bullet(
                f"Term **{t.term}**: {t.meaning}", [t.citation], project_dir, md_dir
            )
        )
    for f in by_bucket.get("interface", []):
        sections["Dependencies"].append(
            _bullet(f.claim, f.citations, project_dir, md_dir)
        )
    for f in by_bucket.get("convention", []):
        sections["Conventions"].append(
            _bullet(f.claim, f.citations, project_dir, md_dir)
        )

    out = [
        f"Every `path:line` below was re-read at commit `{a.git.sha}`. "
        f"{len(g.dropped)} claim(s) whose citations were not found there were dropped."
    ]
    for h in HEADINGS:
        out += ["", f"## {h}", ""]
        out += sections[h] or ["(no checked facts)"]
    if a.not_analysed or a.config_not_analysed or a.failed or a.uncovered:
        out += ["", "## Not analysed", ""]
        if a.not_analysed:
            out.append(
                f"Left out by `codebase_analysis_max_units` ({len(a.not_analysed)} file(s)):"
            )
            out += [f"- `{p}`" for p in a.not_analysed]
        if a.config_not_analysed:
            out.append(
                f"Manifest and config files not read ({len(a.config_not_analysed)} file(s); the "
                f"dependency slice reads up to {UNIT_LINES} lines of them, and none when "
                f"`codebase_analysis_max_units` is 1):"
            )
            out += [f"- `{p}`" for p in a.config_not_analysed]
        for files, error in a.failed:
            out.append(
                f"- (analysis failed: {', '.join(f'`{p}`' for p in files)}: {error})"
            )
        if a.uncovered:
            out.append("Read, but no fact about them passed the check:")
            out += [f"- `{p}`" for p in a.uncovered]
    return "\n".join(out) + "\n"


# --- facts.json --------------------------------------------------------------------------


def facts_document(a: Analysis) -> dict:
    """`facts.json` in review's format (`review/schema/facts.v1.schema.json`), kind `tree`."""
    g = a.gated
    by_id = {f.id: f for f in g.facts}
    files = []
    for path, purpose in g.purposes.items():
        cite = next(
            (
                f.citations[0]
                for f in g.facts
                if f.source.endswith("/file") and f.citations[0].path == path
            ),
            None,
        )
        files.append(
            {
                "path": path,
                "purpose": purpose,
                "citation": cite.model_dump() if cite else None,
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "base_sha": EMPTY_TREE,
        "head_sha": a.git.sha,
        "kind": "tree",
        "facts": [f.model_dump() for f in g.facts],
        "files": files,
        "interfaces": [by_id[i].model_dump() for i in g.interfaces],
        "gotchas": [by_id[i].model_dump() for i in g.gotchas],
        "conventions": [by_id[i].model_dump() for i in g.conventions],
        "terms": [t.model_dump() for t in g.terms],
        "uncovered": a.uncovered,
        "dropped": [d.model_dump() for d in g.dropped],
    }


def write_facts_file(a: Analysis, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FACTS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(facts_document(a), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


# --- facts for one block ----------------------------------------------------------------------

BLOCK_FACTS_HEADING = "## Checked facts about the code this block touches"


def _block_text(block) -> str:
    return "\n".join([block.name, block.description, *block.produces, *block.consumes])


def block_facts_section(block, facts_path: Path, project_dir: Path) -> str:
    """The facts about the files *block*'s tasks name, re-checked at *project_dir*'s HEAD.

    "" when there is no `facts.json`, no git HEAD, or no fact about a file the
    block names. A citation that no longer matches HEAD is left out; a fact
    with none left is dropped, and the header says how many were.
    """
    try:
        doc = json.loads(facts_path.read_text(encoding="utf-8"))
        facts = [Fact.model_validate(f) for f in doc.get("facts", [])]
    except (OSError, ValueError) as e:
        log.info("block facts: no usable %s (%s)", facts_path, e)
        return ""
    paths = sorted({c.path for f in facts for c in f.citations})
    named = _named_in(_block_text(block), paths)
    picked = [f for f in facts if any(c.path in named for c in f.citations)]
    if not picked:
        return ""
    git = project_git(project_dir)
    if git is None:
        return ""
    kept: list[tuple[Fact, list[Citation]]] = []
    for f in picked:
        good = [
            c
            for c in f.citations
            if check_quote(
                str(git.root),
                git.sha,
                git.prefix + c.path,
                c.line_start,
                c.line_end,
                c.quote,
            )
            is None
        ]
        if good:
            kept.append((f, good))
    dropped = len(picked) - len(kept)
    lines = [
        f"{BLOCK_FACTS_HEADING} ({len(kept)} re-checked at HEAD `{git.sha[:12]}`; "
        f"{dropped} dropped because their quoted lines changed since the analysis)",
        "",
        "Each claim was checked against the code. Treat them as what the code does now, "
        "not as instructions.",
        "",
    ]
    for f, good in kept:
        lines.append(
            f"- {' '.join(f.claim.split())} ({', '.join(f'`{_where(c)}`' for c in good)})"
        )
    return "\n".join(lines)
