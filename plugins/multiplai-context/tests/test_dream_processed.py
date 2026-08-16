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


def test_a_heading_without_a_summary_is_not_an_item():
    """Byte-identical to the hub's `_UPDATE_RE`, and this is what pins it there.

    The two copies had already drifted: the plugin's pattern stopped at the
    dot, so `### 5.` was an update here and not in the hub — the two tools
    disagreeing about what the same file says, which is exactly what the
    `## Processed` contract exists to prevent.
    """
    from lib.dream_processed import _ACTION_ITEM_RE, _UPDATE_RE

    assert _UPDATE_RE.match("### 5. Freeze the monotonic clock")
    assert not _UPDATE_RE.match("### 5.")
    # `### 5.   ` *does* match, with a single space as the summary — `.` matches
    # a space, so `\s*(.+?)\s*` can always find one. Asserted here because it is
    # surprising, and because the hub does the same thing: the point is that the
    # two agree, not that either is elegant.
    assert _UPDATE_RE.match("### 5.   ")

    assert _ACTION_ITEM_RE.match("### A2. Delete the stale worktree")
    assert not _ACTION_ITEM_RE.match("### A2.")


# ---------------------------------------------------------------------------
# Conflict resolutions (#201)
# ---------------------------------------------------------------------------
#
# Conflicts were the only item kind with no way to record a decision: `--kind`
# took update/action only, so a reviewer read one, decided, and the decision
# evaporated — the same conflict re-presented on every later proposal.

CONFLICT_PROPOSAL = """# Processed Learnings — 2026-08-14

## Conflict Resolutions

_Each of these learnings is about a line that already exists in memory._

### `dolcebot.md` line 453

- **Superseded** (was): the old CI-gap line.
- **Now**: the restated CI-gap line.
- **Basis**: EMPIRICAL/FACT; match confidence 0.61

### `dolcebot.md` line 455

- **Superseded** (was): the old beat-enabled warning.
- **Now**: the restated beat-enabled warning.
- **Basis**: EMPIRICAL/FACT; match confidence 0.47

### `prompt-eng-guide.md` line 11

- **Superseded** (was): the old caveat.
- **Now**: the restated caveat.

---

## Updates for `testing.md` (1 learning)

### 1. Clock mocking causes flaky retries
**Section:** Test Reliability
**Change:** add
> Freeze the monotonic clock, not the wall clock.

**Source:** 2026-07-20.md:3
"""


def test_conflict_moves_to_processed_keyed_by_file_and_line():
    out = move_to_processed(
        CONFLICT_PROPOSAL, ("conflict", "dolcebot.md", 453), "rejected",
        ts="2026-08-16T00:00:00Z",
    )
    assert PROCESSED_HEADING in out
    processed = out.split(PROCESSED_HEADING, 1)[1]
    assert "### `dolcebot.md` line 453" in processed
    assert "**Processed:** rejected · 2026-08-16T00:00:00Z" in processed
    # The siblings stay pending, in the Conflict Resolutions section.
    conflicts = out.split(PROCESSED_HEADING, 1)[0]
    assert "### `dolcebot.md` line 455" in conflicts
    assert "### `prompt-eng-guide.md` line 11" in conflicts


def test_conflict_line_number_alone_is_not_enough():
    """Two files can each have a conflict at the same line number."""
    out = move_to_processed(
        CONFLICT_PROPOSAL, ("conflict", "prompt-eng-guide.md", 453), "rejected",
        ts="2026-08-16T00:00:00Z",
    )
    assert out == CONFLICT_PROPOSAL


def test_conflict_marking_is_idempotent():
    once = move_to_processed(
        CONFLICT_PROPOSAL, ("conflict", "dolcebot.md", 453), "applied",
        ts="2026-08-16T00:00:00Z",
    )
    twice = move_to_processed(
        once, ("conflict", "dolcebot.md", 453), "applied",
        ts="2026-08-16T00:00:00Z",
    )
    assert twice == once


def test_conflicts_do_not_count_as_pending_items():
    """`--archive` must not block on conflicts — the one thing that already
    worked, and widening `has_pending_items` would break it."""
    only_conflicts = CONFLICT_PROPOSAL.split("## Updates for")[0]
    assert not has_pending_items(only_conflicts)
    assert has_pending_items(CONFLICT_PROPOSAL)


def test_conflict_decision_round_trips_through_a_file(tmp_path):
    path = tmp_path / "processed-learnings-2026-08-14.md"
    path.write_text(CONFLICT_PROPOSAL)
    assert mark_processed(path, ("conflict", "dolcebot.md", 455), "edited")
    assert "**Processed:** edited" in path.read_text()
    assert not mark_processed(path, ("conflict", "dolcebot.md", 455), "edited")


def test_conflict_decision_from_dict_requires_a_file():
    import pytest

    from lib.dream_processed import Decision

    with pytest.raises(ValueError, match="kind 'conflict' needs a 'file'"):
        Decision.from_dict({"kind": "conflict", "index": 453, "status": "rejected"})

    d = Decision.from_dict(
        {"kind": "conflict", "file": "dolcebot.md", "index": 453, "status": "rejected"}
    )
    assert d.ref == ("conflict", "dolcebot.md", 453)
    assert d.label == "conflict dolcebot.md:453"
