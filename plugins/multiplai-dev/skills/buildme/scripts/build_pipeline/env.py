"""Load .env and multiplai.conf from the claude-code-multiplai project root.

Ported from deep-research pipeline — same project root detection and env loading.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward from `start` looking for the claude-code-multiplai root.

    A directory qualifies if it contains both `.env.example` AND `dotfiles/`.
    Falls back to the first ancestor with a `.env` file.
    """
    current = (start or Path(__file__)).resolve()
    for ancestor in [current, *current.parents]:
        if (ancestor / ".env.example").exists() and (ancestor / "dotfiles").is_dir():
            return ancestor
    for ancestor in [current, *current.parents]:
        if (ancestor / ".env").exists():
            return ancestor
    return None


def load_env() -> bool:
    """Load .env from the project root. Existing env vars are NOT overridden.

    $CLAUDE_MULTIPLAI_HOME (the kit root, exported by claude.sh) wins over the
    walk-up: when this skill ships inside a plugin, the install dir may live
    outside the kit tree, so walking up from the script can't find the kit .env.
    """
    home = os.environ.get("CLAUDE_MULTIPLAI_HOME")
    if home and (Path(home) / ".env").exists():
        env_file = Path(home) / ".env"
    else:
        root = find_project_root()
        if root is None:
            log.debug("No project root found — skipping .env load")
            return False
        env_file = root / ".env"
    if not env_file.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        log.warning("python-dotenv not installed; cannot auto-load %s", env_file)
        return False
    loaded = load_dotenv(env_file, override=False)
    if loaded:
        log.info("Loaded .env from %s", env_file)
    return loaded


def load_multiplai_conf() -> dict:
    """Load multiplai.conf with optional INI-style section support.

    Returns a dict with global keys at top level, plus ``_sections`` for per-skill overrides.
    """
    import re

    # multiplai.conf lives at the kit project root ($CLAUDE_MULTIPLAI_HOME)
    multiplai_home = os.environ.get("CLAUDE_MULTIPLAI_HOME")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if multiplai_home:
        conf_path = Path(multiplai_home) / "multiplai.conf"
    elif config_dir:
        # Fallback: derive kit root as parent of CLAUDE_CONFIG_DIR (dotfiles/)
        conf_path = Path(config_dir).parent / "multiplai.conf"
    else:
        root = find_project_root()
        conf_path = root / "multiplai.conf" if root else Path("/nonexistent")
    if not conf_path.exists():
        return {"_sections": {}}

    result: dict[str, str] = {}
    sections: dict[str, dict[str, str]] = {}
    current_section: str | None = None

    for line in conf_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = re.match(r"^\[([a-zA-Z0-9_-]+)\]\s*$", line)
        if section_match:
            current_section = section_match.group(1)
            sections.setdefault(current_section, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if current_section:
                sections[current_section][key] = value
            else:
                result[key] = value

    result["_sections"] = sections  # type: ignore[assignment]
    return result


_TIERS = {"haiku": 1, "sonnet": 2, "opus": 3}
_EFFORT_TIERS = {"low": 1, "medium": 2, "high": 3, "max": 4}


def _tier(model: str) -> int:
    for name, rank in _TIERS.items():
        if name in model.lower():
            return rank
    return 2


def _effort_tier(effort: str) -> int:
    return _EFFORT_TIERS.get(effort.lower(), 3)


def resolve_model(requested: str, ceiling: str | None = None) -> str:
    """Return the requested model, or the ceiling if requested is above it."""
    if ceiling is None:
        ceiling = os.environ.get("MULTIPLAI_MODEL", "claude-sonnet-4-6")
    if _tier(requested) > _tier(ceiling):
        log.info("Model ceiling: %s → %s", requested, ceiling)
        return ceiling
    return requested


def resolve_effort(requested: str, ceiling: str | None = None) -> str:
    """Return the requested effort, or the ceiling if requested is above it."""
    if ceiling is None:
        ceiling = os.environ.get("MULTIPLAI_EFFORT", "high")
    if _effort_tier(requested) > _effort_tier(ceiling):
        log.info("Effort ceiling: %s → %s", requested, ceiling)
        return ceiling
    return requested
