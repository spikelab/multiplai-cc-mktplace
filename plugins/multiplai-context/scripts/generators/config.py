"""Catalog configuration dataclass and loader.

Provides CatalogConfig with validation and load_catalog_config() to read
settings from CLAUDE_PLUGIN_OPTION_* environment variables with defaults.
"""

import os
from dataclasses import dataclass

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MODEL_DIARY = ""  # empty → inherits DEFAULT_MODEL
DEFAULT_TTL_HOURS = 168  # 7 days
DEFAULT_DIARY_CATALOG_DAYS = 7
DEFAULT_SKILLS_DIR = "~/.claude/skills"
DEFAULT_CATALOG_CONCURRENCY = 5  # Anthropic API tolerates this comfortably;
                                 # raise via env var if you have higher quotas.
DEFAULT_RECOMMEND_COOLDOWN_TURNS = 4  # Suppress re-recommending a file for
                                      # this many turns after it was injected
                                      # (already in conversation context).
                                      # 0 disables the cooldown.

# Resources retrieval backend. "catalog" is the original catalog+router
# path; "qmd" routes resources retrieval through a qmd index instead
# (see scripts/qmd_retrieval.py).
VALID_RESOURCES_RETRIEVAL = ("catalog", "qmd")
DEFAULT_RESOURCES_RETRIEVAL = "catalog"
VALID_QMD_MODES = ("local", "ssh", "http")
DEFAULT_QMD_MODE = "local"
DEFAULT_QMD_SSH_HOST = "host.docker.internal"
DEFAULT_QMD_COLLECTION = "resources"
VALID_QMD_STRATEGIES = ("fused", "hybrid", "fts")
DEFAULT_QMD_STRATEGY = "fused"
# http mode: resident `qmd mcp --http` daemon on the host.
DEFAULT_QMD_HTTP_URL = "http://host.docker.internal:8181"
DEFAULT_QMD_CANDIDATE_LIMIT = 10   # docs the daemon reranks (latency dial)
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
    resources_retrieval: str = DEFAULT_RESOURCES_RETRIEVAL
    qmd_mode: str = DEFAULT_QMD_MODE
    qmd_ssh_host: str = DEFAULT_QMD_SSH_HOST
    qmd_collection: str = DEFAULT_QMD_COLLECTION
    qmd_strategy: str = DEFAULT_QMD_STRATEGY
    qmd_http_url: str = DEFAULT_QMD_HTTP_URL
    qmd_candidate_limit: int = DEFAULT_QMD_CANDIDATE_LIMIT
    qmd_min_score: float = DEFAULT_QMD_MIN_SCORE
    catalog_concurrency: int = DEFAULT_CATALOG_CONCURRENCY
    recommend_cooldown_turns: int = DEFAULT_RECOMMEND_COOLDOWN_TURNS
    # When on, session_start fires a detached, flock-guarded cost collector
    # that prices the session-transcript corpus into the monthly ledger.
    # Local-only and cheap in steady state, but opt-in like the other flags.
    enable_costs: bool = False
    # Conflict-surfacing directive + last-updated stamps rendered above
    # every injected MEMORY block. On by default (reliability feature);
    # opt out to save ~90 tokens per memory-carrying turn.
    memory_conflict_preamble: bool = True

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

        if self.resources_retrieval not in VALID_RESOURCES_RETRIEVAL:
            self.resources_retrieval = DEFAULT_RESOURCES_RETRIEVAL

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

        if not 0.0 <= self.qmd_min_score <= 1.0:
            self.qmd_min_score = DEFAULT_QMD_MIN_SCORE

    @property
    def effective_diary_model(self) -> str:
        """Model for diary catalog — falls back to main catalog model if unset."""
        return self.model_diary.strip() or self.model


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes", "on")


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def load_catalog_config() -> CatalogConfig:
    """Load CatalogConfig from CLAUDE_PLUGIN_OPTION_* environment variables.

    Falls back to defaults for any unset or invalid values.
    """
    model = os.environ.get("CLAUDE_PLUGIN_OPTION_catalog_model", DEFAULT_MODEL)
    model_diary = os.environ.get("CLAUDE_PLUGIN_OPTION_catalog_model_diary", DEFAULT_MODEL_DIARY)
    ttl_hours = _parse_int(
        os.environ.get("CLAUDE_PLUGIN_OPTION_catalog_ttl_hours", str(DEFAULT_TTL_HOURS)),
        DEFAULT_TTL_HOURS,
    )
    diary_catalog_days = _parse_int(
        os.environ.get(
            "CLAUDE_PLUGIN_OPTION_diary_catalog_days", str(DEFAULT_DIARY_CATALOG_DAYS)
        ),
        DEFAULT_DIARY_CATALOG_DAYS,
    )
    enable_skills = _parse_bool(
        os.environ.get("CLAUDE_PLUGIN_OPTION_enable_skills", "false")
    )
    skills_dir = os.environ.get("CLAUDE_PLUGIN_OPTION_skills_dir", DEFAULT_SKILLS_DIR)
    plugins_dir = os.environ.get("CLAUDE_PLUGIN_OPTION_plugins_dir", "")
    enable_resources = _parse_bool(
        os.environ.get("CLAUDE_PLUGIN_OPTION_enable_resources", "false")
    )
    resources_dir = os.environ.get("CLAUDE_PLUGIN_OPTION_resources_dir", "")
    resources_retrieval = os.environ.get(
        "CLAUDE_PLUGIN_OPTION_resources_retrieval", DEFAULT_RESOURCES_RETRIEVAL
    )
    qmd_mode = os.environ.get("CLAUDE_PLUGIN_OPTION_qmd_mode", DEFAULT_QMD_MODE)
    qmd_ssh_host = os.environ.get(
        "CLAUDE_PLUGIN_OPTION_qmd_ssh_host", DEFAULT_QMD_SSH_HOST
    )
    qmd_collection = os.environ.get(
        "CLAUDE_PLUGIN_OPTION_qmd_collection", DEFAULT_QMD_COLLECTION
    )
    qmd_strategy = os.environ.get(
        "CLAUDE_PLUGIN_OPTION_qmd_strategy", DEFAULT_QMD_STRATEGY
    )
    qmd_http_url = os.environ.get(
        "CLAUDE_PLUGIN_OPTION_qmd_http_url", DEFAULT_QMD_HTTP_URL
    )
    qmd_candidate_limit = _parse_int(
        os.environ.get(
            "CLAUDE_PLUGIN_OPTION_qmd_candidate_limit", str(DEFAULT_QMD_CANDIDATE_LIMIT)
        ),
        DEFAULT_QMD_CANDIDATE_LIMIT,
    )
    qmd_min_score = _parse_float(
        os.environ.get(
            "CLAUDE_PLUGIN_OPTION_qmd_min_score", str(DEFAULT_QMD_MIN_SCORE)
        ),
        DEFAULT_QMD_MIN_SCORE,
    )
    catalog_concurrency = _parse_int(
        os.environ.get(
            "CLAUDE_PLUGIN_OPTION_catalog_concurrency", str(DEFAULT_CATALOG_CONCURRENCY)
        ),
        DEFAULT_CATALOG_CONCURRENCY,
    )
    recommend_cooldown_turns = _parse_int(
        os.environ.get(
            "CLAUDE_PLUGIN_OPTION_recommend_cooldown_turns",
            str(DEFAULT_RECOMMEND_COOLDOWN_TURNS),
        ),
        DEFAULT_RECOMMEND_COOLDOWN_TURNS,
    )
    enable_costs = _parse_bool(
        os.environ.get("CLAUDE_PLUGIN_OPTION_enable_costs", "false")
    )
    memory_conflict_preamble = _parse_bool(
        os.environ.get("CLAUDE_PLUGIN_OPTION_memory_conflict_preamble", "true")
    )

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
        resources_retrieval=resources_retrieval,
        qmd_mode=qmd_mode,
        qmd_ssh_host=qmd_ssh_host,
        qmd_collection=qmd_collection,
        qmd_strategy=qmd_strategy,
        qmd_http_url=qmd_http_url,
        qmd_candidate_limit=qmd_candidate_limit,
        qmd_min_score=qmd_min_score,
        catalog_concurrency=catalog_concurrency,
        recommend_cooldown_turns=recommend_cooldown_turns,
        enable_costs=enable_costs,
        memory_conflict_preamble=memory_conflict_preamble,
    )
