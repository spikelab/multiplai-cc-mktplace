"""Unresolved review threads on the epic children's pull requests: the one
network read of the needs-you list (`/api/needs-you`), its last step.

Each PR is read with `gh api graphql` (the person's own `gh` login; no token
passes through here), every page of its threads, and the answer kept for
`TTL_S` seconds, so a page polling the list asks GitHub at most once a minute
per PR. A failed read (no `gh`, not logged in, no network, a timeout, an
answer that is not the expected JSON) is a `GitHubError`, kept for `TTL_S`
seconds too: `add_thread_rows` records it in `NeedsYou.github` and the rest
of the list stands. One list gets at most `BUDGET_S` seconds of GitHub, well
inside the page's 20-second request timeout; a PR not read by then is an
error of that list.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from viewer_kit.people import Person

from .config import ViewerConfig
from .models import NeedsYou, NeedsYouRow
from .status import iso_utc, people_of, sort_needs

TTL_S = 60.0
TIMEOUT_S = 20
BUDGET_S = 10.0  # all of one list's reads: the page gives up at 20 s
MIN_READ_S = 1.0  # less time left than this: the PR is not read
MAX_PAGES = 20  # 2,000 threads; more is reported as a read that did not finish
PR_URL = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s]+)/pull/(\d+)")
QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $after: String) {"
    " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
    " reviewThreads(first: 100, after: $after) { nodes { isResolved"
    " comments(first: 1) { nodes { createdAt url } } }"
    " pageInfo { hasNextPage endCursor } } } } }"
)


class GitHubError(Exception):
    """A pull request's review threads could not be read."""


@dataclass
class Threads:
    count: int
    oldest: str | None = None


def origin_repo(url: str | None) -> tuple[str, str] | None:
    """`(owner, name)` of a GitHub remote URL, else None."""
    if not url:
        return None
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return (m.group(1), m.group(2)) if m else None


def _target(pr: str, default_repo: tuple[str, str] | None) -> tuple[str, str, int]:
    m = PR_URL.search(pr)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    if pr.strip().lstrip("#").isdigit() and default_repo is not None:
        return default_repo[0], default_repo[1], int(pr.strip().lstrip("#"))
    raise GitHubError(f"{pr} names no GitHub pull request")


@dataclass
class ThreadReader:
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    clock: Callable[[], float] = time.monotonic
    _cache: dict[tuple[str, str, int], tuple[float, Threads | GitHubError]] = field(
        default_factory=dict
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def unresolved(
        self,
        pr: str,
        default_repo: tuple[str, str] | None,
        timeout: float = TIMEOUT_S,
    ) -> Threads:
        """The PR's unresolved review threads, and when the oldest one began.
        *timeout*: the seconds its pages may take together."""
        key = _target(pr, default_repo)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and self.clock() - hit[0] < TTL_S:
                if isinstance(hit[1], GitHubError):
                    raise hit[1]
                return hit[1]
        if timeout < MIN_READ_S:  # the list's budget is spent: not GitHub's failure
            raise GitHubError(
                f"{key[0]}/{key[1]}#{key[2]} not read: no time was left for it"
            )
        try:
            res = self._read(key, self.clock() + timeout)
        except GitHubError as e:
            with self._lock:
                self._cache[key] = (self.clock(), e)
            raise
        with self._lock:
            self._cache[key] = (self.clock(), res)
        return res

    def _read(self, key: tuple[str, str, int], until: float) -> Threads:
        owner, name, number = key
        open_: list[dict] = []
        after = None
        for _ in range(MAX_PAGES):
            left = until - self.clock()
            if left < MIN_READ_S:
                raise GitHubError(
                    f"{owner}/{name}#{number} not read: GitHub did not answer in time"
                )
            nodes, after = self._page(key, after, left)
            open_ += [n for n in nodes if not n["isResolved"]]
            if after is None:
                break
        else:
            raise GitHubError(
                f"{owner}/{name}#{number} has over {MAX_PAGES * 100} review "
                "threads: not all were read"
            )
        try:
            dates = [c["createdAt"] for n in open_ for c in n["comments"]["nodes"][:1]]
        except (KeyError, TypeError) as e:
            raise GitHubError(
                f"unexpected answer from GitHub for {owner}/{name}#{number}: {e!r}"
            ) from None
        return Threads(count=len(open_), oldest=iso_utc(min(dates)) if dates else None)

    def _page(
        self, key: tuple[str, str, int], after: str | None, timeout: float
    ) -> tuple[list[dict], str | None]:
        """One page of threads, and the cursor of the next one (None at the end)."""
        owner, name, number = key
        argv = [
            "gh", "api", "graphql",
            "-f", f"query={QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"number={number}",
        ]  # fmt: skip
        if after is not None:
            argv += ["-f", f"after={after}"]
        try:
            proc = self.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            raise GitHubError(
                f"{owner}/{name}#{number} not read: GitHub did not answer in "
                f"{timeout:.0f}s"
            ) from None
        except (OSError, subprocess.SubprocessError) as e:
            raise GitHubError(f"gh could not run: {e}") from None
        if proc.returncode != 0:
            raise GitHubError(
                f"gh api graphql failed for {owner}/{name}#{number}: "
                + (proc.stderr or "").strip()[:300]
            )
        try:
            threads = json.loads(proc.stdout)["data"]["repository"]["pullRequest"][
                "reviewThreads"
            ]
            nodes = list(threads["nodes"])
            page = threads.get("pageInfo") or {}
            more = page.get("hasNextPage") is True and page.get("endCursor")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise GitHubError(
                f"unexpected answer from GitHub for {owner}/{name}#{number}: {e!r}"
            ) from None
        return nodes, (str(page["endCursor"]) if more else None)


def add_thread_rows(
    needs: NeedsYou,
    prs: list[tuple[str, str, str]],
    reader: ThreadReader,
    cfg: ViewerConfig,
    people: Mapping[str, Person],
    default_repo: tuple[str, str] | None,
    now: datetime,
) -> NeedsYou:
    """Add one row per child PR (`status.child_prs`) with unresolved review
    threads, on the engineers, and say in `needs.github` whether every PR
    was read. The rows already there are kept whatever GitHub answers. All
    the reads share `BUDGET_S` seconds; a PR an earlier read left no time
    for is an error, not a wait."""
    engineers = people_of("engineer", cfg, people)
    errors = []
    until = reader.clock() + BUDGET_S
    for change, child, pr in prs:
        try:
            t = reader.unresolved(
                pr, default_repo, timeout=min(TIMEOUT_S, until - reader.clock())
            )
        except GitHubError as e:
            errors.append(str(e))
            continue
        if not t.count:
            continue
        since = t.oldest
        waited = None
        if since:
            waited = (now - datetime.fromisoformat(since)).total_seconds()
        needs.rows.append(
            NeedsYouRow(
                kind="review-threads",
                change=change,
                child=child,
                what=f"{t.count} unresolved review thread"
                + ("" if t.count == 1 else "s")
                + f" on {child}'s pull request",
                waiting_on=engineers,
                since=since,
                waited_s=waited,
                link=f"#change={change}",
                url=pr,
                count=t.count,
            )
        )
    needs.rows = sort_needs(needs.rows)
    needs.github.prs = len(prs)
    needs.github.checked = not errors
    needs.github.error = "; ".join(errors[:3]) or None
    return needs
