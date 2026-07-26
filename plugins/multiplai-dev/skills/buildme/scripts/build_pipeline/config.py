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

from .env import load_multiplai_conf, pick_model, resolve_effort, resolve_model

log = logging.getLogger(__name__)

Tier = Literal["advanced", "standard"]
Mode = Literal["scratch", "brief", "only"]


def detect_tier() -> tuple[Tier, str]:
    """Detect model tier from the model buildme will actually run.

    Returns (tier, model_name). Derives advanced/standard from ``DEFAULT_MODEL``
    — the model resolved by ``pick_model`` from the family + ``multiplai.conf``
    ceiling — rather than the ``CLAUDE_MODEL`` env var.

    DEV-3 fix: Claude Code (v2.1.x) does NOT export ``CLAUDE_MODEL`` to Bash
    subprocesses, and buildme's SKILL.md invokes this pipeline via a plain
    `uv run ...` with no `CLAUDE_MODEL=` prefix, so the old env-based check
    ALWAYS returned 'standard' in production regardless of the pinned model.
    Deriving from ``DEFAULT_MODEL`` makes the tier reflect the real model (opus
    → advanced under an opus ceiling). An explicit ``CLAUDE_MODEL``, if set,
    still wins as an override.
    """
    model = os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL
    if _is_advanced_model(model):
        return "advanced", model
    return "standard", model


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


# Resolve buildme's model from its semantic tier (opus — hard work) capped by
# the MULTIPLAI_MODEL ceiling. pick_model applies a [buildme] override from
# multiplai.conf if present; the family→ID map is the single source of truth in
# multiplai_core.env, so there is no dated model literal to go stale here.
DEFAULT_MODEL = pick_model("opus", task="buildme")


# The effort names the SDK accepts. Mirrors multiplai_core.env's tier table;
# kept here because that table is private (`_EFFORT_TIERS`).
KNOWN_EFFORTS = frozenset({"low", "medium", "high", "max"})


def conf_effort(task: str, default: str | None = None) -> str | None:
    """``EFFORT=`` for *task* from multiplai.conf, capped by MULTIPLAI_EFFORT.

    The effort twin of ``pick_model``'s ``[task] MODEL=`` override: model and
    effort are two axes of the same tuning decision, and only the model half
    was reachable from the conf file. ``[buildme] EFFORT=low`` dials the whole
    pipeline down; ``[buildme.review] EFFORT=high`` tunes one step.

    Returns *default* when the conf says nothing, so behaviour is unchanged
    unless someone opts in. The MULTIPLAI_EFFORT ceiling still applies — a
    budget run forces every step down and a conf override can't escape it.

    An unrecognized value falls back to *default* rather than passing through:
    `resolve_effort` ranks an unknown name at the "high" tier, so the ceiling
    never trips and `[buildme] EFFORT=turbo` would otherwise reach the SDK
    verbatim.

    CONSOLIDATE: `multiplai_core.env.pick_effort` (core #7) is this function
    with the same normalization. Once that release is pinned, both this and
    deep-research's copy should call it instead.
    """
    section = (load_multiplai_conf().get("_sections", {}) or {}).get(task) or {}
    requested = (section.get("EFFORT") or "").strip().lower()
    if not requested:
        return default
    if requested not in KNOWN_EFFORTS:
        log.warning("Unknown [%s] EFFORT=%r in multiplai.conf (expected one of %s) "
                    "— ignoring", task, requested, ", ".join(sorted(KNOWN_EFFORTS)))
        return default
    return resolve_effort(requested)


@dataclass
class GateToggles:
    """Per-gate on/off switches from config.yaml."""
    # RESERVED / not yet wired: no code path consults these two toggles. The
    # active per-block review is run_code_review (llm_steps/review_steps.py),
    # called from tdd_engine._run_quality_review; it always runs — there is no
    # off-switch consulted yet. No security-review gate exists either.
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
    # Restores the pre-0.4 accept-and-continue behavior for overnight runs:
    # review exhaustion and final-review failures/errors log-and-continue
    # instead of failing the build.
    lenient_review: bool = False

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

    # Optional stronger model for quality reviews (None → falls back to
    # `model`). Populated from BUILDME_REVIEW_MODEL (env, wins) or
    # `code_review.model` in specs/config.yaml; both ceiling-capped by
    # resolve_model, matching the existing model-resolution pattern.
    review_model: str | None = None

    # Reasoning effort, the second axis of the same tuning decision as `model`.
    # None → SDK default. `[buildme] EFFORT=` sets the pipeline-wide value;
    # `[buildme.spec]` / `[buildme.review]` / `[buildme.agent]` tune one step.
    # All four read the conf at construction (not import) time, so the
    # pipeline-wide value and the per-step ones can never disagree about which
    # conf they saw.
    #
    # `effort` is the root the three per-step fields fall back to, applied in
    # __post_init__ rather than in each default_factory: a caller that
    # constructs `BuildConfig(effort="low")` directly must get a low-effort
    # pipeline, which per-field conf reads would silently ignore.
    effort: str | None = field(default_factory=lambda: conf_effort("buildme"))
    spec_effort: str | None = field(default_factory=lambda: conf_effort("buildme.spec"))
    review_effort: str | None = field(default_factory=lambda: conf_effort("buildme.review"))
    agent_effort: str | None = field(default_factory=lambda: conf_effort("buildme.agent"))

    # Coding-standards docs pushed into the reviewer's context (paths from
    # `standards_files` in specs/config.yaml). Pull-for-implementer,
    # push-for-reviewer: these are NOT added to implementer prompts.
    standards_files: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Per-step effort falls back to the pipeline-wide root, so `effort` is
        # the one field a caller (or `[buildme] EFFORT=`) has to set to move
        # every step.
        for attr in ("spec_effort", "review_effort", "agent_effort"):
            if getattr(self, attr) is None:
                setattr(self, attr, self.effort)

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
            lenient_review=getattr(args, "lenient_review", False),
        )
        config.specs_dir = config.project_dir / "specs"
        config._load_specs_config()
        # Env override wins over config.yaml (same precedence as CLAUDE_MODEL
        # in detect_tier); ceiling-capped like every other model resolution.
        env_review_model = os.environ.get("BUILDME_REVIEW_MODEL")
        if env_review_model:
            config.review_model = resolve_model(env_review_model)
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

        # Reviewer context: coding standards + optional stronger review model
        self.standards_files = data.get("standards_files") or []
        code_review_cfg = data.get("code_review", {}) or {}
        if code_review_cfg.get("model") and not self.review_model:
            self.review_model = resolve_model(code_review_cfg["model"])

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
        # Normalize the change name so a hostile/careless --change value
        # (e.g. '../../foo') can't escape specs/changes/ — archive() will
        # shutil.move this directory, so an out-of-tree path is dangerous.
        from .change_manager import normalize_change_name
        return self.specs_dir / "changes" / normalize_change_name(self.change_name)

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

    def standards_text(self) -> str:
        """Concatenated contents of the coding-standards docs for the reviewer.

        Resolves each `standards_files` entry (absolute, or relative to the
        project dir, then $CLAUDE_CONFIG_DIR/reference/dev/, then
        $CLAUDE_CONFIG_DIR). Missing OR unreadable files are logged and
        skipped — one bad standards doc must not fail the block. Returns ""
        when nothing resolves — the review prompt then says
        "(no standards provided)".
        """
        parts: list[str] = []
        for entry in self.standards_files:
            path = self._resolve_standards_file(entry)
            if path is None:
                log.warning("Standards file not found, skipping: %s", entry)
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError) as e:
                log.warning("Standards file unreadable, skipping: %s (%s)", path, e)
                continue
            parts.append(f"### Standard: {path.name}\n{text}")
        return "\n\n".join(parts)

    def _resolve_standards_file(self, entry: str) -> Path | None:
        p = Path(entry).expanduser()
        if p.is_absolute():
            return p if p.is_file() else None
        for base in (self.project_dir, self.config_dir / "reference" / "dev", self.config_dir):
            candidate = base / p
            if candidate.is_file():
                return candidate
        return None

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
