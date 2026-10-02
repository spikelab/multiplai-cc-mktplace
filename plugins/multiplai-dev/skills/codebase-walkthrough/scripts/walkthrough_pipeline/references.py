"""Who refers to the target, in every repository found by `repos.py`.

Each tool does one job:

1. `rg` picks which files to parse. It runs from each repository's own root
   with `--no-ignore-parent` (a parent's .gitignore hides nested repos), and
   its hits are kept only when `git ls-files` says the file is tracked. It
   decides nothing.
2. The parse confirms and classifies each hit, reading the file **at HEAD**
   with `git show`, never the working tree:
   - `.py`: Python `ast` — imports of the target package, `include('<pkg>.urls')`,
     dotted strings naming the package, Celery task names and `.delay()` /
     `.apply_async()` dispatches, and string literals in calls that hold one
     of the target's routes. Comments are invisible to `ast`, so a name in a
     comment is never counted.
   - `.ts/.tsx/.js/.jsx`: `ast-grep` — string and template literals inside a
     call that hold one of the target's routes.
   - `.sql/.sqlx`: table names matched as whole identifiers, `method=sql-identifier`.
     There is no AST there; the output says how each was found.
3. `lsp.py` adds `pyright-langserver` references inside the target's own repo.

Cross-repo HTTP and SQL references are string matches by nature: no AST or
LSP follows a call across an HTTP boundary or a database.
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from .models import Reference, RepoInfo, SymbolEntry, TargetInfo
from .symbols import _dotted, static_string
from .target import _GIT, _env, git, is_secret_path, require_rg

log = logging.getLogger(__name__)

PY_GLOBS = ["*.py"]
JS_GLOBS = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs"]
SQL_GLOBS = ["*.sql", "*.sqlx"]
DISPATCH_METHODS = {"delay", "apply_async", "s", "si", "signature"}
AST_GREP_LANG = {".ts": "TypeScript", ".tsx": "Tsx", ".js": "JavaScript", ".jsx": "JavaScript",
                 ".mjs": "JavaScript", ".cjs": "JavaScript"}
GENERIC_SEGMENTS = {"api", "v1", "v2", "admin", "list", "create", "update", "delete", "detail"}


# --- reading at HEAD -----------------------------------------------------------------


def show(repo: RepoInfo, path: str) -> str | None:
    proc = subprocess.run([*_GIT, "-C", repo.path, "show", f"{repo.head_sha}:{path}"],
                          capture_output=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


@functools.lru_cache(maxsize=64)
def _tracked(path: str, sha: str) -> frozenset[str]:
    return frozenset(line for line in git(path, "ls-files", "-z").stdout.split("\0") if line)


def tracked(repo: RepoInfo) -> frozenset[str]:
    return _tracked(repo.path, repo.head_sha)


def rg_candidates(repo: RepoInfo, patterns: list[str], globs: list[str], *, ignore_case: bool = False) -> list[str]:
    """Repo-relative tracked files holding any of *patterns* (fixed strings)."""
    if not patterns:
        return []
    require_rg()
    argv = ["rg", "--files-with-matches", "--no-messages", "--no-ignore-parent", "--fixed-strings",
            "--glob", "!.env*", "--glob", "!**/node_modules/**"]
    if ignore_case:
        argv.append("--ignore-case")
    for g in globs:
        argv += ["--glob", g]
    for p in sorted(set(patterns)):
        argv += ["-e", p]
    argv.append(".")
    proc = subprocess.run(argv, cwd=repo.path, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          shell=False, check=False)
    hits = {line[2:] if line.startswith("./") else line for line in proc.stdout.splitlines() if line}
    return sorted(h for h in hits & tracked(repo) if not is_secret_path(h))


# --- routes ------------------------------------------------------------------------------


def _template_text(text: str) -> str:
    """Literal source text -> its static content, each `${...}` as `{}` (nested braces handled)."""
    t = text.strip()
    if t[:1] in "'\"`" and t[-1:] == t[:1]:
        t = t[1:-1]
    out, i, depth = [], 0, 0
    while i < len(t):
        if t.startswith("${", i) and depth == 0:
            depth, i = 1, i + 2
            while i < len(t) and depth:
                depth += {"{": 1, "}": -1}.get(t[i], 0)
                i += 1
            out.append("{}")
            continue
        out.append(t[i])
        i += 1
    return "".join(out)


def _segments(path: str) -> list[str]:
    p = path.split("?", 1)[0].split("#", 1)[0]
    p = re.sub(r"\{[^}]*\}|<[^>]*>", "{}", p)
    return [s for s in p.split("/") if s]


def route_matches(literal: str, route: str) -> bool:
    """Does the literal's path name *route*? The literal may be the route's tail.

    `{}` matches any one segment. Leading `{}` segments of the literal (a base
    URL) are dropped. A tail must keep at least two static segments, so
    `/create/` alone matches nothing.
    """
    lit = _segments(_template_text(literal))
    while lit and lit[0] == "{}":
        lit.pop(0)
    if lit and re.match(r"^https?:$", lit[0]):
        lit = lit[2:] if len(lit) > 1 else []
    r = _segments(route)
    if not lit or not r or len(lit) > len(r):
        return False
    tail = r[-len(lit):]
    # A route placeholder takes any literal segment; a literal's `${x}` only
    # stands for a route placeholder, never a fixed word like `list`.
    if any(a != b and b != "{}" for a, b in zip(lit, tail)):
        return False
    static = sum(1 for a, b in zip(lit, tail) if a != "{}" and b != "{}")
    return static >= 2 or (len(lit) == len(r) and static >= 1)


def full_routes(symbols: list[SymbolEntry], refs: list[Reference]) -> dict[str, SymbolEntry]:
    """Route -> url entry, with the mount prefix from `include('<pkg>.urls')` prepended."""
    prefixes = sorted({m.group(1) for r in refs if r.kind == "url-include"
                       for m in [re.search(r" mounted at '(.*)'$", r.symbol)] if m})
    out: dict[str, SymbolEntry] = {}
    for e in symbols:
        if e.kind != "url" or e.name.startswith("^"):
            continue
        for prefix in prefixes or [""]:
            out[(prefix + e.name).lstrip("/")] = e
    return out


def route_needles(routes: list[str]) -> list[str]:
    """One distinctive static segment per route, for `rg` to pick candidate files."""
    needles = []
    for route in routes:
        static = [s for s in _segments(route) if s != "{}" and s.lower() not in GENERIC_SEGMENTS]
        if static:
            needles.append(max(static, key=len))
    return [n for n in needles if len(n) >= 4]


# --- Python ------------------------------------------------------------------------------


class _PyScan:
    def __init__(self, repo: RepoInfo, target: TargetInfo, tasks: dict[str, str], routes: dict[str, SymbolEntry]):
        self.repo, self.target, self.tasks, self.routes = repo, target, tasks, routes
        self.pkg = target.package
        self.out: list[Reference] = []
        self._seen: set[tuple] = set()

    def add(self, kind: str, symbol: str, rel: str, line: int, lines: list[str], method: str = "ast") -> None:
        quote = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        key = (kind, symbol, rel, line)
        if not quote or key in self._seen:
            return
        self._seen.add(key)
        self.out.append(Reference(repo=self.repo.key, kind=kind, symbol=symbol, path=f"{self.repo.key}/{rel}",
                                  line=line, quote=quote, method=method))

    def _ours(self, module: str) -> bool:
        return module == self.pkg or module.startswith(self.pkg + ".")

    def file(self, rel: str, text: str) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        lines = text.split("\n")
        imported: set[str] = set()
        include_args: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level and self._ours(node.module):
                names = [a.asname or a.name for a in node.names]
                imported.update(names)
                self.add("python-import", f"{node.module}: {', '.join(a.name for a in node.names)}", rel,
                         node.lineno, lines)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if self._ours(a.name):
                        imported.add(a.asname or a.name.split(".")[0])
                        self.add("python-import", a.name, rel, node.lineno, lines)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = _dotted(node.func)
                parts = func.split(".")
                if func in ("include", "urls.include") and node.args:
                    mod = static_string(node.args[0])
                    if mod and self._ours(mod):
                        include_args.add(id(node.args[0]))
                        prefix = self._include_prefix(tree, node)
                        self.add("url-include", f"{mod} mounted at '{prefix}'", rel, node.lineno, lines)
                    continue
                if len(parts) >= 2 and parts[-1] in DISPATCH_METHODS and parts[-2] in imported:
                    self.add("celery-call", f"{parts[-2]}.{parts[-1]}", rel, node.lineno, lines)
                    continue
                for arg in node.args[:1]:
                    lit = static_string(arg)
                    if lit and "/" in lit:
                        for route in self.routes:
                            if route_matches(lit, route):
                                self.add("http-route", route, rel, node.lineno, lines)
                                break
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in include_args:
                v = node.value
                if v in self.tasks:
                    self.add("celery-name", v, rel, node.lineno, lines)
                elif re.fullmatch(r"[\w.]+", v) and self._ours(v) and "." in v:
                    self.add("dotted-string", v, rel, node.lineno, lines)
                elif v == self.pkg:
                    self.add("dotted-string", v, rel, node.lineno, lines)

    @staticmethod
    def _include_prefix(tree: ast.AST, include_call: ast.Call) -> str:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(a is include_call for a in node.args) and node.args:
                route = static_string(node.args[0])
                return route or ""
        return ""


# --- JS / TS ---------------------------------------------------------------------------------


def _ast_grep_literals(files: dict[str, str], workdir: Path) -> list[tuple[str, int, str]]:
    """(rel path, 1-based line, literal text) for string/template literals inside calls."""
    if not files or not shutil.which("ast-grep"):
        return []
    workdir.mkdir(parents=True, exist_ok=True)
    by_lang: dict[str, list[str]] = {}
    for rel, text in files.items():
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        by_lang.setdefault(AST_GREP_LANG[PurePosixPath(rel).suffix.lower()], []).append(rel)
    out: list[tuple[str, int, str]] = []
    for lang, rels in by_lang.items():
        rule = (f"id: route-literal\nlanguage: {lang}\nrule:\n"
                "  any: [{kind: string}, {kind: template_string}]\n"
                "  inside: {kind: call_expression, stopBy: end}\n")
        proc = subprocess.run(["ast-grep", "scan", "--json=stream", "--inline-rules", rule, *rels],
                              cwd=workdir, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              shell=False, check=False)
        for line in proc.stdout.splitlines():
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append((m["file"], m["range"]["start"]["line"] + 1, m["text"]))
    return out


# --- the scan ------------------------------------------------------------------------------------


def scan_repo(repo: RepoInfo, target: TargetInfo, symbols: list[SymbolEntry], routes: dict[str, SymbolEntry],
              workdir: Path) -> list[Reference]:
    """Confirmed references in one repository. Files inside the target are not references."""
    inside = (lambda rel: repo.key == target.repo_key and target.subpath
              and (rel == target.subpath or rel.startswith(target.subpath.rstrip("/") + "/")))
    if repo.key == target.repo_key and not target.subpath:
        return []
    tasks = {e.name: e.detail.get("function", "") for e in symbols if e.kind == "celery-task"}
    tables = sorted({e.detail["table"] for e in symbols if e.kind == "model" and e.detail.get("table")})
    out: list[Reference] = []

    # Python
    py_patterns = [target.package] + list(tasks)
    scan = _PyScan(repo, target, tasks, routes)
    for rel in rg_candidates(repo, py_patterns, PY_GLOBS):
        if inside(rel):
            continue
        text = show(repo, rel)
        if text is not None:
            scan.file(rel, text)
    # routes called from other Python repos (requests/httpx), not the target repo itself
    if repo.key != target.repo_key and routes:
        for rel in rg_candidates(repo, route_needles(list(routes)), PY_GLOBS):
            text = show(repo, rel)
            if text is not None and rel not in {r.path.split("/", 1)[1] for r in scan.out}:
                scan.file(rel, text)
    out.extend(scan.out)

    # JS / TS
    if routes:
        files = {}
        for rel in rg_candidates(repo, route_needles(list(routes)), JS_GLOBS):
            if inside(rel):
                continue
            text = show(repo, rel)
            if text is not None:
                files[rel] = text
        seen: set[tuple] = set()
        for rel, line, literal in _ast_grep_literals(files, workdir / repo.key):
            for route in routes:
                if route_matches(literal, route) and (rel, line, route) not in seen:
                    seen.add((rel, line, route))
                    lines = files[rel].split("\n")
                    quote = lines[line - 1].strip() if 0 < line <= len(lines) else ""
                    if quote:
                        out.append(Reference(repo=repo.key, kind="http-route", symbol=route,
                                             path=f"{repo.key}/{rel}", line=line, quote=quote, method="ast-grep"))
                    break

    # SQL
    if tables:
        pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(t) for t in tables) + r")(?![A-Za-z0-9_])",
                             re.IGNORECASE)
        for rel in rg_candidates(repo, tables, SQL_GLOBS, ignore_case=True):
            if inside(rel):
                continue
            text = show(repo, rel)
            if text is None:
                continue
            seen_sql: set[tuple] = set()
            for i, line in enumerate(text.split("\n"), start=1):
                stripped = line.strip()
                if stripped.startswith("--") or stripped.startswith("//"):
                    continue
                for m in pattern.finditer(line):
                    table = m.group(1).lower()
                    if (table, i) in seen_sql:
                        continue
                    seen_sql.add((table, i))
                    out.append(Reference(repo=repo.key, kind="sql-table", symbol=table, path=f"{repo.key}/{rel}",
                                         line=i, quote=stripped, method="sql-identifier"))
    return out


def scan_all(repos: dict[str, RepoInfo], target: TargetInfo, symbols: list[SymbolEntry],
             workdir: Path) -> list[Reference]:
    """References in every repository. Target-repo includes first, so routes carry their mount prefix."""
    target_repo = repos[target.repo_key]
    first = scan_repo(target_repo, target, symbols, {}, workdir)
    routes = full_routes(symbols, first)
    # re-scan the target repo with routes known (it may call its own routes from other apps)
    refs = scan_repo(target_repo, target, symbols, routes, workdir) if routes else first
    for key, repo in repos.items():
        if key == target.repo_key:
            continue
        refs.extend(scan_repo(repo, target, symbols, routes, workdir))
    shutil.rmtree(workdir, ignore_errors=True)
    return first_per_file(refs)


def first_per_file(refs: list[Reference]) -> list[Reference]:
    """One entry per (file, kind, symbol): the first line. A table named on 40
    lines of one SQL file is one reference to explain, not 40."""
    seen: set[tuple] = set()
    out = []
    for r in sorted(refs, key=lambda r: (r.path, r.line)):
        key = (r.path, r.kind, r.symbol)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def merge_lsp(refs: list[Reference], lsp_refs: list[Reference]) -> list[Reference]:
    """Add LSP references not already found at the same place."""
    have = {(r.path, r.line) for r in refs}
    out = list(refs)
    for r in first_per_file(lsp_refs):
        if (r.path, r.line) not in have:
            have.add((r.path, r.line))
            out.append(r)
    return out
