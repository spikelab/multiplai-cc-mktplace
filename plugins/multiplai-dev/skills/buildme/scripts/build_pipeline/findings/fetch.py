"""Read a PR's open comments and decide which ones are acted on.

Every GitHub call goes through `git_ops._run_gh`, so tests stub one seam. All
of them are reads. Comment bodies are untrusted: they are fenced before any
model sees them (`PRComment.fenced_body`), never become argv or a path, and a
body that matches an injection pattern is listed and not acted on.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from multiplai_core.untrusted import contains_injection

from .. import git_ops
from ..config import FindingsSettings
from .models import PRComment

log = logging.getLogger(__name__)

# The marker `/multiplai-dev:review post` writes on every finding it posts,
# copied verbatim from review_pipeline/pr_marker.py (MARKER_VERSION 1), which
# documents it for this reader. buildme does not import review_pipeline.
FINDING_MARKER_RE = re.compile(
    r"<!--\s*multiplai-review:finding\s+id=(?P<id>[0-9a-f]+)\s+round=(?P<round>[0-9a-f]+(?:-[0-9]+)?)"
    r"\s+v=(?P<v>[0-9]+)\s*-->"
)

# The last line of every reply this pipeline writes. The person running it
# usually also writes review comments under the same login, so without this
# the pipeline's own replies would come back as new comments next round.
REPLY_MARKER = "<!-- multiplai-buildme:reply v=1 -->"
_REPLY_MARKER_RE = re.compile(r"<!--\s*multiplai-buildme:reply\s+v=\d+\s*-->")
# The marker on the comment the pr skill's `edit` posts when it first takes
# over a PR body (publish runs `edit`), copied from pr_pipeline/markers.py
# `NOTICE`. That comment is never a review comment to act on.
PR_NOTICE_MARKER = "<!-- multiplai-pr:notice v=1 -->"
_PR_NOTICE_RE = re.compile(r"<!--\s*multiplai-pr:notice\s+v=\d+\s*-->")
# multiplai-dev 0.42.0 posted the same comment without the marker.
_PR_NOTICE_UNMARKED = "The pr skill (`/multiplai-dev:pr`) edited this PR's description."

# `role_name` values that count as "collaborators with write access".
WRITE_ROLES = frozenset({"admin", "maintain", "write"})

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line
          comments(first: 100) {
            nodes { databaseId author { login __typename } authorAssociation body createdAt url }
          }
        }
      }
    }
  }
}
"""

_MAX_PAGES = 50


class FetchError(RuntimeError):
    """A GitHub read failed. The round stops; nothing was acted on."""


def parse_finding_id(body: str | None) -> str | None:
    """The finding id in the review marker on *body*, or None."""
    m = FINDING_MARKER_RE.search(body or "")
    return m["id"] if m else None


def is_pipeline_reply(body: str | None) -> bool:
    return bool(_REPLY_MARKER_RE.search(body or ""))


def is_pr_skill_notice(body: str | None) -> bool:
    return bool(_PR_NOTICE_RE.search(body or "")) or (body or "").startswith(_PR_NOTICE_UNMARKED)


def _split_repo(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    return owner, name


def repo_slug(project_dir: Path) -> str:
    """`owner/name` of the repo's GitHub remote, from `gh repo view`."""
    res = git_ops._run_gh(["gh", "repo", "view", "--json", "nameWithOwner",
                           "--jq", ".nameWithOwner"], cwd=project_dir)
    slug = res.stdout.strip()
    if not res.ok or not slug:
        raise FetchError(f"could not read the repo's GitHub name: {(res.stderr or res.stdout).strip()}")
    return slug


def _json(res: git_ops.GitResult, what: str):
    if not res.ok:
        raise FetchError(f"{what} failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise FetchError(f"{what} returned something that is not JSON: {e}") from e


def _is_bot(login: str, typename: str | None = None, user_type: str | None = None) -> bool:
    return typename == "Bot" or user_type == "Bot" or login.endswith("[bot]")


def _review_comments(repo: str, pr: int, cwd: Path | None) -> list[PRComment]:
    owner, name = _split_repo(repo)
    out: list[PRComment] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        argv = ["gh", "api", "graphql", "-f", f"query={_THREADS_QUERY}",
                "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={pr}"]
        if cursor:
            argv += ["-f", f"cursor={cursor}"]
        data = _json(git_ops._run_gh(argv, cwd=cwd), "listing review threads")
        try:
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (KeyError, TypeError) as e:
            raise FetchError(f"unexpected review-thread response: {str(data)[:300]}") from e
        for t in threads.get("nodes") or []:
            if t.get("isResolved"):
                continue
            comments = ((t.get("comments") or {}).get("nodes")) or []
            if not comments:
                continue
            first_id = comments[0].get("databaseId")
            for c in comments:
                body = c.get("body") or ""
                if is_pipeline_reply(body):
                    continue
                author = c.get("author") or {}
                login = author.get("login") or "ghost"
                out.append(PRComment(
                    id=c["databaseId"], kind="review", author=login,
                    is_bot=_is_bot(login, typename=author.get("__typename")),
                    body=body, url=c.get("url") or "", created_at=c.get("createdAt") or "",
                    path=t.get("path"), line=t.get("line"), thread_id=t.get("id"),
                    reply_to=first_id, outdated=bool(t.get("isOutdated")),
                    finding_id=parse_finding_id(body),
                ))
        info = threads.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return out
        cursor = info.get("endCursor")
    raise FetchError(f"more than {_MAX_PAGES} pages of review threads")


def _issue_comments(repo: str, pr: int, since: str | None, cwd: Path | None) -> list[PRComment]:
    res = git_ops._run_gh(["gh", "api", "--paginate", f"repos/{repo}/issues/{pr}/comments",
                           "--jq", ".[]"], cwd=cwd)
    if not res.ok:
        raise FetchError(f"listing PR comments failed (exit {res.returncode}): "
                         f"{(res.stderr or res.stdout).strip()}")
    out: list[PRComment] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError as e:
            raise FetchError(f"a PR comment is not JSON: {e}") from e
        created = c.get("created_at") or ""
        if since and created <= since:
            continue
        body = c.get("body") or ""
        if is_pipeline_reply(body) or is_pr_skill_notice(body):
            continue
        user = c.get("user") or {}
        login = user.get("login") or "ghost"
        out.append(PRComment(
            id=c["id"], kind="issue", author=login,
            is_bot=_is_bot(login, user_type=user.get("type")),
            body=body, url=c.get("html_url") or "", created_at=created,
            finding_id=parse_finding_id(body),
        ))
    return out


def fetch_comments(repo: str, pr: int, since: str | None, *,
                   cwd: Path | None = None) -> list[PRComment]:
    """Unresolved review-thread comments (outdated ones kept and marked), plus
    top-level comments created after *since* (an ISO-8601 UTC timestamp, as
    GitHub writes them; None for every one). The pipeline's own replies are
    left out. Raises `FetchError` when a read fails."""
    return [*_review_comments(repo, pr, cwd), *_issue_comments(repo, pr, since, cwd)]


def _bare(login: str) -> str:
    """GraphQL names an app `dependabot`, REST `dependabot[bot]`."""
    login = login.strip().lower()
    return login[:-5] if login.endswith("[bot]") else login


def _role_name(repo: str, login: str, cwd: Path | None) -> str:
    res = git_ops._run_gh(["gh", "api", f"repos/{repo}/collaborators/{login}/permission",
                           "--jq", ".role_name"], cwd=cwd)
    if not res.ok:
        # A non-collaborator is a 404 on some repos. Unknown is never "allowed".
        log.info("No permission for %s on %s (exit %d): %s", login, repo, res.returncode,
                 (res.stderr or res.stdout).strip()[:200])
        return ""
    return res.stdout.strip()


_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")


def is_allowed(comment: PRComment, settings: FindingsSettings, cache: dict[str, bool], *,
               repo: str, cwd: Path | None = None) -> bool:
    """Whether *comment*'s author is one whose comments are acted on.

    A bot only when it is in `findings.allow_bots`. Anyone else: with
    `findings.authors: collaborators`, when their `role_name` on the repo is
    admin, maintain or write (one API call per login per run, kept in
    *cache*); with a list, when they are on it."""
    login = comment.author
    if login in cache:
        return cache[login]
    if comment.is_bot:
        allowed = _bare(login) in {_bare(b) for b in settings.allow_bots}
    elif isinstance(settings.authors, list):
        allowed = login.lower() in {a.strip().lower() for a in settings.authors}
    elif not _LOGIN_RE.match(login):
        # Never put a malformed login into an API path.
        allowed = False
    else:
        allowed = _role_name(repo, login, cwd) in WRITE_ROLES
    cache[login] = allowed
    return allowed


def prepare(comments: list[PRComment], settings: FindingsSettings, cache: dict[str, bool], *,
            repo: str, cwd: Path | None = None) -> list[PRComment]:
    """Set `allowed` and `injection` on each comment, in place; returns them."""
    for c in comments:
        c.allowed = is_allowed(c, settings, cache, repo=repo, cwd=cwd)
        c.injection = contains_injection(c.body)
    return comments
