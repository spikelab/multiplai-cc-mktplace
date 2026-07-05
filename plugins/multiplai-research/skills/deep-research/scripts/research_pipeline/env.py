"""Load .env from the multiplai-kit project root.

The convention for this project is a single .env at the repo root, gitignored,
shared across all skills that need secrets. This module finds it by walking up
from the script location until it finds a directory containing a .env file
(or the project marker `requirements.txt` + `dotfiles/` combo).

Import this module early in pipeline startup — it's safe to call load_env()
multiple times (python-dotenv is idempotent by default).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward from `start` looking for the multiplai-kit root.

    A directory qualifies as the project root if it contains both a
    `.env.example` AND a `dotfiles/` directory (matches multiplai-kit
    layout). Falls back to the first ancestor with a `.env` file.
    """
    current = (start or Path(__file__)).resolve()

    # Walk up looking for the project marker
    for ancestor in [current, *current.parents]:
        if (ancestor / ".env.example").exists() and (ancestor / "dotfiles").is_dir():
            return ancestor

    # Fallback: first ancestor with a .env
    for ancestor in [current, *current.parents]:
        if (ancestor / ".env").exists():
            return ancestor

    return None


def _env_candidates() -> list[Path]:
    """Ordered .env locations, most explicit first.

    Covers a plain plugin install (no kit tree): an explicit override, the kit
    home, the current working directory, and finally the marker/walk-up. Without
    the cwd + explicit paths, a user running deep-research with external APIs
    (`--no-claude-tools`) outside a kit workspace would silently get no keys.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("MULTIPLAI_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit))
    home = os.environ.get("CLAUDE_MULTIPLAI_HOME")
    if home:
        candidates.append(Path(home) / ".env")
    candidates.append(Path.cwd() / ".env")
    root = find_project_root()
    if root is not None:
        candidates.append(root / ".env")
    return candidates


def load_env() -> bool:
    """Load .env into os.environ from the first candidate that exists.

    Returns True if a .env file was found and loaded, False otherwise.
    Existing environment variables are NOT overridden — explicit env wins.
    """
    env_file = next((p for p in _env_candidates() if p.exists()), None)
    if env_file is None:
        log.debug("No .env found in any candidate location — skipping")
        return False

    try:
        from dotenv import load_dotenv
    except ImportError:
        log.warning(
            "python-dotenv not installed; cannot auto-load %s. "
            "Install with: pip install python-dotenv",
            env_file,
        )
        return False

    # override=False: environment variables set externally take precedence
    loaded = load_dotenv(env_file, override=False)
    if loaded:
        log.info("Loaded .env from %s", env_file)
    return loaded


def load_multiplai_conf() -> dict:
    """Load multiplai.conf with optional INI-style section support.

    Returns a dict with global keys at the top level, plus a ``_sections``
    dict for per-skill overrides::

        {"MULTIPLAI_MODEL": "claude-sonnet-4-6", "_sections": {"deep-research": {"MODEL": "opus"}}}
    """
    import os
    import re

    # multiplai.conf lives at the kit project root ($CLAUDE_MULTIPLAI_HOME)
    multiplai_home = os.environ.get("CLAUDE_MULTIPLAI_HOME")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if multiplai_home:
        conf_path = Path(multiplai_home) / "multiplai.conf"
    elif config_dir:
        conf_path = Path(config_dir).parent / "multiplai.conf"
    else:
        root = find_project_root()
        if root:
            conf_path = root / "multiplai.conf"
        else:
            return {"_sections": {}}

    if not conf_path.exists():
        log.debug("No multiplai.conf at %s", conf_path)
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
    log.info("Loaded multiplai.conf: %s", {k: v for k, v in result.items() if "KEY" not in k and k != "_sections"})
    return result


# --- Model + effort resolver (mirrors hooks/model_resolver.py) ---

_TIERS = {"haiku": 1, "sonnet": 2, "opus": 3}
_EFFORT_TIERS = {"low": 1, "medium": 2, "high": 3, "max": 4}


def _tier(model: str) -> int:
    model_lower = model.lower()
    for name, rank in _TIERS.items():
        if name in model_lower:
            return rank
    return 2  # default to sonnet


def resolve_model(requested: str, ceiling: str | None = None) -> str:
    """Return the requested model, or the ceiling if requested is above it.

    If ceiling is None, reads MULTIPLAI_MODEL from env (set by load_multiplai_conf
    or run-hook-python). Defaults to sonnet if unset.
    """
    import os

    if ceiling is None:
        ceiling = os.environ.get("MULTIPLAI_MODEL", "claude-sonnet-4-6")
    ceiling_tier = _tier(ceiling)
    requested_tier = _tier(requested)
    if requested_tier > ceiling_tier:
        log.info("Model ceiling: %s → %s (ceiling=%s)", requested, ceiling, ceiling)
        return ceiling
    return requested


def _effort_tier(effort: str) -> int:
    return _EFFORT_TIERS.get(effort.lower(), 3)


def resolve_effort(requested: str, ceiling: str | None = None) -> str:
    """Return the requested effort, or the ceiling if requested is above it."""
    import os

    if ceiling is None:
        ceiling = os.environ.get("MULTIPLAI_EFFORT", "high")
    if _effort_tier(requested) > _effort_tier(ceiling):
        log.info("Effort ceiling: %s → %s (ceiling=%s)", requested, ceiling, ceiling)
        return ceiling
    return requested
