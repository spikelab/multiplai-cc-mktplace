"""Build pipeline configuration — presets, tier detection, config loading."""

from __future__ import annotations

import argparse
import os
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .env import load_multiplai_conf, resolve_model

log = logging.getLogger(__name__)

Tier = Literal["advanced", "standard"]
Mode = Literal["scratch", "brief", "only"]


def detect_tier() -> tuple[Tier, str]:
    """Detect model tier from the CLAUDE_MODEL environment variable.

    Returns (tier, model_name). Defaults to 'standard' if unset or unknown.

    KNOWN LIMITATION (verified 2026-07): Claude Code (v2.1.x) does NOT export
    CLAUDE_MODEL to Bash subprocesses — it exports CLAUDE_EFFORT but not
    CLAUDE_MODEL — and buildme's SKILL.md invokes this pipeline via a plain
    `uv run ...` with no `CLAUDE_MODEL=` prefix. So in production CLAUDE_MODEL is
    empty here and this ALWAYS returns 'standard', regardless of the skill's
    pinned model (claude-opus-4-7). The version-range logic below is correct in
    isolation and future-proofs the day the model is plumbed through, but the
    tier stays inert until the skill propagates the model into the environment
    (e.g. `CLAUDE_MODEL="{model}" uv run ...`) or the pipeline grows an explicit
    --tier/--model flag. Do not assume advanced tier runs today.
    """
    model = os.environ.get("CLAUDE_MODEL", "")
    if _is_advanced_model(model):
        return "advanced", model
    return "standard", model or "unknown"


def _is_advanced_model(model: str) -> bool:
    """Advanced tier = the Opus family at version >= 4.5.

    A version-range check rather than a literal allowlist, so the next Opus bump
    (4-7, 4-8, 5-0, ...) is recognized automatically instead of silently
    downgrading to 'standard'. Non-Opus models (sonnet/haiku/other) → False.
    """
    m = re.search(r"opus-(\d+)(?:-(\d+))?", model)
    if not m:
        return False
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    return (major, minor) >= (4, 5)


# Load model ceiling from multiplai.conf
_conf = load_multiplai_conf()
_MODEL_CEILING = _conf.get("MULTIPLAI_MODEL", "claude-sonnet-4-6")
DEFAULT_MODEL = resolve_model("claude-opus-4-6", ceiling=_MODEL_CEILING)


@dataclass
class GateToggles:
    """Per-gate on/off switches from config.yaml."""
    # RESERVED / not yet wired: no code path consults these two. The active
    # per-block review is an inline prompt in tdd_engine._run_quality_review;
    # there is no separate code-review or security-review gate to toggle yet.
    code_review_per_block: bool = True
    security_review_per_block: bool = True
    test_quality_enabled: bool = True
    e2e_test_entry_point_check: bool = True


@dataclass
class BuildConfig:
    """Complete configuration for a build pipeline run."""

    # Core
    mode: Mode = "scratch"
    project_dir: Path = field(default_factory=lambda: Path.cwd())
    change_name: str = ""
    tier: Tier = "standard"
    model_name: str = ""

    # Flags
    auto: bool = False
    spec_only: bool = False
    skip_research: bool = False

    # Project context (from specs/config.yaml)
    project_name: str = ""
    project_description: str = ""
    stack: str = ""
    test_command: str = ""

    # Memory files
    core_memory_files: list[str] = field(default_factory=lambda: ["technical-pref.md"])
    stack_memory_files: list[str] = field(default_factory=list)
    additional_memory_files: list[str] = field(default_factory=list)

    # Gate toggles
    gates: GateToggles = field(default_factory=GateToggles)

    # Paths (resolved after config load)
    config_dir: Path = field(default_factory=lambda: Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser())
    specs_dir: Path = field(default_factory=lambda: Path.cwd() / "specs")

    # Model for LLM calls
    model: str = DEFAULT_MODEL

    @classmethod
    def from_cli_args(cls, args: argparse.Namespace) -> BuildConfig:
        tier, model_name = detect_tier()
        config = cls(
            mode=getattr(args, "mode", "scratch"),
            project_dir=Path(args.project_dir),
            change_name=getattr(args, "change", "") or "",
            tier=tier,
            model_name=model_name,
            auto=getattr(args, "auto", False),
            spec_only=getattr(args, "spec_only", False),
            skip_research=getattr(args, "skip_research", False),
        )
        config.specs_dir = config.project_dir / "specs"
        config._load_specs_config()
        config._discover_test_command()
        log.info("Running in %s mode (%s)", tier, model_name)
        return config

    def _load_specs_config(self) -> None:
        """Load project context from specs/config.yaml if it exists."""
        config_path = self.specs_dir / "config.yaml"
        if not config_path.exists():
            return
        try:
            data = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError:
            log.warning("Failed to parse %s", config_path)
            return

        self.project_name = data.get("context", "").split("\n")[0] if data.get("context") else ""
        self.project_description = data.get("context", "")

        # Memory files
        mem = data.get("memory_files", {})
        self.core_memory_files = mem.get("core", ["technical-pref.md"])
        stacks = mem.get("stacks", {})
        if stacks:
            self.stack = next(iter(stacks))
            self.stack_memory_files = stacks[self.stack]
        self.additional_memory_files = mem.get("additional", [])

        # TDD config
        tdd = data.get("tdd", {})
        if tdd.get("test_command"):
            self.test_command = tdd["test_command"]

        # Gate toggles
        code_review = data.get("code_review", {})
        security_review = data.get("security_review", {})
        test_quality = data.get("test_quality", {})
        e2e_test = data.get("e2e_test", {})
        self.gates = GateToggles(
            code_review_per_block=code_review.get("per_block", True),
            security_review_per_block=security_review.get("per_block", True),
            test_quality_enabled=test_quality.get("enabled", True),
            e2e_test_entry_point_check=e2e_test.get("entry_point_check", True),
        )

    def _discover_test_command(self) -> None:
        """Auto-detect test command if not specified in config."""
        if self.test_command:
            return
        p = self.project_dir
        discovery = [
            (p / "pytest.ini", "pytest -xvs"),
            (p / "pyproject.toml", "pytest -xvs"),
            (p / "setup.py", "pytest -xvs"),
            (p / "Package.swift", "swift test"),
            (p / "Cargo.toml", "cargo test"),
            (p / "go.mod", "go test ./..."),
            (p / "package.json", "npm test"),
        ]
        for marker, cmd in discovery:
            if marker.exists():
                self.test_command = cmd
                self.stack = self.stack or marker.name.split(".")[0]
                log.info("Discovered test command: %s", cmd)
                return

    @property
    def change_dir(self) -> Path:
        return self.specs_dir / "changes" / self.change_name

    @property
    def research_path(self) -> Path:
        return self.change_dir / "research.md"

    @property
    def design_path(self) -> Path:
        return self.change_dir / "design.md"

    @property
    def tasks_path(self) -> Path:
        return self.change_dir / "tasks.md"

    @property
    def rubric_path(self) -> Path:
        return self.change_dir / "rubric.md"

    def state_file_path(self) -> Path:
        return self.change_dir / ".build-state.json"

    def progress_file_path(self) -> Path:
        return self.project_dir / "build-progress.md"

    def stack_reference_docs(self) -> list[Path]:
        """Return reference doc paths for the detected stack."""
        ref_dir = self.config_dir / "reference" / "dev"
        mapping: dict[str, list[str]] = {
            "pyproject": ["uv-python-best-practices.md", "python-project-structure.md"],
            "Package": ["swift-best-practices.md", "swift-testing-strategies.md"],
            "package": ["bun-vite-react-best-practices.md"],
            "Cargo": [],
            "go": [],
        }
        docs = mapping.get(self.stack, [])
        return [ref_dir / d for d in docs if (ref_dir / d).exists()]

    # --- Tier-dependent behavior properties ---

    @property
    def task_granularity(self) -> str:
        """'blocks' for advanced tier, 'checkboxes' for standard."""
        return "blocks" if self.tier == "advanced" else "checkboxes"

    @property
    def agent_scope(self) -> str:
        """'per_block' for advanced, 'per_task' for standard."""
        return "per_block" if self.tier == "advanced" else "per_task"

    @property
    def refactor_phase(self) -> bool:
        """Whether to run a separate refactor agent. False for advanced tier."""
        return self.tier != "advanced"

    @property
    def tdd_phases(self) -> list[str]:
        """TDD phases: [test, implement] for advanced, [test, implement, refactor] for standard."""
        if self.tier == "advanced":
            return ["test", "implement"]
        return ["test", "implement", "refactor"]

    @property
    def implementer_prompt_style(self) -> str:
        """'clean' for advanced (refactor merged in), 'minimum' for standard."""
        return "clean" if self.tier == "advanced" else "minimum"
