"""Tests for the *applying* half of `dream.py --triage`.

`test_dream_triage.py` covers the classifier — which items a human must read.
This file covers what happens to the ones the human never reads: nothing
between a hallucinating applier and a rewritten memory file except
`_is_additive_result`, and nothing between a proposal whose routing gate never
ran and a full auto-apply except the `has_routing_section` refusal.
"""

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR


def _load_dream_module(alias: str):
    from multiplai_core.paths import _reset_cache
    _reset_cache()
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS_DIR / "dream.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dream(tmp_path, monkeypatch):
    """dream.py loaded against a throwaway workspace."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    (data_dir / "catalogs").mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_DIR", str(memory_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DIARY_DIR", str(diary_dir))
    mod = _load_dream_module(f"dream_triage_apply_{tmp_path.name}")
    return mod, memory_dir


ORIGINAL = """# Python

## Python Tooling

- uv resolves the workspace from the root lock.
- Ruff runs in pre-commit.

## Packaging

- Wheels are built by hatchling.
"""


class TestAdditiveVerification:
    """`_is_safe_memory_update` accepts a 40% rewrite. For triage — where every
    item is an `add` — that is not a near-miss, it is the exact outcome the
    feature promises cannot happen."""

    def test_a_real_append_passes(self, dream):
        mod, _ = dream
        new = ORIGINAL.replace(
            "- Ruff runs in pre-commit.",
            "- Ruff runs in pre-commit.\n- Pytest runs from the member directory.",
        )
        assert mod._is_additive_result(ORIGINAL, new, ["Pytest runs from the member directory."]) is None

    def test_a_dropped_line_is_refused(self, dream):
        mod, _ = dream
        new = ORIGINAL.replace("- Ruff runs in pre-commit.\n", "")
        reason = mod._is_additive_result(ORIGINAL, new, ["something"])
        assert reason and "lost or altered" in reason

    def test_a_reworded_line_is_refused(self, dream):
        """Rewording is indistinguishable from destroying, from here — the
        original line is gone and nobody reviewed the replacement."""
        mod, _ = dream
        new = ORIGINAL.replace(
            "- Ruff runs in pre-commit.", "- Ruff is run by the pre-commit hook."
        )
        reason = mod._is_additive_result(ORIGINAL, new, ["something"])
        assert reason and "lost or altered" in reason

    def test_a_wholesale_rewrite_is_refused(self, dream):
        """The case `_is_safe_memory_update` waves through: plausible prose,
        similar length, nothing of the original left."""
        mod, _ = dream
        new = "# Python\n\n## Python Tooling\n\n- Some entirely different content here.\n" * 2
        assert mod._is_additive_result(ORIGINAL, new, ["something"]) is not None

    def test_invented_bulk_is_refused_even_with_every_line_intact(self, dream):
        mod, _ = dream
        new = ORIGINAL + "\n" + "\n".join(f"- Invented fact {i}." for i in range(200))
        reason = mod._is_additive_result(ORIGINAL, new, ["one short item"])
        assert reason and "3x" in reason

    def test_blank_line_churn_is_tolerated(self, dream):
        """Only non-blank lines are compared: an applier that re-flows spacing
        around an insert has not lost anything."""
        mod, _ = dream
        new = ORIGINAL.replace("\n\n", "\n\n\n") + "\n- One new fact.\n"
        assert mod._is_additive_result(ORIGINAL, new, ["One new fact."]) is None


class TestScopedCommit:
    def test_only_the_named_files_are_committed(self, dream):
        """An unrelated hand-edit sitting in memory must not be swept into a
        commit captioned as an automatic triage apply."""
        mod, memory_dir = dream
        for cmd in (
            ["init", "-q"],
            ["config", "user.email", "t@multiplai.local"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", "-C", str(memory_dir), *cmd], check=True)
        (memory_dir / "python.md").write_text("# Python\n")
        (memory_dir / "life.md").write_text("# Life\n")
        subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit", "-qm", "base"], check=True
        )

        (memory_dir / "python.md").write_text("# Python\n\n- applied\n")
        (memory_dir / "life.md").write_text("# Life\n\n- a hand edit\n")

        assert mod._commit_memory_changes(
            memory_dir, pathspec=["python.md"], message="dream: triage auto-apply"
        )

        changed = subprocess.run(
            ["git", "-C", str(memory_dir), "show", "--name-only", "--format=%s", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "dream: triage auto-apply" in changed
        assert "python.md" in changed
        assert "life.md" not in changed


class TestRoutingSectionRefusal:
    def test_a_proposal_with_no_routing_section_applies_nothing(self, dream, capsys):
        """`flagged_by_routing` returns an empty set both when nothing was
        flagged and when the gate never ran, so without this check every item
        the gate would have caught applies unattended."""
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text("# DolceBot\n\n## DolceEngine\n\n- a\n")
        proposal = Path(memory_dir.parent / "proposal.md")
        proposal.write_text(
            "# Processed Learnings — 2026-08-06\n\n"
            "## Updates for `dolcebot.md` (1 learnings)\n\n"
            "### 1. Logging is unimplemented\n"
            "**Section:** DolceEngine\n**Change:** add\n"
            "> VM logs are specified and unimplemented.\n\n"
            "**Source:** 2026-08-05.md:1\n"
        )
        before = (memory_dir / "dolcebot.md").read_text()

        with patch.object(mod, "create_client", new=AsyncMock()) as client:
            rc = asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        assert rc == 1
        assert "Routing Warnings" in capsys.readouterr().out
        client.assert_not_called()
        assert (memory_dir / "dolcebot.md").read_text() == before


class TestDryRunGuard:
    def test_dry_run_without_triage_is_an_error(self, dream, monkeypatch):
        """`--dry-run` is read only by the triage path; without the guard,
        `dream.py --dry-run` runs a full consolidation and writes a proposal."""
        mod, _ = dream
        monkeypatch.setattr("sys.argv", ["dream.py", "--dry-run"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
