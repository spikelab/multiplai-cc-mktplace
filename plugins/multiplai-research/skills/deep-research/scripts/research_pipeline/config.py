"""ResearchConfig and preset definitions.

Presets control depth/breadth of research. CLI args override preset defaults.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

SummaryLevel = Literal["gist", "structured", "detailed"]
Preset = Literal["micro", "quick", "standard", "thorough"]
ResearchType = Literal["general", "company", "job-market", "fact-check", "theme"]

from .env import load_multiplai_conf, pick_model, resolve_effort

log = logging.getLogger(__name__)

# Reasoning nodes run opus (hard work); the high-volume per-source parse nodes
# (triage, extract) run sonnet (cheap bulk work). Both are resolved from a
# semantic tier via pick_model and capped by the MULTIPLAI_MODEL ceiling, so a
# sonnet ceiling still forces every node to sonnet. No dated model literal here —
# the family→ID map is the single source of truth in multiplai_core.env. Retune
# per task in multiplai.conf: [deep-research] / [deep-research.parse] MODEL=...
DEFAULT_MODEL = pick_model("opus", task="deep-research")
PARSE_MODEL = pick_model("sonnet", task="deep-research.parse")


def _conf_sections() -> Mapping[str, Mapping[str, str]]:
    """The ``_sections`` half of multiplai.conf, defensively unwrapped."""
    return load_multiplai_conf().get("_sections", {}) or {}


def _conf_section_value(
    task: str,
    key: str,
    sections: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """One normalized ``KEY=`` value from the ``[task]`` section, or ``""``.

    The same lookup was written out once per tuning axis; this is it once.
    ``sections`` lets a caller reuse a conf it has already loaded — the loader
    re-reads and re-parses the file on **every** call (no cache, deliberately:
    the tests point ``CLAUDE_MULTIPLAI_HOME`` at a tmp dir per test), so the
    per-node maps below would otherwise pay one file read per node per axis.
    """
    if sections is None:
        sections = _conf_sections()
    section = sections.get(task) or {}
    return (section.get(key) or "").strip().lower()


def conf_effort(
    task: str,
    default: str | None = None,
    *,
    sections: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """``EFFORT=`` for *task* from multiplai.conf, capped by MULTIPLAI_EFFORT.

    Model and effort are two axes of the same tuning decision, and only the
    model half was configurable without a code edit — a node could be retuned
    to sonnet from the conf file but not dialled down to `low` thinking. This
    is the missing half: ``[deep-research.extract] EFFORT=low``.

    Returns *default* when the conf says nothing, so every existing per-node
    default below is unchanged unless someone opts in.
    """
    requested = _conf_section_value(task, "EFFORT", sections)
    if not requested:
        return default
    # The ceiling exists so a budget run can force every node down; it must
    # apply to a conf override exactly as it applies to a code default.
    return resolve_effort(requested)


def _node_effort(
    node: str,
    default: str | None,
    *,
    sections: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """Per-node effort: the node's own conf section wins over the skill-wide
    one, which wins over the code default."""
    return conf_effort(
        f"deep-research.{node}",
        conf_effort("deep-research", default, sections=sections),
        sections=sections,
    )


# Extended thinking is ON by default in the Agent SDK, and effort does not
# remove its latency: a cold no-tools call measured 18.4s → 2.9s with
# thinking={"type": "disabled"} (2026-08-09). Mechanical parse/search nodes
# disable it below; reasoning nodes keep the SDK default (None).
#
# Read-only, for the reason multiplai_core.env freezes EFFORT_TIERS: it is one
# shared default behind every node, and a caller that mutated it would retune
# the whole pipeline from a distance. Every path that reaches the SDK copies it
# into a real dict first — ClaudeAgentOptions wants a plain mapping, not a
# proxy — which is what the `dict(...)` calls below are for.
THINKING_DISABLED: Final[Mapping[str, str]] = MappingProxyType({"type": "disabled"})

# Conf values that restore the SDK default (thinking back on), and those that
# turn it off. Anything else is a typo rather than a third option — see
# conf_thinking, which ignores it instead of guessing.
_THINKING_ON_VALUES = ("1", "true", "yes", "on", "enabled")
_THINKING_OFF_VALUES = ("0", "false", "no", "off", "disabled")

# (task, value) pairs already warned about, so a bad skill-wide THINKING= is
# one log line and not one per node.
_warned_thinking_values: set[tuple[str, str]] = set()


def conf_thinking(
    task: str,
    default: dict | Mapping[str, str] | None = None,
    *,
    sections: Mapping[str, Mapping[str, str]] | None = None,
) -> dict | None:
    """``THINKING=`` for *task* from multiplai.conf.

    The efforts map got its conf half in ``conf_effort``; this is the same
    mechanic for the thinking axis. ``THINKING=on`` restores the SDK default
    (returns ``None``); ``THINKING=off`` disables thinking. An unrecognized
    value is **ignored with a warning**, not treated as "off" — a typo must
    not be able to silently strip extended thinking from the reasoning nodes,
    which is the failure mode that guessing would produce. This matches
    ``multiplai_core.env._normalize_effort``, which drops an unrecognized
    ``EFFORT=`` rather than acting on it.

    Returns *default* when the conf says nothing, so every per-node default
    below is unchanged unless someone opts in.
    """
    requested = _conf_section_value(task, "THINKING", sections)
    # Fresh copy per node — callers must never share one mutable dict, and the
    # shared default is a read-only proxy the SDK would not accept anyway.
    fallback = dict(default) if default is not None else None
    if not requested:
        return fallback
    if requested in _THINKING_ON_VALUES:
        return None
    if requested in _THINKING_OFF_VALUES:
        return dict(THINKING_DISABLED)
    if (task, requested) not in _warned_thinking_values:
        _warned_thinking_values.add((task, requested))
        log.warning(
            "multiplai.conf [%s] THINKING=%s is not a recognized value; "
            "ignoring it and keeping this node's default. Use %s to restore "
            "extended thinking, or %s to disable it.",
            task, requested,
            "/".join(_THINKING_ON_VALUES), "/".join(_THINKING_OFF_VALUES),
        )
    return fallback


def _node_thinking(
    node: str,
    default: dict | Mapping[str, str] | None,
    *,
    sections: Mapping[str, Mapping[str, str]] | None = None,
) -> dict | None:
    """Per-node thinking: the node's own conf section wins over the skill-wide
    one, which wins over the code default."""
    return conf_thinking(
        f"deep-research.{node}",
        conf_thinking("deep-research", default, sections=sections),
        sections=sections,
    )


# Per-node reasoning effort: mechanical parse/search work runs "low", the
# quality gate "medium", reasoning nodes None (SDK default).
_EFFORT_DEFAULTS: tuple[tuple[str, str | None], ...] = (
    ("plan", None),
    ("diverge", None),
    ("challenge", None),
    ("search", "low"),
    ("triage_relevance", "low"),
    ("extract", "low"),
    ("verify", "low"),
    ("reassess", None),
    ("synthesize", None),
    ("adversarial", None),
    ("quality_check", "medium"),
)

# Per-node extended thinking: mechanical parse/search work runs with thinking
# disabled (the SDK default costs ~15s per call and buys nothing on
# formatting/parsing); reasoning nodes keep the SDK default (None).
#
# Worth saying out loud, because it is not a coincidence: the four nodes that
# lose extended thinking here are also the four that ingest attacker-authored
# text — `search` parses model output built from search results,
# `triage_relevance` scores third-party titles and snippets, `extract` and the
# fetch it feeds handle raw page content. What bounds that risk is not the
# thinking budget but the fail-closed deny-list in `sdk._deny_list` (every tool
# outside the call's allow-list is explicitly denied under bypassPermissions)
# plus the `defang_untrusted` fencing at the call sites. Re-enable per node
# with `[deep-research.<node>] THINKING=on` if a future prompt change makes a
# node's reasoning load-bearing.
_THINKING_DEFAULTS: tuple[tuple[str, Mapping[str, str] | None], ...] = (
    ("plan", None),
    ("diverge", None),
    ("challenge", None),
    ("search", THINKING_DISABLED),
    ("triage_relevance", THINKING_DISABLED),
    ("extract", THINKING_DISABLED),
    ("verify", THINKING_DISABLED),
    ("reassess", None),
    ("synthesize", None),
    ("adversarial", None),
    ("quality_check", None),
)


def _default_efforts() -> dict[str, str | None]:
    """Build the per-node effort map from a single conf read."""
    sections = _conf_sections()
    return {
        node: _node_effort(node, default, sections=sections)
        for node, default in _EFFORT_DEFAULTS
    }


def _default_thinkings() -> dict[str, dict | None]:
    """Build the per-node thinking map from a single conf read."""
    sections = _conf_sections()
    return {
        node: _node_thinking(node, default, sections=sections)
        for node, default in _THINKING_DEFAULTS
    }


@dataclass
class PresetConfig:
    name: Preset
    sources: int  # sources to read
    max_total_fetches: int  # hard cap on fetches (sources + link-follows)
    link_depth: int  # 0, 1, or 2
    max_sub_pages: int  # max links followed per source
    follow_links: bool
    summary_level: SummaryLevel
    min_sources: int  # minimum sources that must survive triage
    max_sub_questions: int
    max_reassess_findings: int = 80  # cap for REASSESS context budget


PRESETS: dict[Preset, PresetConfig] = {
    "micro": PresetConfig(
        name="micro",
        sources=3,
        max_total_fetches=3,
        link_depth=0,
        max_sub_pages=0,
        follow_links=False,
        summary_level="gist",
        min_sources=1,
        max_sub_questions=2,
        max_reassess_findings=20,
    ),
    "quick": PresetConfig(
        name="quick",
        sources=10,
        max_total_fetches=15,
        link_depth=0,
        max_sub_pages=0,
        follow_links=False,
        summary_level="gist",
        min_sources=5,
        max_sub_questions=3,
        max_reassess_findings=50,
    ),
    "standard": PresetConfig(
        name="standard",
        sources=20,
        max_total_fetches=40,
        link_depth=1,
        max_sub_pages=3,
        follow_links=True,
        summary_level="structured",
        min_sources=10,
        max_sub_questions=3,
        max_reassess_findings=80,
    ),
    "thorough": PresetConfig(
        name="thorough",
        sources=30,
        max_total_fetches=60,
        link_depth=2,
        max_sub_pages=3,
        follow_links=True,
        summary_level="detailed",
        min_sources=15,
        max_sub_questions=5,
        max_reassess_findings=100,
    ),
}


@dataclass
class ResearchConfig:
    """Complete configuration for a single research run."""

    query: str
    output_dir: Path
    preset: PresetConfig
    research_type: ResearchType = "general"
    date: str = ""
    auto: bool = False
    parallel: bool = False
    agents: int | None = None
    deep: bool = False
    challenge: bool = False
    no_challenge: bool = False
    no_memory: bool = False

    # Context injected by SKILL.md wrapper
    personal_context: str = ""
    prior_knowledge: str = ""

    # Plan review flow
    plan_only: bool = False
    approved_plan: Path | None = None

    # Claude Agent tools (WebSearch/WebFetch via SDK)
    prefer_claude_tools: bool = True  # use SDK for search/fetch by default
    allow_paid_fallback: bool = False  # if Claude Agent fails, don't auto-fallback to paid APIs

    # Session tracking
    session_id: str = ""  # Claude Code session ID for log correlation

    # Effort level for all SDK calls (low/medium/high). None = SDK default.
    # Kept for record/CLI parity — call sites read the per-node `efforts` map.
    effort: str | None = None

    # Extended thinking for all SDK calls ("on"/"off"). None = per-node
    # defaults. Kept for record/CLI parity — call sites read `thinkings`.
    thinking: str | None = None

    # Per-node model tiers: opus for reasoning, sonnet for the high-volume
    # per-source parse nodes (triage, extract). `--model` overrides all nodes.
    models: dict[str, str] = field(
        default_factory=lambda: {
            "plan": DEFAULT_MODEL,
            "diverge": DEFAULT_MODEL,
            "challenge": DEFAULT_MODEL,
            "search": PARSE_MODEL,
            "triage_relevance": PARSE_MODEL,
            "extract": PARSE_MODEL,
            "verify": PARSE_MODEL,
            "reassess": DEFAULT_MODEL,
            "synthesize": DEFAULT_MODEL,
            "adversarial": DEFAULT_MODEL,
            "quality_check": PARSE_MODEL,
        }
    )

    # Per-node reasoning effort (defaults + rationale: _EFFORT_DEFAULTS).
    # `--effort` overrides all nodes, mirroring `--model`.
    efforts: dict[str, str | None] = field(default_factory=_default_efforts)

    # Per-node extended thinking (defaults + rationale: _THINKING_DEFAULTS).
    # Conf-overridable per node or skill-wide, mirroring EFFORT:
    # [deep-research.search] THINKING=on / [deep-research] THINKING=on.
    # `--thinking on|off` overrides all nodes, mirroring `--effort`.
    thinkings: dict[str, dict | None] = field(default_factory=_default_thinkings)

    @classmethod
    def from_cli_args(cls, args: argparse.Namespace) -> "ResearchConfig":
        """Build ResearchConfig from parsed CLI arguments."""
        preset = PRESETS[args.preset]
        config = cls(
            query=args.query,
            output_dir=Path(args.output),
            preset=preset,
            research_type=args.research_type,
            date=args.date,
            auto=args.auto,
            parallel=args.parallel,
            agents=args.agents,
            deep=args.deep,
            challenge=args.challenge,
            no_challenge=args.no_challenge,
            no_memory=args.no_memory,
            personal_context=args.personal_context,
            prior_knowledge=args.prior_knowledge,
            plan_only=args.plan_only,
            approved_plan=args.approved_plan,
            prefer_claude_tools=not getattr(args, "no_claude_tools", False),
            allow_paid_fallback=getattr(args, "allow_paid_fallback", False),
            session_id=getattr(args, "session_id", "") or "",
            effort=getattr(args, "effort", None),
            thinking=getattr(args, "thinking", None),
        )
        # Global model override — bypasses ceiling, sets all nodes to same model
        model_override = getattr(args, "model", None)
        if model_override:
            config.models = {k: model_override for k in config.models}
        # Global effort override — mirrors --model, sets all nodes to same effort
        if config.effort:
            config.efforts = {k: config.effort for k in config.efforts}
        # Global thinking override — mirrors --effort. This is the per-run
        # escape hatch from the disabled-by-default mechanical nodes; without
        # it the only way back was editing multiplai.conf, which is a
        # machine-level file and not a property of one run. Fresh dict per
        # node, same as the default map.
        if config.thinking:
            config.thinkings = {
                k: (None if config.thinking == "on" else dict(THINKING_DISABLED))
                for k in config.thinkings
            }
        return config

    @property
    def challenge_enabled(self) -> bool:
        """Whether adversarial review runs after synthesis."""
        if self.no_challenge:
            return False
        if self.challenge:
            return True
        # Auto-trigger on thorough
        return self.preset.name == "thorough"

    def per_agent_preset(self) -> Preset:
        """Downscaled preset for parallel sub-agents."""
        if self.deep:
            return self.preset.name
        downscale = {
            "micro": "micro",
            "quick": "quick",  # quick stays quick (no parallel anyway)
            "standard": "quick",
            "thorough": "standard",
        }
        return downscale[self.preset.name]  # type: ignore[return-value]

    def query_slug(self) -> str:
        """URL-safe slug from the query for filenames."""
        import re
        slug = re.sub(r"[^\w\s-]", "", self.query.lower())
        slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
        return slug[:80]  # limit length

    def output_file_path(self) -> Path:
        return self.output_dir / f"{self.query_slug()}-{self.date}.md"

    def state_file_path(self) -> Path:
        return self.output_dir / f"{self.query_slug()}-{self.date}-state.json"

    def progress_file_path(self) -> Path:
        return self.output_dir / f"{self.query_slug()}-{self.date}-progress.md"
