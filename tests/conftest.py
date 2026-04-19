"""Shared fixtures for multiplai plugin unit tests.

Tests live alongside the plugin in this repo (dev-only — never loaded
by the plugin runtime). PLUGIN_ROOT is the repo root.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def import_script(module_name: str, filename: str):
    """Import a plugin script by filename (handles hyphens or unusual names).

    Clears hook guard env vars (_HOOK_CHILD_SESSION, _MEMORY_HOOK_ACTIVE)
    so module-level guards don't sys.exit() during import.
    """
    _guard_vars = ("_HOOK_CHILD_SESSION", "_MEMORY_HOOK_ACTIVE")
    _saved = {k: os.environ.pop(k) for k in _guard_vars if k in os.environ}

    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    if spec is None or spec.loader is None:
        os.environ.update(_saved)
        raise ImportError(f"Could not find script: {SCRIPTS_DIR / filename}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        for k in _guard_vars:
            os.environ.pop(k, None)
        os.environ.update(_saved)
    return mod


@pytest.fixture
def plugin_root():
    return PLUGIN_ROOT


@pytest.fixture
def template_files(plugin_root):
    templates_dir = plugin_root / "templates"
    return list(templates_dir.glob("*.md")) if templates_dir.exists() else []


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def plugin_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(PLUGIN_ROOT / "data"))


@pytest.fixture
def reset_paths_cache():
    from lib.paths import _reset_cache
    _reset_cache()
    yield
    _reset_cache()
