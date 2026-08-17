"""Tests for `dream.py --reconcile` — finishing proposals that were fully
decided but never filed (issue #202).

The failure this closes is not hypothetical. `processed-learnings-2026-08-10.md`
was fully applied on 2026-08-11 — 661 items, memory commit `9e8475d` — and then
none of the three finalization steps ran: it was never moved to `dreams/applied/`,
`--gc-learnings` never collected its five spent learnings files, and
`dream_state.yaml` still read `last_run: 2026-08-05`. Two days later the file sat
in the dreams root looking pending and the gate nudged on every session start.

Nothing caught it because nothing was looking: `/dream-remember` Step 1 takes the
newest proposal by mtime and never inspects older ones, so re-running the skill
reviews the *newer* file and cannot fix the stale one. The compounding part is
`_gc_learnings`, which requires a proposal to be archived before collecting its
sources — so the missed archive pinned the learnings too, and the next `/dream`
drafted 54 items from a file that should already have been gone.
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest  # noqa: E402

import dream  # noqa: E402

DECIDED = """# Processed Learnings — 2026-08-10

## Updates for `testing.md` (1 learning)

---

## Processed

_Items decided via `/dream-remember` or the GUI._

### 1. Clock mocking causes flaky retries
**Processed:** applied → testing.md · 2026-08-11T10:00:00Z
**Section:** Test Reliability
**Change:** add
> Freeze the monotonic clock, not the wall clock.
"""

PENDING = """# Processed Learnings — 2026-08-14

## Updates for `testing.md` (1 learning)

### 1. Clock mocking causes flaky retries
**Section:** Test Reliability
**Change:** add
> Freeze the monotonic clock, not the wall clock.

**Source:** 2026-08-14.md:3
"""

# Every *update* decided, one conflict resolution still undecided. By
# `has_pending_items` alone this reads as finished — and filing it would archive
# the proposal, stamp, and let `_gc_learnings` delete the learnings the conflict
# was derived from, after which nothing can re-derive it.
UNDECIDED_CONFLICT = """# Processed Learnings — 2026-08-12

## Conflict Resolutions

### `dolcebot.md` line 453

- **Superseded** (was): the old CI-gap line.
- **Now**: the restated CI-gap line.

---

## Updates for `testing.md` (1 learning)

---

## Processed

### 1. Clock mocking causes flaky retries
**Processed:** applied → testing.md · 2026-08-13T10:00:00Z
**Section:** Test Reliability
"""

# Reviewed in full and turned down in full: `/dream-remember` Step 6 requires
# `--archive-as rejected` for this, and the statuses to prove it are on the
# `**Processed:**` lines.
ALL_REJECTED = """# Processed Learnings — 2026-08-09

## Updates for `testing.md` (2 learnings)

---

## Processed

### 1. Clock mocking causes flaky retries
**Processed:** rejected · 2026-08-11T10:00:00Z
**Section:** Test Reliability

### 2. Prefer fixture factories
**Processed:** rejected · 2026-08-11T10:00:00Z
**Section:** Test Reliability
"""


@pytest.fixture
def dreams(tmp_path, monkeypatch):
    """A paths object whose dreams/state live under tmp_path, and a stubbed
    `_gc_learnings` so these tests assert on reconcile, not on collection."""
    dreams_dir = tmp_path / "dreams"
    dreams_dir.mkdir()
    state_file = tmp_path / "dream_state.yaml"

    class _Paths:
        def dreams_dir(self):
            return dreams_dir

        def dream_state_file(self):
            return state_file

    calls = {"gc": 0}
    monkeypatch.setattr(dream, "get_paths", lambda: _Paths())
    monkeypatch.setattr(dream, "_gc_learnings", lambda: calls.__setitem__("gc", calls["gc"] + 1))
    return dreams_dir, state_file, calls


def test_empty_root_is_clean(dreams, capsys):
    _, _, calls = dreams
    assert dream._reconcile() == 0
    assert "nothing to reconcile" in capsys.readouterr().out.lower()
    assert calls["gc"] == 0


def test_pending_proposal_is_left_alone(dreams, capsys):
    dreams_dir, state_file, calls = dreams
    (dreams_dir / "processed-learnings-2026-08-14.md").write_text(PENDING)

    assert dream._reconcile() == 0
    out = capsys.readouterr().out
    assert "pending:" in out
    # Untouched: still in the root, no stamp, no collection.
    assert (dreams_dir / "processed-learnings-2026-08-14.md").is_file()
    assert not state_file.exists()
    assert calls["gc"] == 0


def test_finished_proposal_is_archived_stamped_and_collected(dreams, capsys):
    dreams_dir, state_file, calls = dreams
    stale = dreams_dir / "processed-learnings-2026-08-10.md"
    stale.write_text(DECIDED)

    assert dream._reconcile() == 0

    assert not stale.exists(), "the finished proposal should leave the dreams root"
    assert (dreams_dir / "applied" / "processed-learnings-2026-08-10.md").is_file()
    assert state_file.exists(), "dream_state must be stamped or the gate nudges forever"
    assert "last_run" in state_file.read_text()
    assert calls["gc"] == 1, "collection runs only after the archive makes sources spent"
    assert "archived:" in capsys.readouterr().out


def test_dry_run_reports_without_touching_anything(dreams, capsys):
    dreams_dir, state_file, calls = dreams
    stale = dreams_dir / "processed-learnings-2026-08-10.md"
    stale.write_text(DECIDED)

    assert dream._reconcile(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "WOULD FINISH" in out
    assert stale.is_file()
    assert not (dreams_dir / "applied").exists()
    assert not state_file.exists()
    assert calls["gc"] == 0


def test_mixed_root_finishes_only_the_decided_one(dreams):
    dreams_dir, state_file, calls = dreams
    (dreams_dir / "processed-learnings-2026-08-10.md").write_text(DECIDED)
    (dreams_dir / "processed-learnings-2026-08-14.md").write_text(PENDING)

    assert dream._reconcile() == 0

    assert not (dreams_dir / "processed-learnings-2026-08-10.md").exists()
    assert (dreams_dir / "processed-learnings-2026-08-14.md").is_file()
    assert calls["gc"] == 1


def test_archive_failure_does_not_stamp(dreams, monkeypatch, capsys):
    """Stamping a run whose archive failed would silence the gate that is
    correctly tripping — the proposal is still unfiled."""
    dreams_dir, state_file, calls = dreams
    (dreams_dir / "processed-learnings-2026-08-10.md").write_text(DECIDED)

    def _boom(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(dream, "_archive_proposal", _boom)

    assert dream._reconcile() == 1
    assert (dreams_dir / "processed-learnings-2026-08-10.md").is_file()
    assert not state_file.exists()
    assert calls["gc"] == 0
    assert "ERROR" in capsys.readouterr().out


def test_undecided_conflict_blocks_the_archive(dreams, capsys):
    """The loss this guards against is total: archiving strips the proposal's
    only copy of the conflict, `_gc_learnings` then deletes the learnings block
    it was derived from, and `_with_conflict_resolutions` only ever re-derives
    from blocks still missing from the ledger. Nothing brings it back."""
    dreams_dir, state_file, calls = dreams
    proposal = dreams_dir / "processed-learnings-2026-08-12.md"
    proposal.write_text(UNDECIDED_CONFLICT)

    assert dream._reconcile() == 0
    out = capsys.readouterr().out
    assert "pending:" in out
    assert proposal.is_file(), "an undecided conflict must keep the proposal in the root"
    assert not (dreams_dir / "applied").exists()
    assert not state_file.exists()
    assert calls["gc"] == 0, "collection would delete the conflict's source learnings"


def test_deciding_the_conflict_unblocks_it(dreams):
    """Same file, conflict marked: it is genuinely finished and must file."""
    from lib.dream_processed import move_to_processed

    dreams_dir, state_file, calls = dreams
    proposal = dreams_dir / "processed-learnings-2026-08-12.md"
    proposal.write_text(
        move_to_processed(
            UNDECIDED_CONFLICT, ("conflict", "dolcebot.md", 453), "rejected",
            ts="2026-08-16T00:00:00Z",
        )
    )

    assert dream._reconcile() == 0
    assert not proposal.exists()
    assert (dreams_dir / "applied" / "processed-learnings-2026-08-12.md").is_file()
    assert calls["gc"] == 1


def test_fully_rejected_proposal_files_as_rejected(dreams):
    """`applied/` is positive evidence that memory was written — a proposal
    every item of which was turned down must not claim it."""
    dreams_dir, _, _ = dreams
    (dreams_dir / "processed-learnings-2026-08-09.md").write_text(ALL_REJECTED)

    assert dream._reconcile() == 0
    assert (dreams_dir / "rejected" / "processed-learnings-2026-08-09.md").is_file()
    assert not (dreams_dir / "applied").exists()


def test_a_single_applied_item_keeps_it_applied(dreams):
    dreams_dir, _, _ = dreams
    mixed = ALL_REJECTED.replace(
        "### 2. Prefer fixture factories\n**Processed:** rejected",
        "### 2. Prefer fixture factories\n**Processed:** applied → testing.md",
    )
    (dreams_dir / "processed-learnings-2026-08-09.md").write_text(mixed)

    assert dream._reconcile() == 0
    assert (dreams_dir / "applied" / "processed-learnings-2026-08-09.md").is_file()
    assert not (dreams_dir / "rejected").exists()


def test_unreadable_proposal_is_named_and_counted(dreams, monkeypatch, capsys):
    """A file that cannot be read is in neither bucket by construction, so the
    closing count used to under-report the root and never named it. Fail closed
    and visibly, the way `_gc_learnings` next door already does."""
    dreams_dir, state_file, calls = dreams
    bad = dreams_dir / "processed-learnings-2026-08-13.md"
    bad.write_text(DECIDED)

    real_read = Path.read_text

    def _read(self, *a, **kw):
        if self.name == bad.name:
            raise OSError("permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _read)

    assert dream._reconcile() == 1
    out = capsys.readouterr().out
    assert bad.name in out
    assert "1 proposal(s) still pending" in out
    assert bad.is_file()
    assert not state_file.exists()
    assert calls["gc"] == 0


def test_unreadable_proposal_does_not_stop_a_readable_one(dreams, monkeypatch, capsys):
    dreams_dir, state_file, calls = dreams
    bad = dreams_dir / "processed-learnings-2026-08-13.md"
    bad.write_text(DECIDED)
    good = dreams_dir / "processed-learnings-2026-08-10.md"
    good.write_text(DECIDED)

    real_read = Path.read_text

    def _read(self, *a, **kw):
        if self.name == bad.name:
            raise OSError("permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _read)

    assert dream._reconcile() == 1, "the unreadable file is still an unclean pass"
    assert (dreams_dir / "applied" / good.name).is_file()
    assert bad.is_file()
    out = capsys.readouterr().out
    assert f"ERROR: could not read {bad.name}" in out


def test_bare_dry_run_is_still_rejected():
    """The guard exists because a bare `dream.py --dry-run` would otherwise run
    a full consolidation and write a proposal — the opposite of the flag's
    promise. Widening it for --reconcile must not have removed it."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "dream.py"), "--dry-run"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "--dry-run requires" in result.stderr


def test_reconcile_accepts_dry_run():
    import subprocess

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "dream.py"), "--reconcile", "--dry-run"],
        capture_output=True, text=True,
    )
    assert "--dry-run requires" not in result.stderr
