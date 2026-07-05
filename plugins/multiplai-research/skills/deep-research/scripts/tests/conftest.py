"""Pytest configuration — add scripts dir to sys.path so `research_pipeline` imports work.

This avoids needing a pyproject.toml or `pip install -e .` while keeping the
package importable during test runs. The package is invoked in production via
`python -m research_pipeline` from the scripts directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
