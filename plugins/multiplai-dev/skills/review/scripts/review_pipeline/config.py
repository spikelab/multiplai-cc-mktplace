"""Run configuration: concurrency and the model/effort per stage.

Precedence, highest first (same order as buildme's config.py: the project's
own file beats multiplai.conf, which beats the default):

1. `review.yaml` in the output directory (`<out>/review.yaml`):
   `concurrency`, `finder_model`, `verifier_model`, `merger_model`, `effort`,
   `max_turns`, `explain`, `find_with_facts`.
2. `multiplai.conf` keys `review_finder_model`, `review_verifier_model`,
   `review_effort` (the merger uses the verifier's), `review_explain`.
3. Default: no model and no effort passed, so every stage runs on the
   session's model. Verifiers still each get a fresh context.

`explain` (default false) runs the explain stage before find: `code_facts`
over the diff, written to `facts.json`. `find_with_facts` (default false,
needs `explain`) also puts those facts in each finder's prompt. Both stay off
by default until real runs are compared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core.env import load_multiplai_conf
from multiplai_core.stage_config import check_effort, coerce_int, conf_value, read_yaml_mapping

from .budget import DEFAULT_MAX_USD

log = logging.getLogger(__name__)

DIMENSIONS: tuple[str, ...] = ("diff-bugs", "callers", "history", "conventions", "tests")
DEFAULT_CONCURRENCY = 4


@dataclass
class ReviewConfig:
    concurrency: int = DEFAULT_CONCURRENCY
    finder_model: str | None = None
    verifier_model: str | None = None
    merger_model: str | None = None
    effort: str | None = None
    max_turns: int = 60
    max_cost_usd: float | None = DEFAULT_MAX_USD
    dimensions: tuple[str, ...] = field(default=DIMENSIONS)
    explain: bool = False
    find_with_facts: bool = False


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _flag(value, key: str, where) -> bool | None:
    """A yes/no setting: a YAML bool or one of the words above; None (logged) for anything else."""
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    log.warning("%s: %s must be true or false, got %r; ignored", where, key, value)
    return None


def load_config(out_dir: Path | None, *, max_cost_usd: float | None = DEFAULT_MAX_USD) -> ReviewConfig:
    cfg = ReviewConfig(max_cost_usd=max_cost_usd)
    try:
        conf = load_multiplai_conf()
    except Exception as e:  # a broken conf file degrades to defaults
        log.warning("Could not read multiplai.conf (%s); using defaults", e)
        conf = {}
    cfg.finder_model = conf_value(conf, "review_finder_model")
    cfg.verifier_model = conf_value(conf, "review_verifier_model")
    cfg.merger_model = cfg.verifier_model
    cfg.effort = conf_value(conf, "review_effort")
    explain = _flag(conf_value(conf, "review_explain"), "review_explain", "multiplai.conf")
    if explain is not None:
        cfg.explain = explain

    yaml_path = (out_dir / "review.yaml") if out_dir else None
    data = read_yaml_mapping(yaml_path)
    for key in ("finder_model", "verifier_model", "merger_model", "effort"):
        if data.get(key):
            setattr(cfg, key, str(data[key]))
    if data.get("verifier_model") and not data.get("merger_model"):
        cfg.merger_model = str(data["verifier_model"])
    for key in ("concurrency", "max_turns"):
        value = coerce_int(data, key, yaml_path)
        if value is not None:
            setattr(cfg, key, value)

    for key in ("explain", "find_with_facts"):
        value = _flag(data.get(key), key, yaml_path)
        if value is not None:
            setattr(cfg, key, value)
    if cfg.find_with_facts and not cfg.explain:
        log.warning("find_with_facts needs explain: true; the finders get no facts")
        cfg.find_with_facts = False

    cfg.effort = check_effort(cfg.effort)
    return cfg


SESSION_DEFAULT = "session default"
# The config field that sets each stage's model.
STAGE_MODELS = {"explain": "finder_model", "find": "finder_model", "verify": "verifier_model", "merge": "merger_model",
                "repeats": "merger_model", "assess": "merger_model"}


def run_config(cfg: ReviewConfig) -> dict:
    """What a run used, per stage. An unset model or effort is "session default", never blank.

    This is the configured model. The model the SDK actually ran is not
    returned by multiplai-core's `run_agent`, so it is not recorded.
    """
    return {
        "stages": {stage: {"model": getattr(cfg, attr) or SESSION_DEFAULT, "effort": cfg.effort or SESSION_DEFAULT}
                   for stage, attr in STAGE_MODELS.items()},
        "concurrency": cfg.concurrency,
        "max_turns": cfg.max_turns,
    }
