"""Build pipeline configuration — presets, tier detection, config loading."""

from __future__ import annotations

import argparse
import json
import os
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .env import KNOWN_EFFORTS, load_multiplai_conf, pick_effort, pick_model, resolve_model
from .models import ReviewGatePolicy

log = logging.getLogger(__name__)

Tier = Literal["advanced", "standard"]
Mode = Literal["scratch", "brief", "only"]

# Built-in stack/framework → reference doc names, resolved under
# $CLAUDE_CONFIG_DIR/reference/dev/. These are DEFAULTS: a `reference_docs:`
# key in specs/config.yaml replaces the list for whichever keys it names and
# leaves the rest alone (see BuildConfig.reference_doc_names).
#
# The manifest keys ("pyproject", "Package", "package", "Cargo", "go") are the
# `stack` value that _discover_test_command derives from the manifest
# filename. The framework keys ("django", "react") are appended by
# detect_frameworks(), which looks inside the manifests — a Django project and
# a plain-library project are both `stack == "pyproject"`, so the manifest
# filename alone cannot tell them apart.
#
# A name here must match a filename on disk exactly — resolution does no
# fuzzy matching, and a miss is only an INFO line. Renaming a doc without
# updating this map silently removes it from every build (which is what
# happened to the Django and React entries between 2026-07 and 2026-08). The
# renaming contract is stated next to the docs, in reference/dev/README.md.
_DEFAULT_REFERENCE_DOCS: dict[str, list[str]] = {
    "pyproject": ["uv-python-best-practices.md", "python-project-structure.md"],
    "Package": ["swift-best-practices.md", "swift-testing-strategies.md"],
    "package": ["bun-vite-react-best-practices.md"],
    "Cargo": [],
    "go": [],
    "django": ["django-drf-best-practices.md"],
    "react": ["react-nextjs-best-practices.md"],
    "fastapi": ["fastapi-best-practices.md"],
}

# Per-document ceiling when reference docs are inlined into a spec-generation
# prompt. The spec-gen system prompt forbids tools, so inlining is the only
# channel — and an unbounded standards doc would crowd out the specs it is
# meant to inform.
#
# Sized against the real corpus rather than a round number: the docs run
# 20k-61k chars, so 8000 (the original value, set when they were ~20k) cut the
# Django doc to 13% of itself. 24000 admits most docs whole and, for the two
# that overflow, `summarize_reference_doc` keeps whole sections plus a full
# section index — so the generator always knows what it did not receive.
REFERENCE_DOC_CHAR_LIMIT = 24000


_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def summarize_reference_doc(name: str, text: str, limit: int) -> str:
    """`text` trimmed to `limit` chars on section boundaries, never mid-sentence.

    Returns `text` unchanged when it already fits. Otherwise emits, in order:
    everything above the first H2 (the doc's own framing), an index of ALL its
    H2 sections, then whole sections until the budget runs out, then a marker
    naming the sections that were dropped and the file to Read for them.

    Two properties matter more than the exact packing. The generator never
    sees half a sentence presented as a standard — the old flat
    ``text[:limit]`` cut mid-line, and a truncated rule reads as a complete
    one. And the index is unconditional, so a section that did not fit is
    *known to exist* rather than invisible; the TDD phases hold ``Read`` and
    can fetch it.

    Falls back to a plain character cut for a doc with no H2 headers at all —
    there are no boundaries to respect, so there is nothing better to do.
    """
    if len(text) <= limit:
        return text

    matches = list(_H2_RE.finditer(text))
    if not matches:
        return (
            text[:limit]
            + f"\n\n[... truncated at {limit} chars — read {name} in full for the rest ...]"
        )

    headers = [m.group(1).strip() for m in matches]
    bounds = [m.start() for m in matches] + [len(text)]
    preamble = text[: bounds[0]].rstrip()
    index = "\n".join(f"- {h}" for h in headers)
    header_block = (
        f"{preamble}\n\n"
        f"[Section index for {name} — every section in the doc, whether or not "
        f"its body is included below:]\n{index}\n"
    )

    kept: list[str] = []
    kept_names: list[str] = []
    used = len(header_block)
    for i, header in enumerate(headers):
        section = text[bounds[i] : bounds[i + 1]].rstrip()
        if used + len(section) + 2 > limit:
            continue
        kept.append(section)
        kept_names.append(header)
        used += len(section) + 2

    dropped = [h for h in headers if h not in kept_names]
    tail = ""
    if dropped:
        tail = (
            f"\n\n[... {len(dropped)} section(s) omitted for length: "
            f"{', '.join(dropped)}. Read {name} for them ...]"
        )
    return header_block + "\n" + "\n\n".join(kept) + tail


def _requirement_name(spec: str) -> str:
    """The bare package name from a requirement line, lowercased.

    `Django>=5.0`, `django[argon2]==5.0`, `  django  # comment` → `django`.
    Returns "" for blanks, comments and flag lines (`-r other.txt`), which is
    never equal to a package name being looked for.
    """
    text = spec.split("#", 1)[0].strip()
    if not text or text.startswith("-"):
        return ""
    name = re.split(r"[\[\s<>=!~;,]", text, maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def _pyproject_dependency_specs(data: dict) -> list[str]:
    """Every dependency requirement string in a parsed pyproject.toml.

    Covers PEP 621 (`project.dependencies`, `project.optional-dependencies`)
    and Poetry (`tool.poetry.dependencies`, where the *keys* are the names).
    """
    specs: list[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            specs.extend(d for d in deps if isinstance(d, str))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    specs.extend(d for d in group if isinstance(d, str))
    poetry = (data.get("tool") or {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict):
        for section in ("dependencies", "dev-dependencies"):
            group = poetry.get(section)
            if isinstance(group, dict):
                specs.extend(str(name) for name in group)
    return specs


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


# The effort names the SDK accepts — core's own table, imported rather than
# mirrored (core v0.11.0 exports it; it used to be private `_EFFORT_TIERS`).
# The name is still bound here because `conf_effort` below needs it to emit a
# user-facing warning *before* delegating: `pick_effort` normalizes an unknown
# value silently, which would leave a conf typo undiagnosable. Validating
# against core's set rather than a copy is the point — a copy that drifts makes
# `conf_effort` return "high" instead of *default* for a name it wrongly
# believes is valid.


def conf_effort(task: str, default: str | None = None) -> str | None:
    """``EFFORT=`` for *task* from multiplai.conf, capped by MULTIPLAI_EFFORT.

    The effort twin of ``pick_model``'s ``[task] MODEL=`` override: model and
    effort are two axes of the same tuning decision, and only the model half
    was reachable from the conf file. ``[buildme] EFFORT=low`` dials the whole
    pipeline down; ``[buildme.review] EFFORT=high`` tunes one step.

    Returns *default* when the conf says nothing, so behaviour is unchanged
    unless someone opts in — ``None`` means "omit effort, let the SDK decide",
    a state ``multiplai_core.env.pick_effort`` cannot express, which is why
    this wrapper exists at all. Normalization and the MULTIPLAI_EFFORT ceiling
    are ``pick_effort``'s job: a budget run forces every step down and a conf
    override can't escape it. Delegating to core changed one thing — a
    ``MULTIPLAI_EFFORT`` *global in the conf file* now caps conf-set efforts
    too (previously only the env var did); the two ceilings were never meant
    to disagree.

    An unrecognized value falls back to *default* rather than passing through,
    with a warning — ``pick_effort`` drops unknown names silently, which would
    leave a `[buildme] EFFORT=turbo` typo undiagnosable from the conf side.
    """
    section = (load_multiplai_conf().get("_sections", {}) or {}).get(task) or {}
    requested = (section.get("EFFORT") or "").strip().lower()
    if not requested:
        return default
    if requested not in KNOWN_EFFORTS:
        log.warning("Unknown [%s] EFFORT=%r in multiplai.conf (expected one of %s) "
                    "— ignoring", task, requested, ", ".join(sorted(KNOWN_EFFORTS)))
        return default
    return pick_effort(requested, task=task)


def _normalize_prototype_toggle(value) -> str:
    """Map a config.yaml `prototype.enabled` value to "auto" | "true" | "false".

    YAML turns `enabled: true` into a bool and `enabled: auto` into a string, so
    both spellings arrive here. Anything unrecognized falls back to "auto" with
    a warning — an unreadable toggle should leave the applicability rule in
    charge, not silently disable the stage.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in ("auto", "true", "false"):
        return text
    if text in ("yes", "on", "1"):
        return "true"
    if text in ("no", "off", "0"):
        return "false"
    log.warning("Unrecognized prototype.enabled value %r — using 'auto'", value)
    return "auto"


@dataclass
class GateToggles:
    """Per-gate on/off switches from config.yaml."""
    # RESERVED / not yet wired: no code path consults this toggle. The
    # active per-block review is run_code_review (llm_steps/review_steps.py),
    # called from tdd_engine._run_quality_review; it always runs — there is no
    # off-switch consulted yet.
    code_review_per_block: bool = True
    test_quality_enabled: bool = True
    e2e_test_entry_point_check: bool = True
    # B1 explainer gate: write unknowns.md before depending on anything new to
    # this project. Default ON — reading the explainers is the anti-slop step.
    explainers_enabled: bool = True
    # Prototype-first stage: "auto" (run when gates.prototype_required says the
    # change has a UI or a user-visible output format), "true" (always), or
    # "false" (never). Tri-state rather than bool because the useful default is
    # "decide from the change", not on/off.
    prototype: str = "auto"
    # `respec: {halt_on_contradiction: true}` in specs/config.yaml. When an
    # agent reports SPEC_IMPACT: contradicts, the build stops with a diagnosis
    # instead of steering around the spec. Default False — the note is always
    # recorded and surfaced; halting is the opt-in escalation.
    respec_halt_on_contradiction: bool = False


@dataclass
class GitToggles:
    """Git lifecycle switches (`git:` block in specs/config.yaml, overridden by
    --no-worktree / --no-push / --no-pr / --pr-ready).

    Defaults are the product defaults: the build runs in its own worktree on
    its own branch, pushes it, and opens a **draft** PR. `worktree=False`
    reproduces the pre-git-lifecycle pipeline exactly (build in place, no
    push, no PR — push/PR only ever act on a branch the pipeline created).
    """

    worktree: bool = True
    push: bool = True
    pr: Literal["draft", "ready", "none"] = "draft"


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
    # --skip-explainers: skip the B1 unknowns/edge-case explainer pass.
    # unknowns.md is still written (recording the skip) so the artifact DAG
    # stays satisfied and the absence is visible rather than silent.
    skip_explainers: bool = False
    # Prototype-first stage: "auto" | "true" | "false". Resolved from
    # specs/config.yaml (`prototype: {enabled: ...}`) with --prototype /
    # --no-prototype winning over it.
    prototype_mode: str = "auto"

    # Project context (from specs/config.yaml)
    project_name: str = ""
    project_description: str = ""
    stack: str = ""
    test_command: str = ""

    # Memory files
    core_memory_files: list[str] = field(default_factory=lambda: ["technical-pref.md"])
    stack_memory_files: list[str] = field(default_factory=list)
    additional_memory_files: list[str] = field(default_factory=list)

    # Reference docs: per-stack/framework overrides from specs/config.yaml's
    # `reference_docs:` key. Empty means "built-in defaults only"; a key
    # present here replaces _DEFAULT_REFERENCE_DOCS for that key alone.
    reference_docs: dict[str, list[str]] = field(default_factory=dict)

    # Gate toggles
    gates: GateToggles = field(default_factory=GateToggles)

    # Git lifecycle toggles
    git: GitToggles = field(default_factory=GitToggles)

    # Runtime marker, not user-configurable: set to the branch name once the
    # pipeline has created its own worktree+branch (or re-bound to one on
    # resume). None means "we do not own a branch" — every commit/push/PR
    # helper no-ops, which is what keeps --no-worktree behavior unchanged.
    pipeline_branch: str | None = None

    # Runtime marker, not user-configurable: stack_reference_docs() announces
    # the resolved set once per process (it is called per artifact and per TDD
    # block, and one REFERENCES: line is information while twenty is noise).
    references_announced: bool = field(default=False, repr=False)

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

    # Resolved reviewer models for the per-block review panel, from
    # `code_review.panel` in specs/config.yaml. Empty = single reviewer on
    # review_model-or-model, i.e. the pre-panel behavior. A panel is opt-in
    # because each member is another full-diff call.
    review_panel: list[str] = field(default_factory=list)

    # Thresholds for the graded review gate (`code_review.gate`). Defaults
    # reproduce the previous hardcoded binary behavior.
    review_gate: ReviewGatePolicy = field(default_factory=ReviewGatePolicy)

    # Orchestrator adjudication of reviewer findings (`code_review.adjudicate`).
    # On by default: reviewers propose, the orchestrator disposes. Turning it
    # off does NOT auto-apply findings — it drops them, because applying an
    # unadjudicated reviewer suggestion is the thing this pipeline must never do.
    adjudicate_findings: bool = True

    # Per-build ceilings from `budget:` in specs/config.yaml. None = unlimited
    # (the pre-budget behavior).
    budget_max_tokens: int | None = None
    budget_max_usd: float | None = None

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
            skip_explainers=getattr(args, "skip_explainers", False),
        )
        config.specs_dir = config.project_dir / "specs"
        config._load_specs_config()
        # config.yaml supplies the default; the CLI flags override it.
        config.prototype_mode = config.gates.prototype
        if getattr(args, "prototype", False):
            config.prototype_mode = "true"
        if getattr(args, "no_prototype", False):
            config.prototype_mode = "false"
        # CLI flags win over the config.yaml `git:` block (same precedence as
        # every other override here). Absent flags leave the config.yaml /
        # dataclass defaults alone.
        config._apply_git_cli_overrides(args)
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

        # Reference docs (per stack/framework key, overriding the built-ins)
        self._load_reference_docs(data.get("reference_docs"))

        # TDD config
        tdd = data.get("tdd", {})
        if tdd.get("test_command"):
            self.test_command = tdd["test_command"]

        # Reviewer context: coding standards + optional stronger review model
        self.standards_files = data.get("standards_files") or []
        code_review_cfg = data.get("code_review", {}) or {}
        if code_review_cfg.get("model") and not self.review_model:
            self.review_model = resolve_model(code_review_cfg["model"])
        self._load_review_panel(code_review_cfg)
        self._load_review_gate(code_review_cfg)
        self.adjudicate_findings = bool(code_review_cfg.get("adjudicate", True))
        self._load_budget(data.get("budget", {}) or {})

        # Gate toggles
        code_review = data.get("code_review", {})
        test_quality = data.get("test_quality", {})
        e2e_test = data.get("e2e_test", {})
        explainers = data.get("explainers", {}) or {}
        prototype = data.get("prototype", {}) or {}
        respec = data.get("respec", {}) or {}
        self.gates = GateToggles(
            code_review_per_block=code_review.get("per_block", True),
            test_quality_enabled=test_quality.get("enabled", True),
            e2e_test_entry_point_check=e2e_test.get("entry_point_check", True),
            explainers_enabled=explainers.get("enabled", True),
            prototype=_normalize_prototype_toggle(prototype.get("enabled", "auto")),
            respec_halt_on_contradiction=respec.get("halt_on_contradiction", False),
        )

        # Git lifecycle: git: {worktree: true, push: true, pr: draft|ready|none}
        git_cfg = data.get("git", {}) or {}
        pr_mode = str(git_cfg.get("pr", "draft")).lower()
        if pr_mode not in ("draft", "ready", "none"):
            log.warning("Invalid git.pr value %r in config.yaml — using 'draft'", pr_mode)
            pr_mode = "draft"
        self.git = GitToggles(
            worktree=bool(git_cfg.get("worktree", True)),
            push=bool(git_cfg.get("push", True)),
            pr=pr_mode,
        )

    def _apply_git_cli_overrides(self, args: argparse.Namespace) -> None:
        """--no-worktree / --no-push / --no-pr / --pr-ready beat config.yaml."""
        if getattr(args, "no_worktree", False):
            self.git.worktree = False
        if getattr(args, "no_push", False):
            self.git.push = False
        if getattr(args, "no_pr", False):
            self.git.pr = "none"
        if getattr(args, "pr_ready", False):
            self.git.pr = "ready"

    def rebind_project_dir(self, new_project_dir: Path) -> None:
        """Point the whole config at a different checkout of the same project.

        Called once, in BOOTSTRAP, after the build's worktree is created (and
        on resume when re-binding to an existing one). `specs_dir` is the only
        derived path stored as a field; `change_dir`, `state_file_path()` and
        `progress_file_path()` are computed from these two, so every later
        phase lands inside the worktree with no other code change.
        """
        self.project_dir = Path(new_project_dir)
        self.specs_dir = self.project_dir / "specs"

    def _load_review_panel(self, code_review_cfg: dict) -> None:
        """Resolve `code_review.panel` into concrete reviewer model IDs.

        Accepts either a list of dicts (`- model: opus`) or bare strings. Each
        entry is ceiling-capped by `resolve_model` like every other model
        resolution here, EXCEPT provider-qualified entries (`openai:gpt-5`),
        which pass through untouched — the ceiling ranks the Claude family
        only, and the point of a cross-family panel member is that it is not
        in that family.
        """
        entries = code_review_cfg.get("panel") or []
        if not isinstance(entries, list):
            log.warning("code_review.panel is not a list — ignoring")
            return
        panel: list[str] = []
        for entry in entries:
            name = entry.get("model") if isinstance(entry, dict) else entry
            if not name or not isinstance(name, str):
                log.warning("Skipping malformed code_review.panel entry: %r", entry)
                continue
            panel.append(name if ":" in name else resolve_model(name))
        self.review_panel = panel
        if panel:
            log.info("Review panel configured: %s", panel)

    def _load_review_gate(self, code_review_cfg: dict) -> None:
        """Load graded-gate thresholds, keeping the defaults on bad input."""
        gate_cfg = code_review_cfg.get("gate") or {}
        if not isinstance(gate_cfg, dict):
            log.warning("code_review.gate is not a mapping — using defaults")
            return
        try:
            self.review_gate = ReviewGatePolicy(
                **{
                    k: v
                    for k, v in gate_cfg.items()
                    if k in ReviewGatePolicy.model_fields
                }
            )
        except Exception as e:
            # A typo'd threshold must not silently loosen the gate.
            log.warning("Invalid code_review.gate (%s) — using defaults", e)

    def _load_budget(self, budget_cfg: dict) -> None:
        """Load per-build ceilings. Absent/unparseable → unlimited (as before)."""
        if not isinstance(budget_cfg, dict):
            return
        raw_tokens = budget_cfg.get("max_tokens")
        raw_usd = budget_cfg.get("max_usd")
        try:
            self.budget_max_tokens = int(raw_tokens) if raw_tokens else None
        except (TypeError, ValueError):
            log.warning("Invalid budget.max_tokens=%r — ignoring", raw_tokens)
        try:
            self.budget_max_usd = float(raw_usd) if raw_usd else None
        except (TypeError, ValueError):
            log.warning("Invalid budget.max_usd=%r — ignoring", raw_usd)

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

    @property
    def unknowns_path(self) -> Path:
        return self.change_dir / "unknowns.md"

    @property
    def codebase_analysis_path(self) -> Path:
        """Where BuildPhase.CODEBASE_ANALYSIS writes its report."""
        return self.change_dir / "codebase-analysis.md"

    @property
    def explainers_active(self) -> bool:
        """Whether the B1 explainer pass runs. The CLI flag wins over config.yaml
        (same precedence as every other flag/config pair here)."""
        return self.gates.explainers_enabled and not self.skip_explainers

    @property
    def prototype_dir(self) -> Path:
        """The only directory the prototype agent may write to."""
        return self.change_dir / "prototype"

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

    def _load_reference_docs(self, raw) -> None:
        """Parse specs/config.yaml's `reference_docs:` key.

        Shape is `{<stack-or-framework>: [<doc name>, ...]}`. A malformed
        entry is dropped with a warning rather than failing the build — the
        built-in defaults then stand for that key.
        """
        if not raw:
            return
        if not isinstance(raw, dict):
            log.warning("reference_docs must be a mapping, got %s — ignoring", type(raw).__name__)
            return
        parsed: dict[str, list[str]] = {}
        for key, value in raw.items():
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                log.warning(
                    "reference_docs['%s'] must be a list of doc names — ignoring", key,
                )
                continue
            parsed[str(key)] = list(value)
        self.reference_docs = parsed

    def detect_frameworks(self) -> list[str]:
        """Frameworks visible in the project's manifests, beyond the manifest
        filename that `stack` records.

        A Django app and a plain Python library are both `stack == "pyproject"`,
        so the framework has to be read out of the dependency list (or, for
        Django, its unmistakable `manage.py`). Detection is additive: each hit
        appends its own reference-doc key.
        """
        found: list[str] = []
        p = self.project_dir
        if (p / "manage.py").is_file() or self._python_deps_mention("django"):
            found.append("django")
        if self._python_deps_mention("fastapi"):
            found.append("fastapi")
        # `next` counts as react: the Next.js doc is the React doc, and a
        # Next project that lists only `next` (react arriving transitively)
        # would otherwise resolve no frontend standards at all.
        if self._node_deps_mention("react") or self._node_deps_mention("next"):
            found.append("react")
        return found

    def _python_deps_mention(self, package: str) -> bool:
        """Whether `package` appears in pyproject.toml or requirements.txt deps."""
        pyproject = self.project_dir / "pyproject.toml"
        if pyproject.is_file():
            try:
                data = tomllib.loads(pyproject.read_text())
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                log.warning("Could not parse %s for framework detection: %s", pyproject, e)
                data = {}
            for spec in _pyproject_dependency_specs(data):
                if _requirement_name(spec) == package:
                    return True
        reqs = self.project_dir / "requirements.txt"
        if reqs.is_file():
            try:
                lines = reqs.read_text().splitlines()
            except (OSError, UnicodeDecodeError) as e:
                log.warning("Could not read %s for framework detection: %s", reqs, e)
                lines = []
            for line in lines:
                if _requirement_name(line) == package:
                    return True
        return False

    def _node_deps_mention(self, package: str) -> bool:
        """Whether `package` is a dependency in package.json."""
        manifest = self.project_dir / "package.json"
        if not manifest.is_file():
            return False
        try:
            data = json.loads(manifest.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("Could not parse %s for framework detection: %s", manifest, e)
            return False
        if not isinstance(data, dict):
            return False
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            section = data.get(key)
            if isinstance(section, dict) and package in section:
                return True
        return False

    def reference_doc_names(self) -> list[str]:
        """The reference doc filenames for this project, before existence checks.

        Built-in defaults, overridden per key by `reference_docs:` in
        specs/config.yaml, for the detected `stack` plus every framework
        `detect_frameworks()` finds. Order is stack docs first, then frameworks
        in detection order; duplicates collapse to their first appearance.
        """
        mapping = dict(_DEFAULT_REFERENCE_DOCS)
        mapping.update(self.reference_docs)

        names: list[str] = []
        for key in [self.stack, *self.detect_frameworks()]:
            for name in mapping.get(key, []):
                if name not in names:
                    names.append(name)
        return names

    def stack_reference_docs(self) -> list[Path]:
        """Reference doc paths for the detected stack and frameworks.

        A name with no file under $CLAUDE_CONFIG_DIR/reference/dev/ is skipped
        (the mapping may name a doc that has not been written yet), but the
        skip is logged rather than silent, and resolving *nothing* for a
        project that does have a detected stack is a warning: it means every
        spec below is being written with no conventions to build to.
        """
        ref_dir = self.config_dir / "reference" / "dev"
        names = self.reference_doc_names()
        resolved: list[Path] = []
        for name in names:
            path = ref_dir / name
            if path.is_file():
                resolved.append(path)
            else:
                log.info("Reference doc not found, skipping: %s", path)
        self._announce_references(names, resolved)
        return resolved

    def _announce_references(self, names: list[str], resolved: list[Path]) -> None:
        """Emit the `REFERENCES:` progress line once per process."""
        if names and not resolved:
            log.warning(
                "Stack '%s' detected but NONE of its reference docs resolved under %s "
                "(wanted: %s) — specs will be generated with no conventions to build to",
                self.stack or "(unknown)",
                self.config_dir / "reference" / "dev",
                ", ".join(names),
            )
        if self.references_announced:
            return
        self.references_announced = True
        listed = ", ".join(p.name for p in resolved) or "(none)"
        print(f"REFERENCES:{listed}", flush=True)
        log.info("REFERENCES stack=%s docs=%s", self.stack or "(unknown)", listed)

    def reference_docs_text(self) -> str:
        """The reference docs inlined for a spec-generation prompt.

        The spec-generation system prompt forbids tools, so inlining is the
        only channel the generator has. Each doc over REFERENCE_DOC_CHAR_LIMIT
        is reduced by `summarize_reference_doc` — whole sections plus a full
        section index, never a mid-sentence cut. An unreadable doc is skipped
        rather than failing the build. Returns "" when nothing resolves — the
        prompt then says "(none available)".
        """
        parts: list[str] = []
        for doc in self.stack_reference_docs():
            try:
                text = doc.read_text()
            except (OSError, UnicodeDecodeError) as e:
                log.warning("Reference doc unreadable, skipping: %s (%s)", doc, e)
                continue
            if len(text) > REFERENCE_DOC_CHAR_LIMIT:
                log.info(
                    "Reference doc %s is %d chars, reducing to fit %d",
                    doc.name, len(text), REFERENCE_DOC_CHAR_LIMIT,
                )
                text = summarize_reference_doc(doc.name, text, REFERENCE_DOC_CHAR_LIMIT)
            parts.append(f"### Reference: {doc.name}\n{text}")
        return "\n\n".join(parts)

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
        """Whether to run a separate refactor agent. True on every tier.

        This property stays the single switch for the per-block refactor step;
        it used to be off on the advanced tier on the theory that a "write it
        clean" implementer made a second pass redundant. It is on everywhere
        now because the refactor step is verified rather than trusted: the
        block's tests are re-run and `unchanged_tests_gate` is re-applied
        afterwards, and a refactor that fails either is discarded by resetting
        to the implementation commit. A pass that can only ever be a no-op or
        an improvement is worth running on both tiers.
        """
        return True

    @property
    def tdd_phases(self) -> list[str]:
        """TDD phases — [test, implement, refactor] on every tier."""
        return ["test", "implement", "refactor"]

    @property
    def implementer_prompt_style(self) -> str:
        """'clean' for advanced (refactor merged in), 'minimum' for standard."""
        return "clean" if self.tier == "advanced" else "minimum"
