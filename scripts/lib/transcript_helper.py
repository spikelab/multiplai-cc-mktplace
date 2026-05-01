"""Read the last assistant response from a Claude Code transcript.

The UserPromptSubmit hook receives ``transcript_path`` in its stdin
payload — a JSONL file with one record per turn. The router can use
the most recent assistant turn to disambiguate the user prompt
(e.g., distinguishing API "costs" from personal-finance "costs"
based on what was just being discussed).

Tail-reading is deliberately cheap (<50ms even on multi-MB transcripts)
so the hook stays well within the 5-second timeout. Any failure
(missing path, malformed JSONL, no assistant turn yet) returns
``None`` — the router falls back to prompt-only routing without
blowing up.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Tail size in bytes — large enough to capture a typical final
# assistant message even after tool blocks, small enough to read
# instantly. Most transcripts have <16KB final turns.
_TAIL_BYTES = 65_536


def read_last_assistant_response(transcript_path: Path | str | None) -> str | None:
    """Return plain text of the most recent assistant turn, or ``None``.

    Reads only the tail of the file to keep the hook fast. Walks the
    parsed records from the end, picks the first ``role: assistant``
    record, and extracts text content (handling both string and
    structured-block content shapes used by the SDK).

    Returns ``None`` on any failure — missing file, unreadable, no
    assistant turn yet, malformed JSONL. Callers must treat ``None``
    as "no last-response available, route on prompt alone."
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.exists() or not path.is_file():
        return None

    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
                # Drop the (likely partial) first line.
                f.readline()
            tail = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        logger.debug("Could not read transcript %s: %s", path, e)
        return None

    # Walk lines from the end so we find the most recent assistant turn first.
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        text = _extract_assistant_text(record)
        if text is not None:
            return text
    return None


def _extract_assistant_text(record: object) -> str | None:
    """Pull plain text from a transcript record if it's an assistant turn.

    Handles both shapes Claude Code emits today:
      - ``{"type": "assistant", "message": {"content": [...]}}`` (SDK shape)
      - ``{"role": "assistant", "content": "..."}`` (raw shape)
      - ``{"role": "assistant", "content": [{"type": "text", "text": "..."}]}``
    """
    if not isinstance(record, dict):
        return None

    role = record.get("role") or record.get("type")
    if role != "assistant":
        return None

    # SDK wraps the actual message under "message"
    message = record.get("message", record)
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts).strip() or None
    return None
