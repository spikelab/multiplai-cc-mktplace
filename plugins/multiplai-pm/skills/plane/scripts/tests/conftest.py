"""Shared constants and helpers for the plane test suite.

Every file here needs the same preamble: plane.py importable from the parent
directory, allowlist fixture data, a guard() shorthand, and an argparse-shaped
namespace. They lived as five diverging copies before this file. Import with
`from conftest import ...` — pytest puts this directory on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plane  # noqa: E402

# One or two allowed projects and one blocked. Most files guard against
# ALLOWED; use ALLOWED_TWO where a second allowed project matters.
OK = "1fa4d2f6-e016-428a-aca7-5ebb1c8bca4f"
OK2 = "6155159a-ff3b-447a-83db-6104be74ffb4"
BAD = "b996f98b-0bdc-4bea-9ec0-92da5268f054"

ALLOWED = {OK: "Mine"}
ALLOWED_TWO = {OK: "Mine", OK2: "Also mine"}

CFG = {
    "base": "https://api.plane.so",
    "workspace": "ws",
    "token": "t",
    "allowed": ALLOWED,
}


def guard(method, path, allowed=None):
    plane._guard(method, path, ALLOWED if allowed is None else allowed)


class Args:
    """argparse.Namespace stand-in with the one flag every command reads."""

    def __init__(self, **kw):
        self.json = False
        self.__dict__.update(kw)
