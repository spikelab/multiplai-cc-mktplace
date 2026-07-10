"""Shared fixtures for multiplai plugin unit tests.

Tests live alongside the plugin in this repo (dev-only — never loaded
by the plugin runtime).

Layout (marketplace monorepo):
    REPO_ROOT/                         <- marketplace repo root
      .claude-plugin/marketplace.json  <- MARKETPLACE_JSON
      plugins/multiplai-context/   <- PLUGIN_ROOT
        .claude-plugin/plugin.json     <- PLUGIN_JSON
        hooks/hooks.json               <- HOOKS_JSON (official CC schema)
        scripts/  skills/  templates/  tests/
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# --- Import-time workspace isolation ---------------------------------------
# Plugin scripts configure logging at MODULE IMPORT time (e.g.
# ``logger = setup_logging("backfill")`` at top level), and pytest imports
# test modules during collection — BEFORE any fixture (including the autouse
# ``_isolate_env``) runs. Scrubbing env vars in fixtures is therefore too
# late: with a leaked WORKSPACE the import-time FileHandlers bind to the real
# workspace logs and tests write into production. Pin the workspace to a
# throwaway temp dir NOW, at conftest import, which precedes every
# test-module import. (multiplai_core.log_utils additionally refuses non-tmp
# log dirs under pytest as defense in depth.)
def _is_ambient_key(key: str) -> bool:
    """Env vars that leak host/session state into tests.

    CLAUDE_PLUGIN_* / WORKSPACE anchor runtime paths; the autocompact vars
    flip the checkpoint hooks into silent "auto mode" (nudge tests then fail
    on any machine where the launcher steers native auto-compaction).
    """
    return (
        key.startswith("CLAUDE_PLUGIN")
        or key == "WORKSPACE"
        or key.startswith("CLAUDE_CODE_AUTO_COMPACT")
        or key.startswith("CLAUDE_AUTOCOMPACT")
    )


_ISOLATED_WORKSPACE = tempfile.mkdtemp(prefix="multiplai-test-ws-")
for _key in list(os.environ):
    if _is_ambient_key(_key):
        del os.environ[_key]
os.environ["WORKSPACE"] = _ISOLATED_WORKSPACE

PLUGIN_ROOT = Path(__file__).parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"
PLUGIN_JSON = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# Maps Claude Code hook event names to the plugin script each invokes.
EXPECTED_HOOK_SCRIPTS = {
    "SessionStart": ["scripts/session_start.py"],
    "UserPromptSubmit": ["scripts/context_manager.py", "scripts/checkpoint_nudge.py"],
    "Stop": ["scripts/session_stop.py"],
    "SessionEnd": ["scripts/session_end.py"],
    "PreCompact": ["scripts/pre_compact.py"],
}

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CONTEXT_MANAGER = SCRIPTS_DIR / "context_manager.py"


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


# --- Shared context_manager E2E harness -------------------------------------
# One canonical copy of the sandbox layout + subprocess runner. Before
# this lived here, three test files carried drifting near-copies (one
# had already dropped an env var, another changed the timeout).


@pytest.fixture
def env_setup(tmp_path):
    """Sandboxed plugin layout: data, memory, skills, resources, workspace.

    A superset of what any one test file needs — unused dirs are inert
    (e.g. resources injection stays off unless enable_resources is set).
    """
    data_dir = tmp_path / "plugin_data"
    catalogs_dir = data_dir / "catalogs"
    memory_dir = tmp_path / "memory"
    skills_dir = tmp_path / "skills"
    resources_dir = tmp_path / "resources"
    workspace = tmp_path / "ws"

    for d in (catalogs_dir, memory_dir, skills_dir, resources_dir, workspace):
        d.mkdir(parents=True)

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "catalogs_dir": catalogs_dir,
        "memory_dir": memory_dir,
        "skills_dir": skills_dir,
        "resources_dir": resources_dir,
        "workspace": workspace,
    }


def write_catalog(catalogs_dir: Path, filename: str, entries: list[dict]) -> None:
    """Write a schema-current catalog file the way the generators would."""
    from generators.base import CATALOG_SCHEMA_VERSION

    payload = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-05-01T00:00:00Z",
        "entries": entries,
    }
    (catalogs_dir / filename).write_text(json.dumps(payload, indent=2))


def run_context_hook(
    env_setup,
    *,
    prompt: str,
    extra_env: dict | None = None,
    cwd: str = "/tmp",
    timeout: int = 15,
) -> dict:
    """Invoke context_manager.py as a subprocess and return parsed stdout JSON.

    ``extra_env`` overrides/extends the base env (option flags, HOME,
    PATH, …); ``cwd`` is the hook-payload cwd field, not the process cwd.
    """
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("CLAUDE_PLUGIN"):
            del env[k]
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    env["CLAUDE_PLUGIN_DATA"] = str(env_setup["data_dir"])
    env["CLAUDE_PLUGIN_OPTION_memory_dir"] = str(env_setup["memory_dir"])
    env["CLAUDE_PLUGIN_OPTION_skills_dir"] = str(env_setup["skills_dir"])
    env["CLAUDE_PLUGIN_OPTION_resources_dir"] = str(env_setup["resources_dir"])
    if extra_env:
        env.update(extra_env)

    stdin = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": cwd,
    })
    result = subprocess.run(
        [sys.executable, str(CONTEXT_MANAGER)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"context_manager exited {result.returncode}\nstderr: {result.stderr[:500]}"
        )
    # Stdout may have warning lines from logging; the LAST line is the JSON.
    out = result.stdout.strip().splitlines()
    if not out:
        raise AssertionError(f"No stdout from context_manager. stderr: {result.stderr[:500]}")
    return json.loads(out[-1])


@pytest.fixture
def plugin_root():
    return PLUGIN_ROOT


@pytest.fixture
def template_files(plugin_root):
    templates_dir = plugin_root / "templates"
    return list(templates_dir.glob("*.md")) if templates_dir.exists() else []


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Scrub ambient CLAUDE_PLUGIN_* / WORKSPACE before every test.

    ``data_dir`` is workspace-anchored: a leaked host ``WORKSPACE`` (or
    ``CLAUDE_PLUGIN_OPTION_workspace_dir``) would point runtime state at
    the real workspace and break isolation. Tests that need these set
    them explicitly via monkeypatch (applied after this autouse fixture).
    WORKSPACE is deliberately NOT re-pinned here: an explicit workspace
    outranks CLAUDE_PLUGIN_DATA in data-dir resolution, which would hijack
    every test that anchors via CLAUDE_PLUGIN_DATA.

    NOTE: this fixture cannot undo import-time state — loggers bound during
    collection point at the conftest-level pinned temp dir above, and
    multiplai_core.log_utils refuses non-tmp log dirs under pytest as
    defense in depth.
    """
    for key in list(os.environ):
        if _is_ambient_key(key):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if _is_ambient_key(key):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def plugin_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(PLUGIN_ROOT / "data"))


@pytest.fixture
def reset_paths_cache():
    from multiplai_core.paths import _reset_cache
    _reset_cache()
    yield
    _reset_cache()
