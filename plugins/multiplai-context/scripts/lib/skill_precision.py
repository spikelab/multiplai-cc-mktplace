"""Skill-routing precision: did a suggested skill get invoked?

The context hook (``context_manager.py``) suggests skills per prompt. What the
model actually saw is recorded in the session transcript itself: Claude Code
stores every ``UserPromptSubmit`` hook's output as an ``attachment`` entry of
type ``hook_additional_context``, chained by ``parentUuid`` back to the
``user`` entry it belongs to. The hook's ``=== SKILLS ===`` block lists each
suggested skill as a ``## <name>`` heading. Large outputs are spilled to a
``<persisted-output>`` file whose path the attachment names.

That record is the right source for three reasons the ``ROUTING`` log line is
not: it is written *after* the re-recommendation cooldown, so it holds only
skills the model was shown; it is keyed to the prompt by uuid, so no
timestamp join is needed; and transcripts are kept for a year where the logs
are swept after seven days.

Whether the session then *used* a suggestion is in the same transcript: a
``Skill`` tool_use block (``input.skill``) in an assistant message, or a
``<command-name>`` slash invocation in a user turn.

Join rule: a *suggestion event* is a user prompt whose hook attachment names
at least one skill. Its window runs, in transcript order, from that user
entry (inclusive — a slash command *is* the prompt) up to the session's next
real user prompt. The event is a *hit* when at least one suggested skill was
invoked inside the window. One invocation therefore credits at most one
prompt.

Why the number matters: "Demystifying Agent Skills: Why They Work — Until They
Don't" (arXiv 2608.14036) measured actual-use retrieval precision falling from
29.6% to 3.3% as the skill pool grew from 5 to 100. A catalog that only grows
needs this figure to notice the decay.

Name matching: suggestions are bare names (``costs``); invocations may be
qualified (``multiplai-context:costs``). Both sides reduce to the segment
after the last colon.

Scope: main-session transcripts only (``projects/<proj>/<session>.jsonl``, also
under ``projects_migration_tmp/``). Subagent transcripts live in
subdirectories and are never read: a Skill call inside a subagent was not a
response to the parent prompt's suggestion.

Nothing here writes. It reads transcripts and returns numbers.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from lib.costing_collector import _COMMAND_IGNORE, _COMMAND_RE, _user_text
from lib.timeparse import parse_ts

SKILLS_HEADER = "=== SKILLS ==="
_SECTION_HEADER_RE = re.compile(r"^=== .+ ===$", re.MULTILINE)
_SKILL_HEADING_RE = re.compile(r"^## (\S+)", re.MULTILINE)
_PERSISTED_PATH_RE = re.compile(r"Full output saved to: (\S+)")


# ----------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------


@dataclass
class Suggestion:
    """One user prompt whose hook attachment named at least one skill."""

    ts: Optional[datetime]
    session: str
    skills: list[str]
    invoked: list[str] = field(default_factory=list)  # suggested names invoked in-window


def bare_name(name: str) -> str:
    """``multiplai-context:costs`` → ``costs``; ``/costs`` → ``costs``."""
    name = name.strip().lstrip("/")
    return name.rsplit(":", 1)[-1].strip()


# ----------------------------------------------------------------------
# Attachment side
# ----------------------------------------------------------------------


def suggested_skills(text: str) -> list[str]:
    """Bare skill names under the ``=== SKILLS ===`` block of hook output.

    The block ends at the next ``=== ... ===`` header or at end of text.
    """
    start = text.find(SKILLS_HEADER)
    if start < 0:
        return []
    body = text[start + len(SKILLS_HEADER):]
    nxt = _SECTION_HEADER_RE.search(body)
    if nxt:
        body = body[: nxt.start()]
    seen: list[str] = []
    for name in _SKILL_HEADING_RE.findall(body):
        bare = bare_name(name)
        if bare and bare not in seen:
            seen.append(bare)
    return seen


def attachment_text(entry: dict) -> str:
    """The hook output an attachment carries, following a persisted-output
    pointer to its file when the harness spilled it. Unreadable → ``""``."""
    content = (entry.get("attachment") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(c if isinstance(c, str) else json.dumps(c) for c in content)
    else:
        return ""
    if "<persisted-output>" in text:
        m = _PERSISTED_PATH_RE.search(text)
        if not m:
            return ""
        try:
            return Path(m.group(1)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return text


def _is_prompt_hook_attachment(entry: dict) -> bool:
    att = entry.get("attachment")
    return (
        entry.get("type") == "attachment"
        and isinstance(att, dict)
        and att.get("type") == "hook_additional_context"
        and att.get("hookName") == "UserPromptSubmit"
    )


# ----------------------------------------------------------------------
# Transcript side
# ----------------------------------------------------------------------


def iter_main_transcripts(config_dir: Path, *, modified_since: Optional[datetime] = None) -> Iterator[Path]:
    """Main-session transcripts under ``projects/``: one directory level down
    (plus the ``projects_migration_tmp/`` prefix), ``*.jsonl`` files only.

    A single ``scandir`` pass; subagent transcripts sit deeper and are never
    listed. ``modified_since`` skips files untouched since that instant —
    a transcript with no writes in the window has no prompts in it.
    """
    projects = Path(config_dir) / "projects"
    if not projects.is_dir():
        return
    roots = [projects]
    tmp = projects / "projects_migration_tmp"
    if tmp.is_dir():
        roots.append(tmp)
    cutoff = modified_since.timestamp() if modified_since else None
    for root in roots:
        try:
            project_dirs = [d for d in os.scandir(root) if d.is_dir() and d.name != "projects_migration_tmp"]
        except OSError:
            continue
        for proj in sorted(project_dirs, key=lambda d: d.name):
            try:
                files = list(os.scandir(proj.path))
            except OSError:
                continue
            for f in sorted(files, key=lambda e: e.name):
                if not f.name.endswith(".jsonl") or not f.is_file():
                    continue
                if cutoff is not None:
                    try:
                        if f.stat().st_mtime < cutoff:
                            continue
                    except OSError:
                        continue
                yield Path(f.path)


def _load_entries(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with Path(path).open("rb") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    out.append(entry)
    except OSError:
        return []
    return out


def _skill_tool_uses(entry: dict) -> list[str]:
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
            skill = (block.get("input") or {}).get("skill")
            if isinstance(skill, str) and skill.strip():
                names.append(bare_name(skill))
    return names


def _command_in_prompt(text: str) -> Optional[str]:
    m = _COMMAND_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    if not name or name in _COMMAND_IGNORE:
        return None
    return bare_name(name)


def measure_transcript(
    path: Path, *, since: Optional[datetime] = None
) -> tuple[int, list[Suggestion], Optional[datetime]]:
    """``(prompts, suggestions, earliest_prompt_ts)`` for one main transcript.

    ``prompts`` counts real user prompts at or after ``since``; each
    suggestion's window is resolved by transcript order, not timestamps.
    """
    entries = _load_entries(path)
    session = Path(path).stem
    by_uuid = {e["uuid"]: e for e in entries if isinstance(e.get("uuid"), str)}

    # Pass 1: real user prompts in order, with their index; invocations by index.
    prompt_idx: list[int] = []
    prompt_text: dict[int, str] = {}
    invocations: list[tuple[int, str]] = []  # (index, bare name)
    for i, e in enumerate(entries):
        if e.get("isSidechain"):
            continue
        kind = e.get("type")
        if kind == "user":
            text = _user_text(e)
            if text is None:
                continue
            prompt_idx.append(i)
            prompt_text[i] = text
            cmd = _command_in_prompt(text)
            if cmd:
                invocations.append((i, cmd))
        elif kind == "assistant":
            for name in _skill_tool_uses(e):
                invocations.append((i, name))

    # Pass 2: hook attachments → owning user prompt (walk parentUuid).
    index_of = {id(e): i for i, e in enumerate(entries)}
    skills_by_prompt: dict[int, list[str]] = {}
    for e in entries:
        if not _is_prompt_hook_attachment(e):
            continue
        # A slash-command prompt is followed by an ``isMeta`` user entry that
        # carries the skill body, and the attachment hangs off *that*; keep
        # walking until the entry is a real prompt.
        cur: Optional[dict] = e
        hops = 0
        pi: Optional[int] = None
        while cur is not None and hops < 50:
            if cur.get("type") == "user":
                idx = index_of.get(id(cur))
                if idx in prompt_text:
                    pi = idx
                    break
            cur = by_uuid.get(cur.get("parentUuid") or "")
            hops += 1
        if pi is None:
            continue
        names = suggested_skills(attachment_text(e))
        if names:
            skills_by_prompt.setdefault(pi, [])
            for n in names:
                if n not in skills_by_prompt[pi]:
                    skills_by_prompt[pi].append(n)

    # Pass 3: windows and hits.
    prompts = 0
    earliest: Optional[datetime] = None
    suggestions: list[Suggestion] = []
    for n, pi in enumerate(prompt_idx):
        ts = parse_ts(entries[pi].get("timestamp"))
        if since is not None and (ts is None or ts < since):
            continue
        prompts += 1
        if ts is not None and (earliest is None or ts < earliest):
            earliest = ts
        skills = skills_by_prompt.get(pi)
        if not skills:
            continue
        end = prompt_idx[n + 1] if n + 1 < len(prompt_idx) else len(entries)
        wanted = set(skills)
        hit = sorted({name for idx, name in invocations if pi <= idx < end and name in wanted})
        suggestions.append(Suggestion(ts, session, list(skills), invoked=hit))
    return prompts, suggestions, earliest


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------


@dataclass
class Report:
    since: Optional[datetime]
    earliest_prompt: Optional[datetime] = None  # oldest prompt actually read
    transcripts_read: int = 0
    prompts_total: int = 0  # real user prompts in window
    suggestions: list[Suggestion] = field(default_factory=list)

    @property
    def suggested_prompts(self) -> int:
        return len(self.suggestions)

    @property
    def hit_prompts(self) -> int:
        return len([s for s in self.suggestions if s.invoked])

    @property
    def precision(self) -> Optional[float]:
        n = self.suggested_prompts
        return (self.hit_prompts / n) if n else None

    def per_skill(self) -> list[dict]:
        """One row per suggested skill: suggested, invoked-after-suggestion, ratio."""
        suggested: Counter[str] = Counter()
        invoked: Counter[str] = Counter()
        for s in self.suggestions:
            for name in set(s.skills):
                suggested[name] += 1
            for name in set(s.invoked):
                invoked[name] += 1
        rows = []
        for name, n in suggested.most_common():
            k = invoked.get(name, 0)
            rows.append({"skill": name, "suggested": n, "invoked": k, "ratio": (k / n) if n else None})
        return rows

    def never_invoked(self, *, min_suggested: int = 3) -> list[dict]:
        return [r for r in self.per_skill() if r["invoked"] == 0 and r["suggested"] >= min_suggested]

    def to_dict(self) -> dict:
        return {
            "since": self.since.isoformat() if self.since else None,
            "earliest_prompt": self.earliest_prompt.isoformat() if self.earliest_prompt else None,
            "transcripts_read": self.transcripts_read,
            "prompts_total": self.prompts_total,
            "suggested_prompts": self.suggested_prompts,
            "hit_prompts": self.hit_prompts,
            "precision": self.precision,
            "per_skill": self.per_skill(),
            "never_invoked": self.never_invoked(),
        }


def run(
    config_dir: Path,
    *,
    days: Optional[int] = 30,
    now: Optional[datetime] = None,
) -> Report:
    """Measure precision over the last ``days`` days.

    ``days`` of ``None`` or ``0`` means every transcript on disk; a negative
    value is a caller error.
    """
    if days is not None and days < 0:
        raise ValueError(f"days must be >= 0 or None, got {days}")
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)) if days else None
    report = Report(since=since)
    for path in iter_main_transcripts(Path(config_dir), modified_since=since):
        prompts, suggestions, earliest = measure_transcript(path, since=since)
        report.transcripts_read += 1
        report.prompts_total += prompts
        report.suggestions.extend(suggestions)
        if earliest is not None and (report.earliest_prompt is None or earliest < report.earliest_prompt):
            report.earliest_prompt = earliest
    return report


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _day(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "n/a"


def render(report: Report, *, limit: int = 25) -> str:
    lines = []
    window = f"since {_day(report.since)}" if report.since else "all transcripts"
    lines.append(f"# Skill routing precision ({window}; earliest prompt read {_day(report.earliest_prompt)})")
    lines.append("")
    lines.append("| Measure | Value |")
    lines.append("|---|---|")
    lines.append(f"| Transcripts read | {report.transcripts_read} |")
    lines.append(f"| Prompts in window | {report.prompts_total} |")
    lines.append(f"| Prompts with a skill suggestion | {report.suggested_prompts} |")
    lines.append(f"| ... where a suggested skill was then invoked | {report.hit_prompts} |")
    lines.append(f"| **Prompt-level precision** | **{_pct(report.precision)}** |")
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
        "Suggestions are read from the prompt's hook attachment in the transcript, "
        "so only skills the model was actually shown are counted. "
        "Reference: arXiv 2608.14036 measured precision falling 29.6% → 3.3% "
        "as a skill pool grew from 5 to 100."
    )
    return "\n".join(lines)
