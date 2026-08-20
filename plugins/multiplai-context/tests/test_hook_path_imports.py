"""What the session hooks are allowed to drag in when they import.

`lib/fleet_sources/__init__.py` states the invariant in prose — the collectors
shell out to `git` and `gh`, so they are "**never** on the hook path… impossible
to accidentally import into the fast path" — and `lib/fleet.py` repeats it in
the comment above its `TYPE_CHECKING` block. Nothing checked it.

It broke the way prose invariants do: a cleanup pass moved a six-line timestamp
parser into `lib.fleet_sources.common` and imported it from `lib.fleet`, four
lines above the comment saying that must not happen. Importing any name out of
that package runs its `__init__`, which imports all four collectors and
`subprocess` with them — on a module `session_start` imports every session.

These tests are a subprocess check because import side effects are global: once
pytest has imported anything, `sys.modules` says nothing about what a fresh
interpreter would load.
"""

import subprocess
import sys

import pytest

from conftest import SCRIPTS_DIR

# Modules the session hooks import directly, which must stay pure file reads.
HOOK_PATH_MODULES = ["lib.fleet", "lib.fsio", "lib.session_registry"]

# What "expensive" means here: the collector modules, and the stdlib machinery
# that only they need. `subprocess` is the tell — nothing on the hook path
# should be able to start a process.
FORBIDDEN_PREFIXES = ["lib.fleet_sources"]
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
def test_hook_path_module_does_not_load_the_collectors(module):
    loaded = _imports(module)
    leaked = sorted(
        name
        for name in loaded
        if any(name.startswith(p) for p in FORBIDDEN_PREFIXES)
    )
    assert not leaked, (
        f"{module} pulled the expensive fleet collectors onto the hook path: "
        f"{leaked}. Import the shared helper from a leaf module instead of "
        f"from lib.fleet_sources.*"
    )


@pytest.mark.parametrize("module", HOOK_PATH_MODULES)
def test_hook_path_module_cannot_start_a_process(module):
    loaded = _imports(module)
    leaked = sorted(set(FORBIDDEN_MODULES) & loaded)
    assert not leaked, (
        f"{module} imported {leaked}. Nothing a session hook imports should be "
        f"able to shell out; this is the signature of a collector coming along "
        f"for the ride."
    )


def test_the_shared_timestamp_parser_is_one_function():
    """The cleanup pass was right that three copies of `parse_ts` had already
    drifted. Moving it to a leaf keeps the single definition AND the invariant
    above — so assert both modules resolve to the same object rather than
    letting a future edit fix the import by copying the function back."""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r});"
        "from lib.timeparse import parse_ts as leaf;"
        "from lib.fleet_sources.common import parse_ts as collector;"
        "assert leaf is collector, (leaf, collector);"
        "print('same')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "same"
