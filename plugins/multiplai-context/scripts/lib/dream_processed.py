"""Cross-tool ``## Processed`` coordination for dream proposals.

THE CROSS-REPO CONTRACT (both writers MUST agree byte-for-byte)
==============================================================

A dream proposal ``.md`` is *itself* the decision record — there is **no
sidecar and no key scheme**. When an item is decided (applied, edited, or
rejected) its block is **moved to a ``## Processed`` section** appended at the
end of the file. The proposal parser ignores any unrecognized ``## `` section,
so a moved item is no longer *pending*. That is the whole cross-tool contract:
each side (the multiplai-gui hub GUI and this plugin's ``/dream-remember``) sees
only the items still above the processed section, and moving an item down is how
a review is split between the GUI and the CLI without double-applies,
re-presented items, or orphaned state.

The canonical implementation lives in the hub at
``multiplai-gui``'s ``hub/src/multiplai_hub/dreams.py`` (shipped in PR #29,
"the proposal .md IS the decision record"). This module ports the shared writer
verbatim so both repos produce a byte-identical ``## Processed`` block. **Do not
diverge these two files** — if the hub's ``move_to_processed`` /
``_find_block`` / ``PROCESSED_HEADING`` / regex set changes, mirror it here (and
vice-versa). Only the ``## Processed`` heading and the item/group headings need
to agree; the ``**Processed:**`` annotation is history, never re-parsed.

Item reference scheme (used only as a CLI address for ``--mark-processed`` — it
is NOT persisted anywhere): updates → ``update:<raw-target-file>#<index>``;
action items → ``action:A<index>``. ``<raw-target-file>`` is the exact string
from the proposal's ``## Updates for `<file>` `` heading (never a resolved
path); ``<index>`` is the ``### N.`` number (per-file, may repeat across files);
``A<index>`` is the ``### A{N}.`` number.
"""

import re
from datetime import UTC, datetime

# --- parser regexes (identical to the hub's dreams.py) -------------------------
_GROUP_RE = re.compile(r"^## Updates for `(?P<file>[^`]+)`(?:\s.*)?$")
_ACTIONS_HEADER_RE = re.compile(r"^## Action Items\b")
_UPDATE_RE = re.compile(r"^### (?P<index>\d+)\.\s*(?P<summary>.+?)\s*$")
_ACTION_ITEM_RE = re.compile(r"^### A(?P<index>\d+)\.\s*(?P<summary>.+?)\s*$")


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- processed section (the proposal .md is the decision record) ------------------
#
# A decided item's block is moved out of its ``## Updates for``/``## Action
# Items`` group into a ``## Processed`` section appended at the end of the
# proposal. The parser skips any unrecognized ``## `` section, so a moved item
# stops being pending — retries can't re-apply it, and both the GUI and
# ``/dream-remember`` naturally present only what is still above the processed
# section. The moved block is kept verbatim (so it can be restored by hand) with
# one ``**Processed:**`` status line inserted after its heading; the annotation
# is history, never re-parsed, so the two writers do not need to agree on it
# byte-for-byte — only on the ``## Processed`` heading itself.

PROCESSED_HEADING = "## Processed"
_PROCESSED_NOTE = (
    "_Items decided via the GUI or `/dream-remember`, moved here so they are no "
    "longer pending. Kept for history; delete the `**Processed:**` line and move "
    "a block back up to restore it._"
)
# A block ends at the next item/section heading or a horizontal-rule separator.
_BLOCK_BOUNDARY_RE = re.compile(r"^(?:#{2,3}\s|---\s*$)")


def _find_block(lines: list[str], ref: tuple) -> tuple[int, int] | tuple[None, None]:
    """Line range ``[start, end)`` of ``ref``'s block, trailing blanks trimmed.

    Group-aware (mirrors the parser): an update matches only under its own
    ``## Updates for`` target, an action only inside ``## Action Items``. A block
    already under ``## Processed`` never matches, so moving is idempotent.
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
    proposal_path,
    ref: tuple,
    status: str,
    *,
    target: str | None = None,
    ts: str | None = None,
) -> bool:
    """Move a decided item to ``## Processed`` in the proposal file (write-then-
    rename). No-op if the file is gone or the item is already processed. Returns
    ``True`` if the file changed. This is the commit step: it runs right after
    the memory/PLANS write, so a crash can only ever leave a just-written item
    still pending — never a silent loss."""
    try:
        text = proposal_path.read_text()
    except OSError:
        return False
    new = move_to_processed(text, ref, status, target=target, ts=ts)
    if new != text:
        tmp = proposal_path.with_name(proposal_path.name + ".tmp")
        tmp.write_text(new)
        tmp.replace(proposal_path)
        return True
    return False


# -- pending detection (mirrors the hub's parse_proposal item counting) ----------


def pending_items(text: str) -> list[tuple]:
    """Refs of items still *pending* (above ``## Processed``), in file order.

    Mirrors the hub's ``parse_proposal`` state machine: an ``## Updates for``
    heading opens a target group, ``## Action Items`` opens the actions group,
    and any other ``## `` heading (including ``## Processed`` and
    ``## Filtered Out``) closes both — so items moved under ``## Processed`` are
    not counted. Updates → ``("update", file, index)``; actions →
    ``("action", index)``.
    """
    refs: list[tuple] = []
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
        if target is not None:
            m = _UPDATE_RE.match(line)
            if m:
                refs.append(("update", target, int(m.group("index"))))
        elif in_actions:
            m = _ACTION_ITEM_RE.match(line)
            if m:
                refs.append(("action", int(m.group("index"))))
    return refs


def count_pending(text: str) -> int:
    """How many items are still pending (not yet moved to ``## Processed``)."""
    return len(pending_items(text))


# -- CLI ref encoding (address only; never persisted) ----------------------------


def item_key(ref: tuple) -> str:
    """Encode a ref tuple as its CLI string: ``update:<file>#<index>`` /
    ``action:A<index>``."""
    if ref[0] == "update":
        return f"update:{ref[1]}#{ref[2]}"
    return f"action:A{ref[1]}"


def parse_ref(refstr: str) -> tuple:
    """Parse a ``--ref`` string into a ref tuple. Inverse of ``item_key``.

    ``update:<file>#<index>`` → ``("update", <file>, <index>)``
    ``action:A<index>``       → ``("action", <index>)``
    Raises ``ValueError`` on a malformed ref.
    """
    if refstr.startswith("update:"):
        body = refstr[len("update:") :]
        if "#" not in body:
            raise ValueError(f"malformed update ref (missing '#index'): {refstr!r}")
        file, _, index = body.rpartition("#")
        if not file or not index.isdigit():
            raise ValueError(f"malformed update ref: {refstr!r}")
        return ("update", file, int(index))
    if refstr.startswith("action:A"):
        index = refstr[len("action:A") :]
        if not index.isdigit():
            raise ValueError(f"malformed action ref: {refstr!r}")
        return ("action", int(index))
    raise ValueError(
        f"unrecognized ref {refstr!r} — expected 'update:<file>#<N>' or 'action:A<N>'"
    )
