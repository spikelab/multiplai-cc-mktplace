"""Memory-vs-session conflict surfacing.

Injected memory is long-term state and can lag reality; a document fed
into the session (pasted, Read, or injected) is often newer. The hook
cannot detect a semantic contradiction itself — only the model sees the
full session context — so context_manager renders a conflict-surfacing
preamble above every injected MEMORY block, plus a last-modified stamp
per file, making it mandatory for the model to tell the user when
memory and session context disagree.

Unit tests cover the rendering contract on both injection paths (router
and recency fallback) and its absence when no memory is injected.

The E2E test (opt-in: ``MULTIPLAI_E2E_LLM=1``, needs an authenticated
``claude`` CLI) demonstrates the goal end-to-end: a sandbox memory file
states a stale fact, the hook's real output is placed in a session next
to a document stating the opposite, and the model's reply must surface
the disagreement to the user.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import import_script

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
CONTEXT_MANAGER = SCRIPTS_DIR / "context_manager.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generators.base import CATALOG_SCHEMA_VERSION

# Stable fragments of the preamble asserted below — if the wording is
# rephrased, update these sentinels together with the constant.
PREAMBLE_SENTINEL = "MUST surface the disagreement"
STAMP_SENTINEL = "(file last modified: "


# ---------------------------------------------------------------------------
# Harness (same shape as test_context_manager_multi_corpus)
# ---------------------------------------------------------------------------


@pytest.fixture
def env_setup(tmp_path):
    """Build a sandboxed plugin layout: data, memory, skills dirs."""
    data_dir = tmp_path / "plugin_data"
    catalogs_dir = data_dir / "catalogs"
    memory_dir = tmp_path / "memory"
    skills_dir = tmp_path / "skills"
    for d in (catalogs_dir, memory_dir, skills_dir):
        d.mkdir(parents=True)
    return {
        "data_dir": data_dir,
        "catalogs_dir": catalogs_dir,
        "memory_dir": memory_dir,
        "skills_dir": skills_dir,
    }


def _write_catalog(catalogs_dir: Path, filename: str, entries: list[dict]) -> None:
    payload = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-05-01T00:00:00Z",
        "entries": entries,
    }
    (catalogs_dir / filename).write_text(json.dumps(payload, indent=2))


def _run_hook(env_setup, *, prompt: str, extra_env: dict | None = None) -> dict:
    """Invoke context_manager.py as a subprocess and return parsed stdout JSON."""
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("CLAUDE_PLUGIN"):
            del env[k]
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    env["CLAUDE_PLUGIN_DATA"] = str(env_setup["data_dir"])
    env["CLAUDE_PLUGIN_OPTION_memory_dir"] = str(env_setup["memory_dir"])
    env["CLAUDE_PLUGIN_OPTION_skills_dir"] = str(env_setup["skills_dir"])
    if extra_env:
        env.update(extra_env)

    stdin = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": "/tmp",
    })
    result = subprocess.run(
        [sys.executable, str(CONTEXT_MANAGER)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"context_manager exited {result.returncode}\nstderr: {result.stderr[:500]}"
        )
    out = result.stdout.strip().splitlines()
    if not out:
        raise AssertionError(f"No stdout from context_manager. stderr: {result.stderr[:500]}")
    return json.loads(out[-1])


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


class TestStampMemoryDates:
    def test_section_ref_stats_base_file(self, tmp_path):
        cm = import_script("cm_conflict_test", "context_manager.py")
        (tmp_path / "big.md").write_text("body")
        stamped = cm._stamp_memory_dates(
            tmp_path, {"big.md#Section": "section slice"}
        )
        assert stamped["big.md#Section"].startswith(STAMP_SENTINEL)
        assert stamped["big.md#Section"].endswith("section slice")

    def test_missing_file_skips_stamp(self, tmp_path):
        cm = import_script("cm_conflict_test2", "context_manager.py")
        stamped = cm._stamp_memory_dates(tmp_path, {"gone.md": "content"})
        assert stamped["gone.md"] == "content"


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
