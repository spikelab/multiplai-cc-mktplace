"""Memory-vs-session conflict surfacing.

Injected memory is long-term state and can lag reality; a document fed
into the session (pasted, Read, or injected) is often newer. The hook
cannot detect a semantic contradiction itself — only the model sees the
full session context — so context_manager renders a conflict-surfacing
preamble above every injected MEMORY block, plus a last-updated stamp
per file, making it mandatory for the model to tell the user when
memory and session context disagree.

The stamp prefers the in-content ``**Last Updated:**`` header (the
dream tooling maintains it) over filesystem mtime, which lies whenever
files are re-materialized without a content change (git clone/checkout,
rsync). ``memory_conflict_preamble=false`` turns the feature off.

Unit tests cover the rendering contract on both injection paths (router
and recency fallback), the toggle, and the date-resolution rules.

The E2E test (opt-in: ``MULTIPLAI_E2E_LLM=1``, needs an authenticated
``claude`` CLI) demonstrates the goal end-to-end: a sandbox memory file
states a stale fact, the hook's real output is placed in a session next
to a document stating the opposite, and the model's reply must surface
the disagreement to the user.
"""

import os
import shutil
import subprocess
from datetime import datetime

import pytest

import context_manager
from conftest import run_context_hook as _run_hook, write_catalog as _write_catalog

# Stable fragments of the preamble asserted below — if the wording is
# rephrased, update these sentinels together with the constant.
PREAMBLE_SENTINEL = "MUST surface the disagreement"
STAMP_SENTINEL = "(last updated: "


# ---------------------------------------------------------------------------
# Rendering contract
# ---------------------------------------------------------------------------


class TestConflictPreambleRendering:
    def test_preamble_and_stamp_on_router_path(self, env_setup):
        """Router-picked memory renders the conflict preamble + date stamp."""
        (env_setup["memory_dir"] / "writing.md").write_text("# Writing guide")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "writing.md", "summary": "guide",
              "intent_domains": ["writing a blog post"]}],
        )

        out = _run_hook(env_setup, prompt="help me write a blog post")
        ctx = out["context"]
        assert "=== MEMORY ===" in ctx
        assert PREAMBLE_SENTINEL in ctx
        assert STAMP_SENTINEL in ctx
        # Preamble sits between the section header and the first file.
        assert ctx.index("=== MEMORY ===") < ctx.index(PREAMBLE_SENTINEL) < ctx.index("## writing.md")
        # Stamp precedes the file body it annotates.
        assert ctx.index("## writing.md") < ctx.index(STAMP_SENTINEL) < ctx.index("# Writing guide")

    def test_preamble_on_recency_fallback_path(self, env_setup):
        """Memory injected by the no-catalog recency fallback gets the preamble too."""
        (env_setup["memory_dir"] / "me.md").write_text("# About me\nfacts")
        # No memory.json catalog → router never runs → recency fallback fires.

        out = _run_hook(env_setup, prompt="tell me something")
        ctx = out["context"]
        assert "## me.md" in ctx
        assert PREAMBLE_SENTINEL in ctx
        assert STAMP_SENTINEL in ctx

    def test_no_preamble_without_memory(self, env_setup):
        """Skills-only injection must not carry the memory conflict preamble."""
        (env_setup["memory_dir"] / "chem.md").write_text("# Chemistry notes")
        skill_dir = env_setup["skills_dir"] / "writing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Writing skill body")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "chem.md", "intent_domains": ["quantum chemistry lab notes"]}],
        )
        _write_catalog(
            env_setup["catalogs_dir"],
            "skills.json",
            [{"source": "writing", "name": "writing",
              "summary": "Drafts and edits blog posts",
              "intent_domains": ["writing a blog post"]}],
        )

        out = _run_hook(
            env_setup,
            prompt="writing a blog post",
            extra_env={"CLAUDE_PLUGIN_OPTION_enable_skills": "true"},
        )
        ctx = out["context"]
        assert "=== SKILLS ===" in ctx
        assert "=== MEMORY ===" not in ctx
        assert PREAMBLE_SENTINEL not in ctx

    def test_toggle_off_renders_plain_memory_block(self, env_setup):
        """memory_conflict_preamble=false drops both preamble and stamps."""
        (env_setup["memory_dir"] / "me.md").write_text("# About me\nfacts")

        out = _run_hook(
            env_setup,
            prompt="tell me something",
            extra_env={"CLAUDE_PLUGIN_OPTION_memory_conflict_preamble": "false"},
        )
        ctx = out["context"]
        assert "## me.md" in ctx
        assert PREAMBLE_SENTINEL not in ctx
        assert STAMP_SENTINEL not in ctx


# ---------------------------------------------------------------------------
# Date resolution (unit level)
# ---------------------------------------------------------------------------


class TestStampMemoryDates:
    def test_last_updated_header_preferred_over_mtime(self, tmp_path):
        """The in-content header outranks a fresh mtime — a re-clone or an
        unrelated rewrite must not stamp stale facts as current."""
        (tmp_path / "infra.md").write_text(
            "# Infrastructure\n\n**Last Updated:** 2025-03-01\n\nfacts"
        )
        stamped = context_manager._stamp_memory_dates(
            tmp_path, {"infra.md": "facts"}
        )
        assert stamped["infra.md"].startswith("(last updated: 2025-03-01)\n")

    def test_mtime_fallback_without_header(self, tmp_path):
        """No header → filesystem mtime is the best remaining signal."""
        (tmp_path / "plain.md").write_text("# Plain\nno header here")
        today = datetime.now().strftime("%Y-%m-%d")
        stamped = context_manager._stamp_memory_dates(
            tmp_path, {"plain.md": "no header here"}
        )
        assert stamped["plain.md"].startswith(f"(last updated: {today})\n")

    def test_section_ref_uses_base_file_date(self, tmp_path):
        """A file.md#Section slice carries the base file's date, even though
        the slice content itself doesn't include the header."""
        (tmp_path / "big.md").write_text(
            "# Big\n\n**Last Updated:** 2026-01-15\n\n## Section\nslice"
        )
        stamped = context_manager._stamp_memory_dates(
            tmp_path, {"big.md#Section": "section slice"}
        )
        assert stamped["big.md#Section"] == "(last updated: 2026-01-15)\nsection slice"

    def test_missing_file_skips_stamp(self, tmp_path):
        stamped = context_manager._stamp_memory_dates(
            tmp_path, {"gone.md": "content"}
        )
        assert stamped["gone.md"] == "content"

    def test_out_of_range_mtime_fails_open(self, tmp_path):
        """An mtime past datetime's year-9999 ceiling raises ValueError /
        OverflowError (NOT OSError) from fromtimestamp; the stamp must be
        skipped, not escalate to the hook's emit-empty-context catch-all."""
        p = tmp_path / "corrupt.md"
        p.write_text("# Corrupt mtime, no header")
        far_future = 400_000_000_000  # year ~14000
        try:
            os.utime(p, (far_future, far_future))
        except OverflowError:
            pytest.skip("filesystem rejects out-of-range mtimes")
        stamped = context_manager._stamp_memory_dates(
            tmp_path, {"corrupt.md": "content"}
        )
        assert stamped["corrupt.md"] == "content"


# ---------------------------------------------------------------------------
# E2E: live-model demonstration of the goal
# ---------------------------------------------------------------------------

E2E_ENABLED = bool(os.environ.get("MULTIPLAI_E2E_LLM"))
CLAUDE_CLI = shutil.which("claude")

STALE_MEMORY = (
    "# Infrastructure\n\n"
    "**Last Updated:** 2025-03-01\n\n"
    "Production database: PostgreSQL 12 on host db-old.internal.\n"
)

SESSION_DOCUMENT = (
    "## Infrastructure changelog — 2026-07-01\n\n"
    "Production database migrated off PostgreSQL: production now runs "
    "MySQL 8.0 on host db-new.internal. db-old.internal was "
    "decommissioned.\n"
)


@pytest.mark.skipif(
    not (E2E_ENABLED and CLAUDE_CLI),
    reason="live-LLM e2e: set MULTIPLAI_E2E_LLM=1 and have an authenticated `claude` CLI",
)
def test_e2e_memory_document_disagreement_is_surfaced(env_setup):
    """Stale memory + contradicting session document → reply flags the conflict.

    Reproduces exactly what Claude Code does with the hook output: the
    additionalContext lands in the conversation ahead of the user turn,
    which here also carries a newer document contradicting the memory.
    """
    assert CLAUDE_CLI  # skipif-guarded; narrows Optional for type checkers
    (env_setup["memory_dir"] / "infrastructure.md").write_text(STALE_MEMORY)
    _write_catalog(
        env_setup["catalogs_dir"],
        "memory.json",
        [{"source": "infrastructure.md", "summary": "infra facts",
          "intent_domains": ["production database infrastructure"]}],
    )

    out = _run_hook(env_setup, prompt="which database engine does production run on?")
    injected = out["hookSpecificOutput"]["additionalContext"]
    assert "PostgreSQL 12" in injected
    assert PREAMBLE_SENTINEL in injected
    # The header date, not the sandbox file's fresh mtime, is the stamp.
    assert "(last updated: 2025-03-01)" in injected

    user_message = (
        f"<system-reminder>\n{injected}\n</system-reminder>\n\n"
        "Here is the current infrastructure changelog:\n\n"
        f"{SESSION_DOCUMENT}\n\n"
        "Which database engine does production run on?"
    )
    result = subprocess.run(
        [CLAUDE_CLI, "-p", "--model", "claude-haiku-4-5", user_message],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "_HOOK_CHILD_SESSION": "1"},
    )
    assert result.returncode == 0, result.stderr[:800]
    reply = result.stdout.lower()

    # The answer must follow the newer source...
    assert "mysql" in reply, result.stdout
    # ...and explicitly surface that memory and the document disagree.
    assert any(
        marker in reply
        for marker in ("disagree", "conflict", "contradict", "differs",
                       "out of date", "outdated", "out-of-date", "stale")
    ), f"reply did not surface the disagreement:\n{result.stdout}"
