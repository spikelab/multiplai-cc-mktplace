"""Structured learning extraction (Stop hook).

Asks the LLM to decompose the session transcript into logical units of
work, then to attach typed learnings (``OBSERVATION``, ``PREFERENCE``,
``CORRECTION``, ``PATTERN``, ``RULE-PROPOSAL``) with a trust level, a
target memory file, and a one-sentence action. Writes one entry per
learning to a per-day ``{YYYY-MM-DD}.md`` file in the structured kit
format::

    ---
    ## Session Learnings — {timestamp}
    Session: {session_id}
    - **[trust: medium]** OBSERVATION ... → Target: file.md — action

The format is what ``/process-learnings`` reads when applying captured
learnings into memory files. Free-form bullets (the previous
plugin format) lost the ``type`` taxonomy and the ``target`` field
that lets the processor route updates to the right memory file —
this rewrite restores that schema.

Correction-pattern detection (regex) still runs alongside the LLM and
contributes ``CORRECTION``-tagged entries with ``trust: verified``,
making sure corrections fire reliably even when the LLM misses them.
"""

import asyncio
import fcntl
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.log_utils import setup_logging
from lib.correction_patterns import detect_corrections_in_transcript

logger = setup_logging("extract_learnings")


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
- "diary": one-line summary (max 300 chars) of what happened in this unit
- "learnings": array of things worth remembering across sessions (can be empty [])

Each learning object:
- "trust": "verified" (confirmed via code/logs/tests) | "high" (strong evidence) | "medium" (inference)
- "type": OBSERVATION | PREFERENCE | CORRECTION | PATTERN | RULE-PROPOSAL
- "description": concise but complete (one sentence)
- "target": one of the valid memory file names below (e.g., "technical-pref.md")
- "action": what to add/change in the target file (one sentence)

## Valid target files

Use EXACTLY these file names for the "target" field:
{valid_targets}

Do NOT invent new file names. If no file is a good fit, use the closest match.

## Correction detection

When the user corrects Claude's output or assumption, tag it as:
- type: CORRECTION, trust: verified
Indicators: "use X not Y", "no, that's wrong", "actually...", \
user explicitly contradicts something Claude stated, a fact is discovered to \
contradict memory. Corrections are highest priority — they prevent recurring mistakes.

## Rules

- Group by logical unit, not by turn
- A unit with 0 learnings is fine (diary-only, for context)
- Deduplicate: same insight appears ONCE across all units, even if it applies to \
multiple files — emit it ONCE with the primary target
- If something was CORRECTED later, output only the final corrected version
- Skip trivial exchanges, greetings, routine tool usage
- If the entire session is trivial, return {{"units": []}}

## Transcript

{transcript}

## Output

Return ONLY this JSON (no markdown fences, no explanation, no commentary):
{{"units": [{{"timestamp": "...", "diary": "...", "learnings": [...]}}]}}
"""


def _list_valid_targets(memory_dir: Path) -> list[str]:
    """Return the *.md files in the memory dir for the prompt's target list."""
    if not memory_dir.exists():
        return []
    return sorted(p.name for p in memory_dir.glob("*.md") if p.is_file())


def _parse_units(raw: str) -> list[dict]:
    """Parse the LLM JSON response into a list of unit dicts.

    Tolerates fenced code blocks; logs and returns [] on any failure
    so a malformed extraction skips writing rather than crashing the
    Stop hook.
    """
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Extraction LLM returned non-JSON; skipping")
        return []
    if not isinstance(parsed, dict):
        return []
    units = parsed.get("units")
    if not isinstance(units, list):
        return []
    return [u for u in units if isinstance(u, dict)]


def _format_learning_entry(learning: dict) -> str:
    """Render one learning dict into kit's structured single-line format."""
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
    """Render a regex-detected correction as a structured CORRECTION line."""
    excerpt = (match.get("excerpt") or "").replace("\n", " ").strip()
    category = match.get("category", "correction")
    return (
        f"- **[trust: verified]** CORRECTION user signal {category} "
        f"detected: {excerpt!r} → Target: wrong.md — log the correction "
        "and apply downstream"
    )


async def extract() -> None:
    """Run a single extraction pass on the Stop-hook stdin input."""
    paths = get_paths()
    memory_dir = paths.memory_dir()
    learnings_file = paths.learnings_file()  # today's per-day file

    hook_input = sys.stdin.read()
    if not hook_input.strip():
        logger.info("No session data on stdin, skipping extraction")
        return

    transcript_data: dict = {}
    try:
        transcript_data = json.loads(hook_input)
        transcript = transcript_data.get("transcript", hook_input)
    except (json.JSONDecodeError, AttributeError):
        transcript = hook_input
    session_id = (
        transcript_data.get("session_id", "")
        if isinstance(transcript_data, dict)
        else ""
    )

    valid_targets = _list_valid_targets(memory_dir)
    targets_block = (
        "\n".join(f"- {t}" for t in valid_targets) if valid_targets else "(none)"
    )

    units: list[dict] = []
    try:
        client = await create_client()
        logger.info("Extract learnings using %s", type(client).__name__)
        response = await client.query(
            system="You are a learnings extractor. Output ONLY valid JSON.",
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(
                    valid_targets=targets_block,
                    transcript=transcript,
                ),
            }],
        )
        units = _parse_units(response.content)
    except Exception:
        logger.exception("LLM call failed during learning extraction")
        # Continue: regex-detected corrections are still worth writing.

    correction_matches = detect_corrections_in_transcript(transcript)

    if not units and not correction_matches:
        logger.info("No actionable learnings found, nothing to append")
        return

    # Atomic append per-day with session dedup. Lock prevents double
    # writes when the Stop hook fires twice or two extractions race.
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = learnings_file.parent / f".{learnings_file.name}.lock"

    with open(lock_file, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if session_id and learnings_file.exists():
                existing = learnings_file.read_text()
                if f"Session: {session_id}" in existing:
                    logger.info(
                        "Session %s already appended to %s, skipping",
                        session_id, learnings_file,
                    )
                    return

            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
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

            if wrote_any:
                logger.info("Appended structured learnings to %s", learnings_file)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def main() -> None:
    asyncio.run(extract())


if __name__ == "__main__":
    main()
