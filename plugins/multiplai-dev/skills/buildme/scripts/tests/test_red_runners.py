"""Real runner failures and command-relative pytest evidence."""
import os
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from build_pipeline.gates import red_gate, run_test_suite


@pytest.mark.parametrize('wrapper', ['cd api && {command}', 'sh -c {quoted}'])
def test_changed_directory_pytest_red(tmp_path, monkeypatch, wrapper):
    monkeypatch.setenv('BUILDME_TRUST_REPO', '1')
    (tmp_path / 'api/tests').mkdir(parents=True)
    (tmp_path / 'api/tests/test_new.py').write_text('def test_new():\n    assert 1 == 2\n')
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
    assert not wrong.passed


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
