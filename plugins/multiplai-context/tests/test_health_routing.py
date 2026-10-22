"""Tests for health_check's routing block against the router's real log lines.

On 2026-10-20 health reported ``live: {"samples": 0}`` and the skill read that
as "routing not yet meaningful", while the hook had routed 911 prompts with the
llm router that week. ``ROUTING_SCORES`` lines are written only by routers that
expose scores (token_overlap, llm_hybrid); the llm router writes
``Router llm picked: memory=N skills=M`` and, when it picks nothing,
``LLMRouter abstained``. These tests pin health to the lines each router
actually writes, and make the strategy the hook last used visible beside the
configured one.
"""

import json
from types import SimpleNamespace

import health_check
import pytest


def _ln(ts, msg, level="INFO"):
    return f"[2026-10-20T{ts}Z] [context_manager] [session:aaaaaaaa] {level}: {msg}\n"


def _llm_run(ts, memory, skills=0, abstained=False, degraded=False):
    out = ""
    if degraded:
        out += _ln(
            ts,
            "LLMRouter timed out after 25.0s; degrading to "
            "token_overlap for this prompt",
            "WARNING",
        )
    if abstained:
        out += _ln(ts, "LLMRouter abstained: model ran and picked nothing")
    out += _ln(ts, f"Router llm picked: memory={memory} skills={skills}")
    out += _ln(ts, "ROUTING memory=[] skills=[] resources=[]")
    return out


def _overlap_run(ts, picked):
    scores = {
        "picked": [[f"f{i}.md", 1.0 - i / 10] for i in range(picked)],
        "cap": 10,
        "n_candidates": 20,
        "n_picked": picked,
        "capped": False,
        "floor_excluded": None,
        "prompt": "x",
    }
    return _ln(ts, f"Router token_overlap picked: memory={picked} skills=0") + _ln(
        ts, "ROUTING_SCORES memory=" + json.dumps(scores)
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    paths = SimpleNamespace(logs_dir=lambda: logs, data_dir=lambda: data)
    monkeypatch.setattr(health_check, "detect_client_type", lambda: "sdk")

    def configure(strategy):
        monkeypatch.setattr(health_check, "resolve_strategy", lambda: strategy)

    return SimpleNamespace(logs=logs, paths=paths, configure=configure)


def test_llm_router_stats_come_from_its_own_log_lines(env):
    env.configure("llm")
    (env.logs / "context_manager-2026-10-20.log").write_text(
        _llm_run("10:00:00", 2, 1)
        + _llm_run("10:01:00", 0, 0, abstained=True)
        + _llm_run("10:02:00", 0, 1)
        + _llm_run("10:03:00", 4, 0, degraded=True)
    )

    live = health_check._routing_status(env.paths)["live"]

    assert live["strategy"] == "llm"
    assert live["source"] == "router_picks"
    assert live["samples"] == 4
    assert live["memory_empty_pct"] == 50.0
    assert live["abstained_pct"] == 25.0
    assert live["degraded_pct"] == 25.0
    assert live["mean_picked_memory"] == 1.5


def test_token_overlap_keeps_its_score_aggregates(env):
    env.configure("token_overlap")
    (env.logs / "context_manager-2026-10-20.log").write_text(
        _overlap_run("10:00:00", 3) + _overlap_run("10:01:00", 0)
    )

    live = health_check._routing_status(env.paths)["live"]

    assert live["strategy"] == "token_overlap"
    assert live["source"] == "ROUTING_SCORES"
    assert live["samples"] == 2
    assert live["empty_pct"] == 50.0
    assert live["mean_top_score"] == 1.0


def test_the_strategy_the_hook_last_logged_is_reported(env):
    env.configure("token_overlap")
    (env.logs / "context_manager-2026-10-19.log").write_text(
        _overlap_run("09:00:00", 2)
    )
    (env.logs / "context_manager-2026-10-20.log").write_text(_llm_run("10:00:00", 1))

    status = health_check._routing_status(env.paths)

    assert status["configured_strategy"] == "token_overlap"
    assert status["logged_strategy"] == "llm"
    assert status["logged_strategy_at"] == "2026-10-20T10:00:00Z"
    assert status["strategy_mismatch"] is True
    # Live stats describe what the hook ran, not the configured value it
    # disagrees with — so they come from the llm lines, not the old scores.
    assert status["live"]["strategy"] == "llm"
    assert status["live"]["samples"] == 1


def test_no_mismatch_when_the_hook_ran_the_configured_strategy(env):
    env.configure("llm")
    (env.logs / "context_manager-2026-10-20.log").write_text(_llm_run("10:00:00", 1))

    status = health_check._routing_status(env.paths)

    assert status["logged_strategy"] == "llm"
    assert status["strategy_mismatch"] is False


def test_no_router_lines_at_all_reports_zero_samples(env):
    env.configure("llm")

    status = health_check._routing_status(env.paths)

    assert status["logged_strategy"] is None
    assert status["strategy_mismatch"] is False
    assert status["live"]["samples"] == 0
    assert status["live"]["strategy"] == "llm"
    assert status["live"]["source"] == "router_picks"


def test_the_llm_window_is_bounded_to_the_newest_runs(env, monkeypatch):
    env.configure("llm")
    monkeypatch.setattr(health_check, "_MAX_ROUTING_SAMPLES", 2)
    (env.logs / "context_manager-2026-10-20.log").write_text(
        _llm_run("10:00:00", 0, abstained=True)
        + _llm_run("10:01:00", 0, abstained=True)
        + _llm_run("10:02:00", 2)
        + _llm_run("10:03:00", 2)
    )

    live = health_check._routing_status(env.paths)["live"]

    assert live["samples"] == 2
    assert live["abstained_pct"] == 0.0
    assert live["memory_empty_pct"] == 0.0


def test_a_mismatch_becomes_a_recommendation(env):
    env.configure("token_overlap")
    (env.logs / "context_manager-2026-10-20.log").write_text(_llm_run("10:00:00", 1))
    status = health_check._routing_status(env.paths)

    recs = health_check._routing_recommendations(status)

    assert any("llm" in r and "token_overlap" in r for r in recs)


def _hybrid_run(ts, abstained=False, degraded=False):
    out = ""
    if degraded:
        out += _ln(
            ts,
            "LLMRouter returned non-JSON; degrading to token_overlap for this prompt",
            "WARNING",
        )
    if abstained:
        out += _ln(ts, "LLMRouter abstained: model ran and picked nothing")
    out += _ln(ts, "Router llm_hybrid picked: memory=0 skills=0")
    return out


def test_llm_rates_count_only_llm_runs_after_a_switch_from_hybrid(env):
    """llm_hybrid wraps LLMRouter and logs the same abstain/degrade lines."""
    env.configure("llm")
    (env.logs / "context_manager-2026-10-20.log").write_text(
        "".join(
            _hybrid_run(f"09:0{i}:00", abstained=True, degraded=True) for i in range(6)
        )
        + _llm_run("10:00:00", 1)
        + _llm_run("10:01:00", 0, abstained=True)
    )

    live = health_check._routing_status(env.paths)["live"]

    assert live["samples"] == 2
    assert live["abstained_pct"] == 50.0
    assert live["degraded_pct"] == 0.0


def test_router_text_quoted_in_a_prompt_is_not_a_router_event(env):
    """ROUTING_SCORES lines carry the user's prompt; only real records count."""
    env.configure("token_overlap")
    skills = json.dumps(
        {
            "picked": [],
            "n_picked": 0,
            "capped": False,
            "prompt": "why did it log Router llm picked: "
            "memory=0 skills=0 and LLMRouter abstained",
        }
    )
    (env.logs / "context_manager-2026-10-20.log").write_text(
        _overlap_run("10:00:00", 2) + _ln("10:00:00", "ROUTING_SCORES skills=" + skills)
    )

    status = health_check._routing_status(env.paths)

    assert status["logged_strategy"] == "token_overlap"
    assert status["strategy_mismatch"] is False
    assert status["live"]["source"] == "ROUTING_SCORES"


def test_score_stats_cover_only_the_strategy_they_are_labelled_with(env):
    env.configure("llm_hybrid")
    hybrid_scores = json.dumps(
        {"picked": [["h.md", 0.5]], "n_picked": 1, "capped": True}
    )
    (env.logs / "context_manager-2026-10-20.log").write_text(
        _overlap_run("09:00:00", 0)
        + _overlap_run("09:01:00", 0)
        + _ln("10:00:00", "Router llm_hybrid picked: memory=1 skills=0")
        + _ln("10:00:00", "ROUTING_SCORES memory=" + hybrid_scores)
    )

    live = health_check._routing_status(env.paths)["live"]

    assert live["strategy"] == "llm_hybrid"
    assert live["samples"] == 1
    assert live["cap_saturation_pct"] == 100.0


def test_scores_with_no_pick_lines_are_not_labelled_as_any_router(env):
    """Old logs with no `Router <s> picked` lines cannot say who wrote them."""
    env.configure("llm")
    scores = json.dumps({"picked": [["a.md", 1.0]], "n_picked": 1})
    (env.logs / "context_manager.log").write_text(
        _ln("10:00:00", "ROUTING_SCORES memory=" + scores)
    )

    live = health_check._routing_status(env.paths)["live"]

    assert live["source"] == "ROUTING_SCORES"
    assert live["strategy"] is None
    assert live["samples"] == 1


def test_llm_hybrid_without_a_client_degrades_like_llm(env, monkeypatch):
    env.configure("llm_hybrid")
    monkeypatch.setattr(health_check, "detect_client_type", lambda: "none")
    (env.logs / "context_manager-2026-10-20.log").write_text(
        _overlap_run("10:00:00", 1)
    )

    status = health_check._routing_status(env.paths)

    assert status["effective_strategy"] == "token_overlap"
    assert status["degraded_to_fallback"] is True
    assert status["strategy_mismatch"] is False


def test_run_health_check_carries_the_mismatch_recommendation(
    env, tmp_path, monkeypatch, capsys
):
    env.configure("token_overlap")
    (env.logs / "context_manager-2026-10-20.log").write_text(_llm_run("10:00:00", 1))
    memory = tmp_path / "memory"
    memory.mkdir()
    paths = SimpleNamespace(
        logs_dir=env.paths.logs_dir,
        data_dir=env.paths.data_dir,
        memory_dir=lambda: memory,
        diary_dir=lambda: tmp_path / "diary",
        learnings_dir=lambda: tmp_path / "learnings",
        plugin_data=lambda: tmp_path / "data",
        venv_dir=lambda: tmp_path / "venv",
    )
    monkeypatch.setattr(health_check, "get_paths", lambda: paths)

    report = health_check.run_health_check()
    capsys.readouterr()

    assert report["routing"]["strategy_mismatch"] is True
    assert any("last routed with llm" in r for r in report["recommendations"])
