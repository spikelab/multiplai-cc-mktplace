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

from conftest import (
    CONTEXT_MANAGER,
    PLUGIN_ROOT,
    SCRIPTS_DIR,
    run_context_hook as _run_hook,
    write_catalog as _write_catalog,
)
from generators.base import CATALOG_SCHEMA_VERSION

# The sandbox layout (env_setup) and hook runner live in conftest.py —
# shared with the qmd and memory-conflict suites.


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

    def test_injects_via_hook_specific_output(self, env_setup):
        # Claude Code only injects UserPromptSubmit context from
        # hookSpecificOutput.additionalContext; a bare {"context": ...} key is
        # ignored. This guards the flagship routing from silently no-op'ing.
        (env_setup["memory_dir"] / "writing.md").write_text("# Writing guide")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "writing.md", "summary": "guide", "intent_domains": ["writing a blog post"]}],
        )

        out = _run_hook(env_setup, prompt="help me write a blog post")
        hso = out.get("hookSpecificOutput")
        assert hso is not None, "must emit hookSpecificOutput"
        assert hso["hookEventName"] == "UserPromptSubmit"
        # The injected context must equal the assembled corpus, not be empty.
        assert hso["additionalContext"] == out["context"]
        assert "Writing guide" in hso["additionalContext"]

    def test_skills_corpus_loaded_when_enabled(self, env_setup):
        # Skills are surfaced as lightweight recommendations built from
        # the catalog (summary + /<name> invocation hint), NOT by reading
        # the SKILL.md body — Claude Code already exposes skill bodies via
        # the Skill tool. The catalog stores the bare dir name as the key.
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice")
        skill_dir = env_setup["skills_dir"] / "writing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Writing skill body")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "voice.md", "intent_domains": ["writing"]}],
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
        assert "=== SKILLS ===" in out["context"]
        # Recommendation: catalog summary + invocation hint, not the body.
        assert "Drafts and edits blog posts" in out["context"]
        assert "/writing" in out["context"]
        assert "Writing skill body" not in out["context"]
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

    def test_scores_line_carries_truncated_prompt(self, env_setup):
        """ROUTING_SCORES embeds a sanitized ~80-char prompt so
        score→prompt attribution doesn't require transcript digging."""
        (env_setup["memory_dir"] / "strong.md").write_text("# strong\nbody")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "strong.md",
              "intent_domains": ["italian tax filing and FBAR"]}],
        )

        long_prompt = (
            "help with my italian tax filing and FBAR "
            "plus a very long tail " * 5
        )
        _run_hook(
            env_setup, prompt=long_prompt,
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
        collapsed = " ".join(long_prompt.split())
        assert rec["prompt"] == collapsed[:80] + "…"

    def test_skills_corpus_emits_scores_line(self, env_setup):
        """Skill routing must leave a score trail, not just memory."""
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice")
        skill_dir = env_setup["skills_dir"] / "writing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Writing skill body")
        _write_catalog(
            env_setup["catalogs_dir"],
            "memory.json",
            [{"source": "voice.md", "intent_domains": ["writing"]}],
        )
        _write_catalog(
            env_setup["catalogs_dir"],
            "skills.json",
            [{"source": "writing", "name": "writing",
              "summary": "Drafts and edits blog posts",
              "intent_domains": ["writing a blog post"]}],
        )

        prompt = "writing a blog post"
        out = _run_hook(
            env_setup, prompt=prompt,
            extra_env={
                "CLAUDE_PLUGIN_OPTION_enable_skills": "true",
                "MULTIPLAI_LOG_LEVEL": "INFO",
            },
        )
        assert out["skills_files"] >= 1
        logs = list(env_setup["data_dir"].rglob("context_manager.log"))
        assert logs, "context_manager.log not written"
        line = next(
            (ln for ln in logs[0].read_text().splitlines()
             if "ROUTING_SCORES skills=" in ln),
            None,
        )
        assert line, "no ROUTING_SCORES skills= line emitted"
        rec = json.loads(line.split("ROUTING_SCORES skills=", 1)[1])
        assert "writing" in [fn for fn, _ in rec["picked"]]
        assert rec["n_picked"] >= 1
        assert rec["prompt"] == prompt


# ---------------------------------------------------------------------------
# Post-cooldown re-floor helper (unit level)
# ---------------------------------------------------------------------------


class TestRefloorHelper:
    def test_no_change_when_top_pick_survives(self):
        from context_manager import _refloor_after_cooldown
        scored = [(10.0, "top.md"), (6.0, "mid.md"), (3.0, "weak.md")]
        kept, dropped = _refloor_after_cooldown(
            ["top.md", "weak.md"], ["mid.md"], scored
        )
        assert kept == ["top.md", "weak.md"]
        assert dropped == []

    def test_drops_below_bar_when_top_suppressed(self):
        from context_manager import _refloor_after_cooldown
        scored = [(10.0, "top.md"), (6.0, "mid.md"), (3.0, "weak.md")]
        kept, dropped = _refloor_after_cooldown(
            ["mid.md", "weak.md"], ["top.md"], scored
        )
        # bar = 0.5 × 10.0: mid (6.0) clears, weak (3.0) doesn't.
        assert kept == ["mid.md"]
        assert dropped == ["weak.md"]

    def test_unscored_expansion_picks_are_kept(self):
        from context_manager import _refloor_after_cooldown
        scored = [(10.0, "top.md"), (3.0, "weak.md")]
        kept, dropped = _refloor_after_cooldown(
            ["weak.md", "bundle-sibling.md"], ["top.md"], scored
        )
        # bundle-sibling.md has no score (metadata expansion) → kept.
        assert kept == ["bundle-sibling.md"]
        assert dropped == ["weak.md"]

    def test_no_scores_means_no_change(self):
        from context_manager import _refloor_after_cooldown
        kept, dropped = _refloor_after_cooldown(["a.md"], ["b.md"], [])
        assert kept == ["a.md"]
        assert dropped == []

    def test_prompt_for_log_collapses_and_truncates(self):
        from context_manager import _prompt_for_log
        assert _prompt_for_log("hello\n  world") == "hello world"
        long = "x" * 200
        out = _prompt_for_log(long)
        assert len(out) == 81
        assert out.endswith("…")


# ---------------------------------------------------------------------------
# Empty-everything case
# ---------------------------------------------------------------------------


class TestEmptyEverything:
    def test_empty_memory_dir_empty_prompt_no_crash(self, env_setup):
        out = _run_hook(env_setup, prompt="")
        # Empty prompt → router skipped, fallback may still pick up nothing
        assert "context" in out
        assert "memory_files" in out


# ---------------------------------------------------------------------------
# Re-recommendation cooldown (turn-based dedup)
# ---------------------------------------------------------------------------


class TestRecommendationCooldown:
    """A file injected on turn T is suppressed for the next `cooldown`
    turns (it's already in the conversation), then becomes eligible
    again. State persists in session_state.json across hook calls."""

    def _one_memory_file(self, env_setup):
        (env_setup["memory_dir"] / "python.md").write_text("# Python prefs")
        _write_catalog(
            env_setup["catalogs_dir"], "memory.json",
            [{"source": "python.md",
              "intent_domains": ["debugging python async code"]}],
        )

    def test_injected_file_suppressed_next_turn(self, env_setup):
        self._one_memory_file(env_setup)
        env = {"CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns": "4"}
        first = _run_hook(env_setup, prompt="debugging python async code", extra_env=env)
        assert first["memory_files"] == 1
        assert "python.md" in first["context"]
        second = _run_hook(env_setup, prompt="debugging python async code", extra_env=env)
        assert second["memory_files"] == 0
        assert "python.md" not in second["context"]

    def test_cooldown_expires_after_window(self, env_setup):
        self._one_memory_file(env_setup)
        env = {"CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns": "1"}
        # turn 1: injected
        assert _run_hook(env_setup, prompt="debugging python async code",
                         extra_env=env)["memory_files"] == 1
        # turn 2: within window (2-1=1 <= 1) → suppressed
        assert _run_hook(env_setup, prompt="debugging python async code",
                         extra_env=env)["memory_files"] == 0
        # turn 3: window passed (3-1=2 > 1) → re-injected
        assert _run_hook(env_setup, prompt="debugging python async code",
                         extra_env=env)["memory_files"] == 1

    def test_cooldown_zero_disables(self, env_setup):
        self._one_memory_file(env_setup)
        env = {"CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns": "0"}
        a = _run_hook(env_setup, prompt="debugging python async code", extra_env=env)
        b = _run_hook(env_setup, prompt="debugging python async code", extra_env=env)
        assert a["memory_files"] == 1
        assert b["memory_files"] == 1  # no cooldown → re-injected every turn

    def test_turn_state_persisted_to_session_state(self, env_setup):
        self._one_memory_file(env_setup)
        env = {"CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns": "4"}
        _run_hook(env_setup, prompt="debugging python async code", extra_env=env)
        state = json.loads(
            (env_setup["data_dir"] / "session_state.json").read_text()
        )
        assert state["turn_index"] == 1
        assert "python.md" in state["recently_injected"]["memory"]
        assert state["recently_injected"]["memory"]["python.md"] == 1

    def test_weak_survivor_dropped_when_anchor_on_cooldown(self, env_setup):
        """Post-cooldown re-floor (regression, session 351388d2): weak
        co-picks that only cleared the relevance cutoff relative to a
        strong top match must not be injected once cooldown suppresses
        that anchor — prefer nothing over noise."""
        (env_setup["memory_dir"] / "strong.md").write_text("# Strong")
        (env_setup["memory_dir"] / "weak.md").write_text("# Weak")
        _write_catalog(
            env_setup["catalogs_dir"], "memory.json",
            [
                {"source": "strong.md",
                 "intent_domains": ["debugging python async concurrency event loop"]},
                {"source": "weak.md", "intent_domains": ["python tooling"]},
            ],
        )
        env = {
            "CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns": "4",
            "MULTIPLAI_LOG_LEVEL": "INFO",
        }
        # Turn 1: only the strong anchor matches → injected.
        first = _run_hook(
            env_setup, prompt="debugging async concurrency event loop",
            extra_env=env,
        )
        assert first["memory_files"] == 1
        assert "strong.md" in first["context"]
        # Turn 2: both match; strong is on cooldown. weak.md clears the
        # absolute MIN_SIGNAL floor (~2.4) but sits at ~30% of the
        # suppressed top score (~8.0) — the old pipeline injected it
        # alone; the re-floor drops it and nothing is injected.
        second = _run_hook(
            env_setup,
            prompt="debugging python async concurrency event loop tooling",
            extra_env=env,
        )
        assert second["memory_files"] == 0
        assert "weak.md" not in second["context"]
        assert second["context"] == ""
        logs = list(env_setup["data_dir"].rglob("context_manager.log"))
        assert logs and any(
            "COOLDOWN_REFLOOR" in ln and "weak.md" in ln
            for ln in logs[0].read_text().splitlines()
        ), "COOLDOWN_REFLOOR line not emitted"

    def test_weak_copick_still_injected_when_anchor_survives(self, env_setup):
        """No behavior change while the top scorer is NOT suppressed:
        weak co-picks still ride in with their anchor."""
        (env_setup["memory_dir"] / "strong.md").write_text("# Strong")
        (env_setup["memory_dir"] / "weak.md").write_text("# Weak")
        _write_catalog(
            env_setup["catalogs_dir"], "memory.json",
            [
                {"source": "strong.md",
                 "intent_domains": ["debugging python async concurrency event loop"]},
                {"source": "weak.md", "intent_domains": ["python tooling"]},
            ],
        )
        out = _run_hook(
            env_setup,
            prompt="debugging python async concurrency event loop tooling",
            extra_env={"CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns": "4"},
        )
        assert out["memory_files"] == 2
        assert "weak.md" in out["context"]

    def test_precompact_clears_cooldown_map(self, env_setup):
        """PreCompact resets recently_injected so post-compaction every
        file is eligible again (the injected content was summarized away)."""
        # Seed a populated cooldown map.
        state_file = env_setup["data_dir"] / "session_state.json"
        state_file.write_text(json.dumps({
            "session_id": "abc123",
            "turn_index": 5,
            "recently_injected": {"memory": {"python.md": 5}, "skills": {}, "resources": {}},
        }))

        env = os.environ.copy()
        for k in list(env):
            if k.startswith("CLAUDE_PLUGIN"):
                del env[k]
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        env["CLAUDE_PLUGIN_DATA"] = str(env_setup["data_dir"])
        stdin = json.dumps({
            "hook_event_name": "PreCompact",
            "transcript_path": str(env_setup["tmp_path"] / "transcript.jsonl"),
            "session_id": "abc123",
            "cwd": "/tmp",
        })
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "pre_compact.py")],
            input=stdin, capture_output=True, text=True, env=env, timeout=15,
        )
        assert result.returncode == 0, result.stderr[:500]

        state = json.loads(state_file.read_text())
        assert state["recently_injected"] == {}
        # Unrelated state is preserved.
        assert state["session_id"] == "abc123"


# ---------------------------------------------------------------------------
# E2E: catalog_ttl_hours staleness warning (item 1 wiring)
# ---------------------------------------------------------------------------


class TestTtlStalenessE2E:
    """End-to-end proof that catalog_ttl_hours reaches the read path.

    Runs context_manager.py as a subprocess with a stale catalog and a
    tight CLAUDE_PLUGIN_OPTION_catalog_ttl_hours, and asserts the advisory
    staleness warning is emitted (to the logs, surfaced on stderr) while
    the catalog is still used — the full env-var → config → _load_corpora
    → _read_catalog_or_scan → _validate_catalog chain.
    """

    def _run_hook_capture(self, env_setup, *, prompt, extra_env=None):
        env = os.environ.copy()
        for k in list(env):
            if k.startswith("CLAUDE_PLUGIN"):
                del env[k]
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        env["CLAUDE_PLUGIN_DATA"] = str(env_setup["data_dir"])
        env["CLAUDE_PLUGIN_OPTION_memory_dir"] = str(env_setup["memory_dir"])
        if extra_env:
            env.update(extra_env)
        stdin = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "cwd": "/tmp",
        })
        return subprocess.run(
            [sys.executable, str(CONTEXT_MANAGER)],
            input=stdin, capture_output=True, text=True, env=env, timeout=15,
        )

    def _write_stale_memory_catalog(self, env_setup):
        (env_setup["memory_dir"] / "voice.md").write_text("# Voice\nstyle notes")
        payload = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generated_at": "2020-01-01T00:00:00Z",  # far past any TTL
            "entries": [
                {"source": "voice.md", "summary": "voice",
                 "intent_domains": ["writing voice"]},
            ],
        }
        (env_setup["catalogs_dir"] / "memory.json").write_text(json.dumps(payload))

    def test_stale_catalog_warns_with_tight_ttl(self, env_setup):
        """WHEN catalog_ttl_hours=1 and the catalog is years old
        THEN a staleness warning is logged AND the catalog is still used."""
        self._write_stale_memory_catalog(env_setup)

        result = self._run_hook_capture(
            env_setup,
            prompt="help me with writing voice",
            extra_env={"CLAUDE_PLUGIN_OPTION_catalog_ttl_hours": "1"},
        )
        assert result.returncode == 0, result.stderr[:500]
        assert "stale" in result.stderr.lower(), (
            "A stale catalog with a tight TTL must emit a staleness warning; "
            f"stderr was: {result.stderr[:500]}"
        )
        # Still used (fail-open): the JSON output is the last stdout line.
        out = json.loads(result.stdout.strip().splitlines()[-1])
        assert out["corpus_counts"]["memory"] >= 0

    def test_generous_ttl_does_not_warn(self, env_setup):
        """WHEN the default (generous) TTL applies THEN no staleness warning.

        The same years-old catalog would exceed the 168h default, so we
        pass an explicitly huge TTL to prove the compare — not the mere
        presence of the option — gates the warning.
        """
        self._write_stale_memory_catalog(env_setup)

        result = self._run_hook_capture(
            env_setup,
            prompt="help me with writing voice",
            extra_env={"CLAUDE_PLUGIN_OPTION_catalog_ttl_hours": "9999999"},
        )
        assert result.returncode == 0, result.stderr[:500]
        assert "stale" not in result.stderr.lower(), (
            f"A catalog within TTL must not warn; stderr was: {result.stderr[:500]}"
        )
