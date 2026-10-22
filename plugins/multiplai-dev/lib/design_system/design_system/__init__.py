"""design_system: resolves and checks the folder of design tokens an HTML page is styled with.

Shared by the design-system skill's CLI and by the skills that render pages (codebase-walkthrough).
"""

from .settings import NEUTRAL, SettingsError, default_name, names, resolve, systems_dir
from .tokens import (
    COLOURS,
    CONTRAST_PAIRS,
    REQUIRED,
    SHAPE,
    check_folder,
    contrast,
    parse,
    ratios,
)

__all__ = [
    "COLOURS",
    "CONTRAST_PAIRS",
    "NEUTRAL",
    "REQUIRED",
    "SHAPE",
    "SettingsError",
    "check_folder",
    "contrast",
    "default_name",
    "names",
    "parse",
    "ratios",
    "resolve",
    "systems_dir",
]
