"""Shared extraction logic: LLM call, diary write, learnings append.

Diary-first extraction: each unit of work yields a rich diary entry;
learnings are a projection of it. Extracted by extract_units(), persisted
by write_diary_entries() and append_learnings().
"""

import fcntl
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


EXTRACTION_PROMPT = """\
You are analyzing a conversation transcript between a user and an AI \
assistant ("Claude"). Extract diary entries and learnings grouped by \
**logical unit of work** — like commits, not turns.

## What is a "unit of work"?

A coherent topic, task, or decision that stands on its own. One turn \
might contain multiple units; multiple turns might form one unit. \
Group by logical coherence, not by message boundaries.

## Output format

A JSON object with a "units" array. Each unit has:
- "timestamp": ISO timestamp closest to the unit (or "" if unknown)
- "diary_entry": rich narrative of what happened — what was attempted, \
built, or decided; the key decisions made and their rationale; how the \
work evolved; what changed. Write 1-3 substantive paragraphs. This is \
the PRIMARY output — invest most effort here.
- "learnings": array of things worth remembering across sessions (can be [])

Each learning object:
- "trust": "verified" (confirmed via code/logs/tests) | "high" (strong \
evidence) | "medium" (inference)
- "type": OBSERVATION | PREFERENCE | CORRECTION | PATTERN | RULE-PROPOSAL
- "description": concise but complete (one sentence)
- "target": one of the valid memory file names below
- "action": what to add/change in that file (one sentence)

## Valid target files

{valid_targets}

Do NOT invent new file names. Use the closest match if unsure.

## Correction detection

When the user corrects Claude's output or assumption:
- type: CORRECTION, trust: verified
Signals: "use X not Y", "no, that's wrong", "actually...", explicit contradictions.
Corrections are highest priority — they prevent recurring mistakes.

## Rules

- diary_entry is PRIMARY — learnings are a projection of it
- A unit with 0 learnings is fine (diary-only is valid)
- Deduplicate: emit each insight ONCE, even if it spans multiple units
- If something was CORRECTED later, output only the final corrected version
- Skip trivial exchanges, greetings, routine tool usage
- If the entire session is trivial, return {"units": []}

## Transcript

{transcript}

## Output

Return ONLY valid JSON (no markdown fences, no explanation):
{"units": [{"timestamp": "...", "diary_entry": "...", "learnings": [...]}]}
"""


def _parse_units(raw: str) -> list[dict]:
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    units = parsed.get("units")
    if not isinstance(units, list):
        return []
    return [u for u in units if isinstance(u, dict)]


async def extract_units(
    text: str,
    *,
    valid_targets: list[str],
    client,
) -> list[dict]:
    """Call LLM to extract diary units + learnings from transcript text.

    Returns list of unit dicts with 'timestamp', 'diary_entry', 'learnings'.
    Raises on LLM failure — caller decides whether to continue with
    correction-only output.
    """
    targets_block = "\n".join(f"- {t}" for t in valid_targets) if valid_targets else "(none)"
    # NOT str.format: transcript text routinely contains literal { } (JSON,
    # code, f-strings) which would raise KeyError/ValueError and silently
    # kill extraction. Plain replacement never interprets braces. Replace
    # valid_targets first (controlled), transcript last (untrusted).
    prompt = (
        EXTRACTION_PROMPT
        .replace("{valid_targets}", targets_block)
        .replace("{transcript}", text)
    )
    response = await client.query(
        system="You are a learnings extractor. Output ONLY valid JSON.",
        messages=[{
            "role": "user",
            "content": prompt,
        }],
    )
    return _parse_units(response.content)


def write_diary_entries(
    units: list[dict],
    diary_dir: Path,
    session_id: str,
    cwd: str,
    timestamp: str,
) -> Optional[Path]:
    """Write canonical diary entries to diary/YYYY-MM-DD/<sessionId>.md.

    Format: first line is ``[ts] [session_id] [cwd]`` (parsed by
    synthesize_now._parse_diary_entry); body is one rich entry per unit.

    Idempotent: returns existing path without overwriting.
    Returns None if no units have diary content.
    """
    diary_units = [u for u in units if (u.get("diary_entry") or "").strip()]
    if not diary_units:
        return None

    # Date from first unit's timestamp; fall back to provided timestamp
    first_ts = diary_units[0].get("timestamp") or timestamp
    try:
        dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        date_str = timestamp[:10] if len(timestamp) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    day_dir = diary_dir / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    diary_file = day_dir / f"{session_id}.md"
    if diary_file.exists():
        return diary_file

    lines = [f"[{timestamp}] [{session_id}] [{cwd}]", ""]
    for unit in diary_units:
        entry = unit["diary_entry"].strip()
        unit_ts = unit.get("timestamp") or timestamp
        lines.append(f"[{unit_ts}]")
        lines.append("")
        lines.append(entry)
        lines.append("")

    diary_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return diary_file


def _format_learning_entry(learning: dict) -> str:
    trust = learning.get("trust", "medium")
    ltype = learning.get("type", "OBSERVATION")
    desc = (learning.get("description") or "").strip()
    target = (learning.get("target") or "").strip()
    action = (learning.get("action") or "").strip()
    line = f"- **[trust: {trust}]** {ltype} {desc}"
    if target:
        line += f" → Target: {target}"
        if action:
            line += f" — {action}"
    return line


def _format_correction_entry(match: dict) -> str:
    excerpt = (match.get("excerpt") or "").replace("\n", " ").strip()
    category = match.get("category", "correction")
    return (
        f"- **[trust: verified]** CORRECTION user signal {category} "
        f"detected: {excerpt!r} → Target: wrong.md — log the correction "
        "and apply downstream"
    )


def append_learnings(
    units: list[dict],
    learnings_file: Path,
    session_id: str,
    correction_matches: list[dict],
    timestamp: str,
) -> bool:
    """Atomic append learnings to per-day file with flock + Session: dedup.

    Returns True if anything was written.
    """
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = learnings_file.parent / f".{learnings_file.name}.lock"

    with open(lock_file, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if session_id and learnings_file.exists():
                if f"Session: {session_id}" in learnings_file.read_text():
                    return False

            with open(learnings_file, "a") as f:
                wrote_any = False
                for unit in units:
                    learnings = unit.get("learnings") or []
                    if not learnings:
                        continue
                    ts = unit.get("timestamp") or timestamp
                    f.write(f"\n---\n## Session Learnings — {ts}\n")
                    if session_id:
                        f.write(f"Session: {session_id}\n")
                    for learning in learnings:
                        if not isinstance(learning, dict):
                            continue
                        f.write(_format_learning_entry(learning) + "\n")
                    wrote_any = True

                if correction_matches:
                    if not wrote_any:
                        f.write(f"\n---\n## Session Learnings — {timestamp}\n")
                        if session_id:
                            f.write(f"Session: {session_id}\n")
                    for match in correction_matches:
                        f.write(_format_correction_entry(match) + "\n")
                    wrote_any = True

            return wrote_any
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
