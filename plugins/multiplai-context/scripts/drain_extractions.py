# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.12.0"]
# ///
"""Standalone drain for deferred extraction markers.

``session_end.py`` and ``pre_compact.py`` are kill-within-seconds hooks, so
they only drop a marker into ``<data_dir>/pending_extractions/``. Until now the
sole thing that picked those markers up was the *next* ``SessionStart`` in any
project — which meant closing your last tab on a Friday evening produced
Friday's diary entry on Monday morning.

This script is the other consumer. It drains the same queue, through the same
``lib.extraction_drain`` code, from outside a Claude Code session — so the
container launcher can run it on the host immediately after the container
exits, at exactly the moment a marker was just written.

Usage::

    uv run --no-project drain_extractions.py --data-dir ~/knowhere/.multiplai/data
    uv run --no-project drain_extractions.py --wait --verbose   # by hand

``--data-dir`` is optional; without it the standard path cascade applies
(``CLAUDE_PLUGIN_OPTION_data_dir`` → ``<workspace>/.multiplai/data`` → …).

Environment the caller must supply
----------------------------------

The drain makes no LLM call itself, but the ``extract_learnings.py`` children
it launches inherit this process's environment, and they do. Audited against
what the extraction path actually reads:

``CLAUDE_PLUGIN_OPTION_workspace_dir`` (or ``WORKSPACE``)
    **Required off-host-default.** Everything extraction writes — the diary,
    learnings, ``now/``, the registry — is resolved from this by
    ``multiplai_core.paths``. Unset, it all silently lands in
    ``~/.multiplai/`` instead of the workspace: not a crash, just the work
    appearing nowhere anyone looks. Passing ``--data-dir`` alone is *not*
    enough — it fixes the queue location, not the diary's.

``CLAUDE_CONFIG_DIR``
    Points the Agent SDK's bundled ``claude`` CLI at the OAuth credentials
    (``$CLAUDE_CONFIG_DIR/.credentials.json``). The SDK spawns the CLI with an
    inherited environment, so exporting it here is what makes host-side auth
    work at all.

Deliberately left unset
-----------------------

``CLAUDE_PLUGIN_OPTION_anthropic_api_key``
    Its *absence* is the point: ``create_client()`` returns ``AgentSDKClient``
    and the existing OAuth token is used, rather than billing a separate API
    key. It is only ever consulted as a fallback when the SDK is unimportable.

``MULTIPLAI_SDK_CALL_TIMEOUT_S``
    Left at the 600 s default — the same value extraction runs with inside the
    container, so host runs behave identically.

Catalog, router, and qmd ``CLAUDE_PLUGIN_OPTION_*`` knobs
    Not read anywhere on the extraction path (``extract_learnings.py`` →
    ``lib/extraction.py`` → ``multiplai_core``); the diary/learnings write is
    driven entirely by the path cascade above.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging, log_event
from lib.extraction_drain import pending_count, process_deferred_extractions

logger = setup_logging("drain_extractions")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Drain deferred extraction markers left by SessionEnd/PreCompact "
            "hooks, launching extract_learnings.py for each."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Runtime data directory holding pending_extractions/. "
            "Defaults to the resolved plugin data dir."
        ),
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help=(
            "Block until every launched extraction finishes and let its "
            "stderr through. For running by hand; never used by the launcher."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a one-line summary to stdout. Silent otherwise.",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser().resolve() if args.data_dir else get_paths().data_dir()
    scripts_dir = Path(__file__).parent
    extract_script = scripts_dir / "extract_learnings.py"

    if not extract_script.exists():
        logger.error("extract_learnings.py not found beside %s", __file__)
        if args.verbose:
            print(f"[drain] extract_learnings.py missing in {scripts_dir}", file=sys.stderr)
        return 1

    # Run the pass even with an empty queue: recover_stale_processing (inside
    # process_deferred_extractions) requeues markers orphaned by a crashed
    # child, which is how those eventually get retried.
    logger.info(
        "Draining %s (%d marker(s) pending)", data_dir, pending_count(data_dir)
    )
    processed = process_deferred_extractions(data_dir, extract_script, wait=args.wait)

    if processed:
        logger.info("Drained %d deferred extraction(s) from %s", processed, data_dir)
        log_event(
            "extract", "launch",
            f"host drain launched {processed} deferred extraction(s)",
            count=processed,
        )
    if args.verbose:
        print(f"[drain] {processed} extraction(s) launched from {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
