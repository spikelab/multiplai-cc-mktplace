"""Drift-guard: the multiplai-core pin must be consistent everywhere.

The plugin declares its multiplai-core dependency in two places:

  1. Every runtime script's PEP 723 inline metadata (``# /// script``),
     which ``uv run`` uses to build the ephemeral env at hook time.
  2. ``requirements-dev.txt``, which the test suite installs so pytest can
     import ``multiplai_core`` directly.

If these drift, the tests exercise a *different* core version than the
runtime fetches — exactly the class of silent bug the 2026-07-08 review
flagged (requirements-dev pinned an older tag than the scripts). These
tests fail loudly the moment any pin diverges.
"""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
REQUIREMENTS_DEV = PLUGIN_ROOT / "requirements-dev.txt"

# Matches the version ref immediately after the repo name in the git URL,
# e.g. ".../multiplai-core@v0.6.0" -> "v0.6.0". Tolerates the optional
# extras marker (multiplai-core[sdk]@...) because the match anchors on the
# URL path segment, not the requirement name.
_CORE_PIN_RE = re.compile(r"multiplai-core@(v\d+\.\d+\.\d+)")


def _core_pins_in(text: str) -> list[str]:
    """All multiplai-core git-ref pins found in *text*."""
    return _CORE_PIN_RE.findall(text)


def _pep723_core_pins() -> dict[Path, str]:
    """Map each script that pins multiplai-core to its pinned version.

    Only considers the PEP 723 metadata header (the commented ``# ///
    script`` block at the top), which is what ``uv run`` reads.
    """
    pins: dict[Path, str] = {}
    for script in sorted(SCRIPTS_DIR.rglob("*.py")):
        text = script.read_text(encoding="utf-8")
        found = _core_pins_in(text)
        if found:
            # A single script should pin exactly one core version.
            assert len(set(found)) == 1, (
                f"{script.relative_to(PLUGIN_ROOT)} pins multiplai-core at "
                f"multiple versions: {sorted(set(found))}"
            )
            pins[script] = found[0]
    return pins


def test_scripts_actually_pin_core():
    """Sanity: the scan finds core pins (guards against a broken regex)."""
    pins = _pep723_core_pins()
    assert pins, (
        "Expected at least one script to pin multiplai-core via PEP 723 "
        "metadata; found none — the pin scan is likely broken"
    )


def test_all_pep723_core_pins_are_consistent():
    """Every script's PEP 723 multiplai-core pin must be the same version."""
    pins = _pep723_core_pins()
    distinct = sorted(set(pins.values()))
    assert len(distinct) == 1, (
        "multiplai-core PEP 723 pins have drifted across scripts: "
        + ", ".join(
            f"{p.relative_to(PLUGIN_ROOT)}={v}" for p, v in sorted(pins.items())
        )
    )


def test_requirements_dev_matches_pep723_pin():
    """requirements-dev.txt must pin the same core version as the scripts.

    This is the drift the review caught: requirements-dev lagged the
    scripts, so the suite tested an older core than the runtime used.
    """
    req_text = REQUIREMENTS_DEV.read_text(encoding="utf-8")
    req_pins = _core_pins_in(req_text)
    assert req_pins, (
        "requirements-dev.txt must pin multiplai-core to a git ref "
        "(multiplai-core @ git+...@vX.Y.Z)"
    )
    assert len(set(req_pins)) == 1, (
        f"requirements-dev.txt pins multiplai-core at multiple versions: "
        f"{sorted(set(req_pins))}"
    )

    script_pins = set(_pep723_core_pins().values())
    assert len(script_pins) == 1, (
        "Cannot compare: script PEP 723 pins are themselves inconsistent "
        f"({sorted(script_pins)})"
    )

    req_pin = req_pins[0]
    script_pin = next(iter(script_pins))
    assert req_pin == script_pin, (
        f"multiplai-core pin drift: requirements-dev.txt pins {req_pin} but "
        f"the scripts' PEP 723 metadata pins {script_pin}. Bump them together."
    )
