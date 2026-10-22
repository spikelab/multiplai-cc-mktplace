"""pm-spec shares only `multiplai_core.specs` and the change dir with buildme,
and names no ticket system."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "pm_spec_pipeline"
SKILL = PACKAGE.parents[1] / "SKILL.md"


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def test_no_module_imports_buildme_or_another_plugin():
    for path in PACKAGE.rglob("*.py"):
        for name in _imports(path):
            assert not name.startswith(("build_pipeline", "plugins", "multiplai_dev")), (path, name)


def test_no_ticket_system_is_named():
    pattern = re.compile(r"plane|github|jira|linear", re.IGNORECASE)
    for path in [*PACKAGE.rglob("*.py"), SKILL]:
        assert not pattern.search(path.read_text()), path
