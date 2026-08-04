"""Fleet view — what every agent is doing, from files that already exist.

Running ten Claude Code sessions at once is only sustainable if walking away
from them is cheap, and walking away is expensive when the state of each one
lives in your head. Two stores already hold the answer and neither is readable:

* ``<data_dir>/sessions/*.json`` — the registry the lifecycle hooks maintain.
  Knows *where* and *when*: project, container hostname, cwd, and the last
  lifecycle event. Says nothing about what the session is doing.
* ``<data_dir>/checkpoints/<sid>/checkpoint.md`` — the 11-section checkpoint.
  Knows *what*: current intent, next action, involved files. Written only when
  a token band is crossed, so plenty of sessions have none.

This module joins them into one reading. It is **pure aggregation** — no LLM
call, no network, and nothing here is a source of truth. Delete both outputs
and re-run and they come back identical; anything that ever wrote *into*
``AGENTS.md`` as primary state would make it a fourth store that silently
disagrees with the other three.

The hook-path output is ``AGENTS.md`` — the full read, grouped by whether an
agent needs you, listing everything still on the board, idle tabs included.
The ranked console digest and ``fleet.json`` (see :mod:`lib.fleet_digest` and
:func:`fleet_json`) are further renderings of the same collection, produced
only when a human runs the ``fleet-status`` CLI.

``fleet.txt`` — the one-line status-bar count — is retired. A count with no
referent ("9 fronts · 4 need you") tells you there is a fire without telling
you where; the digest replaced it, and :func:`write_fleet_view` deletes any
leftover file so an old status line goes blank rather than stale.

All of it is a *reading*, not a rule: no thresholds to breach, no "too many
agents" warning, no recommendation. Just what is true right now.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from lib.fsio import atomic_write
from lib.session_registry import entry_disposition_block

if TYPE_CHECKING:  # pragma: no cover - annotations only
    # Type-only so the expensive collectors cannot reach the hook path by
    # accident. `lib.fleet_sources` shells out to git and gh; this module is
    # imported from session hooks and must stay a pure file read.
    from lib.fleet_sources.backlog import Backlog
    from lib.fleet_sources.git_repos import RepoState
    from lib.fleet_sources.jobs import BackgroundJob
    from lib.fleet_sources.prs import PRScan

logger = logging.getLogger(__name__)

AGENTS_FILENAME = "AGENTS.md"

# Written by pre-digest releases; write_fleet_view() deletes a leftover so an
# old status line reading it goes blank instead of showing a frozen count.
RETIRED_FLEET_FILENAME = "fleet.txt"

# A session quiet for longer than this is "idle" rather than "working".
# One day, because the unit that matters is "did I touch this today" — a
# 30-minute window would file half a working fleet as idle every lunch break.
IDLE_AFTER_HOURS = 24

# Two agents holding one file is only a collision while both are still in a
# position to write it. Same window as IDLE_AFTER_HOURS, and for the same
# reason: past a day, "we both touched this" is shared history, not a clash.
COLLISION_MAX_AGE_HOURS = IDLE_AFTER_HOURS

# Checkpoint sections this view reads. The other eight are for context
# rebuild, not for a fleet glance.
_INTENT = "Current intent"
_NEXT = "Next action"
_FILES = "Involved files"

_SECTION_RE = re.compile(r"^##[ \t]+(?P<name>.+?)[ \t]*$", re.MULTILINE)

# A path inside an "Involved files" bullet, in either shape the writer emits:
#   - `path/to/file.py` — why it matters
#   - path/to/file.py — why it matters
_BULLET_RE = re.compile(r"^[-*][ \t]+(?P<body>.+?)[ \t]*$", re.MULTILINE)


@dataclass
class Agent:
    """One session, joined across the registry and its checkpoint."""

    session_id: str
    project: str = ""
    hostname: str = ""
    cwd: str = ""
    branch: str = ""
    repo_root: str = ""           # the checkout containing cwd, "" outside git
    repo_id: str = ""             # main .git dir — shared across worktrees
    started_at: str = ""
    last_ts: datetime | None = None
    last_kind: str = ""
    status: str = "idle"          # working | waiting_input | idle | ended
    disposition: str = "active"   # active | parked | done — how it was LEFT
    disposition_reason: str = ""
    intent: str = ""
    next_action: str = ""
    files: list[str] = field(default_factory=list)
    has_checkpoint: bool = False

    @property
    def group(self) -> str:
        """Which heading this agent renders under, or ``""`` for not listed.

        Liveness and disposition are different axes, and this is the one place
        they have to meet. Disposition wins, because it is the only one of the
        two that carries the user's intent: a ``parked`` session stays on the
        list whatever its process did — that is what parking it means — and a
        ``done`` one is finished even if its container is still up.

        Deriving the grouping and :attr:`live` from one expression is what
        keeps the header count and the body in agreement.
        """
        if self.disposition == "parked":
            return "Parked"
        if self.disposition == "done" or self.status == "ended":
            return ""
        if self.status == "waiting_input":
            return "Needs you"
        if self.status == "working":
            return "Working"
        return "Idle"

    @property
    def live(self) -> bool:
        """Is this entry still on the board at all — i.e. does it render?"""
        return bool(self.group)

    @property
    def front(self) -> bool:
        """Does this agent have a claim on your attention right now?

        Narrower than :attr:`live`, and the distinction is the whole point of
        the one-line reading. ``Idle`` is live — a session quiet since Tuesday
        is still listed, and you may well want it listed — but it is not a
        front, because a count that folds it in answers "how many tabs have I
        opened lately" while appearing to answer "how many agents am I
        running". The second question is the one being asked, and the first
        one's answer is roughly ten times larger.
        """
        return self.group in _FRONT_GROUPS

    def age(self, now: datetime) -> timedelta:
        if self.last_ts is None:
            return timedelta(0)
        return max(timedelta(0), now - self.last_ts)


@dataclass
class Collision:
    """One file two or more live agents both have in hand."""

    path: str
    session_ids: list[str]
    labels: list[str]


@dataclass
class Fleet:
    """Every agent, plus — when a human asked — everything around them.

    The four extra fields are **empty on the hook path and populated only by
    :mod:`fleet_status`**, which a person ran on purpose. That asymmetry is
    deliberate: :func:`collect` reads local files and is cheap enough to run
    from a session hook, while the extra sources shell out to ``git`` and
    ``gh``. Keeping them optional on one dataclass — rather than forking into
    two — is what lets ``AGENTS.md``, the digest and ``fleet.json`` stay three
    renderings of one truth instead of three views that can disagree.

    ``None`` and ``[]`` mean different things here and the renderers rely on
    it: ``None`` is *not collected*, an empty list is *collected, nothing
    found*. "I didn't look" and "there is nothing" must never print the same.
    """

    agents: list[Agent]
    collisions: list[Collision]
    prs: "PRScan | None" = None
    repos: "list[RepoState] | None" = None
    jobs: "list[BackgroundJob] | None" = None
    backlog: "Backlog | None" = None
    # Scheduled routines live server-side; only a live Claude Code session can
    # enumerate them (CronList). The script cannot, so this stays None there —
    # and the digest says "not tracked" rather than implying zero.
    scheduled: list[str] | None = None

    def by_status(self, *statuses: str) -> list[Agent]:
        return [a for a in self.agents if a.status in statuses]

    def in_group(self, title: str) -> list[Agent]:
        return [a for a in self.agents if a.group == title]

    @property
    def live(self) -> list[Agent]:
        return [a for a in self.agents if a.live]

    @property
    def fronts(self) -> list[Agent]:
        return [a for a in self.agents if a.front]


# ---------------------------------------------------------------------------
# Reading the two stores
# ---------------------------------------------------------------------------

def _parse_ts(raw) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def split_sections(text: str) -> dict[str, str]:
    """Split a checkpoint into ``{section name: body}``.

    Tolerant by design: a checkpoint missing sections, carrying extra ones, or
    truncated mid-write still yields whatever it does have. A fleet view that
    crashes on a malformed checkpoint is worse than one that shows a partial
    entry.
    """
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group("name").strip()] = text[m.end():end].strip()
    return out


def _first_line(body: str) -> str:
    for line in body.splitlines():
        # Remove one leading bullet marker only — `lstrip("-*")` would also
        # eat the opening `**` of a bold intent.
        line = re.sub(r"^[-*][ \t]+", "", line.strip()).strip()
        if line:
            return line
    return ""


def parse_involved_files(body: str) -> list[str]:
    """Pull the file paths out of an ``Involved files`` section.

    Only tokens that look like paths are kept — the section's bullets carry a
    trailing prose description, and counting prose as a filename would invent
    collisions that don't exist.
    """
    paths: list[str] = []
    for m in _BULLET_RE.finditer(body):
        item = m.group("body").strip()
        if item.startswith("`"):
            close = item.find("`", 1)
            if close > 1:
                token = item[1:close]
            else:
                # Unterminated backtick — a truncated or hand-mangled bullet.
                # Keep only the first whitespace-delimited token so trailing
                # prose never becomes a "path" in the collision key space.
                rest = item[1:].split()
                token = rest[0] if rest else ""
        else:
            # Split on the em-dash/hyphen separator the writer uses, then take
            # the first whitespace-delimited token of what's left.
            token = re.split(r"\s+—\s+|\s+--?\s+", item, maxsplit=1)[0]
            token = token.split()[0] if token.split() else ""
        token = token.strip().strip("`,;")
        if not token:
            continue
        if "/" not in token and not token.startswith("~"):
            continue
        paths.append(token)
    # Preserve order, drop duplicates within one checkpoint.
    seen: set[str] = set()
    return [p for p in paths if not (p in seen or seen.add(p))]


def _git_info(cwd: str) -> tuple[str, str, str]:
    """``(branch label, checkout root, repo identity)`` for *cwd*, off disk.

    No subprocess: this may run from a hook-adjacent path, and `git` costs
    more than the answer is worth. A linked worktree's ``.git`` is a file
    pointing at ``<main>/.git/worktrees/<name>`` — those render as
    ``branch (worktree: name)`` because "which of my six worktrees is this"
    is exactly the question the fleet view exists to answer. A submodule's
    ``.git`` is *also* a file, but points at ``<parent>/.git/modules/<name>``
    — no ``worktrees/`` component, so it renders as a plain branch.

    The repo identity is the main ``.git`` directory shared by every worktree
    of one repo; :func:`find_collisions` keys on it so the same relative path
    in two worktrees intersects.
    """
    if not cwd:
        return "", "", ""
    try:
        here = Path(cwd)
        for candidate in [here, *here.parents]:
            dotgit = candidate / ".git"
            if not dotgit.exists():
                continue
            worktree = ""
            if dotgit.is_file():
                pointer = dotgit.read_text(encoding="utf-8", errors="replace").strip()
                if not pointer.startswith("gitdir:"):
                    return "", "", ""
                gitdir = Path(pointer.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    # Submodules record a relative gitdir pointer.
                    gitdir = (candidate / gitdir).resolve()
                if gitdir.parent.name == "worktrees":
                    worktree = gitdir.name
                    repo_id = str(gitdir.parent.parent)
                else:
                    repo_id = str(gitdir)
            else:
                gitdir = dotgit
                repo_id = str(dotgit)
            head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref: refs/heads/"):
                name = head[len("ref: refs/heads/"):]
            else:
                name = head[:8] or ""
            branch = f"{name} (worktree: {worktree})" if worktree else name
            return branch, str(candidate), repo_id
    except OSError:
        return "", "", ""
    return "", "", ""


def _status_of(kind: str, last_ts: datetime | None, now: datetime) -> str:
    """Map a registry entry onto the contracted liveness vocabulary.

    ``working | waiting_input | idle | ended`` is frozen in the multiplai-gui
    API contract; this derives the same four values from what the hooks
    record, rather than inventing a parallel notion of "needs you". A
    Notification hook fires precisely when Claude Code is waiting on the user,
    which is what ``waiting_input`` means.

    **Quiet is checked before kind, and that ordering is the whole point.**
    Only a clean quit fires ``SessionEnd``; a container killed by a reboot, a
    closed terminal, ``docker kill`` or the OOM killer records nothing at all,
    so the registry keeps entries whose last event is a Notification from two
    weeks ago. Read literally that is 24 agents waiting on you; read honestly
    it is one live prompt and 23 corpses. A "Needs you" list nobody can act on
    is worse than no list, because you stop reading it. The API contract's own
    fallback-discovery rule says the same thing: quiet is idle.

    Quiet is a *guess* at death, and deliberately the conservative one — the
    entry stays on the board as ``idle`` rather than being declared over,
    because the session may merely be thinking. Nothing outside a session can
    do better: a hook is code running inside one, so it cannot report its own
    process dying, and an external observer that could was measured to buy
    nothing here (see the 0.15.1 changelog).
    """
    if kind == "end":
        return "ended"
    if last_ts is None:
        return "idle"
    if (now - last_ts) >= timedelta(hours=IDLE_AFTER_HOURS):
        return "idle"
    return "waiting_input" if kind == "notification" else "working"


def load_agent(entry_path: Path, data_dir: Path, now: datetime) -> Agent | None:
    """Build one :class:`Agent` from a registry entry plus its checkpoint.

    A session with no checkpoint directory is a valid entry with the fields
    the registry does have — never dropped, never a crash. That case is the
    common one: the checkpoint writer only fires past a token band.
    """
    try:
        raw = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    sid = str(raw.get("session_id") or entry_path.stem).strip()
    if not sid:
        return None

    last = raw.get("last_event") or {}
    last_kind = str(last.get("kind") or "") if isinstance(last, dict) else ""
    last_ts = _parse_ts(last.get("ts")) if isinstance(last, dict) else None
    if last_ts is None:
        last_ts = _parse_ts(raw.get("started_at"))

    cwd = str(raw.get("cwd") or "")
    # One parse for both state and reason — two hand-rolled reads of the
    # same block is how they drift.
    disp_state, disp_reason = entry_disposition_block(raw)
    branch, repo_root, repo_id = _git_info(cwd)
    agent = Agent(
        session_id=sid,
        project=str(raw.get("project") or ""),
        hostname=str(raw.get("hostname") or ""),
        cwd=cwd,
        branch=branch,
        repo_root=repo_root,
        repo_id=repo_id,
        started_at=str(raw.get("started_at") or ""),
        last_ts=last_ts,
        last_kind=last_kind,
        status=_status_of(last_kind, last_ts, now),
        disposition=disp_state,
        disposition_reason=disp_reason,
    )

    cp = data_dir / "checkpoints" / sid / "checkpoint.md"
    try:
        text = cp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return agent

    agent.has_checkpoint = True
    sections = split_sections(text)
    agent.intent = _first_line(sections.get(_INTENT, ""))
    agent.next_action = _first_line(sections.get(_NEXT, ""))
    agent.files = parse_involved_files(sections.get(_FILES, ""))
    return agent


def _label(agent: Agent) -> str:
    """Short human handle for an agent in a collision line.

    The project name alone is not enough: the common collision is two
    worktrees of the *same* project, which renders as "workspace, workspace"
    and identifies nobody. Qualify with the worktree — that is what a tmux tab
    actually is here — falling back to the branch and then the session id.
    """
    project = agent.project or agent.hostname or agent.session_id[:8]
    branch = agent.branch
    if branch.endswith(")") and "(worktree: " in branch:
        qualifier = branch.split("(worktree: ", 1)[1][:-1]
    else:
        qualifier = branch or agent.session_id[:8]
    return f"{project}@{qualifier}"


def _collision_key(agent: Agent, path: str) -> tuple[tuple[str, str], str]:
    """``(intersection key, display path)`` for one involved-file entry.

    Checkpoints record **absolute** paths (the writer prompt mandates it), so
    the same logical file edited in two worktrees of one repo appears under
    two different prefixes — ``/ws/.worktrees/feat-a/src/x.py`` vs
    ``…/feat-b/src/x.py`` — and raw string intersection would never report the
    headline collision. Strip the agent's checkout root and key on
    ``(repo identity, relative path)``; the repo identity is the main ``.git``
    dir every worktree shares, so worktrees intersect while an unrelated repo's
    identical relative path does not. Paths outside any detected checkout keep
    the absolute string as the key, which still catches workspace-shared files.
    """
    root = agent.repo_root
    if root:
        prefix = root.rstrip("/") + "/"
        if path.startswith(prefix):
            rel = path[len(prefix):]
            return (agent.repo_id or root, rel), rel
    return ("", path), path


def find_collisions(agents: list[Agent], now: datetime | None = None) -> list[Collision]:
    """Files held by two or more agents that are **both still in play**.

    This is the overlapping-work anxiety answered without any agent talking to
    another agent: a set intersection over what each one already wrote down —
    normalized per-agent by :func:`_collision_key` so two worktrees of one
    repo collide on the logical file, not the raw string.

    "In play" is a front (:attr:`Agent.front`) that has been heard from within
    ``COLLISION_MAX_AGE_HOURS``, and both halves of that earn their keep.
    Against the real registry, filtering on liveness alone reported eight
    collisions of which eight were between pairs of sessions dead for three to
    eighteen days — a warning that is wrong every time is one you stop reading,
    which costs more than not having it. A collision is a claim that two agents
    might *now* write the same file; a dead one cannot, and neither can a
    parked one that has not moved since last week.
    """
    now = now or datetime.now(timezone.utc)
    max_age = timedelta(hours=COLLISION_MAX_AGE_HOURS)
    holders: dict[tuple[str, str], list[Agent]] = {}
    display: dict[tuple[str, str], str] = {}
    for agent in agents:
        if not agent.front or agent.age(now) > max_age:
            continue
        for path in agent.files:
            key, shown = _collision_key(agent, path)
            holders.setdefault(key, []).append(agent)
            display.setdefault(key, shown)

    out = []
    for key, owners in holders.items():
        if len(owners) < 2:
            continue
        labels = [_label(a) for a in owners]
        # Two tabs in the same worktree would still label identically; the
        # session id is the last resort that is always distinct.
        if len(set(labels)) < len(labels):
            labels = [f"{lb} ({a.session_id[:8]})" for lb, a in zip(labels, owners)]
        out.append(Collision(
            path=display[key],
            session_ids=[a.session_id for a in owners],
            labels=labels,
        ))
    out.sort(key=lambda c: c.path)
    return out


def collect(data_dir: Path, now: datetime | None = None) -> Fleet:
    """Read both stores and join them. Never raises on bad input."""
    now = now or datetime.now(timezone.utc)
    sessions_dir = data_dir / "sessions"
    agents: list[Agent] = []
    try:
        entries = sorted(sessions_dir.glob("*.json"))
    except OSError:
        entries = []
    for entry in entries:
        agent = load_agent(entry, data_dir, now)
        if agent is not None:
            agents.append(agent)

    # Newest first within a group; stable on session_id so two entries with
    # the same timestamp always render in the same order (the cache property
    # depends on this).
    agents.sort(key=lambda a: (-(a.last_ts or datetime.min.replace(
        tzinfo=timezone.utc)).timestamp(), a.session_id))
    return Fleet(agents=agents, collisions=find_collisions(agents, now))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def format_age(delta: timedelta) -> str:
    """Coarse age: ``3d`` / ``5h`` / ``12m`` / ``just now``."""
    secs = int(delta.total_seconds())
    if secs >= 86400:
        return f"{secs // 86400}d"
    if secs >= 3600:
        return f"{secs // 3600}h"
    if secs >= 60:
        return f"{secs // 60}m"
    return "just now"


# Only live agents get an entry. Finished ones — ended, or labelled `done` —
# are counted in the header and nothing more: against the real registry there
# were 105 of them to 35 live, so listing them turned a fleet view into a
# 700-line graveyard with the answer buried at the top. The registry GCs them
# within a week anyway, and "what did that finished session decide" is the
# diary's job, not this file's.
#
# Parked sits between Working and Idle deliberately. It is not urgent, but it
# is the pile you chose to come back to — burying it under two dozen tabs that
# merely went quiet would make parking pointless.
_GROUP_ORDER = ("Needs you", "Working", "Parked", "Idle")

# The groups that count as a *front* (see :attr:`Agent.front`): every group
# except Idle. Parked is in, and deliberately — its process is usually long
# gone, but "I am coming back to this" is a claim on you in a way that a tab
# which merely went quiet is not. That is the entire difference between
# parking something and abandoning it.
_FRONT_GROUPS = frozenset(_GROUP_ORDER) - {"Idle"}


def _sanitize_reason(reason: str) -> str:
    """Strip what would break AGENTS.md structure out of an LLM-quoted reason.

    The single-line regex and the registry's 500-char cap already bound the
    damage; this handles the remainder — a leading ``#`` would open a new
    heading, and ``|`` reads as a table cell to some renderers. Deliberately
    minimal: display sanitization, not general markdown escaping.
    """
    reason = reason.strip().lstrip("#").strip()
    return reason.replace("|", "/")


def _render_agent(agent: Agent, now: datetime) -> list[str]:
    head = agent.project or agent.cwd or agent.session_id[:8]
    lines = [f"### {head} — {format_age(agent.age(now))}"]
    meta = []
    if agent.hostname:
        meta.append(f"container `{agent.hostname}`")
    if agent.branch:
        meta.append(f"branch `{agent.branch}`")
    if agent.cwd:
        meta.append(f"cwd `{agent.cwd}`")
    meta.append(f"session `{agent.session_id}`")
    lines.append("- " + " · ".join(meta))
    if agent.disposition == "parked":
        # The reason is the user's own closing words, which say more about why
        # this is parked than any intent line reconstructed from the work.
        shown = _sanitize_reason(agent.disposition_reason)
        reason = f" — {shown}" if shown else ""
        lines.append(f"- **Parked**{reason}")
    if agent.intent:
        lines.append(f"- **Doing:** {agent.intent}")
    if agent.next_action:
        lines.append(f"- **Next:** {agent.next_action}")
    if agent.files:
        lines.append("- **Files:** " + ", ".join(f"`{p}`" for p in agent.files))
    if not agent.has_checkpoint:
        lines.append("- _No checkpoint — registry only._")
    lines.append("")
    return lines


def render_agents_md(fleet: Fleet, now: datetime, generated_at: str | None = None) -> str:
    """The full fleet read.

    Everything below the generation stamp is a function of the two stores
    plus *now* — rendered ages are coarse buckets (``5h`` / ``3d``) and
    statuses have a quiet threshold, so two runs straddling such a boundary
    can differ beyond the stamp. Between boundaries the output is
    byte-identical, which is what makes the file a cache rather than a record.
    """
    stamp = generated_at or now.isoformat()
    fronts = fleet.fronts
    needs = fleet.in_group("Needs you")
    idle = fleet.in_group("Idle")
    finished = [a for a in fleet.agents if not a.live]

    # Headline counts what has a claim on you; idle and finished are trailing
    # context. The status line reads the same way, and the two must agree —
    # a header saying "34 live" over a bar saying "5 fronts" is how you stop
    # trusting both.
    counts = (
        f"**{len(fronts)} front(s) · {len(needs)} need you · "
        f"{len(fleet.collisions)} collision(s)**"
    )
    if idle:
        counts += f" · {len(idle)} idle"
    if finished:
        counts += f" · {len(finished)} finished, not listed"

    out = [
        "# Agents",
        "",
        f"_Generated {stamp} — a cache of `sessions/` + `checkpoints/`. "
        "Nothing reads this file as state; delete it and it comes back._",
        "",
        counts,
        "",
    ]

    for title in _GROUP_ORDER:
        group = fleet.in_group(title)
        if not group:
            continue
        out.append(f"## {title} ({len(group)})")
        out.append("")
        for agent in group:
            out.extend(_render_agent(agent, now))

    out.append("## Collisions")
    out.append("")
    if not fleet.collisions:
        out.append("_None — no file is held by two agents still in play._")
        out.append("")
    else:
        for c in fleet.collisions:
            out.append(f"- `{c.path}` — {', '.join(c.labels)}")
        out.append("")

    out.extend(_render_extras(fleet))
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# The extra sections — only when someone paid for them
# ---------------------------------------------------------------------------

def _render_prs(scan: "PRScan") -> list[str]:
    out = ["## Pull requests", ""]
    if not scan.available:
        out += ["_`gh` is unavailable or unauthenticated — PRs not read. "
                "Install the GitHub CLI and run `gh auth login`._", ""]
        return out
    human, bots = scan.human, scan.bots
    if not scan.prs:
        out += ["_None open._", ""]
    else:
        out.append(f"**{len(human)} yours · {len(bots)} bot**")
        out.append("")
        for pr in sorted(human, key=lambda p: (p.repo, p.number)):
            bits = []
            if pr.is_draft:
                bits.append("draft")
            if pr.review_decision:
                bits.append(pr.review_decision.replace("_", " ").lower())
            bits.append(f"CI {pr.ci}")
            out.append(f"- `{pr.label}` {pr.title} — {' · '.join(bits)}  \n  {pr.url}")
        if bots:
            out.append(f"- _plus {len(bots)} bot PR(s) — dependency bumps, not listed._")
        out.append("")
    for slug, err in sorted(scan.errors.items()):
        out.append(f"- ⚠️ `{slug}` — {err}")
    if scan.errors:
        out.append("")
    if scan.no_access:
        out.append(
            f"_{len(scan.no_access)} repo(s) not visible to this GitHub token — "
            f"{', '.join(f'`{s}`' for s in scan.no_access)}. A standing fact "
            "about the credential, not a failure._"
        )
        out.append("")
    return out


def _render_repos(repos: "list[RepoState]") -> list[str]:
    out = ["## Repos", ""]
    noisy = [r for r in repos if not r.clean]
    if not noisy:
        out += [f"_All {len(repos)} checkout(s) clean, pushed, and tracking._", ""]
    else:
        out.append(f"**{len(noisy)} of {len(repos)} checkout(s) want something**")
        out.append("")
        for repo in noisy:
            bits = []
            if repo.dirty:
                bits.append(f"{repo.dirty} uncommitted ({repo.untracked} untracked)")
            if repo.unpushed:
                bits.append("unpushed: " + ", ".join(repo.unpushed))
            if repo.no_upstream:
                bits.append("never pushed: " + ", ".join(repo.no_upstream))
            if repo.error:
                bits.append(f"error: {repo.error}")
            out.append(f"- `{repo.path}` (on `{repo.branch}`) — {'; '.join(bits)}")
        out.append("")
    # Worktrees render regardless of cleanliness: a clean repo with a stale
    # linked worktree is exactly the "branch merged three weeks ago" case this
    # section exists to surface.
    worktrees = [(r.path, w) for r in repos for w in r.worktrees]
    if worktrees:
        out.append(f"**{len(worktrees)} linked worktree(s)**")
        out.append("")
        for owner, path in worktrees:
            out.append(f"- `{path}` — from `{owner}`")
        out.append("")
    return out


def _render_jobs(jobs: "list[BackgroundJob]") -> list[str]:
    out = ["## Background jobs", ""]
    if not jobs:
        out += ["_None recorded._", ""]
        return out
    running = [j for j in jobs if j.running]
    out.append(f"**{len(running)} running · {len(jobs) - len(running)} finished or stale**")
    out.append("")
    for job in jobs:
        if job.running:
            mark = "running"
        elif job.finished:
            mark = job.state
        else:
            # Stale is a *guess*: the pids in the roster belong to another
            # process namespace, so nothing here can confirm a death.
            mark = f"{job.state or 'unknown'}, stale"
        detail = f" — {job.detail}" if job.detail else ""
        out.append(f"- `{job.short}` [{mark}]{detail}")
    out.append("")
    return out


def _render_backlog(backlog: "Backlog") -> list[str]:
    out = ["## Backlog", ""]
    if backlog.empty:
        out += ["_Nothing pending._", ""]
        return out
    if backlog.learnings_lines:
        oldest = f", oldest {backlog.oldest_learning}" if backlog.oldest_learning else ""
        out.append(
            f"- {backlog.learnings_lines} learning line(s) across "
            f"{backlog.learnings_files} file(s){oldest} — `/multiplai-context:dream`"
        )
    if backlog.dreams_pending:
        out.append(
            f"- {backlog.dreams_pending} dream proposal(s) unapplied — "
            "`/multiplai-context:dream-remember`"
        )
    if backlog.pending_extractions:
        out.append(f"- {backlog.pending_extractions} extraction marker(s) not drained")
    if backlog.failed_extractions:
        out.append(f"- ⚠️ {backlog.failed_extractions} failed extraction(s) quarantined")
    if backlog.inbox_items:
        out.append(f"- {backlog.inbox_items} INBOX item(s) awaiting review")
    out.append("")
    return out


def _render_extras(fleet: Fleet) -> list[str]:
    """The sections that exist only when the expensive collectors ran.

    Absent sources render **nothing at all** rather than an empty section: on
    the hook path none of them are collected, and this is what keeps
    ``AGENTS.md`` byte-identical to its pre-digest shape there.
    """
    out: list[str] = []
    if fleet.prs is not None:
        out += _render_prs(fleet.prs)
    if fleet.repos is not None:
        out += _render_repos(fleet.repos)
    if fleet.jobs is not None:
        out += _render_jobs(fleet.jobs)
    if fleet.backlog is not None:
        out += _render_backlog(fleet.backlog)
    return out


# ---------------------------------------------------------------------------
# fleet.json — the machine rendering
# ---------------------------------------------------------------------------

FLEET_JSON_FILENAME = "fleet.json"

# Bumped when a field changes meaning or disappears. The multiplai hub is the
# intended second consumer, and a consumer that cannot tell which shape it is
# holding has to guess.
FLEET_JSON_VERSION = 1


def _jsonable(value):
    """Datetimes to ISO strings, dataclasses to dicts, recursively."""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _agent_json(agent: Agent, now: datetime) -> dict:
    data = {k: _jsonable(v) for k, v in asdict(agent).items()}
    data["group"] = agent.group
    data["front"] = agent.front
    data["age_seconds"] = int(agent.age(now).total_seconds())
    return data


def fleet_json(fleet: Fleet, now: datetime, generated_at: str | None = None) -> str:
    """Serialize a :class:`Fleet` for another program to render.

    Carries the derived properties — ``group``, ``front``, ``age_seconds`` —
    alongside the raw fields, because they encode rules (disposition beats
    liveness; idle is not a front) that a consumer re-deriving them would get
    subtly wrong. The hub rendering a different set of fronts from the digest
    would be worse than the hub having no fleet view at all.
    """
    payload = {
        "version": FLEET_JSON_VERSION,
        "generated_at": generated_at or now.isoformat(),
        "counts": {
            "agents": len(fleet.agents),
            "live": len(fleet.live),
            "fronts": len(fleet.fronts),
            "needs_you": len(fleet.in_group("Needs you")),
            "collisions": len(fleet.collisions),
        },
        "agents": [_agent_json(agent, now) for agent in fleet.agents],
        "collisions": _jsonable(fleet.collisions),
        # Preserved as null when not collected — see the Fleet docstring.
        "prs": _jsonable(fleet.prs),
        "repos": _jsonable(fleet.repos),
        "jobs": _jsonable(fleet.jobs),
        "backlog": _jsonable(fleet.backlog),
        "scheduled": _jsonable(fleet.scheduled),
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def write_fleet_view(data_dir: Path, now: datetime | None = None) -> Path:
    """Render ``AGENTS.md`` into *data_dir*; return its path.

    One function so the hook and the CLI cannot drift into rendering two
    different files. Also deletes a leftover ``fleet.txt`` from a pre-digest
    release: the kit status line renders whatever that file says, and a frozen
    count in every tab is worse than no segment at all.
    """
    now = now or datetime.now(timezone.utc)
    fleet = collect(data_dir, now)

    agents_path = data_dir / AGENTS_FILENAME
    atomic_write(agents_path, render_agents_md(fleet, now))
    try:
        (data_dir / RETIRED_FLEET_FILENAME).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a stale file is cosmetic, never fatal
        pass

    logger.info(
        "Fleet: %d front(s), %d listed of %d session(s), %d collision(s) → %s",
        len(fleet.fronts), len(fleet.live), len(fleet.agents),
        len(fleet.collisions), agents_path,
    )
    return agents_path
