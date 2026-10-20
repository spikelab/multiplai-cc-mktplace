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
@pytest.mark.parametrize('directory_name', ['api', 'api space', 'api&&docs'])
def test_changed_directory_pytest_red(tmp_path, monkeypatch, wrapper, parent_config, directory_name):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / directory_name / 'tests').mkdir(parents=True)
    (tmp_path / directory_name / 'tests/test_new.py').write_text('def test_new():\n    assert 1 == 2\n')
    if parent_config:
        (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    python = shlex.quote(sys.executable)
    simple = f'{python} -m pytest -q'
    command = wrapper.replace('api', shlex.quote(directory_name)).format(
        command=simple, quoted=shlex.quote(f'cd {shlex.quote(directory_name)} && {simple}'))
    code, output = run_test_suite(command, tmp_path)
    assert code == 1, output
    gate = red_gate(output, code, runner=None, expected_tests=[f'{directory_name}/tests/test_new.py'],
                    test_command=command, project_dir=tmp_path)
    assert gate.passed, gate.reason
    wrong = red_gate(output, code, runner=None, expected_tests=['other/tests/test_new.py'],
                     test_command=command, project_dir=tmp_path)
    assert not wrong.passed and wrong.action == 'fix_tests'


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

@pytest.mark.parametrize('prefix', ['', 'cd api && ', 'sh -c '])
@pytest.mark.parametrize('case', ['empty', 'usage', 'unrelated'])
def test_direct_pytest_rejects_runs_without_changed_test_failures(tmp_path, monkeypatch, prefix, case):
    from build_pipeline.config import BuildConfig
    from build_pipeline.models import BlockInfo
    from build_pipeline.tdd_engine import _block_test_command
    from build_pipeline.narrow_tests import runner_of
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    directory = tmp_path / 'api' if prefix else tmp_path
    directory.mkdir(exist_ok=True)
    (directory / 'test_new.py').write_text(
        '# no tests\n' if case == 'empty' else 'def test_new():\n    assert True\n')
    if case == 'unrelated':
        (directory / 'test_old.py').write_text('def test_old():\n    assert False\n')
    command = f'{shlex.quote(sys.executable)} -m pytest -q'
    if case == 'usage':
        command += ' ' + shlex.quote('--no-such-option=load|x')
    if prefix == 'sh -c ':
        command = prefix + shlex.quote('cd api && ' + command)
    else:
        command = prefix + command
    config = BuildConfig(project_dir=tmp_path, test_command=command)
    block = BlockInfo(number=1, name='New behavior', description='new behavior')
    command = _block_test_command(config, block)
    code, output = run_test_suite(command, tmp_path)
    assert code == {'empty': 5, 'usage': 4, 'unrelated': 1}[case], output
    expected = ['api/test_new.py'] if prefix else ['test_new.py']
    gate = red_gate(output, code, runner=runner_of(command, tmp_path), expected_tests=expected,
                    test_command=command, project_dir=tmp_path)
    assert not gate.passed and gate.action == 'fix_tests', gate.reason


@pytest.mark.parametrize('flags', ['-rs', '-rN', '--no-summary', '-p no:terminal'])
@pytest.mark.parametrize('in_addopts', [False, True], ids=['argv', 'addopts'])
def test_direct_pytest_without_failure_summary_is_unverified(tmp_path, monkeypatch, flags, in_addopts):
    from build_pipeline.config import BuildConfig
    from build_pipeline.models import BlockInfo
    from build_pipeline.narrow_tests import runner_of
    from build_pipeline.tdd_engine import _block_test_command

    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'test_new.py').write_text('def test_new():\n    assert 1 == 2\n')
    command = f'{shlex.quote(sys.executable)} -m pytest'
    if in_addopts:
        (tmp_path / 'pytest.ini').write_text(f'[pytest]\naddopts = {flags}\n')
    else:
        command += f' {flags}'
    config = BuildConfig(project_dir=tmp_path, test_command=command)
    block = BlockInfo(number=1, name='New behavior', description='new behavior')
    command = _block_test_command(config, block)
    code, output = run_test_suite(command, tmp_path)
    assert code == 1, output
    assert not any(line.startswith('FAILED ') for line in output.splitlines()), output
    gate = red_gate(output, code, runner=runner_of(command, tmp_path), expected_tests=['test_new.py'],
                    test_command=command, project_dir=tmp_path)
    assert gate.passed, gate.reason
    assert gate.metadata['classification'] == 'unverified'
