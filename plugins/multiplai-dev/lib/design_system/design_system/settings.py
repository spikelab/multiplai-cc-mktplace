"""Which design-system folder a page uses.

Two settings, each read from the environment first and then from `multiplai.conf`:

- `MULTIPLAI_DESIGN_SYSTEMS`: an absolute path (`~` allowed) to the folder that holds one subfolder per
  design system.
- `MULTIPLAI_DESIGN_SYSTEM`: the default subfolder's name.

With no name, the neutral theme shipped in this plugin is used. A name that does not exist is an error
listing the names that do; it never falls back to neutral.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DIR_KEY = "MULTIPLAI_DESIGN_SYSTEMS"
NAME_KEY = "MULTIPLAI_DESIGN_SYSTEM"
NEUTRAL_NAME = "neutral"

# lib/design_system/design_system/settings.py → the plugin root is three levels above the package.
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
NEUTRAL = PLUGIN_ROOT / "skills" / "design-system" / "assets" / NEUTRAL_NAME


class SettingsError(Exception):
    """A setting is malformed, or names a design system that does not exist."""


def _conf() -> Mapping[str, object]:
    from multiplai_core.env import load_multiplai_conf

    return load_multiplai_conf()


def _setting(
    key: str, env: Mapping[str, str] | None, conf: Mapping[str, object] | None
) -> str:
    env = os.environ if env is None else env
    value = env.get(key, "").strip()
    if value:
        return value
    conf = _conf() if conf is None else conf
    found = conf.get(key)
    return found.strip() if isinstance(found, str) else ""


def systems_dir(
    env: Mapping[str, str] | None = None, conf: Mapping[str, object] | None = None
) -> Path | None:
    """The folder of design systems, or None when unset. Raises SettingsError for a relative path."""
    raw = _setting(DIR_KEY, env, conf)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SettingsError(
            f"{DIR_KEY} must be an absolute path (~ allowed), got {raw!r}"
        )
    return path


def default_name(
    env: Mapping[str, str] | None = None, conf: Mapping[str, object] | None = None
) -> str:
    """The default design system's name; empty when unset."""
    return _setting(NAME_KEY, env, conf)


def names(
    env: Mapping[str, str] | None = None, conf: Mapping[str, object] | None = None
) -> list[str]:
    """`neutral`, then every subfolder of the design-systems folder that holds a `tokens.css`."""
    d = systems_dir(env, conf)
    found = (
        sorted(p.name for p in d.iterdir() if (p / "tokens.css").is_file())
        if d and d.is_dir()
        else []
    )
    return [NEUTRAL_NAME, *(n for n in found if n != NEUTRAL_NAME)]


def resolve(
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    conf: Mapping[str, object] | None = None,
) -> Path:
    """The folder of design system *name*, else of the default name, else neutral. Raises SettingsError."""
    chosen = (name or "").strip() or default_name(env, conf)
    d = systems_dir(env, conf)
    if not chosen:
        return NEUTRAL
    if d is not None and (d / chosen / "tokens.css").is_file():
        return d / chosen
    if chosen == NEUTRAL_NAME:
        return NEUTRAL
    if d is None:
        raise SettingsError(
            f"design system {chosen!r} was asked for, but {DIR_KEY} is not set; "
            f"set it to the absolute path of the folder that holds {chosen}/"
        )
    raise SettingsError(
        f"no design system {chosen!r} in {d} (a folder with a tokens.css); available: {', '.join(names(env, conf))}"
    )
