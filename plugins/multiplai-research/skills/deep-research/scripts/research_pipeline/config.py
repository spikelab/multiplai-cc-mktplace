"""ResearchConfig and preset definitions.

Presets control depth/breadth of research. CLI args override preset defaults.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SummaryLevel = Literal["gist", "structured", "detailed"]
Preset = Literal["micro", "quick", "standard", "thorough"]
ResearchType = Literal["general", "company", "job-market", "fact-check", "theme"]

from .env import pick_model

# Reasoning nodes run opus (hard work); the high-volume per-source parse nodes
# (triage, extract) run sonnet (cheap bulk work). Both are resolved from a
# semantic tier via pick_model and capped by the MULTIPLAI_MODEL ceiling, so a
# sonnet ceiling still forces every node to sonnet. No dated model literal here —
# the family→ID map is the single source of truth in multiplai_core.env. Retune
# per task in multiplai.conf: [deep-research] / [deep-research.parse] MODEL=...
DEFAULT_MODEL = pick_model("opus", task="deep-research")
PARSE_MODEL = pick_model("sonnet", task="deep-research.parse")


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

    # Per-node reasoning effort: mechanical parse/search work runs "low",
    # the quality gate "medium", reasoning nodes None (SDK default).
    # `--effort` overrides all nodes, mirroring `--model`.
    efforts: dict[str, str | None] = field(
        default_factory=lambda: {
            "plan": None,
            "diverge": None,
            "challenge": None,
            "search": "low",
            "triage_relevance": "low",
            "extract": "low",
            "verify": "low",
            "reassess": None,
            "synthesize": None,
            "adversarial": None,
            "quality_check": "medium",
        }
    )

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
        )
        # Global model override — bypasses ceiling, sets all nodes to same model
        model_override = getattr(args, "model", None)
        if model_override:
            config.models = {k: model_override for k in config.models}
        # Global effort override — mirrors --model, sets all nodes to same effort
        if config.effort:
            config.efforts = {k: config.effort for k in config.efforts}
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
