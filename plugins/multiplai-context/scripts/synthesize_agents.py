"""Render the fleet view: ``AGENTS.md``.

Ten concurrent agents in ten tmux tabs is only workable if walking away is
cheap, and walking away is expensive when the state of each tab lives in your
head. Everything needed to answer "what is each one doing, which need me, and
are two of them editing the same file" is already on disk in two stores that
nobody can read: the session registry and the per-session checkpoints.

This script joins them. **No LLM call, no network** — it is pure aggregation,
cheap enough to run from a hook, and safe to run at any time because it has no
inputs of its own. See :mod:`lib.fleet` for the join and the rendering.

Usage::

    uv run --all-packages --project <repo-root> synthesize_agents.py            # write it
    uv run --all-packages --project <repo-root> synthesize_agents.py --stdout   # preview
    uv run --all-packages --project <repo-root> synthesize_agents.py --data-dir DIR

The output lands in ``<data_dir>/`` — **not** ``.multiplai/now/``, which the
multiplai hub globs into one NowCard per filename, where an ``AGENTS.md``
would surface as a bogus project named "AGENTS". (``fleet.txt``, the retired
status-bar line, is deleted here if a pre-digest release left one behind.)

The output is a **cache**. ``sessions/`` and ``checkpoints/`` are the sole
source of truth; delete the file and the next run reconstructs it
byte-for-byte apart from the generation stamp. Nothing may ever write into
``AGENTS.md`` as primary state — that would make it a fourth store, free to
disagree silently with the other three.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging
from lib.fleet import collect, render_agents_md, write_fleet_view

logger = setup_logging("synthesize_agents")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render the fleet view (AGENTS.md) from the session registry "
            "and per-session checkpoints. No LLM call."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Runtime data directory holding sessions/ and checkpoints/. "
            "Defaults to the resolved plugin data dir."
        ),
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print AGENTS.md to stdout instead of writing it.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the written path after writing. Silent otherwise.",
    )
    args = parser.parse_args()

    data_dir = (
        args.data_dir.expanduser().resolve() if args.data_dir else get_paths().data_dir()
    )
    now = datetime.now(timezone.utc)

    if args.stdout:
        print(render_agents_md(collect(data_dir, now), now), end="")
        return 0

    try:
        agents_path = write_fleet_view(data_dir, now)
    except OSError as exc:
        # A read-only or missing data dir is a degraded environment, not a
        # crash: this runs from hooks that must never break a session.
        logger.warning("Could not write the fleet view under %s: %s", data_dir, exc)
        if args.verbose:
            print(f"[agents] not written: {exc}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"[agents] wrote {agents_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
