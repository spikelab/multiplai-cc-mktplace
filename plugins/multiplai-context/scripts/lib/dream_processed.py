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

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_GROUP_RE = re.compile(r"^## Updates for `(?P<file>[^`]+)`(?:\s.*)?$")
_ACTIONS_HEADER_RE = re.compile(r"^## Action Items\b")
# These two are duplicated in multiplai-gui's `hub/src/multiplai_hub/dreams.py`
# on purpose — the cross-tool contract is the `## Processed` heading, not a
# shared library, and vendoring one into the other would couple a web service
# to a plugin's release cycle. Duplication is the cheaper of the two costs.
#
# What it is *not* is licence to drift. These had already diverged on day one:
# the plugin accepted a bare `### 5.` that the hub rejected, so a malformed
# heading would have been an update here and not there — the two tools
# disagreeing about what the same file says, which is the one failure this
# format exists to prevent. Keep them byte-identical; change both or neither.
_UPDATE_RE = re.compile(r"^### (?P<index>\d+)\.\s*(?P<summary>.+?)\s*$")
_ACTION_ITEM_RE = re.compile(r"^### A(?P<index>\d+)\.\s*(?P<summary>.+?)\s*$")
# Conflict resolutions are the third item kind, and the only one not numbered:
# dream keys them by the memory line they propose to supersede, so the identity
# is `file` + `line`, e.g. "### `dolcebot.md` line 453" (issue #201).
_CONFLICTS_HEADER_RE = re.compile(r"^## Conflict Resolutions\b")
_CONFLICT_RE = re.compile(r"^### `(?P<file>[^`]+)` line (?P<index>\d+)\s*$")
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
    kind = ref[0]
    # update -> ("update", group file, index); conflict -> ("conflict", memory
    # file, line); action -> ("action", index).
    want_target = ref[1] if kind in ("update", "conflict") else None
    want_index = ref[2] if kind in ("update", "conflict") else ref[1]
    target: str | None = None
    in_actions = False
    in_conflicts = False
    for i, line in enumerate(lines):
        group = _GROUP_RE.match(line)
        if group:
            target, in_actions, in_conflicts = group.group("file"), False, False
            continue
        if _ACTIONS_HEADER_RE.match(line):
            target, in_actions, in_conflicts = None, True, False
            continue
        if _CONFLICTS_HEADER_RE.match(line):
            target, in_actions, in_conflicts = None, False, True
            continue
        if line.startswith("## "):
            target, in_actions, in_conflicts = None, False, False
            continue
        if kind == "update" and target == want_target:
            m = _UPDATE_RE.match(line)
        elif kind == "action" and in_actions:
            m = _ACTION_ITEM_RE.match(line)
        elif kind == "conflict" and in_conflicts:
            m = _CONFLICT_RE.match(line)
            # The conflict heading carries its own file, so unlike an update
            # (located by the group heading above it) the file must match here.
            if m and m.group("file") != want_target:
                continue
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


def _relocate(
    lines: list[str], ref: tuple, status: str, target: str | None, ts: str
) -> bool:
    """Move ``ref``'s block to the end of ``## Processed``, in place.

    The single-item and batch entry points share this so a batched call and N
    single-flag calls cannot drift: there is exactly one implementation of what
    a decided block looks like on disk.
    """
    start, end = _find_block(lines, ref)
    if start is None:
        return False
    block = lines[start:end]
    del lines[start:end]
    annotated = [block[0], _processed_line(status, target, ts), *block[1:]]
    while lines and not lines[-1].strip():
        lines.pop()
    if not any(line.strip() == PROCESSED_HEADING for line in lines):
        lines += ["", "---", "", PROCESSED_HEADING, "", _PROCESSED_NOTE]
    lines += ["", *annotated]
    return True


def move_to_processed(
    text: str, ref: tuple, status: str, *, target: str | None = None, ts: str | None = None
) -> str:
    """Relocate ``ref``'s block into the ``## Processed`` section. Idempotent:
    if the item is already processed (not found in a pending group), returns
    ``text`` unchanged."""
    ts = ts or _now_iso()
    lines = text.splitlines()
    if not _relocate(lines, ref, status, target, ts):
        return text
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


# ---------------------------------------------------------------------------
# Batch marking
# ---------------------------------------------------------------------------
#
# A review of a real proposal decides 50–70 items. Marking them one flag-
# invocation at a time costs one ``uv run`` cold start each — a fresh interpreter
# and environment check per item, to move text inside one local file — and that
# per-item cost is what fills a reviewing session's context window
# until it has to hand off mid-review. The batch path takes the same decisions in
# one process, one read and one write.
#
# Deliberately NOT the hub's vocabulary: ``status`` here is an *outcome*
# (``applied``/``edited``/``rejected``) that has already happened, where the hub's
# ``Decision.status`` is an *intent* (``approve``/``reject``/``edit``) that has
# not been carried out yet. The field names ``kind``/``file``/``index`` do mirror
# the hub's model, because those identify the same item in the same document.

_VALID_KINDS = ("update", "action", "conflict")
_VALID_STATUSES = ("applied", "edited", "rejected")


@dataclass(frozen=True)
class Decision:
    """One already-carried-out decision about one proposal item.

    ``file`` is the ``## Updates for `file` `` group the item lives under (what
    locates it); ``target`` is the memory file the text was actually written to
    (what the ``**Processed:**`` annotation records). They are usually equal, and
    a reroute is exactly when they are not.

    For ``kind="conflict"`` the pair means something slightly different but
    consistent: ``file`` is the memory file named in the conflict heading and
    ``index`` is the **line number** in it, because that is what identifies a
    conflict resolution — there is no ``### N.`` to key on.
    """

    kind: str
    index: int
    status: str
    file: str | None = None
    target: str | None = None

    @property
    def ref(self) -> tuple:
        if self.kind == "update":
            return ("update", self.file, self.index)
        if self.kind == "conflict":
            return ("conflict", self.file, self.index)
        return ("action", self.index)

    @property
    def label(self) -> str:
        if self.kind == "update":
            return f"update {self.file}#{self.index}"
        if self.kind == "conflict":
            return f"conflict {self.file}:{self.index}"
        return f"action A{self.index}"

    @classmethod
    def from_dict(cls, raw: object) -> "Decision":
        """Build from one JSON object, raising ``ValueError`` on anything the
        writer would otherwise silently skip."""
        if not isinstance(raw, dict):
            raise ValueError(f"decision must be an object, got {type(raw).__name__}")
        kind = raw.get("kind", "update")
        if kind not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")
        status = raw.get("status")
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {_VALID_STATUSES}, got {status!r}")
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"index must be an integer, got {index!r}")
        file = raw.get("file")
        if kind == "update" and not file:
            raise ValueError("kind 'update' needs a 'file' (its `## Updates for` group)")
        if kind == "conflict" and not file:
            raise ValueError(
                "kind 'conflict' needs a 'file' (the memory file in its heading); "
                "'index' is the line number in that file"
            )
        target = raw.get("target")
        return cls(kind=kind, index=index, status=status, file=file, target=target)


def order_decisions(decisions: list[Decision]) -> list[Decision]:
    """Canonical application order: groups in first-appearance order, and
    **descending by index within each group**.

    Descending index is the safety property the plan asks for: it holds even for
    an implementation that located items positionally, where moving item 1 out
    would shift item 2. (:func:`_find_block` matches on the ``### N.`` label, so
    ordering does not change *which* block is found — but the invariant is cheap
    and the batch path is not the place to depend on that detail.)

    Groups keep first-appearance order rather than sorting, so a batch produces
    the same ``## Processed`` ordering — and therefore the same bytes — as the
    same decisions applied one flag-invocation at a time in the order given.
    Sorting the groups would reorder the appended blocks and break that.
    """
    groups: dict[tuple, list[Decision]] = {}
    for d in decisions:
        groups.setdefault((d.kind, d.file), []).append(d)
    out: list[Decision] = []
    for members in groups.values():
        out.extend(sorted(members, key=lambda d: -d.index))
    return out


def move_many_to_processed(
    text: str, decisions: list[Decision], *, ts: str | None = None
) -> tuple[str, int]:
    """Apply every decision to *text* in one pass. Returns ``(new_text, marked)``.

    One shared timestamp for the batch: the decisions were made in one review,
    and per-item clock reads would only add noise to a history line nothing
    re-parses.
    """
    ts = ts or _now_iso()
    lines = text.splitlines()
    marked = 0
    for d in order_decisions(decisions):
        if _relocate(lines, d.ref, d.status, d.target, ts):
            marked += 1
    if not marked:
        return text, 0
    return "\n".join(lines) + "\n", marked


def mark_many_processed(
    proposal_path: Path, decisions: list[Decision], *, ts: str | None = None
) -> tuple[int, int]:
    """Mark many decided items in one read/write. Returns ``(marked, unchanged)``.

    The whole document is built in memory and swapped in with ``os.replace``, so
    a failure part-way through leaves the proposal byte-identical to what it was
    — never half-decided, which no reader could tell apart from a real review
    that stopped early.
    """
    text = proposal_path.read_text()
    new, marked = move_many_to_processed(text, decisions, ts=ts)
    if marked:
        fd, tmp = tempfile.mkstemp(
            dir=str(proposal_path.parent), prefix=f".{proposal_path.name}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, proposal_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return marked, len(decisions) - marked


def latest_pending_proposal(dreams_dir: Path) -> Path | None:
    """Newest pending proposal in the dreams **root**, by mtime.

    Root only — ``applied/``, ``rejected/`` and ``superseded/`` hold decided
    proposals. By mtime and not by name: a same-day re-run writes ``-2``,
    ``-3``… which is newer but sorts *before* the base name, because ``-``
    (0x2D) sorts before ``.`` (0x2E). Every tool that picks a proposal for the
    user must agree on which one that is, so they all call this.
    """
    if not dreams_dir.exists():
        return None
    candidates = [p for p in dreams_dir.glob("processed-learnings-*.md") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


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
