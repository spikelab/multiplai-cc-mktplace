"""Tests for dream_processed.py — the in-file decision record shared with the
multiplai-gui hub. A decided item's block moves under ``## Processed`` (the one
cross-tool contract); items there are no longer pending, so the file itself
tracks what has been reviewed.
"""

from lib.dream_processed import (
    PROCESSED_HEADING,
    has_pending_items,
    mark_processed,
    move_to_processed,
)

PROPOSAL = """# Processed Learnings — 2026-07-20

**Sources:** 2 files, ~5 entries

---

## Updates for `testing.md` (2 learnings)

### 1. Clock mocking causes flaky retries
**Section:** Test Reliability
**Change:** add
> Freeze the monotonic clock, not the wall clock.

**Source:** 2026-07-20.md:3

### 2. Prefer fixture factories
**Section:** Test Reliability
**Change:** update
> Default to factory functions.

**Source:** 2026-07-20.md:9

---

## Updates for `build-tools.md` (1 learning)

### 2. bun install needs a lockfile refresh
**Section:** Bundlers
**Change:** add
> Delete bun.lockb and reinstall after a registry move.

**Source:** 2026-07-19.md:5

---

## Action Items (1 item)

### A1. Fix flaky-retry harness
**What:** Add a monotonic-clock freeze fixture.
**Why:** Wall-clock mocking reproduces the flake.
**Source:** 2026-07-20.md:14

---

## Filtered Out (1 item)

- "one-off git stash confusion" — diary material, not reusable
"""


def _pending(text):
    """Very small pending-key extractor for assertions (parallels the hub)."""
    import re

    group_re = re.compile(r"^## Updates for `([^`]+)`")
    keys, target, in_actions = set(), None, False
    for line in text.splitlines():
        g = group_re.match(line)
        if g:
            target, in_actions = g.group(1), False
        elif line.startswith("## Action Items"):
            target, in_actions = None, True
        elif line.startswith("## "):
            target, in_actions = None, False
        elif target and (m := re.match(r"^### (\d+)\.", line)):
            keys.add(("update", target, int(m.group(1))))
        elif in_actions and (m := re.match(r"^### A(\d+)\.", line)):
            keys.add(("action", int(m.group(1))))
    return keys


def test_applied_update_moves_to_processed():
    out = move_to_processed(
        PROPOSAL, ("update", "testing.md", 1), "applied", target="testing.md", ts="2026-07-20T10:00:00Z"
    )
    assert ("update", "testing.md", 1) not in _pending(out)
    # everything else stays pending
    assert _pending(out) == _pending(PROPOSAL) - {("update", "testing.md", 1)}
    processed = out.split(PROCESSED_HEADING)[1]
    assert "### 1. Clock mocking causes flaky retries" in processed
    assert "**Processed:** applied → testing.md · 2026-07-20T10:00:00Z" in processed
    assert "Freeze the monotonic clock" in processed  # block moved verbatim


def test_rejected_item_annotation():
    out = move_to_processed(PROPOSAL, ("update", "testing.md", 2), "rejected", ts="2026-07-20T10:00:00Z")
    processed = out.split(PROCESSED_HEADING)[1]
    assert "**Processed:** rejected · 2026-07-20T10:00:00Z" in processed
    assert "→" not in processed.split("\n\n")[1]  # no target arrow on a reject line


def test_duplicate_index_targets_the_right_group():
    # index 2 exists in both testing.md and build-tools.md
    out = move_to_processed(PROPOSAL, ("update", "build-tools.md", 2), "applied", target="build-tools.md")
    assert ("update", "build-tools.md", 2) not in _pending(out)
    assert ("update", "testing.md", 2) in _pending(out)  # the other #2 untouched
    assert "bun install" in out.split(PROCESSED_HEADING)[1]


def test_action_item_moves_to_processed():
    out = move_to_processed(PROPOSAL, ("action", 1), "applied")
    assert ("action", 1) not in _pending(out)
    assert "### A1. Fix flaky-retry harness" in out.split(PROCESSED_HEADING)[1]


def test_move_is_idempotent():
    out = move_to_processed(PROPOSAL, ("update", "testing.md", 1), "applied", target="testing.md")
    # a second move of the same item finds nothing pending → unchanged
    assert move_to_processed(out, ("update", "testing.md", 1), "applied") == out


def test_missing_item_is_noop():
    assert move_to_processed(PROPOSAL, ("update", "nope.md", 9), "applied") == PROPOSAL
    assert move_to_processed(PROPOSAL, ("action", 99), "applied") == PROPOSAL


def test_multiple_moves_accumulate_under_one_heading():
    out = move_to_processed(PROPOSAL, ("update", "testing.md", 1), "applied", target="testing.md")
    out = move_to_processed(out, ("action", 1), "rejected")
    assert out.count(PROCESSED_HEADING) == 1  # single Processed section
    processed = out.split(PROCESSED_HEADING)[1]
    assert "### 1. Clock mocking" in processed
    assert "### A1. Fix flaky-retry harness" in processed


def test_has_pending_items():
    assert has_pending_items(PROPOSAL) is True
    # move every item, then nothing is pending
    text = PROPOSAL
    for ref, target in [
        (("update", "testing.md", 1), "testing.md"),
        (("update", "testing.md", 2), "testing.md"),
        (("update", "build-tools.md", 2), "build-tools.md"),
        (("action", 1), None),
    ]:
        text = move_to_processed(text, ref, "applied", target=target)
    assert has_pending_items(text) is False


def test_mark_processed_writes_atomically(tmp_path):
    path = tmp_path / "processed-learnings-2026-07-20.md"
    path.write_text(PROPOSAL)
    changed = mark_processed(path, ("update", "testing.md", 1), "applied", target="testing.md")
    assert changed is True
    assert "**Processed:** applied → testing.md" in path.read_text()
    assert not (tmp_path / (path.name + ".tmp")).exists()  # temp cleaned up by rename
    # re-marking the same item does not rewrite
    assert mark_processed(path, ("update", "testing.md", 1), "applied") is False
