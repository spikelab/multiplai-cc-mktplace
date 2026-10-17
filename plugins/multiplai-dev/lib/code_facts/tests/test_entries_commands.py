"""Block 6: `SKILL.md` and hook commands, resolved through shell launchers.

Every `SKILL.md`, `hooks.json`, `.claude/settings*.json` and `pyproject.toml` is
generated into `tmp_path`; none is committed.
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
from pathlib import Path

import pytest

from code_facts import Seed
from conftest import line_of, make_tree
from trace_fixtures import lib


def discover(root, seeds=()):
    return lib("discover_entries")(root, seeds)


def of_kind(disc, kind):
    return [e for e in disc.entries if e.kind == kind]


def edges(entry):
    return {(k.from_.path, k.from_.line, k.to.path, k.to.line) for k in entry.known_edges}


def edges_from(entry, path):
    """`(to.path, to.line)` of the entry's known edges that start in `path`."""
    return {(t_path, t_line) for f_path, _, t_path, t_line in edges(entry) if f_path == path}


def handler(entry):
    return None if entry.handler is None else (entry.handler.path, entry.handler.line, entry.handler.symbol)


def holder(disc, kind, name):
    """The entry that is the `kind`/`name` invoker, or that lists it in `also_invoked_by` after a merge."""
    hits = [e for e in disc.entries if (e.kind, e.name) == (kind, name)
            or any((i.kind, i.name) == (kind, name) for i in e.also_invoked_by)]
    assert len(hits) == 1, f"{kind} {name!r} held by {len(hits)} entries: {[(e.kind, e.name) for e in disc.entries]}"
    return hits[0]


def invoker_location(entry, kind, name):
    if (entry.kind, entry.name) == (kind, name):
        return entry.location.path, entry.location.line
    (inv,) = [i for i in entry.also_invoked_by if (i.kind, i.name) == (kind, name)]
    return inv.location.path, inv.location.line


def diags(disc, reason):
    return [(d.path, d.line) for d in disc.diagnostics if d.reason == reason]


def fenced(info: str, body: str) -> str:
    return f"```{info}\n{body}\n```\n"


def skill_md(*blocks: str) -> str:
    return "# A skill\n\nSome prose.\n\n" + "\n".join(blocks)


# --- python -m targets -----------------------------------------------------------------------

# pr_pipeline dispatching on `args.command`: its subcommands keep a null handler at their add_parser lines.
PR_MAIN = """\
import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pr")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("open")
    sub.add_parser("render")
    args = parser.parse_args(argv)
    if args.command == "open":
        return 0
    return 1


if __name__ == "__main__":
    main()
"""

# The same CLI resolving `open` through set_defaults.
PR_MAIN_RESOLVED = """\
import argparse


def cmd_open(args):
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pr")
    sub = parser.add_subparsers(dest="command", required=True)
    p_open = sub.add_parser("open")
    p_open.set_defaults(func=cmd_open)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
"""

GUARDED_MAIN = 'def main():\n    return 0\n\n\nif __name__ == "__main__":\n    main()\n'
PR = "skills/pr/scripts/pr_pipeline/__main__.py"
PR_SKILL = "skills/pr/SKILL.md"


def pr_tree(tmp_path, skill_text, main_src=PR_MAIN, extra=None):
    return make_tree(tmp_path / "snap", {
        PR_SKILL: skill_text,
        "skills/pr/scripts/pr_pipeline/__init__.py": "",
        PR: main_src,
        **(extra or {}),
    })


# --- SKILL.md --------------------------------------------------------------------------------


class TestSkillCommands:
    def test_module_in_scripts_dir_with_null_handler_subcommand_edges_to_add_parser(self, tmp_path):
        text = skill_md(fenced("bash", "python3 -m pr_pipeline open --draft"))
        disc = discover(pr_tree(tmp_path, text))
        (e,) = of_kind(disc, "skill_command")
        assert e.name == "pr: pr_pipeline open"
        assert (e.location.path, e.location.line) == (PR_SKILL, line_of(text, "python3 -m pr_pipeline"))
        assert e.invocation == "python3 -m pr_pipeline open --draft"
        assert e.handler is None
        assert edges(e) == {(PR_SKILL, e.location.line, PR, line_of(PR_MAIN, 'add_parser("open")'))}
        assert disc.diagnostics == []

    def test_subcommand_with_resolved_handler_edges_to_it_and_merges(self, tmp_path):
        text = skill_md(fenced("bash", "python3 -m pr_pipeline open"))
        disc = discover(pr_tree(tmp_path, text, PR_MAIN_RESOLVED))
        line = line_of(text, "python3 -m pr_pipeline")
        def_line = line_of(PR_MAIN_RESOLVED, "def cmd_open(")
        e = holder(disc, "skill_command", "pr: pr_pipeline open")
        # The argparse subcommand outranks the skill command on the shared `def cmd_open`.
        assert (e.kind, e.name, handler(e)) == ("argparse_subcommand", "open", (PR, def_line, "cmd_open"))
        assert invoker_location(e, "skill_command", "pr: pr_pipeline open") == (PR_SKILL, line)
        assert (PR_SKILL, line, PR, def_line) in edges(e)

    def test_command_naming_no_subcommand_edges_to_the_main_module_guard_line(self, tmp_path):
        text = skill_md(fenced("bash", "python -m pr_pipeline --help"))
        disc = discover(pr_tree(tmp_path, text))
        e = holder(disc, "skill_command", "pr: pr_pipeline")
        line = line_of(text, "python -m pr_pipeline")
        assert (PR_SKILL, line, PR, line_of(PR_MAIN, 'if __name__ == "__main__":')) in edges(e)

    def test_command_naming_no_subcommand_of_a_guardless_main_edges_to_line_1(self, tmp_path):
        text = skill_md(fenced("sh", "python -m tool"))
        root = make_tree(tmp_path / "snap", {
            "skills/t/SKILL.md": text, "skills/t/tool/__init__.py": "",
            "skills/t/tool/__main__.py": "from tool.cli import main\n\nmain()\n",
            "skills/t/tool/cli.py": "def main():\n    return 0\n"})
        e = holder(discover(root), "skill_command", "t: tool")
        assert ("skills/t/SKILL.md", line_of(text, "python -m tool"), "skills/t/tool/__main__.py", 1) in edges(e)

    def test_first_positional_that_is_not_a_subcommand_is_named_but_edges_to_main(self, tmp_path):
        text = skill_md(fenced("bash", "python3 -m pr_pipeline close"))
        e = holder(discover(pr_tree(tmp_path, text)), "skill_command", "pr: pr_pipeline close")
        assert edges_from(e, PR_SKILL) == {(PR, line_of(PR_MAIN, 'if __name__ == "__main__":'))}

    @pytest.mark.parametrize("info", ["bash", "sh", "shell", "zsh", "console"])
    def test_every_shell_info_string_is_scanned(self, tmp_path, info):
        text = skill_md(fenced(info, "python3 -m pr_pipeline open"))
        assert [e.name for e in of_kind(discover(pr_tree(tmp_path, text)), "skill_command")] == [
            "pr: pr_pipeline open"]

    def test_non_shell_blocks_unlabelled_blocks_and_prose_are_ignored(self, tmp_path):
        text = skill_md(
            "Run `python -m pr_pipeline open` to open a PR, or python -m pr_pipeline render.\n",
            fenced("python", "python -m pr_pipeline open"),
            fenced("", "python -m pr_pipeline open"),
            fenced("text", "python3 -m pr_pipeline render"))
        disc = discover(pr_tree(tmp_path, text))
        assert of_kind(disc, "skill_command") == []
        assert all(i.kind != "skill_command" for e in disc.entries for i in e.also_invoked_by)
        assert disc.diagnostics == []

    def test_unresolvable_module_is_diagnosed_at_its_line_and_not_emitted(self, tmp_path):
        text = skill_md(fenced("bash", "echo start\npython -m missing_mod run"))
        disc = discover(pr_tree(tmp_path, text))
        assert of_kind(disc, "skill_command") == []
        assert diags(disc, "unresolved_target") == [(PR_SKILL, line_of(text, "missing_mod"))]

    def test_line_continuations_are_joined_and_located_at_the_first_line(self, tmp_path):
        text = skill_md(fenced("bash", "python3 -m pr_pipeline \\\n    open \\\n    --draft"))
        (e,) = of_kind(discover(pr_tree(tmp_path, text)), "skill_command")
        assert e.location.line == line_of(text, "python3 -m pr_pipeline")
        assert "\\\n" not in e.invocation
        assert shlex.split(e.invocation) == ["python3", "-m", "pr_pipeline", "open", "--draft"]
        assert e.name == "pr: pr_pipeline open"

    def test_a_block_with_several_commands_gives_one_entry_each_on_its_own_line(self, tmp_path):
        text = skill_md(fenced("bash", "python3 -m pr_pipeline open\n\npython3 -m pr_pipeline render"))
        found = {e.name: e.location.line for e in of_kind(discover(pr_tree(tmp_path, text)), "skill_command")}
        assert found == {"pr: pr_pipeline open": line_of(text, "pr_pipeline open"),
                         "pr: pr_pipeline render": line_of(text, "pr_pipeline render")}

    @pytest.mark.parametrize("command", [
        'FOO=1 BAR="x y" python3 -m pr_pipeline open',
        'cd "${CLAUDE_SKILL_DIR}/scripts" && python3 -m pr_pipeline open',
        'uv run --directory "${CLAUDE_SKILL_DIR}/scripts" python -m pr_pipeline open',
        'uv run --project "${CLAUDE_SKILL_DIR}/scripts" --frozen python3 -m pr_pipeline open',
        'cd "${CLAUDE_SKILL_DIR}" && FOO=1 uv run --quiet python -m pr_pipeline open',
    ])
    def test_accepted_prefixes_keep_the_full_command_as_invocation(self, tmp_path, command):
        text = skill_md(fenced("bash", command))
        (e,) = of_kind(discover(pr_tree(tmp_path, text)), "skill_command")
        assert e.name == "pr: pr_pipeline open"
        assert e.invocation == command
        assert {t for *_, t, _ in edges(e)} == {PR}

    def test_uv_directory_under_plugin_root_is_a_search_root(self, tmp_path):
        command = 'uv run --directory "${CLAUDE_PLUGIN_ROOT}/tools" python -m pr_pipeline open'
        text = skill_md(fenced("bash", command))
        root = make_tree(tmp_path / "snap", {
            "plugin/.claude-plugin/plugin.json": "{}\n", "plugin/skills/pr/SKILL.md": text,
            "plugin/tools/pr_pipeline/__init__.py": "", "plugin/tools/pr_pipeline/__main__.py": PR_MAIN})
        (e,) = of_kind(discover(root), "skill_command")
        assert {t for *_, t, _ in edges(e)} == {"plugin/tools/pr_pipeline/__main__.py"}

    ORDER = ["pythonpath", "cd", "skill", "scripts", "repo", "src"]
    PLACES = {"pythonpath": "skills/x/vendor", "cd": "skills/x/work", "skill": "skills/x",
              "scripts": "skills/x/scripts", "repo": "", "src": "src"}

    @pytest.mark.parametrize("winner", ORDER)
    def test_module_search_roots_are_tried_in_order(self, tmp_path, winner):
        command = 'cd "${CLAUDE_SKILL_DIR}/work" && PYTHONPATH="${CLAUDE_SKILL_DIR}/vendor" python -m tool'
        text = skill_md(fenced("bash", command))
        files = {"skills/x/SKILL.md": text}
        # The module exists at the winning root and every later one, never at an earlier one.
        for place in self.ORDER[self.ORDER.index(winner):]:
            pkg = posixpath.join(self.PLACES[place], "tool")
            files[f"{pkg}/__init__.py"] = ""
            files[f"{pkg}/__main__.py"] = GUARDED_MAIN
        disc = discover(make_tree(tmp_path / "snap", files))
        e = holder(disc, "skill_command", "x: tool")
        assert {t for t, _ in edges_from(e, "skills/x/SKILL.md")} == {
            posixpath.join(self.PLACES[winner], "tool/__main__.py")}

    def test_single_module_file_target(self, tmp_path):
        text = skill_md(fenced("bash", "python -m report"))
        root = make_tree(tmp_path / "snap", {"skills/r/SKILL.md": text, "skills/r/scripts/report.py": GUARDED_MAIN})
        e = holder(discover(root), "skill_command", "r: report")
        assert edges_from(e, "skills/r/SKILL.md") == {
            ("skills/r/scripts/report.py", line_of(GUARDED_MAIN, 'if __name__ == "__main__":'))}


# --- hooks -----------------------------------------------------------------------------------

RUN_SH = 'cd "$(dirname "$0")/.." && exec python3 "$@"\n'
PLUGIN_JSON = '{"name": "plugin"}\n'
GUARDLESS_SCRIPT = "def main():\n    return 0\n\n\nmain()\n"
RUN = 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh"'


def hooks_doc(*groups):
    """`groups` are (event, matcher or None, [command, ...]); `prompt` hooks are given as ("prompt", text)."""
    doc: dict = {"hooks": {}}
    for event, matcher, commands in groups:
        group: dict = {} if matcher is None else {"matcher": matcher}
        group["hooks"] = [{"type": c[0], "prompt": c[1]} if isinstance(c, tuple)
                          else {"type": "command", "command": c, "timeout": 10} for c in commands]
        doc["hooks"].setdefault(event, []).append(group)
    return json.dumps(doc, indent=2) + "\n"


def command_line(text, command, nth=1):
    return line_of(text, json.dumps(command), nth)


EIGHT_EVENTS = [("SessionStart", None), ("UserPromptSubmit", None), ("PreToolUse", "Bash"),
                ("PostToolUse", "Edit|Write"), ("Notification", None), ("Stop", None),
                ("SessionEnd", None), ("PreCompact", "auto")]


def eight_hook_plugin(tmp_path, script_src):
    commands = [f'{RUN} "${{CLAUDE_PLUGIN_ROOT}}/scripts/s{i}.py"' for i in range(8)]
    text = hooks_doc(*[(ev, m, [c]) for (ev, m), c in zip(EIGHT_EVENTS, commands, strict=True)])
    files = {"plugin/.claude-plugin/plugin.json": PLUGIN_JSON, "plugin/hooks/hooks.json": text,
             "plugin/hooks/run.sh": RUN_SH}
    files.update({f"plugin/scripts/s{i}.py": script_src for i in range(8)})
    return make_tree(tmp_path / "snap", files), text, commands


def hook_name(event, matcher):
    return event if matcher is None else f"{event}:{matcher}"


class TestHookCommands:
    def test_eight_run_sh_hooks_are_eight_entries_each_on_its_own_script(self, tmp_path):
        root, text, commands = eight_hook_plugin(tmp_path, GUARDLESS_SCRIPT)
        disc = discover(root)
        hooks = of_kind(disc, "hook_command")
        assert len(hooks) == 8
        got = {e.name: e for e in hooks}
        assert set(got) == {hook_name(ev, m) for ev, m in EIGHT_EVENTS}
        for i, ((ev, m), command) in enumerate(zip(EIGHT_EVENTS, commands, strict=True)):
            e = got[hook_name(ev, m)]
            script = f"plugin/scripts/s{i}.py"
            assert (e.location.path, e.location.line) == ("plugin/hooks/hooks.json", command_line(text, command))
            assert e.invocation == command
            assert handler(e) == (script, 1, "main")
            assert {t for *_, t, _ in edges(e)} == {script}
            assert all(f == "plugin/hooks/hooks.json" for f, *_ in edges(e))
        assert disc.diagnostics == []

    def test_eight_hooks_on_guarded_scripts_stay_eight_separate_paths(self, tmp_path):
        root, _, _ = eight_hook_plugin(tmp_path, GUARDED_MAIN)
        disc = discover(root)
        # Each hook folds into its own script's main_module entry, never into a shared run.sh.
        per_script = {}
        for i, (ev, m) in enumerate(EIGHT_EVENTS):
            e = holder(disc, "hook_command", hook_name(ev, m))
            assert e.handler is not None and e.handler.path == f"plugin/scripts/s{i}.py"
            per_script[e.handler.path] = e
        assert len(per_script) == 8
        assert all(e.handler.path != "plugin/hooks/run.sh" for e in disc.entries if e.handler)

    def test_hook_with_no_python_target_targets_the_launcher_at_line_1(self, tmp_path):
        warm = f"{RUN} --warm"
        text = hooks_doc(("Setup", None, [warm]))
        root = make_tree(tmp_path / "snap", {"plugin/.claude-plugin/plugin.json": PLUGIN_JSON,
                                             "plugin/hooks/hooks.json": text, "plugin/hooks/run.sh": RUN_SH})
        (e,) = of_kind(discover(root), "hook_command")
        assert (e.name, e.location.line) == ("Setup", command_line(text, warm))
        assert handler(e) == ("plugin/hooks/run.sh", 1, None)
        assert edges(e) == {("plugin/hooks/hooks.json", e.location.line, "plugin/hooks/run.sh", 1)}

    def test_identical_command_strings_are_assigned_lines_in_document_order(self, tmp_path):
        warm = f"{RUN} --warm"
        text = hooks_doc(("Setup", None, [warm]), ("SessionStart", "startup", [warm]), ("Stop", None, [warm]))
        root = make_tree(tmp_path / "snap", {"plugin/.claude-plugin/plugin.json": PLUGIN_JSON,
                                             "plugin/hooks/hooks.json": text, "plugin/hooks/run.sh": RUN_SH})
        found = sorted((e.location.line, e.name) for e in of_kind(discover(root), "hook_command"))
        assert found == [(command_line(text, warm, 1), "Setup"), (command_line(text, warm, 2), "SessionStart:startup"),
                         (command_line(text, warm, 3), "Stop")]

    def test_plugin_root_is_the_nearest_ancestor_holding_a_plugin_manifest(self, tmp_path):
        command = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"'
        text = hooks_doc(("Stop", None, [command]))
        root = make_tree(tmp_path / "snap", {
            "plugin/.claude-plugin/plugin.json": PLUGIN_JSON, "plugin/config/deep/hooks.json": text,
            "plugin/scripts/a.py": GUARDLESS_SCRIPT,
            # Where the parent-of-the-hooks-directory fallback would wrongly look.
            "plugin/config/scripts/a.py": GUARDLESS_SCRIPT})
        (e,) = of_kind(discover(root), "hook_command")
        assert handler(e) == ("plugin/scripts/a.py", 1, "main")

    def test_without_a_manifest_plugin_root_is_the_hooks_directory_parent(self, tmp_path):
        command = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"'
        text = hooks_doc(("PreToolUse", "Bash", [command]))
        root = make_tree(tmp_path / "snap", {"tool/hooks/hooks.json": text, "tool/scripts/a.py": GUARDLESS_SCRIPT})
        (e,) = of_kind(discover(root), "hook_command")
        assert (e.name, handler(e)) == ("PreToolUse:Bash", ("tool/scripts/a.py", 1, "main"))

    @pytest.mark.parametrize("settings", [".claude/settings.json", ".claude/settings.local.json"])
    @pytest.mark.parametrize("var", ["$CLAUDE_PROJECT_DIR", "${CLAUDE_PROJECT_DIR}"])
    def test_settings_hook_expands_project_dir_to_the_repository_root(self, tmp_path, settings, var):
        command = f'python3 "{var}/scripts/stop.py"'
        text = hooks_doc(("Stop", None, [command]))
        root = make_tree(tmp_path / "snap", {"repo/.git/HEAD": "ref: refs/heads/main\n", f"repo/{settings}": text,
                                             "repo/scripts/stop.py": GUARDLESS_SCRIPT,
                                             "scripts/stop.py": GUARDLESS_SCRIPT})
        (e,) = of_kind(discover(root), "hook_command")
        assert (e.name, e.location.path) == ("Stop", f"repo/{settings}")
        assert e.location.line == command_line(text, command)
        assert handler(e) == ("repo/scripts/stop.py", 1, "main")
        assert e.invocation == command

    def test_settings_file_keys_that_are_not_hooks_are_ignored(self, tmp_path):
        doc = json.loads(hooks_doc(("Stop", None, ['python3 "$CLAUDE_PROJECT_DIR/scripts/stop.py"'])))
        doc["statusLine"] = {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/scripts/status.py"'}
        root = make_tree(tmp_path / "snap", {".claude/settings.json": json.dumps(doc, indent=2),
                                             "scripts/stop.py": GUARDLESS_SCRIPT,
                                             "scripts/status.py": GUARDLESS_SCRIPT})
        assert [e.name for e in of_kind(discover(root), "hook_command")] == ["Stop"]

    def test_python_m_hook_resolves_like_a_skill_command(self, tmp_path):
        command = "python3 -m pkg.hook"
        text = hooks_doc(("PreToolUse", "Bash", [command]))
        root = make_tree(tmp_path / "snap", {
            ".claude/settings.json": text, "pkg/__init__.py": "", "pkg/hook/__init__.py": "",
            "pkg/hook/__main__.py": GUARDED_MAIN})
        e = holder(discover(root), "hook_command", "PreToolUse:Bash")
        guard = line_of(GUARDED_MAIN, 'if __name__ == "__main__":')
        assert (".claude/settings.json", command_line(text, command), "pkg/hook/__main__.py", guard) in edges(e)

    def test_prompt_hooks_are_ignored_without_a_diagnostic(self, tmp_path):
        text = hooks_doc(("Stop", None, [("prompt", "Summarise the session")]))
        disc = discover(make_tree(tmp_path / "snap", {"tool/hooks/hooks.json": text}))
        assert of_kind(disc, "hook_command") == []
        assert disc.diagnostics == []

    def test_missing_script_target_is_diagnosed_at_the_command_line(self, tmp_path):
        gone = 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/gone.py"'
        good = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"'
        text = hooks_doc(("PreToolUse", "Bash", [gone]), ("Stop", None, [good]))
        root = make_tree(tmp_path / "snap", {"plugin/.claude-plugin/plugin.json": PLUGIN_JSON,
                                             "plugin/hooks/hooks.json": text,
                                             "plugin/scripts/a.py": GUARDLESS_SCRIPT})
        disc = discover(root)
        assert [e.name for e in of_kind(disc, "hook_command")] == ["Stop"]
        assert diags(disc, "unresolved_target") == [("plugin/hooks/hooks.json", command_line(text, gone))]

    def test_invalid_json_is_a_file_level_parse_error_and_other_files_still_count(self, tmp_path):
        good = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"'
        root = make_tree(tmp_path / "snap", {
            "broken/hooks/hooks.json": '{"hooks": {"Stop": [ {"hooks": [}\n',
            "tool/hooks/hooks.json": hooks_doc(("Stop", None, [good])), "tool/scripts/a.py": GUARDLESS_SCRIPT})
        disc = discover(root)
        assert [(d.path, d.line, d.reason) for d in disc.diagnostics] == [
            ("broken/hooks/hooks.json", None, "parse_error")]
        assert [e.location.path for e in of_kind(disc, "hook_command")] == ["tool/hooks/hooks.json"]

    def test_hook_entry_publishes_as_kind_hook_with_its_command(self, tmp_path):
        root, _, commands = eight_hook_plugin(tmp_path, GUARDLESS_SCRIPT)
        e = next(e for e in of_kind(discover(root), "hook_command") if e.name == "Stop")
        published = lib("to_path_entry")(e)
        assert (published.kind, published.invocation) == ("hook", commands[5])


# --- merging and the mixed-failure count --------------------------------------------------------


class TestCommandMerging:
    def test_hook_and_skill_running_the_same_module_merge_into_one_entry(self, tmp_path):
        skill = skill_md(fenced("bash", "python -m pkg"))
        hook_cmd = "python3 -m pkg"
        hooks = hooks_doc(("Stop", None, [hook_cmd]))
        main_src = 'from pkg.cli import main\n\nif __name__ == "__main__":\n    main()\n'
        cli = "def main():\n    return 0\n"
        root = make_tree(tmp_path / "snap", {
            "skills/s/SKILL.md": skill, ".claude/settings.json": hooks,
            "pkg/__init__.py": "", "pkg/__main__.py": main_src, "pkg/cli.py": cli})
        disc = discover(root)
        on_main = [e for e in disc.entries if handler(e) == ("pkg/cli.py", 1, "main")]
        assert len(on_main) == 1
        (e,) = on_main
        assert e.kind == "main_module"
        assert {(i.kind, i.name) for i in e.also_invoked_by} == {("skill_command", "s: pkg"), ("hook_command", "Stop")}
        guard = line_of(main_src, "if __name__")
        assert {("skills/s/SKILL.md", line_of(skill, "python -m pkg"), "pkg/__main__.py", guard),
                (".claude/settings.json", command_line(hooks, hook_cmd), "pkg/__main__.py", guard)} <= edges(e)
        assert of_kind(disc, "skill_command") == [] and of_kind(disc, "hook_command") == []


DYNAMIC = """\
import argparse

NAME = "go"


def build():
    sub = argparse.ArgumentParser().add_subparsers()
    sub.add_parser(NAME)
"""


class TestMixedFailures:
    def test_exactly_five_diagnostics_and_valid_command_entries_kept(self, tmp_path):
        skill = skill_md(fenced("bash", "python -m tool"))
        warm = f"{RUN} --warm"
        root = make_tree(tmp_path / "snap", {
            "broken.py": "def broken(:\n",
            "cli.py": DYNAMIC,
            "hooks/hooks.json": "{not json\n",
            "pyproject.toml": '[project]\nname = "x"\nversion = "0"\n\n[project.scripts]\next = "thirdparty.cli:main"\n',
            "skills/x/SKILL.md": skill, "skills/x/tool/__init__.py": "", "skills/x/tool/__main__.py": GUARDED_MAIN,
            "good/hooks/hooks.json": hooks_doc(("Setup", None, [warm])), "good/hooks/run.sh": RUN_SH,
        })
        disc = discover(root, [Seed(path="nope.py", symbol="x")])
        assert [(d.path, d.reason) for d in disc.diagnostics] == [
            ("broken.py", "parse_error"), ("cli.py", "dynamic_name"), ("hooks/hooks.json", "parse_error"),
            ("nope.py", "seed_not_found"), ("pyproject.toml", "unresolved_target")]
        assert sorted(d.reason for d in disc.diagnostics) == sorted(
            ["parse_error", "parse_error", "unresolved_target", "dynamic_name", "seed_not_found"])
        holder(disc, "skill_command", "x: tool")
        assert [e.name for e in of_kind(disc, "hook_command")] == ["Setup"]

    def test_discovery_with_commands_is_byte_identical_across_runs(self, tmp_path):
        root, _, _ = eight_hook_plugin(tmp_path, GUARDED_MAIN)
        make_tree(root, {"plugin/skills/pr/SKILL.md": skill_md(fenced("bash", "python3 -m pr_pipeline open")),
                         "plugin/skills/pr/scripts/pr_pipeline/__init__.py": "",
                         "plugin/skills/pr/scripts/pr_pipeline/__main__.py": PR_MAIN})
        disc = discover(root)
        invokers = [(i.kind, i.name) for e in disc.entries for i in e.also_invoked_by] + [
            (e.kind, e.name) for e in disc.entries]
        assert sum(kind == "hook_command" for kind, _ in invokers) == 8
        assert ("skill_command", "pr: pr_pipeline open") in invokers
        first = disc.model_dump_json(by_alias=True)
        assert discover(root).model_dump_json(by_alias=True) == first


# --- command_tokens and resolve_python_m -----------------------------------------------------------


def as_rel(token: str, root: Path) -> str:
    """A token naming a path under `root`, made root-relative; any other token unchanged.

    Expanded variables may yield absolute or root-relative paths; both are accepted.
    """
    if os.path.isabs(token):
        for base in (root.resolve(), root):
            try:
                return Path(token).relative_to(base).as_posix()
            except ValueError:
                continue
        return token
    return posixpath.normpath(token) if "/" in token else token


@pytest.fixture
def plugin(tmp_path):
    return make_tree(tmp_path / "snap", {
        "plugin/.claude-plugin/plugin.json": PLUGIN_JSON, "plugin/hooks/hooks.json": "{}\n",
        "plugin/hooks/run.sh": RUN_SH, "plugin/scripts/a.py": GUARDLESS_SCRIPT})


def tokens(command, root, source_file="plugin/hooks/hooks.json"):
    return [as_rel(t, root) for t in lib("command_tokens")(command, source_file=source_file, root=root)]


class TestCommandTokens:
    def test_run_sh_launcher_is_stripped_leaving_the_script(self, plugin):
        command = f'{RUN} "${{CLAUDE_PLUGIN_ROOT}}/scripts/a.py"'
        assert tokens(command, plugin) == ["plugin/scripts/a.py"]

    @pytest.mark.parametrize("launcher", ["sh", "bash", "/bin/sh", "/bin/bash", "exec", "env", "exec env bash"])
    def test_interpreter_and_exec_launchers_are_stripped(self, plugin, launcher):
        command = f'{launcher} python3 "${{CLAUDE_PLUGIN_ROOT}}/scripts/a.py" --flag'
        assert tokens(command, plugin) == ["python3", "plugin/scripts/a.py", "--flag"]

    def test_uv_run_and_its_flags_are_stripped(self, plugin):
        command = 'uv run --directory "${CLAUDE_PLUGIN_ROOT}" --frozen python -m pkg sub --x'
        assert tokens(command, plugin) == ["python", "-m", "pkg", "sub", "--x"]

    def test_a_sh_file_that_does_not_exist_is_kept(self, plugin):
        command = 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/nope.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"'
        assert tokens(command, plugin) == ["plugin/hooks/nope.sh", "plugin/scripts/a.py"]

    def test_non_launcher_first_token_is_left_alone(self, plugin):
        assert tokens("python3 -m pkg.hook", plugin) == ["python3", "-m", "pkg.hook"]

    @pytest.mark.parametrize("var", ["$CLAUDE_PROJECT_DIR", "${CLAUDE_PROJECT_DIR}"])
    def test_project_dir_expands_to_the_repository_root(self, tmp_path, var):
        root = make_tree(tmp_path / "snap", {"repo/.git/HEAD": "x\n", "repo/.claude/settings.json": "{}\n",
                                             "repo/scripts/stop.py": GUARDLESS_SCRIPT})
        assert tokens(f'python3 "{var}/scripts/stop.py"', root, "repo/.claude/settings.json") == [
            "python3", "repo/scripts/stop.py"]


class TestResolvePythonM:
    @pytest.fixture
    def index(self, tmp_path):
        root = make_tree(tmp_path / "snap", {
            "skills/pr/scripts/pr_pipeline/__init__.py": "", "skills/pr/scripts/pr_pipeline/__main__.py": PR_MAIN,
            "pr_pipeline/__init__.py": "", "pr_pipeline/__main__.py": PR_MAIN,
            "skills/pr/scripts/report.py": GUARDED_MAIN,
            "pkg/__init__.py": "", "pkg/hook/__init__.py": "", "pkg/hook/__main__.py": GUARDED_MAIN})
        return lib("PyIndex").build(root)

    def resolve(self, module, roots, index):
        return lib("resolve_python_m")(module, roots, index)

    def test_package_resolves_to_its_main_under_the_first_root_that_has_it(self, index):
        assert self.resolve("pr_pipeline", ["skills/pr", "skills/pr/scripts", ""], index) == PR
        assert self.resolve("pr_pipeline", ["", "skills/pr/scripts"], index) == "pr_pipeline/__main__.py"

    def test_plain_module_and_dotted_package_targets(self, index):
        assert self.resolve("report", ["skills/pr/scripts"], index) == "skills/pr/scripts/report.py"
        assert self.resolve("pkg.hook", ["skills/pr", ""], index) == "pkg/hook/__main__.py"

    def test_missing_module_is_none(self, index):
        assert self.resolve("missing_mod", ["skills/pr", "skills/pr/scripts", ""], index) is None
        assert self.resolve("report", [""], index) is None
