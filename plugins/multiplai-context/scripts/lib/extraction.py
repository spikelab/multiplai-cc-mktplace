"""Shared extraction logic: LLM call, diary write, learnings append.

Diary-first extraction: each unit of work yields a rich diary entry;
learnings are a projection of it. Extracted by extract_units(), persisted
by write_diary_entries() and append_learnings().
"""

import fcntl
import hashlib
import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Union

logger = logging.getLogger(__name__)


def load_target_charters(memory_dir: Path, catalogs_dir: Optional[Path] = None) -> list[dict]:
    """Return the valid extraction targets with their routing charters.

    Each entry is ``{"name", "purpose", "not_here"}`` where ``purpose`` is the
    first sentence of the file's catalog summary and ``not_here`` its
    ``anti_domains`` — the two fields that let the extractor route by domain
    instead of guessing from a bare filename list. Files absent from the
    catalog (or the whole catalog being absent/unreadable) degrade gracefully
    to name-only entries, so extraction never depends on catalog freshness.
    """
    if not memory_dir.exists():
        return []
    names = sorted(p.name for p in memory_dir.glob("*.md") if p.is_file())

    catalog: dict[str, dict] = {}
    catalog_file = (catalogs_dir / "memory.json") if catalogs_dir else None
    if catalog_file and catalog_file.exists():
        try:
            data = json.loads(catalog_file.read_text())
            for entry in data.get("entries", []):
                src = entry.get("source")
                if src:
                    catalog[src] = entry
        except (json.JSONDecodeError, OSError):
            logger.warning("Memory catalog unreadable at %s — extraction targets fall back to bare names", catalog_file)
            catalog = {}

    charters = []
    for name in names:
        entry = catalog.get(name, {})
        summary = (entry.get("summary") or "").strip()
        # First sentence keeps the prompt cost bounded (~30 tokens/file).
        purpose = summary.split(". ")[0].rstrip(".") if summary else ""
        charters.append({
            "name": name,
            "purpose": purpose,
            "not_here": entry.get("anti_domains") or [],
        })
    return charters


def render_target_line(target: Union[str, dict]) -> str:
    """Render one valid-target line for the extraction prompt.

    Accepts a bare filename (back-compat / catalog-less fallback) or a
    charter dict from :func:`load_target_charters`.
    """
    if isinstance(target, str):
        return f"- {target}"
    line = f"- {target['name']}"
    if target.get("purpose"):
        line += f" — {target['purpose']}"
    if target.get("not_here"):
        line += ". NOT: " + "; ".join(target["not_here"])
    return line


def _lock_path(target: Path) -> Path:
    """Return a flock file path for *target*, kept in a temp dir rather than
    beside the target. The lock files used to be written into the user's diary/
    learnings dirs and never removed, littering their (often git-tracked)
    workspace. A deterministic per-target path in the temp dir preserves the
    mutual-exclusion semantics without polluting the content dirs.
    """
    lock_dir = Path(tempfile.gettempdir()) / "multiplai-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(target.resolve()).encode("utf-8")).hexdigest()[:16]
    return lock_dir / f"{target.name}.{digest}.lock"


# Tag-delimited output, NOT JSON. diary_entry is long prose full of quotes,
# backslashes, newlines and code snippets; asking the model to escape that
# inside JSON strings failed strict json.loads on 5/8 real backfill sessions
# (unescaped \escape, unterminated string, missing delimiter), and
# json-repair fallbacks silently dropped ~half the units on a real broken
# sample. Prose between tags needs no escaping, and a truncated response
# still yields every completed <unit> block instead of losing everything.
# Bake-off on the 4 failed sessions, 2026-07-07: tags 12/12 clean.
EXTRACTION_PROMPT = """\
Today's date: {today}

You are analyzing a conversation transcript between a user and an AI \
assistant ("Claude"). Extract diary entries and learnings grouped by \
**logical unit of work** — like commits, not turns.

## What is a "unit of work"?

A coherent topic, task, or decision that stands on its own. One turn \
might contain multiple units; multiple turns might form one unit. \
Group by logical coherence, not by message boundaries.

## Output format

Emit one <unit> block per unit of work, using EXACTLY this structure:

<unit>
<timestamp>ISO timestamp closest to the unit, or empty</timestamp>
<diary>
Rich narrative of what happened — what was attempted, built, or decided; \
the key decisions made and their rationale; how the work evolved; what \
changed. Write 1-3 substantive paragraphs. This is the PRIMARY output — \
invest most effort here. Plain prose; no escaping needed.
</diary>
<learning>
trust: verified | high | medium
type: OBSERVATION | PREFERENCE | CORRECTION | PATTERN | RULE-PROPOSAL | INTENTION
target: one valid memory file name from the list below, or unknown
description: concise but complete (one sentence, single line)
action: what to add/change in that file (one sentence, single line)
</learning>
</unit>

- Repeat <learning>...</learning> inside a unit for each learning; omit \
it entirely if the unit has none (diary-only units are valid).
- trust: "verified" (confirmed via code/logs/tests) | "high" (strong \
evidence) | "medium" (inference)
- If the entire session is trivial, output exactly: <no-units/>

## Valid target files

Each line is `file — purpose`, with a `NOT:` note naming content that belongs elsewhere:

{valid_targets}

Do NOT invent new file names. Route by the file whose purpose owns the learning's
SUBJECT — not by which tool happened to run the work. Respect the `NOT:` notes: they
name content that belongs in a different file. If no file's domain fits, use
`target: unknown` — downstream consolidation reroutes or filters it. Never force a
learning into the closest broadly-named file.

## Correction detection

When the user corrects Claude's output or assumption:
- type: CORRECTION, trust: verified
Signals: "use X not Y", "no, that's wrong", "actually...", explicit contradictions.
Corrections are highest priority — they prevent recurring mistakes.

## Intention detection (prospective memory)

When the user states something to come back to LATER — a future check, a
revisit, a "when X happens, do Y":
- type: INTENTION, target: prospective.md
Signals: "remind me to...", "revisit this in September", "check back after the
release", "when the runtime updates, re-run X", "let's decide this next quarter".

Write the description in one of exactly two shapes, so it can be stored
machine-readably:
- `due: YYYY-MM-DD — <what to do>` when a date is stated or clearly implied
  (resolve relative dates like "in September" or "in two weeks" against today's
  date, given above).
- `on: <condition in plain words> — <what to do>` when the trigger is an event
  rather than a date. Do NOT invent a date for a condition; "when X ships" has
  no date and guessing one produces a reminder that fires at the wrong time.

Only capture intentions the user actually expressed. An open question Claude
noticed, or work that merely remains unfinished, is not an intention — that
belongs in the diary.

## Verdict detection (revealed preference)

When the user delivers an explicit verdict on something Claude produced —
keeping it, killing it, or asking for more of it:
- type: PREFERENCE, trust: verified, and BEGIN the description with
  `verdict: keep` / `verdict: kill` / `verdict: expand` followed by ` — ` and
  what the verdict was about.
Signals: "this is great, more like this", "never do that again", "drop the
tables", "keep it this short", "stop adding X", "yes, exactly this".

A verdict on a concrete output beats a stated preference: it is what the user
did, not what they said they wanted. Do NOT mark ordinary approval to proceed
("yes", "go ahead", "sounds good") as a verdict — that is consent to an action,
not a judgment about output style.

## Session disposition (how the session was left)

AFTER the last <unit> block, emit exactly one:

<disposition>
state: active | parked | done
reason: one line — the closing words that decided it
</disposition>

Judge ONLY from how the conversation ends — the last few exchanges, not the \
work's overall completeness.

- `done` — the user signalled the work is finished. "we're done", "that's it", \
"ship it", "merged, thanks", "closing this out".
- `parked` — the user signalled they are stopping WITHOUT finishing, intending \
to come back. "park it for now", "let's pick this up tomorrow", "leave it \
here", "shelve this", "I'll come back to this".
- `active` — anything else, and the default. No closing signal, an abrupt end, \
a session that simply stops mid-work, or a question left hanging. **When \
unsure, emit `active`** — mislabelling live work as finished is the costly \
error; leaving finished work labelled active costs nothing.

Half-finished work the user did not comment on is `active`, not `parked`: \
parked means they SAID they were setting it down.

## Rules

- diary is PRIMARY — learnings are a projection of it
- A unit with 0 learnings is fine (diary-only is valid)
- Deduplicate: emit each insight ONCE, even if it spans multiple units
- If something was CORRECTED later, output only the final corrected version
- Skip trivial exchanges, greetings, routine tool usage

## Transcript

{transcript}

## Output

Output ONLY <unit> blocks (or <no-units/>), then one <disposition> block — \
no markdown fences, no explanation.
"""


class ExtractionParseError(ValueError):
    """The model's response could not be parsed into the expected shape.

    Distinct from a genuinely empty extraction (an explicit ``<no-units/>``
    marker): a parse failure means we don't KNOW whether the session had
    learnings, so the caller must retain the marker and retry rather than
    dropping it.
    """


_UNIT_RE = re.compile(r"<unit>(.*?)</unit>", re.DOTALL)
_TIMESTAMP_RE = re.compile(r"<timestamp>(.*?)</timestamp>", re.DOTALL)
_DIARY_RE = re.compile(r"<diary>\n?(.*?)\n?</diary>", re.DOTALL)
_LEARNING_RE = re.compile(r"<learning>(.*?)</learning>", re.DOTALL)
_LEARNING_KEYS = frozenset({"trust", "type", "target", "description", "action"})
_NO_UNITS_MARKER = "<no-units/>"

# --- Session disposition ----------------------------------------------------
# How the session was LEFT, which is a different axis from whether its process
# is running. `Session.status` (working | waiting_input | idle | ended) is
# liveness and is frozen in the multiplai-gui API contract; a session can be
# `ended` and `parked`, or `idle` and `done`. This is a NEW key, never an
# overload of that one.
#
# The design is Spike's, and its virtue is that there is no verb to remember
# at the exact moment you are overwhelmed and leaving: you type "park it for
# now" as you would anyway, and the extraction pass that is already reading
# the whole transcript picks it up.
DISPOSITIONS = ("active", "parked", "done")
DEFAULT_DISPOSITION = "active"

_DISPOSITION_RE = re.compile(r"<disposition>(.*?)</disposition>", re.DOTALL)
_DISPOSITION_STATE_RE = re.compile(r"^\s*state\s*:\s*([a-z]+)", re.MULTILINE | re.IGNORECASE)
_DISPOSITION_REASON_RE = re.compile(r"^\s*reason\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def parse_disposition(raw: str) -> dict:
    """Read the session-level ``<disposition>`` block, if the model emitted one.

    Returns ``{"state": ..., "reason": ...}``, always. **Never raises and
    never fails extraction** — this field is a convenience layered on top of
    a pipeline whose real job is the diary, and an unparseable disposition
    must not cost a session its diary entry.

    Absent, malformed, or an unrecognised state all fall back to ``active``.
    That default is deliberately the safe direction: labelling live work
    "done" hides it from the fleet view and stops the registry protecting it,
    whereas leaving finished work labelled "active" costs nothing but a line.

    The last block wins. Chunked transcripts are handled by the caller, which
    keeps only the final chunk's answer — that is the chunk holding the
    closing exchange, and the closing exchange is the whole signal.
    """
    blocks = _DISPOSITION_RE.findall(raw or "")
    if not blocks:
        return {"state": DEFAULT_DISPOSITION, "reason": ""}

    body = blocks[-1]
    state_match = _DISPOSITION_STATE_RE.search(body)
    state = state_match.group(1).strip().lower() if state_match else ""
    if state not in DISPOSITIONS:
        if state:
            logger.warning("Unrecognised disposition %r; treating as active", state)
        state = DEFAULT_DISPOSITION

    reason_match = _DISPOSITION_REASON_RE.search(body)
    return {"state": state, "reason": reason_match.group(1).strip() if reason_match else ""}


def _parse_learning(block: str) -> Optional[dict]:
    """Parse one <learning> body of ``key: value`` lines.

    Unknown keys and lines without a colon are ignored (values are
    single-line by prompt contract). A learning without a description
    carries no signal — drop it.
    """
    entry: dict = {}
    for line in block.strip().splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key in _LEARNING_KEYS:
            entry[key] = value.strip()
    return entry if entry.get("description") else None


def _parse_units(raw: str) -> list[dict]:
    """Parse tag-delimited model output into unit dicts.

    Non-greedy block matching means a response truncated mid-unit still
    yields every completed <unit> — partial salvage instead of total loss.
    """
    text = raw.strip()
    match = re.search(r"```[a-z]*\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    units: list[dict] = []
    for block in _UNIT_RE.findall(text):
        ts_match = _TIMESTAMP_RE.search(block)
        diary_match = _DIARY_RE.search(block)
        learnings = [
            entry
            for lblock in _LEARNING_RE.findall(block)
            if (entry := _parse_learning(lblock)) is not None
        ]
        units.append({
            "timestamp": ts_match.group(1).strip() if ts_match else "",
            "diary_entry": diary_match.group(1).strip() if diary_match else "",
            "learnings": learnings,
        })
    if units:
        return units
    if _NO_UNITS_MARKER in text:
        # Explicit trivial-session marker — genuinely empty extraction.
        return []
    raise ExtractionParseError(
        "response contained no <unit> blocks and no <no-units/> marker"
    )


async def extract_units(
    text: str,
    *,
    valid_targets: Sequence[Union[str, dict]],
    client,
) -> list[dict]:
    """Call LLM to extract diary units + learnings from transcript text.

    ``valid_targets`` entries are charter dicts from
    :func:`load_target_charters` (or bare filenames as a fallback).
    Returns list of unit dicts with 'timestamp', 'diary_entry', 'learnings'.
    Raises on LLM failure — caller decides whether to continue with
    correction-only output.

    See :func:`extract_units_and_disposition` for the variant that also
    returns how the session was left.
    """
    units, _ = await extract_units_and_disposition(
        text, valid_targets=valid_targets, client=client
    )
    return units


async def extract_units_and_disposition(
    text: str,
    *,
    valid_targets: Sequence[Union[str, dict]],
    client,
) -> tuple[list[dict], dict]:
    """:func:`extract_units`, plus the session-level disposition block.

    Two functions rather than one changed signature: ``extract_units`` has
    several callers and a large test surface, and only the live extraction
    path cares how the session was left. The disposition rides along on the
    *same* model response — it costs no extra call.
    """
    targets_block = (
        "\n".join(render_target_line(t) for t in valid_targets)
        if valid_targets else "(none)"
    )
    # NOT str.format: transcript text routinely contains literal { } (JSON,
    # code, f-strings) which would raise KeyError/ValueError and silently
    # kill extraction. Plain replacement never interprets braces. Replace
    # valid_targets first (controlled), transcript last (untrusted).
    # An INTENTION must resolve "in September" / "in two weeks" to a real date,
    # and the model has no clock. Without this the relative dates it emits are
    # anchored to training time, i.e. wrong, i.e. reminders that fire on the
    # wrong day — worse than no reminder.
    prompt = (
        EXTRACTION_PROMPT
        .replace("{today}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        .replace("{valid_targets}", targets_block)
        .replace("{transcript}", text)
    )
    # Parse failures are stochastic (a fresh sample of the same prompt
    # usually parses), so retry once with the identical prompt rather
    # than surfacing the first bad roll to the caller.
    last_error: Optional[ExtractionParseError] = None
    for attempt in range(2):
        response = await client.query(
            system=(
                "You are a learnings extractor. Output ONLY the "
                "tag-delimited format requested."
            ),
            messages=[{
                "role": "user",
                "content": prompt,
            }],
        )
        try:
            units = _parse_units(response.content)
        except ExtractionParseError as e:
            last_error = e
            logger.warning("extraction parse failed (attempt %d): %s", attempt + 1, e)
            continue
        # Parsed only after the units did: a missing or malformed
        # disposition falls back to "active" rather than costing the
        # session its diary entry.
        return units, parse_disposition(response.content)
    assert last_error is not None  # loop either returned or set last_error
    raise last_error


def write_diary_entries(
    units: list[dict],
    diary_dir: Path,
    session_id: str,
    cwd: str,
    timestamp: str,
) -> Optional[Path]:
    """Atomic append diary entries to per-day file ``diary/YYYY-MM-DD.md``.

    Layout aligned with ``append_learnings``:
      - One file per UTC day; new sessions append a ``## Session: <id>``
        block. Day header ``# Diary — YYYY-MM-DD`` written on first touch.
      - ``fcntl.flock`` on a sibling lock file serialises concurrent
        SessionStart subprocesses writing the same day.
      - Idempotent on ``session_id``: if ``## Session: <id>`` is already
        in the file, this is a no-op and returns the existing path.

    Returns the day file path on write (or existing-session no-op),
    or ``None`` if no units have diary content.
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

    diary_dir.mkdir(parents=True, exist_ok=True)
    diary_file = diary_dir / f"{date_str}.md"
    lock_file = _lock_path(diary_file)

    with open(lock_file, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if session_id and diary_file.exists():
                if f"## Session: {session_id}" in diary_file.read_text():
                    return diary_file

            with open(diary_file, "a", encoding="utf-8") as f:
                if diary_file.stat().st_size == 0:
                    f.write(f"# Diary — {date_str}\n")
                # Session boundary header: id, kickoff ts, cwd. Mirrors
                # the structure synthesize_now and the diary catalog
                # generator parse on.
                f.write(
                    f"\n## Session: {session_id} — {timestamp} — {cwd}\n\n"
                )
                for unit in diary_units:
                    unit_ts = unit.get("timestamp") or timestamp
                    entry = unit["diary_entry"].strip()
                    f.write(f"[{unit_ts}]\n\n{entry}\n\n")

            return diary_file
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


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


def append_learnings(
    units: list[dict],
    learnings_file: Path,
    session_id: str,
    timestamp: str,
) -> bool:
    """Atomic append learnings to per-day file with flock + Session: dedup.

    Returns True if anything was written.
    """
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(learnings_file)

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

            return wrote_any
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
