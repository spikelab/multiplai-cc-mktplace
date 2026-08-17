"""Catalog configuration dataclass and loader.

Provides CatalogConfig with validation and load_catalog_config() to read
settings from the plugin's userConfig options with defaults. Option reads go
through ``multiplai_core.plugin_options``, which resolves the uppercased
variable name Claude Code actually exports.
"""

import logging
from dataclasses import dataclass

from multiplai_core.plugin_options import (
    option,
    option_bool,
    option_float,
    option_int,
    option_present,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MODEL_DIARY = ""  # empty → inherits DEFAULT_MODEL
DEFAULT_TTL_HOURS = 168  # 7 days
DEFAULT_DIARY_CATALOG_DAYS = 7
DEFAULT_SKILLS_DIR = "~/.claude/skills"
DEFAULT_CATALOG_CONCURRENCY = 5  # Anthropic API tolerates this comfortably;
                                 # raise via the catalog_concurrency option if
                                 # you have higher quotas.
DEFAULT_RECOMMEND_COOLDOWN_TURNS = 4  # Suppress re-recommending a file for
                                      # this many turns after it was injected
                                      # (already in conversation context).
                                      # 0 disables the cooldown.
DEFAULT_KEEP_RATIO = 0.30  # token_overlap relative-cutoff: drop memory
                           # files scoring < ratio × top. Higher = stricter
                           # (fewer, more-relevant files; less cap saturation).
                           # See memory_router.DEFAULT_KEEP_RATIO for the
                           # production-replay calibration. Clamped to (0, 1].

# A resources corpus is retrieved through qmd and nothing else
# (see scripts/qmd_retrieval.py). The catalog+router path that memory,
# banks, diary and skills use was removed in 0.52.0: it summarised each
# resource file with an LLM call and routed on those summaries, which
# suits a corpus you wrote and re-read, not one you collected. A research
# archive is long, heterogeneous, and matched on passages rather than on
# whole documents — the shape qmd's chunk-level index is built for.
VALID_QMD_MODES = ("local", "ssh", "http")
DEFAULT_QMD_MODE = "local"
DEFAULT_QMD_SSH_HOST = "host.docker.internal"
DEFAULT_QMD_COLLECTION = "resources"
VALID_QMD_STRATEGIES = ("fused", "hybrid", "fts")
DEFAULT_QMD_STRATEGY = "fused"
# http mode: resident `qmd mcp --http` daemon on the host.
DEFAULT_QMD_HTTP_URL = "http://host.docker.internal:8181"
DEFAULT_QMD_CANDIDATE_LIMIT = 10   # docs the daemon reranks (latency dial)
# Ceiling for qmd_candidate_limit: the HTTP request timeout scales linearly
# with this dial (qmd_retrieval.http_timeout: 3 + 0.7s/doc), so an
# unbounded value would let a config typo stall every prompt for minutes.
# 50 docs ≈ 38s worst-case — already generous.
MAX_QMD_CANDIDATE_LIMIT = 50
DEFAULT_QMD_MIN_SCORE = 0.30       # weak-match cutoff applied to results


@dataclass
class CatalogConfig:
    """Configuration for catalog generation."""

    model: str = DEFAULT_MODEL
    model_diary: str = DEFAULT_MODEL_DIARY
    ttl_hours: int = DEFAULT_TTL_HOURS
    diary_catalog_days: int = DEFAULT_DIARY_CATALOG_DAYS
    enable_skills: bool = False
    skills_dir: str = DEFAULT_SKILLS_DIR
    plugins_dir: str = ""  # empty → derived from $CLAUDE_CONFIG_DIR/plugins at use time
    enable_resources: bool = False
    resources_dir: str = ""
    qmd_mode: str = DEFAULT_QMD_MODE
    qmd_ssh_host: str = DEFAULT_QMD_SSH_HOST
    qmd_collection: str = DEFAULT_QMD_COLLECTION
    qmd_strategy: str = DEFAULT_QMD_STRATEGY
    qmd_http_url: str = DEFAULT_QMD_HTTP_URL
    qmd_candidate_limit: int = DEFAULT_QMD_CANDIDATE_LIMIT
    qmd_min_score: float = DEFAULT_QMD_MIN_SCORE
    catalog_concurrency: int = DEFAULT_CATALOG_CONCURRENCY
    recommend_cooldown_turns: int = DEFAULT_RECOMMEND_COOLDOWN_TURNS
    keep_ratio: float = DEFAULT_KEEP_RATIO
    # When on, session_start fires a detached, flock-guarded cost collector
    # that prices the session-transcript corpus into the monthly ledger.
    # Local-only and cheap in steady state, but opt-in like the other flags.
    enable_costs: bool = False
    # Conflict-surfacing directive + last-updated stamps rendered above
    # every injected MEMORY block. On by default (reliability feature);
    # opt out to save ~90 tokens per memory-carrying turn.
    memory_conflict_preamble: bool = True
    # Stack-detected pointers to $CLAUDE_CONFIG_DIR/reference/dev/ docs,
    # injected once per (session, project). On by default: it costs ~60
    # tokens once and it is the only mechanism that loads engineering
    # standards outside buildme. No-ops when reference/dev/ is absent.
    enable_dev_references: bool = True

    def __post_init__(self):
        if not self.model or not self.model.strip():
            self.model = DEFAULT_MODEL

        if self.ttl_hours < 0:
            self.ttl_hours = DEFAULT_TTL_HOURS

        if self.diary_catalog_days < 0:
            self.diary_catalog_days = DEFAULT_DIARY_CATALOG_DAYS

        if self.catalog_concurrency < 1:
            self.catalog_concurrency = DEFAULT_CATALOG_CONCURRENCY

        if self.recommend_cooldown_turns < 0:
            self.recommend_cooldown_turns = DEFAULT_RECOMMEND_COOLDOWN_TURNS

        # keep_ratio is a fraction of the top score; a value <= 0 would
        # admit everything (defeating the cutoff) and > 1 would keep only
        # the top file. Clamp out-of-range configs to the default.
        if not 0.0 < self.keep_ratio <= 1.0:
            self.keep_ratio = DEFAULT_KEEP_RATIO

        if self.qmd_mode not in VALID_QMD_MODES:
            self.qmd_mode = DEFAULT_QMD_MODE

        if not self.qmd_ssh_host.strip():
            self.qmd_ssh_host = DEFAULT_QMD_SSH_HOST

        if not self.qmd_collection.strip():
            self.qmd_collection = DEFAULT_QMD_COLLECTION

        if self.qmd_strategy not in VALID_QMD_STRATEGIES:
            self.qmd_strategy = DEFAULT_QMD_STRATEGY

        if not self.qmd_http_url.strip():
            self.qmd_http_url = DEFAULT_QMD_HTTP_URL

        if self.qmd_candidate_limit < 1:
            self.qmd_candidate_limit = DEFAULT_QMD_CANDIDATE_LIMIT
        elif self.qmd_candidate_limit > MAX_QMD_CANDIDATE_LIMIT:
            self.qmd_candidate_limit = MAX_QMD_CANDIDATE_LIMIT

        if not 0.0 <= self.qmd_min_score <= 1.0:
            self.qmd_min_score = DEFAULT_QMD_MIN_SCORE

    @property
    def effective_diary_model(self) -> str:
        """Model for diary catalog — falls back to main catalog model if unset."""
        return self.model_diary.strip() or self.model


# Every option this loader reads, in declaration order. Used only for the
# fallback log line below — the reads themselves each carry their own default.
_KNOWN_OPTIONS = (
    "catalog_model", "catalog_model_diary", "catalog_ttl_hours",
    "diary_catalog_days", "enable_skills", "skills_dir", "plugins_dir",
    "enable_resources", "resources_dir", "qmd_mode",
    "qmd_ssh_host", "qmd_collection", "qmd_strategy", "qmd_http_url",
    "qmd_candidate_limit", "qmd_min_score", "catalog_concurrency",
    "recommend_cooldown_turns", "keep_ratio", "enable_costs",
    "memory_conflict_preamble", "enable_dev_references", "memory_write_mode",
)


def _log_defaulted_options() -> None:
    """Log, in one line, which options the harness did not deliver.

    The reason the wrong-case bug (#148) survived eight days is that a *dead*
    option and a *deliberately off* option looked identical in the logs: both
    were simply absent. Naming the defaulted ones makes the next silent-config
    failure visible without anyone having to go looking.

    Names only, never values — one of the declared options
    (``anthropic_api_key``) is ``sensitive: true``, and a log line that prints
    values is one rename away from leaking it. This runs on every prompt, so
    it stays a single INFO line.
    """
    defaulted = [name for name in _KNOWN_OPTIONS if not option_present(name)]
    if defaulted:
        logger.info(
            "Plugin options at default (%d/%d, not set by the user): %s",
            len(defaulted), len(_KNOWN_OPTIONS), ", ".join(defaulted),
        )


def load_catalog_config() -> CatalogConfig:
    """Load CatalogConfig from the plugin's userConfig options.

    Falls back to defaults for any unset or invalid values. Names are the bare
    option keys; ``multiplai_core.plugin_options`` uppercases them to the
    ``CLAUDE_PLUGIN_OPTION_<KEY>`` variables the harness exports.
    """
    model = option("catalog_model", DEFAULT_MODEL)
    model_diary = option("catalog_model_diary", DEFAULT_MODEL_DIARY)
    ttl_hours = option_int("catalog_ttl_hours", DEFAULT_TTL_HOURS)
    diary_catalog_days = option_int(
        "diary_catalog_days", DEFAULT_DIARY_CATALOG_DAYS
    )
    enable_skills = option_bool("enable_skills", False)
    skills_dir = option("skills_dir", DEFAULT_SKILLS_DIR)
    plugins_dir = option("plugins_dir", "")
    enable_resources = option_bool("enable_resources", False)
    resources_dir = option("resources_dir", "")
    qmd_mode = option("qmd_mode", DEFAULT_QMD_MODE)
    qmd_ssh_host = option("qmd_ssh_host", DEFAULT_QMD_SSH_HOST)
    qmd_collection = option("qmd_collection", DEFAULT_QMD_COLLECTION)
    qmd_strategy = option("qmd_strategy", DEFAULT_QMD_STRATEGY)
    qmd_http_url = option("qmd_http_url", DEFAULT_QMD_HTTP_URL)
    qmd_candidate_limit = option_int(
        "qmd_candidate_limit", DEFAULT_QMD_CANDIDATE_LIMIT
    )
    qmd_min_score = option_float("qmd_min_score", DEFAULT_QMD_MIN_SCORE)
    catalog_concurrency = option_int(
        "catalog_concurrency", DEFAULT_CATALOG_CONCURRENCY
    )
    recommend_cooldown_turns = option_int(
        "recommend_cooldown_turns", DEFAULT_RECOMMEND_COOLDOWN_TURNS
    )
    keep_ratio = option_float("keep_ratio", DEFAULT_KEEP_RATIO)
    enable_costs = option_bool("enable_costs", False)
    memory_conflict_preamble = option_bool("memory_conflict_preamble", True)
    enable_dev_references = option_bool("enable_dev_references", True)

    _log_defaulted_options()

    return CatalogConfig(
        model=model,
        model_diary=model_diary,
        ttl_hours=ttl_hours,
        diary_catalog_days=diary_catalog_days,
        enable_skills=enable_skills,
        skills_dir=skills_dir,
        plugins_dir=plugins_dir,
        enable_resources=enable_resources,
        resources_dir=resources_dir,
        qmd_mode=qmd_mode,
        qmd_ssh_host=qmd_ssh_host,
        qmd_collection=qmd_collection,
        qmd_strategy=qmd_strategy,
        qmd_http_url=qmd_http_url,
        qmd_candidate_limit=qmd_candidate_limit,
        qmd_min_score=qmd_min_score,
        catalog_concurrency=catalog_concurrency,
        recommend_cooldown_turns=recommend_cooldown_turns,
        keep_ratio=keep_ratio,
        enable_costs=enable_costs,
        memory_conflict_preamble=memory_conflict_preamble,
        enable_dev_references=enable_dev_references,
    )
