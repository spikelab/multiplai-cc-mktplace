"""Real runner failures and command-relative pytest evidence."""
import os
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from build_pipeline.gates import red_gate, run_test_suite


@pytest.mark.parametrize('wrapper', ['cd api && {command}', 'cd api&&{command}', 'sh -c {quoted}'])
@pytest.mark.parametrize('parent_config', [False, True])
def test_changed_directory_pytest_red(tmp_path, monkeypatch, wrapper, parent_config):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'api/tests').mkdir(parents=True)
    (tmp_path / 'api/tests/test_new.py').write_text('def test_new():\n    assert 1 == 2\n')
    if parent_config:
        (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    python = shlex.quote(sys.executable)
    simple = f'{python} -m pytest -q'
    command = wrapper.format(command=simple, quoted=shlex.quote(f'cd api && {simple}'))
    code, output = run_test_suite(command, tmp_path)
    assert code == 1, output
    gate = red_gate(output, code, runner=None, expected_tests=['api/tests/test_new.py'],
                    test_command=command, project_dir=tmp_path)
    assert gate.passed, gate.reason
    wrong = red_gate(output, code, runner=None, expected_tests=['other/tests/test_new.py'],
                     test_command=command, project_dir=tmp_path)
    assert wrong.passed and wrong.metadata['classification'] == 'unverified'


def test_real_unittest_failure_is_unverified_red(tmp_path, monkeypatch):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'test_new.py').write_text(
        'import unittest\nclass TestNew(unittest.TestCase):\n'
        '    def test_new(self):\n        self.assertEqual(1, 2)\n')
    code, output = run_test_suite(f'{shlex.quote(sys.executable)} -m unittest discover', tmp_path)
    assert code == 1, output
    gate = red_gate(output, code, runner=None)
    assert gate.passed and gate.metadata['classification'] == 'unverified', gate.reason


def test_real_vitest_failure_is_unverified_red(tmp_path, monkeypatch):
    binary = os.environ.get('BUILDME_VITEST_BIN') or shutil.which('vitest')
    if not binary:
        pytest.skip('Set BUILDME_VITEST_BIN to run the real Vitest regression')
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'new.test.js').write_text('test("new behavior", () => { expect(1).toBe(2) })\n')
    code, output = run_test_suite(
        f'{shlex.quote(binary)} run --globals --maxWorkers=1 --minWorkers=1', tmp_path)
    assert code == 1 and 'AssertionError' in output, output
    gate = red_gate(output, code, runner='node')
    assert gate.passed and gate.metadata['classification'] == 'unverified', gate.reason


@pytest.mark.parametrize('runner, output', [
    ('cargo', 'test tests::answer ... FAILED\nfailures:\n    tests::answer\ntest result: FAILED. 0 passed; 1 failed'),
    ('go', '--- FAIL: TestAnswer (0.00s)\n    answer_test.go:8: got 1, want 2\nFAIL\nFAIL example.test 0.002s'),
    ('swift', "Test Case '-[Tests testAnswer]' failed (0.001 seconds).\nExecuted 1 test, with 1 failure"),
])
def test_unparsed_runners_remain_explicitly_unverified(runner, output):
    gate = red_gate(output, 1, runner=runner)
    assert gate.passed and gate.metadata['classification'] == 'unverified'


@pytest.mark.parametrize('existing', ['none', 'package', 'module'])
@pytest.mark.parametrize('module_name', ['new_pkg.feature', 'new_pkg.middle.feature'])
def test_prompt_recipe_reaches_red_then_green_for_new_python_apis(tmp_path, monkeypatch, existing, module_name):
    import re
    from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    example = re.search(r"```python\n(.*?)```", TEST_WRITER_PROMPT, re.S)
    assert example is not None, 'test-writing advice needs an executable import recipe'
    (tmp_path / 'test_api.py').write_text(example.group(1).replace('new_pkg.feature', module_name))
    package = tmp_path / 'new_pkg'
    if existing != 'none':
        package.mkdir()
        (package / '__init__.py').write_text('')
    package = tmp_path.joinpath(*module_name.split('.')[:-1])
    if existing == 'module':
        package.mkdir(parents=True, exist_ok=True)
        (package / 'feature.py').write_text('')
    command = f'{shlex.quote(sys.executable)} -m pytest -q'
    code, output = run_test_suite(command, tmp_path)
    gate = red_gate(output, code, expected_tests=['test_api.py'])
    assert gate.passed, output
    package.mkdir(parents=True, exist_ok=True)
    (package / '__init__.py').write_text('')
    (package / 'feature.py').write_text('def answer():\n    return 42\n')
    code, output = run_test_suite(command, tmp_path)
    assert code == 0, output

@pytest.mark.parametrize('wrapper', ['script', 'make'])
def test_opaque_cd_wrapper_keeps_real_red_unverified(tmp_path, monkeypatch, wrapper):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'api/tests').mkdir(parents=True)
    (tmp_path / 'api/tests/test_new.py').write_text('def test_new():\n    assert False\n')
    simple = f'{shlex.quote(sys.executable)} -m pytest -q'
    if wrapper == 'script':
        script = tmp_path / 'test.sh'
        script.write_text(f'#!/bin/sh\ncd api && {simple}\n')
        script.chmod(0o755)
        command = './test.sh'
    else:
        (tmp_path / 'Makefile').write_text(f'test:\n\tcd api && {simple}\n')
        command = 'make test'
    code, output = run_test_suite(command, tmp_path)
    assert code and 'FAILED tests/test_new.py' in output
    gate = red_gate(output, code, runner=None, expected_tests=['api/tests/test_new.py'],
                    test_command=command, project_dir=tmp_path)
    assert gate.passed and gate.metadata['classification'] == 'unverified'

@pytest.mark.parametrize('word', ['load#x', 'load|x', 'load;x', 'load&&x'])
def test_test_command_preserves_literal_argument_punctuation(tmp_path, monkeypatch, word):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    command = f'{shlex.quote(sys.executable)} -c "import sys; print(sys.argv[1:])" {word}'
    code, output = run_test_suite(command, tmp_path)
    assert code == 0 and repr([word]) in output

@pytest.mark.parametrize('runner', ['cargo', 'go', 'swift', 'vitest', 'node', None])
def test_unparsed_runner_nonzero_is_unverified_even_for_setup_diagnostics(runner):
    gate = red_gate('SyntaxError: invalid syntax\ncommand not found: compiler', 2, runner=runner)
    assert gate.passed and gate.metadata['classification'] == 'unverified'

def test_invalid_output_declaration_is_unavailable_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'specs').mkdir()
    (tmp_path / 'specs/config.yaml').write_text('test_outputs: ["out/*"]\n')
    code, output = run_test_suite(f'{sys.executable} -c "print(42)"', tmp_path)
    assert code is None and 'literal' in output
