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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Union

from lib import taxonomy
from lib.runtime import lock_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The diary's bookmark
# ---------------------------------------------------------------------------
#
# ``extract_learnings.py`` used to call ``distill(path)`` with no ``since``,
# re-reading the whole transcript on every pass — an 11.8 MB file read in 10
# chunks for a session that had already had 8 of them extracted. The checkpoint
# writer solved the same problem years-of-commits ago with a bookmark, and the
# reading half of that transfers directly.
#
# The WRITING half must not. The checkpoint folds each slice into one file it
# overwrites; the diary appends each slice permanently, because the diary is
# the record learnings project from and dream consolidates. Diffing two
# checkpoints of one session 28 hours apart showed exactly what overwriting
# costs: "11/21 done, block 13 reviewing" became "21/21 done" and four
# learnings-grade findings simply vanished. So: same bookmark, opposite write
# semantics.
#
# Stored apart from the checkpoint's ``last_checkpoint_ts`` on purpose. The two
# run on different cadences (a save every 30 minutes, an extraction once or
# twice a session) and sharing the field would make each one skip the other's
# slices.

def extraction_state_dir(data_dir: Path) -> Path:
    return data_dir / "extraction_state"


def bookmark_file(data_dir: Path, session_id: str) -> Path:
    return extraction_state_dir(data_dir) / f"{session_id}.json"


def load_diary_bookmark(data_dir: Path, session_id: str) -> Optional[datetime]:
    """The timestamp the last successful extraction of *session_id* reached.

    None when there is none, when it is unreadable, or when it is corrupt —
    all three mean "read the transcript from the beginning", which costs a
    re-read and never costs a slice.
    """
    if not session_id:
        return None
    try:
        raw = bookmark_file(data_dir, session_id).read_text(encoding="utf-8")
        value = json.loads(raw).get("last_extracted_ts")
        if not value:
            return None
        ts = datetime.fromisoformat(str(value))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return None


def save_diary_bookmark(data_dir: Path, session_id: str, ts: datetime) -> bool:
    """Advance *session_id*'s diary bookmark to *ts*. Returns whether it stuck.

    Callers must only reach this after the diary entry is on disk — the same
    discipline the checkpoint writer follows, and for the same reason: a
    bookmark that moves past unwritten work deletes that work silently.
    """
    if not session_id:
        return False
    path = bookmark_file(data_dir, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"session_id": session_id,
                        "last_extracted_ts": ts.isoformat()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except OSError:
        logger.warning("Could not save diary bookmark for %s", session_id)
        return False


def clear_diary_bookmark(data_dir: Path, session_id: str) -> None:
    """Drop the bookmark once the session is finished with (best-effort)."""
    if not session_id:
        return
    try:
        bookmark_file(data_dir, session_id).unlink(missing_ok=True)
    except OSError:
        pass


def slice_key(session_id: str, through: Optional[datetime]) -> str:
    """Identity of one extraction slice, for append-idempotency.

    ``session_id`` alone was the dedup key, which is correct while a session
    extracts exactly once and silently drops the second entry the moment it
    extracts twice. Keying on the slice keeps the guard doing its real job —
    a marker retried after a child died mid-write must not duplicate — while
    letting a genuinely new slice through.

    *through* is where the slice **ends** (the newest turn consumed), not where
    it starts. That matters when ``save_diary_bookmark`` fails: the next pass
    then re-reads from the same start, so a start-keyed slice would collide
    with the marker the previous pass wrote and its genuinely-new turns would
    be dropped as a duplicate. Keyed by the end, the second pass has read
    further and so carries a different key. The retry case still dedups
    correctly, because a retry reads a transcript that has stopped growing and
    therefore lands on the same end timestamp.

    ``None`` (no timestamp available — the raw-stdin path, which cannot
    bookmark) falls back to a per-session key, i.e. the pre-slice behaviour.
    """
    return f"{session_id}:{through.isoformat() if through else 'start'}"


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
    """Return a flock file path for *target*, kept in the plugin data dir's
    ``locks/`` bucket rather than beside the target. The lock files used to be
    written into the user's diary/learnings dirs and never removed, littering
    their (often git-tracked) workspace. A deterministic per-target path
    elsewhere preserves the mutual-exclusion semantics without polluting the
    content dirs.

    It is deliberately NOT in the temp dir, where it used to live: every Claude
    session runs in its own OrbStack container, so ``/tmp`` is container-local
    and two sessions appending to the same diary file would lock two different
    paths and both proceed — precisely the interleaved-write corruption this
    guards against. The data dir is on the shared workspace filesystem.
    """
    digest = hashlib.sha1(str(target.resolve()).encode("utf-8")).hexdigest()[:16]
    return lock_path(f"{target.name}.{digest}")


# Tag-delimited output, NOT JSON. diary_entry is long prose full of quotes,
# backslashes, newlines and code snippets; asking the model to escape that
# inside JSON strings failed strict json.loads on 5/8 real backfill sessions
# (unescaped \escape, unterminated string, missing delimiter), and
# json-repair fallbacks silently dropped ~half the units on a real broken
# sample. Prose between tags needs no escaping, and a truncated response
# still yields every completed <unit> block instead of losing everything.
# Bake-off on the 4 failed sessions, 2026-07-07: tags 12/12 clean.
# Split into a static half and a per-call half, and sent as `system` +
# `messages` rather than one giant user message. Every token before the first
# byte that changes between calls is a cacheable prefix; every token after it
# is re-written at 1.25x input price and never read again. With the transcript
# and the date interleaved into the instructions, that prefix was ~0 bytes and
# extraction ran at a 59% cache-WRITE share (26.7M written vs 18.5M read over
# 1,086 calls, 30d to 2026-08-06) — the worst cache citizen in the ledger.
# EXTRACTION_SYSTEM is byte-identical from one call to the next in the steady
# state; `{valid_targets}` rewrites it when a memory file is added or the
# catalog is regenerated (168h TTL), which costs one write and then caches
# again. EXTRACTION_USER holds the only per-call parts. Do NOT move `{today}`
# or `{transcript}` back into the system half: either one invalidates the
# prefix on every extraction. TestSystemHalfIsACacheablePrefix enforces this —
# no other test in the suite can tell the two halves apart.
#
# The untrusted transcript still sits BETWEEN instructions — the closing
# "## Output" block follows it — and the instructions now arrive over the
# stronger `system` channel. See docs/untrusted-content.md.
EXTRACTION_SYSTEM = """\
You are a learnings extractor. Output ONLY the tag-delimited format requested.

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
provenance: RESEARCH | EMPIRICAL | CORRECTION | DECLARATION | INFERENCE
kind: FACT | RULE | DECISION | INTENTION
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

## Provenance and kind — two separate questions

`provenance` answers **where the knowledge came from**, which is what says how
it could ever be checked again:

- `RESEARCH` — read in an external source: docs, a web page, a paper, someone
  else's repo. Re-checkable by re-reading that source.
- `EMPIRICAL` — observed while doing the work. It broke, it was fixed, the test
  went green. Re-checkable by running the thing again.
- `CORRECTION` — the user said the assistant had it wrong.
- `DECLARATION` — the user stated it unprompted, with no error to overwrite.
- `INFERENCE` — the assistant concluded it and nobody confirmed it.

`kind` answers **what sort of thing it is**, which is a different question:

- `FACT` — can be true or false. "The router caps injection at 40 KB."
- `RULE` — normative, so neither true nor false. "Always stage with a pathspec."
- `DECISION` — a commitment in force until something overturns it. "We're going
  with Postgres."
- `INTENTION` — something to come back to later (see below).

Three distinctions carry almost all of the difficulty:

- **CORRECTION vs DECLARATION** — was there an error to overwrite? "No, the
  default is Opus, not Sonnet" is a CORRECTION. "I prefer short commit
  messages", said out of nowhere, is a DECLARATION.
- **INFERENCE vs EMPIRICAL** — was it *observed* or *concluded*? Watching a
  test fail and then pass after the fix is EMPIRICAL. Reasoning that the fix
  probably also covers the sibling case, without running it, is INFERENCE.
- **FACT vs RULE** — "the API always returns UTF-8" is a FACT (it could turn
  out to be false). "Always decode API responses as UTF-8" is a RULE (it can
  only be revoked).

When provenance is genuinely unclear, answer `INFERENCE`. When kind is
genuinely unclear, answer `RULE`. Both of those send the item to a human rather
than past one — a wrong `EMPIRICAL` or a wrong `FACT` is far more expensive
than an over-cautious label, so guessing upward is the costly mistake.

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
- provenance: CORRECTION, trust: verified
Signals: "use X not Y", "no, that's wrong", "actually...", explicit contradictions.
Corrections are highest priority — they prevent recurring mistakes.

Pick the `kind` from what was corrected, not from the fact that it was a
correction: a wrong value or claim is `FACT`, a correction about how to behave
("don't ask before running read-only commands") is `RULE`.

## Intention detection (prospective memory)

When the user states something to come back to LATER — a future check, a
revisit, a "when X happens, do Y":
- kind: INTENTION, provenance: DECLARATION, target: prospective.md
Signals: "remind me to...", "revisit this in September", "check back after the
release", "when the runtime updates, re-run X", "let's decide this next quarter".

Write the description in one of exactly two shapes, so it can be stored
machine-readably:
- `due: YYYY-MM-DD — <what to do>` when a date is stated or clearly implied
  (resolve relative dates like "in September" or "in two weeks" against today's
  date, stated at the top of the user message).
- `on: <condition in plain words> — <what to do>` when the trigger is an event
  rather than a date. Do NOT invent a date for a condition; "when X ships" has
  no date and guessing one produces a reminder that fires at the wrong time.

Only capture intentions the user actually expressed. An open question Claude
noticed, or work that merely remains unfinished, is not an intention — that
belongs in the diary.

## Verdict detection (revealed preference)

When the user delivers an explicit verdict on something Claude produced —
keeping it, killing it, or asking for more of it:
- kind: RULE (a verdict is normative — it says what to do next time, and it can
  only be revoked, never falsified), provenance: DECLARATION, or CORRECTION when
  the verdict overturns something Claude had just done
- trust: verified, and BEGIN the description with
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

## Memory utilisation (which injected memory this session actually used)

The user message lists the memory sections that were injected into this \
session's context. AFTER the <disposition> block, emit exactly one:

<utilisation>
<used file="FILENAME" section="SECTION NAME">short quote from the session, or a \
concrete statement of what it informed</used>
</utilisation>

- List ONLY sections the session demonstrably RELIED ON. Judge dependence, not \
topical similarity: a section about the same subject the session touched was \
not used unless the work actually leaned on what it said.
- **"None of them" is a normal and expected answer.** Most injected memory goes \
unused. Emit an empty <utilisation></utilisation> block and move on — do not \
look for a reason to name one.
- Every <used> MUST carry evidence in its body. A <used> with an empty body is \
recorded as unsupported and does not count. If you cannot point at anything, \
leave the section out.
- Omit the `section` attribute when the listed entry has no section (the whole \
file was injected). Copy `file` and `section` EXACTLY as listed.
- Never name a file or section that is not in the injected list.
- When the list is empty, emit an empty <utilisation></utilisation> block.

## Rules

- diary is PRIMARY — learnings are a projection of it
- A unit with 0 learnings is fine (diary-only is valid)
- Deduplicate: emit each insight ONCE, even if it spans multiple units
- If something was CORRECTED later, output only the final corrected version
- Skip trivial exchanges, greetings, routine tool usage
"""

EXTRACTION_USER = """\
Today's date: {today}

## Injected memory sections

{injected_sections}

## Transcript

{transcript}

## Output

Output ONLY <unit> blocks (or <no-units/>), then one <disposition> block, then \
one <utilisation> block — no markdown fences, no explanation.
"""

# The two halves as one string, in the order the model sees them. Kept because
# a large test surface asserts on the prompt's content as a whole; nothing at
# runtime sends this — the call site sends the halves separately.
EXTRACTION_PROMPT = EXTRACTION_SYSTEM + "\n" + EXTRACTION_USER


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
# ``provenance`` and ``kind`` are the two-axis taxonomy (lib/taxonomy.py).
#
# ``type`` stays in the accepted set because 227 records on disk still use it
# and nothing rewrites them — dropping it here would make every one of them
# parse as unlabelled. ``trust`` also stays, but it is **deprecated**: it is a
# confidence rating the extractor assigns by feel, with no link to where the
# claim came from, and provenance now answers that question properly. It is
# kept for one release rather than removed in the same change that adds two
# fields, so the two can be evaluated separately.
_LEARNING_KEYS = frozenset(
    {"trust", "type", "provenance", "kind", "target", "description", "action"}
)
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


# --- Memory utilisation (estimator A: self-report) --------------------------
# The session grading its own session. Biased upward by construction, so the
# ONE available brake is the evidence requirement: a claim with an empty body
# is recorded `supported: False` and never counts toward use. See
# lib/utilisation.py for how that flows into the aggregate, and the master
# plan's decision 9 for why this number is always labelled an estimate.

_UTILISATION_RE = re.compile(r"<utilisation>(.*?)</utilisation>", re.DOTALL)
_USED_RE = re.compile(r"<used\b([^>]*)>(.*?)</used>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# Shown when nothing was injected — an explicit statement beats a blank, which
# the model would otherwise try to fill.
NO_INJECTED_SECTIONS = "(none — no memory was injected into this session)"


def render_injected_sections(keys: Sequence[str]) -> str:
    """Render the injected-section list for the prompt, fenced as untrusted.

    Section names come from memory files, which are themselves distilled from
    transcripts that ingested web pages, repos and documents — so they are
    externally-authored text and contract C2 applies (`docs/untrusted-content.md`).
    """
    if not keys:
        return NO_INJECTED_SECTIONS
    from multiplai_core.untrusted import fence

    body = "\n".join(f"- {key}" for key in keys)
    lines = fence(body, "memory catalog — injected section names")
    return "\n".join(lines) if lines else NO_INJECTED_SECTIONS


def parse_utilisation(raw: str) -> Optional[list[dict]]:
    """Read the ``<utilisation>`` block into self-report entries.

    Returns ``[{"file", "section", "evidence", "supported"}]``, or **``None``
    when the response carried no ``<utilisation>`` block at all**. Never raises
    and never fails extraction: this field is telemetry layered on a pass whose
    real job is the diary, and a malformed block must not cost a session its
    diary entry.

    The ``None`` is the whole point, and it used to be ``[]``. "A blank is not a
    zero" is this feature's headline invariant, and the judge half honours it by
    raising ``JudgeParseError`` on a missing block — but this half, which runs on
    **every** session, conflated the two. The distinction the old docstring
    claimed ("the caller knows by whether it passed an injected list") does not
    cover the case that matters: the caller gates on whether the model call
    *succeeded*, and a truncated response is a success that parses. With
    ``DEFAULT_MAX_TOKENS = 4096``, no override on this path, ``_parse_units``
    deliberately salvaging a truncated reply, and ``<utilisation>`` emitted
    **last** — after every diary unit — truncation is the ordinary failure, not
    an exotic one. Recorded as ``[]`` it becomes "observed, and used nothing",
    which ranks a genuinely-used section top of the pruning table with
    ``zero_estimated_use: true``. That table is what the doctor pass reads to
    propose deletions.

    An explicitly empty block still returns ``[]`` — "I used none of them" is a
    real answer and must stay distinguishable from never having answered.
    """
    blocks = _UTILISATION_RE.findall(raw or "")
    if not blocks:
        return None
    out: list[dict] = []
    seen: set[tuple[str, Optional[str]]] = set()
    for attrs, body in _USED_RE.findall(blocks[-1]):
        parsed = dict(_ATTR_RE.findall(attrs))
        file = (parsed.get("file") or "").strip()
        if not file:
            continue
        section = (parsed.get("section") or "").strip() or None
        if (file, section) in seen:
            continue
        seen.add((file, section))
        evidence = " ".join((body or "").split())
        out.append({
            "file": file,
            "section": section,
            "evidence": evidence,
            "supported": bool(evidence),
        })
    return out


def _parse_learning(block: str) -> Optional[dict]:
    """Parse one <learning> body of ``key: value`` lines.

    Unknown keys and lines without a colon are ignored (values are
    single-line by prompt contract). A learning without a description
    carries no signal — drop it.

    ``provenance`` and ``kind`` are validated against their closed value sets.
    An out-of-set value is **dropped, not coerced**: the record then says it
    has no provenance, which is true, rather than claiming the nearest legal
    label, which would be a fabricated statement about where the knowledge came
    from. The description survives either way — the label is the doubtful part,
    not the learning.
    """
    entry: dict = {}
    for line in block.strip().splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key in _LEARNING_KEYS:
            entry[key] = value.strip()
    for key, normalize in (
        ("provenance", taxonomy.normalize_provenance),
        ("kind", taxonomy.normalize_kind),
    ):
        if key not in entry:
            continue
        normalized = normalize(entry[key])
        if normalized is None:
            logger.warning(
                "Learning %s=%r is not in the accepted set — dropped rather than "
                "coerced; the record parses without it", key, entry[key],
            )
            del entry[key]
        else:
            entry[key] = normalized
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

    See :func:`extract_session_signals` for the variant that also returns the
    self-reported memory utilisation.
    """
    units, disposition, _ = await extract_session_signals(
        text, valid_targets=valid_targets, client=client
    )
    return units, disposition


async def extract_session_signals(
    text: str,
    *,
    valid_targets: Sequence[Union[str, dict]],
    client,
    injected_sections: Optional[Sequence[str]] = None,
) -> tuple[list[dict], dict, Optional[list[dict]]]:
    """Units, disposition, and self-reported memory utilisation, in one call.

    ``injected_sections`` is the list of ``file.md#Section`` keys this session
    had injected. Passing it is what makes the utilisation answer meaningful;
    passing nothing still asks the question (the prompt's static half cannot
    vary — it is the cacheable prefix) but tells the model the list is empty,
    so the honest answer is an empty block.

    The third element is ``None`` when the reply carried no ``<utilisation>``
    block — see :func:`parse_utilisation`. The caller must not record that as a
    zero.

    The utilisation answer rides on the *same* response as the diary and the
    learnings, costing no extra call. That is deliberate, and so is the order
    of precedence if it ever conflicts: the diary is this pass's primary
    product, and a shorter diary is not a trade worth making for telemetry —
    split this into its own call before letting that happen.
    """
    targets_block = (
        "\n".join(render_target_line(t) for t in valid_targets)
        if valid_targets else "(none)"
    )
    # NOT str.format: transcript text routinely contains literal { } (JSON,
    # code, f-strings) which would raise KeyError/ValueError and silently
    # kill extraction. Plain replacement never interprets braces. The
    # untrusted transcript is substituted last, and into the user half only —
    # it can never reach the system half, whose placeholders are already gone
    # by then.
    system = EXTRACTION_SYSTEM.replace("{valid_targets}", targets_block)
    # An INTENTION must resolve "in September" / "in two weeks" to a real date,
    # and the model has no clock. Without this the relative dates it emits are
    # anchored to training time, i.e. wrong, i.e. reminders that fire on the
    # wrong day — worse than no reminder.
    prompt = (
        EXTRACTION_USER
        .replace("{today}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        .replace("{injected_sections}",
                 render_injected_sections(injected_sections or []))
        .replace("{transcript}", text)
    )
    # Parse failures are stochastic (a fresh sample of the same prompt
    # usually parses), so retry once with the identical prompt rather
    # than surfacing the first bad roll to the caller.
    last_error: Optional[ExtractionParseError] = None
    for attempt in range(2):
        response = await client.query(
            system=system,
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
        # disposition (or utilisation block) falls back to a safe default
        # rather than costing the session its diary entry.
        return (
            units,
            parse_disposition(response.content),
            parse_utilisation(response.content),
        )
    assert last_error is not None  # loop either returned or set last_error
    raise last_error


SLICE_MARKER = "<!-- slice: {key} -->"


def write_diary_entries(
    units: list[dict],
    diary_dir: Path,
    session_id: str,
    cwd: str,
    timestamp: str,
    slice_id: str = "",
) -> Optional[Path]:
    """Atomic append diary entries to per-day file ``diary/YYYY-MM-DD.md``.

    Layout aligned with ``append_learnings``:
      - One file per UTC day; new sessions append a ``## Session: <id>``
        block. Day header ``# Diary — YYYY-MM-DD`` written on first touch.
      - ``fcntl.flock`` on a sibling lock file serialises concurrent
        SessionStart subprocesses writing the same day.
      - Idempotent. With *slice_id*, on that slice: a session extracting a
        second, later slice appends a second block, while a marker retried
        after its child died mid-write still writes nothing twice. Without
        one, on ``session_id``, exactly as before.

    **Appends. Never replaces.** This file is the permanent record; the
    checkpoint is the overwritten one. Do not merge the two semantics.

    Returns the day file path on write (or an already-written no-op),
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
            marker = SLICE_MARKER.format(key=slice_id) if slice_id else ""
            if diary_file.exists():
                existing = diary_file.read_text()
                if marker:
                    if marker in existing:
                        return diary_file
                elif session_id and f"## Session: {session_id}" in existing:
                    return diary_file

            with open(diary_file, "a", encoding="utf-8") as f:
                if diary_file.stat().st_size == 0:
                    f.write(f"# Diary — {date_str}\n")
                # Session boundary header: id, kickoff ts, cwd. Mirrors
                # the structure synthesize_now and the diary catalog
                # generator parse on. The slice marker rides BELOW it, as a
                # comment, so the header's shape (and the regexes reading it)
                # are untouched.
                f.write(
                    f"\n## Session: {session_id} — {timestamp} — {cwd}\n\n"
                )
                if marker:
                    f.write(f"{marker}\n\n")
                for unit in diary_units:
                    unit_ts = unit.get("timestamp") or timestamp
                    entry = unit["diary_entry"].strip()
                    f.write(f"[{unit_ts}]\n\n{entry}\n\n")

            return diary_file
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _format_learning_entry(learning: dict) -> str:
    """Render one learning as the line that lands in ``.multiplai/learnings/``.

    Two forms, chosen by what the record actually carries:

    - ``- **[CORRECTION/FACT]** <desc> → Target: <file> — <action>`` when the
      record states a provenance or a kind of its own.
    - the legacy ``- **[trust: verified]** CORRECTION <desc> → …`` when it has
      only the old ``type``.

    A legacy record is never reprinted in the new form. The mapping in
    ``taxonomy.LEGACY_TYPE_MAP`` exists for *reading*, and printing its output
    into a file would turn a conservative reading into a written-down claim
    about origin that the record never made.

    ``trust`` is dropped from the new line while staying in the record. Two
    confidence-ish markers on one line is what made the old format ambiguous —
    the reader could not tell whether ``trust: verified`` or ``CORRECTION``
    was the load-bearing part.
    """
    desc = (learning.get("description") or "").strip()
    target = (learning.get("target") or "").strip()
    action = (learning.get("action") or "").strip()
    if taxonomy.has_taxonomy(learning):
        provenance, kind = taxonomy.pair(learning)
        line = f"- **[{taxonomy.format_pair(provenance, kind)}]** {desc}"
    else:
        trust = learning.get("trust", "medium")
        ltype = learning.get("type", "OBSERVATION")
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
    slice_id: str = "",
) -> bool:
    """Atomic append learnings to per-day file with flock + dedup.

    Dedup follows the diary's: on *slice_id* when given, on ``session_id``
    otherwise. Without the slice key a session's second extraction pass would
    match the first pass's ``Session:`` line and silently drop every learning
    it found.

    Returns True if anything was written.
    """
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(learnings_file)

    with open(lock_file, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            marker = f"Slice: {slice_id}" if slice_id else ""
            if learnings_file.exists():
                existing = learnings_file.read_text()
                if marker:
                    if marker in existing:
                        return False
                elif session_id and f"Session: {session_id}" in existing:
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
                    if marker:
                        f.write(f"{marker}\n")
                    for learning in learnings:
                        if not isinstance(learning, dict):
                            continue
                        f.write(_format_learning_entry(learning) + "\n")
                    wrote_any = True

            return wrote_any
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
