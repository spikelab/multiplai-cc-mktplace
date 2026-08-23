"""Thinking-config resolution for the plugin's mechanical model calls.

Extended thinking is OFF by default at the mechanical call sites in this
plugin — extraction, checkpoint writing, the memory doctor's duplication
confirmation, now-summaries, and catalog generation.
Measured 2026-08-09 (see ``lib/memory_router.py``, where the pattern shipped
first): a cold no-tools SDK call takes 18.4s with thinking on and 2.9s with it
disabled. These calls are parse/classify/summarise work over input the model can
see in full — exactly the shape of task that does not need deliberation — so
disabling thinking is a latency win, not a quality trade.

Calls that *do* reason keep thinking on: dream proposal generation and the
memory doctor's **contradiction** pass keep the SDK default and deliberately do
not use this module at all; the **utilisation judge** goes through this module
but pins ``default=True``, because its answer is a measurement and the setting
must be visible in code rather than inherited (see
:func:`resolve_thinking_option`). The contradiction pass reads as mechanical from the outside and is not —
it has to hold two statements side by side and decide whether they can both be
true, which is why ``doctor_contradiction`` gives it a 600s timeout against the
180s the duplication pass gets. multiplai-core's own note on this knob is the
rule being followed here: it "buys latency by giving up reasoning depth — do
not set it on work where the answer's quality matters more than its arrival
time."

Each call site names its own plugin option, so any one subsystem can be
individually opted back to the SDK default (extended thinking on) without a
code change. Values are parsed by ``multiplai_core.plugin_options.option_bool``,
the same reader every other boolean option in this plugin uses: ``true``/``1``/
``yes``/``on`` turn thinking back on, ``false``/``0``/``no``/``off`` leave it
off, and anything else logs a warning and falls back to the default.

**Old-dependency tolerance.** multiplai-core must accept ``thinking=`` on the
call path in use (``ModelClient.query`` or ``run_agent``); an older core rejects
the keyword *name*, whatever its value. :func:`resolve_thinking` yields ``None``
— "send no config at all" — when it cannot, with one warning per target naming
a fix an installed user can perform, and behaviour degrades to today's (thinking
on) rather than raising. Callers must **omit** the keyword rather than pass
``None``. Use :func:`thinking_kwargs` and splat it; that is the one place the
omit-or-send decision is made, and the only place worth testing it.

**A second boundary exists and is deliberately not checked here.** Core forwards
the value into ``claude_agent_sdk.ClaudeAgentOptions(**opts_kwargs)``, so an SDK
too old to have that field fails even against a current core — and
``create_client()`` prefers ``AgentSDKClient`` (zero-config), so both targets
reach the SDK in the normal configuration. This module cannot answer that
question: no script under ``scripts/`` may import ``claude_agent_sdk``
(``tests/test_integration_wiring.py`` → ``TestNoDirectSDKImportsAnywhere``; all
model access goes through core), and core exports no capability accessor for it.
Answering it belongs in core, which already imports the SDK. What core *does*
guarantee is that the mismatch arrives typed — ``ClaudeAgentOptions(**kwargs)``
raising ``TypeError`` is converted to ``AgentRunError``, and
``AgentSDKClient.query`` re-raises that as ``SDKQueryError`` — so every call site
here sees an ordinary model-call failure and takes its existing failure path,
rather than an unhandled ``TypeError``.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from multiplai_core.plugin_options import option_bool

logger = logging.getLogger(__name__)

#: The Messages-API/Agent-SDK config that turns extended thinking off.
THINKING_DISABLED = {"type": "disabled"}

#: The two core call paths this plugin sends model calls through. Probed
#: separately: they are different functions and could drift independently.
QUERY = "query"
RUN_AGENT = "run_agent"

# Option keys, one per mechanical subsystem. Bound to module constants in the
# ``*_OPTION = "<key>"`` form because that is what the wiring test scans for
# (tests/test_integration_wiring.py) — a key that only ever appears as a call
# argument elsewhere would read as dead config.
UTILISATION_THINKING_OPTION = "utilisation_thinking"
NOW_THINKING_OPTION = "now_thinking"
#: The memory doctor's *duplication* confirmation only. The contradiction pass
#: keeps the SDK default and has no switch, so this is deliberately not named
#: ``doctor_thinking``: an option that says "doctor" but moves one of the two
#: doctor passes is a trap for whoever reads it next.
DUPLICATION_THINKING_OPTION = "duplication_thinking"
EXTRACTION_THINKING_OPTION = "extraction_thinking"
CHECKPOINT_THINKING_OPTION = "checkpoint_thinking"
CATALOG_THINKING_OPTION = "catalog_thinking"

#: Named once so both warnings end with the same sentence, and so the fix stays
#: the one an *installed* user can perform. An installed plugin is a copy of the
#: plugin subtree with no workspace root above it — there is no repo root, no
#: `uv.lock`, and nothing to commit (docs/degradation-contract.md, rule 1).
_UNSUPPORTED_FIX = (
    "Extended thinking stays on for these calls, which costs ~15s each; "
    "nothing else is affected. Fix: update this plugin to a version with "
    "current dependencies (reinstall it from the marketplace)."
)


def _load_query() -> Any:
    from multiplai_core.model_client import ModelClient

    return ModelClient.query


def _load_run_agent() -> Any:
    from multiplai_core.agent_runner import run_agent

    return run_agent


#: target -> (name to print, importer). A dict rather than an if/else chain so
#: an unrecognised target cannot fall through to the ``query`` default and get
#: a True cached under a key nothing probed — :func:`probe_core_thinking`
#: raises on a miss instead.
_TARGETS: dict[str, tuple[str, Callable[[], Any]]] = {
    QUERY: ("ModelClient.query", _load_query),
    RUN_AGENT: ("run_agent", _load_run_agent),
}


def _probe(target: str) -> tuple[bool, str]:
    """``(supported, why_not)`` for *target*. ``why_not`` is "" when supported.

    Raises:
        ValueError: *target* is not one of :data:`QUERY` / :data:`RUN_AGENT`.
            Developer error in code, not user config — a typo that fell through
            to a default would probe one call path and hand the kwarg to
            another.
    """
    if target not in _TARGETS:
        raise ValueError(
            f"unknown thinking probe target {target!r}: "
            f"expected one of {sorted(_TARGETS)}"
        )
    label, load = _TARGETS[target]
    try:
        params = inspect.signature(load()).parameters
    except Exception as e:
        # No core, no client, unreadable signature — the call paths have their
        # own guards for all three, and core's own import error names the real
        # problem better than anything this could say.
        logger.debug("Could not introspect %s (%s); assuming unsupported", label, e)
        return False, ""
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        # A **kwargs callable swallows anything (test fakes included), and
        # there is no declared forwarding path left to probe.
        return True, ""
    if "thinking" not in params:
        return False, f"multiplai-core's {label} does not accept thinking=."
    return True, ""


def probe_core_thinking(target: str = QUERY) -> bool:
    """Uncached probe: does the resolved core accept ``thinking=`` on *target*?

    Silent by design; :func:`core_supports_thinking` owns the one warning per
    target, because its cache is what makes "once" true.
    """
    return _probe(target)[0]


_SUPPORT_CACHE: dict[str, bool] = {}


def core_supports_thinking(target: str = QUERY) -> bool:
    """Cached :func:`probe_core_thinking`, warning once per target on a miss.

    Some callers run per prompt (the memory router, on a blocking hook path),
    so the probe must not re-import and re-introspect every time.
    """
    if target not in _SUPPORT_CACHE:
        supported, why_not = _probe(target)
        _SUPPORT_CACHE[target] = supported
        if why_not:
            logger.warning("%s %s", why_not, _UNSUPPORTED_FIX)
    return _SUPPORT_CACHE[target]


def resolve_thinking_option(option_name: str, *, default: bool = False) -> dict | None:
    """Pure option read: disabled unless the option asks for thinking back.

    ``None`` is the "send nothing" signal that multiplai-core needs for
    old-dependency tolerance, so this returns the dict for the *disabled* case
    and ``None`` for thinking-on — the inverse of how boolean options usually
    read. No support guard here; :func:`resolve_thinking` adds it.

    ``default`` is the value used when the option is unset, and it exists for
    one reason: a call site whose *output is telemetry* must pin its own
    setting rather than inherit this module's. When ``lib/thinking.py`` shipped
    in 0.48.0 it flipped the utilisation judge from thinking-on (the SDK
    default — no thinking module existed) to thinking-off, and nothing about
    that call site changed in the diff, so the flip was invisible. Measured
    afterwards on a fixed 30-session subset with the prompt held constant:
    14.5% of sections credited with thinking off against 2.8% with it on. An
    instrument that silently changes sensitivity by 5x is not an instrument.
    So the judge passes ``default=True`` (see
    ``lib/utilisation_judge.JUDGE_THINKING_DEFAULT``) and every other call site
    keeps ``False``, which is the behaviour they already had.
    """
    return None if option_bool(option_name, default) else THINKING_DISABLED


def resolve_thinking(
    option_name: str, *, target: str = QUERY, default: bool = False
) -> dict | None:
    """The ``thinking`` config for a mechanical call site, or ``None``.

    ``None`` means "do not send the keyword at all" and covers two cases the
    caller treats identically: the user opted this subsystem back to the SDK
    default, or a resolved dependency cannot carry the keyword.

    Prefer :func:`thinking_kwargs` at call sites — it is what keeps the
    omit-or-send decision in one place. Resolve once per run, outside any retry
    loop: the answer cannot change between retries, and re-reading the option
    there only hides that.
    """
    config = resolve_thinking_option(option_name, default=default)
    if config is None:
        return None
    if not core_supports_thinking(target):
        return None
    return config


def thinking_kwargs(
    option_name: str, *, target: str = QUERY, default: bool = False
) -> dict:
    """``{"thinking": cfg}`` to splat into a model call, or ``{}``.

    The whole point of this module in one function. Every call site does::

        await client.query(system=..., messages=..., **thinking_kwargs(OPT))

    rather than hand-rolling ``if thinking is not None: kwargs["thinking"] =
    ...``, because that hand-rolled line is the invariant an old core depends
    on — it rejects the keyword *name*, whatever its value — and seven copies
    of it is seven chances to get it wrong in a way no test would catch.

    Returns a fresh dict each call, so no two model calls share one mutable
    thinking config on its way into the SDK.
    """
    config = resolve_thinking(option_name, target=target, default=default)
    return {} if config is None else {"thinking": dict(config)}
