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
from lib.extraction_drain import (
    pending_count,
    process_deferred_extractions,
    processing_count,
)
from lib.fleet import write_fleet_view

logger = setup_logging("drain_extractions")


def main(argv: list[str] | None = None) -> int:
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
            "stderr through; exit 1 if any child exits nonzero. For running "
            "by hand; never used by the launcher."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Print a one-line summary to stdout. Errors always go to stderr "
            "regardless; --verbose gates only the success summary."
        ),
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir.expanduser().resolve() if args.data_dir else get_paths().data_dir()
    scripts_dir = Path(__file__).parent
    extract_script = scripts_dir / "extract_learnings.py"

    if not extract_script.exists():
        logger.error("extract_learnings.py not found beside %s", __file__)
        # Never gate errors on --verbose: the launcher runs this silently,
        # and a bare exit 1 with only a log-file line is an invisible
        # failure. --verbose gates the success summary only.
        print(f"[drain] extract_learnings.py missing in {scripts_dir}", file=sys.stderr)
        return 1

    # Run the pass even with an empty queue: recover_stale_processing (inside
    # process_deferred_extractions) requeues markers orphaned by a crashed
    # child, which is how those eventually get retried.
    #
    # Report both queues. The pending count alone is measured *before* that
    # recovery step, so a run that is about to rescue an orphan would announce
    # "0 marker(s) pending" and then report having drained one — which reads
    # as a malfunction and cost three by-hand runs to see through (2026-08-01).
    logger.info(
        "Draining %s (%d pending, %d in flight)",
        data_dir,
        pending_count(data_dir),
        processing_count(data_dir),
    )
    result = process_deferred_extractions(data_dir, extract_script, wait=args.wait)

    if result.launched:
        logger.info(
            "Drained %d deferred extraction(s) from %s", result.launched, data_dir
        )
        log_event(
            "extract", "launch",
            f"host drain launched {result.launched} deferred extraction(s)",
            count=result.launched,
        )
    # Refresh the fleet view. This is the walk-away moment — the tab that just
    # closed is the one whose state Spike was carrying in his head — so
    # AGENTS.md and fleet.txt should reflect the exit before anything else
    # runs. The registry is already current (SessionEnd updated `last_event`
    # before the container died); what the launched children will add later is
    # the diary entry, which this view does not read. Runs before the failure
    # check on purpose: a failed extraction child changes nothing about what
    # the fleet view should show.
    try:
        write_fleet_view(data_dir)
    except Exception:
        logger.warning("Fleet view refresh failed (non-fatal)", exc_info=True)

    if result.failed:
        # Only ever nonzero with --wait (fire-and-forget never reaps its
        # children). A scripted check (`drain … --wait && echo ok`) must not
        # report success when every extraction failed.
        logger.error(
            "%d of %d extraction child(ren) exited nonzero", result.failed, result.launched
        )
        print(
            f"[drain] {result.failed} of {result.launched} extraction child(ren) failed",
            file=sys.stderr,
        )
        return 1
    if args.verbose:
        print(f"[drain] {result.launched} extraction(s) launched from {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
