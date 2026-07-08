"""Catalog configuration dataclass and loader.

Provides CatalogConfig with validation and load_catalog_config() to read
settings from CLAUDE_PLUGIN_OPTION_* environment variables with defaults.
"""

import os
from dataclasses import dataclass

VALID_REASONING_EFFORTS = ("low", "medium", "high")

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MODEL_DIARY = ""  # empty → inherits DEFAULT_MODEL
DEFAULT_REASONING_EFFORT = "medium"
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
VALID_QMD_MODES = ("local", "ssh")
DEFAULT_QMD_MODE = "local"
DEFAULT_QMD_SSH_HOST = "host.docker.internal"
DEFAULT_QMD_COLLECTION = "resources"
VALID_QMD_STRATEGIES = ("fused", "hybrid", "fts")
DEFAULT_QMD_STRATEGY = "fused"


@dataclass
class CatalogConfig:
    """Configuration for catalog generation."""

    model: str = DEFAULT_MODEL
    model_diary: str = DEFAULT_MODEL_DIARY
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
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
    catalog_concurrency: int = DEFAULT_CATALOG_CONCURRENCY
    recommend_cooldown_turns: int = DEFAULT_RECOMMEND_COOLDOWN_TURNS

    def __post_init__(self):
        if not self.model or not self.model.strip():
            self.model = DEFAULT_MODEL

        if self.reasoning_effort not in VALID_REASONING_EFFORTS:
            self.reasoning_effort = DEFAULT_REASONING_EFFORT

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


def load_catalog_config() -> CatalogConfig:
    """Load CatalogConfig from CLAUDE_PLUGIN_OPTION_* environment variables.

    Falls back to defaults for any unset or invalid values.
    """
    model = os.environ.get("CLAUDE_PLUGIN_OPTION_catalog_model", DEFAULT_MODEL)
    model_diary = os.environ.get("CLAUDE_PLUGIN_OPTION_catalog_model_diary", DEFAULT_MODEL_DIARY)
    reasoning_effort = os.environ.get(
        "CLAUDE_PLUGIN_OPTION_catalog_reasoning_effort", DEFAULT_REASONING_EFFORT
    )
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

    return CatalogConfig(
        model=model,
        model_diary=model_diary,
        reasoning_effort=reasoning_effort,
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
        catalog_concurrency=catalog_concurrency,
        recommend_cooldown_turns=recommend_cooldown_turns,
    )
