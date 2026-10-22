"""Pytest configuration — add scripts dir to sys.path so `research_pipeline` imports work.

This avoids needing a pyproject.toml or `pip install -e .` while keeping the
package importable during test runs. The package is invoked in production via
`python -m research_pipeline` from the scripts directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# multiplai-core 0.21: a step with no configured model or effort runs on
# MULTIPLAI_MODEL / MULTIPLAI_EFFORT, else the Claude Code session's own
# (multiplai_core.session). Run inside a session, the developer's values would
# set a test's model, so clear them and hide the developer's multiplai.conf
# from that lookup. A test that wants one sets it with monkeypatch.
_SESSION_DEFAULT_VARS = (
    "MULTIPLAI_MODEL",
    "MULTIPLAI_EFFORT",
    "MULTIPLAI_SESSION_MODEL",
    "MULTIPLAI_SESSION_EFFORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
)


@pytest.fixture(autouse=True)
def _no_session_defaults(monkeypatch):
    for key in _SESSION_DEFAULT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "multiplai_core.session.load_multiplai_conf", lambda: {"_sections": {}}
    )
