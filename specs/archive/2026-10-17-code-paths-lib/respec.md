# Respec Proposal: code-paths-lib

Proposed spec delta derived from `implementation-notes.md`. **Nothing here has been applied** — no requirements or design file was modified by the build. Apply it deliberately (or fold it into the next change) using the ADDED/MODIFIED/REMOVED delta format.

## ADDED Requirements

### Requirement: Cap and trace limit are not validated on unscoped runs
When changed lines are `None`, `map_paths(...)` SHALL accept any integer `cap` and `trace_limit`, including values below 1, and SHALL NOT raise for them. The written document SHALL have `"cap": null`, whatever `cap` value was passed. The `ValueError` rules for `cap` and `trace_limit` below 1 apply only when changed lines are supplied.

#### Scenario: Unscoped run with a non-positive cap
- **WHEN** `map_paths` runs with changed lines `None`, `cap=0` and `trace_limit=-1`
- **THEN** it does not raise
- **AND** every discovered entry is traced
- **AND** the written document has `"scoped": false` and `"cap": null`

#### Scenario: Scoped run with the same values
- **WHEN** `map_paths` runs with changed lines `{"pkg/core.py": {10}}` and `cap=0`
- **THEN** it raises `ValueError` before any `CallFn` call

_Motivated by:_ Block 1 test_writer: "The spec doesn't say what happens with `cap`/`trace_limit` below 1 on an unscoped run; I pinned 'accepted, cap written as null'."

### Requirement: Fallback drops without an in-root citation use a placeholder location
A fallback entry SHALL be recorded in `dropped` with the location `{path: "(unplaced)", line: 1}` when it is dropped and its citation gives no location inside the snapshot root. This covers three cases:
- the citation is missing;
- the cited path is absolute;
- the cited path escapes the root, for example through `../`.

A dropped fallback entry whose citation names an in-root path SHALL keep that path and the cited line, clamped to at least 1.

#### Scenario: Missing citation
- **WHEN** the stub fallback returns an entry with no citation
- **THEN** `dropped` has a record with stage `model_fallback_citation`, reason `missing_citation`, and `entry.location` equal to `{"path": "(unplaced)", "line": 1}`
- **AND** the written file validates against the schema

#### Scenario: Escaping citation
- **WHEN** the stub fallback returns an entry citing `../outside.py:4`
- **THEN** the drop record has reason `citation_unverified` and `entry.location.path` equal to `"(unplaced)"`

_Motivated by:_ Block 7 implementer: "A dropped entry with no in-root citation needs a placeholder location (`(unplaced)`, line 1) because `PathEntry.location` is required and the spec gives none." Block 7 test_writer: "No location is defined for drops of entries with no citation, a `../` path or an absolute path." *(Reviewer: a sentinel inside a path field is a choice the build made, not one it was forced into. Making `entry.location` nullable on drops is the alternative.)*

### Requirement: Unreadable cached answers are treated as missing
When `map_paths` loads the answer cache at start-up, it SHALL treat a cache file as absent if the file is empty, is not valid JSON, or cannot be read. The answer for that key SHALL be requested again through `CallFn`. Loading such a file SHALL NOT raise.

#### Scenario: Corrupt cache file
- **WHEN** a completed run's cache directory has one answer file truncated to zero bytes, and another overwritten with `{not json`, and the run is repeated
- **THEN** `map_paths` does not raise
- **AND** exactly the two affected answers are requested again
- **AND** the final output equals that of an uninterrupted run with the same fake answers

_Motivated by:_ Block 4 test_writer: "The behaviour for an empty or corrupt cache file at start-up isn't specified; I test it as a skipped key." Block 4 implementer: "The spec doesn't cover unreadable cache files … I treat a bad file as missing."

### Requirement: Tests that patch out the entry's handler are not listed
A test SHALL NOT appear in either the `e2e_tests` or the `e2e_tests_mocking_hop_2` list for a path when it patches either of these, using any patch form from the hop-2 requirement:
- the entry's handler;
- a function that contains one of the entry's known-edge `from` lines.

In both cases the handler never runs. A patch of the handler counts when it names the handler in either of two ways:
- at its defining module;
- under the name that a known-edge `from` line looks it up by.

#### Scenario: Handler patched at its defining module
- **WHEN** the `open` entry's handler is `pkg.cmds.handle_open`, and a test that calls `main(["open"])` is decorated with `@mock.patch("pkg.cmds.handle_open")`
- **THEN** the test is not listed for the `open` path in either list

#### Scenario: Handler patched under the name the dispatch line uses
- **WHEN** `pkg/__main__.py` does `from pkg.cmds import handle_open`, and its known-edge `from` line calls `handle_open`, and a test patches `pkg.__main__.handle_open` and invokes the entry
- **THEN** the test is not listed for that path in either list

#### Scenario: Dispatching function patched
- **WHEN** the entry's known-edge `from` line is inside `pkg.cli.main`, and a test patches `pkg.cli.main` and invokes the entry through `subprocess`
- **THEN** the test is not listed for that path

_Motivated by:_ Block 10 (title: "drop tests that patch out the handler") and Block 10 implementer: "The handler exclusion also covers the handler patched under the name a known-edge `from` line looks up (e.g. `pkg.__main__.handle_open`), which the spec does not state." D15 exclusion 2 exists in the design but is not in any capability spec.

## MODIFIED Requirements

### Requirement: Top-level document shape
A `paths.json` document SHALL be an object with exactly these required fields, in this order:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | constant string `"paths.v1"` | Schema version |
| `commit` | string | The commit that all citations were re-read at |
| `scoped` | boolean | `true` if and only if changed lines were supplied |
| `cap` | integer or `null` | `null` when `scoped` is `false` |
| `fallback` | object `{status, reason}` | `status` is `ok`, `failed` or `skipped`. `reason` is a non-empty string when `status` is `failed`, and `null` otherwise |
| `paths` | array of path objects | The kept paths |
| `dropped` | array of drop records | Every dropped entry or path |
| `diagnostics` | array of `{path, line, reason, detail}` | Discovery diagnostics |
| `test_listing_skips` | array of `{file, reason}` | Test files skipped by e2e listing |

#### Scenario: Version constant enforced
- **WHEN** a document with `"schema_version": "paths.v2"` is validated
- **THEN** validation fails

#### Scenario: Missing required field
- **WHEN** a valid document has any one of the nine fields removed and is then validated
- **THEN** validation fails in each case

#### Scenario: Unscoped run
- **WHEN** `map_paths` runs without changed lines and its result is written
- **THEN** the document has `"scoped": false` and `"cap": null`

#### Scenario: Scoped run
- **WHEN** `map_paths` runs with changed lines and `cap=5` and its result is written
- **THEN** the document has `"scoped": true` and `"cap": 5`

#### Scenario: Fallback status recorded
- **WHEN** `map_paths` runs with `fallback=False` and its result is written
- **THEN** the document has `"fallback": {"status": "skipped", "reason": null}`

#### Scenario: Strict boolean
- **WHEN** a document with `"scoped": "false"` is validated
- **THEN** validation fails

_Motivated by:_ Block 1 implementer: "The spec's 'Top-level document shape' table still lists 6 of the 9 required fields; it's outside D1 rows 6/7/9/10, so I left it." Block 1 test_writer: "The schema reject list includes `scoped: "false"`, which assumes a strict boolean in the schema."

### Requirement: Schema and models agree
Every document produced by `write_paths_file` from a model instance SHALL validate against the schema. Every document that validates against the schema, and also meets the cross-field rules a JSON Schema cannot state, SHALL parse into the model. The model enforces these rules, and `write_paths_file` checks them after schema validation:
- path ids are exactly `p1`…`pN` in array order;
- `stops_at` is at most `len(hops)`;
- no `node_id` is in both `tests.e2e` and `tests.mocks_hop_2`;
- `cap` is non-null exactly when `scoped` is `true`;
- when `scoped` is `false`, there are no drops with stage `import_reachability`, `trace_limit`, `no_changed_line` or `cap`, and every path's `changed_lines_covered` is empty;
- `fallback.reason` is non-null exactly when `fallback.status` is `failed`.

#### Scenario: Full-coverage fixture
- **WHEN** a hand-built result that uses every optional and nullable field and every enum value is written and then validated
- **THEN** validation passes

#### Scenario: Schema-valid document parses
- **WHEN** each JSON fixture under `tests/` that validates against the schema is passed to `model_validate_json`
- **THEN** parsing succeeds for every fixture

#### Scenario: Cap without scope rejected by the model
- **WHEN** the result model is built with `scoped=False` and `cap=5`
- **THEN** model validation raises an error

#### Scenario: Scope-stage drop on an unscoped run rejected
- **WHEN** the result model is built with `scoped=False` and one drop with stage `cap`
- **THEN** model validation raises an error

#### Scenario: Fallback reason mismatch rejected
- **WHEN** the result model is built with `fallback.status="ok"` and a non-null `fallback.reason`, or with `fallback.status="failed"` and `fallback.reason=None`
- **THEN** model validation raises an error

_Motivated by:_ Block 1 implementer: "The models also check id contiguity, that `cap` is set exactly when scoped, no scope-stage drops or coverage when unscoped, and that a fallback reason is given exactly when the status is `failed` (all from D4/D14)."

### Requirement: Every drop is recorded with a reason
Each item of `dropped` SHALL have these fields, in this order:
- `stage`: one of `model_fallback_citation`, `model_fallback_duplicate`, `import_reachability`, `trace_limit`, `trace`, `no_changed_line`, `cap`.
- `reason`: a non-empty code from the stage's closed set:

  | Stage | Reasons |
  |---|---|
  | `model_fallback_citation` | `missing_citation`, `citation_unverified` |
  | `model_fallback_duplicate` | `duplicate` |
  | `import_reachability` | `unreachable` |
  | `trace_limit` | `over_trace_limit` |
  | `trace` | `call_failed`, or any trace-gate break code |
  | `no_changed_line` | `no_changed_line_covered` |
  | `cap` | `over_cap` |

- `detail`: free text for humans, never parsed.
- `entry`: an entry object.

Drop records SHALL be sorted by these keys, in order:
1. `stage`, in the order listed above;
2. entry `location.path`;
3. `location.line`;
4. `kind`, compared as the output-kind string. This key does not use `KIND_RANK`.

When `scoped` is `false`, records with the stages `import_reachability`, `trace_limit`, `no_changed_line` and `cap` SHALL NOT appear.

#### Scenario: Cap drop recorded
- **WHEN** a scoped fixture run has 3 qualifying paths and `cap=2`
- **THEN** `paths` has 2 items
- **AND** `dropped` has exactly one record with `"stage": "cap"` and `"reason": "over_cap"`

#### Scenario: Uncited model entry recorded
- **WHEN** a stub fallback agent returns an entry whose citation does not re-read at the commit
- **THEN** no path has that entry
- **AND** `dropped` has a record with `"stage": "model_fallback_citation"` and `"reason": "citation_unverified"` for it

#### Scenario: Empty reason rejected
- **WHEN** a drop record with `"reason": ""` is validated
- **THEN** validation fails

#### Scenario: Kind tie-break is lexical
- **WHEN** two drop records share stage, `location.path` and `location.line`, and have kinds `main` and `hook`
- **THEN** the `hook` record comes first

_Motivated by:_ Block 3 implementer: "The drop sort's last key is the kind string, per D4's wording; `KIND_RANK` would be the other reading."

### Requirement: Changed-line paths are validated and normalised
Changed-line file paths SHALL be interpreted relative to the snapshot root, and SHALL be normalised to POSIX separators with no leading `./`.

`map_paths` SHALL raise `ValueError` before any `CallFn` call in these cases:
- a path is absolute;
- a path contains a `\`;
- a path resolves outside the snapshot root;
- a line number is below 1.

`map_paths` SHALL raise `TypeError` before any `CallFn` call when a line number is not an `int`.

A path that does not exist in the snapshot SHALL NOT raise. Such a path contributes no reachability and cannot be covered.

#### Scenario: Absolute path rejected
- **WHEN** changed lines contain `{"/etc/passwd": {1}}`
- **THEN** `map_paths` raises `ValueError`
- **AND** the recording `CallFn` has received zero calls

#### Scenario: Path escaping the snapshot rejected
- **WHEN** changed lines contain `{"../other/x.py": {3}}`
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

#### Scenario: Backslash path rejected
- **WHEN** changed lines contain `{"pkg\\core.py": {10}}`
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

#### Scenario: Non-positive line rejected
- **WHEN** changed lines contain `{"pkg/core.py": {0}}`
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

#### Scenario: Non-integer line rejected
- **WHEN** changed lines contain `{"pkg/core.py": {"10"}}`
- **THEN** `map_paths` raises `TypeError` before any `CallFn` call

#### Scenario: Equivalent spellings normalise to one file
- **WHEN** changed lines contain `{"./pkg/core.py": {10}}`
- **THEN** the result behaves identically to changed lines `{"pkg/core.py": {10}}`, including path ids and coverage counts

#### Scenario: Missing file tolerated
- **WHEN** changed lines contain only `{"pkg/deleted.py": {4}}` and that file does not exist at head
- **THEN** `map_paths` returns without raising
- **AND** every entry is dropped with reason `unreachable`

#### Scenario: Multi-repository snapshot paths
- **WHEN** the snapshot root contains `repo_a/` and `repo_b/`, and changed lines are `{"repo_b/src/mod.py": {7}}`
- **THEN** reachability and coverage are evaluated against `repo_b/src/mod.py`
- **AND** every file path in the output is relative to the snapshot root, for example `repo_b/src/mod.py`, never to `repo_b/`

_Motivated by:_ Block 1 implementer: "The design doesn't cover changed-line paths with `\` or non-integer lines; I raise ValueError and TypeError." Block 1 refactorer: "`normalise_changed_lines` raises `TypeError` (not `ValueError`) for a line that isn't an int; I left that as it is."

### Requirement: Static import-graph reachability pre-filter
When changed lines are supplied, the library SHALL build a static import graph of the snapshot's Python modules using `ast`, with no code execution and no model call.

Import edges are added as follows:
- `import a.b.c` adds edges to `a/__init__.py`, `a/b/__init__.py` and `a/b/c.py`.
- `from a.b import c` adds an edge to `a/b/__init__.py`, and also to `a/b/c.py` when that is a module. It does **not** add an edge to `a/__init__.py`.

An entry SHALL be considered reachable when either of these holds:
- (a) the entry's definition file is a changed file; or
- (b) the module containing the entry's resolved handler transitively imports a changed Python file.

Each unreachable entry SHALL be dropped before tracing with reason `unreachable`, and no `CallFn` call SHALL be made for it. This applies to entries from deterministic discovery, from the model fallback, and from caller seeds.

#### Scenario: Transitively importing entry kept
- **WHEN** entry `cli.main` is in `pkg/cli.py`, `pkg/cli.py` imports `pkg/service.py`, `pkg/service.py` imports `pkg/core.py`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** `cli.main` is traced

#### Scenario: Non-importing entry dropped without a model call
- **WHEN** entry `tools.lint` is in `pkg/tools.py`, which imports nothing that reaches `pkg/core.py`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** `tools.lint` appears in the drop records with reason `unreachable`
- **AND** the recording `CallFn` received no trace prompt for `tools.lint`

#### Scenario: Entry whose definition file changed is kept
- **WHEN** a `SKILL.md` command entry is defined in `skills/x/SKILL.md`, and changed lines are `{"skills/x/SKILL.md": {12}}`
- **THEN** that entry is traced, even though `SKILL.md` is not a Python module

#### Scenario: Relative and package imports resolved
- **WHEN** `pkg/a.py` contains `from . import b`, `pkg/b.py` contains `from .sub import c`, and the changed file is `pkg/sub/c.py`
- **THEN** an entry defined in `pkg/a.py` is considered reachable

#### Scenario: from-import does not reach the grandparent package
- **WHEN** an entry's handler module contains only `from a.b import c`, and the only changed file is `a/__init__.py`
- **THEN** the entry is dropped with reason `unreachable`

#### Scenario: Import cycle terminates
- **WHEN** `pkg/x.py` imports `pkg/y.py` and `pkg/y.py` imports `pkg/x.py`, and neither reaches the changed file
- **THEN** the reachability check terminates
- **AND** entries in both files are dropped with reason `unreachable`

#### Scenario: Unparseable or unresolvable imports do not fail the run
- **WHEN** one module in the snapshot has a syntax error, and another imports a third-party module that is not in the snapshot
- **THEN** `map_paths` completes without raising
- **AND** the module with the syntax error is treated as having no outgoing imports
- **AND** the unresolved third-party import contributes no edge

#### Scenario: Empty changed-lines mapping
- **WHEN** changed lines are `{}`
- **THEN** zero paths are returned
- **AND** every entry is dropped with reason `unreachable`
- **AND** the recording `CallFn` received zero trace calls

_Motivated by:_ Block 8 implementer: "As the spec words it, `from a.b import c` adds edges to `a/b/__init__.py` and `a/b/c.py` but not to `a/__init__.py`, even though Python does run `a/__init__.py`. I followed the spec." Block 8 test_writer: "Read literally, `from x import y` adds an edge to `x` only, not to `x`'s parent packages." *(Reviewer: this writes down the current under-approximation. If you want the runtime behaviour instead, change the second edge bullet and invert the new scenario.)*

### Requirement: Entry record shape
Each discovered entry SHALL have these fields:

- `kind`: one of `argparse_subcommand`, `click_command`, `typer_command`, `project_script`, `main_module`, `skill_command`, `hook_command` or `seed`.
- `name`: a non-empty string.
- `location`: `{path, line}`, where the entry is declared.
- `handler`: `{path, line, symbol}`, or `null` if no handler resolves.
- `invocation`: the literal command text for `skill_command` and `hook_command`, otherwise `null`.
- `known_edges`: a list of `{from: {path, line}, to: {path, line}}`.
- `also_invoked_by`: a list of `{kind, name, location}`. In discovery output each `kind` is a discovery kind from the same set as `kind`. The mapping to output kinds happens only when `paths.json` is written.

All `path` values SHALL be POSIX-style and relative to the snapshot root. All `line` values SHALL be 1-based.

#### Scenario: Fields present and well-formed
- **WHEN** discovery runs on a fixture containing at least one entry of every discoverable kind
- **THEN** every entry has all seven fields
- **AND** every `path` is relative, uses `/` separators and does not contain `..`
- **AND** every `line` is an integer of 1 or more that does not exceed the line count of the referenced file

#### Scenario: Invoker kinds are discovery kinds until output
- **WHEN** a `project_script` and a `main_module` resolve to the same `def main`
- **THEN** in `discover_entries()` output, the kept entry's `also_invoked_by` record has `kind == "project_script"`
- **AND** in the written `paths.json`, the same record has `kind == "script"`

_Motivated by:_ Block 5 implementer: "The Block 2 interface types `Entry.also_invoked_by` as the paths.v1 `Invoker`, whose kinds are output kinds, while D5 and the tests need discovery kinds; I added an `EntryInvoker` model and `to_path_entry` converts it." Also Block 2 test_writer (2) and Block 2 implementer (4).

### Requirement: argparse handlers resolve through set_defaults and dispatch dicts only
Discovery SHALL resolve a subcommand's `handler` in only two cases:

- **`set_defaults`.** The subparser object calls `set_defaults(func=<name>)`.
- **Dispatch dict.** A module-level or function-level dict literal maps the subcommand's literal name to a function reference.

For dispatch dicts:
- The key is matched against the subcommand's own `add_parser` string literal, not against its space-joined nested name.
- The dict is searched for in the parser's module, and in same-repository modules that the parser's module imports directly, including function-local imports.
- Modules reached only through further imports are not searched.

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

#### Scenario: Nested subcommand matched by its own literal
- **WHEN** subcommand `db` has a nested `add_parser("migrate")`
- **AND** a dispatch dict maps `"migrate"` to `do_migrate`
- **THEN** the `db migrate` entry's handler is `def do_migrate`

#### Scenario: Dict two imports away is not searched
- **WHEN** the parser module imports `a.py`, `a.py` imports `b.py`, and only `b.py` defines a dispatch dict for `open`
- **THEN** the `open` entry has `handler == null`

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

_Motivated by:_ Block 2 implementer: "Dispatch dicts match the `add_parser` literal, not the space-joined name, and are searched in the parser module plus its directly imported same-repo modules."

### Requirement: click and typer commands are discovered
Discovery SHALL emit entries for functions decorated with click or typer command decorators:

| Kind | Decorators |
|---|---|
| `click_command` | `@click.command`, `@<group>.command`, `@click.group`, `@<group>.group` |
| `typer_command` | `@<typer_app>.command`, `@<typer_app>.callback` |

For each entry:

- `location` is the decorator line.
- `handler` is the decorated function's `def` line, with its symbol.
- `name` is the explicit name argument if one is given. Otherwise it is the function name with `_` replaced by `-`. No other transformation applies: there is no suffix stripping and no lowercasing. The name is prefixed by any parent group names, separated by single spaces.

The same naming rule applies to `@<typer_app>.callback` entries.

Groups, apps, parent prefixes and `add_typer` mounts SHALL be resolved only from definitions in the same module as the decorated function.

#### Scenario: click command with default name
- **WHEN** `@cli.command()` decorates `def build_all()`
- **AND** `cli` is a `@click.group()` named `cli`
- **THEN** a `click_command` entry named `cli build-all` is emitted
- **AND** its handler is `def build_all`

#### Scenario: click command with explicit name
- **WHEN** `@cli.command("ship")` decorates `def do_ship()`
- **THEN** the entry name ends with `ship`
- **AND** it does not end with `do-ship`

#### Scenario: No click 8.2 suffix stripping
- **WHEN** `@cli.command()` decorates `def sync_cmd()`
- **THEN** the entry is named `cli sync-cmd`, not `cli sync`

#### Scenario: typer sub-app prefix
- **WHEN** `app.add_typer(users_app, name="users")` is called
- **AND** `@users_app.command()` decorates `def add_user()`
- **THEN** a `typer_command` entry named `users add-user` is emitted

#### Scenario: typer callback name
- **WHEN** `app.add_typer(users_app, name="users")` is called
- **AND** `@users_app.callback()` decorates `def users_main()`
- **THEN** a `typer_command` entry named `users users-main` is emitted

#### Scenario: Group defined in another module
- **WHEN** `cmds.py` has `@cli.command()` on `def build_all()`, and `cli` is a `@click.group()` defined in `main.py` and imported into `cmds.py`
- **THEN** no entry named `cli build-all` is emitted

_Motivated by:_ Block 5 implementer: "The spec has no naming rule for typer callbacks; the command rule is used." and "click and typer groups and apps are resolved within one module only; click 8.2 suffix stripping and lowercasing are not done." Block 5 test_writer: "No naming rule for a typer `@callback` entry."

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

- If the first positional argument after the module names a discovered **top-level** argparse subcommand of that module, the edge goes to that subcommand's handler, or to its `add_parser` line when the handler is `null`. Later positional arguments are not used to select a nested subcommand.
- Otherwise the edge goes to the module's `main_module` location: its `if __name__ == "__main__":` line, or line 1 without one.

The entry's `handler` is the subcommand's handler in the first case, and the `main_module` handler of the target file in the second, so a command merges with the entry it runs.

A command line that `shlex` cannot tokenise (for example, an unclosed quote) SHALL NOT be emitted, and SHALL NOT produce a diagnostic. A line that cannot be tokenised is not known to be a `python -m` command.

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

#### Scenario: Nested subcommand words select only the top level
- **WHEN** the command is `python -m tool db migrate`, and discovery has found both `db` and `db migrate` subcommands of `tool`
- **THEN** the known edge targets the `db` subcommand's handler, or its `add_parser` line when that handler is `null`

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

#### Scenario: Untokenisable command skipped silently
- **WHEN** a `bash` block contains `python -m pr_pipeline open "unclosed`
- **THEN** no entry is emitted for that line
- **AND** no diagnostic is recorded for it

_Motivated by:_ Block 6 refactorer: "A command that can't be tokenised (for example, an unclosed quote) is skipped with no diagnostic in a `SKILL.md`, but gets an `unresolved_target` diagnostic in a hooks file." Block 6 implementer: "only top-level argparse subcommands are matched by a command's first positional." *(Reviewer: the current text says an untokenisable `python -m` command records `unresolved_target`, so the code contradicts it for `SKILL.md`. This modification aligns the spec with the code. If you would rather fix the code, keep the old sentence and move this scenario's expectation to `unresolved_target`.)*

### Requirement: Hook commands are discovered
Discovery SHALL read every `hooks.json` file and every `.claude/settings*.json` file. For every hook object with `"type": "command"`, it SHALL emit one `hook_command` entry:

- `invocation` is the command string.
- `name` is `<event>:<matcher>`, or `<event>` when there is no matcher.
- `location` is the line of the file on which that command string appears. When identical command strings appear more than once, occurrences are assigned in document order.

The command is tokenised after expanding variables:

- `${CLAUDE_PLUGIN_ROOT}` becomes the nearest ancestor of the hooks file that contains `.claude-plugin/plugin.json`. If there is none, it becomes the parent of the hooks file's directory.
- `$CLAUDE_PROJECT_DIR` and `${CLAUDE_PROJECT_DIR}` become the repository root.

Relative paths left in the command after expansion are resolved from the repository root.

The target SHALL then be resolved through shell launchers:

1. Leading launcher tokens are stripped while they are `sh`, `bash`, `/bin/sh`, `/bin/bash`, `exec`, `env`, an environment assignment, a `.sh` file that exists in the snapshot, or `uv run` with its flags.
2. `python[3] -m X`, or `-m X` left after a launcher, uses the same rules as `SKILL.md` commands. The hooks file's directory takes the place of the `SKILL.md` directory in the search roots.
3. Otherwise the first `.py` token that exists resolves as a `main_module` script: its location is the `if __name__ == "__main__":` line or line 1, and its handler is the `main_module` handler.
4. Only when no module or `.py` target exists (the command names none, or the one it names is missing) does the hook target the stripped launcher, or else the first existing file it names, at line 1 with `symbol: null`.

When none of these exists, or the command cannot be tokenised, the target is missing. In that case no entry is emitted, and a diagnostic with reason `unresolved_target` is recorded at the command's line. Otherwise discovery SHALL add a known edge from the command's line to the resolved target.

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

#### Scenario: python -m resolved from the hooks file's directory
- **WHEN** `plugin/hooks/hooks.json` has a hook running `python3 -m guardpkg`
- **AND** `plugin/hooks/guardpkg/__main__.py` exists
- **THEN** the hook's known edge targets the `main_module` location of `plugin/hooks/guardpkg/__main__.py`

#### Scenario: Relative script path resolved from the repository root
- **WHEN** a hooks file at `plugin/hooks/hooks.json` in a repository rooted at the snapshot root has a hook running `python3 scripts/check.py`
- **AND** `scripts/check.py` exists at the repository root
- **THEN** the hook resolves to `scripts/check.py`

#### Scenario: Non-command hook types ignored
- **WHEN** a hook object has `"type": "prompt"`
- **THEN** no entry is emitted for it

#### Scenario: Missing script target
- **WHEN** a hook command references `${CLAUDE_PLUGIN_ROOT}/hooks/gone.py`
- **AND** that file does not exist
- **THEN** no entry is emitted
- **AND** a diagnostic with reason `unresolved_target` is recorded at the command's line

#### Scenario: Missing module target
- **WHEN** a hook runs `python3 -m gone_mod`, with no launcher, and no `gone_mod` exists under any search root
- **THEN** no entry is emitted
- **AND** a diagnostic with reason `unresolved_target` is recorded at the command's line

#### Scenario: Untokenisable hook command
- **WHEN** a hook's command is `python3 "unclosed`
- **THEN** no entry is emitted
- **AND** a diagnostic with reason `unresolved_target` is recorded at the command's line

#### Scenario: Invalid JSON
- **WHEN** a `hooks.json` file is not valid JSON
- **THEN** discovery does not raise
- **AND** a diagnostic with reason `parse_error` is recorded for that file

_Motivated by:_ Block 6 implementer: "hook `python -m` uses the hooks file's directory where SKILL.md uses its own; relative paths run from the repository root; a named but missing `.py` or `-m` target is `unresolved_target` rather than a launcher fallback." Block 6 refactorer: untokenisable hook commands get "an `unresolved_target` diagnostic in a hooks file. The spec doesn't cover this case." *(Reviewer: the implementer's "rather than a launcher fallback" could be read as contradicting the existing "Missing script behind a launcher" scenario. This delta keeps that scenario and pins the reading only for commands with no launcher. Check it against the hook tests before applying.)*

### Requirement: Caller-supplied seeds become entries
Discovery SHALL accept seeds from the caller. Each seed has a `path` relative to the snapshot root, plus a `line`, a `symbol` or both, and an optional `name`. When a seed gives both `line` and `symbol`, the `symbol` is used and the `line` is ignored.

For each valid seed, discovery SHALL emit a `seed` entry:

- `handler` is the function definition at or enclosing that line, or the definition of the named symbol. A line seed with no enclosing definition has `handler: null`.
- `location` of a line seed is the seed's line.
- `name` is the given name if there is one. Otherwise it is `<path>:<symbol>`, or `<path>:<line>` for a line seed with no enclosing definition.

An invalid seed SHALL produce a diagnostic and no entry. Invalid means any of these:

- The file is missing.
- The line is out of range.
- The symbol is not found.

A seed whose `path` is absolute or escapes the snapshot root SHALL raise `ValueError` instead.

#### Scenario: Seed by symbol
- **WHEN** the caller passes the seed `{path: "lib/api.py", symbol: "handle_request"}`
- **AND** that function is defined on line 30
- **THEN** a `seed` entry is emitted with `handler.line == 30`

#### Scenario: Symbol wins over line
- **WHEN** the caller passes `{path: "lib/api.py", line: 5, symbol: "handle_request"}`, and `handle_request` is defined on line 30
- **THEN** the seed entry's handler is `def handle_request` at line 30

#### Scenario: Line seed outside any function
- **WHEN** the caller passes `{path: "lib/api.py", line: 2}`, and line 2 is a module-level import
- **THEN** a `seed` entry is emitted with `location.line == 2`, `handler == null` and name `lib/api.py:2`

#### Scenario: Seed with missing file
- **WHEN** the caller passes a seed whose path does not exist in the snapshot
- **THEN** no entry is emitted for it
- **AND** a diagnostic with reason `seed_not_found` is recorded
- **AND** discovery does not raise

#### Scenario: Seed line out of range
- **WHEN** a seed names line 9999 of a 50-line file
- **THEN** a diagnostic with reason `seed_not_found` is recorded
- **AND** no entry is emitted for it

#### Scenario: Escaping seed path
- **WHEN** the caller passes a seed with path `../x.py` or `/abs/x.py`
- **THEN** discovery raises `ValueError`

_Motivated by:_ Block 2 implementer: "an absolute or escaping seed path raises `ValueError` instead of producing a diagnostic" and "Seed rules the spec leaves open: symbol wins over line; a line seed's location is the seed line; a line seed with no enclosing def gives `handler: null` and the name `<path>:<line>`." Block 2 test_writer (3).

### Requirement: Entries resolving to the same handler are merged
When two or more entries resolve to the same handler `(path, line)`, and that handler is a Python function (`handler.symbol` is not `null`), discovery SHALL emit exactly one entry for that handler.

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

Entries with `handler == null`, or with a handler whose `symbol` is `null` (a launcher or file at line 1), are merged only with entries that have an identical `(kind, location)`.

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

#### Scenario: Unresolved subcommands declared on the same line are merged
- **WHEN** one helper function calls `sub.add_parser("list")` and is called for two parent subcommands, so both `list` subcommands share kind and `add_parser` line, and both have `handler == null`
- **THEN** one entry is emitted for that location
- **AND** the other subcommand appears in its `also_invoked_by`

#### Scenario: Shared launcher targets not merged
- **WHEN** two hooks both resolve to `{path: "plugin/hooks/run.sh", line: 1, symbol: null}` from different command lines
- **THEN** both are emitted as separate entries

_Motivated by:_ Block 5 implementer: "D7's null-handler merge on identical (kind, location) contradicts the Block 2 test where one helper declares `list` under two parents on the same line; the Block 2 test was changed to accept the merged form." This also brings D7's `symbol`-null rule, which already governs the hook-launcher scenarios, into the merge requirement.

### Requirement: Every skipped candidate is explained
Every candidate that discovery examines but does not emit as an entry SHALL produce exactly one diagnostic `{path, line, reason, detail}`. This includes candidates skipped for an unparseable file, an unresolved target, a dynamic name or an invalid seed.

`reason` SHALL be one of `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name` or `seed_not_found`. `line` SHALL be `null` only in two cases: file-level parse errors, and `seed_not_found` for a symbol seed whose file is missing.

Entries merged into another entry are recorded in `also_invoked_by`, not as diagnostics.

An `unresolved_handler` diagnostic accompanies an entry that is still emitted, with `handler == null`. It is recorded when:
- an argparse subcommand's `set_defaults(func=…)` value, or its dispatch-dict value, does not resolve to a function in the same repository; or
- a `main_module` guard or module body calls more than one candidate function after unwrapping transparent wrappers.

It is not recorded in these cases:
- an argparse subcommand with no handler mechanism (no `set_defaults(func=…)` and no matching dispatch-dict entry);
- an argparse subcommand dispatched by comparison;
- a `main_module` whose body calls no candidate function.

#### Scenario: Mixed failures in one fixture
- **WHEN** a fixture contains one broken `.py` file, one invalid `hooks.json`, one `[project.scripts]` key with an external target, and one `add_parser(name_var)` call
- **AND** the caller passes one missing seed
- **THEN** discovery returns exactly five diagnostics
- **AND** their reasons are `parse_error`, `parse_error`, `unresolved_target`, `dynamic_name` and `seed_not_found`
- **AND** every valid entry in the fixture is still returned

#### Scenario: Subcommand with no handler mechanism
- **WHEN** a module calls `sub.add_parser("open")` and nothing sets or maps a handler for `open`
- **THEN** the `open` entry is emitted with `handler == null`
- **AND** no `unresolved_handler` diagnostic is recorded for it

#### Scenario: main_module calling two functions
- **WHEN** `tools/report.py` has `if __name__ == "__main__": setup(); run()`, with both functions defined in the same file
- **THEN** a `main_module` entry is emitted with `handler == null`
- **AND** one `unresolved_handler` diagnostic is recorded at the guard line

#### Scenario: Missing file for a symbol seed
- **WHEN** the caller passes `{path: "gone.py", symbol: "f"}` and `gone.py` does not exist
- **THEN** the `seed_not_found` diagnostic has `line: null`

_Motivated by:_ Block 2 implementer: "`unresolved_handler` is emitted only when a `set_defaults(func=…)` or a dispatch-dict value fails to resolve; a subcommand with no handler mechanism gets no diagnostic, unlike D6's 'every null handler' wording" and "`seed_not_found` for a symbol seed on a missing file has `line: null`, against 'line is null only for parse errors'". Block 5 implementer: "a `main_module` only gets one when it calls several functions, in line with Block 2's partial approach." Block 5 test_writer: "the spec's mixed-failure count only works if it doesn't." *(Reviewer: D6 says "every null-handler entry" except comparison dispatch. Once this is applied, D6 should be narrowed to match.)*

### Requirement: Fallback entries do not duplicate deterministic entries
A fallback entry SHALL be dropped with reason `duplicate` in either of these cases:

- its cited definition `(path, line)` is the same as that of an entry already found by deterministic discovery or by caller seeds;
- it resolves to the same handler as such an entry. This means the enclosing `def` of its cited line is that entry's handler.

Deterministic and seeded entries always win over model entries. Two fallback entries that cite the same `(path, line)` SHALL be collapsed into one. Entries that cite the same line number in different files are not duplicates.

#### Scenario: Model repeats a deterministic entry
- **WHEN** deterministic discovery found an argparse subcommand at `cli.py:20`, and the stub also returns an entry citing `cli.py:20`
- **THEN** only the deterministic entry is traced
- **AND** the model entry is recorded as dropped with reason `duplicate`

#### Scenario: Model cites a line inside a discovered handler
- **WHEN** discovery resolved a `click_command` to the handler `def sync()` at `app/cli.py:30-45`, and the stub returns an entry citing `app/cli.py:33` with matching text
- **THEN** the model entry is dropped with reason `duplicate`

#### Scenario: Model lists the same entry twice
- **WHEN** the stub returns two entries that cite the same verified line
- **THEN** exactly one model entry for that line is passed on for tracing

#### Scenario: Same line number in different files
- **WHEN** the stub returns verified entries citing `a.py:5` and `b.py:5`
- **THEN** both are passed on for tracing

_Motivated by:_ Block 7 test_writer: "The duplicate check is unclear on whether it compares path and line or the line alone; the tests use path and line." Block 7 refactorer: "`_Filter._duplicate` also drops a fallback entry whose enclosing `def` is a discovered entry's handler." *(D8 should gain the same-handler rule, which the spec already states.)*

### Requirement: Malformed or empty answers break at hop 1
The gate SHALL report hop 1 as the broken hop in two cases: when an agent answer cannot be parsed into a list of hops, and when it parses to zero hops. The broken hop index is the gate's `broken_at`. It is not the path's `stops_at`, which counts retained hops. No hop is retained in either case, so after the one re-ask such an entry is never written as a path. If the re-asked answer still breaks at hop 1, the entry is dropped with stage `trace` and the break code as its reason.

#### Scenario: Unparseable answer
- **WHEN** the agent answer does not validate against the hop model
- **THEN** the gate reports broken hop index 1 and `break_reason` = `malformed_answer`

#### Scenario: Empty hop list
- **WHEN** the agent answer parses to an empty hop list, including `gate_hops([])`
- **THEN** the gate reports broken hop index 1 and `break_reason` = `empty_trace`

#### Scenario: Still empty after the re-ask
- **WHEN** both the first answer and the re-asked answer have zero hops
- **THEN** no path is returned for the entry
- **AND** it is dropped with stage `trace` and reason `empty_trace`

_Motivated by:_ Block 3 test_writer: "trace-gate's 'Malformed or empty answers break at hop 1' scenarios still say 'stops_at = 1'; that is gate-level `broken_at`, and I left them alone because they are outside D1 rows 2–5" and "the tests expect `gate_hops([])` to return broken_at 1 with `empty_trace`."

### Requirement: Every hop citation re-reads at the commit
Each hop's citation SHALL be re-read through `Resolve` at the commit under trace. The citation check fails if any of these is true:

- the file does not exist;
- the cited line range falls outside the file: the start line is below 1, the end line is before the start line, or the end line is past the end of the file;
- the quoted text does not match the file content at the cited lines.

Text is compared after the same whitespace normalisation that `code_facts` citation checking already uses.

#### Scenario: Missing file
- **WHEN** a hop cites a file that does not exist at the commit
- **THEN** that hop is broken with `break_reason` = `missing_file`

#### Scenario: Line out of range
- **WHEN** a hop cites line 500 of a 40-line file
- **THEN** that hop is broken with `break_reason` = `line_out_of_range`

#### Scenario: Start line below 1
- **WHEN** a hop cites lines 0–3 of an existing file
- **THEN** that hop is broken with `break_reason` = `line_out_of_range`

#### Scenario: End before start
- **WHEN** a hop cites lines 12–10 of an existing file
- **THEN** that hop is broken with `break_reason` = `line_out_of_range`

#### Scenario: Quoted text differs
- **WHEN** a hop cites an existing line, but its quoted text differs from that line at the commit
- **THEN** that hop is broken with `break_reason` = `text_mismatch`

#### Scenario: Citation matches
- **WHEN** a hop's path, line range and quoted text all match the commit
- **THEN** the citation part of the check passes for that hop

_Motivated by:_ Block 3 implementer: "`TraceHop` lines are plain ints, unlike walkthrough `Hop` (`ge=1`), so that `line_out_of_range` can cover start below 1 or end before start, as D11 lists."

### Requirement: Traces are capped at 20 hops
The gate SHALL accept at most 20 hops. Hops beyond the 20th SHALL be discarded and SHALL NOT trigger a re-ask. When hops are discarded, the gate reports broken hop index 21 with `hop_limit`, and the path records `stops_at` = 20.

#### Scenario: Over-long trace
- **WHEN** an answer contains 25 hops and the first 20 are valid
- **THEN** the gate reports broken hop index 21 with `break_reason` = `hop_limit`
- **AND** the result has 20 hops, `stops_at` = 20 and `break_reason` = `hop_limit`
- **AND** no re-ask is issued

#### Scenario: Exactly 20 hops
- **WHEN** an answer contains exactly 20 valid hops
- **THEN** the result has 20 hops, and both `stops_at` and `break_reason` are null

_Motivated by:_ Block 3 implementer: "`GateResult.broken_at` is 21 for `hop_limit`."

### Requirement: The first broken hop decides the outcome
The gate SHALL check hops in order and SHALL stop at the first broken hop. Its 1-based index is the gate's broken hop index. A path cut there keeps the hops before it, so `stops_at` is one less than that index. When a hop fails several rules, the reported `break_reason` SHALL be the first failing rule in this order:

1. path_outside_root
2. missing_file
3. line_out_of_range
4. text_mismatch
5. base_side_citation
6. entry_mismatch
7. unconnected_hop
8. repeated_hop

#### Scenario: Later hops ignored after a break
- **WHEN** hop 3 is broken and hop 6 is also broken
- **THEN** the gate reports broken hop index 3, and `break_reason` reflects hop 3 only
- **AND** if this is the re-asked answer, the path keeps hops 1–2 with `stops_at` = 2

#### Scenario: Multiple failures on one hop
- **WHEN** a hop cites an out-of-range line and also has no connection to its predecessor
- **THEN** `break_reason` = `line_out_of_range`

_Motivated by:_ Block 3 test_writer (4): the trace-gate scenarios' "stops_at" values are "gate-level `broken_at`". The same reading is applied here, so that "Later hops ignored after a break" no longer contradicts D1 row 2 ("`stops_at` is the 1-based index of the last retained hop").

### Requirement: A broken trace gets exactly one re-ask
When the first gate check reports a break, the tracing step SHALL issue one re-ask through the caller's `CallFn`. The exception is `hop_limit`, which gets no re-ask.

The re-ask SHALL state:
- the entry, by its handler symbol, or by its name when it has no handler, even when no hop was accepted;
- the broken hop index;
- the `break_reason` and its detail;
- the accepted prefix.

The re-asked answer SHALL be checked by the gate from hop 1. No second re-ask SHALL be issued.

#### Scenario: Re-ask repairs the trace
- **WHEN** the first answer breaks at hop 4, and the re-asked answer passes every check
- **THEN** exactly two `CallFn` calls are made for that entry
- **AND** the result uses the re-asked hops, with null `stops_at` and `break_reason`

#### Scenario: Re-ask prompt carries the reason
- **WHEN** a re-ask is issued for a break at hop 4 with `text_mismatch`
- **THEN** the prompt passed to `CallFn` contains the hop index 4 and the string `text_mismatch`

#### Scenario: Re-ask prompt names the entry with no accepted hops
- **WHEN** the first answer for an entry with handler symbol `cmd_open` is malformed
- **THEN** the re-ask prompt contains `cmd_open`

#### Scenario: No second re-ask
- **WHEN** both the first answer and the re-asked answer break
- **THEN** exactly two `CallFn` calls are made for that entry

#### Scenario: Valid first answer needs no re-ask
- **WHEN** the first answer passes every check
- **THEN** exactly one `CallFn` call is made for that entry

_Motivated by:_ Block 3 test_writer: "The fake needs `paths_reask` to name the entry (its handler symbol or name) even when there are no accepted hops, because re-ask labels can't be linked back to an entry."

### Requirement: CLI subcommand invocation is recognised
For entries of kind argparse, click or typer subcommand, a test SHALL be listed when the subcommand's program is invoked with that subcommand's name as a token. The invocation must match one of these forms:
- **argparse program call.** Either a call to a function of the subparser's module whose body calls `.parse_args(` or `.parse_known_args(`, or `<f>().parse_args(argv)` where `f` is a function of that module (a parser builder). In both cases the argv argument must be a literal list or tuple containing the subcommand name.
- **CliRunner.** `CliRunner().invoke(<resolved click/typer app>, <literal args>)`, with the subcommand name in the args. The args are matched relative to the invoked app's mount prefix. An invocation whose args name no command word does not match any command path, even for a typer app with a single command.
- **Subprocess.** A subprocess invocation as defined in the subprocess requirement.

A literal list or tuple may hold elements that are not string constants (such as `str(tmp_path)`). Those are unknown tokens and never match a subcommand name. Argv that is not a literal list or tuple SHALL NOT match under this rule.

Tests that cannot reach the handler SHALL NOT be listed (D1#13). Argv containing `-h`, `--help` or `--version` never matches, because argparse exits before any handler runs.

A test that calls a helper with literal arguments SHALL be matched through the helper (D15) when the helper forwards a parameter into one of these forms as `p`, `[*prefix, *p]`, `["x", *p]` or `list(p)`. A helper is a function of any of these:
- the test module;
- a `conftest.py` in the test's directory or above;
- a non-test module under the test's `tests/` tree.

Inlining goes at most two helpers deep. The record takes the test's call line and the inner form's match kind.

#### Scenario: argparse main with subcommand argv
- **WHEN** a test calls `main(["render", "--out", "x"])`, where `main` resolves to the module that defines the `render` subparser
- **THEN** the test is listed for the `render` path with match kind `cli_argv`, and not for the `open` path of the same program

#### Scenario: click CliRunner invocation
- **WHEN** a test calls `CliRunner().invoke(cli, ["sync"])`, where `cli` resolves to the click group that owns the `sync` command
- **THEN** the test is listed for the `sync` path with match kind `cli_runner`

#### Scenario: typer sub-app invoked directly
- **WHEN** `app.add_typer(users_app, name="users")` mounts `users_app`, and a test calls `CliRunner().invoke(users_app, ["add-user"])`
- **THEN** the test is listed for the `users add-user` path with match kind `cli_runner`

#### Scenario: Single-command typer app invoked without its name
- **WHEN** a typer `app` has exactly one `@app.command()` on `def run()`, and a test calls `CliRunner().invoke(app, ["--fast"])`
- **THEN** the test is not listed for the `run` path

#### Scenario: Dispatch on args.command still matches
- **WHEN** the fixture mirrors `pr_pipeline`, which dispatches on `args.command`, so the `open` entry lands on its `add_parser` line, and a test calls `main(["open"])`
- **THEN** the test is listed for the `open` path

#### Scenario: Non-literal argv does not match
- **WHEN** a test calls `main(argv)`, where `argv` is a local variable or a parametrized argument
- **THEN** the test is not listed under `cli_argv` for any subcommand path

#### Scenario: Help and version runs are excluded
- **WHEN** a test calls `main(["open", "--help"])`, `main(["open", "-h"])` or `main(["open", "--version"])`
- **THEN** the test is not listed for the `open` path

#### Scenario: Helper forwarding literal argv
- **WHEN** a test module defines `def run(argv): return commands.run(build_parser().parse_args(argv))`, `build_parser` is defined in the module that declares the `open` subparser, and a test calls `run(["open", "--repo", str(repo)])`
- **THEN** the test is listed for the `open` path with match kind `cli_argv` and the `run(...)` call's line as `invocation_line`

#### Scenario: Inlining stops after two helpers
- **WHEN** `three(argv)` calls `two(argv)`, which calls `one(argv)`, which calls `main(argv)`, and one test calls `two(["open"])` and another `three(["open"])`
- **THEN** only the test calling `two(["open"])` is listed for the `open` path

_Motivated by:_ Block 9 implementer: "Typer words are relative to the app's mount prefix, so a single-command typer app invoked without its name is missed." *(Reviewer: the last typer scenario records a known gap, not a goal. Delete it if the gap is meant to be fixed.)*

### Requirement: Subprocess, runpy and script invocation is recognised
A test SHALL be listed when it statically runs the entry's command line through any of these:
- `subprocess.run`;
- `subprocess.call`;
- `subprocess.check_call`;
- `subprocess.check_output`;
- `subprocess.Popen`;
- `runpy.run_module`.

The command can be a literal list of string constants or a single string constant. It matches the entry when its tokens match the entry's invocation:
- `python -m <module> [<subcommand>]` (with `sys.executable`, `python` or `python3` as the interpreter) for `__main__` and `SKILL.md` entries;
- the script name as the first token for `[project.scripts]` entries;
- the hook's configured command for hook entries.

A console-script invocation matches only the path whose entry is, or lists in `also_invoked_by`, that `[project.scripts]` entry. Later tokens are not matched against the subcommands of an argparse program the script's target runs.

#### Scenario: python -m with sys.executable
- **WHEN** a test calls `subprocess.run([sys.executable, "-m", "pkg", "render"])`
- **THEN** the test is listed for the `pkg` `render` path with match kind `subprocess`

#### Scenario: runpy module run
- **WHEN** a test calls `runpy.run_module("pkg", run_name="__main__")`, and the path's entry is the `pkg` `__main__` module
- **THEN** the test is listed with match kind `runpy`

#### Scenario: Console script name
- **WHEN** `[project.scripts]` maps `mytool` to `pkg.cli:main`, and a test calls `subprocess.run(["mytool", "build"])`
- **THEN** the test is listed for the `mytool` path with match kind `subprocess`

#### Scenario: Console script running an argparse program
- **WHEN** `[project.scripts]` maps `mytool` to `pkg.cli:main`, `pkg/cli.py` declares a `render` subparser with its own handler, and a test calls `subprocess.run(["mytool", "render"])`
- **THEN** the test is listed for the path that carries the `mytool` entry
- **AND** it is not listed for the `render` subcommand path

#### Scenario: Hook command string
- **WHEN** a hook entry's command is `python3 hooks/guard.py`, and a test calls `subprocess.run("python3 hooks/guard.py", shell=True)`
- **THEN** the test is listed for that hook path with match kind `subprocess`

#### Scenario: Different module does not match
- **WHEN** a test runs `python -m otherpkg render`
- **THEN** the test is not listed for the `pkg` `render` path

_Motivated by:_ Block 9 implementer: "A console script that targets an argparse program's main does not match that program's subcommand paths, only the project_script entry."

### Requirement: Tests that mock hop 2 are listed separately
A test that would otherwise be listed for a path SHALL instead appear in that path's `e2e_tests_mocking_hop_2` list, and not in `e2e_tests`, when it patches the symbol at hop 2 of the path. Patching is recognised in these forms:
- `mock.patch` or `unittest.mock.patch` with a string target, used as a decorator, as a context manager, or through `.start()`;
- `mock.patch.object` with a resolved object and an attribute name;
- `monkeypatch.setattr` with either a dotted-string target or an object-and-name pair.

The target matches hop 2 when either of these holds:
- it names the symbol at its defining module;
- it names the symbol under a name bound in hop 1's module. A binding counts when it is visible at the lines hop 1 cites: module-level bindings, plus function-local imports in each scope that encloses those lines.

Patches are matched statically, whatever the invocation form. A patch still counts when the test starts the entry in a separate process, where it could not take effect at runtime.

#### Scenario: Patch at the defining module
- **WHEN** hop 2 is `pkg.core.run`, and a test that calls `main(["render"])` is decorated with `@mock.patch("pkg.core.run")`
- **THEN** the test appears in `e2e_tests_mocking_hop_2` for the `render` path, and not in `e2e_tests`

#### Scenario: Patch at the importing module
- **WHEN** hop 1's module `pkg.cli` does `from pkg.core import run`, and a test uses `with mock.patch("pkg.cli.run"):` around `main(["render"])`
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: Patch at a function-local binding
- **WHEN** hop 1 cites lines inside `pkg.cli.main`, `main` contains `from pkg.core import run`, and a test patches `pkg.cli.run` and calls `main(["render"])`
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: monkeypatch.setattr with module object
- **WHEN** a test does `monkeypatch.setattr(core, "run", fake)`, where `core` resolves to `pkg.core`, and then invokes the entry
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: Patch counted for a subprocess invocation
- **WHEN** a test patches `pkg.core.run` (hop 2) and runs `subprocess.run([sys.executable, "-m", "pkg", "render"])`
- **THEN** the test appears in `e2e_tests_mocking_hop_2`, and not in `e2e_tests`

#### Scenario: Patching another symbol does not move the test
- **WHEN** a test invokes the entry and patches only `pkg.core.log`, which is not hop 2
- **THEN** the test appears in `e2e_tests`, and not in `e2e_tests_mocking_hop_2`

#### Scenario: Single-hop path
- **WHEN** a path has exactly one hop, and a test invokes its entry with patches applied
- **THEN** the test appears in `e2e_tests`, and `e2e_tests_mocking_hop_2` is empty

_Motivated by:_ Block 10 implementer: "The spec says hop-2 names come from 'hop 1's module', but function-local bindings depend on the line, so I resolve them at the lines hop 1 cites (one lookup per enclosing scope)" and "Patches still count when the test starts the CLI in a separate process, where they could not take effect at runtime; static matching can't tell."

### Requirement: Hop-2 patches applied by fixtures are recognised
A patch of hop 2 SHALL also count when it is applied inside a pytest fixture that either of these makes available to the test:
- the test requests it by parameter name;
- it is `autouse=True`.

A parameter requests a fixture only when pytest itself would treat it as a fixture request. These parameters do not request fixtures:
- `self`;
- parameters with a default value;
- parameters filled by a `mock.patch` decorator.

The fixture can be defined in the test's module or in a `conftest.py` in the test file's directory or any ancestor up to the snapshot root.

#### Scenario: Requested conftest fixture patches hop 2
- **WHEN** `tests/conftest.py` defines a fixture `fake_run` that does `monkeypatch.setattr("pkg.core.run", ...)`, and a test with parameter `fake_run` invokes the entry
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: Unrequested non-autouse fixture is ignored
- **WHEN** the same fixture exists, but the test does not request it and it is not autouse
- **THEN** the test appears in `e2e_tests`

#### Scenario: Defaulted or mock-filled parameter does not request a fixture
- **WHEN** the same fixture `fake_run` exists, and a test either declares `fake_run=None`, or is decorated with `@mock.patch("pkg.other")` whose mock fills its first parameter named `fake_run`
- **THEN** the fixture is not treated as requested, and the test appears in `e2e_tests`

_Motivated by:_ Block 10 implementer: "I followed pytest's own rules for which parameters request fixtures (mock-filled, `self` and defaulted parameters don't), which the spec leaves open."

### Requirement: Unreadable test files are skipped with a reason
A test file that cannot be decoded as UTF-8, or that raises `SyntaxError` under `ast.parse`, SHALL be skipped. The skip SHALL be recorded in the result's test-listing skips, with the file path relative to the snapshot root and a reason of `decode_error` or `syntax_error`. Listing SHALL continue for all other files.

An empty (zero-byte) test file is neither skipped nor a source of tests. It produces no skip record and no listed tests.

#### Scenario: Syntax error in one file
- **WHEN** `tests/test_broken.py` has a syntax error and `tests/test_ok.py` invokes an entry
- **THEN** `tests/test_ok.py` is listed for that entry, and the skips contain `{file: "tests/test_broken.py", reason: "syntax_error"}`

#### Scenario: Non-UTF-8 file
- **WHEN** a test file contains invalid UTF-8 bytes
- **THEN** it is recorded with reason `decode_error`, and no exception propagates from `map_paths(...)`

#### Scenario: Zero-byte test file
- **WHEN** `tests/test_empty.py` is zero bytes long
- **THEN** no skip is recorded for it, and no test from it is listed

_Motivated by:_ Block 9 test_writer: "Unspecified behaviour pinned: … a zero-byte test file is neither a skip nor a test."

## REMOVED Requirements

## Notes considered and not proposed
- **Stray `code_facts/uv.lock` from the test command** (Blocks 1, 3, 4; Block 5 refactorer says one was committed). D18's test command `uv run --directory plugins/multiplai-dev/lib/code_facts …` contradicts D18's "only the root `uv.lock` changes". This belongs in the design and repo process, not the capability specs. Recommend changing D18's command, for example running from a workspace member that depends on `code_facts`, and removing the committed lockfile.
- **The two Block 1 implementer entries disagree.** One says drop `reason`, `break_reason` and `Invoker.kind` are closed Literals (enums in the schema) and that defaulted fields are required. The other says they stay `str` and that `note`/`side` are not required. Block 2's notes ("Block 1's `Invoker.kind` is the output-kind Literal") support the first. No enum or `required` rule is proposed until someone checks the committed `paths.v1.schema.json`. If the enums are real, "Every drop is recorded with a reason" and "Path object shape" should say so.
- **`$defs` use class names instead of D16's lowercase names; docstrings become schema `description`s** (Block 1). These are D16 and process detail. The spec only requires `$defs` for reused shapes.
- **New modules not in D2** (`cache.py`, `commands.py`, `scope.py`) **and circular-import workarounds** (Blocks 2, 3, 4, 6, 7, 8). These are design-layout updates for D2. No behaviour changes.
- **`ask` gained `schema`/`budget_label`/`encode` parameters, contrary to D9's "reused unchanged"** (Block 3 implementer). D9 should be updated. No capability requirement changes.
- **`cache_key` hashing details** (`{"kind", "payload"}`, `ensure_ascii=False`; Block 4). These are D9 key-construction detail and give no observable difference for ASCII inputs.
- **D8 lacks the same-handler duplicate rule** (Block 7). The capability spec already has it. D8 should be brought in line.
- **Internal type choices.** These are `Diagnostic` as an alias of `PathDiagnostic`, `EntryInvoker`, `Entry.kind` widened to include `"model"`, the placeholder id `"p1"` before renumbering, `{"malformed": …}` cache storage, and `repo_root` spelt `""`. They are internal representations, and the layered kind vocabulary is already captured in "Entry record shape".
- **`UNTRUSTED_RULES` variant pointing at `note`** (Block 3). This is prompt wording. No contract change.
- **Cross-repository import fallback order** (Block 8 test_writer, implementer). The build did not settle "all roots first" against "each root then its `src/`". The test_writer chose fixtures that are robust to both. Writing a rule would invent one, so D13 should pin it first.
- **"Root group function name as prefix gives names nobody types"** (Block 5 test_writer). This is a product question about the existing rule, not a mismatch. It is left for a deliberate decision.
- **A patch of `pkg.cli.go` counts as hop 2 even though it would raise `AttributeError` at runtime** (Block 10 test_writer). This is a consequence of the static binding rule proposed above. It is accepted as over-approximation, consistent with the design's "treat `tests.e2e` as candidates".
- **Already reflected in the current spec text:**
  - `<builder>().parse_args(argv)` and computed list elements (Block 9);
  - the console-script scenario using `build` (Block 9);
  - commands taking the `main_module` handler (Block 6);
  - empty drop reason rejected (Block 1 test_writer 4);
  - out-of-range fallback citations → `citation_unverified` (Block 7);
  - argparse edge from the `set_defaults` line when there is no dispatch call (Block 10 test_writer);
  - `build/` and `dist/` not excluded from test listing (Block 9).
- **Test-process observations.**
  - Untested `fallback=True` and vacuously passing tests (Blocks 1, 7, 8, 10).
  - The `_ReversedScandir` fake not being an iterator (Blocks 2, 9).
  - Tests edited after the test step (Blocks 5, 9).
  - The `ruff` `I001` finding (Block 2).
  - The `e2e.py` vs `e2e_tests.py` rubric naming (Block 9).
  - The unused `root` parameter on `applied_patches` (Block 10).
  - Interim "every entry is affected" before reachability existed (Block 4).
  - `tomllib` line scanning (Block 5).

  None of these changes the contract.
