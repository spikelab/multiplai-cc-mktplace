"""RED gate verdicts, recorded from the gate before it read them from a table.

`test_red_verdicts_match_base` gives `red_gate` every distinct input shape that
`test_red_runners.py` and `test_gates.py` send it, plus one case per row of the
decision table, and asserts the verdict the gate returned when these cases were
recorded: `passed`, the whole `metadata` (so `classification` and which keys are
present), `action`, and the reason with the output's tail written as `<tail>`.
A case is added here when a row is added; an existing case is never edited to
fit new code.
"""
from __future__ import annotations

from typing import NamedTuple

import pytest

from build_pipeline.gates import red_gate


class Case(NamedTuple):
    id: str
    output: str
    exit_code: int | None
    runner: str | None = "pytest"
    expected_tests: list[str] | None = None
    test_command: str = ""
    # True: the gate is given a project directory (a fresh tmp_path).
    project: bool = False


# Real runner output, captured from pytest 9.1, unittest and make with the
# temporary directory written as /project.
PYTEST_ASSERT = """\
F                                                                        [100%]
=================================== FAILURES ===================================
________________________________ test_behavior _________________________________

    def test_behavior():
>       assert 1 == 2
E       assert 1 == 2

test_behavior.py:2: AssertionError
=========================== short test summary info ============================
FAILED test_behavior.py::test_behavior - assert 1 == 2
1 failed in 0.01s
"""

PYTEST_MISSING_FIXTURE = """\
E                                                                        [100%]
==================================== ERRORS ====================================
_______________________ ERROR at setup of test_behavior ________________________
file /project/test_behavior.py, line 1
  def test_behavior(missing_fixture):
E       fixture 'missing_fixture' not found
>       available fixtures: capfd, capsys, monkeypatch, tmp_path
>       use 'pytest --fixtures [testpath]' for help on them.

/project/test_behavior.py:1
=========================== short test summary info ============================
ERROR test_behavior.py::test_behavior
1 error in 0.00s
"""

PYTEST_MISSING_INPUT = """\
F                                                                        [100%]
=================================== FAILURES ===================================
________________________________ test_behavior _________________________________

    def test_behavior():
>       open('missing-input')
E       FileNotFoundError: [Errno 2] No such file or directory: 'missing-input'

test_behavior.py:2: FileNotFoundError
=========================== short test summary info ============================
FAILED test_behavior.py::test_behavior - FileNotFoundError: [Errno 2] No such...
1 failed in 0.01s
"""

PYTEST_IMPORT_MISSING = """\

==================================== ERRORS ====================================
______________________ ERROR collecting test_behavior.py _______________________
ImportError while importing test module '/project/test_behavior.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.12/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
test_behavior.py:1: in <module>
    import missing_buildme_test_module
E   ModuleNotFoundError: No module named 'missing_buildme_test_module'
=========================== short test summary info ============================
ERROR test_behavior.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.03s
"""

PYTEST_PATCHED_EXCEPTION = """\
F                                                                        [100%]
=================================== FAILURES ===================================
________________________________ test_behavior _________________________________

    def test_behavior():
        with patch('builtins.open', side_effect=FileNotFoundError):
>           assert 1 == 2
E           assert 1 == 2

test_behavior.py:5: AssertionError
=========================== short test summary info ============================
FAILED test_behavior.py::test_behavior - assert 1 == 2
1 failed in 0.01s
"""

PYTEST_NEW_MODULE = """\
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________________ test_api ___________________________________

    def test_api():
>       assert importlib.util.find_spec('new_buildme_feature') is not None
E       AssertionError: assert None is not None
E        +  where None = <function find_spec at 0xffff81456340>('new_buildme_feature')

test_new.py:4: AssertionError
=========================== short test summary info ============================
FAILED test_new.py::test_api - AssertionError: assert None is not None
1 failed in 0.01s
"""

# `-rN`: no short test summary, so no FAILED line.
PYTEST_NO_SUMMARY = """\
============================= test session starts ==============================
collected 1 item

test_new.py F                                                            [100%]

=================================== FAILURES ===================================
___________________________________ test_new ___________________________________

    def test_new():
>       assert 1 == 2
E       assert 1 == 2

test_new.py:2: AssertionError
============================== 1 failed in 0.01s ===============================
"""

PYTEST_EMPTY = "\nno tests ran in 0.00s\n"

PYTEST_USAGE = """\
ERROR: usage: python -m pytest [options] [file_or_dir] [file_or_dir] [...]
python -m pytest: error: unrecognized arguments: --no-such-option=load|x
  inifile: None
  rootdir: /project

"""

PYTEST_UNRELATED = """\
.F                                                                       [100%]
=================================== FAILURES ===================================
___________________________________ test_old ___________________________________

    def test_old():
>       assert False
E       assert False

test_old.py:2: AssertionError
=========================== short test summary info ============================
FAILED test_old.py::test_old - assert False
1 failed, 1 passed in 0.01s
"""

# Run from api/: the paths are relative to it.
PYTEST_IN_API = """\
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________________ test_new ___________________________________

    def test_new():
>       assert 1 == 2
E       assert 1 == 2

tests/test_new.py:2: AssertionError
=========================== short test summary info ============================
FAILED tests/test_new.py::test_new - assert 1 == 2
1 failed in 0.01s
"""

UNITTEST_FAILURE = """\
F
======================================================================
FAIL: test_new (test_new.TestNew.test_new)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/project/test_new.py", line 4, in test_new
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (failures=1)
"""

VITEST_FAILURE = """\
 FAIL  new.test.js > new behavior
AssertionError: expected 1 to be 2 // Object.is equality
 Test Files  1 failed (1)
      Tests  1 failed (1)
"""

MAKE_MISSING_TARGET = "make: *** No rule to make target 'test'.  Stop.\n"

JEST_FAILED = (
    "FAIL src/auth.test.js\n"
    "  ● login › returns a token\n"
    "Tests: 1 failed, 2 passed, 3 total\n"
)

JEST_SUITE_FAILED_TO_RUN = (
    "FAIL tests/a.test.js\n  ● Test suite failed to run\n\n"
    "    Cannot find module 'not-installed' from 'tests/a.test.js'\n\n"
    "Test Suites: 1 failed, 1 total\nTests: 0 total"
)

PY = "python -m pytest -q"
SETUP_DIAGNOSTICS = "SyntaxError: invalid syntax\ncommand not found: compiler"

CASES = [
    # --- test_gates.py: TestRedGate ---
    Case("assertion_failed_line", "FAILED tests/test_auth.py::test_login - AssertionError: expected token", 1),
    Case("not_implemented", "FAILED tests/test_auth.py::test_login - NotImplementedError", 1),
    Case("missing_attribute",
         "FAILED tests/test_auth.py::test_login - AttributeError: module 'auth' has no attribute 'login'", 1),
    Case("suite_passed", "5 passed in 0.3s", 0),
    Case("collection_error", "ERROR collecting tests/test_auth.py\nSyntaxError: invalid syntax", 2),
    Case("syntax_error_e_line", "E   SyntaxError: invalid syntax (test_auth.py, line 12)", 2),
    Case("unrecognized_exit_1", "something exploded unrecognizably", 1),
    Case("not_run", "Repo not trusted", None),
    Case("terse_summary", "1 failed, 3 passed in 0.12s", 1),
    Case("jest_failed", JEST_FAILED, 1, runner="jest"),
    Case("zero_failed_summary", "0 failed, 5 passed", 1),
    Case("prose", "warning: flaky tests may fail intermittently", 1),
    # --- test_gates.py: module-level RED tests ---
    Case("missing_fixture_file_with_failed_line",
         "FileNotFoundError: missing-fixture.json\nFAILED tests/test_app.py::test_read\n1 failed", 1),
    Case("setup_error_with_assertion", "ERROR at setup of test_app\nAssertionError\n1 failed, 1 error", 1),
    Case("real_assert", PYTEST_ASSERT, 1, expected_tests=["test_behavior.py"]),
    Case("real_missing_fixture", PYTEST_MISSING_FIXTURE, 1, expected_tests=["test_behavior.py"]),
    Case("real_missing_input", PYTEST_MISSING_INPUT, 1, expected_tests=["test_behavior.py"]),
    Case("real_import_missing", PYTEST_IMPORT_MISSING, 2, expected_tests=["test_behavior.py"]),
    Case("collection_error_runner_none", PYTEST_IMPORT_MISSING, 2, runner=None),
    Case("collection_error_runner_jest", PYTEST_IMPORT_MISSING, 2, runner="jest"),
    Case("collection_error_runner_pytest", PYTEST_IMPORT_MISSING, 2, runner="pytest"),
    Case("patched_exception_in_source", PYTEST_PATCHED_EXCEPTION, 1, expected_tests=["test_behavior.py"]),
    Case("unknown_runner_command_not_found", "command not found: node", 127, runner=None),
    Case("unknown_runner_cannot_find_module", "Error: Cannot find module 'jest'", 1, runner=None),
    Case("new_module", PYTEST_NEW_MODULE, 1, expected_tests=["test_new.py"]),
    Case("jest_suite_failed_to_run", JEST_SUITE_FAILED_TO_RUN, 1, runner="jest"),
    # --- test_red_runners.py ---
    Case("cd_wrapper_inside", PYTEST_IN_API, 1, None, ["api/tests/test_new.py"], f"cd api && {PY}", True),
    Case("cd_wrapper_outside", PYTEST_IN_API, 1, None, ["other/tests/test_new.py"], f"cd api && {PY}", True),
    Case("cd_wrapper_no_spaces_inside", PYTEST_IN_API, 1, None, ["api/tests/test_new.py"], f"cd api&&{PY}", True),
    Case("cd_wrapper_quoted_dir_inside", PYTEST_IN_API, 1, None, ["api space/tests/test_new.py"],
         f"cd 'api space' && {PY}", True),
    Case("sh_c_wrapper_inside", PYTEST_IN_API, 1, None, ["api/tests/test_new.py"], f"sh -c 'cd api && {PY}'", True),
    Case("sh_c_wrapper_outside", PYTEST_IN_API, 1, None, ["other/tests/test_new.py"],
         f"sh -c 'cd api && {PY}'", True),
    Case("unittest_failure", UNITTEST_FAILURE, 1, runner=None),
    Case("vitest_failure", VITEST_FAILURE, 1, runner="node"),
    Case("cargo_failure",
         "test tests::answer ... FAILED\nfailures:\n    tests::answer\ntest result: FAILED. 0 passed; 1 failed",
         1, runner="cargo"),
    Case("go_failure", "--- FAIL: TestAnswer (0.00s)\n    answer_test.go:8: got 1, want 2\nFAIL\nFAIL example.test 0.002s",
         1, runner="go"),
    Case("swift_failure", "Test Case '-[Tests testAnswer]' failed (0.001 seconds).\nExecuted 1 test, with 1 failure",
         1, runner="swift"),
    Case("opaque_script_wrapper", PYTEST_IN_API, 1, None, ["api/tests/test_new.py"], "./test.sh", True),
    Case("opaque_make_wrapper", PYTEST_IN_API + "make: *** [Makefile:2: test] Error 1\n", 2, None,
         ["api/tests/test_new.py"], "make test", True),
    *[Case(f"unparsed_runner_setup_diagnostics_{runner}", SETUP_DIAGNOSTICS, 2, runner=runner)
      for runner in ("cargo", "go", "swift", "vitest", "node", None)],
    Case("direct_empty", PYTEST_EMPTY, 5, "pytest", ["test_new.py"], PY, True),
    Case("direct_usage", PYTEST_USAGE, 4, "pytest", ["test_new.py"], PY + " '--no-such-option=load|x'", True),
    Case("direct_unrelated", PYTEST_UNRELATED, 1, "pytest", ["test_new.py"], PY, True),
    Case("cd_direct_empty", PYTEST_EMPTY, 5, None, ["api/test_new.py"], f"cd api && {PY}", True),
    Case("cd_direct_usage", PYTEST_USAGE, 4, None, ["api/test_new.py"],
         f"cd api && {PY} '--no-such-option=load|x'", True),
    Case("cd_direct_unrelated", PYTEST_UNRELATED, 1, None, ["api/test_new.py"], f"cd api && {PY}", True),
    Case("sh_c_direct_empty", PYTEST_EMPTY, 5, None, ["api/test_new.py"], f"sh -c 'cd api && {PY}'", True),
    Case("sh_c_direct_unrelated", PYTEST_UNRELATED, 1, None, ["api/test_new.py"], f"sh -c 'cd api && {PY}'", True),
    Case("direct_no_summary", PYTEST_NO_SUMMARY, 1, "pytest", ["test_new.py"], "python -m pytest -rN", True),
    # --- one per row of the decision table ---
    Case("row_not_run_wrapper", "command not found: pytest", None, None, ["tests/test_a.py"], f"cd api && {PY}", True),
    Case("row_exit_0_wrapper", "1 passed in 0.01s", 0, None, ["tests/test_a.py"], "make test", True),
    Case("row_broken_jest_by_output", "Test Suites: 1 failed, 1 total\nCannot find module 'x' from 'a.test.js'",
         1, runner=None),
    Case("row_direct_exit_1_no_failed_paths", "1 failed in 0.01s", 1, "pytest", ["test_new.py"], PY, True),
    Case("row_wrapper_no_failed_paths", "wrapper: errors during collection were reported", 1, runner=None),
    Case("row_direct_exit_2_with_failed_paths", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 2),
    Case("row_unresolvable_parent_dir", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 1,
         "pytest", ["tests/test_a.py"], f"cd ../elsewhere && {PY}", True),
    Case("row_unresolvable_variable_dir", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 1,
         None, ["tests/test_a.py"], f"cd $HOME && {PY}", True),
    Case("row_direct_no_changed_tests", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 1,
         "pytest", [], PY, True),
    Case("row_direct_outside_without_project", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 1,
         "pytest", ["tests/test_b.py"]),
    Case("row_wrapper_outside_without_project", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 1,
         None, ["tests/test_b.py"]),
    Case("row_jest_observed_node_runner", JEST_FAILED, 1, runner="node"),
    Case("row_make_missing_target", MAKE_MISSING_TARGET, 2, None, ["tests/test_a.py"], "make test", True),
    Case("row_behavioral_inside", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 1,
         "pytest", ["tests/test_a.py"], PY, True),
    Case("row_behavioral_wrapper_exit_2", "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", 2,
         runner=None),
    Case("row_pytest_wrong_reason", "FAILED tests/test_a.py::test_x - ValueError: boom\n1 failed", 1,
         "pytest", ["tests/test_a.py"]),
    Case("row_jest_without_failed_tests", "Test Suites: 1 failed, 1 total\nTests: 0 total", 1, runner="jest"),
    Case("row_jest_output_node_runner_without_failed_tests", "Test Suites: 1 failed, 1 total\nTests: 0 total",
         1, runner="node"),
]

# id -> (passed, metadata, action, reason with the output's last 1000
# characters written as <tail>), as the gate returned them when recorded.
VERDICTS: dict[str, tuple[bool, dict, str | None, str]] = {
    'assertion_failed_line': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'not_implemented': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'missing_attribute': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'suite_passed': (False, {'exit_code': 0}, 'rewrite_tests', 'RED gate failed: suite passed BEFORE implementation — the new tests exercise nothing unimplemented and prove nothing'),
    'collection_error': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 2): <tail>'),
    'syntax_error_e_line': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 2): <tail>'),
    'unrecognized_exit_1': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Pytest exited 1 without failure paths; RED is unverified'),
    'not_run': (False, {'exit_code': None}, 'fix_tests', 'RED gate could not run the suite: <tail>'),
    'terse_summary': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Pytest exited 1 without failure paths; RED is unverified'),
    'jest_failed': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'zero_failed_summary': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Pytest exited 1 without failure paths; RED is unverified'),
    'prose': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Pytest exited 1 without failure paths; RED is unverified'),
    'missing_fixture_file_with_failed_line': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 1): <tail>'),
    'setup_error_with_assertion': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 1): <tail>'),
    'real_assert': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'real_missing_fixture': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 1): <tail>'),
    'real_missing_input': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 1): <tail>'),
    'real_import_missing': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 2): <tail>'),
    'collection_error_runner_none': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 2): <tail>'),
    'collection_error_runner_jest': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 2): <tail>'),
    'collection_error_runner_pytest': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 2): <tail>'),
    'patched_exception_in_source': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'unknown_runner_command_not_found': (True, {'exit_code': 127, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'unknown_runner_cannot_find_module': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'new_module': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'jest_suite_failed_to_run': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 1): <tail>'),
    'cd_wrapper_inside': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'cd_wrapper_outside': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'cd_wrapper_no_spaces_inside': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'cd_wrapper_quoted_dir_inside': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'sh_c_wrapper_inside': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'sh_c_wrapper_outside': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'unittest_failure': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'vitest_failure': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'cargo_failure': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'go_failure': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'swift_failure': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'opaque_script_wrapper': (True, {'classification': 'unverified'}, None, "RED cannot attribute the wrapper's failure to the changed tests; accepted as unverified"),
    'opaque_make_wrapper': (True, {'classification': 'unverified'}, None, "RED cannot attribute the wrapper's failure to the changed tests; accepted as unverified"),
    'unparsed_runner_setup_diagnostics_cargo': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'unparsed_runner_setup_diagnostics_go': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'unparsed_runner_setup_diagnostics_swift': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'unparsed_runner_setup_diagnostics_vitest': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'unparsed_runner_setup_diagnostics_node': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'unparsed_runner_setup_diagnostics_None': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'direct_empty': (False, {'exit_code': 5}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 5): <tail>'),
    'direct_usage': (False, {'exit_code': 4}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 4): <tail>'),
    'direct_unrelated': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'cd_direct_empty': (False, {'exit_code': 5}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 5): <tail>'),
    'cd_direct_usage': (False, {'exit_code': 4}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 4): <tail>'),
    'cd_direct_unrelated': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'sh_c_direct_empty': (False, {'exit_code': 5}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 5): <tail>'),
    'sh_c_direct_unrelated': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'direct_no_summary': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Pytest exited 1 without failure paths; RED is unverified'),
    'row_not_run_wrapper': (False, {'exit_code': None}, 'fix_tests', 'RED gate could not run the suite: <tail>'),
    'row_exit_0_wrapper': (False, {'exit_code': 0}, 'rewrite_tests', 'RED gate failed: suite passed BEFORE implementation — the new tests exercise nothing unimplemented and prove nothing'),
    'row_broken_jest_by_output': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: tests error instead of failing (collection/syntax problem, exit 1): <tail>'),
    'row_direct_exit_1_no_failed_paths': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Pytest exited 1 without failure paths; RED is unverified'),
    'row_wrapper_no_failed_paths': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 1): <tail>'),
    'row_direct_exit_2_with_failed_paths': (False, {'exit_code': 2}, 'fix_tests', 'RED gate failed: pytest supplied no usable test failures (exit 2): <tail>'),
    'row_unresolvable_parent_dir': (True, {'classification': 'unverified'}, None, 'Pytest failure paths cannot be resolved; RED is unverified'),
    'row_unresolvable_variable_dir': (True, {'classification': 'unverified'}, None, 'Pytest failure paths cannot be resolved; RED is unverified'),
    'row_direct_no_changed_tests': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'row_direct_outside_without_project': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: pytest failures are outside the changed tests'),
    'row_wrapper_outside_without_project': (True, {'classification': 'unverified'}, None, "RED cannot attribute the wrapper's failure to the changed tests; accepted as unverified"),
    'row_jest_observed_node_runner': (True, {'exit_code': 1, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'row_make_missing_target': (True, {'exit_code': 2, 'classification': 'unverified'}, None, 'Nonzero test-command exit accepted as unverified RED for this runner'),
    'row_behavioral_inside': (True, {'exit_code': 1, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 1)'),
    'row_behavioral_wrapper_exit_2': (True, {'exit_code': 2, 'classification': 'behavioral'}, None, 'RED confirmed: tests fail for the right reason (exit 2)'),
    'row_pytest_wrong_reason': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: suite exits 1 but with no recognizable test-failure signature: <tail>'),
    'row_jest_without_failed_tests': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: suite exits 1 but with no recognizable test-failure signature: <tail>'),
    'row_jest_output_node_runner_without_failed_tests': (False, {'exit_code': 1}, 'fix_tests', 'RED gate failed: suite exits 1 but with no recognizable test-failure signature: <tail>'),
}


def _verdict(case: Case, project_dir):
    gate = red_gate(case.output, case.exit_code, runner=case.runner, expected_tests=case.expected_tests,
                    test_command=case.test_command, project_dir=project_dir if case.project else None)
    return gate.passed, gate.metadata, gate.action, gate.reason.replace(case.output[-1000:], "<tail>")


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_red_verdicts_match_base(case, tmp_path):
    assert _verdict(case, tmp_path) == VERDICTS[case.id]


def test_every_case_has_a_recorded_verdict():
    assert sorted(VERDICTS) == sorted(c.id for c in CASES)


def test_every_table_row_decides_at_least_one_case(tmp_path):
    from build_pipeline.gates import RED_TABLE, _red_facts, _red_row
    hit = {_red_row(_red_facts(c.output, c.exit_code, c.runner, c.expected_tests, c.test_command,
                               tmp_path if c.project else None))
           for c in CASES}
    assert [row for row in RED_TABLE if row not in hit] == []


@pytest.mark.parametrize("report, expected", [
    ("STATUS: DONE\nTEST_COUNT: 2\n"
     "RED_EXPECTED: tests/test_a.py::test_x, tests/test_a.py::test_y — login() does not exist yet\n"
     "SURPRISES: none\nSPEC_IMPACT: none\n",
     (["tests/test_a.py::test_x", "tests/test_a.py::test_y"], "login() does not exist yet")),
    ("STATUS: DONE\nTEST_COUNT: 2\nSURPRISES: none\nSPEC_IMPACT: none\n", None),
    ("- **RED_EXPECTED:** `tests/test_a.py::test_x[1,2]` - parse() returns None\n- **SURPRISES:** none\n",
     (["tests/test_a.py::test_x[1,2]"], "parse() returns None")),
    ("RED_EXPECTED: tests/test_a.py::test_x — add() is missing\n"
     "TEST CHANGE REQUIRED: the old test asserted the bug\n",
     (["tests/test_a.py::test_x"], "add() is missing")),
    ("RED_EXPECTED: <test ids that must fail, comma-separated> — <what is missing that makes them fail>\n", None),
], ids=["present", "missing", "bold_label", "followed_by_test_change", "template_echo"])
def test_parse_red_expected(report, expected):
    from build_pipeline.gates import parse_red_expected
    assert parse_red_expected(report) == expected


def test_red_expected_stops_the_surprises_slot():
    from build_pipeline.gates import parse_implementation_note
    note = parse_implementation_note(
        "SURPRISES: the design named it sum()\nRED_EXPECTED: t.py::test_a — no add()\nSPEC_IMPACT: clarify\n",
        block_number=1, block_name="Block1", role="test_writer")
    assert note is not None and note.surprises == "the design named it sum()"


@pytest.mark.parametrize("report, output, runner, expected", [
    ("RED_EXPECTED: tests/test_a.py::test_x — no x()",
     "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", "pytest", "named tests failed"),
    ("RED_EXPECTED: test_x — no x()",
     "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", "pytest", "named tests failed"),
    ("RED_EXPECTED: tests/test_a.py::test_x — no x()",
     "FAILED tests/test_a.py::test_x[1] - AssertionError\n1 failed", "pytest", "named tests failed"),
    ("RED_EXPECTED: tests/test_a.py::test_x, tests/test_a.py::test_z — no x()",
     "FAILED tests/test_a.py::test_x - AssertionError\n1 failed", "pytest",
     "named tests not among the failures: tests/test_a.py::test_z"),
    ("STATUS: DONE", "FAILED tests/test_a.py::test_x - AssertionError", "pytest", "not stated"),
    (None, "FAILED tests/test_a.py::test_x - AssertionError", "pytest", "not stated"),
    ("RED_EXPECTED: — no x()", "FAILED tests/test_a.py::test_x - AssertionError", "pytest", "not stated"),
    ("RED_EXPECTED: tests/test_a.py::test_x — no x()", "1 failed in 0.01s", "pytest",
     "cannot check for this runner"),
    ("RED_EXPECTED: answer_test.go — no Answer()", "--- FAIL: TestAnswer (0.00s)\nFAIL", "go",
     "cannot check for this runner"),
], ids=["exact", "bare_name", "parametrized", "not_among", "no_slot", "no_report", "no_ids",
        "suppressed_summary", "other_runner"])
def test_red_expected_note_result(report, output, runner, expected):
    from build_pipeline.gates import red_expected_note
    note = red_expected_note(report, output, 1, runner=runner)
    assert note.splitlines()[-1] == f"RED_EXPECTED result: {expected}"


def test_red_expected_note_keeps_the_slot_text():
    from build_pipeline.gates import red_expected_note
    note = red_expected_note("RED_EXPECTED: t.py::test_a, t.py::test_b — add() is missing",
                             "FAILED t.py::test_a - AssertionError\nFAILED t.py::test_b - AssertionError", 1,
                             runner="pytest")
    assert note == ("RED_EXPECTED: t.py::test_a, t.py::test_b — add() is missing\n"
                    "RED_EXPECTED result: named tests failed")
