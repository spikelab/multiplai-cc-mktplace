## ADDED Requirements

### Requirement: Discovery is deterministic and makes no model call
Entry-point discovery in `code_facts/entries.py` SHALL find entry points using only static analysis: Python `ast`, `tomllib`, `json` and plain text scanning. It SHALL NOT call the caller's `CallFn` or any other model.

#### Scenario: No model call during discovery
- **WHEN** `map_paths(...)` runs on a fixture repository with a spy `CallFn` that records every call
- **AND** the run is stopped once discovery has finished, before the model fallback and tracing start
- **THEN** the spy has recorded zero calls

#### Scenario: Discovery runs without a CallFn
- **WHEN** the discovery function is called directly on a fixture repository, with no `CallFn` available
- **THEN** it returns entries and does not raise

### Requirement: Discovery never imports or runs target code
Discovery SHALL NOT import, execute or spawn any code from the snapshot it is analysing.

#### Scenario: Module with import-time side effects
- **WHEN** discovery runs on a fixture that contains a `__main__.py` and a click module
- **AND** each module writes a sentinel file and raises an exception at import time
- **THEN** discovery returns the entries for both modules
- **AND** no sentinel file exists after the run
- **AND** no exception from the fixture code is raised

### Requirement: Entry record shape
Each discovered entry SHALL have these fields:

- `kind`: one of `argparse_subcommand`, `click_command`, `typer_command`, `project_script`, `main_module`, `skill_command`, `hook_command` or `seed`.
- `name`: a non-empty string.
- `location`: `{path, line}`, where the entry is declared.
- `handler`: `{path, line, symbol}`, or `null` if no handler resolves.
- `invocation`: the literal command text for `skill_command` and `hook_command`, otherwise `null`.
- `invoked_by`: who runs the entry, in words, derived from its kind without a model call: `an agent following <SKILL.md path>` for `skill_command`; `Claude Code, on the <event[:matcher]> hook (<hooks file>)` for `hook_command`; the seed's name, else `the caller (seed)`, for `seed`; `a person or a script running <invocation or name>` for every other kind.
- `known_edges`: a list of `{from: {path, line}, to: {path, line}}`.
- `also_invoked_by`: a list of `{kind, name, location}`.

All `path` values SHALL be POSIX-style and relative to the snapshot root. All `line` values SHALL be 1-based.

#### Scenario: Fields present and well-formed
- **WHEN** discovery runs on a fixture containing at least one entry of every discoverable kind
- **THEN** every entry has all seven fields
- **AND** every `path` is relative, uses `/` separators and does not contain `..`
- **AND** every `line` is an integer of 1 or more that does not exceed the line count of the referenced file

### Requirement: Paths are relative to the snapshot root, including multi-repository snapshots
When the snapshot root contains several repositories, discovery SHALL scan all of them. It SHALL report every path relative to the snapshot root, not relative to an individual repository.

A file's repository root is the nearest ancestor directory, at or below the snapshot root, that contains `.git` or `pyproject.toml`. If there is no such directory, the snapshot root is used.

#### Scenario: Two repositories under one snapshot
- **WHEN** the snapshot root contains `repo_a/pyproject.toml` and `repo_b/pyproject.toml`
- **AND** each declares one `[project.scripts]` entry
- **THEN** discovery returns two `project_script` entries
- **AND** their `location.path` values are `repo_a/pyproject.toml` and `repo_b/pyproject.toml`
- **AND** each handler path starts with its own repository directory

#### Scenario: Module resolution stays within the right repository
- **WHEN** `repo_a` and `repo_b` both contain a package named `tool` with a `cli.py`
- **AND** `repo_a/pyproject.toml` declares `run = "tool.cli:main"`
- **THEN** the entry's handler path is `repo_a/tool/cli.py`, or `repo_a/src/tool/cli.py` when the package lives under `src/`
- **AND** it is never a path under `repo_b`

### Requirement: Excluded and unsafe locations are not scanned
Discovery SHALL skip any directory named `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build` or `dist`. It SHALL NOT follow a symlink whose target resolves outside the snapshot root.

#### Scenario: Vendored entry points ignored
- **WHEN** a fixture contains a `__main__.py` under `.venv/lib/site-packages/somepkg/`
- **AND** it contains one under `node_modules/x/`
- **THEN** no entry has a `location.path` inside either directory

#### Scenario: Symlink escaping the snapshot
- **WHEN** a fixture contains a symlink `link/` that points to a directory outside the snapshot root
- **AND** that directory contains a `__main__.py`
- **THEN** no entry is discovered through `link/`
- **AND** discovery does not raise

### Requirement: argparse subcommands are discovered
For every `add_parser(<string literal>, ...)` call on an object returned by `add_subparsers(...)`, discovery SHALL emit one `argparse_subcommand` entry:

- `location` is the line of the `add_parser` call.
- `name` is the subcommand name. For nested subparsers, the parent subcommand names are prefixed and separated by single spaces.

#### Scenario: Flat subcommands
- **WHEN** a module calls `sub.add_parser("open")` and `sub.add_parser("close")` on the result of `parser.add_subparsers()`
- **THEN** discovery emits two `argparse_subcommand` entries named `open` and `close`
- **AND** each `location.line` is the line of its `add_parser` call

#### Scenario: Nested subcommands
- **WHEN** subcommand `db` has its own `add_subparsers()` with `add_parser("migrate")`
- **THEN** an entry named `db migrate` is emitted
- **AND** an entry named `db` is emitted at its own `add_parser` line

#### Scenario: Non-literal subcommand name
- **WHEN** a module calls `sub.add_parser(name_var)`, where `name_var` is not a string literal
- **THEN** no entry is emitted for that call
- **AND** a diagnostic with reason `dynamic_name` and the call's path and line is recorded

### Requirement: argparse handlers resolve through set_defaults and dispatch dicts only
Discovery SHALL resolve a subcommand's `handler` in only two cases:

- **`set_defaults`.** The subparser object calls `set_defaults(func=<name>)`.
- **Dispatch dict.** A module-level or function-level dict literal maps the subcommand's literal name to a function reference.

In both cases, `<name>` or the function reference SHALL resolve to a function defined in the same module, or imported into it from a module inside the same repository.

Discovery SHALL NOT resolve handlers from comparisons such as `args.command == "<name>"`. In that case, and whenever resolution fails, `handler` SHALL be `null` and the entry stays anchored at its `add_parser` line.

#### Scenario: set_defaults handler
- **WHEN** a subparser `p_open` calls `p_open.set_defaults(func=cmd_open)`
- **AND** `def cmd_open` is defined in the same module
- **THEN** the `open` entry has `handler.symbol == "cmd_open"`
- **AND** `handler.line` is the line of `def cmd_open`

#### Scenario: Dispatch dict handler imported from a sibling module
- **WHEN** a module defines `HANDLERS = {"render": render_cmd}`
- **AND** it has `from .commands import render_cmd`
- **AND** `render_cmd` is defined in `commands.py` in the same package
- **THEN** the `render` entry's `handler.path` is that `commands.py`
- **AND** `handler.line` is the line of `def render_cmd`

#### Scenario: args.command dispatch lands on add_parser lines (pr_pipeline case)
- **WHEN** discovery runs on a fixture that reproduces `skills/pr/scripts/pr_pipeline/__main__.py`
- **AND** that fixture dispatches with `if args.command == "open": ...` for `open`, `edit` and `render`
- **THEN** three `argparse_subcommand` entries named `open`, `edit` and `render` are emitted
- **AND** each has `handler == null`
- **AND** each `location.line` equals the line of its `add_parser` call

#### Scenario: Handler imported from outside the repository
- **WHEN** `set_defaults(func=x)` refers to `x`, which is imported from a third-party package not present in the snapshot
- **THEN** the entry has `handler == null`
- **AND** a diagnostic with reason `unresolved_handler` is recorded

### Requirement: argparse dispatch lines become known edges
When an argparse handler resolves, discovery SHALL add a known edge that ends at the handler's definition line. The edge starts as follows:

| Case | Edge starts at |
|---|---|
| The same module contains a call through `args.func(...)`, or a subscript of the dispatch dict (`HANDLERS[args.command](...)`) | The line of that dispatch call |
| No such dispatch call exists | The `set_defaults` line, or the dict entry line |

#### Scenario: Edge from args.func call
- **WHEN** `main()` calls `args.func(args)` on line 80
- **AND** the `open` handler `cmd_open` is defined on line 20
- **THEN** the `open` entry's `known_edges` contains `{from: {path, line: 80}, to: {path, line: 20}}`

#### Scenario: Edge without a dispatch call
- **WHEN** `set_defaults(func=cmd_open)` is on line 45
- **AND** the module contains no `args.func(...)` call
- **THEN** `known_edges` contains an edge from line 45 to the line of `def cmd_open`

### Requirement: click and typer commands are discovered
Discovery SHALL emit entries for functions decorated with click or typer command decorators:

| Kind | Decorators |
|---|---|
| `click_command` | `@click.command`, `@<group>.command`, `@click.group`, `@<group>.group` |
| `typer_command` | `@<typer_app>.command`, `@<typer_app>.callback` |

For each entry:

- `location` is the decorator line.
- `handler` is the decorated function's `def` line, with its symbol.
- `name` is the explicit name argument if one is given. Otherwise it is the function name with `_` replaced by `-`. It is prefixed by any parent group names, separated by single spaces.

#### Scenario: click command with default name
- **WHEN** `@cli.command()` decorates `def build_all()`
- **AND** `cli` is a `@click.group()` named `cli`
- **THEN** a `click_command` entry named `cli build-all` is emitted
- **AND** its handler is `def build_all`

#### Scenario: click command with explicit name
- **WHEN** `@cli.command("ship")` decorates `def do_ship()`
- **THEN** the entry name ends with `ship`
- **AND** it does not end with `do-ship`

#### Scenario: typer sub-app prefix
- **WHEN** `app.add_typer(users_app, name="users")` is called
- **AND** `@users_app.command()` decorates `def add_user()`
- **THEN** a `typer_command` entry named `users add-user` is emitted

### Requirement: [project.scripts] entries are discovered
For each key in the `[project.scripts]` table of every `pyproject.toml`, discovery SHALL emit one `project_script` entry:

- `name` is the key.
- `location` is the line of that key in the file.
- `handler` is the target function resolved from `"<module>:<function>"`. The module is searched for under the repository root, then under `<repository root>/src`.

Discovery SHALL add a known edge from the key's line to the handler's definition line.

#### Scenario: Script resolves under src layout
- **WHEN** `pyproject.toml` declares `mytool = "mytool.cli:main"`
- **AND** `src/mytool/cli.py` defines `main` on line 12
- **THEN** a `project_script` entry named `mytool` is emitted
- **AND** its handler is at `src/mytool/cli.py:12`
- **AND** `known_edges` contains an edge from the `mytool =` line to line 12

#### Scenario: Script target not in snapshot
- **WHEN** `[project.scripts]` maps `ext = "thirdparty.cli:main"`
- **AND** no `thirdparty` package exists in the repository
- **THEN** no entry is emitted for `ext`
- **AND** a diagnostic with reason `unresolved_target` is recorded at the key's line

#### Scenario: Invalid TOML
- **WHEN** a `pyproject.toml` is not valid TOML
- **THEN** discovery does not raise
- **AND** a diagnostic with reason `parse_error` is recorded for that file
- **AND** entries from other files are still returned

### Requirement: __main__ modules and packages are discovered
Discovery SHALL emit a `main_module` entry in two cases:

| Case | `location` | `name` |
|---|---|---|
| A package contains `__main__.py` | Line 1 of `__main__.py` | The dotted package name |
| A non-test module contains a top-level `if __name__ == "__main__":` block | The line of the `if` statement | The dotted module name |

In both cases, `handler` SHALL resolve to the function's definition when the block or module body calls a single function. It is `null` otherwise.

Test files SHALL be excluded. These are files named `test_*.py`, `*_test.py` or `conftest.py`, and files under a `tests/` directory.

#### Scenario: Package __main__ calling main()
- **WHEN** `pkg/__main__.py` contains `from pkg.cli import main` and a top-level call `main()`
- **THEN** a `main_module` entry named `pkg` is emitted
- **AND** its handler is `def main` in `pkg/cli.py`

#### Scenario: Script guard in a module
- **WHEN** `tools/report.py` contains `if __name__ == "__main__": run()`
- **AND** `run` is defined in the same file
- **THEN** a `main_module` entry is emitted at the `if` line
- **AND** its handler is `def run`

#### Scenario: Guard in a test file ignored
- **WHEN** `tests/test_report.py` contains an `if __name__ == "__main__":` block
- **THEN** no entry is emitted for that file

#### Scenario: Unparseable Python file
- **WHEN** a `.py` file has a syntax error
- **THEN** discovery does not raise
- **AND** a diagnostic with reason `parse_error` is recorded for that file
- **AND** entries from other files are still returned

### Requirement: python -m commands in SKILL.md fenced shell blocks are discovered
In every `SKILL.md`, discovery SHALL scan fenced code blocks whose info string is `bash`, `sh`, `shell`, `zsh` or `console`. Commands are tokenised with `shlex` after joining `\` line continuations. For each command that runs `python -m <module>` or `python3 -m <module>`, it SHALL emit one `skill_command` entry. A command optionally prefixed by environment assignments, by `cd <dir> &&`, or by `uv run [--directory D | --project D | other flags]` also counts.

For each entry:

- `location` is the line in `SKILL.md` where the command appears.
- `invocation` is the full command text with line continuations joined.
- `name` is `<skill directory name>: <module> [<first positional argument>]`.

The module SHALL be resolved by searching these roots in order. The first match wins:

1. Any directory named in a `PYTHONPATH=` assignment in the command.
2. Any directory named in a `cd <dir> &&` prefix, or by `uv run --directory` or `--project`, in the command.
3. The `SKILL.md` directory.
4. Its `scripts/` subdirectory.
5. The repository root.
6. `<repository root>/src`.

`${CLAUDE_SKILL_DIR}` (the `SKILL.md` directory) and `${CLAUDE_PLUGIN_ROOT}` (as for hooks) are expanded where they appear.

The known edge goes from the command's line to one of two targets:

- If the first positional argument after the module names a discovered argparse subcommand of that module, the edge goes to that subcommand's handler, or to its `add_parser` line when the handler is `null`.
- Otherwise the edge goes to the module's `main_module` location: its `if __name__ == "__main__":` line, or line 1 without one.

The entry's `handler` is the subcommand's handler in the first case, and the `main_module` handler of the target file in the second, so a command merges with the entry it runs.

A `python -m` command that cannot be tokenised (an unclosed quote) SHALL NOT be emitted, and a diagnostic with reason `unresolved_target` SHALL be recorded at its line.

#### Scenario: Module in scripts directory
- **WHEN** `skills/pr/SKILL.md` contains a `bash` block with `python3 -m pr_pipeline open --draft`
- **AND** `skills/pr/scripts/pr_pipeline/__main__.py` exists
- **THEN** a `skill_command` entry is emitted at that `SKILL.md` line
- **AND** its `invocation` is the full command
- **AND** `known_edges` contains an edge from that line to the resolved target

#### Scenario: Command naming a subcommand resolves to the subcommand handler
- **WHEN** the command is `python -m pr_pipeline open`
- **AND** discovery has resolved the argparse `open` subcommand of `pr_pipeline` to a handler
- **THEN** the `skill_command` known edge points to that handler's definition line

#### Scenario: Command naming a subcommand with no resolved handler
- **WHEN** the command is `python -m pr_pipeline open`
- **AND** discovery has found the argparse `open` subcommand but could not resolve its handler
- **THEN** the edge points to that subcommand's `add_parser` line

#### Scenario: Command naming no subcommand
- **WHEN** the command is `python -m pr_pipeline --help`
- **THEN** the known edge points to the `main_module` location of `pr_pipeline/__main__.py`: its `if __name__ == "__main__":` line, or line 1 when it has no such guard

#### Scenario: Non-shell fenced blocks and prose ignored
- **WHEN** `SKILL.md` contains `python -m foo` inside a `python` fenced block, inside an unlabelled fenced block, and in inline prose
- **THEN** no `skill_command` entry is emitted for any of them

#### Scenario: Unresolvable module
- **WHEN** a `bash` block runs `python -m missing_mod`
- **AND** no `missing_mod` exists under any search root
- **THEN** no entry is emitted
- **AND** a diagnostic with reason `unresolved_target` is recorded at that `SKILL.md` line

### Requirement: Hook commands are discovered
Discovery SHALL read every `hooks.json` file and every `.claude/settings*.json` file. For every hook object with `"type": "command"`, it SHALL emit one `hook_command` entry:

- `invocation` is the command string.
- `name` is `<event>:<matcher>`, or `<event>` when there is no matcher.
- `location` is the line of the file on which that command string appears. When identical command strings appear more than once, occurrences are assigned in document order.

The command is tokenised after expanding variables:

- `${CLAUDE_PLUGIN_ROOT}` becomes the nearest ancestor of the hooks file that contains `.claude-plugin/plugin.json`. If there is none, it becomes the parent of the hooks file's directory.
- `$CLAUDE_PROJECT_DIR` and `${CLAUDE_PROJECT_DIR}` become the repository root.

The target SHALL then be resolved through shell launchers:

1. Leading launcher tokens are stripped while they are `sh`, `bash`, `/bin/sh`, `/bin/bash`, `exec`, `env`, an environment assignment, a `.sh` file that exists in the snapshot, or `uv run` with its flags.
2. `python[3] -m X`, or `-m X` left after a launcher, uses the same rules as `SKILL.md` commands.
3. Otherwise the first `.py` token that exists resolves as a `main_module` script: its location is the `if __name__ == "__main__":` line or line 1, and its handler is the `main_module` handler.
4. Only when no module or `.py` target exists (the command names none, or the one it names is missing) does the hook target the stripped launcher, or else the first existing file it names, at line 1 with `symbol: null`.

When none of these exists, or the command cannot be tokenised, the target is missing. Discovery SHALL add a known edge from the command's line to the resolved target.

#### Scenario: Plugin hook running a Python script
- **WHEN** `plugin/hooks/hooks.json` has a `PreToolUse` hook with matcher `Bash` and command `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/guard.py`
- **AND** `plugin/.claude-plugin/plugin.json` exists
- **AND** `plugin/hooks/guard.py` exists
- **THEN** a `hook_command` entry named `PreToolUse:Bash` is emitted
- **AND** its known edge targets `plugin/hooks/guard.py`

#### Scenario: Hooks run through a shared launcher stay separate
- **WHEN** `plugin/hooks/hooks.json` has 8 hooks, each running `sh ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh ${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py` for a different script
- **AND** `plugin/hooks/run.sh` and every script exist
- **THEN** 8 entries are emitted, one per hook
- **AND** each one's known edge and handler target its own script, never `run.sh`

#### Scenario: Hook with no Python target
- **WHEN** a hook runs `sh ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh --warm`
- **THEN** its known edge targets line 1 of `plugin/hooks/run.sh`
- **AND** its handler is `{path: "plugin/hooks/run.sh", line: 1, symbol: null}`

#### Scenario: Missing script behind a launcher
- **WHEN** a hook runs `sh ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh ${CLAUDE_PLUGIN_ROOT}/scripts/gone.py`
- **AND** `plugin/hooks/run.sh` exists but `gone.py` does not
- **THEN** its known edge targets line 1 of `plugin/hooks/run.sh`
- **AND** no diagnostic is recorded

#### Scenario: Settings file hook
- **WHEN** `.claude/settings.local.json` defines a `Stop` hook running `python3 "$CLAUDE_PROJECT_DIR/scripts/stop.py"`
- **THEN** a `hook_command` entry named `Stop` is emitted
- **AND** it resolves to `scripts/stop.py`

#### Scenario: Non-command hook types ignored
- **WHEN** a hook object has `"type": "prompt"`
- **THEN** no entry is emitted for it

#### Scenario: Missing script target
- **WHEN** a hook command references `${CLAUDE_PLUGIN_ROOT}/hooks/gone.py`
- **AND** that file does not exist
- **THEN** no entry is emitted
- **AND** a diagnostic with reason `unresolved_target` is recorded at the command's line

#### Scenario: Invalid JSON
- **WHEN** a `hooks.json` file is not valid JSON
- **THEN** discovery does not raise
- **AND** a diagnostic with reason `parse_error` is recorded for that file

### Requirement: Caller-supplied seeds become entries
Discovery SHALL accept seeds from the caller. Each seed has a `path` relative to the snapshot root, plus either a `line` or a `symbol`, and an optional `name`.

For each valid seed, discovery SHALL emit a `seed` entry:

- `handler` is the function definition at or enclosing that line, or the definition of the named symbol.
- `name` is the given name if there is one. Otherwise it is `<path>:<symbol>`.

An invalid seed SHALL produce a diagnostic and no entry. Invalid means any of these:

- The file is missing.
- The line is out of range.
- The symbol is not found.

#### Scenario: Seed by symbol
- **WHEN** the caller passes the seed `{path: "lib/api.py", symbol: "handle_request"}`
- **AND** that function is defined on line 30
- **THEN** a `seed` entry is emitted with `handler.line == 30`

#### Scenario: Seed with missing file
- **WHEN** the caller passes a seed whose path does not exist in the snapshot
- **THEN** no entry is emitted for it
- **AND** a diagnostic with reason `seed_not_found` is recorded
- **AND** discovery does not raise

#### Scenario: Seed line out of range
- **WHEN** a seed names line 9999 of a 50-line file
- **THEN** a diagnostic with reason `seed_not_found` is recorded
- **AND** no entry is emitted for it

### Requirement: Entries resolving to the same handler are merged
When two or more entries resolve to the same handler `(path, line)`, discovery SHALL emit exactly one entry for that handler.

The kept entry is the one whose `kind` comes first in this precedence list. *(Assumption to confirm, as stated in the proposal.)*

1. `argparse_subcommand`
2. `click_command`
3. `typer_command`
4. `main_module`
5. `project_script`
6. `skill_command`
7. `hook_command`
8. `seed`

Ties within one kind SHALL be broken by the smallest `(location.path, location.line)`.

Every other merged entry SHALL be listed in the kept entry's `also_invoked_by` as `{kind, name, location}`. The list is sorted by `(location.path, location.line, kind)`. Their `known_edges` SHALL be added to the kept entry's `known_edges`, with duplicates removed.

Entries with `handler == null` are merged only with entries that have an identical `location`.

#### Scenario: Script and __main__ resolve to the same main()
- **WHEN** `[project.scripts] tool = "pkg.cli:main"` resolves to `def main`
- **AND** `pkg/__main__.py` also resolves to the same `def main`
- **THEN** one entry with `kind == "main_module"` is emitted
- **AND** its `also_invoked_by` contains the `project_script` entry `tool`
- **AND** its `known_edges` includes the edge from the `pyproject.toml` key line

#### Scenario: Hook and SKILL.md invoke the same module
- **WHEN** a hook command and a `SKILL.md` command both run `python -m pkg`
- **THEN** one entry is emitted for that handler
- **AND** `also_invoked_by` lists the other invoker

#### Scenario: Seed duplicating a discovered entry
- **WHEN** a caller seed resolves to the handler of a discovered `click_command`
- **THEN** only the `click_command` entry is emitted
- **AND** its `also_invoked_by` contains the seed

#### Scenario: Unresolved handlers not merged
- **WHEN** two argparse subcommands both have `handler == null` at different `add_parser` lines
- **THEN** both are emitted as separate entries

### Requirement: Discovery output is stably ordered
Discovery SHALL return entries sorted by `(location.path, location.line, kind, name)`. It SHALL return diagnostics sorted by `(path, line, reason)`. Given the same snapshot and seeds, two runs SHALL produce identical output, independent of filesystem iteration order.

#### Scenario: Repeat runs identical
- **WHEN** discovery runs twice on the same fixture
- **THEN** the serialised entries and diagnostics are byte-identical

#### Scenario: Seed order does not matter
- **WHEN** discovery runs with seeds `[A, B]` and then with seeds `[B, A]`
- **THEN** the serialised outputs are byte-identical

### Requirement: Every skipped candidate is explained
Every candidate that discovery examines but does not emit as an entry SHALL produce exactly one diagnostic `{path, line, reason, detail}`. This includes candidates skipped for an unparseable file, an unresolved target, a dynamic name or an invalid seed.

`reason` SHALL be one of `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name` or `seed_not_found`. `line` SHALL be `null` only for file-level parse errors.

Entries merged into another entry are recorded in `also_invoked_by`, not as diagnostics. An `unresolved_handler` diagnostic accompanies an entry that is still emitted, with `handler == null`.

#### Scenario: Mixed failures in one fixture
- **WHEN** a fixture contains one broken `.py` file, one invalid `hooks.json`, one `[project.scripts]` key with an external target, and one `add_parser(name_var)` call
- **AND** the caller passes one missing seed
- **THEN** discovery returns exactly five diagnostics
- **AND** their reasons are `parse_error`, `parse_error`, `unresolved_target`, `dynamic_name` and `seed_not_found`
- **AND** every valid entry in the fixture is still returned