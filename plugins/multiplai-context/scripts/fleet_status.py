"""One command, the whole picture: agents, PRs, repos, jobs, backlog.

``AGENTS.md`` used to be written from the session registry and the checkpoints
alone. That worked, and for a week nobody opened it — while the status bar
rendered the same data as ``9 fronts · 4 need you``, a number with no referent
that tells you there is a fire without telling you where.

This script is the whole picture. It writes ``AGENTS.md`` from the registry and
the checkpoints, collects what the registry cannot see — open pull requests
with CI and review state, dirty and unpushed checkouts, background jobs, the
pending backlog — folds them into the same file, and prints a **ranked**
reading of what is actually blocked on you.

Three renderings, one collection, and that is the point: the digest is a
summary of ``AGENTS.md``, not a second opinion about it, and ``fleet.json``
hands the identical structure to the multiplai hub.

Usage (deps come from the workspace root, not PEP 723 — see the root
``pyproject.toml``)::

    uv run --project <plugin>/scripts <plugin>/scripts/fleet_status.py            # digest
    uv run --project <plugin>/scripts <plugin>/scripts/fleet_status.py --full     # AGENTS.md
    uv run --project <plugin>/scripts <plugin>/scripts/fleet_status.py --json     # fleet.json
    uv run --project <plugin>/scripts <plugin>/scripts/fleet_status.py --fresh    # skip PR cache
    uv run --project <plugin>/scripts <plugin>/scripts/fleet_status.py --offline  # skip gh

Read-only. It writes ``AGENTS.md``, ``fleet.json`` and a PR cache and touches
nothing else — no merges, no branch deletion, no session is killed. Both output
files remain pure caches: delete them and the next run rebuilds them.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.log_utils import setup_logging
from multiplai_core.paths import get_paths

from lib.fleet import (
    AGENTS_FILENAME,
    FLEET_JSON_FILENAME,
    Fleet,
    collect,
    fleet_json,
    render_agents_md,
)
from lib.fleet_digest import render_digest
from lib.fleet_sources.backlog import collect_backlog
from lib.fleet_sources.common import DEFAULT_TTL_SECONDS, cache_read, cache_write
from lib.fleet_sources.git_repos import collect_repos
from lib.fleet_sources.jobs import collect_jobs
from lib.fleet_sources.prs import collect_prs, scan_from_dict, scan_to_dict
from lib.fsio import atomic_write

logger = setup_logging("fleet_status")

_PR_CACHE_KEY = "prs"


def collect_full(
    data_dir: Path,
    workspace: Path,
    now: datetime,
    *,
    ttl: float = DEFAULT_TTL_SECONDS,
    offline: bool = False,
) -> Fleet:
    """The session join plus every extra source.

    Ordering is deliberate: the local, always-correct sources are gathered
    first, so a hung network call degrades the PR section and nothing else.
    """
    fleet = collect(data_dir, now)
    fleet.repos = collect_repos(workspace)
    fleet.jobs = collect_jobs(now=now)
    fleet.backlog = collect_backlog(data_dir, now=now)

    if offline:
        # `None` means "not collected" — distinct from an empty scan, which
        # would claim there are no open PRs.
        fleet.prs = None
        return fleet

    cached = scan_from_dict(cache_read(data_dir, _PR_CACHE_KEY, ttl))
    if cached is not None:
        fleet.prs = cached
        return fleet

    slugs = [r.slug for r in (fleet.repos or []) if r.slug]
    scan = collect_prs(slugs)
    fleet.prs = scan
    # Only cache a scan that reached GitHub. Caching an unavailable-`gh` result
    # would keep reporting "not read" for five minutes after `gh auth login`.
    if scan.available:
        cache_write(data_dir, _PR_CACHE_KEY, scan_to_dict(scan))
    return fleet


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ranked snapshot of everything in flight: agent sessions, open "
            "PRs, repo hygiene, background jobs, and the pending backlog."
        )
    )
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Runtime data dir holding sessions/ and checkpoints/.")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="Root to scan for git checkouts. Defaults to the workspace.")
    parser.add_argument("--full", action="store_true",
                        help="Print the full AGENTS.md report instead of the digest.")
    parser.add_argument("--json", action="store_true",
                        help="Print fleet.json to stdout instead of the digest.")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the PR cache and re-query GitHub.")
    parser.add_argument("--offline", action="store_true",
                        help="Skip GitHub entirely. PRs render as not collected.")
    parser.add_argument("--ttl", type=float, default=DEFAULT_TTL_SECONDS,
                        help=f"PR cache lifetime in seconds (default {DEFAULT_TTL_SECONDS:g}).")
    parser.add_argument("--no-write", action="store_true",
                        help="Print only; do not refresh AGENTS.md or fleet.json.")
    args = parser.parse_args()

    paths = get_paths()
    data_dir = args.data_dir.expanduser().resolve() if args.data_dir else paths.data_dir()
    workspace = (
        args.workspace.expanduser().resolve() if args.workspace
        else data_dir.parent.parent
    )
    now = datetime.now(timezone.utc)

    fleet = collect_full(
        data_dir, workspace, now,
        ttl=0 if args.fresh else args.ttl,
        offline=args.offline,
    )

    agents_md = render_agents_md(fleet, now)
    payload = fleet_json(fleet, now)
    agents_path = data_dir / AGENTS_FILENAME

    if not args.no_write:
        try:
            atomic_write(agents_path, agents_md)
            atomic_write(data_dir / FLEET_JSON_FILENAME, payload)
        except OSError as exc:
            # A read-only data dir degrades to a printed digest. The reading is
            # still true; only the cache of it is missing.
            logger.warning("Fleet view not written under %s: %s", data_dir, exc)
            print(f"[fleet] not written: {exc}", file=sys.stderr)

    if args.full:
        print(agents_md, end="")
    elif args.json:
        print(payload, end="")
    else:
        print(render_digest(fleet, now, str(agents_path)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
