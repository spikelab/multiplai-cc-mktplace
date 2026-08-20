"""Prompt prose borrowed from third-party sources, kept apart from our own.

Everything under this package is adapted from someone else's Apache-2.0 text
and carries an attribution header saying so. `SOURCES.json` records the repo,
the path, the blob SHA and the tree SHA each block was taken at, so a staleness
check can re-fetch and diff without reading the Python. `LICENSE` is the
upstream licence text, unchanged.

Nothing here is executed and nothing here grants a tool — these are strings
that `prompts/review.py` composes into `CODE_REVIEW_PROMPT`.
"""

from .reviewer_blocks import (
    CODE_REVIEWER_CONVENTIONS_BLOCK,
    SILENT_FAILURE_OUTPUT_BLOCK,
    SILENT_FAILURE_PROCESS_BLOCK,
    TEST_COVERAGE_BLOCK,
    TYPE_INVARIANT_BLOCK,
)

__all__ = [
    "CODE_REVIEWER_CONVENTIONS_BLOCK",
    "SILENT_FAILURE_OUTPUT_BLOCK",
    "SILENT_FAILURE_PROCESS_BLOCK",
    "TEST_COVERAGE_BLOCK",
    "TYPE_INVARIANT_BLOCK",
]
