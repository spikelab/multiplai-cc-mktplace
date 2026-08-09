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


PROPOSAL = """# Processed Learnings — 2026-08-06

## Updates for `dolcebot.md` (2 learnings)

### 1. Logging is unimplemented
**Section:** DolceEngine
**Change:** add
**Provenance:** CORRECTION/FACT
> VM logs are specified and unimplemented.

**Source:** 2026-08-05.md:1

### 2. Always read the diary first
**Section:** DolceEngine
**Change:** add
**Provenance:** CORRECTION/RULE
> Before starting work, read today's diary entry.

**Source:** 2026-08-05.md:2

## Routing Warnings

(none)
"""

MEMORY = "# DolceBot\n\n## DolceEngine\n\n- a\n"


def _proposal_file(memory_dir: Path) -> Path:
    path = memory_dir.parent / "proposal.md"
    path.write_text(PROPOSAL)
    return path


class _Reply:
    def __init__(self, content):
        self.content = content


GOOD_VERDICTS = (
    "dolcebot.md#1: provenance=CORRECTION kind=FACT citation=supported "
    "redundant=no verdict=apply reason=plain fact from the user\n"
    "dolcebot.md#2: provenance=CORRECTION kind=RULE citation=supported "
    "redundant=no verdict=apply reason=the judge would allow it"
)


def _client_returning(*payloads):
    """A stub client whose `query` returns each payload in turn, then repeats
    the last. Counts calls so a cache hit is provable rather than asserted."""
    client = AsyncMock()
    seq = list(payloads)
    calls = []

    async def _query(**kwargs):
        calls.append(kwargs)
        return _Reply(seq[min(len(calls) - 1, len(seq) - 1)])

    client.query = _query
    client.calls = calls
    return client


class TestDegradation:
    """Contracts C3 and C4: failure always goes toward *more* human review."""

    def test_total_model_failure_applies_nothing(self, dream, capsys):
        """Criterion 10. Every batch erroring must give the `review`-mode
        partition — not the rubric's permissive cells."""
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)

        client = AsyncMock()
        client.query = AsyncMock(side_effect=RuntimeError("model exploded"))
        with patch.object(mod, "create_client", new=AsyncMock(return_value=client)):
            rc = asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        assert rc == 0
        assert (memory_dir / "dolcebot.md").read_text() == MEMORY
        out = capsys.readouterr().out
        assert "APPLIED (0)" in out

    def test_an_unparseable_reply_applies_nothing(self, dream, capsys):
        """Criterion 9. A reply that matches nothing contributes zero verdicts,
        and zero verdicts is the same partition."""
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)

        with patch.object(mod, "create_client",
                          new=AsyncMock(return_value=_client_returning(
                              "Sure! Here is my analysis of these items:"))):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        assert (memory_dir / "dolcebot.md").read_text() == MEMORY
        assert "APPLIED (0)" in capsys.readouterr().out

    def test_no_sdk_reviews_everything(self, dream, capsys):
        """Criterion 11. `create_client` raising is caught, and the fallback is
        `review` for every item — never the rubric's `auto` cells."""
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)

        with patch.object(mod, "create_client",
                          new=AsyncMock(side_effect=RuntimeError("no SDK"))):
            rc = asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        assert rc == 0
        assert (memory_dir / "dolcebot.md").read_text() == MEMORY
        out = capsys.readouterr().out
        assert "no model client" in out
        assert "APPLIED (0)" in out

    def test_review_mode_never_calls_the_model(self, dream, capsys, monkeypatch):
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_WRITE_MODE", "review")

        with patch.object(mod, "create_client", new=AsyncMock()) as client:
            asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        client.assert_not_called()
        assert "memory_write_mode=review" in capsys.readouterr().out
        assert (memory_dir / "dolcebot.md").read_text() == MEMORY


class TestOnlyLowerEndToEnd:
    def test_a_judge_apply_on_a_rule_writes_nothing(self, dream):
        """The judge above says `apply` for BOTH items, including the RULE.
        Only the FACT may land."""
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)

        client = _client_returning(GOOD_VERDICTS)
        applied_text = MEMORY.replace(
            "- a\n", "- a\n- VM logs are specified and unimplemented.\n")

        async def _apply(_client, _path, _slice):
            return applied_text

        with patch.object(mod, "create_client", new=AsyncMock(return_value=client)), \
                patch.object(mod, "_apply_proposal_to_file", new=_apply):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        written = (memory_dir / "dolcebot.md").read_text()
        assert "VM logs are specified" in written
        assert "read today's diary entry" not in written


class TestVerdictCache:
    def test_a_second_run_costs_no_model_calls(self, dream):
        """Criterion 12. The same item must not classify differently across
        runs, or a receipt is impossible to reason about.

        Both runs here are REAL runs. This test used to drive two ``dry_run=True``
        runs and assert one model call — which only passed because a dry run
        primed the cache, i.e. the assertion was load-bearing for a bug. A dry
        run must not change what the next real run does.
        """
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)
        client = _client_returning(GOOD_VERDICTS)

        with patch.object(mod, "create_client", new=AsyncMock(return_value=client)):
            first = asyncio.run(mod.dream_triage(str(proposal), dry_run=False))
            calls_after_first = len(client.calls)
            second = asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        assert first == second == 0
        assert calls_after_first >= 1
        assert len(client.calls) == calls_after_first, "the second run must not re-judge"

    def test_a_dry_run_leaves_no_cache_behind(self, dream):
        """Requirement: a preview does not change the next real run.

        Priming the cache makes a dry run something a later run applies from,
        which is the one thing a preview must not be — and the help text
        promised it wrote nothing. Asserted on the cache file rather than on a
        second run's call count, because two runs in one process share the dream
        run lock and the second would be skipped for an unrelated reason.
        """
        from multiplai_core.paths import get_paths

        from lib import memory_judge

        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)
        client = _client_returning(GOOD_VERDICTS)
        cache_path = memory_judge.default_cache_path(get_paths().data_dir())

        with patch.object(mod, "create_client", new=AsyncMock(return_value=client)):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=True))

        assert client.calls, "the dry run should still judge — that is the preview"
        assert not cache_path.exists(), (
            "a dry run wrote the verdict cache, so a later real run would apply "
            "from a preview"
        )

    def test_a_real_run_does_write_the_cache(self, dream):
        """The other half: caching is the point, on a run that is not a preview."""
        from multiplai_core.paths import get_paths

        from lib import memory_judge

        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)
        client = _client_returning(GOOD_VERDICTS)
        cache_path = memory_judge.default_cache_path(get_paths().data_dir())

        with patch.object(mod, "create_client", new=AsyncMock(return_value=client)):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        assert cache_path.exists()

    def test_a_dry_run_writes_nothing_at_all(self, dream):
        mod, memory_dir = dream
        before = (memory_dir / "dolcebot.md")
        before.write_text(MEMORY)
        proposal = _proposal_file(memory_dir)
        snapshot = before.read_text()
        client = _client_returning(GOOD_VERDICTS)

        with patch.object(mod, "create_client", new=AsyncMock(return_value=client)):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=True))

        assert before.read_text() == snapshot


class TestRejectionLog:
    def test_a_dropped_item_is_recorded(self, dream):
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)

        dropped = (
            "dolcebot.md#1: provenance=CORRECTION kind=FACT citation=supported "
            "redundant=yes verdict=apply reason=already in DolceEngine\n"
            "dolcebot.md#2: provenance=CORRECTION kind=RULE citation=none "
            "redundant=no verdict=review reason=a standing rule"
        )
        with patch.object(mod, "create_client",
                          new=AsyncMock(return_value=_client_returning(dropped))):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=False))

        from lib import rejections
        records = rejections.read(rejections.default_path(mod.get_paths().data_dir()))
        assert [r["number"] for r in records] == [1]
        assert records[0]["reason"] == "redundant"
        assert records[0]["judge_reason"] == "already in DolceEngine"
        assert records[0]["item_key"]
        # `drop` is not `review`: only the dropped item is logged.
        assert all(r["number"] != 2 for r in records)

    def test_a_dry_run_writes_no_rejection_log(self, dream):
        mod, memory_dir = dream
        (memory_dir / "dolcebot.md").write_text(MEMORY)
        proposal = _proposal_file(memory_dir)
        dropped = (
            "dolcebot.md#1: provenance=CORRECTION kind=FACT citation=supported "
            "redundant=yes verdict=drop reason=dupe"
        )
        with patch.object(mod, "create_client",
                          new=AsyncMock(return_value=_client_returning(dropped))):
            asyncio.run(mod.dream_triage(str(proposal), dry_run=True))

        from lib import rejections
        assert rejections.read(
            rejections.default_path(mod.get_paths().data_dir())) == []
