"""The digest — a ranked reading of the fleet, capped at what a person reads.

``AGENTS.md`` is the full picture and it went unread for a week, which is the
whole reason this module exists. A 216-line file and a bare count in the status
bar fail in opposite directions: one is too much to look at while walking away,
the other carries no referent to act on. The digest is the thing in between.

Two rules shape everything here:

**Rank by what is blocked on a decision only the user can make.** Not by
recency, not by severity in the abstract. An approved PR outranks a red CI run
because nothing else moves until someone clicks merge; a red CI run outranks a
waiting session because it is rotting whether or not anyone is at the keyboard.

**Cap the urgent list.** Eight items, hard. An unbounded "needs you" list is
the same overwhelm in a new font — and if there are really twelve fires, the
useful fact is "twelve", not twelve lines you skim and forget.

Everything not urgent collapses into count lines. Counts are safe *here*
precisely because every one of them has the ranked list above it and the full
file behind it; the status-line count had neither.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from lib.fleet import Fleet, format_age
from lib.fleet_sources.prs import stacks

logger = logging.getLogger(__name__)

# A session in `waiting_input` past this is not a pending question, it is an
# abandoned tab. Confirmed with the user 2026-08-04. Keeping it in the ranked
# list would mean the list is mostly corpses, which is how you learn to skip it.
WAITING_STALE_HOURS = 12

# Hard cap on the ranked list. See the module docstring.
MAX_ITEMS = 8

# A checkpoint's "next action" is written for a session resuming work and runs
# to three lines; eight of those is a wall of text, which is the failure mode
# this digest exists to avoid. Enough to recognise the task, not to re-read it.
MAX_TEXT = 120


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    """Trim to *limit* on a word boundary, marking that something was cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{cut}…"

# Rank tiers, lowest first. Named rather than inline so the ordering can be
# read in one place and asserted in tests.
_RANK_APPROVED = 10
_RANK_RED_CI = 20
_RANK_STACK = 30
_RANK_WAITING = 40
_RANK_COLLISION = 50


@dataclass
class Item:
    """One line of the ranked list."""

    rank: int
    text: str
    sort_key: tuple = ()

    @property
    def order(self) -> tuple:
        return (self.rank, *self.sort_key)


def _pr_items(fleet: Fleet) -> list[Item]:
    if fleet.prs is None or not fleet.prs.available:
        return []
    human = [p for p in fleet.prs.human if not p.is_draft]

    chains = [c for c in stacks(human) if len(c) > 1]
    stacked: set[tuple[str, int]] = {
        (pr.repo, pr.number) for chain in chains for pr in chain
    }

    items: list[Item] = []
    for chain in chains:
        # The chain is merge-ordered; the first member is the only one that can
        # merge now, so it is the one whose state decides whether this is
        # actionable. Four PRs, one decision, one line.
        head = chain[0]
        order = "→".join(f"#{pr.number}" for pr in chain)
        # Same rule as single PRs below: approved is only actionable when CI
        # lets it merge. "approved" over a red head invites a bouncing click.
        state = "approved" if head.approved and not head.failing else f"CI {head.ci}"
        repo = head.repo.split("/")[-1]
        items.append(Item(
            _RANK_STACK,
            f"{repo} stack — {len(chain)} PRs, merge order {order} "
            f"(next: #{head.number}, {state})",
            (head.repo, head.number),
        ))

    for pr in human:
        if (pr.repo, pr.number) in stacked:
            continue
        if pr.approved and pr.ci != "failing":
            items.append(Item(
                _RANK_APPROVED,
                f"{pr.label} — approved, awaiting your merge: {_clip(pr.title)}",
                (pr.repo, pr.number),
            ))
        elif pr.failing:
            items.append(Item(
                _RANK_RED_CI,
                f"{pr.label} — CI red: {_clip(pr.title)}",
                (pr.repo, pr.number),
            ))
    return items


def _waiting_items(fleet: Fleet, now: datetime) -> tuple[list[Item], int]:
    """``(ranked items, stale count)`` for sessions that asked a question."""
    cutoff = timedelta(hours=WAITING_STALE_HOURS)
    items: list[Item] = []
    stale = 0
    for agent in fleet.in_group("Needs you"):
        age = agent.age(now)
        if age > cutoff:
            stale += 1
            continue
        where = agent.hostname or agent.session_id[:8]
        # Project first, then the container it is in — but never both when they
        # are the same string, which is what a session with no project used to
        # render: ``hostname `hostname` — …``.
        who = f"{agent.project} `{where}`" if agent.project else f"`{where}`"
        # The checkpoint's next action is what it is waiting on; without one
        # the honest answer is that we only know it stopped to ask.
        what = agent.next_action or agent.intent or "waiting on you (no checkpoint)"
        items.append(Item(
            _RANK_WAITING,
            f"{who} — {_clip(what)} ({format_age(age)})",
            (int(age.total_seconds()),),
        ))
    return items, stale


def _collision_items(fleet: Fleet) -> list[Item]:
    return [
        Item(_RANK_COLLISION,
             f"collision on `{c.path}` — {', '.join(c.labels)}",
             (c.path,))
        for c in fleet.collisions
    ]


def rank(fleet: Fleet, now: datetime) -> tuple[list[Item], int]:
    """``(ranked items, stale-prompt count)`` — the ordering rules, in one place."""
    waiting, stale = _waiting_items(fleet, now)
    items = _pr_items(fleet) + waiting + _collision_items(fleet)
    items.sort(key=lambda i: i.order)
    return items, stale


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _summary_lines(fleet: Fleet, now: datetime, stale_prompts: int) -> list[str]:
    out: list[str] = []

    needs = fleet.in_group("Needs you")
    working = fleet.in_group("Working")
    parked = fleet.in_group("Parked")
    idle = fleet.in_group("Idle")

    # One number for "how many agents do I have", then its breakdown. `RUNNING`
    # used to be the Working count alone, which read as the whole fleet and was
    # not: a session that stopped to ask a question leaves `Working` for
    # `Needs you`, so four live containers rendered as `RUNNING (2)` with the
    # other two accounted for only in the ranked list above. Every reading of
    # that line was wrong in the same direction — fewer agents alive than there
    # are. The total is what the question asks; the split is what to do about it.
    bits = [f"IN FLIGHT ({len(needs) + len(working) + len(parked)})"]
    if needs:
        bits.append(f"{len(needs)} waiting on you")
    if working:
        bits.append(f"{len(working)} working")
    if parked:
        bits.append(f"{len(parked)} parked")
    if idle:
        oldest = max((a.age(now) for a in idle), default=timedelta(0))
        bits.append(f"{len(idle)} idle (oldest {format_age(oldest)})")
    if stale_prompts:
        bits.append(f"{stale_prompts} stale prompt(s)")
    out.append(" · ".join(bits))

    if fleet.prs is not None:
        if not fleet.prs.available:
            out.append("PRs: gh unavailable — not read")
        else:
            human, bots = len(fleet.prs.human), len(fleet.prs.bots)
            line = f"PRs open ({human + bots} — {human} yours, {bots} bot)"
            if fleet.prs.errors:
                line += f" · {len(fleet.prs.errors)} repo(s) unreachable"
            if fleet.prs.no_access:
                line += f" · {len(fleet.prs.no_access)} not visible to this token"
            out.append(line)

    if fleet.repos is not None:
        dirty = [r for r in fleet.repos if r.dirty]
        unpushed = [r for r in fleet.repos if r.unpushed or r.no_upstream]
        worktrees = sum(len(r.worktrees) for r in fleet.repos)
        parts = [f"{len(dirty)} dirty", f"{len(unpushed)} with unpushed branches",
                 f"{worktrees} worktrees"]
        out.append(f"Repos ({len(fleet.repos)}): " + " · ".join(parts))

    if fleet.jobs is not None:
        running = [j for j in fleet.jobs if j.running]
        stale_jobs = [j for j in fleet.jobs if not j.running and not j.finished]
        line = f"Background jobs: {len(running)} running"
        if stale_jobs:
            line += f" · {len(stale_jobs)} stale"
        out.append(line)

    # Scheduled routines are server-side; a script cannot enumerate them. Say
    # "not tracked" rather than "0" — the difference is a promise we can keep.
    if fleet.scheduled is None:
        out.append("Scheduled: not tracked (server-side; ask Claude to run CronList)")
    elif fleet.scheduled:
        out.append(f"Scheduled: {len(fleet.scheduled)} routine(s)")

    if fleet.backlog is not None:
        b = fleet.backlog
        if b.empty:
            out.append("Backlog: clear")
        else:
            parts = []
            if b.learnings_lines:
                parts.append(f"{b.learnings_lines} learnings")
            if b.dreams_pending:
                parts.append(f"{b.dreams_pending} dream proposal(s)")
            if b.pending_extractions:
                parts.append(f"{b.pending_extractions} extractions queued")
            if b.failed_extractions:
                parts.append(f"{b.failed_extractions} extractions FAILED")
            if b.inbox_items:
                parts.append(f"{b.inbox_items} INBOX")
            out.append("Backlog: " + " · ".join(parts))

    out.append(
        "Collisions: none" if not fleet.collisions
        else f"Collisions: {len(fleet.collisions)}"
    )
    return out


def render_digest(fleet: Fleet, now: datetime, agents_path: str = "") -> str:
    """The console reading: ranked urgent list, then counts, then the pointer."""
    items, stale_prompts = rank(fleet, now)
    shown = items[:MAX_ITEMS]

    out: list[str] = []
    if not items:
        out.append("NEEDS YOU (0) — nothing is blocked on you.")
    else:
        out.append(f"NEEDS YOU ({len(items)})")
        for i, item in enumerate(shown, 1):
            out.append(f" {i}. {item.text}")
        if len(items) > MAX_ITEMS:
            out.append(f" … +{len(items) - MAX_ITEMS} more — see the full report")
    out.append("")
    out.extend(_summary_lines(fleet, now, stale_prompts))
    out.append("")
    pointer = f"   ({agents_path})" if agents_path else ""
    # The plugin-qualified form, because that is what actually invokes it —
    # a pointer to a command that does not exist is worse than no pointer.
    out.append(f"Full detail: /multiplai-context:fleet-status --full{pointer}")
    return "\n".join(out) + "\n"
