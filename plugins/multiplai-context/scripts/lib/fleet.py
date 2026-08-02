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

Two outputs, deliberately different shapes:

* ``AGENTS.md`` — the full read, grouped by whether an agent needs you.
* ``fleet.txt`` — one short line for the terminal status bar, which re-renders
  constantly and can afford exactly one ``cat``.

Both are a *reading*, not a rule: no thresholds to breach, no "too many agents"
warning, no recommendation. Just what is true right now.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.fsio import atomic_write

logger = logging.getLogger(__name__)

AGENTS_FILENAME = "AGENTS.md"
FLEET_FILENAME = "fleet.txt"

# A session quiet for longer than this is "idle" rather than "working".
# One day, because the unit that matters is "did I touch this today" — a
# 30-minute window would file half a working fleet as idle every lunch break.
IDLE_AFTER_HOURS = 24

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
    intent: str = ""
    next_action: str = ""
    files: list[str] = field(default_factory=list)
    has_checkpoint: bool = False

    @property
    def live(self) -> bool:
        return self.status != "ended"

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
    agents: list[Agent]
    collisions: list[Collision]

    def by_status(self, *statuses: str) -> list[Agent]:
        return [a for a in self.agents if a.status in statuses]


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
    Containers get killed without a ``SessionEnd`` — reboot, ``docker kill``,
    OOM — so the registry keeps entries whose last event is a Notification
    from two weeks ago. Read literally that is 24 agents waiting on you; read
    honestly it is one live prompt and 23 corpses. A "Needs you" list nobody
    can act on is worse than no list, because you stop reading it. The API
    contract's own fallback-discovery rule says the same thing: quiet is idle.
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


def find_collisions(agents: list[Agent]) -> list[Collision]:
    """Files held by two or more **live** agents.

    This is the overlapping-work anxiety answered without any agent talking to
    another agent: a set intersection over what each one already wrote down —
    normalized per-agent by :func:`_collision_key` so two worktrees of one
    repo collide on the logical file, not the raw string. Ended sessions are
    excluded — a file two finished sessions both touched is history, not a
    collision.
    """
    holders: dict[tuple[str, str], list[Agent]] = {}
    display: dict[tuple[str, str], str] = {}
    for agent in agents:
        if not agent.live:
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
    return Fleet(agents=agents, collisions=find_collisions(agents))


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


# Only live agents get an entry. Ended sessions are counted in the header and
# nothing more: against the real registry there were 105 of them to 35 live,
# so listing them turned a fleet view into a 700-line graveyard with the
# answer buried at the top. The registry GCs them within a week anyway, and
# "what did that finished session decide" is the diary's job, not this file's.
_GROUPS = (
    ("Needs you", ("waiting_input",)),
    ("Working", ("working",)),
    ("Idle", ("idle",)),
)


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
    live = [a for a in fleet.agents if a.live]
    needs = fleet.by_status("waiting_input")
    ended = fleet.by_status("ended")

    counts = (
        f"**{len(live)} live · {len(needs)} need you · "
        f"{len(fleet.collisions)} collision(s)**"
    )
    if ended:
        counts += f" · {len(ended)} ended, not listed"

    out = [
        "# Agents",
        "",
        f"_Generated {stamp} — a cache of `sessions/` + `checkpoints/`. "
        "Nothing reads this file as state; delete it and it comes back._",
        "",
        counts,
        "",
    ]

    for title, statuses in _GROUPS:
        group = fleet.by_status(*statuses)
        if not group:
            continue
        out.append(f"## {title} ({len(group)})")
        out.append("")
        for agent in group:
            out.extend(_render_agent(agent, now))

    out.append("## Collisions")
    out.append("")
    if not fleet.collisions:
        out.append("_None — no file is held by two live agents._")
        out.append("")
    else:
        for c in fleet.collisions:
            out.append(f"- `{c.path}` — {', '.join(c.labels)}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_fleet_line(fleet: Fleet, now: datetime) -> str:
    """One line for the status bar, or empty when there is no fleet.

    Empty rather than ``0 fronts`` on purpose: the status line renders nothing
    for an empty file, and a permanent "0" in every tab is noise, not a
    reading. Zero-valued segments are dropped for the same reason.
    """
    live = [a for a in fleet.agents if a.live]
    if not live:
        return ""

    parts = [f"{len(live)} front{'s' if len(live) != 1 else ''}"]

    needs = len(fleet.by_status("waiting_input"))
    if needs:
        parts.append(f"{needs} need you")

    oldest = max(a.age(now) for a in live)
    parts.append(f"oldest {format_age(oldest)}")

    n = len(fleet.collisions)
    if n:
        parts.append(f"{n} collision{'s' if n != 1 else ''}")

    return " · ".join(parts) + "\n"


def write_fleet_view(data_dir: Path, now: datetime | None = None) -> tuple[Path, Path]:
    """Render both outputs into *data_dir*; return their paths.

    One function so the hook and the CLI cannot drift into writing two
    different files. Atomic because the status line ``cat``s ``fleet.txt`` on
    every prompt render, and a torn read there flashes garbage into every tab.
    """
    now = now or datetime.now(timezone.utc)
    fleet = collect(data_dir, now)

    agents_path = data_dir / AGENTS_FILENAME
    fleet_path = data_dir / FLEET_FILENAME
    atomic_write(agents_path, render_agents_md(fleet, now))
    atomic_write(fleet_path, render_fleet_line(fleet, now))

    live = sum(1 for a in fleet.agents if a.live)
    logger.info(
        "Fleet: %d live of %d session(s), %d collision(s) → %s",
        live, len(fleet.agents), len(fleet.collisions), agents_path,
    )
    return agents_path, fleet_path
