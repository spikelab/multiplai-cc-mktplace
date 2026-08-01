"""Block-level ledger of which LEARNINGS dream has already consolidated.

Named `learnings_ledger`, not `dream_ledger`, on purpose. A `dream_ledger.py`
was proposed once before as a **sidecar decision record** — which item of a
proposal was applied or rejected — and abandoned when the move-to-processed
contract shipped instead (`## Processed` in the proposal file itself, mirrored
in multiplai-gui's `hub/src/multiplai_hub/dreams.py`). That decision stands and
this module does not revisit it: it tracks dream's **input**, never its output.
Nothing here records a decision, and nothing here is read by the GUI.


Dream used to glob ``.multiplai/learnings/*.md`` and process whatever it found,
all or nothing. Feeding it a subset therefore meant physically moving the other
files out and back, guarded by a shell ``trap … EXIT`` — which is not reliable:
a killed run on 2026-07-31 left four files (204 KB) stranded in a
``.parked-backup/`` directory, invisible to every later run until someone
noticed. Each hand-batched slice also produced its own proposal, so the reviewer
was left reconciling several overlapping documents.

The ledger replaces all of that. Learnings files are **never moved or deleted**
here; instead each ``## Session Learnings`` record is hashed, and dream
consolidates only the records whose hash it has not seen. "Process what's new"
becomes a set difference, a killed run resumes instead of redoing, and repeated
runs are idempotent.

Deletion of learnings files happens in exactly two places: ``dream --auto`` after a
successful apply, and ``dream --gc-learnings``, which removes a file only once
every record in it is recorded here **and** no proposal citing it is still
pending. :func:`prune` then drops the orphaned keys.

Concurrency: callers hold dream's exclusive run lock, so the read-modify-write in
:func:`record` needs no locking of its own. The write is still atomic (temp file
+ ``os.replace``) because a kill mid-write would otherwise corrupt the one file
that says what has already been consolidated — losing it silently re-processes
the entire backlog.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LEDGER_VERSION = 1

# A learnings record opens with this heading and runs to the next one. The
# writer (lib/extraction.py) appends records separated by a `---` rule; the
# separator and any preamble before the first heading belong to no record.
_BLOCK_START_RE = re.compile(r"^##\s+Session Learnings\b")
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")

# 64 bits of key. The corpus is thousands of records, not billions, and a key
# collision would silently drop one learning — 16 hex chars keeps the ledger
# readable while leaving that risk far below the odds of the extractor writing
# the same record twice.
_KEY_CHARS = 16


@dataclass(frozen=True)
class Block:
    """One ``## Session Learnings`` record, located in its source file.

    ``start_line``/``end_line`` are 1-indexed and inclusive, matching what an
    editor shows and what dream's ``**Source:**`` provenance cites. They are
    carried so a chunk can be rebuilt with its ORIGINAL line numbers — renumbering
    a chunk would silently corrupt every provenance line drawn from it.
    """

    file: str
    start_line: int
    end_line: int
    text: str
    key: str


def _normalize(text: str) -> str:
    """Canonical form for hashing: trailing whitespace stripped per line.

    Keys must survive a whitespace-only reformat of a learnings file, or a
    stray editor save would orphan every key in it and re-process the lot.
    Content changes still change the key — an edited record is a new record,
    which is the conservative direction (re-consolidate rather than skip).
    """
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def block_key(text: str) -> str:
    """Stable key for a record's text.

    Deliberately does NOT include the source filename: the same record text in
    two files is the same learning (the extractor can duplicate one across a
    day boundary), and consolidating it twice would put a duplicate entry in
    front of the reviewer.
    """
    digest = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()
    return digest[:_KEY_CHARS]


def parse_blocks(file_name: str, text: str) -> list[Block]:
    """Split one learnings file into its records, in file order.

    Anything before the first ``## Session Learnings`` heading is preamble and
    is dropped: it carries no learning, and including it would make every key in
    the file depend on the file's header.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _BLOCK_START_RE.match(line)]
    if not starts:
        return []

    blocks: list[Block] = []
    bounds = starts + [len(lines)]
    for start, next_start in zip(bounds, bounds[1:]):
        end = next_start - 1
        # Trim the separator rule and blank lines that belong to the gap
        # between records, not to this one.
        while end > start and (
            not lines[end].strip() or _SEPARATOR_RE.match(lines[end])
        ):
            end -= 1
        body = "\n".join(lines[start:end + 1])
        blocks.append(
            Block(
                file=file_name,
                start_line=start + 1,
                end_line=end + 1,
                text=body,
                key=block_key(body),
            )
        )
    return blocks


def default_ledger_path() -> Path:
    """Ledger location: the git-ignored per-skill state bucket.

    ``skill_state_dir`` creates the directory and drops the ``data_dir``
    ``.gitignore`` on first access, so the ledger never lands in a commit.
    Imported lazily so this module stays usable in tests without a resolved
    workspace.
    """
    from multiplai_core.paths import get_paths

    return get_paths().skill_state_dir("dream") / "ledger.json"


def load(path: Path) -> dict:
    """Read the ledger, or an empty one.

    Fail-open on a missing, empty or corrupt file: an unreadable ledger must
    degrade to "nothing processed yet" (re-consolidating is wasteful but safe)
    rather than crash the run and leave the backlog untouched forever.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": LEDGER_VERSION, "processed": {}}
    if not isinstance(data, dict) or not isinstance(data.get("processed"), dict):
        return {"version": LEDGER_VERSION, "processed": {}}
    data.setdefault("version", LEDGER_VERSION)
    return data


def save(path: Path, ledger: dict) -> None:
    """Atomically write the ledger (temp file in the same dir + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(ledger, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def unprocessed(blocks: list[Block], ledger: dict) -> list[Block]:
    """Records not yet consolidated, in input order, de-duplicated by key.

    Two identical records in the same batch collapse to one — otherwise the
    reviewer sees the same proposed memory entry twice.
    """
    seen = ledger.get("processed", {})
    out: list[Block] = []
    batch: set[str] = set()
    for b in blocks:
        if b.key in seen or b.key in batch:
            continue
        batch.add(b.key)
        out.append(b)
    return out


def record(path: Path, blocks: list[Block], proposal: str) -> int:
    """Mark *blocks* consolidated into *proposal*. Returns how many were new.

    Called per chunk, as each draft lands, rather than once at the end: a crash
    then costs only the in-flight chunks, and the next run resumes with the rest.
    """
    if not blocks:
        return 0
    ledger = load(path)
    processed = ledger.setdefault("processed", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    for b in blocks:
        if b.key in processed:
            continue
        processed[b.key] = {"file": b.file, "proposal": proposal, "at": now}
        added += 1
    save(path, ledger)
    return added


def prune(path: Path, existing_files: set[str]) -> int:
    """Drop keys whose source file is gone. Returns how many were removed.

    ``--auto`` and ``/dream-remember`` delete learnings files once applied; their
    keys would otherwise accumulate forever. Pruning is safe because a deleted
    file's records cannot come back — and if one did (restored from git), it is
    genuinely new input to consolidate again.
    """
    ledger = load(path)
    processed = ledger.get("processed", {})
    stale = [k for k, v in processed.items() if v.get("file") not in existing_files]
    for k in stale:
        del processed[k]
    if stale:
        save(path, ledger)
    return len(stale)
