#!/usr/bin/env python3
"""Prospective memory — intentions that fire later.

Every other memory type answers "what is true?". This one answers "what did I
say I'd come back to?" — the September re-check, the "when the runtime updates,
re-run the audit", the deadline that matters in three weeks. Today those live
in the transcript of whichever session they were mentioned in, which means they
exist right up until that session ends and never again.

Storage is `.multiplai/memory/prospective.md`, one intention per line:

    - [due: 2026-09-01] Re-check the Italian tax residency rule (captured 2026-07-26)
    - [on: the runtime updates past v0.5] Re-run the config audit (captured 2026-07-26)

Two trigger kinds, and the distinction is the point:

  `due:`  a date. Machine-checkable, so SessionStart can surface it by itself.
  `on:`   a condition in prose. NOT machine-checkable — no attempt is made to
          evaluate it. Condition-triggered intentions are surfaced by routing
          (the file is retrievable like any other memory) and by the periodic
          sweep below, never by a fake evaluator that guesses whether "the
          runtime updated" is true and is confidently wrong.

Nothing here writes to memory. Capture goes through extraction → dream →
`/dream-remember` like every other learning; this module parses, filters, and
formats.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

PROSPECTIVE_FILENAME = "prospective.md"

INTENTION_RE = re.compile(
    r"^-\s*\[(?:"
    r"due:\s*(?P<due>\d{4}-\d{2}-\d{2})"
    r"|on:\s*(?P<on>[^\]]+)"
    r")\]\s*(?P<text>.+?)"
    r"(?:\s*\(captured\s*(?P<captured>\d{4}-\d{2}-\d{2})\))?\s*$")

# A comment opened AND closed on the same line, anywhere in it.
_CLOSED_COMMENT_RE = re.compile(r"<!--.*?-->")

# How long before its due date an intention starts being surfaced. An intention
# that appears only on the day it's due is a reminder you get too late to act
# on; a week is enough to plan around and short enough not to be background
# noise for a month.
LEAD_DAYS = 7

# Condition-triggered intentions can't be evaluated, so they'd otherwise never
# resurface. Re-surface each one periodically instead, so "when X ships" is
# re-read occasionally rather than buried forever.
CONDITION_SWEEP_DAYS = 30

# Where the sweep's per-intention stamps live. Deliberately NOT in
# prospective.md: that file is human-editable memory, and a machine rewriting
# it on every session start would fight the user for it and put churn in the
# memory diff. This is derived state — losing it costs one early re-surface.
SWEEP_STATE_FILENAME = "prospective_sweep.json"


@dataclass(frozen=True)
class Intention:
    text: str
    due: date | None
    condition: str | None
    captured: date | None
    lineno: int

    @property
    def is_dated(self) -> bool:
        return self.due is not None

    def status(self, today: date) -> str:
        """`overdue` | `due` | `upcoming` | `condition` | `future`."""
        if self.due is None:
            return "condition"
        if self.due < today:
            return "overdue"
        if self.due == today:
            return "due"
        if self.due <= today + timedelta(days=LEAD_DAYS):
            return "upcoming"
        return "future"

    def render(self, today: date) -> str:
        status = self.status(today)
        if self.due is not None:
            if status == "overdue":
                days = (today - self.due).days
                when = f"OVERDUE by {days} day{'s' if days != 1 else ''} (was {self.due})"
            elif status == "due":
                when = "due today"
            else:
                when = f"due {self.due}"
        else:
            when = f"when: {self.condition}"
        return f"- [{when}] {self.text}"


def parse(text: str) -> list[Intention]:
    """Parse intentions, ignoring anything inside an HTML comment.

    Comment-awareness is not cosmetic. The shipped template documents the line
    format *using the line format*, inside a `<!-- -->` block — without this,
    every fresh install parses its own instructions as two real intentions.
    It also gives a way to silence an intention without deleting it: comment
    it out and it stops firing but stays readable.
    """
    out: list[Intention] = []
    in_comment = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        # A comment opened and not closed on the same line swallows what follows.
        if "<!--" in stripped and "-->" not in stripped[stripped.index("<!--"):]:
            in_comment = True
            continue
        if stripped.startswith("<!--"):
            continue  # fully-closed single-line comment
        # A closed comment AFTER the intention is an annotation, not part of
        # the intention. Left in, it rides into `text` and then into the nudge
        # ("- [due 2026-09-01] Re-check the rule <!-- ask the accountant -->").
        stripped = _CLOSED_COMMENT_RE.sub("", stripped).strip()
        match = INTENTION_RE.match(stripped)
        if not match:
            continue
        due_raw, captured_raw = match.group("due"), match.group("captured")
        try:
            due = date.fromisoformat(due_raw) if due_raw else None
            captured = date.fromisoformat(captured_raw) if captured_raw else None
        except ValueError:
            continue  # a malformed date is not an intention we can act on
        on = (match.group("on") or "").strip() or None
        out.append(Intention(
            text=match.group("text").strip(), due=due, condition=on,
            captured=captured, lineno=lineno))
    return out


def load(memory_dir: Path) -> list[Intention]:
    path = memory_dir / PROSPECTIVE_FILENAME
    if not path.is_file():
        return []
    return parse(path.read_text(encoding="utf-8"))


def sweep_key(intention: Intention) -> str:
    """Stable identity for an intention across sessions.

    Keyed on the condition and text, NOT the line number — reordering
    `prospective.md` or removing a line above must not reset another
    intention's sweep clock. Whitespace is collapsed so a reflowed line keeps
    its history; an edit to the wording is a different intention and gets a
    fresh clock, which is the right default (rewritten intention, re-read it).
    """
    condition = " ".join((intention.condition or "").split())
    text = " ".join(intention.text.split())
    return f"{condition} :: {text}"


def load_sweep_state(path: Path) -> dict[str, date]:
    """Read the sweep stamps. Any unreadable/garbage entry is simply dropped.

    A missing or corrupt state file must degrade to "never surfaced" (which
    surfaces, then stamps) and never to "already surfaced" — for this channel
    the safe direction of failure is noise, not silence.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # ValueError covers both JSONDecodeError and the UnicodeDecodeError a
        # binary-garbage file raises out of read_text.
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, date] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = date.fromisoformat(str(value))
        except (TypeError, ValueError):
            continue
    return out


def save_sweep_state(path: Path, state: dict[str, date]) -> None:
    """Persist the sweep stamps. Best-effort by design.

    A failed write costs one duplicate surface next session. Raising here
    would let derived state abort a session start, which is a far worse trade.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.isoformat() for k, v in sorted(state.items())}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def actionable(intentions: list[Intention], today: date, *,
               last_surfaced: dict[str, date] | None = None) -> list[Intention]:
    """Intentions worth surfacing now, most urgent first.

    Condition-triggered ones fire on an *elapsed* schedule, not a coincident
    one: `today - base >= CONDITION_SWEEP_DAYS`, where base is the last time
    this intention was actually surfaced (from *last_surfaced*), falling back
    to its capture date. The earlier `% CONDITION_SWEEP_DAYS == 0` test only
    fired on exact 30-day multiples, so one missed session — no session that
    day, a run either side of the UTC midnight rollover, a closed laptop —
    silently cost another full 30 days on the one memory channel where being
    silent IS the failure.

    Because the test is now "overdue", a fired sweep MUST be stamped by the
    caller (`save_sweep_state`) or it fires again next session. Not stamping
    is therefore noisy, not silent — the correct direction to fail in.

    An intention with neither a stamp nor a capture date surfaces on this
    call and is stamped from here on. That is a deliberate change from the
    old behaviour of ignoring it forever: a condition nobody can evaluate and
    nobody ever sees is indistinguishable from one that was never captured.
    """
    stamps = last_surfaced or {}
    out = [i for i in intentions
           if i.status(today) in {"overdue", "due", "upcoming"}]
    for i in intentions:
        if i.due is not None:
            continue
        base = stamps.get(sweep_key(i)) or i.captured
        if base is None or (today - base).days >= CONDITION_SWEEP_DAYS:
            out.append(i)
    # Overdue first, then by date; conditions last.
    order = {"overdue": 0, "due": 1, "upcoming": 2, "condition": 3}
    return sorted(out, key=lambda i: (order[i.status(today)],
                                      i.due or date.max, i.lineno))


def render_nudge(intentions: list[Intention], today: date, *, cap: int = 5) -> str:
    """The SessionStart nudge text, or empty string when nothing is due.

    Capped: on first rollout a backlog of intentions could all come due at
    once, and a nudge listing twenty of them is one the reader skips entirely.
    The overflow is counted, not silently dropped.
    """
    if not intentions:
        return ""
    shown = intentions[:cap]
    lines = [i.render(today) for i in shown]
    overflow = len(intentions) - len(shown)
    if overflow:
        lines.append(f"- ...and {overflow} more in `{PROSPECTIVE_FILENAME}`")
    body = "\n".join(lines)
    return (
        "\n--- SYSTEM NUDGE ---\n"
        "Prospective memory has intentions that have come due:\n"
        f"{body}\n"
        "Surface these to the user at the next natural stopping point. "
        f"They live in `{PROSPECTIVE_FILENAME}`; once one is acted on or is no "
        "longer relevant, it should be removed from that file via the normal "
        "memory-review path."
    )


def format_line(text: str, *, due: date | None = None,
                condition: str | None = None,
                captured: date | None = None) -> str:
    """Format one intention for writing into ``prospective.md``."""
    if (due is None) == (condition is None):
        raise ValueError("an intention needs exactly one of due= or condition=")
    trigger = f"due: {due.isoformat()}" if due else f"on: {condition}"
    stamp = f" (captured {(captured or date.today()).isoformat()})"
    return f"- [{trigger}] {text.strip()}{stamp}"
