## ADDED Requirements

### Requirement: Listing runs statically with no model call and no execution
End-to-end test listing SHALL be computed in Python by parsing test source files with `ast`. It SHALL NOT call the caller's `CallFn`. It SHALL NOT import, execute or collect test modules. It SHALL NOT spawn subprocesses or run pytest.

#### Scenario: No model call during listing
- **WHEN** `map_paths(...)` runs on a fixture repository with traced paths, and the `CallFn` records every call together with the stage that made it
- **THEN** no recorded call comes from e2e test listing, and the listing output is identical when the test-listing step is re-run with a `CallFn` that raises on any call

#### Scenario: Test module side effects are not triggered
- **WHEN** a fixture test file contains top-level code that raises an exception and writes a marker file
- **THEN** listing completes, the marker file does not exist afterwards, and tests in that file are still considered for listing

#### Scenario: No subprocess is spawned
- **WHEN** listing runs while `subprocess.Popen` and `os.system` are patched to raise
- **THEN** listing completes without raising

### Requirement: Test discovery scope
Listing SHALL consider every file under the snapshot root whose name matches `test_*.py` or `*_test.py`. It SHALL skip files under `.git`, `.venv`, `venv`, `node_modules`, `__pycache__` and `site-packages` directories. A test is one of the following:
- a module-level function whose name starts with `test`;
- a method whose name starts with `test`, defined in a module-level class whose name starts with `Test`.

Nested functions and functions in other classes SHALL NOT be listed as tests.

#### Scenario: Module-level and class-based tests are found
- **WHEN** a fixture contains `tests/test_cli.py` with a function `test_open` and a class `TestRender` with a method `test_basic`, and both invoke a path's entry
- **THEN** both are listed, with node ids `tests/test_cli.py::test_open` and `tests/test_cli.py::TestRender::test_basic`

#### Scenario: Non-test functions and excluded directories are ignored
- **WHEN** an entry is invoked by a helper function `run_helper` in `tests/test_cli.py`, by a nested function inside a test, and by a `test_x` function in `.venv/lib/test_vendor.py`
- **THEN** none of these three are listed for that path

#### Scenario: Paths are relative to the snapshot root in multi-repository snapshots
- **WHEN** the snapshot root contains `repo-a/` and `repo-b/`, and a test in `repo-b/tests/test_hook.py` invokes a path's entry
- **THEN** the listed test's file is `repo-b/tests/test_hook.py`, and its node id starts with `repo-b/tests/test_hook.py::`

### Requirement: Direct handler invocation is recognised
A test SHALL be listed for a path when its body contains a call whose callee resolves to the path's entry handler. Resolution SHALL follow the test module's `import` and `from ... import ... as ...` statements, including attribute access on an imported module. A call to a same-named symbol from a different module SHALL NOT match.

#### Scenario: Aliased import call matches
- **WHEN** a test does `from pkg.cli import handle_open as h` and calls `h(args)`, and the path's entry handler is `pkg.cli.handle_open`
- **THEN** the test is listed with match kind `direct_call`

#### Scenario: Module attribute call matches
- **WHEN** a test does `import pkg.cli as cli` and calls `cli.handle_open(args)`
- **THEN** the test is listed with match kind `direct_call`

#### Scenario: Same name from another module does not match
- **WHEN** a test imports `handle_open` from `other.module` and calls it, and the entry handler is `pkg.cli.handle_open`
- **THEN** the test is not listed for that path

### Requirement: CLI subcommand invocation is recognised
For entries of kind argparse, click or typer subcommand, a test SHALL be listed when the subcommand's program is invoked with that subcommand's name as a token. The invocation must match one of these forms:
- a call to a function of the subparser's module whose body calls `.parse_args(` or `.parse_known_args(`, or `<f>().parse_args(argv)` where `f` is a function of that module (a parser builder), whose argv argument is a literal list or tuple containing the subcommand name;
- `CliRunner().invoke(<resolved click/typer app>, <literal args>)` with the subcommand name in the args;
- a subprocess invocation as defined in the subprocess requirement.

A literal list or tuple may hold elements that are not string constants (such as `str(tmp_path)`); those are unknown tokens and never match a subcommand name. Argv that is not a literal list or tuple SHALL NOT match under this rule.

Tests that cannot reach the handler SHALL NOT be listed (D1#13): argv containing `-h`, `--help` or `--version` never matches, because argparse exits before any handler runs.

A test that calls a helper with literal arguments SHALL be matched through the helper (D15) when the helper forwards a parameter into one of these forms as `p`, `[*prefix, *p]`, `["x", *p]` or `list(p)`. A helper is a function of the test module, of a `conftest.py` in the test's directory or above, or of a non-test module under the test's `tests/` tree. Inlining goes at most two helpers deep; the record takes the test's call line and the inner form's match kind.

#### Scenario: argparse main with subcommand argv
- **WHEN** a test calls `main(["render", "--out", "x"])`, where `main` resolves to the module that defines the `render` subparser
- **THEN** the test is listed for the `render` path with match kind `cli_argv`, and not for the `open` path of the same program

#### Scenario: click CliRunner invocation
- **WHEN** a test calls `CliRunner().invoke(cli, ["sync"])`, where `cli` resolves to the click group that owns the `sync` command
- **THEN** the test is listed for the `sync` path with match kind `cli_runner`

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

#### Scenario: python -m with sys.executable
- **WHEN** a test calls `subprocess.run([sys.executable, "-m", "pkg", "render"])`
- **THEN** the test is listed for the `pkg` `render` path with match kind `subprocess`

#### Scenario: runpy module run
- **WHEN** a test calls `runpy.run_module("pkg", run_name="__main__")`, and the path's entry is the `pkg` `__main__` module
- **THEN** the test is listed with match kind `runpy`

#### Scenario: Console script name
- **WHEN** `[project.scripts]` maps `mytool` to `pkg.cli:main`, and a test calls `subprocess.run(["mytool", "build"])`
- **THEN** the test is listed for the `mytool` path with match kind `subprocess`

#### Scenario: Hook command string
- **WHEN** a hook entry's command is `python3 hooks/guard.py`, and a test calls `subprocess.run("python3 hooks/guard.py", shell=True)`
- **THEN** the test is listed for that hook path with match kind `subprocess`

#### Scenario: Different module does not match
- **WHEN** a test runs `python -m otherpkg render`
- **THEN** the test is not listed for the `pkg` `render` path

### Requirement: Alternate invokers count
A test SHALL be listed for a path when it invokes the path's entry or any invoker recorded in that entry's `also_invoked_by`.

#### Scenario: Test invokes the deduplicated invoker
- **WHEN** a hook command and a `SKILL.md` command resolve to the same handler, the path keeps the `SKILL.md` kind with the hook in `also_invoked_by`, and a test runs only the hook command
- **THEN** the test is listed for that path

### Requirement: Tests that mock hop 2 are listed separately
A test that would otherwise be listed for a path SHALL instead appear in that path's `e2e_tests_mocking_hop_2` list, and not in `e2e_tests`, when it patches the symbol at hop 2 of the path. Patching is recognised in these forms:
- `mock.patch` or `unittest.mock.patch` with a string target, used as a decorator, as a context manager, or through `.start()`;
- `mock.patch.object` with a resolved object and an attribute name;
- `monkeypatch.setattr` with either a dotted-string target or an object-and-name pair.

The target matches hop 2 when either of these holds:
- it names the symbol at its defining module;
- it names the symbol as imported into hop 1's module.

#### Scenario: Patch at the defining module
- **WHEN** hop 2 is `pkg.core.run`, and a test that calls `main(["render"])` is decorated with `@mock.patch("pkg.core.run")`
- **THEN** the test appears in `e2e_tests_mocking_hop_2` for the `render` path, and not in `e2e_tests`

#### Scenario: Patch at the importing module
- **WHEN** hop 1's module `pkg.cli` does `from pkg.core import run`, and a test uses `with mock.patch("pkg.cli.run"):` around `main(["render"])`
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: monkeypatch.setattr with module object
- **WHEN** a test does `monkeypatch.setattr(core, "run", fake)`, where `core` resolves to `pkg.core`, and then invokes the entry
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: Patching another symbol does not move the test
- **WHEN** a test invokes the entry and patches only `pkg.core.log`, which is not hop 2
- **THEN** the test appears in `e2e_tests`, and not in `e2e_tests_mocking_hop_2`

#### Scenario: Single-hop path
- **WHEN** a path has exactly one hop, and a test invokes its entry with patches applied
- **THEN** the test appears in `e2e_tests`, and `e2e_tests_mocking_hop_2` is empty

### Requirement: Hop-2 patches applied by fixtures are recognised
A patch of hop 2 SHALL also count when it is applied inside a pytest fixture that either of these makes available to the test:
- the test requests it by parameter name;
- it is `autouse=True`.

The fixture can be defined in the test's module or in a `conftest.py` in the test file's directory or any ancestor up to the snapshot root.

#### Scenario: Requested conftest fixture patches hop 2
- **WHEN** `tests/conftest.py` defines a fixture `fake_run` that does `monkeypatch.setattr("pkg.core.run", ...)`, and a test with parameter `fake_run` invokes the entry
- **THEN** the test appears in `e2e_tests_mocking_hop_2`

#### Scenario: Unrequested non-autouse fixture is ignored
- **WHEN** the same fixture exists, but the test does not request it and it is not autouse
- **THEN** the test appears in `e2e_tests`

### Requirement: Listing applies to cut paths
Paths that the trace gate cut (with `stops_at` and `break_reason` set) SHALL still receive e2e test listing. Hop 2 SHALL be determined from the retained hops only.

#### Scenario: Cut after hop 1
- **WHEN** a path was cut so that only hop 1 remains, and a test invokes its entry while patching what the model originally proposed as hop 2
- **THEN** the test appears in `e2e_tests`, and `e2e_tests_mocking_hop_2` is empty

### Requirement: Listed test record shape
Each listed test SHALL record:
- `node_id`: the pytest-style id, relative to the snapshot root;
- `file`: relative to the snapshot root;
- `line`: 1-based line of the test's `def`;
- `match`: one of `direct_call`, `cli_argv`, `cli_runner`, `subprocess` or `runpy`;
- `invocation_line`: 1-based line of the matching call.

Entries in `e2e_tests_mocking_hop_2` SHALL additionally record `patch_line`: the 1-based line of the hop-2 patch, which may be in a fixture file.

#### Scenario: Record fields are populated
- **WHEN** a test at line 10 calls `main(["open"])` at line 12, under a `@mock.patch("pkg.core.run")` decorator at line 9
- **THEN** its record in `e2e_tests_mocking_hop_2` has `line` 10, `invocation_line` 12, `match` `cli_argv` and `patch_line` 9

### Requirement: Deterministic ordering and deduplication
Each list SHALL contain each `node_id` at most once. When a test matches more than once, the record SHALL use the earliest `invocation_line`, and within that line the first applicable `match` kind in the order `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`. Lists SHALL be sorted by `file`, then `line`, then `node_id`. One test MAY appear on several paths when it invokes several entries.

#### Scenario: Repeated invocation is listed once
- **WHEN** a test calls `main(["open"])` on line 5 and on line 8
- **THEN** the `open` path lists that test once, with `invocation_line` 5

#### Scenario: Stable order across runs
- **WHEN** listing runs twice on the same snapshot, with the filesystem enumeration order reversed between runs
- **THEN** both runs produce byte-identical `e2e_tests` and `e2e_tests_mocking_hop_2` lists for every path

#### Scenario: One test on two paths
- **WHEN** a test invokes both `main(["open"])` and `main(["render"])`
- **THEN** it appears on both the `open` and `render` paths

### Requirement: Empty lists are explicit
Every path in the output SHALL include both `e2e_tests` and `e2e_tests_mocking_hop_2`. Either SHALL be an empty array when nothing matches, including when the snapshot has no test files.

#### Scenario: Repository without tests
- **WHEN** a fixture repository contains no files matching the test file patterns
- **THEN** every path has `e2e_tests: []` and `e2e_tests_mocking_hop_2: []`

### Requirement: Unreadable test files are skipped with a reason
A test file that cannot be decoded as UTF-8 or raises `SyntaxError` under `ast.parse` SHALL be skipped. The skip SHALL be recorded in the result's test-listing skips, with the file path relative to the snapshot root and a reason of `decode_error` or `syntax_error`. Listing SHALL continue for all other files.

#### Scenario: Syntax error in one file
- **WHEN** `tests/test_broken.py` has a syntax error and `tests/test_ok.py` invokes an entry
- **THEN** `tests/test_ok.py` is listed for that entry, and the skips contain `{file: "tests/test_broken.py", reason: "syntax_error"}`

#### Scenario: Non-UTF-8 file
- **WHEN** a test file contains invalid UTF-8 bytes
- **THEN** it is recorded with reason `decode_error`, and no exception propagates from `map_paths(...)`