"""Processed-section handling for dream proposals — the plugin half of the
in-file decision record shared with the multiplai-gui hub.

When a proposal item is decided (applied, edited, or rejected) its block is
**moved** out of its ``## Updates for``/``## Action Items`` group into a
``## Processed`` section at the end of the proposal ``.md``. Both the hub and
``/dream-remember`` treat anything under that heading as no-longer-pending, so
the file itself is the decision record: whoever reviews next (GUI or CLI) sees
only what is still above the processed section.

That one heading — ``## Processed`` — is the entire cross-tool contract. There
is no sidecar, no key scheme, and the ``**Processed:**`` annotation is history
that is never re-parsed, so the two writers do not need to agree on it
byte-for-byte. Mirror of multiplai-gui ``hub/src/multiplai_hub/dreams.py``
(``move_to_processed`` / ``mark_processed``); keep the two in sync only on the
``## Processed`` heading.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

_GROUP_RE = re.compile(r"^## Updates for `(?P<file>[^`]+)`(?:\s.*)?$")
_ACTIONS_HEADER_RE = re.compile(r"^## Action Items\b")
_UPDATE_RE = re.compile(r"^### (?P<index>\d+)\.")
_ACTION_ITEM_RE = re.compile(r"^### A(?P<index>\d+)\.")
# A block ends at the next item/section heading or a horizontal-rule separator.
_BLOCK_BOUNDARY_RE = re.compile(r"^(?:#{2,3}\s|---\s*$)")

PROCESSED_HEADING = "## Processed"
_PROCESSED_NOTE = (
    "_Items decided via `/dream-remember` or the GUI, moved here so they are no "
    "longer pending. Kept for history; delete the `**Processed:**` line and move "
    "a block back up to restore it._"
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_block(lines: list[str], ref: tuple):
    """Line range ``[start, end)`` of ``ref``'s block, trailing blanks trimmed;
    ``(None, None)`` if not found. Group-aware: an update matches only under its
    own ``## Updates for`` target, an action only inside ``## Action Items``. A
    block already under ``## Processed`` never matches, so moving is idempotent.
    """
    want_update = ref[0] == "update"
    want_target = ref[1] if want_update else None
    want_index = ref[2] if want_update else ref[1]
    target: str | None = None
    in_actions = False
    for i, line in enumerate(lines):
        group = _GROUP_RE.match(line)
        if group:
            target, in_actions = group.group("file"), False
            continue
        if _ACTIONS_HEADER_RE.match(line):
            target, in_actions = None, True
            continue
        if line.startswith("## "):
            target, in_actions = None, False
            continue
        if want_update and target == want_target:
            m = _UPDATE_RE.match(line)
        elif not want_update and in_actions:
            m = _ACTION_ITEM_RE.match(line)
        else:
            continue
        if m and int(m.group("index")) == want_index:
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if _BLOCK_BOUNDARY_RE.match(lines[j]):
                    end = j
                    break
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return i, end
    return None, None


def _processed_line(status: str, target: str | None, ts: str) -> str:
    if target and status in ("applied", "edited"):
        return f"**Processed:** {status} → {target} · {ts}"
    return f"**Processed:** {status} · {ts}"


def move_to_processed(
    text: str, ref: tuple, status: str, *, target: str | None = None, ts: str | None = None
) -> str:
    """Relocate ``ref``'s block into the ``## Processed`` section. Idempotent:
    if the item is already processed (not found in a pending group), returns
    ``text`` unchanged."""
    ts = ts or _now_iso()
    lines = text.splitlines()
    start, end = _find_block(lines, ref)
    if start is None:
        return text
    block = lines[start:end]
    del lines[start:end]
    annotated = [block[0], _processed_line(status, target, ts), *block[1:]]
    while lines and not lines[-1].strip():
        lines.pop()
    if not any(line.strip() == PROCESSED_HEADING for line in lines):
        lines += ["", "---", "", PROCESSED_HEADING, "", _PROCESSED_NOTE]
    lines += ["", *annotated]
    return "\n".join(lines) + "\n"


def mark_processed(
    proposal_path: Path,
    ref: tuple,
    status: str,
    *,
    target: str | None = None,
    ts: str | None = None,
) -> bool:
    """Move a decided item to ``## Processed`` in the proposal file (write-then-
    rename). Returns ``True`` if the file changed. No-op (``False``) if the file
    is gone or the item is already processed."""
    try:
        text = proposal_path.read_text()
    except OSError:
        return False
    new = move_to_processed(text, ref, status, target=target, ts=ts)
    if new == text:
        return False
    tmp = proposal_path.with_name(proposal_path.name + ".tmp")
    tmp.write_text(new)
    tmp.replace(proposal_path)
    return True


def has_pending_items(text: str) -> bool:
    """True if any update/action item is still outside ``## Processed`` — the
    archive backstop: a proposal must be fully processed before it is archived."""
    target: str | None = None
    in_actions = False
    for line in text.splitlines():
        group = _GROUP_RE.match(line)
        if group:
            target, in_actions = group.group("file"), False
            continue
        if _ACTIONS_HEADER_RE.match(line):
            target, in_actions = None, True
            continue
        if line.startswith("## "):
            target, in_actions = None, False
            continue
        if target is not None and _UPDATE_RE.match(line):
            return True
        if in_actions and _ACTION_ITEM_RE.match(line):
            return True
    return False
