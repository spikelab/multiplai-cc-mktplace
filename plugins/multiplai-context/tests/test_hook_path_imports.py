"""What the session hooks are allowed to drag in when they import.

Modules the session hooks import must stay pure file reads: nothing on the hook
path should be able to start a process.

These tests are a subprocess check because import side effects are global: once
pytest has imported anything, `sys.modules` says nothing about what a fresh
interpreter would load.
"""

import subprocess
import sys

import pytest
from conftest import SCRIPTS_DIR

# Modules the session hooks import directly, which must stay pure file reads.
HOOK_PATH_MODULES = ["lib.fsio", "lib.session_registry"]

# `subprocess` is the tell — nothing on the hook path should be able to start a
# process.
FORBIDDEN_MODULES = ["subprocess", "concurrent.futures"]


def _imports(module: str) -> set[str]:
    """`sys.modules` after importing *module* in a fresh interpreter."""
    code = (
        "import sys, json;"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r});"
        f"__import__({module!r});"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    import json

    return set(json.loads(result.stdout))


@pytest.mark.parametrize("module", HOOK_PATH_MODULES)
def test_hook_path_module_cannot_start_a_process(module):
    loaded = _imports(module)
    leaked = sorted(set(FORBIDDEN_MODULES) & loaded)
    assert not leaked, (
        f"{module} imported {leaked}. Nothing a session hook imports should be "
        f"able to shell out."
    )
