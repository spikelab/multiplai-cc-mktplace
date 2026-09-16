"""Skill-routing precision: did a suggested skill get invoked?

``context_manager.py`` writes one ``ROUTING memory=[...] skills=[...]
resources=[...]`` line per prompt to ``<logs_dir>/context_manager-*.log``.
The skills list is what the router *suggested*. Nothing records whether the
session then *used* one of them — that lives in the session transcript, as a
``Skill`` tool_use block (``input.skill``) or a ``<command-name>`` slash
invocation in a user turn.

This module joins the two. Each ROUTING line with a non-empty skills list is a
*suggestion event*. Its window runs from its own timestamp to the next ROUTING
line of the same session (or the end of the transcript), so one invocation
credits at most one prompt. A suggestion event is a *hit* when at least one
suggested skill was invoked inside that window.

Why the number matters: "Demystifying Agent Skills: Why They Work — Until They
Don't" (arXiv 2608.14036) measured actual-use retrieval precision falling from
29.6% to 3.3% as the skill pool grew from 5 to 100. A catalog that only grows
needs this figure to notice the decay.

Name matching: the router suggests bare names (``costs``); invocations are
qualified (``multiplai-context:costs``). Both sides are reduced to the segment
after the last colon before comparison.

Session matching: the log carries an 8-character session prefix; transcripts
are named by the full session id. A prefix that matches more than one main
transcript is reported as ambiguous and skipped, never guessed.

Timestamps: log lines are second-precision UTC (``...:34Z``); transcript
entries carry milliseconds (``...:10.422Z``). Both parse to aware datetimes.

Nothing here writes. It reads logs and transcripts and returns numbers.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from lib.costing_collector import (
    _COMMAND_IGNORE,
    _COMMAND_RE,
    _user_text,
    classify_transcript,
    find_transcripts,
)

ROUTING_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z?\]\s+"
    r"\[context_manager\]\s+"
    r"\[session:(?P<session>[^\]]*)\]\s+"
    r"INFO:\s?ROUTING memory=(?P<memory>\[.*?\]) skills=(?P<skills>\[.*?\]) resources="
)


# ----------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------


@dataclass
class Suggestion:
    """One ROUTING line with a non-empty skills list."""

    ts: datetime
    session_prefix: str
    skills: list[str]
    invoked: list[str] = field(default_factory=list)  # suggested names that were invoked
    transcript: Optional[str] = None  # None: no transcript matched the prefix


@dataclass
class Invocation:
    ts: datetime
    name: str  # bare name, after the last colon
    via: str  # "tool" or "command"


def bare_name(name: str) -> str:
    """``multiplai-context:costs`` → ``costs``; ``costs`` → ``costs``."""
    name = name.strip().lstrip("/")
    return name.rsplit(":", 1)[-1].strip()


def parse_ts(value: object) -> Optional[datetime]:
    """Aware UTC datetime from a log or transcript timestamp, else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ----------------------------------------------------------------------
# Log side
# ----------------------------------------------------------------------


def parse_routing_line(line: str) -> Optional[tuple[datetime, str, list[str]]]:
    """``(ts, session_prefix, skills)`` for a ROUTING line, else ``None``.

    Lines whose skills list is empty parse to an empty list — the caller
    decides whether those count (they are prompts, not suggestion events).
    """
    m = ROUTING_LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    ts = parse_ts(m.group("ts") + "Z")
    if ts is None:
        return None
    try:
        skills = json.loads(m.group("skills"))
    except json.JSONDecodeError:
        return None
    if not isinstance(skills, list):
        return None
    return ts, m.group("session"), [bare_name(str(s)) for s in skills if str(s).strip()]


def iter_routing_lines(logs_dir: Path) -> Iterator[tuple[datetime, str, list[str]]]:
    """Every parseable ROUTING line across ``context_manager*.log``, in file order."""
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        return
    files = sorted(logs_dir.glob("context_manager-*.log")) + [logs_dir / "context_manager.log"]
    for path in files:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parsed = parse_routing_line(line)
                    if parsed is not None:
                        yield parsed
        except OSError:
            continue


# ----------------------------------------------------------------------
# Transcript side
# ----------------------------------------------------------------------


def main_transcripts_by_prefix(config_dir: Path) -> dict[str, list[Path]]:
    """Map every 8-char session prefix to the main-session transcripts it matches.

    Subagent and workflow transcripts are excluded: a Skill call inside a
    subagent was not a response to the parent prompt's suggestion.
    """
    projects = Path(config_dir) / "projects"
    out: dict[str, list[Path]] = defaultdict(list)
    for path in find_transcripts(Path(config_dir)):
        info = classify_transcript(projects, path)
        if info["sidechain"]:
            continue
        session = info["session"]
        if len(session) >= 8:
            out[session[:8]].append(path)
    return dict(out)


def skill_invocations(path: Path) -> list[Invocation]:
    """Skill invocations in one transcript, in file order.

    Two sources: ``Skill`` tool_use blocks in assistant messages, and
    ``<command-name>`` tags in user turns (a skill typed as a slash command
    never goes through the Skill tool). Session-mechanics commands
    (``/clear``, ``/model``, ...) are ignored, matching the cost collector.
    """
    found: list[Invocation] = []
    try:
        fh = Path(path).open("rb")
    except OSError:
        return found
    with fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            ts = parse_ts(entry.get("timestamp"))
            if ts is None:
                continue
            kind = entry.get("type")
            if kind == "assistant":
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Skill"
                    ):
                        skill = (block.get("input") or {}).get("skill")
                        if isinstance(skill, str) and skill.strip():
                            found.append(Invocation(ts, bare_name(skill), "tool"))
            elif kind == "user":
                text = _user_text(entry)
                if text is None:
                    continue
                m = _COMMAND_RE.search(text)
                if m:
                    name = m.group(1).strip()
                    if name and name not in _COMMAND_IGNORE:
                        found.append(Invocation(ts, bare_name(name), "command"))
    return found


# ----------------------------------------------------------------------
# Join
# ----------------------------------------------------------------------


@dataclass
class Report:
    since: Optional[datetime]
    prompts_total: int = 0  # ROUTING lines in window, any skills value
    suggestions: list[Suggestion] = field(default_factory=list)
    unmatched_sessions: int = 0  # suggestion events with no transcript
    ambiguous_sessions: int = 0  # prefix matched >1 main transcript

    @property
    def suggested_prompts(self) -> int:
        return len([s for s in self.suggestions if s.transcript is not None])

    @property
    def hit_prompts(self) -> int:
        return len([s for s in self.suggestions if s.invoked])

    @property
    def precision(self) -> Optional[float]:
        n = self.suggested_prompts
        return (self.hit_prompts / n) if n else None

    def per_skill(self) -> list[dict]:
        """One row per skill ever suggested (in a matched session): suggested,
        invoked-after-suggestion counts, and the ratio."""
        suggested: Counter[str] = Counter()
        invoked: Counter[str] = Counter()
        for s in self.suggestions:
            if s.transcript is None:
                continue
            for name in set(s.skills):
                suggested[name] += 1
            for name in set(s.invoked):
                invoked[name] += 1
        rows = []
        for name, n in suggested.most_common():
            k = invoked.get(name, 0)
            rows.append({"skill": name, "suggested": n, "invoked": k,
                         "ratio": (k / n) if n else None})
        return rows

    def never_invoked(self, *, min_suggested: int = 3) -> list[dict]:
        return [r for r in self.per_skill()
                if r["invoked"] == 0 and r["suggested"] >= min_suggested]

    def to_dict(self) -> dict:
        return {
            "since": self.since.isoformat() if self.since else None,
            "prompts_total": self.prompts_total,
            "suggested_prompts": self.suggested_prompts,
            "hit_prompts": self.hit_prompts,
            "precision": self.precision,
            "unmatched_sessions": self.unmatched_sessions,
            "ambiguous_sessions": self.ambiguous_sessions,
            "per_skill": self.per_skill(),
            "never_invoked": self.never_invoked(),
        }


def measure(
    routing: Iterable[tuple[datetime, str, list[str]]],
    transcripts_by_prefix: dict[str, list[Path]],
    *,
    since: Optional[datetime] = None,
    invocations_for: Optional[Callable[[Path], list[Invocation]]] = None,
) -> Report:
    """Join ROUTING events against transcript invocations.

    ``invocations_for(path) -> list[Invocation]`` defaults to
    :func:`skill_invocations`; tests inject a stub.
    """
    read = invocations_for or skill_invocations
    report = Report(since=since)

    # Group prompts by session, in time order, so each suggestion's window
    # closes at the session's next prompt.
    by_session: dict[str, list[tuple[datetime, list[str]]]] = defaultdict(list)
    for ts, prefix, skills in routing:
        if since is not None and ts < since:
            continue
        report.prompts_total += 1
        by_session[prefix].append((ts, skills))

    cache: dict[Path, list[Invocation]] = {}
    for prefix, prompts in by_session.items():
        prompts.sort(key=lambda p: p[0])
        events = [(ts, skills) for ts, skills in prompts if skills]
        if not events:
            continue
        paths = transcripts_by_prefix.get(prefix, [])
        if len(paths) > 1:
            report.ambiguous_sessions += len(events)
            for ts, skills in events:
                report.suggestions.append(Suggestion(ts, prefix, skills, transcript=None))
            continue
        if not paths:
            report.unmatched_sessions += len(events)
            for ts, skills in events:
                report.suggestions.append(Suggestion(ts, prefix, skills, transcript=None))
            continue
        path = paths[0]
        if path not in cache:
            cache[path] = read(path)
        invocations = cache[path]
        # Window end = next prompt of the same session (any skills value).
        for i, (ts, skills) in enumerate(prompts):
            if not skills:
                continue
            end = prompts[i + 1][0] if i + 1 < len(prompts) else None
            wanted = set(skills)
            hit = sorted({
                inv.name for inv in invocations
                if inv.name in wanted and inv.ts >= ts and (end is None or inv.ts < end)
            })
            report.suggestions.append(
                Suggestion(ts, prefix, skills, invoked=hit, transcript=str(path))
            )
    return report


def run(
    logs_dir: Path,
    config_dir: Path,
    *,
    days: Optional[int] = 30,
    now: Optional[datetime] = None,
) -> Report:
    """Measure precision over the last ``days`` days (``None`` = all logs)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)) if days is not None else None
    return measure(
        iter_routing_lines(Path(logs_dir)),
        main_transcripts_by_prefix(Path(config_dir)),
        since=since,
    )


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render(report: Report, *, limit: int = 25) -> str:
    lines = []
    window = report.since.strftime("%Y-%m-%d") if report.since else "all logs"
    lines.append(f"# Skill routing precision (since {window})")
    lines.append("")
    lines.append("| Measure | Value |")
    lines.append("|---|---|")
    lines.append(f"| Prompts routed | {report.prompts_total} |")
    lines.append(f"| Prompts with a skill suggestion (transcript found) | {report.suggested_prompts} |")
    lines.append(f"| ... where a suggested skill was then invoked | {report.hit_prompts} |")
    lines.append(f"| **Prompt-level precision** | **{_pct(report.precision)}** |")
    lines.append(f"| Suggestion events with no transcript | {report.unmatched_sessions} |")
    lines.append(f"| Suggestion events with an ambiguous session prefix | {report.ambiguous_sessions} |")
    lines.append("")
    rows = report.per_skill()
    if rows:
        lines.append("## Per skill (suggested → invoked after suggestion)")
        lines.append("")
        lines.append("| Skill | Suggested | Invoked | Ratio |")
        lines.append("|---|---:|---:|---:|")
        for r in rows[:limit]:
            lines.append(f"| {r['skill']} | {r['suggested']} | {r['invoked']} | {_pct(r['ratio'])} |")
        if len(rows) > limit:
            lines.append(f"| … {len(rows) - limit} more | | | |")
        lines.append("")
    never = report.never_invoked()
    if never:
        lines.append("## Suggested at least 3 times, never invoked")
        lines.append("")
        lines.append("| Skill | Suggested |")
        lines.append("|---|---:|")
        for r in never:
            lines.append(f"| {r['skill']} | {r['suggested']} |")
        lines.append("")
    lines.append(
        "A suggestion counts as a hit when the session invoked one of the "
        "suggested skills (Skill tool or slash command) before its next prompt. "
        "Reference: arXiv 2608.14036 measured precision falling 29.6% → 3.3% "
        "as a skill pool grew from 5 to 100."
    )
    return "\n".join(lines)
