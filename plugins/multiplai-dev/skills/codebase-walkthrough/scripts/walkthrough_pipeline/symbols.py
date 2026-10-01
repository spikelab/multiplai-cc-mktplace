"""What the target defines and what it reaches out to, read with Python's `ast`.

Only the Python/Django language pack exists. A target file in another
programming language produces a `no symbol pack for <lang>` warning, never a
silent skip.

Every entry is `{kind, name, path, line, quote}` taken from the snapshot: the
quote is the stripped source line at `line`, and `boundary_gate` re-reads it
from the commit.

Defines (`direction="defines"`):
  function, class        top-level, outside test files
  url                    `path()`/`re_path()`/`url()` in a urls.py; name = route
  celery-task            a function decorated with `shared_task`/`*.task`; name = task name
  management-command     `management/commands/<name>.py`
  model                  a Django model class; detail.table = its table name, lower-cased

Calls out (`direction="calls"`):
  import                 a module of another local package
  http                   a call whose first argument is a path literal (`'/bookings'`) or an absolute URL
  setting                `settings.NAME`
  env                    `os.environ[...]`, `os.environ.get`, `os.getenv`, `config(...)`, `env(...)`
  cache                  `cache.get/set/...`
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path, PurePosixPath

from .models import SymbolEntry, TargetInfo

log = logging.getLogger(__name__)

CODE_LANGS = {".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
              ".go": "Go", ".rb": "Ruby", ".java": "Java", ".kt": "Kotlin", ".swift": "Swift",
              ".rs": "Rust", ".php": "PHP", ".cs": "C#", ".scala": "Scala", ".c": "C", ".cpp": "C++"}
HTTP_WORDS = ("get", "post", "put", "patch", "delete", "request", "page", "fetch", "head")
CACHE_METHODS = {"get", "set", "delete", "get_or_set", "incr", "decr", "add", "get_many", "set_many",
                 "delete_many", "touch", "expire", "ttl"}
ENV_FUNCS = {"config", "env", "getenv"}
URL_FUNCS = {"path", "re_path", "url"}
PATH_LITERAL = re.compile(r"^/[A-Za-z_{][^\s]*$")


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return "tests" in p.parts[:-1] or p.name.startswith("test_") or p.name in ("tests.py", "conftest.py")


def _line(lines: list[str], n: int) -> str:
    return lines[n - 1].strip() if 0 < n <= len(lines) else ""


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def static_string(node: ast.AST, *, strip_base: bool = False) -> str | None:
    """The text of a str literal or f-string, with each `{expr}` as `{name}`.

    With *strip_base*, leading `{...}` parts whose expression names a URL or
    base (`{self.base_url}/x`) are dropped, so the path that follows is kept.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    values = list(node.values)
    if strip_base:
        while values and isinstance(values[0], ast.FormattedValue) and re.search(
                r"url|base|host|api|endpoint|root", ast.unparse(values[0].value), re.I):
            values.pop(0)
    for v in values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v, ast.FormattedValue):
            name = ast.unparse(v.value).split(".")[-1].strip("()[]'\" ")
            parts.append("{" + (re.sub(r"\W", "_", name) or "x") + "}")
    return "".join(parts)


def normalise_api_path(path: str) -> str:
    """`/booking_revisions/{revision_id}/ack?x=1` -> `/booking_revisions/{}/ack`; `{{x}}`, `:x`, `<int:x>` too."""
    p = path.split("?", 1)[0].split("#", 1)[0]
    p = re.sub(r"\{\{[^}]*\}\}|\{[^}]*\}|<[^>]*>|(?<=/):[A-Za-z_]\w*", "{}", p)
    p = re.sub(r"/+", "/", p)
    return p.rstrip("/") or "/"


def _app_label(snapshot_root: Path, target: TargetInfo) -> str:
    apps = snapshot_root / target.subpath / "apps.py" if target.subpath else snapshot_root / "apps.py"
    if apps.is_file():
        m = re.search(r"^\s*label\s*=\s*['\"]([^'\"]+)['\"]", apps.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return m.group(1)
    return target.package.split(".")[-1]


def _local_packages(snapshot_root: Path) -> set[str]:
    out = set()
    for child in snapshot_root.iterdir():
        if child.is_dir() and (child / "__init__.py").exists():
            out.add(child.name)
        elif child.suffix == ".py":
            out.add(child.stem)
    return out


def _is_model_class(node: ast.ClassDef, model_names: set[str]) -> bool:
    for base in node.bases:
        name = _dotted(base)
        last = name.split(".")[-1]
        if name in ("models.Model", "Model") or last in model_names or (last.endswith("Model") and "models" in name):
            return True
    return False


def _meta(node: ast.ClassDef) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in node.body:
        if isinstance(item, ast.ClassDef) and item.name == "Meta":
            for stmt in item.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    if isinstance(stmt.value, ast.Constant):
                        out[stmt.targets[0].id] = stmt.value.value
    return out


def _task_name(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ast.expr] | None:
    for dec in fn.decorator_list:
        name = _dotted(dec)
        if name.split(".")[-1] in ("shared_task", "task") or name.endswith(".task"):
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        return kw.value.value, dec
            return fn.name, dec
    return None


class _Collector:
    def __init__(self, target: TargetInfo, snapshot_root: Path):
        self.target = target
        self.root = snapshot_root
        self.entries: list[SymbolEntry] = []
        self._seen: set[tuple] = set()
        self.local = _local_packages(snapshot_root)
        self.top = target.package.split(".")[0]
        self.app_label = _app_label(snapshot_root, target)

    def add(self, kind: str, name: str, rel: str, line: int, lines: list[str], *, direction: str = "defines",
            dedupe: tuple | None = None, **detail: str) -> None:
        quote = _line(lines, line)
        if not quote or not name:
            return
        key = dedupe if dedupe is not None else (kind, name, rel, line)
        if key in self._seen:
            return
        self._seen.add(key)
        self.entries.append(SymbolEntry(kind=kind, name=name, path=self.target.ws(rel), line=line, quote=quote,
                                        direction=direction, detail={k: str(v) for k, v in detail.items() if v}))

    def file(self, rel: str) -> None:
        text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as e:
            log.warning("cannot parse %s: %s", rel, e)
            return
        p = PurePosixPath(rel)
        test = is_test_file(rel)
        model_names: set[str] = set()

        if len(p.parts) >= 3 and p.parts[-3:-1] == ("management", "commands") and not p.name.startswith("_"):
            line = next((n.lineno for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Command"), 1)
            self.add("management-command", p.stem, rel, line, lines)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                task = _task_name(node)
                if task:
                    tname, dec = task
                    self.add("celery-task", tname, rel, dec.lineno, lines, function=node.name)
                if not test:
                    self.add("function", node.name, rel, node.lineno, lines)
            elif isinstance(node, ast.ClassDef):
                if not test:
                    self.add("class", node.name, rel, node.lineno, lines)
                if (p.name == "models.py" or "models" in p.parts[:-1]) and _is_model_class(node, model_names):
                    model_names.add(node.name)
                    meta = _meta(node)
                    if meta.get("abstract") is True:
                        continue
                    table = str(meta.get("db_table") or f"{self.app_label}_{node.name}").lower()
                    self.add("model", node.name, rel, node.lineno, lines, table=table)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import(node, rel, lines)
            elif isinstance(node, ast.Call):
                self._call(node, rel, lines, test)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "settings":
                if node.attr.isupper() or "_" in node.attr:
                    self.add("setting", node.attr, rel, node.lineno, lines, direction="calls",
                             dedupe=("setting", node.attr, rel))
            elif isinstance(node, ast.Subscript) and _dotted(node.value) == "os.environ":
                key = static_string(node.slice)
                if key:
                    self.add("env", key, rel, node.lineno, lines, direction="calls", dedupe=("env", key, rel))

    def _import(self, node: ast.Import | ast.ImportFrom, rel: str, lines: list[str]) -> None:
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return
            modules = [node.module]
            names = ", ".join(a.name for a in node.names)
        else:
            modules = [a.name for a in node.names]
            names = ""
        for mod in modules:
            top = mod.split(".")[0]
            if top == self.top or top not in self.local:
                continue
            self.add("import", mod, rel, node.lineno, lines, direction="calls", dedupe=("import", mod, rel),
                     names=names, models="yes" if mod.endswith(".models") else "")

    def _call(self, node: ast.Call, rel: str, lines: list[str], test: bool) -> None:
        func = _dotted(node.func)
        last = func.split(".")[-1]
        if PurePosixPath(rel).name == "urls.py" and last in URL_FUNCS and node.args:
            route = static_string(node.args[0])
            if route is not None:
                view = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
                url_name = next((k.value.value for k in node.keywords
                                 if k.arg == "name" and isinstance(k.value, ast.Constant)), "")
                self.add("url", route, rel, node.lineno, lines, view=view, url_name=str(url_name))
            return
        if func in ("os.environ.get", "os.getenv", "environ.get") or (last in ENV_FUNCS and func == last) \
                or func.startswith("env."):
            if node.args:
                key = static_string(node.args[0])
                if key and re.fullmatch(r"[A-Z][A-Z0-9_]+", key):
                    self.add("env", key, rel, node.lineno, lines, direction="calls", dedupe=("env", key, rel))
            return
        if func.split(".")[0] == "cache" and last in CACHE_METHODS:
            key = static_string(node.args[0], strip_base=False) if node.args else None
            name = key or "<dynamic key>"
            self.add("cache", name, rel, node.lineno, lines, direction="calls", dedupe=("cache", name, rel),
                     method=last)
            return
        if test or not node.args:
            return
        if any(w in last.lower() for w in HTTP_WORDS):
            arg = static_string(node.args[0], strip_base=True)
            if arg is None:
                return
            if PATH_LITERAL.match(arg) or re.match(r"^https?://", arg):
                self.add("http", arg, rel, node.lineno, lines, direction="calls",
                         normalised=normalise_api_path(re.sub(r"^https?://[^/]+", "", arg) or "/"), method=last)


def collect(target: TargetInfo, snapshot_root: Path) -> tuple[list[SymbolEntry], list[str]]:
    """(entries, warnings) for the target's files in *snapshot_root* (the target repo's snapshot)."""
    col = _Collector(target, snapshot_root)
    warnings: list[str] = []
    langs: dict[str, int] = {}
    for rel in target.files:
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix == ".py":
            col.file(rel)
        elif suffix in CODE_LANGS:
            langs[CODE_LANGS[suffix]] = langs.get(CODE_LANGS[suffix], 0) + 1
    for lang, n in sorted(langs.items()):
        warnings.append(f"no symbol pack for {lang}: {n} file(s) in the target were not parsed for symbols")
    return col.entries, warnings


def outbound_api_paths(entries: list[SymbolEntry]) -> list[str]:
    """Distinct normalised relative API paths the target calls, in first-seen order."""
    seen: dict[str, None] = {}
    for e in entries:
        if e.kind == "http" and e.name.startswith("/"):
            seen.setdefault(e.detail.get("normalised", normalise_api_path(e.name)), None)
    return list(seen)


def setting_definitions(target: TargetInfo, snapshot_root: Path, names: set[str]) -> list[SymbolEntry]:
    """Where each setting the target reads is assigned, in the target repo's settings modules.

    Gives the config group its anchor (`DolceEngine/settings.py: CHANNEX_ENV = config(...)`),
    plus every env var those assignments read.
    """
    out: list[SymbolEntry] = []
    candidates = [f for f in snapshot_root.rglob("*.py")
                  if (f.name.startswith("settings") or "settings" in f.relative_to(snapshot_root).parts[:-1])
                  and not any(part in ("node_modules", "tests") for part in f.parts)]
    for f in sorted(candidates):
        rel = f.relative_to(snapshot_root).as_posix()
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
                if isinstance(node, ast.AnnAssign) else []
            for t in targets:
                if isinstance(t, ast.Name) and t.id in names:
                    envs = []
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and sub.args and _dotted(sub.func).split(".")[-1] in (
                                *ENV_FUNCS, "get"):
                            key = static_string(sub.args[0])
                            if key and re.fullmatch(r"[A-Z][A-Z0-9_]+", key):
                                envs.append(key)
                    out.append(SymbolEntry(kind="setting-definition", name=t.id, path=target.ws(rel),
                                           line=node.lineno, quote=_line(lines, node.lineno), direction="calls",
                                           detail={"env": ",".join(envs)} if envs else {}))
    return out
