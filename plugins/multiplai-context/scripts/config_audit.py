# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.8.1"]
# ///
"""Config-audit state stamping for the multiplai plugin.

The ``/multiplai-context:config-audit`` skill is prompt-driven — the audit
itself runs in the model. This script is the deterministic part: recording
that an audit happened. It resolves the state file exactly the way the
SessionStart gate does (``get_paths().data_dir / "config_audit_state.yaml"``,
the same env cascade: CLAUDE_PLUGIN_OPTION_data_dir → <workspace>/.multiplai/
data → CLAUDE_PLUGIN_DATA → ~/.multiplai/data), so the stamp always lands
where ``session_start._config_audit_gate_open()`` looks. Hand-locating the
directory from the skill prompt (the previous design) broke on installs where
the data dir comes from CLAUDE_PLUGIN_DATA or the option override — the stamp
landed in the wrong place and the nudge fired forever.

Usage (from the skill, step 6)::

    uv run --no-project config_audit.py --stamp \\
        --proposal .multiplai/dreams/config-audit-YYYY-MM-DD.md

Mirrors ``dream.py --stamp`` for the dream gate.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.config import save_yaml
from multiplai_core.log_utils import setup_logging

logger = setup_logging("config_audit")

STATE_FILENAME = "config_audit_state.yaml"


def state_file() -> Path:
    """The gate's state file: ``data_dir/config_audit_state.yaml``.

    Lives beside ``dream_state.yaml`` — same directory the SessionStart
    gate derives it from (``paths.dream_state_file().parent``).
    """
    return get_paths().data_dir / STATE_FILENAME


def stamp(proposal: str | None = None) -> Path:
    """Record a completed config audit; returns the state-file path.

    Writes ``last_run`` (UTC ISO-8601 now) plus the optional ``proposal``
    path. The state is written *fresh*, not merged — only the latest run
    matters, and a stale ``proposal`` key from a previous run must not
    survive a stamp that has none. ``save_yaml`` is atomic (tmp +
    ``os.replace``) and creates parent directories.
    """
    path = state_file()
    state: dict = {"last_run": datetime.now(timezone.utc).isoformat()}
    if proposal:
        state["proposal"] = proposal
    save_yaml(path, state)
    logger.info("Stamped config-audit state: %s", path)
    return path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Config-audit state stamping (closes the 90-day SessionStart nudge gate)"
    )
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="Record that a config audit ran (writes config_audit_state.yaml "
             "so the 90-day SessionStart gate stops nudging). Used by "
             "/multiplai-context:config-audit as its final step.",
    )
    parser.add_argument(
        "--proposal",
        metavar="PATH",
        help="With --stamp: proposal file path to record in the state "
             "(e.g. .multiplai/dreams/config-audit-YYYY-MM-DD.md).",
    )
    args = parser.parse_args()

    if not args.stamp:
        parser.error("nothing to do — pass --stamp")

    path = stamp(args.proposal)
    print(f"Stamped config_audit_state: {path}")


if __name__ == "__main__":
    main()
