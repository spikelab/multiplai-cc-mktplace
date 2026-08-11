"""The one way a hook entry point reads its stdin payload.

Every hook receives a JSON object on stdin from Claude Code. Before this
helper existed the parse was written three different ways across the seven
entry points, and one of them (``sys.stdin.isatty()`` outside any try)
raised ``ValueError`` on a closed stdin — aborting the whole hook before it
did anything. A hook must never die on its input: garbage, EOF, a closed
descriptor, or a non-object payload all mean the same thing, "no usable
input", and the hook decides for itself what to do with an empty dict.

Deliberately no ``isatty`` probe: it answers "is this interactive?", which
is not the question, and it is itself a call that can raise.
"""

from __future__ import annotations

import json
import sys


def read_hook_input() -> dict:
    """Parse the hook's stdin JSON payload; any failure yields ``{}``."""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        # OSError: unreadable descriptor. ValueError: stdin already closed
        # ("I/O operation on closed file") — seen when the harness reaps a
        # hook's pipes early.
        return {}
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
