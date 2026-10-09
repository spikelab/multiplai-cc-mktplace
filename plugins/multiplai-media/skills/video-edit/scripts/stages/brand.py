"""Brand file: fonts, colours and logo that captions, headline, background and
logo read.

    {
      "font_file": "fonts/Brand-Regular.ttf",
      "font_bold_file": "fonts/Brand-Bold.ttf",
      "primary": "#FFFFFF",          caption text
      "accent": "#FFD400",           the word being spoken; the headline box
      "background": "#0A0A0A",       behind stacked panels and padding
      "caption_outline": "#000000",
      "logo": {"path": "logo.png", "position": "tl", "scale": 0.16}
    }

Relative paths resolve against the brand file's directory. Every key is
optional; a missing one keeps the default, which is also what a render with no
brand file uses: the title-card fonts, white captions, a yellow highlight and
no logo.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
POSITIONS = ("tl", "tr", "bl", "br")


@dataclass
class BrandLogo:
    path: str
    position: str = "tl"
    scale: float = 0.16         # fraction of output width


@dataclass
class Brand:
    font_file: Optional[str] = None
    font_bold_file: Optional[str] = None
    primary: str = "#FFFFFF"
    accent: str = "#FFD400"
    background: str = "#0a0a0a"
    caption_outline: str = "#000000"
    logo: Optional[BrandLogo] = None

    def caption_font(self) -> Optional[str]:
        """The font captions and the headline use, when the brand names one."""
        return self.font_bold_file or self.font_file


def default() -> Brand:
    return Brand()


def load(path: str | Path) -> Brand:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"brand file not found: {p}")
    data = json.loads(p.read_text())
    unknown = set(data) - {"font_file", "font_bold_file", "primary", "accent", "background",
                           "caption_outline", "logo"}
    if unknown:
        raise ValueError(f"{p}: unknown brand keys: {', '.join(sorted(unknown))}")

    def resolve(rel: str, what: str) -> str:
        f = (p.parent / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        if not f.exists():
            raise FileNotFoundError(f"{p}: {what} not found: {f}")
        return str(f)

    b = Brand()
    for key in ("font_file", "font_bold_file"):
        if data.get(key):
            setattr(b, key, resolve(data[key], key))
    for key in ("primary", "accent", "background", "caption_outline"):
        if key in data:
            if not _HEX.match(str(data[key])):
                raise ValueError(f"{p}: {key} must be a #RRGGBB colour, got {data[key]!r}")
            setattr(b, key, data[key])
    if data.get("logo"):
        lg = dict(data["logo"])
        lg["path"] = resolve(lg["path"], "logo")
        b.logo = BrandLogo(**lg)
        if b.logo.position not in POSITIONS:
            raise ValueError(f"{p}: logo.position must be one of {', '.join(POSITIONS)}")
    return b


def ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB → ASS &HAABBGGRR (alpha 0 = opaque)."""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()
