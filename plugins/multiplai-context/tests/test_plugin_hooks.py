"""Tests for plugin hook declarations and hook scripts.

Validates the official Claude Code nested hooks schema at
``<plugin>/hooks/hooks.json`` (parsed via ``conftest.parse_hooks()``).
"""

import json

import pytest

from conftest import PLUGIN_ROOT, HOOKS_JSON, EXPECTED_HOOK_SCRIPTS, parse_hooks


def _python_hook_scripts():
    """Every hook that runs a plugin Python script, as ``(hook, path)`` pairs.

    The content assertions below used to open with ``if script_path.is_file():``.
    That guard was there for a real reason — ``parse_hooks()`` also yields the
    Setup hook, whose command is ``run.sh --warm`` and whose ``script`` slot is
    the literal ``--warm`` — but it bought that at the price of silence: a
    ``.py`` path mistyped in ``hooks.json`` also failed ``is_file()``, so every
    assertion in the loop was skipped and the test reported green while checking
    nothing.

    Selecting by *name* separates the two. Non-Python hooks are excluded here;
    a declared ``.py`` that does not exist is caught by
    ``TestDeclaredScriptsResolve`` and fails, loudly, once.
    """
    return [
        (hook, PLUGIN_ROOT / hook["script"])
        for hook in parse_hooks()
        if hook["script"].endswith(".py")
    ]


class TestHookRegistration:
    """Verify hooks.json declares all required event types."""

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        assert HOOKS_JSON.is_file()
        self.hooks = parse_hooks()

    def test_session_start_registered(self):
        events = {h["event"] for h in self.hooks}
        assert "SessionStart" in events

    def test_user_prompt_submit_registered(self):
        events = {h["event"] for h in self.hooks}
        assert "UserPromptSubmit" in events

    def test_stop_registered(self):
        events = {h["event"] for h in self.hooks}
        assert "Stop" in events

    def test_session_end_registered(self):
        events = {h["event"] for h in self.hooks}
        assert "SessionEnd" in events

    def test_pre_compact_registered(self):
        events = {h["event"] for h in self.hooks}
        assert "PreCompact" in events

    def test_notification_registered(self):
        events = {h["event"] for h in self.hooks}
        assert "Notification" in events

    def test_exactly_the_expected_event_types(self):
        event_types = {h["event"] for h in self.hooks}
        assert event_types == set(EXPECTED_HOOK_SCRIPTS.keys())


class TestDeclaredScriptsResolve:
    """Every path `hooks.json` declares must exist, and be the one expected.

    This is what the old `.is_file()` guards were hiding. A hook whose command
    points at a path that is not there is dead at runtime — Claude Code runs the
    command, `uv run` cannot find the file, and the event produces nothing. That
    failure is invisible in a session, so it has to be visible here.
    """

    def test_every_declared_python_script_exists(self):
        missing = [hook["script"] for hook, path in _python_hook_scripts()
                   if not path.is_file()]
        assert not missing, (
            f"hooks.json declares scripts that do not exist: {missing}. "
            "The hook would run and find nothing; nothing else in this file "
            "checks these paths."
        )

    def test_every_hook_command_names_something(self):
        """An entry whose command matches neither a `.py` nor `--warm` parses to
        an empty script slot, which every selector here would then skip."""
        empty = [h["command"] for h in parse_hooks() if not h["script"]]
        assert not empty, f"hook commands name no resolvable target: {empty}"

    def test_the_warm_hook_runs_the_shipped_runner(self):
        warm = [h for h in parse_hooks() if h["script"] == "--warm"]
        assert warm, "no --warm hook declared"
        assert (PLUGIN_ROOT / "hooks" / "run.sh").is_file()

    def test_declared_scripts_match_the_expected_map(self):
        """Existence alone would pass a hook wired to the wrong existing script."""
        declared = {}
        for hook in parse_hooks():
            declared.setdefault(hook["event"], []).append(hook["script"])
        assert {k: sorted(v) for k, v in declared.items()} == \
            {k: sorted(v) for k, v in EXPECTED_HOOK_SCRIPTS.items()}


class TestStopHookNoGit:
    """Verify Stop hook doesn't include git staging."""

    def test_no_git_stage_in_stop_script(self):
        stop_hooks = [(h, p) for h, p in _python_hook_scripts() if h["event"] == "Stop"]
        assert stop_hooks, "No Stop hook found"
        for _hook, script_path in stop_hooks:
            text = script_path.read_text()
            assert "git_stage" not in text
            assert "git add" not in text
            assert "git commit" not in text


class TestVenvPythonUsage:
    """Verify hook scripts carry PEP 723 metadata (launched via `uv run`)."""

    def test_scripts_have_no_inline_metadata(self):
        for _hook, script_path in _python_hook_scripts():
            text = script_path.read_text()
            assert "# /// script" not in text, \
                (f"{script_path.name} carries PEP 723 inline metadata again — "
                 "dependencies belong in scripts/pyproject.toml, which is a "
                 "workspace member; inline blocks make uv re-resolve per run")
            assert "venv_guard" not in text and "ensure_venv_python" not in text, \
                f"{script_path.name} still references the retired venv guard"


class TestPathResolverImports:
    """Verify hook scripts import path resolver."""

    def test_hook_scripts_import_paths(self):
        for _hook, script_path in _python_hook_scripts():
            text = script_path.read_text()
            has_paths_import = (
                "from multiplai_core.paths" in text
                or "multiplai_core.paths" in text
            )
            assert has_paths_import, f"{script_path.name} missing path resolver import"

    def test_no_hardcoded_paths_in_scripts(self):
        for _hook, script_path in _python_hook_scripts():
            text = script_path.read_text()
            assert "~/.multiplai" not in text, f"{script_path.name} has hardcoded path"
            assert "/home/" not in text, f"{script_path.name} has hardcoded home path"


class TestModelClientUsage:
    """Verify hook scripts use model client, not direct SDK imports."""

    def test_no_direct_sdk_imports(self):
        for _hook, script_path in _python_hook_scripts():
            text = script_path.read_text()
            assert "import claude_agent_sdk" not in text, \
                f"{script_path.name} has direct SDK import"
            assert "from claude_agent_sdk" not in text, \
                f"{script_path.name} has direct SDK import"
            # anthropic import allowed only in model_client.py
            if "model_client" not in script_path.name:
                assert "import anthropic" not in text, \
                    f"{script_path.name} has direct anthropic import"


class TestHooksSchemaValidity:
    """Verify hooks/hooks.json conforms to the official nested CC schema."""

    def test_valid_json(self):
        json.loads(HOOKS_JSON.read_text())  # should not raise

    def test_official_nested_schema(self):
        """Top-level "hooks" maps event -> list of groups, each group has a
        "hooks" list of command entries with type/command."""
        data = json.loads(HOOKS_JSON.read_text())
        assert "hooks" in data
        assert isinstance(data["hooks"], dict)
        for event, groups in data["hooks"].items():
            assert isinstance(groups, list), f"{event} must map to a list of groups"
            for group in groups:
                assert "hooks" in group, f"{event} group missing 'hooks' list"
                for entry in group["hooks"]:
                    assert entry.get("type") == "command", \
                        f"{event} entry must be type 'command'"
                    assert entry.get("command"), f"{event} entry missing command"

    def test_all_entries_have_event_and_script(self):
        for hook in parse_hooks():
            assert hook["event"], "Hook missing event"
            assert hook["script"], f"Hook on {hook['event']} missing script path"

    def test_no_duplicate_event_script_pairs(self):
        pairs = [(h["event"], h["script"]) for h in parse_hooks()]
        assert len(pairs) == len(set(pairs))
