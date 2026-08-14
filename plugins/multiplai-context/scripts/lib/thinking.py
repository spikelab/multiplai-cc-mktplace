"""Thinking-config resolution for the plugin's mechanical model calls.

Extended thinking is OFF by default at every mechanical call site in this
plugin — extraction, checkpoint writing, the memory doctor's duplication and
contradiction checks, the utilisation judge, now-summaries, and catalog
generation. Measured 2026-08-09 (see ``lib/memory_router.py``, where the
pattern shipped first): a cold no-tools SDK call takes 18.4s with thinking on
and 2.9s with it disabled. These calls are parse/classify/summarise work over
input the model can see in full — exactly the shape of task that does not need
deliberation — so disabling thinking is a latency win, not a quality trade.
Reasoning-heavy calls (dream proposals) deliberately do not use this module.

Each call site names its own plugin option, so any one subsystem can be
individually opted back to the SDK default (extended thinking on) without a
code change: set the option to ``1``/``true``/``yes``/``on``/``enabled``.

Old-core tolerance: ``thinking=`` landed in multiplai-core 0.14.0 on both call
paths this plugin uses (``ModelClient.query`` and ``run_agent``). An older
core rejects the keyword *name*, whatever its value, so a caller must omit the
keyword entirely rather than pass ``None`` — :func:`resolve_thinking` returns
``None`` (and warns once, naming the fix) when the resolved core cannot accept
it, and every call site sends the keyword only when the value is not ``None``.
"""

from __future__ import annotations

import logging

from multiplai_core.plugin_options import option

logger = logging.getLogger(__name__)

#: The Messages-API/Agent-SDK config that turns extended thinking off.
THINKING_DISABLED = {"type": "disabled"}

#: Option values that opt a call site back to the SDK default (thinking on).
TRUTHY_VALUES = ("1", "true", "yes", "on", "enabled")

#: The two core call paths this plugin sends model calls through. Probed
#: separately: they are different functions and could drift independently.
QUERY = "query"
RUN_AGENT = "run_agent"

# Option keys, one per mechanical subsystem. Bound to module constants in the
# ``*_OPTION = "<key>"`` form because that is what the wiring test scans for
# (tests/test_integration_wiring.py) — a key that only ever appears as a call
# argument elsewhere would read as dead config. ``doctor_thinking`` is shared
# by the duplication and contradiction passes: they are one subsystem (the
# memory doctor) from the user's point of view.
UTILISATION_THINKING_OPTION = "utilisation_thinking"
NOW_THINKING_OPTION = "now_thinking"
DOCTOR_THINKING_OPTION = "doctor_thinking"
EXTRACTION_THINKING_OPTION = "extraction_thinking"
CHECKPOINT_THINKING_OPTION = "checkpoint_thinking"
CATALOG_THINKING_OPTION = "catalog_thinking"


def probe_core_thinking(target: str = QUERY) -> bool:
    """Uncached signature probe: does the resolved core accept ``thinking=``?

    *target* is :data:`QUERY` (``ModelClient.query``) or :data:`RUN_AGENT`
    (``multiplai_core.agent_runner.run_agent``). Kept separate from the cached
    :func:`core_supports_thinking` so ``lib/memory_router.py`` can keep its own
    per-module cache semantics while sharing this implementation.
    """
    try:
        import inspect

        if target == RUN_AGENT:
            from multiplai_core.agent_runner import run_agent as fn
        else:
            from multiplai_core.model_client import ModelClient

            fn = ModelClient.query
        return "thinking" in inspect.signature(fn).parameters
    except Exception:
        # No core, no client, unreadable signature — the call paths have
        # their own guards for all three. Assume unsupported, stay quiet
        # here; resolve_thinking() owns the one loud warning.
        return False


_SUPPORT_CACHE: dict[str, bool] = {}


def core_supports_thinking(target: str = QUERY) -> bool:
    """Cached :func:`probe_core_thinking` — some callers run per prompt."""
    if target not in _SUPPORT_CACHE:
        _SUPPORT_CACHE[target] = probe_core_thinking(target)
    return _SUPPORT_CACHE[target]


def resolve_thinking_option(option_name: str, raw: str | None = None) -> dict | None:
    """Pure option read: disabled unless the option asks for thinking back.

    ``None`` is the "send nothing" signal that multiplai-core needs for
    old-SDK tolerance, so this returns the dict for the *default* case and
    ``None`` for the opt-back — the inverse of how boolean options usually
    read. No core-support guard here; :func:`resolve_thinking` adds it.
    """
    value = (raw if raw is not None else option(option_name)).strip().lower()
    if value in TRUTHY_VALUES:
        return None
    return THINKING_DISABLED


_WARNED_TARGETS: set[str] = set()


def resolve_thinking(option_name: str, *, target: str = QUERY) -> dict | None:
    """The ``thinking`` config for a mechanical call site, or ``None``.

    ``None`` means "do not send the keyword at all" and covers two cases the
    caller treats identically: the user opted this subsystem back to the SDK
    default, or the resolved core is too old to accept the keyword (warned
    once per *target*, naming the fix). Callers must omit the keyword when
    this returns ``None`` — an older core rejects the keyword name, whatever
    its value.

    Resolve once per run, outside any retry loop: the answer cannot change
    between retries, and re-reading the option there only hides that.
    """
    config = resolve_thinking_option(option_name)
    if config is None:
        return None
    if not core_supports_thinking(target):
        if target not in _WARNED_TARGETS:
            _WARNED_TARGETS.add(target)
            logger.warning(
                "Resolved multiplai-core does not accept thinking= on %s "
                "(needs >= 0.14.0), so these calls keep extended thinking on "
                "(~15s extra per cold call). Fix with `uv lock "
                "--upgrade-package multiplai-core` at the repo root, then "
                "commit the lock.",
                "run_agent" if target == RUN_AGENT else "ModelClient.query",
            )
        return None
    return config
