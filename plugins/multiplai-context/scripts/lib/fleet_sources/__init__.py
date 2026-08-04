"""Extra fleet sources — everything the session registry cannot see.

``lib.fleet`` joins the session registry and the checkpoints, and that join is
cheap enough to run from a hook: it reads local files and nothing else. These
modules are the opposite kind of thing. They shell out to ``git``, call the
GitHub API through ``gh``, and walk directories, so they are **never** on the
hook path — only :mod:`fleet_status`, which a human ran on purpose, collects
them.

That split is why they live in their own package rather than growing inside
``lib.fleet``: the expensive collectors are impossible to accidentally import
into the fast path.

Each collector obeys the same three rules, because a status view that crashes
is worse than one that admits a gap:

* **Never raise.** A missing binary, an unreachable network, a repo with no
  remote — each degrades to an ``error`` string on the returned record.
* **Always bounded.** Every subprocess has a timeout; every fan-out has a
  worker cap.
* **Report ignorance as ignorance.** When something cannot be known, say so
  rather than reporting zero. ``PRs: gh unavailable`` is useful; ``0 PRs`` when
  there are eleven is a lie that costs you a merge.
"""

from lib.fleet_sources.backlog import Backlog, collect_backlog
from lib.fleet_sources.git_repos import RepoState, collect_repos, find_repos
from lib.fleet_sources.jobs import BackgroundJob, collect_jobs
from lib.fleet_sources.prs import PullRequest, collect_prs, gh_available

__all__ = [
    "Backlog",
    "BackgroundJob",
    "PullRequest",
    "RepoState",
    "collect_backlog",
    "collect_jobs",
    "collect_prs",
    "collect_repos",
    "find_repos",
    "gh_available",
]
