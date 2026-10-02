"""Run configuration: concurrency and the model/effort per stage.

Precedence, highest first (the same order as review's config.py):

1. `walkthrough.yaml` in the run directory: `concurrency`, `explore_model`,
   `docs_model`, `trace_model`, `write_model`, `effort`, `max_turns`.
2. `multiplai.conf` keys `walkthrough_explore_model`, `walkthrough_docs_model`,
   `walkthrough_trace_model`, `walkthrough_write_model`, `walkthrough_effort`.
3. Default: no model and no effort passed, so every stage runs on the
   session's model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from multiplai_core.env import load_multiplai_conf

from .budget import DEFAULT_MAX_USD

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
KNOWN_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
STAGE_MODELS = ("explore_model", "docs_model", "trace_model", "write_model")


@dataclass
class WalkConfig:
    concurrency: int = DEFAULT_CONCURRENCY
    explore_model: str | None = None
    docs_model: str | None = None
    trace_model: str | None = None
    write_model: str | None = None
    effort: str | None = None
    max_turns: int = 60
    max_cost_usd: float | None = DEFAULT_MAX_USD


def _conf_value(conf: dict, key: str) -> str | None:
    for k in (key, key.upper()):
        value = conf.get(k)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_config(run_dir: Path | None, *, max_cost_usd: float | None = DEFAULT_MAX_USD) -> WalkConfig:
    cfg = WalkConfig(max_cost_usd=max_cost_usd)
    try:
        conf = load_multiplai_conf()
    except Exception as e:  # a broken conf file degrades to defaults
        log.warning("Could not read multiplai.conf (%s); using defaults", e)
        conf = {}
    for key in STAGE_MODELS:
        setattr(cfg, key, _conf_value(conf, f"walkthrough_{key}"))
    cfg.effort = _conf_value(conf, "walkthrough_effort")

    yaml_path = (run_dir / "walkthrough.yaml") if run_dir else None
    if yaml_path and yaml_path.is_file():
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.warning("Ignoring unparseable %s: %s", yaml_path, e)
            data = {}
        if not isinstance(data, dict):
            log.warning("Ignoring %s: expected a mapping", yaml_path)
            data = {}
        for key in (*STAGE_MODELS, "effort"):
            if data.get(key):
                setattr(cfg, key, str(data[key]))
        for key in ("concurrency", "max_turns"):
            if data.get(key) is not None:
                try:
                    setattr(cfg, key, max(1, int(data[key])))
                except (TypeError, ValueError):
                    log.warning("Ignoring %s=%r in %s (want an integer)", key, data[key], yaml_path)

    if cfg.effort and cfg.effort.lower() not in KNOWN_EFFORTS:
        log.warning("Ignoring unknown effort %r (expected one of %s)", cfg.effort, ", ".join(sorted(KNOWN_EFFORTS)))
        cfg.effort = None
    return cfg
