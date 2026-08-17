#!/usr/bin/env python3
"""Plane CLI restricted to an explicit project allowlist.

Talks to Plane (Cloud or self-hosted) over the REST API. Every request passes
through a single chokepoint (`_request`) that refuses any path touching a project
outside PLANE_ALLOWED_PROJECTS. A wrong --project, a stale UUID or a buggy call
site therefore fails closed instead of writing to a project nobody meant to
touch — which matters because the usual setup is one shared team project you
must not disturb alongside the ones you own.

The allowlist has no default. With PLANE_ALLOWED_PROJECTS unset the tool
refuses to run rather than defaulting to "everything the token can reach".

Stdlib only — no pip install required.

Configuration (environment):
    PLANE_API_TOKEN         required. Personal access token (header X-API-Key).
    PLANE_WORKSPACE         required. Workspace slug, as in the app URL.
    PLANE_ALLOWED_PROJECTS  required. Comma-separated project UUIDs. Each entry
                            may carry a label: "<uuid>:My Project".
    PLANE_BASE_URL          optional. Defaults to https://api.plane.so.
                            Self-hosted: https://plane.example.com
    PLANE_ENV_FILE          optional. Path to a KEY=VALUE file to read the above
                            from when they are absent from the environment.
                            Only PLANE_* keys are read; the environment wins.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timezone

# Cloudflare fronts api.plane.so and 403s non-browser User-Agents with
# "error code: 1010". Self-hosted instances accept this UA too, so send it
# unconditionally.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

_PRIORITIES = ["urgent", "high", "medium", "low", "none"]


class PlaneError(RuntimeError):
    """Configuration or API failure."""


class GuardError(RuntimeError):
    """A request would have touched a project outside the allowlist."""


# --- Configuration -----------------------------------------------------------


def _load_env_file() -> None:
    """Fill in missing PLANE_* vars from PLANE_ENV_FILE, if set.

    Exists because agent sandboxes often cannot see the shell environment where
    the token lives. The real environment always wins; this only fills gaps.
    """
    path = os.environ.get("PLANE_ENV_FILE")
    if not path:
        return
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise PlaneError(f"PLANE_ENV_FILE {path!r} could not be read: {exc}") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        # Accept `export PLANE_X=...` so a sourceable shell env file works too.
        if key.startswith("export "):
            key = key[len("export "):].strip()
        # An exported-but-empty var counts as absent, otherwise
        # `export PLANE_API_TOKEN=` silently shadows the file.
        if key.startswith("PLANE_") and not os.environ.get(key, "").strip():
            os.environ[key] = val.strip().strip('"').strip("'")


def _parse_allowlist(raw: str) -> dict[str, str]:
    """Parse "<uuid>[:label],<uuid>[:label]" into {uuid_lower: label}."""
    allowed: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        pid, _, label = part.partition(":")
        pid = pid.strip()
        if not re.fullmatch(_UUID, pid):
            raise PlaneError(
                f"PLANE_ALLOWED_PROJECTS entry {pid!r} is not a project UUID. "
                "Copy it from the project URL: "
                ".../projects/<uuid>/issues"
            )
        allowed[pid.lower()] = label.strip() or pid
    return allowed


def _cfg() -> dict:
    _load_env_file()

    missing = [
        name
        for name in ("PLANE_API_TOKEN", "PLANE_WORKSPACE", "PLANE_ALLOWED_PROJECTS")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise PlaneError(
            "missing required configuration: "
            + ", ".join(missing)
            + "\nSet them in the environment, or point PLANE_ENV_FILE at a file "
            "containing them. PLANE_ALLOWED_PROJECTS has no default on purpose: "
            "without it this tool will not run."
        )

    allowed = _parse_allowlist(os.environ["PLANE_ALLOWED_PROJECTS"])
    if not allowed:
        raise PlaneError("PLANE_ALLOWED_PROJECTS is empty — refusing to run")

    return {
        "token": os.environ["PLANE_API_TOKEN"].strip(),
        "base": os.environ.get("PLANE_BASE_URL", "https://api.plane.so").rstrip("/"),
        "workspace": os.environ["PLANE_WORKSPACE"].strip(),
        "allowed": allowed,
    }


# --- Guardrail ---------------------------------------------------------------


def _guard(method: str, path: str, allowed: dict) -> None:
    """Fail closed on anything outside the project allowlist.

    0. No dot segments. Whoever resolves `..` decides what the path means, and
       it is not this function: nginx in front of a self-hosted Plane rewrites
       `/projects/<allowed>/../<blocked>/issues/` to `/projects/<blocked>/issues/`
       *after* the check, so the allowlisted UUID would launder the rest.
       Percent-encoding is decoded first, for the same reason.
    1. Every project UUID appearing in the path must be allowlisted.
    2. Any query parameter carrying a UUID must be allowlisted too — whatever
       the key is called — so a filter like `?project_id=<other>` (or the same
       filter under another name) cannot reach past the path check. `search`
       alone is exempt: free text, and results are re-filtered client-side.
    3. A path with no project UUID is workspace-scoped: reads only.
    4. The project object itself is read-only (no create, rename or delete).
    """
    method = method.upper()
    bare, _, query = path.partition("?")

    for _ in range(3):
        decoded = urllib.parse.unquote(bare)
        if decoded == bare:
            break
        bare = decoded
    if urllib.parse.unquote(bare) != bare:
        # Still decodable after three passes: refuse rather than judge a
        # spelling some server layer may keep unwrapping.
        raise GuardError(
            f"refusing a path still percent-encoded after 3 decode passes ({path!r})"
        )
    # Some routers and WAFs treat "\" as "/"; normalise before splitting so
    # both spellings are judged the same way. Consecutive slashes are collapsed
    # for the same reason: a fronting proxy that merges them (nginx does, by
    # default) would otherwise see a different path than the one judged here.
    bare = re.sub(r"/+", "/", bare.replace("\\", "/"))
    if any(seg in (".", "..") for seg in bare.split("/")):
        raise GuardError(
            f"refusing a path containing dot segments ({path!r}) — the project "
            "it targets depends on who resolves them"
        )

    path_ids = [m.lower() for m in re.findall(rf"/projects/({_UUID})", bare)]
    # A UUID with the hyphens dropped names the same project; spell it back
    # before the lookup so recognition does not depend on the router's spelling.
    for m in re.findall(r"/projects/([0-9a-fA-F]{32})(?![0-9a-fA-F-])", bare):
        h = m.lower()
        path_ids.append(f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}")

    # Every query parameter is inspected, whatever its key is called: a
    # cross-project filter does not have to spell "project" in its name.
    # `search` is exempt as free text — a user may legitimately search for a
    # UUID, and cmd_search filters the results against the allowlist anyway.
    query_ids = []
    for key, value in urllib.parse.parse_qsl(query):
        if key.lower() == "search":
            continue
        query_ids.extend(m.lower() for m in re.findall(_UUID, value))

    for pid in path_ids + query_ids:
        if pid not in allowed:
            known = ", ".join(f"{v} [{k[:8]}]" for k, v in allowed.items())
            raise GuardError(
                f"project {pid} is not in the allowlist (allowed: {known})"
            )

    # Deliberately keyed on path UUIDs only: a workspace-scoped write is refused
    # even when it names an allowed project in the query string, because this
    # client never needs one and the endpoint's scope is the workspace.
    if not path_ids and method != "GET":
        raise GuardError(
            f"refusing {method} on workspace-scoped path {path!r} — "
            "writes must target an allowlisted project"
        )

    if method != "GET" and re.fullmatch(rf"/projects/{_UUID}/?", bare):
        raise GuardError(f"refusing {method} on the project object itself ({bare})")


# --- Transport ---------------------------------------------------------------


def _request(
    method: str,
    path: str,
    cfg: dict,
    *,
    params: dict | None = None,
    body: dict | None = None,
    dry_run: bool = False,
    max_retries: int = 5,
):
    """The only function that performs network I/O. Guarded on every call."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            path = f"{path}?{urllib.parse.urlencode(clean)}"

    _guard(method, path, cfg["allowed"])

    url = f"{cfg['base']}/api/v1/workspaces/{cfg['workspace']}{path}"

    if dry_run and method.upper() != "GET":
        print(f"[dry-run] {method.upper()} {url}")
        if body is not None:
            print(json.dumps(body, indent=2, ensure_ascii=False))
        return None

    data = json.dumps(body).encode() if body is not None else None
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method.upper())
        req.add_header("X-API-Key", cfg["token"])
        req.add_header("User-Agent", _UA)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                attempt += 1
                # X-RateLimit-Reset arrives in the wild as epoch seconds,
                # delta seconds, or (through some proxies) an HTTP-date.
                # Parse defensively: a non-numeric header falls back to
                # exponential backoff instead of crashing mid-retry.
                wait = float(2**attempt)
                reset = exc.headers.get("X-RateLimit-Reset")
                if reset:
                    try:
                        value = float(reset)
                    except ValueError:
                        value = None
                    if value is not None:
                        # Anything above ~3 years' worth of seconds is an
                        # epoch timestamp, not a delta.
                        wait = value - time.time() if value > 1e8 else value
                time.sleep(min(max(wait, 1), 60))
                continue
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code == 401:
                detail += "  (check PLANE_API_TOKEN)"
            elif exc.code == 404:
                detail += "  (check PLANE_WORKSPACE and the project UUID)"
            raise PlaneError(f"{method.upper()} {url} -> {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise PlaneError(f"{method.upper()} {url} -> {exc.reason}") from None


def _paginate(path: str, cfg: dict, *, params: dict | None = None):
    """Yield every result across Plane's cursor pagination.

    Some endpoints (e.g. /members/) answer with a bare list and no cursor;
    that is a single complete page, not an empty one.
    """
    params = dict(params or {})
    params.setdefault("per_page", 100)
    while True:
        data = _request("GET", path, cfg, params=params)
        if isinstance(data, list):
            yield from data
            return
        if not isinstance(data, dict):
            return
        yield from data.get("results", [])
        cursor = data.get("next_cursor")
        if not data.get("next_page_results") or not cursor:
            return
        params["cursor"] = cursor


# --- Content helpers ---------------------------------------------------------

# Plane's editor mangles some Unicode punctuation; teams commonly ban it in
# ticket bodies. Normalise to ASCII equivalents.
_SANITIZE = {
    "→": "->", "←": "<-", "↔": "<->",
    "⇒": "=>", "⇐": "<=",
    "—": "-", "–": "-",
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "…": "...", " ": " ",
}


def sanitize(text: str) -> str:
    for bad, good in _SANITIZE.items():
        text = text.replace(bad, good)
    return text


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def _fence_delim(line: str) -> str | None:
    """The code fence this line opens, or None."""
    m = _FENCE.match(line)
    return m.group(1) if m else None


def _closes_fence(line: str, delim: str) -> bool:
    """CommonMark closing rule: same character, at least as long, nothing after.

    The length part is what makes ````-quoted markdown work. Tracking fences
    with a boolean that flips on any ``` reads a four-backtick block as four
    separate fences, so the inner sample — the one the fence exists to protect —
    falls outside and gets rewritten, while the wrapper stays pristine.
    """
    m = _FENCE.match(line)
    return bool(
        m
        and m.group(1)[0] == delim[0]
        and len(m.group(1)) >= len(delim)
        and not m.group(2).strip()
    )


def _sanitize_prose(text: str) -> str:
    """Sanitize prose only, leaving fenced code blocks byte-for-byte intact.

    A ticket body often quotes code; rewriting punctuation inside a fence
    corrupts the sample it exists to show.
    """
    out: list[str] = []
    delim: str | None = None
    for line in text.split("\n"):
        if delim is None:
            opened = _fence_delim(line)
            delim = opened
            out.append(line if opened else sanitize(line))
            continue
        out.append(line)
        if _closes_fence(line, delim):
            delim = None
    return "\n".join(out)


def _esc(s: str) -> str:
    """Escape everything that can change how the output is parsed.

    The quote is in the list because `href="..."` is the one attribute this
    module emits, and an unescaped quote in a link target used to close it and
    let the rest of the text become attributes. The apostrophe is deliberately
    left alone: no single-quoted attribute is ever produced, and escaping it
    would rewrite every Italian body ("l'ospite") for no gain.
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Anything else (javascript:, data:, vbscript:) is rendered as text, never as a
# link target.
_SAFE_URL = re.compile(r"^(?:https?://|mailto:|/|#)", re.I)

# Up to three leading spaces is still the same list level (CommonMark). Deeper
# indentation is a nested item, which this converter does not build: it lets it
# fall through to the paragraph branch instead of flattening it into the parent
# list, so the output says "this was not a list" rather than lying about depth.
_ULIST = re.compile(r"^ {0,3}[-*+]\s+")
_OLIST = re.compile(r"^ {0,3}\d+[.)]\s+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _starts_block(line: str) -> bool:
    return bool(
        _ULIST.match(line)
        or _OLIST.match(line)
        or _HEADING.match(line)
        or _fence_delim(line)
    )


def md_to_html(text: str) -> str:
    """Minimal markdown -> HTML for Plane's description_html / comment_html.

    Supports headings, paragraphs, unordered and ordered lists, fenced code,
    images, and inline bold/italic/code/links. Tables, blockquotes and nested
    list items are not supported and degrade to plain paragraphs.
    """
    # \x00 is this function's own marker for a parked code span. A NUL arriving
    # in the body is indistinguishable from one and used to index a list that is
    # too short — an IndexError main() does not catch.
    text = text.replace("\x00", "")

    def inline(s: str) -> str:
        s = _esc(s)

        # Park code spans so bold/italic/link rules cannot reach inside them.
        spans: list[str] = []

        def park(m):
            spans.append(m.group(1))
            return f"\x00{len(spans) - 1}\x00"

        s = re.sub(r"`([^`]+)`", park, s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)

        # Images first: otherwise the link rule eats the bracket pair and leaves
        # the bang glued to an <a>, which is neither a link nor an image.
        # This tool cannot upload, so the URL is referenced where it stands.
        def image(m):
            alt, target = m.group(1), m.group(2)
            if not _SAFE_URL.match(target.strip()):
                return f"{alt} ({target})"
            return f'<img src="{target}" alt="{alt}">'

        def link(m):
            label, target = m.group(1), m.group(2)
            if not _SAFE_URL.match(target.strip()):
                return f"{label} ({target})"
            return f'<a href="{target}">{label}</a>'

        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image, s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, s)
        return re.sub(
            r"\x00(\d+)\x00",
            lambda m: f"<code>{spans[int(m.group(1))]}</code>",
            s,
        )

    out: list[str] = []
    lines = _sanitize_prose(text).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        delim = _fence_delim(line)
        if delim:
            i += 1
            block = []
            while i < len(lines) and not _closes_fence(lines[i], delim):
                block.append(_esc(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(block) + "</code></pre>")
            continue

        heading = _HEADING.match(line)
        if heading:
            lvl = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{inline(heading.group(2))}</h{lvl}>")
            i += 1
            continue

        if _ULIST.match(line):
            items = []
            while i < len(lines) and _ULIST.match(lines[i]):
                items.append(inline(_ULIST.sub("", lines[i], count=1)))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue

        if _OLIST.match(line):
            items = []
            while i < len(lines) and _OLIST.match(lines[i]):
                items.append(inline(_OLIST.sub("", lines[i], count=1)))
                i += 1
            out.append("<ol>" + "".join(f"<li>{x}</li>" for x in items) + "</ol>")
            continue

        if not line.strip():
            i += 1
            continue

        para = []
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(inline(lines[i].strip()))
            i += 1
        out.append("<p>" + " ".join(para) + "</p>")

    return "".join(out) or "<p></p>"


def html_to_text(html: str) -> str:
    """Rough HTML -> text, so issue bodies are readable in a terminal."""
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html)
    s = re.sub(r"</(p|div|h[1-6]|li|pre)>", "\n", s)
    s = re.sub(r"<li>", "  - ", s)
    s = re.sub(r"<[^>]+>", "", s)
    # &amp; last, or "&amp;lt;" would decode twice into "<".
    for ent, ch in (
        ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"),
    ):
        s = s.replace(ent, ch)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# --- Domain helpers ----------------------------------------------------------


def list_projects(cfg: dict) -> list[dict]:
    """Every project the token can see, annotated with allowlist status.

    Cached per config: a single `get SPK-12` used to walk this endpoint twice.
    """
    cached = cfg.get("_projects")
    if cached is not None:
        return cached
    projects = list(_paginate("/projects/", cfg))
    for p in projects:
        p["_allowed"] = p["id"].lower() in cfg["allowed"]
    cfg["_projects"] = projects
    return projects


def allowed_projects(cfg: dict) -> list[dict]:
    return [p for p in list_projects(cfg) if p["_allowed"]]


def resolve_project(cfg: dict, ref: str | None) -> dict:
    """Resolve a project by UUID, identifier (e.g. SPK) or name."""
    projects = list_projects(cfg)
    allowed = [p for p in projects if p["_allowed"]]

    if not ref:
        if len(allowed) == 1:
            return allowed[0]
        names = ", ".join(f"{p.get('identifier')} ({p.get('name')})" for p in allowed)
        raise PlaneError(
            f"--project is required: {len(allowed)} projects are allowed -> {names}"
        )

    needle = ref.strip().lower()
    for p in projects:
        if needle in (
            p["id"].lower(),
            (p.get("identifier") or "").lower(),
            (p.get("name") or "").lower(),
        ):
            if not p["_allowed"]:
                raise GuardError(
                    f"project {p.get('name')!r} ({p.get('identifier')}) is not in "
                    "the allowlist"
                )
            return p
    raise PlaneError(f"no project matching {ref!r} in workspace {cfg['workspace']}")


def _project_id_of(issue: dict) -> str:
    proj = issue.get("project")
    return proj["id"] if isinstance(proj, dict) else str(proj)


def resolve_issue(cfg: dict, ref: str, project_ref: str | None = None) -> dict:
    """Resolve 'SPK-12', a bare sequence number, or a UUID to a full issue."""
    ref = (ref or "").strip()
    if not ref:
        raise PlaneError("empty issue reference")

    if re.fullmatch(_UUID, ref):
        for p in allowed_projects(cfg):
            try:
                found = _request("GET", f"/projects/{p['id']}/issues/{ref}/", cfg)
            except PlaneError as exc:
                # 404 means "not in this project"; anything else (401, 5xx,
                # network) must surface, or a bad token reads as "not found".
                if "-> 404" in str(exc):
                    continue
                raise
            if isinstance(found, dict):
                return found
        raise PlaneError(f"issue {ref} not found in any allowed project")

    # A project identifier may itself contain digits (WEB3-12), so a separator
    # is required after a digit; the separatorless spelling (SPK12) stays
    # supported for the letters-only case.
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)[-\s](\d+)", ref) or re.fullmatch(
        r"([A-Za-z]+)(\d+)", ref
    )
    if m:
        project_ref = project_ref or m.group(1)
        seq = int(m.group(2))
    elif ref.isdigit():
        seq = int(ref)
    else:
        raise PlaneError(
            f"cannot parse issue reference {ref!r} — expected SPK-12, 12, or a UUID"
        )

    proj = resolve_project(cfg, project_ref)
    for issue in _paginate(
        f"/projects/{proj['id']}/issues/",
        cfg,
        params={"expand": "state,assignees,labels"},
    ):
        if issue.get("sequence_id") == seq:
            return issue
    # Says what was actually checked. The scan covers the issue list endpoint,
    # which is not the same set as "every issue that ever existed in the
    # project": deleted and otherwise unlisted issues are invisible to it, so
    # claiming the number does not exist would be a stronger statement than the
    # evidence in hand.
    raise PlaneError(
        f"no issue with sequence {seq} listed by the API in "
        f"{proj.get('identifier')} ({proj.get('name')}) — deleted or otherwise "
        "unlisted issues would not appear here"
    )


def resolve_state(cfg: dict, project_id: str, name: str) -> str:
    states = list(_paginate(f"/projects/{project_id}/states/", cfg))
    needle = name.strip().lower()
    for s in states:
        if s["id"].lower() == needle or (s.get("name") or "").lower() == needle:
            return s["id"]
    avail = ", ".join(s.get("name", "?") for s in states)
    raise PlaneError(f"no state matching {name!r}. Available: {avail}")


def _parse_ts(value: str) -> datetime:
    """Parse a Plane timestamp. `Z` is not something fromisoformat accepts.

    A date-only value (legacy self-hosted cycle bounds) parses naive; it is
    read as UTC so it stays comparable with tz-aware instants instead of
    raising TypeError.
    """
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# An empty cycle list is not proof that the project has none. Verified against
# Plane Cloud on 2026-08-02: with a token whose user is not a *project* member,
# /cycles/ answers 200 with zero results and the cycle detail 404s, while the
# same token reads that project's issues and can even create a cycle in it.
# Adding the user to the project made four already-existing cycles appear.
_NO_CYCLES = (
    "this project lists no cycles. Note that Plane returns an empty cycle list "
    "when the token's user is not a member of the project, so this is not "
    "proof that none exist"
)


def list_cycles(cfg: dict, project_id: str) -> list[dict]:
    return list(_paginate(f"/projects/{project_id}/cycles/", cfg))


def cycle_is_live(cycle: dict, now: datetime) -> bool:
    """Whether `now` falls inside the cycle, compared as instants.

    Plane's bounds are full timestamps, not dates: a sprint ends
    2026-08-03T21:59:00Z and the next starts 2026-08-03T22:00:01Z (local
    midnight). Truncating them to ten characters makes both cycles match on
    every changeover day, and the winner becomes whichever the API listed
    first — a ticket filed that day lands in the wrong sprint.
    """
    if not cycle.get("start_date") or not cycle.get("end_date"):
        return False
    return _parse_ts(cycle["start_date"]) <= now <= _parse_ts(cycle["end_date"])


def active_cycle(cfg: dict, project_id: str) -> dict:
    now = datetime.now(timezone.utc)
    cycles = list_cycles(cfg, project_id)
    if not cycles:
        raise PlaneError(_NO_CYCLES)
    live = [c for c in cycles if cycle_is_live(c, now)]
    if not live:
        raise PlaneError(
            "no cycle is active right now in this project — name one with "
            "--cycle, or check the sprint dates in Plane"
        )
    if len(live) > 1:
        names = ", ".join(f"{c.get('name')} [{c['id'][:8]}]" for c in live)
        raise PlaneError(
            f"{len(live)} cycles are active at the same time ({names}). That is a "
            "project configuration problem; pick one explicitly with --cycle "
            "rather than letting this tool guess"
        )
    return live[0]


def resolve_cycle(cfg: dict, project_id: str, ref: str) -> dict:
    """Resolve 'active', a cycle name, or a cycle UUID."""
    needle = (ref or "").strip().lower()
    if needle in ("", "active"):
        return active_cycle(cfg, project_id)
    cycles = list_cycles(cfg, project_id)
    hits = [
        c for c in cycles
        if needle in (c["id"].lower(), (c.get("name") or "").strip().lower())
    ]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        if not cycles:
            raise PlaneError(_NO_CYCLES)
        avail = ", ".join(c.get("name", "?") for c in cycles)
        raise PlaneError(f"no cycle matching {ref!r}. Available: {avail}")
    raise PlaneError(f"{len(hits)} cycles are named {ref!r} — use the cycle UUID")


def issue_cycle_id(issue: dict) -> str | None:
    """The cycle an issue belongs to, read from the only serializer that tells.

    Two traps here. There is no `cycle` key on an issue, only `cycle_id`, so
    `issue.get("cycle")` reads as "not in a cycle" for every issue. And the
    cycle-issues list serializer returns `cycle_id: null` even for the issues it
    is listing as members, so membership cannot be read from there either.
    """
    value = issue.get("cycle_id")
    return str(value) if value else None


def estimate_points(cfg: dict, project_id: str) -> list[dict]:
    """The project's estimate points, lowest key first.

    Two calls, because `/estimates/` answers a single object rather than a list
    or a paginated envelope: `_paginate` would walk away with nothing.
    """
    try:
        est = _request("GET", f"/projects/{project_id}/estimates/", cfg)
    except PlaneError as exc:
        if "-> 404" in str(exc):
            raise PlaneError(
                "this project has no estimate set — enable one in Plane under "
                "Project settings -> Estimates"
            ) from None
        raise
    if not isinstance(est, dict) or not est.get("id"):
        raise PlaneError("this project has no estimate set")
    points = _request(
        "GET", f"/projects/{project_id}/estimates/{est['id']}/estimate-points/", cfg
    )
    return sorted(points or [], key=lambda p: p.get("key") or 0)


def resolve_estimate_point(cfg: dict, project_id: str, value: str) -> str:
    """Estimate value ("3") or point UUID -> point UUID. `value` is a string."""
    needle = str(value).strip().lower()
    points = estimate_points(cfg, project_id)
    for p in points:
        if needle in (p["id"].lower(), str(p.get("value") or "").strip().lower()):
            return p["id"]
    avail = ", ".join(str(p.get("value")) for p in points)
    raise PlaneError(f"no estimate point {value!r}. Available: {avail}")


def project_members(cfg: dict, project_id: str) -> list[dict]:
    """Members with their role on this project, which is the scope that matters."""
    return list(_paginate(f"/projects/{project_id}/members/", cfg))


def resolve_member(cfg: dict, project_id: str, ref: str) -> str:
    """Display name, email, full name or UUID -> member UUID.

    Ambiguity is refused, never resolved by picking the first: assigning a
    ticket to the wrong person is silent and nobody notices for a sprint.
    """
    needle = (ref or "").strip().lower()
    members = project_members(cfg, project_id)
    for m in members:
        if str(m.get("id", "")).lower() == needle:
            return m["id"]

    hits = []
    for m in members:
        if m.get("is_bot") or not m.get("is_active", True):
            continue
        names = {
            (m.get("display_name") or "").lower(),
            (m.get("email") or "").lower(),
            (m.get("first_name") or "").lower(),
            f"{m.get('first_name', '')} {m.get('last_name', '')}".strip().lower(),
        }
        if needle in names:
            hits.append(m)
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        avail = ", ".join(
            m.get("display_name") or m.get("email") or "?"
            for m in members
            if not m.get("is_bot")
        )
        raise PlaneError(f"no project member matching {ref!r}. Members: {avail}")
    mails = ", ".join(m.get("email") or m["id"] for m in hits)
    raise PlaneError(f"{len(hits)} members match {ref!r} ({mails}) — use the email")


def resolve_label(
    cfg: dict, project_id: str, name: str, *, create: bool = False, dry_run: bool = False
) -> str | None:
    """Label name or UUID -> label UUID, optionally creating a missing one.

    Returns None only under --dry-run for a label that would have to be created:
    there is no id to send yet, and inventing one would make the printed payload
    a lie.
    """
    needle = (name or "").strip().lower()
    if not needle:
        raise PlaneError("empty label name")
    for x in _paginate(f"/projects/{project_id}/labels/", cfg):
        if needle in (x["id"].lower(), (x.get("name") or "").strip().lower()):
            return x["id"]
    if not create:
        raise PlaneError(
            f"no label named {name!r} in this project — pass --create-labels to "
            "create it, or run `labels` to see what exists"
        )
    if dry_run:
        print(f"[dry-run] POST label {name!r} (new)")
        return None
    made = _request(
        "POST", f"/projects/{project_id}/labels/", cfg, body={"name": name.strip()}
    )
    if not isinstance(made, dict) or not made.get("id"):
        raise PlaneError(f"creating label {name!r} returned no id: {made!r}")
    return made["id"]


# Plane Cloud stores a pasted image as an inline node in description_html with
# the asset UUID in src, and leaves issue-attachments/ empty. <img> is matched
# too because self-hosted editors have used it.
_ASSET_SRC = re.compile(r"<(?:image-component|img)\b[^>]*\bsrc=\"([^\"]+)\"", re.I)


def inline_asset_ids(issue: dict) -> list[str]:
    """Asset UUIDs referenced inside an issue description, in order."""
    found: list[str] = []
    for src in _ASSET_SRC.findall(issue.get("description_html") or ""):
        m = re.fullmatch(_UUID, src.strip())
        if m and m.group(0).lower() not in found:
            found.append(m.group(0).lower())
    return found


def asset_meta(cfg: dict, asset_id: str) -> dict:
    """Name, type and a one-hour presigned URL for an asset. Inside the chokepoint."""
    data = _request("GET", f"/assets/{asset_id}/", cfg)
    if not isinstance(data, dict) or not data.get("asset_url"):
        raise PlaneError(f"asset {asset_id} returned no url: {data!r}")
    return data


def _safe_filename(name: str, fallback: str) -> str:
    """A filename from the API is untrusted input; it must not steer the write."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return base or fallback


_ASSET_MAX_BYTES = 32 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_asset(url: str, base: str) -> bytes:
    """The one network call that does not go through `_request`, and why.

    A presigned S3 URL is already authorised — it carries its own signature and
    expires in an hour — so this fetch must be anonymous. It matters more than
    it sounds: urllib's redirect handler copies every header except
    content-length and content-type onto the redirected request, host change
    included, so following a 302 with `X-API-Key` set hands the Plane token to
    amazonaws.com. (`requests` strips `Authorization` cross-host but not custom
    headers, so the same trap catches the obvious rewrite.)

    Hence: no credential header, redirects refused instead of followed, https on
    the API host or an *.amazonaws.com host only (any bucket — an anonymous GET
    carries nothing worth stealing), and a size cap.
    """
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    base_host = (urllib.parse.urlparse(base).hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == base_host or host.endswith(".amazonaws.com")
    ):
        raise PlaneError(
            f"refusing to download an asset from {host or url!r} — not a host "
            "Plane serves assets from"
        )

    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _UA)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=60) as resp:
            blob = resp.read(_ASSET_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PlaneError(f"asset download -> {exc.code} (redirects are not followed)") from None
    except urllib.error.URLError as exc:
        raise PlaneError(f"asset download -> {exc.reason}") from None
    if len(blob) > _ASSET_MAX_BYTES:
        raise PlaneError(f"asset is larger than {_ASSET_MAX_BYTES} bytes — refused")
    return blob


def slim(issue: dict, identifier: str = "") -> dict:
    """List-view projection. Drops description_html, which dwarfs everything else."""
    state = issue.get("state")
    state_name = state.get("name") if isinstance(state, dict) else state

    assignees = issue.get("assignees") or []
    if assignees and isinstance(assignees[0], dict):
        who = [a.get("display_name") or a.get("email") for a in assignees]
    else:
        who = list(assignees)

    labels = issue.get("labels") or []
    if labels and isinstance(labels[0], dict):
        label_names = [x.get("name") for x in labels]
    else:
        label_names = list(labels)

    seq = issue.get("sequence_id")
    return {
        "ref": f"{identifier}-{seq}" if identifier and seq else seq,
        "id": issue.get("id"),
        "name": issue.get("name"),
        "state": state_name,
        "priority": issue.get("priority"),
        "assignees": who,
        "labels": label_names,
        "target_date": issue.get("target_date"),
        "updated_at": issue.get("updated_at"),
    }


# --- Output ------------------------------------------------------------------


def _cell(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    return "" if v is None else str(v)


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = {c: max([len(c)] + [len(_cell(r.get(c))) for r in rows]) for c in columns}
    out = ["  ".join(c.upper().ljust(widths[c]) for c in columns).rstrip()]
    out.append("  ".join("-" * widths[c] for c in columns).rstrip())
    for r in rows:
        out.append("  ".join(_cell(r.get(c)).ljust(widths[c]) for c in columns).rstrip())
    return "\n".join(out)


def _emit(data, args, columns=None):
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif columns and isinstance(data, list):
        print(_table(data, columns))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _body_text(args) -> str | None:
    path = getattr(args, "body_file", None)
    if path:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return getattr(args, "body", None)


# --- Commands ----------------------------------------------------------------


def cmd_check(cfg, _args):
    """Show resolved config and prove the guardrail blocks what it claims to."""
    print(f"base url  : {cfg['base']}")
    print(f"workspace : {cfg['workspace']}")
    # Length only, never a prefix: `check` is the first command every session
    # runs, and its output lands in transcripts and CI logs.
    print(f"token     : set ({len(cfg['token'])} chars)")
    print("allowlist :")
    for pid, label in cfg["allowed"].items():
        print(f"  - {label}  [{pid}]")

    projects = list_projects(cfg)
    print(f"\nprojects visible to this token ({len(projects)}):")
    for p in projects:
        flag = "ALLOWED" if p["_allowed"] else "BLOCKED"
        print(f"  {flag:8} {p.get('identifier','?'):6} {p.get('name')}  [{p['id']}]")

    unknown = [p for p in cfg["allowed"] if p not in {q["id"].lower() for q in projects}]
    for pid in unknown:
        print(f"  MISSING  allowlisted project {pid} is not visible to this token")

    print("\nguardrail self-test:")
    negative: list[tuple[str, str, str]] = []
    for p in (q for q in projects if not q["_allowed"]):
        negative.append(("GET", f"/projects/{p['id']}/issues/", f"read {p.get('name')}"))
        negative.append(("POST", f"/projects/{p['id']}/issues/", f"write {p.get('name')}"))
        negative.append(
            ("PATCH", f"/projects/{p['id']}/issues/{'0'*8}-0000-0000-0000-{'0'*12}/",
             f"patch an issue in {p.get('name')}")
        )
    ok_id = next(iter(cfg["allowed"]))
    # A synthetic UUID nobody allowlisted, so the negative cases exist even when
    # the token happens to see only allowed projects — otherwise `check` could
    # certify the guardrail without once trying to get past it.
    off_id = "00000000-0000-0000-0000-000000000000"
    negative.append(("GET", f"/projects/{off_id}/issues/", "read a project not in the allowlist"))
    negative.append(
        ("GET", f"/projects/{ok_id}/issues/?project_id={off_id}",
         "filter by ?project_id=<not allowlisted>")
    )
    negative.append(
        ("POST", f"/projects/{ok_id}/../{off_id}/issues/",
         "walk out of an allowed project with /../")
    )
    negative.append(
        ("GET", f"/projects/{off_id.replace('-', '')}/issues/",
         "read a blocked project via a hyphenless UUID")
    )
    negative.append(
        ("POST", f"/projects/{ok_id}" + "\\..\\" + f"{off_id}/issues/",
         "walk out of an allowed project with \\..\\")
    )
    negative.append(("DELETE", f"/projects/{ok_id}/", "delete an allowed project"))
    negative.append(("PATCH", f"/projects/{ok_id}/", "rename an allowed project"))
    negative.append(
        ("DELETE", f"/projects/{ok_id}//",
         "delete an allowed project via a trailing double slash")
    )
    negative.append(
        # "%25252530" is "0" percent-encoded four times: three decode passes
        # leave "%30", which must be refused, not waved through UUID-less.
        ("GET", f"/projects/%25252530{off_id[1:]}/issues/",
         "read via a path still percent-encoded after 3 passes")
    )
    negative.append(
        ("GET", f"/projects/{ok_id}/issues/?module={off_id}",
         "filter by a non-'project' query key carrying a blocked UUID")
    )
    negative.append(("POST", "/projects/", "create a project"))
    negative.append(("POST", "/issues/search/", "write via a workspace-scoped path"))

    failures = 0
    for method, path, label in negative:
        try:
            _guard(method, path, cfg["allowed"])
        except GuardError:
            print(f"  blocked  {method:6} {label}")
        else:
            print(f"  LEAKED   {method:6} {label}   <-- guardrail failed")
            failures += 1

    positive = [
        ("GET", f"/projects/{ok_id}/issues/", "read an allowed project"),
        ("POST", f"/projects/{ok_id}/issues/", "create an issue in an allowed project"),
        ("GET", "/members/", "read workspace members"),
    ]
    for method, path, label in positive:
        try:
            _guard(method, path, cfg["allowed"])
            print(f"  allowed  {method:6} {label}")
        except GuardError:
            print(f"  BROKEN   {method:6} {label}   <-- too strict")
            failures += 1

    # The cases above prove `_guard` is written correctly. They say nothing
    # about whether anything still calls it: the safety claim lives on one line
    # inside `_request`, and deleting that line leaves every check above green.
    # These go through `_request` itself, as dry-run writes — which the guard is
    # supposed to refuse before the dry-run branch is even reached, so anything
    # printed here is a leak.
    print("\nchokepoint self-test (through _request):")
    chokepoint = [
        ("POST", f"/projects/{off_id}/issues/", "write a project not in the allowlist"),
        ("POST", "/issues/search/", "write via a workspace-scoped path"),
        ("DELETE", f"/projects/{ok_id}/", "delete an allowed project"),
    ]
    for method, path, label in chokepoint:
        swallowed = io.StringIO()
        try:
            with redirect_stdout(swallowed):
                _request(method, path, cfg, body={}, dry_run=True)
        except GuardError:
            print(f"  blocked  {method:6} {label}")
        else:
            print(f"  LEAKED   {method:6} {label}   <-- _request did not consult the guard")
            failures += 1

    total = len(negative) + len(positive) + len(chokepoint)
    print(f"\n{'FAIL' if failures else 'PASS'}: {total} checks, {failures} failure(s)")
    return 1 if failures else 0


def cmd_projects(cfg, args):
    rows = [
        {
            "identifier": p.get("identifier"),
            "name": p.get("name"),
            "access": "allowed" if p["_allowed"] else "blocked",
            "id": p["id"],
        }
        for p in list_projects(cfg)
    ]
    if not args.all:
        rows = [r for r in rows if r["access"] == "allowed"]
    _emit(rows, args, ["identifier", "name", "access", "id"])


def cmd_issues(cfg, args):
    proj = resolve_project(cfg, args.project)
    pages = _paginate(
        f"/projects/{proj['id']}/issues/",
        cfg,
        params={"expand": "state,assignees,labels"},
    )
    needle = args.state.lower() if args.state else None

    issues = []
    for issue in pages:
        if needle is not None:
            state = issue.get("state")
            if not isinstance(state, dict):
                continue
            if (state.get("name") or "").lower() != needle:
                continue
        issues.append(issue)
        # Stop hitting the API once --limit is satisfied. Note this makes the
        # result "the first N the API returns", not "the N lowest refs".
        if args.limit and len(issues) >= args.limit:
            break

    issues.sort(key=lambda i: i.get("sequence_id") or 0)

    if args.full:
        _emit(issues, args)
        return
    rows = [slim(i, proj.get("identifier", "")) for i in issues]
    _emit(rows, args, ["ref", "name", "state", "priority", "assignees", "target_date"])


def cmd_get(cfg, args):
    issue = resolve_issue(cfg, args.ref, args.project)
    if args.json:
        print(json.dumps(issue, indent=2, ensure_ascii=False, default=str))
        return

    pid = _project_id_of(issue)
    ident = next(
        (
            p.get("identifier", "")
            for p in list_projects(cfg)
            if p["id"].lower() == pid.lower()
        ),
        "",
    )
    s = slim(issue, ident)
    print(f"{s['ref'] or issue.get('sequence_id')}  {issue.get('name')}")
    print(f"state    : {s['state']}")
    print(f"priority : {issue.get('priority')}")
    print(f"assignees: {_cell(s['assignees']) or '-'}")
    print(f"labels   : {_cell(s['labels']) or '-'}")
    print(f"target   : {issue.get('target_date') or '-'}")
    print(f"uuid     : {issue.get('id')}")
    body = html_to_text(issue.get("description_html") or "")
    if body:
        print("\n--- description ---")
        print(body)


def _apply_assignment_flags(cfg, args, project_id: str, payload: dict) -> None:
    """Resolve --assignee / --label / --estimate into issue fields.

    All three are whole-list writes, like --body: what you pass is what the
    issue ends up with. Repeat the flag for several values.
    """
    if args.assignee:
        payload["assignees"] = [
            resolve_member(cfg, project_id, who) for who in args.assignee
        ]
    if args.label:
        ids = [
            resolve_label(
                cfg, project_id, name,
                create=args.create_labels, dry_run=args.dry_run,
            )
            for name in args.label
        ]
        payload["labels"] = [x for x in ids if x]
    if args.estimate:
        payload["estimate_point"] = resolve_estimate_point(
            cfg, project_id, args.estimate
        )


def _place_in_cycle(cfg, project_id: str, issue: dict | None, ref: str, *, dry_run=False):
    """Add an issue to a cycle. Membership is a POST, not a field on the issue."""
    cycle = resolve_cycle(cfg, project_id, ref)
    if issue is None:
        print(f"[dry-run] would add the new issue to cycle {cycle.get('name')!r}")
        return
    if issue_cycle_id(issue) == cycle["id"]:
        print(f"already in cycle {cycle.get('name')}")
        return
    _request(
        "POST", f"/projects/{project_id}/cycles/{cycle['id']}/cycle-issues/", cfg,
        body={"issues": [issue["id"]]}, dry_run=dry_run,
    )
    if not dry_run:
        print(f"added to cycle {cycle.get('name')}")


def cmd_create(cfg, args):
    proj = resolve_project(cfg, args.project)
    payload: dict = {"name": sanitize(args.title)}
    body = _body_text(args)
    if body:
        payload["description_html"] = md_to_html(body)
    if args.priority:
        payload["priority"] = args.priority
    if args.state:
        payload["state"] = resolve_state(cfg, proj["id"], args.state)
    if args.target_date:
        payload["target_date"] = args.target_date
    _apply_assignment_flags(cfg, args, proj["id"], payload)

    result = _request(
        "POST", f"/projects/{proj['id']}/issues/", cfg,
        body=payload, dry_run=args.dry_run,
    )
    # Keyed on the flag, not on `result`: _request also returns None for a
    # real 2xx with an empty body, and that create must not read as a dry-run.
    if args.dry_run:
        if args.cycle:
            _place_in_cycle(cfg, proj["id"], None, args.cycle, dry_run=True)
        return
    if not isinstance(result, dict):
        print(
            "created, but the server answered with no body — "
            "run `issues` to find the new ref"
        )
        if args.cycle:
            print(
                "not added to the cycle: the response carried no issue id",
                file=sys.stderr,
            )
        return
    print(
        f"created {proj.get('identifier')}-{result.get('sequence_id')}  "
        f"{result.get('name')}"
    )
    print(f"uuid: {result.get('id')}")
    if args.cycle:
        _place_in_cycle(cfg, proj["id"], result, args.cycle)


def cmd_update(cfg, args):
    issue = resolve_issue(cfg, args.ref, args.project)
    pid = _project_id_of(issue)

    payload: dict = {}
    if args.title:
        payload["name"] = sanitize(args.title)
    body = _body_text(args)
    if body:
        payload["description_html"] = md_to_html(body)
    if args.priority:
        payload["priority"] = args.priority
    if args.state:
        payload["state"] = resolve_state(cfg, pid, args.state)
    if args.target_date:
        payload["target_date"] = args.target_date
    _apply_assignment_flags(cfg, args, pid, payload)
    if not payload and not args.cycle:
        raise PlaneError(
            "nothing to update — pass --title/--body/--priority/--state/"
            "--target-date/--assignee/--label/--estimate/--cycle"
        )

    if payload:
        result = _request(
            "PATCH", f"/projects/{pid}/issues/{issue['id']}/", cfg,
            body=payload, dry_run=args.dry_run,
        )
        if not args.dry_run:
            name = result.get("name") if isinstance(result, dict) else issue.get("name")
            print(f"updated {name}  ({', '.join(payload)})")
    if args.cycle:
        _place_in_cycle(cfg, pid, issue, args.cycle, dry_run=args.dry_run)


def cmd_comment(cfg, args):
    issue = resolve_issue(cfg, args.ref, args.project)
    pid = _project_id_of(issue)
    text = _body_text(args) or args.text
    if not text:
        raise PlaneError("no comment text — pass TEXT, --body or --body-file")

    result = _request(
        "POST", f"/projects/{pid}/issues/{issue['id']}/comments/", cfg,
        body={"comment_html": md_to_html(text)}, dry_run=args.dry_run,
    )
    if not args.dry_run:
        cid = result.get("id") if isinstance(result, dict) else "?"
        print(f"commented on {issue.get('name')} (comment {cid})")


def cmd_comments(cfg, args):
    issue = resolve_issue(cfg, args.ref, args.project)
    pid = _project_id_of(issue)
    items = list(_paginate(f"/projects/{pid}/issues/{issue['id']}/comments/", cfg))

    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False, default=str))
        return
    if not items:
        print("(no comments)")
        return
    for c in items:
        who = c.get("actor_detail") or {}
        name = who.get("display_name") or who.get("email") or c.get("actor") or "?"
        print(f"--- {name}  {c.get('created_at', '')}")
        print(html_to_text(c.get("comment_html") or "") or "(empty)")
        print()


def cmd_states(cfg, args):
    proj = resolve_project(cfg, args.project)
    rows = [
        {
            "name": s.get("name"),
            "group": s.get("group"),
            "default": s.get("default"),
            "id": s["id"],
        }
        for s in _paginate(f"/projects/{proj['id']}/states/", cfg)
    ]
    _emit(rows, args, ["name", "group", "default", "id"])


def cmd_labels(cfg, args):
    proj = resolve_project(cfg, args.project)
    rows = [
        {"name": x.get("name"), "color": x.get("color"), "id": x["id"]}
        for x in _paginate(f"/projects/{proj['id']}/labels/", cfg)
    ]
    _emit(rows, args, ["name", "color", "id"])


def cmd_cycles(cfg, args):
    now = datetime.now(timezone.utc)
    proj = resolve_project(cfg, args.project)
    rows = [
        {
            "name": c.get("name"),
            "active": "yes" if cycle_is_live(c, now) else "",
            "start": c.get("start_date"),
            "end": c.get("end_date"),
            "issues": c.get("total_issues"),
            "id": c["id"],
        }
        for c in list_cycles(cfg, proj["id"])
    ]
    _emit(rows, args, ["name", "active", "start", "end", "issues", "id"])
    if not rows:
        print(f"\n({_NO_CYCLES})", file=sys.stderr)
    if sum(1 for r in rows if r["active"]) > 1:
        print(
            "\n(more than one cycle is active at this instant — "
            "--cycle active will refuse to choose)",
            file=sys.stderr,
        )


def cmd_estimates(cfg, args):
    proj = resolve_project(cfg, args.project)
    rows = [
        {"value": p.get("value"), "key": p.get("key"), "id": p["id"]}
        for p in estimate_points(cfg, proj["id"])
    ]
    _emit(rows, args, ["value", "key", "id"])


def cmd_attachments(cfg, args):
    """List an issue's attachments, both kinds, and optionally download them.

    Two kinds because there are two: an attachment record under
    issue-attachments/, and an image pasted into the description, which Plane
    Cloud stores as an inline asset and never lists as an attachment.
    """
    issue = resolve_issue(cfg, args.ref, args.project)
    pid = _project_id_of(issue)
    # The list projection has no description_html; re-read the issue itself.
    detail = _request("GET", f"/projects/{pid}/issues/{issue['id']}/", cfg) or issue

    rows = []
    for rec in _paginate(f"/projects/{pid}/issues/{issue['id']}/issue-attachments/", cfg):
        attrs = rec.get("attributes") or {}
        rows.append(
            {
                "kind": "record",
                "name": attrs.get("name"),
                "type": attrs.get("type"),
                "asset": rec.get("asset") or rec.get("id"),
            }
        )
    for asset_id in inline_asset_ids(detail):
        meta = asset_meta(cfg, asset_id)
        rows.append(
            {
                "kind": "inline",
                "name": meta.get("asset_name"),
                "type": meta.get("asset_type"),
                "asset": asset_id,
            }
        )

    if not args.download:
        _emit(rows, args, ["kind", "name", "type", "asset"])
        return

    os.makedirs(args.download, exist_ok=True)
    taken: set[str] = set()
    for n, row in enumerate(rows):
        asset_id = str(row["asset"] or "")
        if not re.fullmatch(_UUID, asset_id):
            print(f"skipped {row['name']!r}: no asset id to fetch", file=sys.stderr)
            continue
        meta = asset_meta(cfg, asset_id)
        blob = fetch_asset(meta["asset_url"], cfg["base"])
        # Two attachments may sanitise to the same name; suffix instead of
        # silently overwriting the first with the second.
        name = _safe_filename(meta.get("asset_name"), f"asset-{n}.bin")
        if name in taken:
            stem, dot, ext = name.partition(".")
            name = f"{stem}-{n}{dot}{ext}"
        taken.add(name)
        dest = os.path.join(args.download, name)
        with open(dest, "wb") as fh:
            fh.write(blob)
        print(f"{dest}  ({len(blob)} bytes)")


def cmd_members(cfg, args):
    # Paginated: a workspace with >100 members used to be silently truncated.
    items = list(_paginate("/members/", cfg))
    rows = [
        {
            "display_name": m.get("display_name")
            or f"{m.get('first_name', '')} {m.get('last_name', '')}".strip(),
            "email": m.get("email"),
            "id": m.get("id"),
        }
        for m in items
    ]
    _emit(rows, args, ["display_name", "email", "id"])


def _search_hit_project(hit: dict) -> str | None:
    """Project UUID of a search hit, or None if it cannot be determined.

    None means "unknown", and the caller must withhold the hit rather than
    assume it is in scope: if Plane ever renames this field, search must go
    quiet, not start printing other projects' issues.
    """
    value = hit.get("project_id") or hit.get("project")
    if isinstance(value, dict):
        value = value.get("id")
    found = re.fullmatch(_UUID, str(value or "").strip())
    return found.group(0).lower() if found else None


def cmd_search(cfg, args):
    """Search issues, then drop hits outside the allowlist.

    Plane's search endpoint is workspace-scoped, so it happily returns issues
    from projects you excluded. Filtering here is what keeps the allowlist
    meaningful for reads as well as writes.

    The query parameter is `search`. Passing `q` instead returns an empty list
    rather than an error, which reads exactly like "no matches".

    `limit` is sent because the server default is 10 and the endpoint is not
    cursor-paginated: without it a query with 29 matches returns 10 of them and
    nothing says so. In a tool whose job is "check before you open a duplicate",
    a quiet truncation produces exactly the wrong conclusion, so a result that
    lands on the limit is called out on stderr instead of being presented as
    the whole answer.
    """
    limit = getattr(args, "limit", None) or 100
    data = _request(
        "GET", "/issues/search/", cfg, params={"search": args.query, "limit": limit}
    )
    if isinstance(data, dict):
        hits = data.get("issues", data.get("results", []))
    else:
        hits = data or []

    kept, dropped, unattributable = [], 0, 0
    for h in hits:
        # Fail closed: _search_hit_project returns None when the hit cannot be
        # attributed, and None is never in the allowlist. An unrecognised
        # response shape therefore withholds instead of turning the filter off.
        pid = _search_hit_project(h)
        if pid not in cfg["allowed"]:
            dropped += 1
            if pid is None:
                unattributable += 1
            continue
        seq = h.get("sequence_id")
        ident = h.get("project__identifier") or ""
        kept.append(
            {
                "ref": f"{ident}-{seq}" if seq else "",
                "name": h.get("name"),
                "id": h.get("id"),
            }
        )
    _emit(kept, args, ["ref", "name", "id"])
    if dropped:
        note = f"\n({dropped} hit(s) outside the allowlist withheld"
        if unattributable:
            note += f", {unattributable} of them with no project in the response"
        print(note + ")", file=sys.stderr)
    if len(hits) >= limit:
        print(
            f"\nTRUNCATED: the server returned exactly the {limit}-hit limit, so "
            f"there are probably more matches. Re-run with --limit {limit * 2} "
            "before concluding anything from this list.",
            file=sys.stderr,
        )


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plane.py",
        description=(
            "Plane CLI restricted to the projects named in "
            "PLANE_ALLOWED_PROJECTS."
        ),
    )
    p.add_argument("--json", action="store_true", help="raw JSON output")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print writes instead of sending them",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, project=True):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        if project:
            sp.add_argument(
                "-p", "--project", help="project UUID, identifier (SPK) or name"
            )
        return sp

    add("check", cmd_check, "show config and self-test the guardrail", project=False)

    sp = add("projects", cmd_projects, "list projects", project=False)
    sp.add_argument(
        "--all", action="store_true", help="include blocked projects, marked as such"
    )

    sp = add("issues", cmd_issues, "list issues")
    sp.add_argument("--state", help="filter by state name")
    sp.add_argument("--limit", type=int, help="max rows")
    sp.add_argument("--full", action="store_true", help="full objects incl. description")

    sp = add("get", cmd_get, "show one issue (SPK-12 | 12 | uuid)")
    sp.add_argument("ref")

    def add_assignment_flags(sp):
        sp.add_argument(
            "--assignee", action="append",
            help="member by display name, email or UUID; repeat to set several",
        )
        sp.add_argument(
            "--label", action="append", help="label by name or UUID; repeat for several"
        )
        sp.add_argument(
            "--create-labels", action="store_true",
            help="create any --label that does not exist yet",
        )
        sp.add_argument("--estimate", help="estimate value, e.g. 3 (see `estimates`)")
        sp.add_argument("--cycle", help="cycle name, UUID, or 'active'")

    sp = add("create", cmd_create, "create an issue")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", help="markdown body")
    sp.add_argument("--body-file", help="markdown body from file ('-' for stdin)")
    sp.add_argument("--priority", choices=_PRIORITIES)
    sp.add_argument("--state", help="state name, e.g. Backlog")
    sp.add_argument("--target-date", help="YYYY-MM-DD")
    add_assignment_flags(sp)

    sp = add("update", cmd_update, "update an issue")
    sp.add_argument("ref")
    sp.add_argument("--title")
    sp.add_argument("--body", help="markdown body (replaces the description)")
    sp.add_argument("--body-file")
    sp.add_argument("--priority", choices=_PRIORITIES)
    sp.add_argument("--state", help="state name to move to")
    sp.add_argument("--target-date", help="YYYY-MM-DD")
    add_assignment_flags(sp)

    sp = add("comment", cmd_comment, "add a comment")
    sp.add_argument("ref")
    sp.add_argument("text", nargs="?", help="comment markdown")
    sp.add_argument("--body")
    sp.add_argument("--body-file")

    sp = add("comments", cmd_comments, "list comments on an issue")
    sp.add_argument("ref")

    add("states", cmd_states, "list workflow states")
    add("labels", cmd_labels, "list labels")
    add("members", cmd_members, "list workspace members", project=False)
    add("cycles", cmd_cycles, "list cycles, marking the active one")
    add("estimates", cmd_estimates, "list the project's estimate points")

    sp = add("attachments", cmd_attachments, "list or download an issue's attachments")
    sp.add_argument("ref")
    sp.add_argument("--download", metavar="DIR", help="download into DIR")

    sp = add(
        "search", cmd_search, "search issues (allowlisted projects only)", project=False
    )
    sp.add_argument("query")
    sp.add_argument(
        "--limit", type=int, default=100,
        help="max hits to ask the server for (default 100; its own default is 10)",
    )

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(_cfg(), args) or 0
    except GuardError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except PlaneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
