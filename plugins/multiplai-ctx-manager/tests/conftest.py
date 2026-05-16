"""Shared fixtures for multiplai plugin unit tests.

Tests live alongside the plugin in this repo (dev-only — never loaded
by the plugin runtime).

Layout (marketplace monorepo):
    REPO_ROOT/                         <- marketplace repo root
      .claude-plugin/marketplace.json  <- MARKETPLACE_JSON
      plugins/multiplai-ctx-manager/   <- PLUGIN_ROOT
        .claude-plugin/plugin.json     <- PLUGIN_JSON
        hooks/hooks.json               <- HOOKS_JSON (official CC schema)
        scripts/  skills/  templates/  tests/
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"
PLUGIN_JSON = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# Maps Claude Code hook event names to the plugin script each invokes.
EXPECTED_HOOK_SCRIPTS = {
    "SessionStart": ["scripts/venv_bootstrap.py", "scripts/session_start.py"],
    "UserPromptSubmit": ["scripts/context_manager.py"],
    "Stop": ["scripts/session_stop.py"],
    "SessionEnd": ["scripts/session_end.py"],
    "PreCompact": ["scripts/pre_compact.py"],
}

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def parse_hooks():
    """Normalize the official Claude Code hooks/hooks.json schema.

    The official schema is::

        {"hooks": {"<Event>": [{"hooks": [
            {"type": "command",
             "command": "python \\"${CLAUDE_PLUGIN_ROOT}/scripts/x.py\\"",
             "timeout": 10}]}]}}

    Returns a flat list of dicts, one per command, each with:
        event   - event name (e.g. "SessionStart")
        script  - script path relative to PLUGIN_ROOT (e.g. "scripts/x.py")
        command - the raw command string
        timeout - declared timeout (seconds) or None
    """
    data = json.loads(HOOKS_JSON.read_text())
    hooks_obj = data["hooks"]
    out = []
    for event, groups in hooks_obj.items():
        for group in groups:
            for entry in group.get("hooks", []):
                command = entry.get("command", "")
                m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+?\.py)", command)
                script = m.group(1) if m else ""
                out.append({
                    "event": event,
                    "script": script,
                    "command": command,
                    "timeout": entry.get("timeout"),
                })
    return out


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
        if key.startswith("CLAUDE_PLUGIN") or key == "WORKSPACE":
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
