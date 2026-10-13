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
from typing import Iterable, Literal

import yaml

from multiplai_core import resolve_model_spec
from multiplai_core.env import CURRENT_MODEL, parse_model_spec, pick_model_spec

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

# Per-document ceiling for the reviewer's standards block (the project's
# CLAUDE.md chain plus any `standards_files`). Tighter than the spec-gen
# ceiling above because this text is paid for far more often: `standards_text`
# is called once per block, and `run_code_review` then fans that prompt out to
# every model in `review_panel` — a 40 KB chain over a 12-block build with a
# 3-member panel is ~1.4 MB of duplicated prompt text.
#
# 12000 is a judgment, not a measurement: it admits a typical CLAUDE.md whole
# and trims only the outliers. Trimming is safe here in a way it is not for the
# spec generator — that one has no tools, while a reviewer holds `Read` and
# `summarize_reference_doc` always emits the full section index, so a section
# that did not fit is known to exist and can be fetched.
STANDARDS_DOC_CHAR_LIMIT = 12000


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


def detect_tier(default_spec: str | None = None,
                configured_tier: str | None = None) -> tuple[Tier, str]:
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

    *default_spec* is `models.default` from specs/config.yaml, already
    resolved, when one is set; it stands in for ``DEFAULT_MODEL``. A Claude
    model's version says which tier it is. Another vendor's model says nothing
    about that, so for a non-Anthropic default the tier is pinned by
    ``BUILDME_TIER`` (env, wins) or *configured_tier* (`tier:` in
    specs/config.yaml), and is ``standard`` when neither is set.
    """
    model = os.environ.get("CLAUDE_MODEL") or default_spec or DEFAULT_MODEL
    spec = parse_model_spec(model)
    if not spec.is_anthropic:
        pinned = os.environ.get("BUILDME_TIER") or configured_tier
        if pinned in ("advanced", "standard"):
            return pinned, spec.qualified
        log.info("tier: standard — the default model %s is not a Claude model, so "
                 "its version says nothing about the tier; set BUILDME_TIER or "
                 "`tier:` in specs/config.yaml to pin it", spec.qualified)
        return "standard", spec.qualified
    if _is_advanced_model(spec.model):
        return "advanced", spec.model
    return "standard", spec.model


def _is_advanced_model(model: str) -> bool:
    """Advanced tier = Fable, or the Opus family at version >= 4.5.

    A version-range check rather than a literal allowlist, so the next Opus bump
    (4-7, 4-8, 5-0, ...) is recognized automatically instead of silently
    downgrading to 'standard'. Fable ranks above Opus, so any Fable is
    advanced. Other models (sonnet/haiku/other) → False.
    """
    if "fable" in model.lower():
        return True
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


def conf_model(task: str, default_tier: str = "opus") -> str | None:
    """``MODEL=`` for *task* from multiplai.conf, or None when it says nothing.

    The model twin of :func:`conf_effort`, and the reason it exists is the same:
    ``pick_model`` cannot express "the conf said nothing". It always returns a
    concrete ID (the *default_tier* one), so a field defaulted straight from it
    could never fall back to another field, and `[buildme.review] MODEL=` would
    be indistinguishable from no override at all. Returning None here keeps the
    fallback chain in ``__post_init__`` — and `review_model`'s "None → use
    `model`" contract — intact.

    A Claude model comes back as a bare, ceiling-capped ID. A Pi model
    (``pi:<model>@<provider>``) comes back qualified: ``run_agent`` runs it on
    its Pi backend. Any other provider (``openai:gpt-5``) has no runner, so it
    is dropped with a warning rather than failing the build at its first call.
    Same shape as `conf_effort`'s unknown-value handling — diagnose in the
    log, fall back to the default.
    """
    section = (load_multiplai_conf().get("_sections", {}) or {}).get(task) or {}
    if not (section.get("MODEL") or "").strip():
        return None
    try:
        spec = pick_model_spec(default_tier, task=task)
    except ValueError as e:
        log.warning("Ignoring [%s] MODEL= in multiplai.conf: %s", task, e)
        return None
    if spec.is_anthropic:
        return spec.model
    if _is_model_spec(spec.qualified):
        return spec.qualified
    log.warning("Ignoring [%s] MODEL= in multiplai.conf: %s has no runner in buildme "
                "(want a Claude model or pi:<model>@<provider>)", task, spec.qualified)
    return None


def conf_int(task: str, key: str, default: int) -> int:
    """An integer knob for *task* from multiplai.conf, or *default*.

    The third member of the `conf_effort` / `conf_model` family, for tuning
    values that are neither a model nor an effort. Same contract: an absent,
    blank or unparseable value logs and yields *default*, so a typo is
    diagnosable from the log instead of silently reshaping a gate.
    """
    section = (load_multiplai_conf().get("_sections", {}) or {}).get(task) or {}
    raw = (section.get(key) or "").strip().strip("\"'")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Unparseable [%s] %s=%r in multiplai.conf (want an integer) "
                    "— using %d", task, key, raw, default)
        return default


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


# Ticket providers buildme can take a ticket from (`intake.provider` in
# specs/config.yaml). One table, mirrored in SKILL.md Step 0: each value says
# how the *wrapper session* fetches a ticket, because the pipeline itself never
# talks to a ticket system (pipeline agents deny the Skill tool, and posting is
# the wrapper's job). Adding a provider later (e.g. `vikunja`) is one entry
# here plus one SKILL.md subsection; the loader and the pipeline flags do not
# change, since every provider hands the pipeline the same --ticket-file.
INTAKE_PROVIDERS: dict[str, str] = {
    "none": "No ticket system. A ticket-looking input stops with "
            "'run `/buildme setup`'.",
    "plane": "Wrapper invokes multiplai-pm:plane `get <ref>` and "
             "`comments <ref>`; a BLOCKED refusal is a stop.",
    "github": "Wrapper runs `gh issue view <n> --json "
              "title,body,comments,url`.",
}

INTERVIEW_MODES: tuple[str, ...] = ("self", "ticket", "user")

# The three stages a step falls back to in `models:` (specs/config.yaml).
MODEL_STAGES: tuple[str, ...] = ("spec", "agent", "review")

# Every buildme step that calls a model, and its stage. A step name is the
# call's `budget_label`, except: every `spec:<id>` artifact (use-cases
# included) is `spec_artifact`; the unlabeled explainer, prototype and respec
# calls are named for what they do; `review_fix` is the fix agent the review
# loop runs (it charges the budget as `implementer`); and
# `integration_fix_escalated` is the last integration-fix attempt, which has
# always run on the review model. Each stage holds the steps whose
# `[buildme.<stage>]` effort they already use.
STEP_STAGES: dict[str, str] = {
    "spec_artifact": "spec",
    "rubric": "spec",
    "design_audit": "spec",
    "tasks_audit": "spec",
    "codebase_analysis": "spec",
    "epic_split": "spec",
    "explainer": "spec",
    "respec": "spec",
    "interview": "spec",
    "research": "spec",
    "findings_spec": "spec",
    "test_writer": "agent",
    "implementer": "agent",
    "refactor": "agent",
    "refactor_all": "agent",
    "mutation_test_writer": "agent",
    "integration_fix": "agent",
    "review_fix": "agent",
    "docs_update": "agent",
    "prototype": "agent",
    "review": "review",
    "adjudication": "review",
    "test_quality_audit": "review",
    "plan_review": "review",
    "integration_fix_escalated": "review",
    "findings_triage": "review",
    "findings_self_check": "review",
}

# The keys `models:` accepts: a step, a stage, or `default`.
MODEL_KEYS: frozenset[str] = frozenset(STEP_STAGES) | frozenset(MODEL_STAGES) | {"default"}

# The existing per-stage field each step reads at the third link of
# `BuildConfig.model_for`'s chain: its stage's field, or its own. Adjudication
# names `model`, which skips the link: the main model, holding the whole build's
# context, judges the reviewers' findings (scripts/CLAUDE.md, "Adjudication is
# a correctness requirement"), so the stronger-reviewer `review_model` must not
# move it. `models.adjudication` or `models.review` still can.
_STAGE_MODEL_FIELDS: dict[str, str] = {
    "spec": "spec_model", "agent": "agent_model", "review": "review_model",
}
_STEP_MODEL_FIELDS: dict[str, str] = {
    "plan_review": "plan_review_model",
    "adjudication": "model",
}


def _is_model_spec(value) -> bool:
    """Whether *value* names a model buildme can run.

    A Claude model (a family name such as ``fable``, a ``claude-…`` ID, either
    one with a ``claude:`` or ``anthropic:`` prefix) or a Pi model
    (``pi:<model>@<provider>``). Anything else, ``gpt-5`` included, is a typo
    or a backend `run_agent` has no runner for, and the loader drops it.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        spec = parse_model_spec(value)
    except ValueError:
        return False
    if spec.is_anthropic:
        model = spec.model.lower()
        return model.startswith("claude-") or any(f in model for f in CURRENT_MODEL)
    return spec.provider == "pi" and "@" in spec.model


@dataclass
class IntakeConfig:
    """`intake:` block — where a ticket comes from.

    `provider` is a key of INTAKE_PROVIDERS. `options` is the provider's own
    mapping, passed through untouched (e.g. `plane: {project: DB}`).
    """

    provider: str = "none"
    options: dict = field(default_factory=dict)


@dataclass
class PrSettings:
    """`pr:` block. Stored for the PR stage; nothing in this pipeline reads it yet."""

    template: str = "auto"
    max_change_lines: int = 1000


@dataclass
class FindingsSettings:
    """`findings:` block, read by `python -m build_pipeline findings`.

    `authors`: `collaborators` (anyone whose `role_name` on the repo is
    `admin`, `maintain` or `write`) or a list of GitHub logins whose PR
    comments are acted on. `max_rounds`: fix rounds before the loop stops.
    `allow_bots`: bot logins whose comments are acted on; any other bot's are
    listed and skipped."""

    authors: str | list[str] = "collaborators"
    max_rounds: int = 3
    allow_bots: list[str] = field(default_factory=list)


@dataclass
class EpicSettings:
    """`epic:` block: how an approved epic runs its children
    (plan-buildme-epic-mode-2026-10-12.md, item 16a). `max_parallel`: children
    building at once. `max_review_rounds`: review rounds per child PR.
    `merge`: `auto` or `manual` (a human merges). `max_usd`: one ceiling for
    the whole epic, None for none."""

    max_parallel: int = 2
    max_review_rounds: int = 6
    merge: Literal["auto", "manual"] = "manual"
    max_usd: float | None = None


def _cfg_int(value, key: str, default: int) -> int:
    """A positive int from config.yaml, or *default* with a warning."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        log.warning("Invalid %s value %r in config.yaml — using %r", key, value, default)
        return default
    return value


def _cfg_mapping(value, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        log.warning("Invalid %s value %r in config.yaml — ignoring it", key, value)
        return {}
    return value


# (field, root) pairs for the per-step effort/model fallbacks, in resolution
# order: a root must be resolved before anything that reads it.
# `plan_review_model` appears twice so it lands on a concrete ID even when
# `review_model` — its own root — is None.
_STEP_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("spec_effort", "effort"),
    ("review_effort", "effort"),
    ("agent_effort", "effort"),
    ("plan_review_effort", "review_effort"),
    ("spec_model", "model"),
    ("agent_model", "model"),
    ("plan_review_model", "review_model"),
    ("plan_review_model", "model"),
)


@dataclass
class MutationSettings:
    """`mutation:` in specs/config.yaml — the TDD engine's MUTATION sub-step.

    `min_score` defaults to 0.8 with no measurement behind it: nobody has run
    mutation testing on buildme output yet, so the default is a starting point
    to be adjusted from real runs.
    """
    enabled: bool = True
    min_score: float = 0.8
    timeout: float = 30.0      # seconds per test-suite run under a mutant
    max_mutants: int = 300     # above it, a deterministic sample runs


def _load_mutation_settings(raw) -> MutationSettings:
    """Validate `mutation:`; a bad value keeps its default and logs a warning,
    like `_normalize_prototype_toggle` — never a crash, never a silent change."""
    settings = MutationSettings()
    if raw is None:
        return settings
    if not isinstance(raw, dict):
        log.warning("mutation: in config.yaml is not a mapping — using defaults")
        return settings
    enabled = raw.get("enabled", True)
    if isinstance(enabled, bool):
        settings.enabled = enabled
    elif str(enabled).strip().lower() in ("yes", "on", "1", "true"):
        settings.enabled = True
    elif str(enabled).strip().lower() in ("no", "off", "0", "false"):
        settings.enabled = False
    else:
        log.warning("Unrecognized mutation.enabled value %r in config.yaml — using true", enabled)

    def _number(key, default, *, cast, ok, rule):
        value = raw.get(key, default)
        if isinstance(value, bool):
            log.warning("Invalid mutation.%s=%r in config.yaml (%s) — using %r", key, value, rule, default)
            return default
        try:
            number = cast(value)
        except (TypeError, ValueError):
            log.warning("Invalid mutation.%s=%r in config.yaml (%s) — using %r", key, value, rule, default)
            return default
        if not ok(number) or (cast is int and number != value):
            log.warning("Invalid mutation.%s=%r in config.yaml (%s) — using %r", key, value, rule, default)
            return default
        return number

    settings.min_score = _number("min_score", settings.min_score, cast=float,
                                 ok=lambda v: 0.0 <= v <= 1.0, rule="a number from 0 to 1")
    settings.timeout = _number("timeout", settings.timeout, cast=float,
                               ok=lambda v: v > 0, rule="seconds, above 0")
    settings.max_mutants = _number("max_mutants", settings.max_mutants, cast=int,
                                   ok=lambda v: v > 0, rule="a whole number above 0")
    return settings



# Root file → test command, in the order test-command discovery checks them.
_TEST_COMMAND_DISCOVERY = (
    ("pytest.ini", "pytest -xvs"),
    ("pyproject.toml", "pytest -xvs"),
    ("setup.py", "pytest -xvs"),
    ("Package.swift", "swift test"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("package.json", "npm test"),
)


def discover_test_command(project_dir: Path | str) -> tuple[str, str] | None:
    """(test command, root file it came from) that discovery picks for
    *project_dir*, or None when no root file matches."""
    p = Path(project_dir)
    for marker, cmd in _TEST_COMMAND_DISCOVERY:
        if (p / marker).exists():
            return cmd, marker
    return None

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
    # a block that exhausts its review iterations without passing logs and
    # continues instead of failing the build.
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
    # Runtime marker, not user-configurable: the root file test-command
    # discovery took `test_command` from ("package.json", ...). Empty when the
    # command was configured (tdd.test_command) or set by the caller.
    test_command_discovered_from: str = ""

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

    # MUTATION sub-step settings (`mutation:` in specs/config.yaml)
    mutation: MutationSettings = field(default_factory=MutationSettings)

    # Ticket intake (`intake:`), interview mode (`interview:`) and the stored-only
    # blocks for later stages (`architecture`, `pr`, `findings`, `models`).
    # Every default reproduces the behaviour of a repo without the keys.
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    # "self" | "ticket" | "user". Under --auto, "user" resolves to "self"
    # (nobody is there to answer); "ticket" is kept, since asking on the
    # ticket works without a human present.
    interview_mode: str = "user"
    architecture: str = ""
    pr: PrSettings = field(default_factory=PrSettings)
    findings: FindingsSettings = field(default_factory=FindingsSettings)
    epic: EpicSettings = field(default_factory=EpicSettings)
    # `build --base-ref`: the commit a new worktree branches from (an epic's
    # coordinator passes origin's default branch). Empty: the source HEAD.
    base_ref: str = ""
    # `models:` from specs/config.yaml: step, stage or `default` → a model spec
    # as written. `model_for` resolves it.
    model_specs: dict[str, str] = field(default_factory=dict)
    # `tier:` from specs/config.yaml. Read only when the default model is not
    # a Claude model (see detect_tier).
    configured_tier: str | None = None

    # Checked facts about the code (`code_facts`), from specs/config.yaml.
    # `codebase_analysis_max_units`: the most explore agents the codebase
    # analysis runs (one per slice of the repo); the files left over are listed
    # under "Not analysed". `block_facts`: hand each block's test writer and
    # implementer the facts about the files its tasks name. Off by default:
    # whether it helps is measured by real builds, which is Spike's call.
    # `regenerate_walkthroughs` / `walkthrough_max_usd`: re-run a stale
    # walkthrough under docs/walkthroughs/ after the build, each run capped at
    # that spend. Off by default: one run may cost up to its cap.
    codebase_analysis_max_units: int = 12
    block_facts: bool = False
    regenerate_walkthroughs: bool = False
    walkthrough_max_usd: float = 10.0

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

    # Overrides the findings stage (`python -m build_pipeline findings`) sets
    # so the TDD engine runs a round's own task file against its own state
    # file, in a change that `--auto` may already have archived. The defaults
    # are today's paths. `tasks_file` and `state_file` are file names inside
    # `change_dir`; the `change_dir` setter stores its target relative to
    # `specs_dir`, so `rebind_project_dir` carries it into the worktree.
    tasks_file: str = "tasks.md"
    state_file: str = ".build-state.json"
    _change_dir_override: str = field(default="", repr=False)

    # Model for LLM calls
    model: str = DEFAULT_MODEL

    # Optional stronger model for quality reviews (None → falls back to
    # `model`). Populated from BUILDME_REVIEW_MODEL (env, wins),
    # `code_review.model` in specs/config.yaml, then `[buildme.review] MODEL=`
    # in multiplai.conf — in that order; the first two are ceiling-capped by
    # resolve_model, the conf one by pick_model, matching the existing
    # model-resolution pattern.
    #
    # The conf read happens in __post_init__ (not a default_factory) so
    # `_review_model_from_conf` can record where the value came from: without
    # it, specs/config.yaml could no longer override a conf-set reviewer,
    # because `_load_specs_config` only fills an *unset* review_model.
    review_model: str | None = None
    _review_model_from_conf: bool = field(default=False, repr=False)
    # Set in __post_init__: the per-step fields nobody asked for explicitly,
    # and therefore the only ones _resolve_step_fallbacks may rewrite.
    _derived_step_fields: set[str] = field(default_factory=set, repr=False)

    # Per-step model overrides, the other axis of the same tuning decision as
    # the effort fields below. `[buildme.spec]` / `[buildme.agent]` /
    # `[buildme.plan_review]` MODEL= tune one step without a code edit.
    #
    # Unlike `review_model` these resolve to a concrete ID in __post_init__
    # (spec/agent from `model`, plan_review from `review_model`-or-`model`), so
    # a call site can pass them straight to the SDK. review_model keeps its
    # None-means-`model` contract because `_panel_models` and its tests already
    # depend on it.
    spec_model: str | None = field(default_factory=lambda: conf_model("buildme.spec"))
    agent_model: str | None = field(default_factory=lambda: conf_model("buildme.agent"))
    plan_review_model: str | None = field(
        default_factory=lambda: conf_model("buildme.plan_review"))

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
    # PLAN_REVIEW is a review, so it inherits the reviewer's effort rather than
    # the pipeline-wide one — `[buildme.review] EFFORT=high` should not have to
    # be repeated to make the plan review think as hard as the code review.
    plan_review_effort: str | None = field(
        default_factory=lambda: conf_effort("buildme.plan_review"))

    # PLAN_REVIEW's oversized-plan triggers, both read from multiplai.conf
    # (`[buildme.plan_review] PLAN_SPLIT_BLOCK_TRIGGER=` /
    # `PLAN_SPLIT_PACKAGE_TRIGGER=`).
    #
    # 10 is a PLACEHOLDER, not a measured number — nobody has measured the
    # block-count distribution across real runs, so it is a guess wearing a
    # decimal point. It lives here as a knob rather than a constant in the
    # source precisely so it can be replaced once the run archive says where
    # per-block review actually starts failing.
    plan_split_block_trigger: int = field(
        default_factory=lambda: conf_int("buildme.plan_review",
                                         "PLAN_SPLIT_BLOCK_TRIGGER", 10))
    # The second trigger: distinct top-level packages a block set touches. A
    # change spanning several packages is a different risk class from a deep
    # change inside one, at identical block count.
    plan_split_package_trigger: int = field(
        default_factory=lambda: conf_int("buildme.plan_review",
                                         "PLAN_SPLIT_PACKAGE_TRIGGER", 5))

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

    # Fix a passing block's accepted Critical/Major/Minor findings once
    # (`code_review.fix_on_pass`). The fix is kept only if the suite stays
    # green and the block still passes review; otherwise it is rolled back and
    # the findings go to `unfixed-findings.md`. False: a passing block stops
    # its review loop at once, as before.
    fix_on_pass: bool = True

    # Per-build ceilings from `budget:` in specs/config.yaml. None = unlimited
    # (the pre-budget behavior).
    budget_max_tokens: int | None = None
    budget_max_usd: float | None = None

    def __post_init__(self) -> None:
        # `review_model` reads its conf section here rather than in a
        # default_factory so the source is recorded: specs/config.yaml must
        # still beat multiplai.conf, and `_load_specs_config` can only know to
        # overwrite a conf-supplied value if it is told which one that was. A
        # caller-supplied review_model leaves the flag False and stands.
        if self.review_model is None:
            self.review_model = conf_model("buildme.review")
            self._review_model_from_conf = self.review_model is not None

        # Which per-step fields the caller (and the conf) left unset. Only
        # these are re-derived later: a field someone asked for explicitly
        # must never be overwritten by a root that moved afterwards.
        self._derived_step_fields = {
            attr for attr, _ in _STEP_FALLBACKS if getattr(self, attr) is None
        }
        self._resolve_step_fallbacks()

    def _resolve_step_fallbacks(self) -> None:
        """Point every unset per-step field at its root.

        Per-step effort/model falls back to its root, so one field moves every
        step that has not been tuned individually. Order matters: `review_*` is
        resolved before the `plan_review_*` entry that reads it, and
        `plan_review_model` gets a second entry so it lands on a concrete ID
        even when `review_model` is None (its own root).

        `from_cli_args` runs this a second time, after specs/config.yaml and
        BUILDME_REVIEW_MODEL have moved `review_model`. Resolving only in
        __post_init__ froze `plan_review_model` on `model` before either
        override existed, so neither ever reached the plan reviewer.
        """
        for attr in self._derived_step_fields:
            setattr(self, attr, None)
        for attr, root in _STEP_FALLBACKS:
            if attr in self._derived_step_fields and getattr(self, attr) is None:
                setattr(self, attr, getattr(self, root))

    def model_for(self, step: str) -> str:
        """The model one step runs on: a bare Claude ID, or a ``pi:…`` spec.

        The chain, first set link wins:

        1. ``models.<step>`` in specs/config.yaml
        2. ``models.<stage>`` (the step's stage in ``STEP_STAGES``)
        3. the existing per-stage field (``spec_model`` / ``agent_model`` /
           ``review_model``; ``plan_review_model`` for ``plan_review``;
           none for ``adjudication``, see ``_STEP_MODEL_FIELDS``)
        4. ``models.default``
        5. ``model``

        Links 1, 2 and 4 go through ``multiplai_core.resolve_model_spec``,
        which caps a Claude model at ``MULTIPLAI_MODEL`` and passes a Pi spec
        through. Links 3 and 5 were resolved when the config loaded, so they
        are returned as they are: resolving them again would pin a dated ID
        (``claude-opus-4-6``) to the current one and change what today's
        steps run on. A per-stage field that was only copied from its root
        is not set; the chain looks at the root instead (``review_model`` for
        ``plan_review``), and ``model`` comes last.

        A Claude model comes back as a bare ID, the string every call site
        passed before this method existed; ``run_agent`` reads a bare ID as
        Anthropic. Any other provider comes back qualified (``pi:…``).

        Raises:
            KeyError: *step* is not in ``STEP_STAGES``.
        """
        stage = STEP_STAGES[step]
        spec = resolve_model_spec([self.model_specs.get(step), self.model_specs.get(stage)])
        if spec is None:
            field_model = self._explicit_field_model(
                _STEP_MODEL_FIELDS.get(step) or _STAGE_MODEL_FIELDS[stage])
            if field_model:
                return field_model
            spec = resolve_model_spec([self.model_specs.get("default")])
        if spec is None:
            return self.model
        return spec.model if spec.is_anthropic else spec.qualified

    def _explicit_field_model(self, attr: str) -> str | None:
        """*attr*'s value if someone set it, else its roots' (``model`` excluded).

        A field in ``_derived_step_fields`` holds a copy of its root, not a
        choice, so the roots in ``_STEP_FALLBACKS`` are asked instead.
        """
        if attr == "model":
            return None
        if attr not in self._derived_step_fields:
            value = getattr(self, attr, None)
            if value:
                return value
        for derived, root in _STEP_FALLBACKS:
            if derived == attr:
                value = self._explicit_field_model(root)
                if value:
                    return value
        return None

    def default_model_spec(self) -> str | None:
        """``models.default`` resolved like ``model_for`` resolves it, or None."""
        spec = resolve_model_spec([self.model_specs.get("default")])
        if spec is None:
            return None
        return spec.model if spec.is_anthropic else spec.qualified

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
        # `models.default` and `tier:` come from specs/config.yaml, which
        # detect_tier above could not see yet.
        default_spec = config.default_model_spec()
        if default_spec or config.configured_tier:
            config.tier, config.model_name = detect_tier(
                default_spec, configured_tier=config.configured_tier)
        # Also covers a repo with no config.yaml, where the loader returns early.
        config._resolve_auto_interview()
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
        # `review_model` has just moved (config.yaml, then the env override),
        # so every step that falls back to it is re-derived here. Fields the
        # caller or multiplai.conf set explicitly are untouched.
        config._resolve_step_fallbacks()
        config._discover_test_command()
        log.info("Running in %s mode (%s)", config.tier, config.model_name)
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
        # The project's own config.yaml outranks multiplai.conf — the conf is a
        # machine-wide default, config.yaml is this repo's stated choice. So a
        # value that only came from `[buildme.review] MODEL=` is overwritable
        # here, while an explicitly-constructed one still wins.
        if code_review_cfg.get("model") and (
            not self.review_model or self._review_model_from_conf
        ):
            self.review_model = resolve_model(code_review_cfg["model"])
            self._review_model_from_conf = False
        self._load_review_panel(code_review_cfg)
        self._load_review_gate(code_review_cfg)
        self.adjudicate_findings = bool(code_review_cfg.get("adjudicate", True))
        self.fix_on_pass = bool(code_review_cfg.get("fix_on_pass", True))
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
        self.mutation = _load_mutation_settings(data.get("mutation"))

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

        self._load_intake_and_stage_settings(data)
        self._load_code_facts_settings(data)

    def _load_code_facts_settings(self, data: dict) -> None:
        """`codebase_analysis_max_units`, `block_facts`, `regenerate_walkthroughs`
        and `walkthrough_max_usd`. An invalid value logs a warning and keeps the
        default, the same contract as `git.pr`. The two switches take only a
        YAML boolean, so a typo never turns one on."""
        self.codebase_analysis_max_units = _cfg_int(
            data.get("codebase_analysis_max_units", 12), "codebase_analysis_max_units", 12)
        for key in ("block_facts", "regenerate_walkthroughs"):
            value = data.get(key, False)
            if isinstance(value, bool):
                setattr(self, key, value)
            else:
                log.warning("Invalid %s value %r in config.yaml — want true | false; using false",
                            key, value)
                setattr(self, key, False)
        usd = data.get("walkthrough_max_usd", 10.0)
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
            log.warning("Invalid walkthrough_max_usd value %r in config.yaml — using 10", usd)
            usd = 10.0
        self.walkthrough_max_usd = float(usd)

    def _load_intake_and_stage_settings(self, data: dict) -> None:
        """`intake`, `interview`, `architecture`, `pr`, `findings` and
        `models` (`mutation` is `_load_mutation_settings`). An invalid value logs a warning and keeps the default,
        the same contract as `git.pr`."""
        intake_cfg = _cfg_mapping(data.get("intake"), "intake")
        provider = str(intake_cfg.get("provider", "none") or "none").lower()
        if provider not in INTAKE_PROVIDERS:
            log.warning("Invalid intake.provider value %r in config.yaml — using 'none' "
                        "(known: %s)", provider, ", ".join(INTAKE_PROVIDERS))
            provider = "none"
        options = _cfg_mapping(intake_cfg.get(provider), f"intake.{provider}") \
            if provider != "none" else {}
        self.intake = IntakeConfig(provider=provider, options=dict(options))

        mode = str(data.get("interview", "user") or "user").lower()
        if mode not in INTERVIEW_MODES:
            log.warning("Invalid interview value %r in config.yaml — using 'user'", mode)
            mode = "user"
        self.interview_mode = mode
        self._resolve_auto_interview()

        arch = data.get("architecture", "") or ""
        if not isinstance(arch, str):
            log.warning("Invalid architecture value %r in config.yaml — using ''", arch)
            arch = ""
        self.architecture = arch

        pr_cfg = _cfg_mapping(data.get("pr"), "pr")
        template = pr_cfg.get("template", "auto") or "auto"
        if not isinstance(template, str):
            log.warning("Invalid pr.template value %r in config.yaml — using 'auto'", template)
            template = "auto"
        self.pr = PrSettings(
            template=template,
            max_change_lines=_cfg_int(pr_cfg.get("max_change_lines", 1000),
                                      "pr.max_change_lines", 1000),
        )

        findings_cfg = _cfg_mapping(data.get("findings"), "findings")
        authors = findings_cfg.get("authors", "collaborators")
        if authors != "collaborators" and not (
            isinstance(authors, list) and authors
            and all(isinstance(a, str) and a.strip() for a in authors)
        ):
            log.warning("Invalid findings.authors value %r in config.yaml — using "
                        "'collaborators'", authors)
            authors = "collaborators"
        allow_bots = findings_cfg.get("allow_bots", [])
        if allow_bots is None:
            allow_bots = []
        if not (isinstance(allow_bots, list)
                and all(isinstance(b, str) and b.strip() for b in allow_bots)):
            log.warning("Invalid findings.allow_bots value %r in config.yaml — using []",
                        allow_bots)
            allow_bots = []
        self.findings = FindingsSettings(
            authors=authors,
            max_rounds=_cfg_int(findings_cfg.get("max_rounds", 3), "findings.max_rounds", 3),
            allow_bots=[b.strip() for b in allow_bots],
        )

        self._load_epic(_cfg_mapping(data.get("epic"), "epic"))

        specs: dict[str, str] = {}
        for key, spec in _cfg_mapping(data.get("models"), "models").items():
            key = str(key)
            if key not in MODEL_KEYS:
                log.warning("Dropping models.%s in config.yaml — not a buildme step, "
                            "a stage (%s) or 'default'", key, ", ".join(MODEL_STAGES))
            elif _is_model_spec(spec):
                specs[key] = spec.strip()
            else:
                log.warning("Dropping models.%s value %r in config.yaml — want haiku | "
                            "sonnet | opus | fable | claude:<name> | "
                            "pi:<model>@<provider>", key, spec)
        self.model_specs = specs

        tier = data.get("tier")
        if tier is not None and tier not in ("advanced", "standard"):
            log.warning("Invalid tier value %r in config.yaml — want advanced | "
                        "standard; ignoring it", tier)
            tier = None
        self.configured_tier = tier

    def _resolve_auto_interview(self) -> None:
        """--auto has nobody to interview: `user` becomes `self` (logged)."""
        if self.auto and self.interview_mode == "user":
            log.info("interview: user → self under --auto (no human to answer)")
            self.interview_mode = "self"

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
        self.base_ref = getattr(args, "base_ref", "") or ""

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

    def _load_epic(self, epic_cfg: dict) -> None:
        merge = epic_cfg.get("merge", "manual")
        if merge not in ("auto", "manual"):
            log.warning("Invalid epic.merge value %r in config.yaml — using 'manual'", merge)
            merge = "manual"
        raw_usd = epic_cfg.get("max_usd")
        max_usd = None
        try:
            max_usd = float(raw_usd) if raw_usd is not None else None
        except (TypeError, ValueError):
            log.warning("Invalid epic.max_usd=%r — ignoring", raw_usd)
        self.epic = EpicSettings(
            max_parallel=_cfg_int(epic_cfg.get("max_parallel", 2), "epic.max_parallel", 2),
            max_review_rounds=_cfg_int(epic_cfg.get("max_review_rounds", 6), "epic.max_review_rounds", 6),
            merge=merge,
            max_usd=max_usd,
        )

    def _discover_test_command(self) -> None:
        """Auto-detect test command if not specified in config."""
        if self.test_command:
            return
        found = discover_test_command(self.project_dir)
        if found:
            cmd, marker = found
            self.test_command = cmd
            self.test_command_discovered_from = marker
            self.stack = self.stack or marker.split(".")[0]
            log.info("Discovered test command: %s", cmd)

    @property
    def change_dir(self) -> Path:
        if self._change_dir_override:
            return self.specs_dir / self._change_dir_override
        # Normalize the change name so a hostile/careless --change value
        # (e.g. '../../foo') can't escape specs/changes/ — archive() will
        # shutil.move this directory, so an out-of-tree path is dangerous.
        from .change_manager import normalize_change_name
        return self.specs_dir / "changes" / normalize_change_name(self.change_name)

    @change_dir.setter
    def change_dir(self, value: Path | str | None) -> None:
        """Point `change_dir` at another change directory; None or "" restores
        the default. The target must be one directory directly under
        `specs/changes/` or `specs/archive/` (ValueError otherwise), the same
        bound the default path keeps through `normalize_change_name`."""
        if value is None or str(value) == "":
            self._change_dir_override = ""
            return
        specs = Path(self.specs_dir).resolve()
        target = (Path(self.specs_dir) / value).resolve()
        try:
            rel = target.relative_to(specs)
        except ValueError:
            raise ValueError(f"change_dir {value} is not inside {self.specs_dir}") from None
        if len(rel.parts) != 2 or rel.parts[0] not in ("changes", "archive") \
                or rel.parts[1].startswith("."):
            raise ValueError(
                f"change_dir {value} must be a directory directly under "
                f"{self.specs_dir / 'changes'} or {self.specs_dir / 'archive'}")
        self._change_dir_override = rel.as_posix()

    def _file_in_change_dir(self, name: str, what: str) -> Path:
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"{what} must be a file name inside the change directory, got {name!r}")
        return self.change_dir / name

    @property
    def research_path(self) -> Path:
        return self.change_dir / "research.md"

    @property
    def design_path(self) -> Path:
        return self.change_dir / "design.md"

    @property
    def tasks_path(self) -> Path:
        return self._file_in_change_dir(self.tasks_file, "tasks_file")

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
        return self._file_in_change_dir(self.state_file, "state_file")

    def progress_file_path(self) -> Path:
        return self.project_dir / "build-progress.md"

    def convention_files(self, changed_paths: Iterable[str] | None = None) -> list[Path]:
        """The `CLAUDE.md` chain that governs *changed_paths*, outermost first.

        Always the repo-root `CLAUDE.md` (this is the file every contributor is
        told to read), plus `CLAUDE.md` / `CLAUDE.local.md` in any directory
        that is an **ancestor** of a changed file. A directory's conventions
        doc governs files at or below it and nothing else — a sibling
        directory's `CLAUDE.md` is not a rule the change has to satisfy, and
        handing one to the reviewer manufactures violations out of rules that
        never applied.

        Paths may be absolute or project-relative (a git diff's are relative).
        Anything resolving outside the project dir is skipped: it has no
        ancestor chain here, and walking one would climb out of the repo.

        Both sides are resolved before the containment check. `relative_to` is
        purely lexical, so `../etc/x.py` under `/proj` "succeeds" and returns
        `../etc/x.py` — and the ancestor walk then reads `..` as a path part
        and collects the parent directory's `CLAUDE.md`, feeding rules from
        outside the repo to the reviewer as this project's conventions.
        """
        found: list[Path] = []

        def _collect(directory: Path) -> None:
            for name in ("CLAUDE.md", "CLAUDE.local.md"):
                candidate = directory / name
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)

        root = self.project_dir
        _collect(root)
        try:
            resolved_root = root.resolve()
        except OSError:  # unresolvable project dir — no chain to walk
            return found

        for entry in changed_paths or []:
            if not entry:
                continue
            path = Path(entry)
            path = path if path.is_absolute() else root / path
            try:
                relative = path.resolve().relative_to(resolved_root)
            except (ValueError, OSError):
                continue
            # Ancestors only, root-down and excluding the file itself, so the
            # ordering is general rule first, most specific rule last.
            directory = root
            for part in relative.parts[:-1]:
                directory = directory / part
                _collect(directory)
        return found

    def standards_text(self, changed_paths: Iterable[str] | None = None) -> str:
        """Concatenated contents of the coding-standards docs for the reviewer.

        Two independent sources, conventions first:

        * the `CLAUDE.md` chain governing *changed_paths* (`convention_files`) —
          automatic, tagged with its project-relative path so the reviewer can
          cite the rule it is quoting;
        * every `standards_files` entry (absolute, or relative to the project
          dir, then $CLAUDE_CONFIG_DIR/reference/dev/, then $CLAUDE_CONFIG_DIR)
          — the opt-in source, unchanged.

        Missing OR unreadable files are logged and skipped — one bad doc must
        not fail the block. Returns "" when nothing resolves — the review
        prompt then says "(no standards provided)".

        Each document is capped at :data:`STANDARDS_DOC_CHAR_LIMIT` on section
        boundaries (:func:`summarize_reference_doc`), because this text is
        rebuilt once per block and then fanned out to every panel member.
        """
        parts: list[str] = []
        for path in self.convention_files(changed_paths):
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError) as e:
                log.warning("Conventions file unreadable, skipping: %s (%s)", path, e)
                continue
            try:
                label = str(path.relative_to(self.project_dir))
            except ValueError:
                label = str(path)
            parts.append(
                f"### Conventions: {label}\n"
                + summarize_reference_doc(label, text, STANDARDS_DOC_CHAR_LIMIT)
            )
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
            parts.append(
                f"### Standard: {path.name}\n"
                + summarize_reference_doc(path.name, text, STANDARDS_DOC_CHAR_LIMIT)
            )
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
