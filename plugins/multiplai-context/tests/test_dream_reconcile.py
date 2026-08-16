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
