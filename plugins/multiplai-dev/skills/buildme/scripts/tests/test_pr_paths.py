"""T12 of plan-spec-feature-branch-2026-10-22.md (Decision 11): buildme opens
every PR through the pr skill (`git_ops._run_pr_skill`), so its body follows
the repo's PR template. No code under `build_pipeline/` runs `gh pr create`,
or creates a pull request through `gh api`; the one `gh pr create` allowed
is the manual fallback `git_ops.manual_finish_commands` prints, never runs."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "build_pipeline"
# The printed manual fallback.
ALLOWED = {("git_ops.py", "manual_finish_commands")}
_PULLS = re.compile(r"(^|/)pulls/?$")


def _strings(nodes) -> list[str | None]:
    return [
        n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None
        for n in nodes
    ]


def _has_run(words: list[str | None], *run: str) -> bool:
    return any(tuple(words[i : i + len(run)]) == run for i in range(len(words)))


def _creates_a_pull(node: ast.AST) -> bool:
    """A `gh api` path ending in `/pulls`: a POST there opens a PR."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_PULLS.search(node.value))
    if isinstance(node, ast.JoinedStr) and node.values:
        last = node.values[-1]
        return isinstance(last, ast.Constant) and bool(_PULLS.search(str(last.value)))
    return False


def violations(source: str, filename: str) -> list[str]:
    """Each place in *source* that opens a PR around the pr skill."""
    tree = ast.parse(source)
    found: list[str] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        allowed = (filename, func) in ALLOWED
        where = f"{filename}:{getattr(node, 'lineno', '?')} ({func or 'module'})"
        if isinstance(node, (ast.List, ast.Tuple)):
            words = _strings(node.elts)
            if _has_run(words, "gh", "pr", "create"):
                found.append(f"{where}: argv gh pr create")
            if _has_run(words, "gh", "api") and any(
                _creates_a_pull(e) for e in node.elts
            ):
                found.append(f"{where}: gh api on a pulls endpoint")
        if isinstance(node, ast.Call) and _has_run(_strings(node.args), "pr", "create"):
            found.append(f"{where}: a call with 'pr', 'create'")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "gh pr create" in node.value
            and not allowed
            and not _is_docstring(node)
        ):
            found.append(f"{where}: a string running gh pr create")
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    docstrings = set()
    for n in ast.walk(tree):
        if isinstance(
            n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = n.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))

    def _is_docstring(node: ast.AST) -> bool:
        return id(node) in docstrings

    visit(tree, "")
    return found


def test_no_code_path_opens_a_pr_around_the_pr_skill():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        found += violations(path.read_text(), path.name)
    assert found == []


def test_the_pr_skill_is_what_publish_runs():
    """`_run_publish` and the findings refresh reach PRs only through
    `_run_pr_skill`; the epic coordinator opens none (its children do, each
    in its own build)."""
    text = {p.name: p.read_text() for p in PACKAGE.rglob("*.py")}
    callers = sorted(n for n, t in text.items() if "_run_pr_skill(" in t)
    assert callers == ["git_ops.py", "publish.py"]
    assert "_run_pr_skill" not in text["coordinator.py"]


def test_the_check_finds_each_way_round_the_pr_skill():
    """The scan is not vacuous: each form it looks for is found."""
    snippets = {
        "argv": 'def f():\n    run(["gh", "pr", "create", "--fill"])\n',
        "call": 'def f(repo):\n    _gh(repo, "pr", "create", "--fill")\n',
        "string": 'def f():\n    os.system("gh pr create --fill")\n',
        "api": 'def f(r):\n    run(["gh", "api", "-X", "POST", f"repos/{r}/pulls"])\n',
    }
    for kind, source in snippets.items():
        assert violations(source, "x.py"), kind
    printed = 'def manual_finish_commands():\n    return "gh pr create --fill"\n'
    assert violations(printed, "git_ops.py") == []
