"""End-to-end tests for the multi-corpus context_manager pipeline (Phase D).

Exercises the full stdin → stdout flow with multiple catalogs enabled,
covering:

- Multi-corpus output structure (=== MEMORY ===, === SKILLS ===,
  === RESOURCES === sections in the assembled context)
- corpus_counts in the JSON output
- Bundle expansion
- Section-level loading via "file.md#Section" picks
- Single-corpus (memory-only) backward compat
- Schema mismatch fallback
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
CONTEXT_MANAGER = SCRIPTS_DIR / "context_manager.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generators.base import CATALOG_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_setup(tmp_path):
    """Build a sandboxed plugin layout: data, memory, skills, resources dirs."""
    data_dir = tmp_path / "plugin_data"
    catalogs_dir = data_dir / "catalogs"
    memory_dir = tmp_path / "memory"
    skills_dir = tmp_path / "skills"
    resources_dir = tmp_path / "resources"

    for d in (catalogs_dir, memory_dir, skills_dir, resources_dir):
        d.mkdir(parents=True)

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "catalogs_dir": catalogs_dir,
        "memory_dir": memory_dir,
        "skills_dir": skills_dir,
        "resources_dir": resources_dir,
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
    env["CLAUDE_PLUGIN_OPTION_resources_dir"] = str(env_setup["resources_dir"])
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
    # Stdout may have warning lines from logging; the LAST line is the JSON.
    out = result.stdout.strip().splitlines()
    if not out:
        raise AssertionError(f"No stdout from context_manager. stderr: {result.stderr[:500]}")
    return json.loads(out[-1])


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------


class TestMultiCorpusOutput:
    def test_output_has_corpus_counts(self, env_setup):
        """JSON output includes corpus_counts dict and per-corpus _files keys."""
        # Memory file with matching intent
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice\nstyle notes")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "voice.md", "summary": "voice", "intent_domains": ["writing voice"]}],
        )

        out = _run_hook(env_setup, prompt="help me with writing voice")
        assert "corpus_counts" in out
        assert set(out["corpus_counts"].keys()) == {"memory", "skills", "resources"}
        assert "memory_files" in out
        assert "skills_files" in out
        assert "resources_files" in out

    def test_memory_section_header_in_context(self, env_setup):
        (env_setup["memory_dir"] / "writing.md").write_text("# Writing guide")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "writing.md", "summary": "guide", "intent_domains": ["writing a blog post"]}],
        )

        out = _run_hook(env_setup, prompt="help me write a blog post")
        assert "=== MEMORY ===" in out["context"]
        assert "## writing.md" in out["context"]
        assert "Writing guide" in out["context"]

    def test_skills_corpus_loaded_when_enabled(self, env_setup):
        # Memory + skills both relevant to writing
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice")
        (env_setup["skills_dir"] / "writing.md").write_text("# Writing skill body")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "voice.md", "intent_domains": ["writing"]}],
        )
        _write_catalog(
            env_setup["catalogs_dir"],
            "skills.json",
            [{"source": "writing.md", "name": "writing",
              "intent_domains": ["writing a blog post"]}],
        )

        out = _run_hook(
            env_setup,
            prompt="writing a blog post",
            extra_env={"CLAUDE_PLUGIN_OPTION_enable_skills": "true"},
        )
        assert "=== SKILLS ===" in out["context"]
        assert "Writing skill body" in out["context"]
        assert out["skills_files"] >= 1
        assert out["corpus_counts"]["skills"] >= 1

    def test_resources_corpus_loaded_when_enabled(self, env_setup):
        (env_setup["memory_dir"] / "tech.md").write_text("# Tech prefs")
        (env_setup["resources_dir"] / "voice-ai.md").write_text("# Voice AI research notes")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "tech.md", "intent_domains": ["voice AI"]}],
        )
        _write_catalog(
            env_setup["catalogs_dir"],
            "resources.json",
            [{"source": "voice-ai.md", "intent_domains": ["voice AI frameworks"]}],
        )

        out = _run_hook(
            env_setup,
            prompt="researching voice AI frameworks",
            extra_env={
                "CLAUDE_PLUGIN_OPTION_enable_resources": "true",
                # resources_dir already set in _run_hook
            },
        )
        assert "=== RESOURCES ===" in out["context"]
        assert "Voice AI research" in out["context"]
        assert out["resources_files"] >= 1

    def test_skills_disabled_means_no_skills_section(self, env_setup):
        """Without enable_skills, skills corpus is silently empty even if catalog exists."""
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice")
        (env_setup["skills_dir"] / "writing.md").write_text("# Writing skill")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "voice.md", "intent_domains": ["writing a blog post"]}],
        )
        _write_catalog(
            env_setup["catalogs_dir"],
            "skills.json",
            [{"source": "writing.md", "name": "writing", "intent_domains": ["writing a blog post"]}],
        )

        # NO enable_skills env var set
        out = _run_hook(env_setup, prompt="writing a blog post")
        assert "=== SKILLS ===" not in out["context"]
        assert out["skills_files"] == 0


# ---------------------------------------------------------------------------
# Bundle expansion
# ---------------------------------------------------------------------------


class TestBundleExpansion:
    def test_bundle_sibling_loaded_with_picked_member(self, env_setup):
        """Picking one bundle member auto-loads its siblings."""
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice")
        (env_setup["memory_dir"] / "style.md").write_text("# Style")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [
                {
                    "source": "voice.md",
                    "intent_domains": ["writing voice"],
                    "bundle": "writing",
                },
                {
                    "source": "style.md",
                    "intent_domains": ["unrelated topic"],
                    "bundle": "writing",
                },
            ],
        )

        out = _run_hook(env_setup, prompt="help with writing voice")
        # voice.md picked by intent, style.md pulled in by bundle
        assert "## voice.md" in out["context"]
        assert "## style.md" in out["context"]


# ---------------------------------------------------------------------------
# Section-level loading
# ---------------------------------------------------------------------------


class TestSectionLoading:
    def test_full_file_loaded_without_section_anchors(self, env_setup):
        body = "# Big File\n\n## Architecture\n\nArch text.\n\n## Decisions\n\nDecision text.\n"
        (env_setup["memory_dir"] / "big.md").write_text(body)
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "big.md", "intent_domains": ["software architecture decisions"]}],
        )

        out = _run_hook(env_setup, prompt="software architecture decisions")
        # Whole file loaded
        assert "Arch text." in out["context"]
        assert "Decision text." in out["context"]


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_memory_only_catalog_still_works(self, env_setup):
        """Plugin with only memory catalog (skills/resources disabled) works."""
        (env_setup["memory_dir"] / "me.md").write_text("# About me")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "me.md", "intent_domains": ["personal context"]}],
        )

        out = _run_hook(env_setup, prompt="something about my personal context")
        assert out["memory_files"] >= 1
        assert out["skills_files"] == 0
        assert out["resources_files"] == 0
        assert "## me.md" in out["context"]

    def test_no_catalog_falls_back_to_metadata_ranking(self, env_setup):
        """No catalog at all → fallback to top-N memory file scan."""
        (env_setup["memory_dir"] / "fallback.md").write_text("# Fallback content")

        out = _run_hook(env_setup, prompt="any prompt at all")
        # Memory fallback path loaded the file
        assert "fallback.md" in out["context"] or out["memory_files"] >= 1

    def test_stale_schema_falls_back_gracefully(self, env_setup):
        """Old schema_version → invalidate, fall back to scan."""
        (env_setup["memory_dir"] / "fallback.md").write_text("# Fallback")
        # Write a 1.0.0 catalog (stale)
        (env_setup["catalogs_dir"] / "memory.json").write_text(json.dumps({
            "schema_version": "1.0.0",
            "entries": [{"source": "old.md"}],
        }))

        out = _run_hook(env_setup, prompt="any prompt")
        # Should not crash; should fall back to scan
        assert "context" in out
        # The non-existent old.md is NOT loaded; fallback scan finds fallback.md
        assert "old.md" not in out.get("context", "")


class TestAbstentionVsFallback:
    """The recency net is a failure safety net, not an abstention override.

    A successful router run that returns no memory picks (sub-floor or
    continuation guard) must inject *nothing* — the old behaviour
    silently dumped 10 recency-ranked files over a correct "nothing
    relevant", which was the dominant source of irrelevant context.
    The net must still fire for genuine failure (picked-but-not-on-disk,
    or the router never ran).
    """

    def test_subfloor_prompt_injects_nothing_not_recency_dump(self, env_setup):
        """Router scores below floor → empty context, NOT a recency dump."""
        # Several real files on disk: the old fallback WOULD have dumped these.
        for name in ("alpha.md", "beta.md", "gamma.md"):
            (env_setup["memory_dir"] / name).write_text(f"# {name}\nbody")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "alpha.md", "intent_domains": ["python programming"]}],
        )

        out = _run_hook(env_setup, prompt="completely unrelated zebra giraffe")
        assert out["memory_files"] == 0
        assert out["context"] == ""

    def test_continuation_prompt_injects_nothing(self, env_setup):
        """A bare go-ahead ("yes") abstains — no recency dump."""
        for name in ("alpha.md", "beta.md"):
            (env_setup["memory_dir"] / name).write_text(f"# {name}\nbody")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "alpha.md", "intent_domains": ["software architecture decisions"]}],
        )

        out = _run_hook(env_setup, prompt="yes")
        assert out["memory_files"] == 0
        assert out["context"] == ""

    def test_genuine_drift_still_uses_recency_safety_net(self, env_setup):
        """Router picks a file that isn't on disk → net still fires.

        This is real failure (catalog↔disk drift), not abstention, so
        the recency net must still surface *something* readable.
        """
        # Catalog points at ghost.md (strong match) but it's absent on disk;
        # only real.md exists, so the net must load real.md.
        (env_setup["memory_dir"] / "real.md").write_text("# Real\nrecoverable body")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "ghost.md", "intent_domains": ["software architecture decisions"]}],
        )

        out = _run_hook(env_setup, prompt="software architecture decisions")
        assert out["memory_files"] >= 1
        assert "real.md" in out["context"]


class TestRoutingScoresEmission:
    """ROUTING_SCORES (consumed by /health) must report the *picked*
    set, not the cap-truncated candidate pool — otherwise /health's
    live floor reads an excluded candidate's score, lower than what
    was actually injected.
    """

    def test_picked_array_matches_n_picked_not_candidate_pool(self, env_setup):
        # One strong entry + several weak candidates the relevance
        # cutoff drops: n_candidates > n_picked, so the old
        # scored[:cap] emission would over-report.
        for n in ("strong.md", "w1.md", "w2.md", "w3.md"):
            (env_setup["memory_dir"] / n).write_text(f"# {n}\nbody")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [
                {"source": "strong.md",
                 "intent_domains": ["italian tax filing and FBAR"]},
                {"source": "w1.md", "intent_domains": ["italian cooking"]},
                {"source": "w2.md", "intent_domains": ["tax software bugs"]},
                {"source": "w3.md", "intent_domains": ["filing cabinets"]},
            ],
        )

        out = _run_hook(
            env_setup,
            prompt="help with my italian tax filing and FBAR",
            extra_env={"MULTIPLAI_LOG_LEVEL": "INFO"},
        )
        logs = list(env_setup["data_dir"].rglob("context_manager.log"))
        assert logs, "context_manager.log not written"
        line = next(
            (ln for ln in logs[0].read_text().splitlines()
             if "ROUTING_SCORES memory=" in ln),
            None,
        )
        assert line, "no ROUTING_SCORES line emitted"
        rec = json.loads(line.split("ROUTING_SCORES memory=", 1)[1])
        # The contract: picked has exactly n_picked rows (the injected
        # set), never the cap/candidate-pool count.
        assert len(rec["picked"]) == rec["n_picked"]
        assert rec["n_picked"] == out["memory_files"]
        assert rec["n_picked"] < rec["n_candidates"]
        # Floor = lowest injected score, sorted desc → last row.
        scores = [s for _, s in rec["picked"]]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Empty-everything case
# ---------------------------------------------------------------------------


class TestEmptyEverything:
    def test_empty_memory_dir_empty_prompt_no_crash(self, env_setup):
        out = _run_hook(env_setup, prompt="")
        # Empty prompt → router skipped, fallback may still pick up nothing
        assert "context" in out
        assert "memory_files" in out
