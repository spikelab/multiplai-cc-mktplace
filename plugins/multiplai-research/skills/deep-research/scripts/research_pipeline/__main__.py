"""CLI entry point for the research pipeline.

Usage:
    python -m research_pipeline --query "..." [options]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date as date_cls
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research_pipeline",
        description="Deep research pipeline — code-driven research workflow",
    )
    parser.add_argument("--query", required=True, help="The research question")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd(),
        help="Directory to write the research output",
    )
    parser.add_argument(
        "--preset",
        choices=["micro", "quick", "standard", "thorough"],
        default="standard",
        help="Research preset (depth/breadth)",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Skip interactive plan review (non-interactive mode)",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run N parallel sub-topic pipelines and merge",
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=None,
        help="Number of parallel sub-agents (2-5) when --parallel",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="In parallel mode, run each sub-agent at parent preset without downscaling",
    )
    parser.add_argument(
        "--challenge",
        action="store_true",
        help="Force adversarial review after synthesis",
    )
    parser.add_argument(
        "--no-challenge",
        action="store_true",
        help="Skip adversarial review even on thorough preset",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Skip memory triage after research completes",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=date_cls.today().isoformat(),
        help="Override the research date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--research-type",
        choices=["general", "company", "job-market", "fact-check", "theme"],
        default="general",
        help="Research type for type-specific patterns",
    )
    parser.add_argument(
        "--personal-context",
        type=str,
        default="",
        help="Personal context from memory files (passed by SKILL.md)",
    )
    parser.add_argument(
        "--prior-knowledge",
        type=str,
        default="",
        help="Prior knowledge summary from workspace scan (passed by SKILL.md)",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Run PLAN/DIVERGE/CHALLENGE and output the plan as JSON, then exit",
    )
    parser.add_argument(
        "--approved-plan",
        type=Path,
        default=None,
        help="Path to a JSON plan file to use (skips PLAN/DIVERGE/CHALLENGE)",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default="",
        help="Claude Code session ID for log correlation",
    )
    parser.add_argument(
        "--no-claude-tools",
        action="store_true",
        help="Disable Claude Agent for search/fetch; use only external APIs (Tavily/Exa/etc.)",
    )
    parser.add_argument(
        "--allow-paid-fallback",
        action="store_true",
        help="If Claude Agent fails, silently fall back to paid external APIs",
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high"],
        default=None,
        help="SDK effort level for all LLM calls (controls thinking depth)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override model for ALL pipeline nodes (bypasses ceiling)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Build config from CLI args and run the full pipeline to completion.
    from research_pipeline.config import ResearchConfig
    from research_pipeline.pipeline import run_pipeline

    config = ResearchConfig.from_cli_args(args)
    return asyncio.run(run_pipeline(config))


if __name__ == "__main__":
    sys.exit(main())
