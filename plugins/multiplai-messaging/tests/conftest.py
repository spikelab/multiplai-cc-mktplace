"""Shared fixtures for the multiplai-messaging plugin tests.

The two skills are standalone PEP 723 scripts (run via ``uv run``), so they are
loaded here by file path rather than as an installed package. ``multiplai-core``
must be importable in the test environment (install the local checkout, e.g.
``uv pip install -e ../multiplai-core``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_GMAIL = _PLUGIN_ROOT / "skills" / "gmail" / "scripts" / "gmail.py"
_SLACK = _PLUGIN_ROOT / "skills" / "slack" / "scripts" / "slack_client.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def gmail():
    return _load("gmail", _GMAIL)


@pytest.fixture(scope="session")
def slack():
    return _load("slack_client", _SLACK)
