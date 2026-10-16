"""The reasoning effort each background job asks for, one plugin option per job.

Every background job in this plugin (the memory router, catalog generation,
the checkpoint writer, dream, learning extraction, the now/ refresh, the
utilisation judge, the memory doctor) has its own ``<job>_effort`` option.
Empty, the shipped default, sends no effort: multiplai-core's ``run_agent``
then uses ``MULTIPLAI_EFFORT`` (environment, then ``multiplai.conf``), and
failing that the effort of the Claude Code session the job runs under.

A value that is not an effort name (``low``, ``medium``, ``high``, ``xhigh``,
``max``) logs a warning and is ignored, so the job falls back the same way.

Callers splat :func:`effort_kwargs` into the model call, which omits the
keyword when nothing is configured, as :mod:`lib.thinking` does for thinking.
"""

from __future__ import annotations

import logging

from multiplai_core.env import KNOWN_EFFORTS
from multiplai_core.plugin_options import option

logger = logging.getLogger(__name__)

ROUTER_EFFORT_OPTION = "router_effort"
CATALOG_EFFORT_OPTION = "catalog_effort"
CHECKPOINT_EFFORT_OPTION = "checkpoint_effort"
DREAM_EFFORT_OPTION = "dream_effort"
EXTRACTION_EFFORT_OPTION = "extraction_effort"
NOW_EFFORT_OPTION = "now_effort"
UTILISATION_JUDGE_EFFORT_OPTION = "utilisation_judge_effort"
MEMORY_DOCTOR_EFFORT_OPTION = "memory_doctor_effort"

EFFORT_OPTIONS = (
    ROUTER_EFFORT_OPTION, CATALOG_EFFORT_OPTION, CHECKPOINT_EFFORT_OPTION,
    DREAM_EFFORT_OPTION, EXTRACTION_EFFORT_OPTION, NOW_EFFORT_OPTION,
    UTILISATION_JUDGE_EFFORT_OPTION, MEMORY_DOCTOR_EFFORT_OPTION,
)


def job_effort(name: str) -> str | None:
    """The effort set in option *name*, or ``None`` when unset or invalid."""
    value = option(name).strip().lower()
    if not value:
        return None
    if value not in KNOWN_EFFORTS:
        logger.warning("Plugin option %s=%r is not an effort (%s); ignoring it",
                       name, value, ", ".join(sorted(KNOWN_EFFORTS)))
        return None
    return value


def effort_kwargs(name: str) -> dict[str, str]:
    """``{"effort": value}`` when option *name* sets an effort, else ``{}``."""
    value = job_effort(name)
    return {"effort": value} if value else {}
