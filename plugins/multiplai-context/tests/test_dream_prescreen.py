"""Tests for the review-time pre-screen and the corpus it screens against.

Every case here is a defect the first cut of `dream_prescreen.py` shipped with,
each of which reported a **false clean** — the one failure mode a screening tool
must not have, because the reviewer then applies a whole file believing it was
checked.
"""

import os
from pathlib import Path

import pytest

from lib import memory_corpus
from lib.conflict_edits import overlap
from lib.dream_processed import latest_pending_proposal
from lib.routing_validation import validate_proposal

from conftest import import_script

prescreen = import_script("dream_prescreen", "dream_prescreen.py")


class FakePaths:
    """The three accessors `memory_corpus` needs, and nothing else."""

    def __init__(self, memory_dir: Path, diary_dir: Path, banks=()):
        self.memory_dir = memory_dir
        self.diary_dir = diary_dir
        self._banks = banks

    def memory_banks(self):
        return self._banks


class FakeBank:
    def __init__(self, name, path):
        self.name = name
        self.path = path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A `.multiplai/` layout with memory, a bank, and three CLAUDE.md files."""
    root = tmp_path / "ws"
    memory_dir = root / ".multiplai" / "memory"
    diary_dir = root / ".multiplai" / "diary"
    bank_dir = root / ".multiplai" / "banks" / "teamname"
    config_dir = tmp_path / "claude-config"
    for d in (memory_dir, diary_dir, bank_dir, config_dir):
        d.mkdir(parents=True)

    # Lines must clear MIN_LINE_LEN — a heading or a one-word bullet is not
    # screenable corpus.
    (memory_dir / "dev.md").write_text(
        "## Dev\n\nUse uv for every Python project rather than pip or poetry.\n"
    )
    (memory_dir / "CLAUDE.md").write_text(
        "## Index\n\nThe index of every memory file and when each one is relevant.\n"
    )
    (root / "CLAUDE.md").write_text("## Workspace\n\nNew files land in INBOX and nowhere else.\n")
    (config_dir / "CLAUDE.md").write_text(
        "## Tool usage\n\nPrefer a bounded probe such as sed -n '40,90p' to reading "
        "an entire file.\n"
    )
    (bank_dir / "dev.md").write_text("## Team Dev\n\nThe team pins every container image by digest.\n")

    monkeypatch.setenv("WORKSPACE", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return FakePaths(
        memory_dir=memory_dir,
        diary_dir=diary_dir,
        banks=(FakeBank("personal", memory_dir), FakeBank("teamname", bank_dir)),
    )


# ---------------------------------------------------------------------------
# Corpus — what gets screened at all
# ---------------------------------------------------------------------------

def test_corpus_includes_the_global_and_workspace_claude_md(workspace):
    """The global file is the largest always-loaded document and was missing.

    `memory/CLAUDE.md` is excluded here on purpose — it is already a
    `memory_dir/*.md` file, and adding it again double-counts one file.
    """
    labels = {label for label, _ in memory_corpus.claude_md_paths(workspace)}
    assert labels == {"CLAUDE.md (global)", "CLAUDE.md (workspace)"}


def test_corpus_includes_shared_bank_files_but_not_the_personal_bank(workspace):
    labels = {label for label, _ in memory_corpus.bank_paths(workspace)}
    assert labels == {"teamname/dev.md"}


def test_prescreen_corpus_spans_memory_claude_md_and_banks(workspace):
    labels = {label for label, _, _, _ in prescreen.corpus_lines(workspace)}
    assert labels == {
        "dev.md",
        "CLAUDE.md",            # memory/CLAUDE.md, under its own name — once
        "CLAUDE.md (global)",
        "CLAUDE.md (workspace)",
        "teamname/dev.md",
    }


def test_workspace_root_refuses_to_guess_when_the_layout_is_not_multiplai(tmp_path, monkeypatch):
    """`memory_dir` is overridable; `parent.parent` would read a stranger's file.

    In the pure-standalone layout memory is `~/.multiplai/memory`, so deriving
    the workspace from it yields `$HOME` and pulls in `~/CLAUDE.md`.
    """
    monkeypatch.delenv("WORKSPACE", raising=False)
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    (tmp_path / "somewhere" / "CLAUDE.md").write_text("not the workspace\n")
    paths = FakePaths(memory_dir=elsewhere, diary_dir=elsewhere)

    assert memory_corpus.workspace_root(paths) is None
    labels = {label for label, _ in memory_corpus.claude_md_paths(paths)}
    assert "CLAUDE.md (workspace)" not in labels


def test_workspace_root_derives_from_the_multiplai_base_without_workspace_env(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSPACE", raising=False)
    root = tmp_path / "ws"
    diary = root / ".multiplai" / "diary"
    diary.mkdir(parents=True)
    paths = FakePaths(memory_dir=root / ".multiplai" / "memory", diary_dir=diary)
    assert memory_corpus.workspace_root(paths) == root


def test_unreadable_and_oversized_files_are_skipped_not_fatal(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * (memory_corpus.MAX_FILE_BYTES + 1))
    ok = tmp_path / "ok.md"
    ok.write_text("a normal memory line that is long enough to matter\n")
    out = memory_corpus.read_files([("big", big), ("ok", ok), ("gone", tmp_path / "nope.md")])
    assert set(out) == {"ok"}


# ---------------------------------------------------------------------------
# Proposal selection
# ---------------------------------------------------------------------------

def test_latest_pending_proposal_prefers_mtime_over_lexical_order(tmp_path):
    """`-` (0x2D) sorts before `.` (0x2E), so a same-day `-2` sorts FIRST.

    `sorted(...)[-1]` therefore returns the *oldest* same-day proposal.
    """
    base = tmp_path / "processed-learnings-2026-08-11.md"
    rerun = tmp_path / "processed-learnings-2026-08-11-2.md"
    base.write_text("first\n")
    rerun.write_text("re-run\n")
    os.utime(base, (1_700_000_000, 1_700_000_000))
    os.utime(rerun, (1_700_000_900, 1_700_000_900))

    assert sorted(tmp_path.glob("processed-learnings-*.md"))[-1] == base  # the bug
    assert latest_pending_proposal(tmp_path) == rerun


def test_latest_pending_proposal_ignores_decided_subdirectories(tmp_path):
    (tmp_path / "applied").mkdir()
    (tmp_path / "applied" / "processed-learnings-2026-08-01.md").write_text("done\n")
    assert latest_pending_proposal(tmp_path) is None


# ---------------------------------------------------------------------------
# Parsing — a heading the pipeline accepts must be one this accepts
# ---------------------------------------------------------------------------

SUFFIXED = """# Processed Learnings — 2026-08-11

## Updates for `python.md` (2 items)

### 1. Pin the interpreter
**Section:** Environments
**Change:** add
> Pin the interpreter version in pyproject so a fresh checkout resolves the same.

**Source:** 2026-08-11.md:4

## Processed

### 2. Already decided
**Section:** Environments
**Change:** add
> This one was decided in an earlier sitting and must never come back.
"""


def test_a_suffixed_updates_for_heading_still_yields_its_items():
    """`## Updates for \\`python.md\\` (2 items)` is the shape real proposals use.

    A regex anchored with `\\n` right after the closing backtick returns zero
    items and exits 0 — indistinguishable from a clean file.
    """
    items = prescreen.pending_items(SUFFIXED, "python.md")
    assert [i["number"] for i in items] == ["1"]


def test_items_under_processed_are_not_pending():
    assert all(i["title"] != "Already decided" for i in prescreen.pending_items(SUFFIXED, None))


def test_all_mode_returns_every_target():
    two_targets = SUFFIXED + "\n## Updates for `dev.md`\n\n### 3. Something\n> A line of insert text here.\n"
    assert {i["target"] for i in prescreen.pending_items(two_targets, None)} == {"python.md", "dev.md"}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_an_item_with_no_parsed_insert_text_is_unscreenable_not_clean():
    """A 0.00 score on an empty token set is not evidence of anything."""
    entry = {"target": "dev.md", "number": "1", "title": "No blockquote", "text": ""}
    result = prescreen.screen(entry, [("dev.md", 1, "a" * 60, {"alpha", "beta", "gamma", "delta"})], 0.35)
    assert result["unscreenable"] is True
    assert result["flagged"] is False


def test_a_verbose_restatement_of_a_short_rule_still_scores():
    """The containment measure `|a ∩ b| / |a|` only finds an item *inside* a line.

    Dream items are full sentences, so the shape that needs catching is the
    verbose restatement — which containment scores lowest of all.
    """
    existing = "New files land in INBOX only; plans belong in files, not console narration."
    verbose = (
        "Anything newly created should be placed into the INBOX directory only, and "
        "any plan we produce belongs in a file rather than narrated to the console."
    )
    containment = len(
        set(verbose.lower().split()) & set(existing.lower().split())
    ) / len(set(verbose.lower().split()))
    assert overlap(verbose, existing) > containment


def test_flagging_uses_the_shared_threshold_and_is_symmetric():
    from lib.conflict_edits import MIN_OVERLAP
    line = "Use uv for every Python project rather than pip or poetry."
    entry = {"target": "dev.md", "number": "1", "title": "uv", "text": line}
    result = prescreen.screen(entry, [("dev.md", 7, line, prescreen.content_words(line))], MIN_OVERLAP)
    assert result["flagged"] is True
    assert result["scored"][0][0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Output volume — the tool must not cost what it saves
# ---------------------------------------------------------------------------

def test_only_flagged_and_unscreenable_items_print(capsys):
    line = "Use uv for every Python project rather than pip or poetry."
    lines = [("dev.md", 7, line, prescreen.content_words(line))]
    entries = [
        {"target": "dev.md", "number": "1", "title": "dupe", "text": line},
        {"target": "dev.md", "number": "2", "title": "novel",
         "text": "Fireflies transcripts are pulled over GraphQL with a bearer token."},
        {"target": "dev.md", "number": "3", "title": "empty", "text": ""},
    ]
    flagged, unscreenable = prescreen.report(entries, lines, 0.35, verbose=False)
    out = capsys.readouterr().out
    assert (flagged, unscreenable) == (1, 1)
    assert "dupe" in out and "empty" in out
    assert "novel" not in out

    # One line per lead, naming both locations — not the item body. Printing
    # bodies and neighbours for the real 602-item backlog cost 411,614 bytes.
    assert len([line for line in out.splitlines() if line.strip()]) == 2
    assert "dev.md:7" in out
    assert line not in out


def test_verbose_prints_every_item_with_its_body_and_neighbours(capsys):
    line = "Use uv for every Python project rather than pip or poetry."
    lines = [("dev.md", 7, line, prescreen.content_words(line))]
    body = "Fireflies transcripts are pulled over GraphQL with a bearer token."
    entries = [{"target": "dev.md", "number": "2", "title": "novel", "text": body}]
    prescreen.report(entries, lines, 0.35, verbose=True)
    out = capsys.readouterr().out
    assert "novel" in out
    assert body in out
    assert line in out


# ---------------------------------------------------------------------------
# The draft-time gate — the wider corpus must reach it, and only as dedup
# ---------------------------------------------------------------------------

GATE_PROPOSAL = """## Updates for `dev.md`

### 1. Bounded probes
**Section:** Tool usage
**Change:** add
> Prefer a bounded probe such as sed -n 40 90p to reading an entire file when
> you already know which part of the file you need.
"""


def test_dedup_extra_flags_a_rule_that_already_lives_in_an_always_loaded_file():
    always_loaded = {
        "CLAUDE.md (global)": (
            "## Tool usage\n\nPrefer a bounded probe such as sed -n 40 90p to reading "
            "an entire file when you already know which part of the file you need.\n"
        )
    }
    assert validate_proposal(GATE_PROPOSAL, {"dev.md": "## Tool usage\n"}) == []
    warnings = validate_proposal(
        GATE_PROPOSAL, {"dev.md": "## Tool usage\n"}, dedup_extra=always_loaded
    )
    assert any("ALWAYS-LOADED" in w and "CLAUDE.md (global)" in w for w in warnings)


def test_dedup_extra_never_joins_the_section_registry():
    """An H2 in a CLAUDE.md does not own a memory section name.

    Registering it would make every ordinary heading a phantom misroute.
    """
    warnings = validate_proposal(
        GATE_PROPOSAL,
        {"dev.md": "## Tool usage\n"},
        dedup_extra={"CLAUDE.md (global)": "## Tool usage\n\nUnrelated prose entirely.\n"},
    )
    assert not any("does not exist in" in w for w in warnings)


# ---------------------------------------------------------------------------
# The tokenizer decision (#199), pinned to its backtest
# ---------------------------------------------------------------------------
#
# One tokenizer serves three consumers — conflict_edits (supersede edits),
# dream_prescreen (review-time lens) and routing_validation (draft-time gate).
# Changing it moves all three at once, and MIN_OVERLAP is calibrated against
# it, so these tests exist to make a casual revert visible.


class TestContentWordsKeepsCodeSpans:
    def test_backtick_content_survives_as_ordinary_words(self):
        """Issue #199: stripping code spans removed the signal with the noise.
        For a rule about tooling the distinctive tokens are usually inside the
        backticks."""
        words = prescreen.content_words("Run `uv run --no-project` before the gate")
        assert "run" in words
        assert "no-project" in words or "--no-project" in words.union(
            {w.lstrip("-") for w in words}
        )

    def test_a_rule_whose_signal_is_only_in_backticks_now_matches(self):
        """The measured case for the change. Both texts say the same thing and
        the only substantial shared tokens are inside the code span, so the old
        tokenizer scored them 0.00 — not a near miss, no overlap at all."""
        from lib.conflict_edits import MIN_OVERLAP

        item = "Use `gh pr merge --squash --delete-branch` to merge in this repo."
        line = "This repo squash-merges only: `gh pr merge --squash --delete-branch`."
        score = prescreen.overlap_sets(
            prescreen.content_words(item), prescreen.content_words(line)
        )
        assert score >= MIN_OVERLAP, f"scored {score:.2f}, below {MIN_OVERLAP}"
        # Guard the direction, not the exact number: stripping code spans is
        # what this test exists to stop coming back.
        assert "delete-branch" in prescreen.content_words(item)

    def test_no_stemming(self):
        """Backtested and rejected: singular collapse looks right on a
        hand-picked pair and made the ratio worse at every threshold over the
        602-item backlog (12.7:1 against 13.2 baseline at 0.35). Do not re-add
        it without a backtest that says otherwise."""
        words = prescreen.content_words("worktrees and directories")
        assert "worktrees" in words and "worktree" not in words
        assert "directories" in words and "directorie" not in words
