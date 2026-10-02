"""load_config: walkthrough.yaml over multiplai.conf over the defaults; bad values are dropped."""

from __future__ import annotations

import os
from pathlib import Path

from walkthrough_pipeline.config import DEFAULT_CONCURRENCY, load_config


def _conf(text: str) -> None:
    home = Path(os.environ["CLAUDE_MULTIPLAI_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "multiplai.conf").write_text(text)


def test_defaults_without_conf_or_yaml(tmp_path):
    cfg = load_config(tmp_path)
    assert (cfg.concurrency, cfg.explore_model, cfg.effort, cfg.max_turns) == (DEFAULT_CONCURRENCY, None, None, 60)


def test_conf_over_defaults_and_yaml_over_conf(tmp_path):
    _conf("walkthrough_explore_model = sonnet\nwalkthrough_write_model = opus\nwalkthrough_effort = medium\n")
    cfg = load_config(tmp_path)
    assert (cfg.explore_model, cfg.write_model, cfg.effort) == ("sonnet", "opus", "medium")
    (tmp_path / "walkthrough.yaml").write_text("explore_model: haiku\neffort: high\nconcurrency: 2\n")
    cfg = load_config(tmp_path)
    assert (cfg.explore_model, cfg.write_model, cfg.effort, cfg.concurrency) == ("haiku", "opus", "high", 2)


def test_bad_integers_and_an_unknown_effort_are_dropped(tmp_path):
    (tmp_path / "walkthrough.yaml").write_text("concurrency: 0\nmax_turns: lots\neffort: turbo\n")
    cfg = load_config(tmp_path)
    assert cfg.concurrency == 1          # clamped
    assert cfg.max_turns == 60           # not an integer: ignored
    assert cfg.effort is None            # unknown effort: dropped


def test_unparseable_yaml_keeps_conf(tmp_path):
    _conf("walkthrough_trace_model = sonnet\n")
    (tmp_path / "walkthrough.yaml").write_text("explore_model: [unclosed\n")
    assert load_config(tmp_path).trace_model == "sonnet"
