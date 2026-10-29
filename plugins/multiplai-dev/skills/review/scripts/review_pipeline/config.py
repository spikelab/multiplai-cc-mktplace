"""Run configuration: concurrency and the model/effort per stage.

Precedence, highest first (same order as buildme's config.py: the project's
own file beats multiplai.conf, which beats the default):

0. `--model` on `review` or `resume` sets the finder, verifier and merger
   models for that run (`__main__.load_config`).
1. `review.yaml` in the output directory (`<out>/review.yaml`; for the
   default output directory, the old default's when the new one has none):
   `concurrency`, `finder_model`, `verifier_model`, `merger_model`, `effort`,
   `max_turns`, `explain`, `find_with_facts`, `trim`, `dimensions`,
   `extra_finders`, `deps`, `deps_max_mb`.
2. `multiplai.conf` keys `review_finder_model`, `review_verifier_model`,
   `review_effort` (the merger uses the verifier's), `review_explain`.
3. Default: no model and no effort passed, so every stage runs on the
   session's model. Verifiers still each get a fresh context.

`explain` (default false) runs the explain stage before find: `code_facts`
over the diff, written to `facts.json`. `find_with_facts` (default false,
needs `explain`) also puts those facts in each finder's prompt. Both stay off
by default until real runs are compared.

`trim` (default false) runs the trim finder (`TRIM_DIMENSION`) on a change
review: it judges the tests and comments the change adds or edits, and its
findings go to the advisory Trim section. A tree review runs it only with
`--trim`, which the review's state keeps (`ReviewState.trim`).

`dimensions` names the finders that run, from `DIMENSIONS` and `trim`; a
list without `trim` turns the trim finder off, and a list naming it turns it
on unless `trim: false`. Without the key, a change review runs `DIMENSIONS`
less `CHANGE_DEFAULT_OFF` (so not `tests`), and `trim` only with `trim: true`;
a tree review runs every one of `DIMENSIONS` it can. An unknown name raises
`ConfigError`, which names the valid ones. A change review keeps the list it
started with (`ReviewState.dimensions`), so `resume` runs the same finders.

`extra_finders` is accepted and ignored, whatever its value: every dimension
finder runs on every piece of a change review. `ReviewConfig.extra_finders`
says only that the key is there, so the review prints its one-line notice.

`deps` (default true; `review --no-deps` turns it off for one run) extracts the
source of the git dependencies a uv.lock at head pins into the snapshot
(`deps.py`); `deps_max_mb` (default 50) skips one whose source is larger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from multiplai_core.env import load_multiplai_conf
from multiplai_core.stage_config import (
    check_effort,
    coerce_int,
    conf_value,
    read_yaml_mapping,
)

from . import ledger
from .budget import DEFAULT_MAX_USD

log = logging.getLogger(__name__)

DIMENSIONS: tuple[str, ...] = (
    "diff-bugs",
    "callers",
    "history",
    "conventions",
    "tests",
)
# Not one of DIMENSIONS: `finder_dimensions` adds it when the review runs it.
TRIM_DIMENSION = "trim"
# A change review runs these only when review.yaml asks: `tests` named in `dimensions`, `trim: true`
# or `trim` named. On PR #295 neither found a root of the answer key in five runs.
CHANGE_DEFAULT_OFF: tuple[str, ...] = ("tests", TRIM_DIMENSION)
DEFAULT_CONCURRENCY = 16


class ConfigError(ValueError):
    """`review.yaml` holds a value the review cannot run with."""


@dataclass
class ReviewConfig:
    concurrency: int = DEFAULT_CONCURRENCY
    finder_model: str | None = None
    verifier_model: str | None = None
    merger_model: str | None = None
    effort: str | None = None
    max_turns: int = 60
    max_cost_usd: float | None = DEFAULT_MAX_USD
    # Where `max_cost_usd` came from: "default" when no --max-cost-usd was given (the cost NOTICE
    # prints then). No config file sets the budget today; "config" is kept for when one does.
    max_cost_source: Literal["default", "cli", "config"] = "default"
    dimensions: tuple[str, ...] = field(default=DIMENSIONS)
    dimensions_listed: bool = False  # review.yaml has `dimensions`
    explain: bool = False
    find_with_facts: bool = False
    trim: bool = False  # a change review's trim finder; a tree review's is `--trim`
    extra_finders: bool = False  # the key is in review.yaml; its value is ignored
    deps: bool = True
    deps_max_mb: int = 50
    # Verify LOW findings too (`review.yaml` `verify_low`, `--verify-low`). Off by default: a LOW
    # finding reaches the review `unverified`, with no verifier call (`stages/verify.py`). `risky`
    # (`--verify-low risky`) verifies only the LOW findings one no-tools call chooses as risky.
    verify_low: bool | Literal["risky"] = False
    # A re-pushed PR or branch reviews only the files whose patch changed (`incremental.py`);
    # `review.yaml` `incremental: false` or `--full` reviews every file.
    incremental: bool = True


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


CONFIG_FILE = "review.yaml"


def _dimensions(value, where) -> tuple[tuple[str, ...], bool]:
    """`dimensions` as (the ones of `DIMENSIONS` it names, in their order; whether it names `trim`)."""
    valid = (*DIMENSIONS, TRIM_DIMENSION)
    names = value if isinstance(value, list) else None
    if not names or not all(isinstance(n, str) for n in names):
        raise ConfigError(
            f"{where}: dimensions must be a list naming at least one of: {', '.join(valid)}"
        )
    unknown = [n for n in names if n.strip() not in valid]
    if unknown:
        raise ConfigError(
            f"{where}: unknown dimension{'' if len(unknown) == 1 else 's'} "
            f"{', '.join(repr(n) for n in unknown)}; the valid ones are: {', '.join(valid)}"
        )
    wanted = {n.strip() for n in names}
    return tuple(d for d in DIMENSIONS if d in wanted), TRIM_DIMENSION in wanted


def _yaml_path(out_dir: Path | None) -> Path | None:
    """`<out>/review.yaml`. When *out_dir* is the default output directory and
    holds none, the one left in the default before multiplai-dev 0.46
    (`ledger.legacy_reviews_root()`) is read instead, with a warning, until it
    is copied over (`migrate` copies it)."""
    if out_dir is None:
        return None
    path = out_dir / CONFIG_FILE
    if path.is_file():
        return path
    try:
        is_default = out_dir.resolve() == ledger.reviews_root().resolve()
    except Exception:  # noqa: BLE001 — no data directory: nothing to fall back from
        return path
    old = ledger.legacy_reviews_root() / CONFIG_FILE
    if is_default and old.is_file():
        log.warning(
            "reading %s: %s is now read from %s; copy it there, or run "
            "`python -m review_pipeline migrate --from %s`",
            old,
            CONFIG_FILE,
            path,
            old.parent,
        )
        return old
    return path


def load_config(
    out_dir: Path | None,
    *,
    max_cost_usd: float | None = DEFAULT_MAX_USD,
    max_cost_source: Literal["default", "cli", "config"] = "default",
) -> ReviewConfig:
    cfg = ReviewConfig(max_cost_usd=max_cost_usd, max_cost_source=max_cost_source)
    try:
        conf = load_multiplai_conf()
    except Exception as e:  # a broken conf file degrades to defaults
        log.warning("Could not read multiplai.conf (%s); using defaults", e)
        conf = {}
    cfg.finder_model = conf_value(conf, "review_finder_model")
    cfg.verifier_model = conf_value(conf, "review_verifier_model")
    cfg.merger_model = cfg.verifier_model
    cfg.effort = conf_value(conf, "review_effort")
    explain = _flag(
        conf_value(conf, "review_explain"), "review_explain", "multiplai.conf"
    )
    if explain is not None:
        cfg.explain = explain

    yaml_path = _yaml_path(out_dir)
    data = read_yaml_mapping(yaml_path)
    for key in ("finder_model", "verifier_model", "merger_model", "effort"):
        if data.get(key):
            setattr(cfg, key, str(data[key]))
    if data.get("verifier_model") and not data.get("merger_model"):
        cfg.merger_model = str(data["verifier_model"])
    for key in ("concurrency", "max_turns", "deps_max_mb"):
        value = coerce_int(data, key, yaml_path)
        if value is not None:
            setattr(cfg, key, value)

    cfg.extra_finders = "extra_finders" in data
    for key in (
        "explain",
        "find_with_facts",
        "trim",
        "deps",
        "verify_low",
        "incremental",
    ):
        if key == "verify_low" and str(data.get(key)).strip().lower() == "risky":
            cfg.verify_low = "risky"
            continue
        value = _flag(data.get(key), key, yaml_path)
        if value is not None:
            setattr(cfg, key, value)
    if "dimensions" in data:
        cfg.dimensions, listed_trim = _dimensions(data["dimensions"], yaml_path)
        cfg.dimensions_listed = True
        cfg.trim = (
            listed_trim and _flag(data.get("trim"), "trim", yaml_path) is not False
        )
    if cfg.find_with_facts and not cfg.explain:
        log.warning("find_with_facts needs explain: true; the finders get no facts")
        cfg.find_with_facts = False

    cfg.effort = check_effort(cfg.effort)
    return cfg


SESSION_DEFAULT = "session default"
# The config field that sets each stage's model.
STAGE_MODELS = {
    "explain": "finder_model",
    "find": "finder_model",
    "verify": "verifier_model",
    "recheck": "verifier_model",
    "merge": "merger_model",
    "repeats": "merger_model",
    "assess": "merger_model",
    "write": "merger_model",
}


def run_config(cfg: ReviewConfig) -> dict:
    """What a run used, per stage. An unset model or effort is "session default", never blank.

    This is the configured model. The model the SDK actually ran is not
    returned by multiplai-core's `run_agent`, so it is not recorded.
    """
    return {
        "stages": {
            stage: {
                "model": getattr(cfg, attr) or SESSION_DEFAULT,
                "effort": cfg.effort or SESSION_DEFAULT,
            }
            for stage, attr in STAGE_MODELS.items()
        },
        "concurrency": cfg.concurrency,
        "max_turns": cfg.max_turns,
    }
