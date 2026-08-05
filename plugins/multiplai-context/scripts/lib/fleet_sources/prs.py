"""Open pull requests, with the two facts that decide whether you must act.

A PR list is not a fleet view. What matters is which ones are *blocked on you*
— approved and waiting for a merge click — and which are *rotting* — red CI on
something you own. Everything else is inventory, and inventory belongs in a
count, not in a list you read every evening.

Two classifications earn their place here:

* **Bot vs. human.** Six of eleven open PRs on one repo were dependabot. Folding
  them into one number turns "11 open PRs" into an anxiety with no action
  attached, which is precisely the failure the status-line count made.
* **Stack membership.** Four PRs that must merge in order are one decision, not
  four. :func:`stacks` finds them by base-branch chaining so the digest can say
  so.

Requires the ``gh`` CLI, authenticated. Without it every function degrades to
"unknown", never to zero — see :func:`gh_available`.
"""

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from lib.fleet_sources.common import run

logger = logging.getLogger(__name__)

# `gh pr list` against a repo is a network round trip. 6s is generous for a
# healthy connection and short enough that one sick repo cannot dominate — a
# single 8s timeout was measured adding 6s to the whole sweep.
PR_TIMEOUT = 6.0

# Higher than the shared cap: these are read-only network calls waiting on
# latency, not on disk or CPU, so more of them in flight is nearly free. Kept
# well under GitHub's secondary rate limits.
PR_WORKERS = 16

_FIELDS = (
    "number,title,url,author,isDraft,reviewDecision,statusCheckRollup,"
    "createdAt,updatedAt,headRefName,baseRefName,mergeable,labels"
)


@dataclass
class PullRequest:
    repo: str = ""                 # owner/name
    number: int = 0
    title: str = ""
    url: str = ""
    author: str = ""
    is_bot: bool = False
    is_draft: bool = False
    review_decision: str = ""      # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | ""
    ci: str = "none"               # passing | failing | pending | none
    head: str = ""
    base: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.review_decision == "APPROVED"

    @property
    def failing(self) -> bool:
        return self.ci == "failing"

    @property
    def label(self) -> str:
        """``repo#123`` using the short repo name — the owner is never the question."""
        short = self.repo.split("/")[-1] if self.repo else ""
        return f"{short}#{self.number}" if short else f"#{self.number}"


@dataclass
class PRScan:
    """What the PR sweep found, plus what it could not find out.

    ``no_access`` and ``errors`` are separated on purpose. A repo the token
    cannot see is a **standing configuration fact** — a different org, a repo
    outside the GitHub App installation — and it is true every single run.
    Filing it as an error means thirteen warnings every evening, which trains
    you to ignore the line that also carries the real failures. An ``error`` is
    something that might work next time.
    """

    prs: list[PullRequest] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)     # slug -> message
    no_access: list[str] = field(default_factory=list)       # invisible to this token
    available: bool = True                                   # was `gh` usable at all

    @property
    def human(self) -> list[PullRequest]:
        return [p for p in self.prs if not p.is_bot]

    @property
    def bots(self) -> list[PullRequest]:
        return [p for p in self.prs if p.is_bot]


def gh_available() -> bool:
    """Is the ``gh`` binary present?

    Presence only, and deliberately: ``gh auth status`` is a **network round
    trip** — measured at 6.4 s against a live account, nearly half the cost of
    the entire sweep — to learn something the sweep itself reports for free. An
    unauthenticated `gh` fails every repo with an auth error, which
    :func:`collect_prs` already recognises and turns into the same
    "unavailable" reading. Pay for the answer once, not twice.
    """
    return shutil.which("gh") is not None


# The wording `gh` uses when the credential, rather than the repo, is the
# problem. Distinguishing this from a per-repo failure is what lets a wholly
# unauthenticated `gh` report as "not read" instead of "no open PRs".
_AUTH_MARKERS = (
    "not logged into",
    "authentication required",
    "gh auth login",
    "bad credentials",
    "requires authentication",
)


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        ts = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _rollup(raw) -> str:
    """Collapse ``statusCheckRollup`` into one word.

    Deliberately pessimistic in ordering: any failure makes the PR failing even
    if forty other checks are green, because one red check blocks the merge.
    Pending only wins when nothing has failed yet.
    """
    if not isinstance(raw, list) or not raw:
        return "none"
    states = set()
    for check in raw:
        if not isinstance(check, dict):
            continue
        # CheckRun uses conclusion/status; StatusContext uses state.
        value = (
            check.get("conclusion")
            or check.get("state")
            or check.get("status")
            or ""
        )
        states.add(str(value).upper())
    if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return "failing"
    if states & {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", ""}:
        return "pending"
    if states & {"SUCCESS", "NEUTRAL", "SKIPPED", "COMPLETED"}:
        return "passing"
    return "none"


def _one(pr: dict, slug: str) -> PullRequest:
    author = pr.get("author") or {}
    login = str(author.get("login") or "")
    return PullRequest(
        repo=slug,
        number=int(pr.get("number") or 0),
        title=str(pr.get("title") or "").strip(),
        url=str(pr.get("url") or ""),
        author=login,
        # `is_bot` is reported by the API; the login prefix is the fallback for
        # app-authored PRs, which arrive as `app/dependabot`.
        is_bot=bool(author.get("is_bot")) or login.startswith("app/"),
        is_draft=bool(pr.get("isDraft")),
        review_decision=str(pr.get("reviewDecision") or ""),
        ci=_rollup(pr.get("statusCheckRollup")),
        head=str(pr.get("headRefName") or ""),
        base=str(pr.get("baseRefName") or ""),
        created_at=_parse_ts(pr.get("createdAt")),
        updated_at=_parse_ts(pr.get("updatedAt")),
    )


# GitHub's two ways of saying "you cannot see this repo": it does not exist as
# far as this token is concerned, or it exists and the token lacks the scope.
# Both are standing facts about the credential, not failures to retry.
_NO_ACCESS_MARKERS = (
    "could not resolve to a repository",
    "resource not accessible",
    "must have admin rights",
    "not found",
)


def _for_repo(slug: str) -> tuple[str, list[PullRequest], str, bool]:
    """``(slug, prs, error, no_access)``."""
    got = run(
        ["gh", "pr", "list", "-R", slug, "--state", "open",
         "--limit", "50", "--json", _FIELDS],
        timeout=PR_TIMEOUT,
    )
    if not got.ok:
        err = (got.err or "gh pr list failed").splitlines()[0][:160]
        blind = any(m in err.lower() for m in _NO_ACCESS_MARKERS)
        return slug, [], ("" if blind else err), blind
    try:
        raw = json.loads(got.out or "[]")
    except (json.JSONDecodeError, ValueError) as exc:
        return slug, [], f"unreadable gh output: {exc}", False
    if not isinstance(raw, list):
        return slug, [], "unexpected gh output", False
    return slug, [_one(p, slug) for p in raw if isinstance(p, dict)], "", False


def collect_prs(slugs: list[str]) -> PRScan:
    """Open PRs across every *slug* (``owner/name``), queried concurrently."""
    scan = PRScan()
    slugs = sorted({s for s in slugs if s})
    if not slugs:
        return scan
    if not gh_available():
        scan.available = False
        return scan

    auth_failures = 0
    with ThreadPoolExecutor(max_workers=PR_WORKERS) as pool:
        for slug, prs, err, blind in pool.map(_for_repo, slugs):
            if blind:
                scan.no_access.append(slug)
            elif err:
                scan.errors[slug] = err
                if any(m in err.lower() for m in _AUTH_MARKERS):
                    auth_failures += 1
            scan.prs.extend(prs)

    # Every repo failing on the credential is one problem, not twenty-seven.
    # Report it as `gh` being unusable so the digest says "not read" rather
    # than listing every repo as unreachable — and, crucially, never "0 open".
    if auth_failures == len(slugs):
        scan.available = False
        scan.errors.clear()

    scan.prs.sort(key=lambda p: (p.repo, p.number))
    scan.no_access.sort()
    return scan


def scan_to_dict(scan: PRScan) -> dict:
    """Serialize a scan for the cache. Datetimes become ISO strings."""
    return {
        "available": scan.available,
        "errors": dict(scan.errors),
        "no_access": list(scan.no_access),
        "prs": [
            {
                **{k: v for k, v in vars(pr).items()
                   if k not in ("created_at", "updated_at")},
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
            }
            for pr in scan.prs
        ],
    }


def scan_from_dict(raw) -> PRScan | None:
    """Rebuild a scan from the cache, or ``None`` if the blob is unusable.

    Returning ``None`` rather than an empty scan matters: a corrupt cache entry
    must fall through to a live query, not silently report "no open PRs".
    """
    if not isinstance(raw, dict):
        return None
    try:
        scan = PRScan(
            available=bool(raw.get("available", True)),
            errors={str(k): str(v) for k, v in (raw.get("errors") or {}).items()},
            no_access=[str(s) for s in (raw.get("no_access") or [])],
        )
        for item in raw.get("prs") or []:
            if not isinstance(item, dict):
                continue
            fields = dict(item)
            fields["created_at"] = _parse_ts(fields.get("created_at"))
            fields["updated_at"] = _parse_ts(fields.get("updated_at"))
            known = {f for f in PullRequest.__dataclass_fields__}
            scan.prs.append(PullRequest(**{k: v for k, v in fields.items() if k in known}))
        return scan
    except (TypeError, ValueError):
        return None


def stacks(prs: list[PullRequest]) -> list[list[PullRequest]]:
    """Group PRs into merge-ordered chains.

    A stack is a run of PRs where one's ``base`` is another's ``head`` in the
    same repo — the shape you get from branching B off A while A is still open.
    They are one decision ("merge these four, in this order"), and listing them
    as four independent items is how a four-PR sprint reads as four fires.

    Returned innermost-first: the PR whose base is *not* another open PR comes
    first, because that is the one that can actually merge now.
    """
    # Both indexes are built once. They depend only on `prs`, so rebuilding
    # `based_on` inside the per-PR loop — as this did — made the whole thing
    # O(n²) for no gain.
    by_repo: dict[str, dict[str, PullRequest]] = {}
    based_on_by_repo: dict[str, dict[str, list[PullRequest]]] = {}
    for pr in prs:
        by_repo.setdefault(pr.repo, {})[pr.head] = pr
        based_on_by_repo.setdefault(pr.repo, {}).setdefault(pr.base, []).append(pr)

    seen: set[tuple[str, int]] = set()
    out: list[list[PullRequest]] = []
    for pr in prs:
        key = (pr.repo, pr.number)
        if key in seen:
            continue
        heads = by_repo.get(pr.repo, {})
        # Walk down to the root of this chain, guarding against a cycle.
        chain: list[PullRequest] = []
        cursor: PullRequest | None = pr
        walked: set[int] = set()
        while cursor is not None and cursor.number not in walked:
            walked.add(cursor.number)
            chain.append(cursor)
            cursor = heads.get(cursor.base)
        chain.reverse()
        # Then climb back up from the root through anything based on it.
        based_on = based_on_by_repo.get(pr.repo, {})
        cursor = chain[-1]
        while True:
            children = [c for c in based_on.get(cursor.head, []) if c.number not in walked]
            if not children:
                break
            child = min(children, key=lambda c: c.number)
            walked.add(child.number)
            chain.append(child)
            cursor = child
        if len(chain) < 2:
            continue
        for member in chain:
            seen.add((member.repo, member.number))
        out.append(chain)
    return out
