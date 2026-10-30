"""Spec generator pipeline — buildme's planning artifacts, in dependency order.

Entry point: run_spec_generator(config, args)

The proposal, use cases and requirements are pm-spec's, marked ready in
`.change.yaml`; buildme checks they are there (`ChangeManager.missing_inputs`)
and never writes them. Flow:
1. Check the inputs
2. Generate design -> unknowns -> tasks -> rubric
3. Run the plan check (standalone)
4. Return exit code

A design that flags a requirement as infeasible or contradictory
(`SPEC_ISSUE:` lines, `design_spec_issues`), and a plan-check finding whose
fix needs a requirements change, raise `readiness.SpecIssuesFound`; the
orchestrator writes them to spec-issues.md for `pm-spec revise` and stops
(exit 4).
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

import yaml
from multiplai_core.specs import change_root

from build_pipeline.budget import BudgetExceededError

from . import budget
from .change_manager import (
    ARTIFACT_DAG,
    OLD_PLAN_FILE,
    PLAN_FILE,
    PLAN_HASH_KEY,
    ChangeManager,
    plan_path,
    requirement_texts,
)
from .dependencies import NewDependency, design_decisions_text, detect_new_dependencies
from .gates import unknowns_gate
from .io_utils import atomic_write
from .models import ArtifactStatus, BuildPhase, PriorAssessment
from .readiness import SpecIssuesFound
from .rubric import generate_rubric
from .state import PLAN_RULES_VERSION, BuildState, SpecGenState

log = logging.getLogger(__name__)


async def run_spec_generator(config, args=None, *, standalone: bool = True) -> int:
    """Main entry point for spec generation pipeline.

    Args:
        config: BuildConfig with project settings
        args: Optional CLI args namespace

    Returns:
        Exit code (0=success, 1=failure, 2=specs not ready, 4=spec issues
        written; a sub-phase raises `SpecIssuesFound` instead of returning 4)
    """
    print(f"PHASE: spec_generation — {config.change_name}")

    if standalone:
        budget.reset()
        budget.configure(
            max_tokens=config.budget_max_tokens, max_usd=config.budget_max_usd
        )
    cm = ChangeManager(config.specs_dir)
    change_dir = config.change_dir
    if standalone:
        from multiplai_core.specs import is_ready

        from .readiness import refuse_not_ready

        ready, reason = is_ready(change_dir)
        if not ready:
            return refuse_not_ready(config.change_name, reason)

    # Load or create state
    state = _load_or_create_state(config)
    if standalone:
        budget.get_budget().load_state(
            state.budget, accept_unknown_spend=config.accept_unknown_spend
        )

    # Ensure spec_gen sub-state exists
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
        state.checkpoint(config.state_file_path())

    try:
        # Generate artifacts in dependency order
        log.info("START phase=ARTIFACT_GENERATION change=%s", config.change_name)
        await _generate_all_artifacts(cm, change_dir, config, state)
        log.info("DONE phase=ARTIFACT_GENERATION")

        if standalone:
            from .llm_steps.plan_review_steps import run_plan_review_stage

            log.info("START phase=PLAN_REVIEW")
            print("PHASE: plan_review")
            await run_plan_review_stage(
                change_dir, config, state, config.state_file_path()
            )
            state.advance_to(BuildPhase.DESIGN_AUDIT, config.state_file_path())

        print("PHASE: spec_generation_complete")
        return 0

    except SpecIssuesFound as found:
        state.checkpoint(config.state_file_path())
        if not standalone:
            raise
        from .readiness import stop_on_issues

        return stop_on_issues(change_dir, found, change=config.change_name)
    except budget.BudgetExceededError:
        state.checkpoint(config.state_file_path())
        raise
    except Exception as e:
        state.checkpoint(config.state_file_path())
        log.error("FAIL phase=SPEC_GENERATION error=%s", e, exc_info=True)
        print(f"PHASE: spec_generation_failed — {e}")
        return 1
    except BaseException:
        state.checkpoint(config.state_file_path())
        raise


async def _generate_all_artifacts(
    cm: ChangeManager,
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Generate all artifacts in dependency order, with resume support.

    Refuses (RuntimeError) when a required input is missing: buildme never
    writes the proposal, use cases or requirements."""
    missing = cm.missing_inputs(change_dir)
    if missing:
        raise RuntimeError(
            f"Required inputs missing in {change_dir}: {', '.join(missing)}. pm-spec writes them: "
            f"Run /multiplai-pm:pm-spec {config.change_name} first"
        )
    reconcile_plan(config, state)
    max_iterations = len(ARTIFACT_DAG) * 2  # safety limit
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        status = cm.artifact_status(
            change_dir, state.spec_gen.stale_artifacts if state.spec_gen else []
        )
        ready = [
            name for name, outcome in status.items() if outcome == ArtifactStatus.READY
        ]

        if not ready:
            # Check if all done
            status = cm.artifact_status(
                change_dir, state.spec_gen.stale_artifacts if state.spec_gen else []
            )
            if all(s == ArtifactStatus.DONE for s in status.values()):
                log.info("All artifacts generated")
                break
            # Deadlock — some artifacts can't be created
            blocked = [a for a, s in status.items() if s == ArtifactStatus.BLOCKED]
            log.error("Deadlock: blocked artifacts %s with nothing ready", blocked)
            raise RuntimeError(f"Artifact generation deadlocked: {blocked}")

        for artifact_id in ready:
            log.info("START artifact=%s", artifact_id)
            await _generate_single_artifact(
                cm,
                change_dir,
                artifact_id,
                config,
                state,
            )
            log.info("DONE artifact=%s", artifact_id)

    # Unknowns-gate resume durability: the gate runs after unknowns.md is written, so a crash mid-gate leaves
    # the artifact DONE and the DAG loop never re-enters it. The checkpoint,
    # not file existence, is the record that the gate ran.
    if state.spec_gen and not state.spec_gen.step_passed("explainers"):
        unknowns_path = change_dir / "unknowns.md"
        if not getattr(config, "explainers_active", True):
            # --skip-explainers on a resumed build: never run detection or the
            # gate's LLM regeneration — that is exactly what the flag exists
            # to prevent. Write the skip-marker document when the crash
            # predates it (mirroring _generate_unknowns) so the DAG stays
            # satisfiable.
            log.info("SKIP phase=UNKNOWNS_GATE reason=explainers-disabled")
            if not unknowns_path.exists():
                unknowns_path.write_text(
                    f"{UNKNOWNS_HEADER}\n{EXPLAINERS_SKIPPED_LINE}\n"
                )
            _record_explainer_outcome(config, state)
            state.checkpoint(config.state_file_path())
        elif unknowns_path.exists():
            log.info(
                "Unknowns gate not recorded complete — running it now "
                "(resume durability)"
            )
            deps = detect_new_dependencies(change_dir, config.project_dir)
            if deps:
                await _audit_unknowns(
                    change_dir,
                    cm.artifact_context(change_dir, "unknowns"),
                    config,
                    unknowns_path,
                    deps,
                )
            _record_explainer_outcome(config, state)
            state.checkpoint(config.state_file_path())
    elif state.spec_gen:
        log.info("SKIP phase=UNKNOWNS_GATE reason=recorded-complete-in-state")

    reconcile_plan(config, state)
    if state.spec_gen and "rubric" in state.spec_gen.stale_artifacts:
        await _generate_rubric(change_dir, config, state)
        reconcile_plan(config, state, regenerated={"rubric"})

    # Verify completeness
    final_status = cm.artifact_status(
        change_dir, state.spec_gen.stale_artifacts if state.spec_gen else []
    )
    done_count = sum(1 for s in final_status.values() if s == ArtifactStatus.DONE)
    log.info("Artifact generation complete: %d/%d done", done_count, len(ARTIFACT_DAG))
    if done_count != len(ARTIFACT_DAG):
        raise RuntimeError("Planning artifacts are incomplete")


async def _generate_single_artifact(
    cm: ChangeManager,
    change_dir: Path,
    artifact_id: str,
    config,
    state: BuildState,
) -> None:
    """Generate a single artifact: design, unknowns, tasks or rubric."""
    context = cm.artifact_context(change_dir, artifact_id)

    # Thread the explainers into the task breakdown: the edge cases are only
    # worth writing down if the blocks that touch them name them as acceptance
    # criteria (which is what turns them into tests).
    if artifact_id == "tasks":
        context["unknowns_content"] = read_unknowns(change_dir)

    if artifact_id == "unknowns":
        await _generate_unknowns(cm, change_dir, config, state)
    elif artifact_id == "rubric":
        await _generate_rubric(change_dir, config, state)
    else:
        from .llm_steps.spec_steps import generate_artifact

        content = await generate_artifact(
            artifact_id,
            context,
            config,
            research=read_research(state),
            codebase_analysis=read_codebase_analysis(state, config),
            reference_docs=config.reference_docs_text(),
        )

        # A design that names a requirement it cannot be built to is not
        # published: the requirements go back to pm-spec first.
        if artifact_id == "design":
            raise_spec_issues(design_spec_issues(content), "design")
        # Write the artifact
        output_path = change_dir / context["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(output_path, content)
        log.info("Wrote artifact: %s", output_path)

    reconcile_plan(config, state, regenerated={artifact_id})
    if state.spec_gen and artifact_id == "unknowns":
        _record_explainer_outcome(config, state)

    # Mark completed. `completed_artifacts` is a resume ledger keyed by id, so
    # a re-generated artifact must not be entered twice.
    if state.spec_gen:
        if artifact_id not in state.spec_gen.completed_artifacts:
            state.spec_gen.completed_artifacts.append(artifact_id)
        state.checkpoint(config.state_file_path())

    print(f"PHASE: artifact_{artifact_id}_complete")


UNKNOWNS_HEADER = "# Unknowns — what we are about to depend on\n"
NO_NEW_DEPENDENCIES_LINE = "No dependencies new to this project."
EXPLAINERS_SKIPPED_LINE = (
    "Explainers skipped (--skip-explainers / explainers.enabled: false)."
)


def read_unknowns(change_dir: Path) -> str:
    """unknowns.md's text, or a placeholder when it hasn't been written."""
    path = change_dir / "unknowns.md"
    if path.exists():
        return path.read_text()
    return "(no unknowns document)"


def read_codebase_analysis(state: BuildState, config=None) -> str:
    """The CODEBASE_ANALYSIS phase's report, as text for the design prompt.

    The checkpoint stores a *path* (`spec_gen.codebase_analysis_path`); the
    prompt needs the contents. A path that no longer resolves falls back to
    the stored string rather than dropping it, and an absent one returns ""
    so the prompt says "(new project)" exactly as it did before the phase
    existed.

    With `planning.analysis: component` in *config*, the report is cut to the
    facts about the files the component's plan.md and specs name, plus every
    convention, and its "Not analysed" list is dropped
    (`code_analysis.component_facts`). The default, `full`, returns the whole
    report.
    """
    raw = (state.spec_gen.codebase_analysis_path if state.spec_gen else "") or ""
    if not raw:
        return ""
    path = Path(raw)
    if path.is_file():
        text = path.read_text()
        if config is not None and getattr(config, "planning_analysis", "full") == (
            "component"
        ):
            from .code_analysis import component_facts

            return component_facts(text, _plan_and_specs_text(config.change_dir))
        return text
    log.warning("Recorded codebase analysis is not a readable file: %s", raw)
    return raw


def _plan_and_specs_text(change_dir: Path) -> str:
    """The component's plan.md and its requirements, as one text to find file names in."""
    plan = Path(change_dir) / "plan.md"
    parts = [plan.read_text()] if plan.is_file() else []
    parts += requirement_texts(Path(change_dir)).values()
    return "\n".join(parts)


def read_research(state: BuildState) -> str:
    """The research file's *content*, fenced as untrusted, for the proposal prompt.

    The checkpoint stores a path (`state.research_path`); the prompt's
    `## Research Findings` slot needs what the file says. Research is written
    from web pages and other external text, so it goes in an untrusted-content
    fence: data for the proposal, never instructions to it. No path recorded
    returns "" (the prompt then says "(no research conducted)"); a recorded
    path that is not a readable file warns and returns "(none provided)".
    """
    raw = state.research_path or ""
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_file():
        log.warning("Recorded research is not a readable file: %s", raw)
        return "(none provided)"
    from multiplai_core.untrusted import fence

    return "\n".join(fence(path.read_text(), source=path.name)) or "(none provided)"


def _dependency_list_text(deps: list[NewDependency]) -> str:
    return (
        "\n".join(
            f"- `{d.name}` — named in {', '.join(d.mentioned_in) or 'the specs'}; {d.evidence}"
            for d in deps
        )
        or "(none)"
    )


def _record_explainer_outcome(config, state: BuildState) -> None:
    """Advisory explanations never impersonate missing required evidence."""
    if state.spec_gen is None:
        return
    if not getattr(config, "explainers_active", True):
        state.spec_gen.record_step("explainers", "skipped", "disabled")
        return
    deps = detect_new_dependencies(config.change_dir, config.project_dir)
    result = unknowns_gate((config.change_dir / "unknowns.md").read_text(), deps)
    state.spec_gen.record_step(
        "explainers", "passed" if result.passed else "advisory", result.reason
    )


async def _generate_unknowns(
    cm: ChangeManager,
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Write unknowns.md — one explainer per dependency new to this project.

    B1: before the build depends on a new tool/library/service, write down its
    contract and its edge cases (the Whisper-silence class of surprise). One
    concurrent LLM call per detected dependency, then the structural gate with
    its single regeneration pass.

    Both "nothing new" and "explainers switched off" still write the file: the
    artifact DAG stays satisfied and the absence is a recorded finding rather
    than a silent skip.
    """
    from .llm_steps.spec_steps import run_explainer

    output_path = change_dir / "unknowns.md"

    if not getattr(config, "explainers_active", True):
        log.info("SKIP phase=EXPLAINERS reason=disabled")
        print("PHASE: explainers_skipped")
        output_path.write_text(f"{UNKNOWNS_HEADER}\n{EXPLAINERS_SKIPPED_LINE}\n")
        if state.spec_gen:
            _record_explainer_outcome(config, state)
        return

    log.info("START phase=EXPLAINERS")
    print("PHASE: explainers")
    deps = detect_new_dependencies(change_dir, config.project_dir)

    if not deps:
        log.info("DONE phase=EXPLAINERS dependencies=0")
        output_path.write_text(
            f"{UNKNOWNS_HEADER}\n{NO_NEW_DEPENDENCIES_LINE}\n\n"
            "The proposal's Impact section and the design's Decisions section "
            "name nothing that is absent from this project's manifests.\n"
        )
        print("PHASE: explainers_complete — 0 new dependencies")
        if state.spec_gen:
            _record_explainer_outcome(config, state)
        return

    log.info(
        "Explaining %d new dependencies: %s",
        len(deps),
        ", ".join(d.name for d in deps),
    )
    usage_context = design_decisions_text(change_dir)
    results = await asyncio.gather(
        *[run_explainer(dep, config, usage_context=usage_context) for dep in deps],
        return_exceptions=True,
    )

    sections: list[str] = []
    for dep, result in zip(deps, results):
        if isinstance(result, BudgetExceededError) or (
            isinstance(result, BaseException) and not isinstance(result, Exception)
        ):
            raise result
        if isinstance(result, BaseException):
            # A failed explainer is a recorded hole, not a silent one — the gate
            # sees the empty section and drives the regeneration pass.
            log.warning("Explainer failed for %s: %s", dep.name, result)
            sections.append(
                f"## {dep.name}\n\n"
                f"### What it is\n(explainer call failed: {result})\n\n"
                f"### The contract we rely on\n(not written)\n\n"
                f"### Edge cases & failure modes\n\n"
                f"### Assumptions we are making\n\n"
                f"### How we would find out cheaply\n(not written)\n"
            )
        else:
            sections.append(str(result).strip())

    output_path.write_text(
        UNKNOWNS_HEADER + "\n" + "\n\n".join(sections).strip() + "\n"
    )
    log.info("Wrote artifact: %s", output_path)

    context = cm.artifact_context(change_dir, "unknowns")
    await _audit_unknowns(change_dir, context, config, output_path, deps)
    if state.spec_gen:
        _record_explainer_outcome(config, state)


async def _audit_unknowns(
    change_dir: Path,
    context: dict,
    config,
    output_path: Path,
    deps: list[NewDependency],
) -> None:
    """Gate unknowns.md; regenerate ONCE when a required list came back empty.

    The gate's findings are injected into exactly one regeneration call. A regenerated document that
    still fails is logged and stands — there is no re-audit loop, so a
    stubbornly incomplete explainer costs one extra call, not an unbounded
    number.
    """
    from .llm_steps.spec_steps import generate_artifact

    log.info("START phase=UNKNOWNS_GATE")
    print("PHASE: unknowns_gate")
    result = unknowns_gate(output_path.read_text(), deps)
    if result.passed:
        log.info("DONE phase=UNKNOWNS_GATE findings=0")
        return

    findings = result.metadata.get("findings", [])
    log.warning(
        "DONE phase=UNKNOWNS_GATE findings=%d — one regeneration pass",
        len(findings),
    )
    for finding in findings:
        log.warning("  finding %s", finding)

    context = dict(context)
    context["current_unknowns"] = output_path.read_text()
    context["dependency_list"] = _dependency_list_text(deps)
    findings_text = "\n".join(f"- {f}" for f in findings) or result.reason

    try:
        content = await generate_artifact(
            "unknowns",
            context,
            config,
            audit_findings=findings_text,
        )
    except BudgetExceededError:
        raise
    except Exception as regen_err:
        log.warning(
            "Unknowns regeneration failed (non-fatal, first pass stands): %s",
            regen_err,
        )
        print(f"PHASE: unknowns_regeneration_failed — {regen_err}")
        return

    atomic_write(output_path, content)
    log.info("Rewrote artifact after unknowns gate: %s", output_path)
    print(f"PHASE: unknowns_regenerated — {len(findings)} findings")

    # Report-only re-check: no second regeneration, so the pass count is fixed
    # at one no matter how incomplete the document remains.
    recheck = unknowns_gate(content, deps)
    if not recheck.passed:
        log.warning(
            "Unknowns still incomplete after the single regeneration pass "
            "(accepted, no loop): %s",
            recheck.reason,
        )
        print("PHASE: unknowns_still_incomplete — accepted after one pass")


# Placeholder text that defers specification to the implementer. Deterministic
# counterpart to the audit prompt's Placeholders check — the regex always runs,
# even when the LLM audit errors out.
_PLACEHOLDER_RE = re.compile(
    r"\bTBD\b|\bTODO\b|add appropriate error handling|similar to block \d+",
    re.IGNORECASE,
)


def scan_placeholders(text: str) -> list[dict]:
    """Deterministic TBD/TODO scan over a generated artifact. Returns audit-
    finding dicts (same shape as the LLM audit) — non-empty triggers the
    single tasks regeneration pass."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _PLACEHOLDER_RE.search(line):
            findings.append(
                {
                    "category": "placeholder",
                    "severity": "major",
                    "description": f"Placeholder text on line {lineno}: {line.strip()[:120]}",
                    "suggestion": "Replace with the concrete content — exact names, "
                    "signatures, literal values (repeat code verbatim "
                    "rather than referencing another block).",
                }
            )
    return findings


# What a block calls its own work: "these", "this slice", "the criteria".
# Never a noun of its own ("Authentication is already handled by Block 1's
# middleware"): that is a side concern another block covers, which says
# nothing about whether this block adds behaviour.
_BLOCK_ITSELF = (
    r"(?:these|they|them|this(?:\s+(?:block|slice|task|step|work))?"
    r"|(?:all\s+(?:of\s+)?)?(?:these|the)\s+(?:criteria|scenarios|cases"
    r"|requirements|behaviou?rs?|checks|assertions))"
)
# A block that says, in its own words, that an earlier block already does its
# job: "these are satisfied by Block 1", "this slice is satisfied by Block 1",
# "Block 1's implementation already handles these". Phrases that only say the
# block USES something an earlier block built ("the tokenizer already built in
# Block 1") are deliberately not here: that is how an ordinary leaf block reads.
_SATISFIED_EARLIER_RE = re.compile(
    rf"\b{_BLOCK_ITSELF}\s+(?:is|are|was|were|has\s+been|have\s+been)\s+"
    r"(?:already\s+)?(?:satisfied|implemented|delivered|built|covered|handled"
    r"|met)\b[^.\n]{0,60}\bBlock\s+\d+"
    r"|(?:^|(?<=\.))\s*(?:already\s+)?(?:satisfied|covered|delivered)\s+by\b"
    r"[^.\n]{0,60}\bBlock\s+\d+"
    r"|\bBlock\s+\d+(?:'s\s+\w+)?\s+already\s+"
    rf"(?:satisfies|covers|meets|handles|implements|delivers)\s+{_BLOCK_ITSELF}\b",
    re.IGNORECASE | re.MULTILINE,
)
# A block that says it writes no code. Not "no code change to the core": a
# "no code" scoped to anything but this block is about another part of it.
# The short form ("no new code") leaves "no new code changes/needed/…" to
# the first, so its scope is checked there.
_NO_CODE_RE = re.compile(
    r"(?:\b(?:no|without)\s+(?:new\s+|production\s+|source\s+|further\s+)?"
    r"(?:code|implementation)\s+(?:changes?|is\s+(?:needed|required)|needed"
    r"|required|work)\b"
    r"|\b(?:needs|requires)\s+no\s+(?:new\s+)?(?:code|implementation)\b"
    r"|\bno\s+(?:new|production|source)\s+code\b"
    r"(?!\s+(?:changes?|is\s+(?:needed|required)|needed|required|work)\b))"
    r"(?!\s+(?:to|in|for|on|outside|beyond)\s+(?!this\b|these\b|here\b))",
    re.IGNORECASE,
)
# Test work: "Add tests for tabs", "Write assertions", "Assert …", "Tests for …".
_TEST_WORK_RE = re.compile(
    r"\b(?:add|write|extend|create|expand)\s+(?:[\w-]+\s+){0,3}?"
    r"(?:tests?|test\s+cases?|assertions?)\b",
    re.IGNORECASE,
)
_TEST_WORK_START_RE = re.compile(
    r"^\W*(?:(?:add|write|extend|create|expand)\s+(?:[\w-]+\s+){0,3}?"
    r"(?:tests?|test\s+cases?|assertions?)\b|assert\b|tests?\b|coverage\b)",
    re.IGNORECASE,
)
_ADD_FILE_START_RE = re.compile(
    r"^\W*(?:add|write|extend|create)\s+`([^`]+)`",
    re.IGNORECASE,
)
# A backticked file path: no spaces or brackets, and a directory or a known
# file extension (so `textkit.summary.summarize` and `str.split` are not paths).
_PATH_TOKEN_RE = re.compile(r"`([\w./-]+)`")
_PATH_EXTENSIONS = frozenset(
    [
        "py",
        "pyi",
        "js",
        "jsx",
        "ts",
        "tsx",
        "mjs",
        "cjs",
        "go",
        "rs",
        "rb",
        "java",
        "kt",
        "kts",
        "swift",
        "c",
        "h",
        "cc",
        "cpp",
        "hpp",
        "cs",
        "php",
        "scala",
        "sh",
        "bash",
        "sql",
        "html",
        "htm",
        "css",
        "scss",
        "vue",
        "svelte",
        "toml",
        "json",
        "yaml",
        "yml",
        "cfg",
        "ini",
        "md",
        "txt",
        "lock",
        "xml",
        "gradle",
    ]
)
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|spec|__tests__)/"
    r"|(?:^|/)(?:test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(?:test|spec)\.[^/]+|conftest\.py)$"
)


def _named_paths(text: str) -> list[str]:
    paths = []
    for token in _PATH_TOKEN_RE.findall(text):
        name = token.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ("/" in token and not token.startswith("/")) or ext in _PATH_EXTENSIONS:
            paths.append(token)
    return paths


def _left_unchanged(path: str, text: str) -> bool:
    """The block says it leaves this file alone."""
    p = re.escape(path)
    return bool(
        re.search(rf"`{p}`[^.;\n]{{0,40}}\bunchanged\b", text, re.IGNORECASE)
        or re.search(
            rf"\b(?:no\s+changes?\s+to|(?:do\s+not|don't|never)\s+"
            rf"(?:modify|change|touch|edit))\s+(?:the\s+)?`{p}`",
            text,
            re.IGNORECASE,
        )
    )


def _block_bodies(text: str) -> dict[int, tuple[str, str]]:
    """Each block's heading and text, prose AND checkboxes, without its
    Interfaces and Satisfies lines. ``parse_blocks`` keeps only the checkbox
    lines of a checkbox block, which drops the prose where a block says what
    it is."""
    header = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)
    matches = list(header.finditer(text))
    bodies = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        body = re.split(r"^Interfaces:\s*$", body, maxsplit=1, flags=re.MULTILINE)[0]
        body = re.sub(r"^Satisfies:.*$", "", body, flags=re.MULTILINE)
        bodies[int(match.group(1))] = (match.group(2), body)
    return bodies


def _first_instruction(body: str) -> str:
    """The first sentence of a block's first line, without a checkbox or an
    item number (`- [ ] 2.1 `)."""
    for line in body.splitlines():
        line = re.sub(
            r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?:\d+(?:\.\d+)*\.?\s+)?", "", line
        ).strip()
        if line:
            return re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    return ""


def _is_test_work(heading: str, body: str) -> bool:
    """The block says it is test work: its heading names a test file or opens
    with tests, or its first instruction adds tests or a test file. A block
    whose tests are a side note ("Tests in `tests/test_cli.py`") is not."""
    first = _first_instruction(body)
    added = _ADD_FILE_START_RE.match(first)
    heading_paths = _named_paths(heading)
    return bool(
        (heading_paths and all(_TEST_PATH_RE.search(p) for p in heading_paths))
        or _TEST_WORK_START_RE.match(heading)
        or _TEST_WORK_START_RE.match(first)
        or (added and _TEST_PATH_RE.search(added.group(1)))
    )


# A step that writes code. A test-first block writes its tests and then its
# code ("- [ ] 2.2 Implement HMAC verification", "…, then implement signature
# checking", "Make count split on tabs"), often without naming its source file.
# A check is not code ("Confirm", "Verify", "Assert", "Make sure", "Make no
# change"): a block whose steps only test and check is the block the scan
# exists to catch.
_CODE_VERB_RE = re.compile(
    r"^(?:implement|make(?!\s+(?:no|sure)\b)|build|wire|handle|raise|return"
    r"|reject|accept|support|change(?!\s+nothing\b)|update|modify|fix"
    r"|refactor|compute|calculate|parse|split|emit|print|output|store|save"
    r"|persist|load|define|register|expose|convert|validate|enforce|route"
    r"|send|replace|rename|move|remove|delete|strip|normali[sz]e|format"
    r"|render|generate|introduce|extract|guard|cache|append|insert|sort"
    r"|filter|merge|(?:de)?seriali[sz]e|encode|decode|hash|sign|wrap"
    r"|add|write|create|extend|expand)\b",
    re.IGNORECASE,
)
# "Add", "Write" and the like write code unless what they add is a test.
_WRITE_VERB_RE = re.compile(r"^(?:add|write|create|extend|expand)\b", re.IGNORECASE)
_TEST_WORD_RE = re.compile(
    r"\b(?:tests?|assert\w*|fixtures?|parametri[sz]\w*)\b",
    re.IGNORECASE,
)
# What a step is for ("… so the tests pass", "… until they pass") says nothing
# about what the step changes, so it is left out of the step.
_PURPOSE_RE = re.compile(r"\s+(?:so(?:\s+that)?|until|to\s+make)\b.*$", re.IGNORECASE)
# A step whose verb acts on a test, a test case or a file (in its first few
# words): "Update `tests/test_words.py` with tab cases", "Fix the flaky test".
_TEST_OBJECT_RE = re.compile(
    r"^\w+\s+(?:[\w-]+\s+){0,3}?(?:(?:tests?|test\s+cases?|assertions?"
    r"|fixtures?)\b|`([^`]+)`)",
    re.IGNORECASE,
)
# "Add a case for an empty string": a case an add/write step adds is a test.
# Not "Add case-insensitive matching" or "Add snake case conversion": there
# "case" describes the code.
_CASE_OBJECT_RE = re.compile(
    r"^\w+\s+(?:[\w-]+\s+){0,3}?cases?"
    r"(?=\s+(?:for|where|when|that|with|covering|to|in|of)\b|\s*[,.;:]|\s*$)",
    re.IGNORECASE,
)
# "Make the failing tests pass", "Fix the failing tests": the code a
# test-first block writes, named by the tests it makes pass.
_PASS_TESTS_RE = re.compile(
    r"^(?:make|fix)\b.*\b(?:failing\s+(?:tests?|test\s+cases?|assertions?)"
    r"|(?:tests?|test\s+cases?|assertions?)\s+pass)\b",
    re.IGNORECASE,
)
# Steps: sentences, clauses after ";", and "…, then …".
_STEP_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|,?\s+(?:and\s+)?then\s+", re.IGNORECASE)
_STEP_LEAD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(?:(?:then|and|also|finally|next|first)\b[,\s]*)*",
    re.IGNORECASE,
)


def _has_code_step(body: str) -> bool:
    """Some step of the block, prose or checkbox, writes code: it opens with
    a verb that changes code, that verb does not act on a test or a test
    file (making failing tests pass is code), and an "add"/"write" step adds
    something that is not a test."""
    for line in body.splitlines():
        for step in _STEP_SPLIT_RE.split(line):
            step = _PURPOSE_RE.sub("", _STEP_LEAD_RE.sub("", step).strip())
            if not _CODE_VERB_RE.match(step):
                continue
            target = _TEST_OBJECT_RE.match(step)
            if (
                target
                and not _PASS_TESTS_RE.match(step)
                and (target.group(1) is None or _TEST_PATH_RE.search(target.group(1)))
            ):
                continue
            if _WRITE_VERB_RE.match(step) and (
                _TEST_WORD_RE.search(step)
                or _CASE_OBJECT_RE.match(step)
                or any(_TEST_PATH_RE.search(p) for p in _named_paths(step))
            ):
                continue
            return True
    return False


def scan_no_behaviour_blocks(tasks_path: Path) -> list[dict]:
    """Deterministic scan for blocks that add no behaviour.

    Such a block's tests pass before it is built, so the TDD RED gate fails it
    and the build with it. A block is flagged only when it produces nothing
    (`Produces: (none)`) AND its own text says it adds no code:
    - it says an earlier block already satisfies it, with itself as the
      subject ("these are satisfied by Block 1"; never a side concern such as
      "authentication is already handled by Block 1's middleware");
    - it says it needs no code, unscoped ("no code change", never "no code
      change to the core");
    - it names no file it changes but test files and is test work (its
      heading or first instruction adds tests or a test file; a leaf block
      need not name its source file, so "Tests in `tests/x.py`" is not
      enough), and no step of it writes code;
    - every file it names is one it leaves unchanged, it adds tests, and no
      step of it writes code.
    A step writes code when it opens with a verb that changes code
    ("Implement …", "Make count split on tabs", "then reject …", "Add a
    `--summary` flag") and that verb does not act on a test or a test file
    ("Update `tests/test_words.py`", "Add a case for …"; a closing "so the
    tests pass" is not what it acts on), so a test-first block (tests, then
    the code) is never flagged on its test file.
    `Produces: (none)` alone only says no later block calls it, and "Verify" or
    "Confirm" is an ordinary verb for new behaviour (verify a signature), so
    neither is ever enough. A block that names no file is never flagged on
    files. The suggestion tells the rewrite to keep a block that does add
    behaviour and name its code, so a false positive costs one rewrite, never
    the block. Returns audit-finding dicts (same shape as
    ``scan_placeholders``, plus the block's ``block`` number and ``name``).
    """
    from .tdd_engine import parse_blocks

    if not tasks_path.exists():
        return []
    bodies = _block_bodies(tasks_path.read_text())
    findings = []
    for block in parse_blocks(tasks_path):
        if block.produces:
            continue
        heading, body = bodies.get(block.number, (block.name, block.description))
        text = f"{heading}\n{body}"
        paths = _named_paths(text)
        changed = [p for p in paths if not _left_unchanged(p, text)]
        if _SATISFIED_EARLIER_RE.search(text):
            why = "it says an earlier block already satisfies it"
        elif _NO_CODE_RE.search(text):
            why = "it says it needs no code"
        elif (
            not paths
            or any(not _TEST_PATH_RE.search(p) for p in changed)
            or _has_code_step(body)
        ):
            continue
        elif changed and _is_test_work(heading, body):
            why = (
                "it is test work and names no code to write (every file it "
                "changes is a test file)"
            )
        elif not changed and _TEST_WORK_RE.search(text):
            why = (
                "it adds tests and names no code to write (every file it "
                "names is one it leaves unchanged)"
            )
        else:
            continue
        source = re.search(r"Block\s+(\d+)", " ".join(block.consumes))
        home = (
            f"Block {source.group(1)}" if source else "the earlier block that builds it"
        )
        findings.append(
            {
                "category": "no-new-behaviour",
                "severity": "critical",
                "block": block.number,
                "name": block.name,
                "description": (
                    f'Block {block.number} "{block.name}" adds no behaviour: it '
                    f"produces nothing and {why}, so its tests would pass before "
                    "it is built and the TDD RED gate would fail the build."
                ),
                "suggestion": (
                    f"If {home} already delivers these criteria, move them and "
                    f"their tests into {home} and remove this block. If this block "
                    "does add behaviour, keep it and name the code it changes and "
                    "what that code does, so its tests fail until it is built."
                ),
            }
        )
    return findings


# The prefix of each step's finding ids: `<prefix>-F<n>`.
FINDING_ID_PREFIXES = {"plan_review": "PR"}

# The evidence a listed finding from the deterministic scans gets when the
# rewritten tasks pass every scan.
SCANS_PASS_EVIDENCE = "The deterministic scans find nothing in the rewritten tasks."


def _finding_text(finding: dict) -> str:
    if finding.get("text"):
        return str(finding["text"])
    return str(finding.get("description", "")) + (
        f" Fix: {finding['suggestion']}" if finding.get("suggestion") else ""
    )


def number_findings(step: str, findings: list[dict], *, start: int = 1) -> list[dict]:
    """Copies of the findings a rewrite is asked to fix, each with an id
    `<prefix>-F<n>` (`FINDING_ID_PREFIXES`) and the text the rewrite and the
    recheck are shown."""
    prefix = FINDING_ID_PREFIXES[step]
    return [
        {
            **f,
            "finding_id": f"{prefix}-F{n}",
            "severity": f.get("severity", "?"),
            "category": f.get("category", ""),
            "text": _finding_text(f),
        }
        for n, f in enumerate(findings, start)
    ]


def next_finding_number(listed: list[dict]) -> int:
    """The number after the highest `-F<n>` id among *listed*, so a later
    rewrite's new findings never reuse an earlier one's id."""
    numbers = [
        int(m.group(1))
        for f in listed
        if (m := re.search(r"-F(\d+)$", str(f.get("finding_id", ""))))
    ]
    return max(numbers, default=0) + 1


# The budget labels of planning's model calls, which `planning.max_usd` caps:
# the design, tasks and unknowns writers (`spec:<artifact>`), the rewrite's
# edits, the plan check, the rubric and the explainers.
PLANNING_LABELS = ("plan_rewrite", "plan_review", "rubric", "explainer")


def planning_spend() -> float:
    """What this build's planning calls have cost so far, in USD."""
    return sum(
        usd
        for label, usd in budget.get_budget().usd_by_label.items()
        if label.startswith("spec:") or label in PLANNING_LABELS
    )


def saved_findings(findings: list[dict]) -> list[dict]:
    """The fields of each finding a checkpoint keeps in `repair_findings`.
    `source` is `scan` for a finding of the deterministic scans."""
    keys = (
        "finding_id",
        "severity",
        "category",
        "text",
        "source",
        "status",
        "evidence",
    )
    return [{k: f[k] for k in keys if k in f} for f in findings]


def from_scans(findings: list[dict]) -> list[dict]:
    """Copies of the deterministic scans' findings, marked `source: scan`."""
    return [{**f, "source": "scan"} for f in findings]


def model_listed(listed: list[dict]) -> list[dict]:
    """The listed findings a recheck model is asked about: those not from the
    deterministic scans, which the scans themselves recheck."""
    return [f for f in listed if f.get("source") != "scan"]


def scan_assessments(
    listed: list[dict], deterministic: list[dict]
) -> list[PriorAssessment]:
    """`resolved` for each listed finding of the deterministic scans when the
    scans find nothing in the rewritten plan; none otherwise, so they count as
    `cannot_verify`."""
    if deterministic:
        return []
    return [
        PriorAssessment(
            finding_id=f["finding_id"], status="resolved", evidence=SCANS_PASS_EVIDENCE
        )
        for f in listed
        if f.get("source") == "scan"
    ]


def assess_listed(listed: list[dict], assessments) -> list[dict]:
    """Each listed finding with the recheck's status and evidence. A finding
    the recheck gave no status is `cannot_verify`, never resolved, as in
    `review_rounds.complete_assessments`."""
    given = {a.finding_id: a for a in assessments or []}
    return [
        {
            **f,
            "status": given[f["finding_id"]].status
            if f["finding_id"] in given
            else "cannot_verify",
            "evidence": given[f["finding_id"]].evidence
            if f["finding_id"] in given
            else "",
        }
        for f in listed
    ]


def _new_finding(finding: dict) -> dict:
    return {
        "finding_id": "",
        "severity": finding.get("severity", "?"),
        "category": finding.get("category", ""),
        "text": _finding_text(finding),
        "status": "new",
    }


def recheck_verdict(
    listed: list[dict],
    assessments,
    new_findings: list[dict],
    *,
    deterministic: list[dict],
) -> tuple[list[dict], list[dict]]:
    """What a recheck after a rewrite blocks on, and what it only reports.

    Blocking: a listed critical finding whose status is not `resolved` (no
    status is `cannot_verify`); a listed major finding that is
    `still_present`; a new critical finding; and every finding of the
    deterministic scans. A listed major finding that is `partially_resolved`
    or `cannot_verify`, a listed minor one, and every other new finding —
    major or minor, in any category, `no-new-behaviour` included — are
    advisory, as SKILL.md says. A resolved listed finding is in neither list.
    """
    blocking = []
    advisory = []
    for f in assess_listed(listed, assessments):
        if f["status"] == "resolved":
            continue
        blocks = f.get("severity") == "critical" or (
            f.get("severity") == "major" and f["status"] == "still_present"
        )
        (blocking if blocks else advisory).append(f)
    blocking += [_new_finding(f) for f in deterministic]
    for finding in new_findings:
        blocks = finding.get("severity") == "critical"
        (blocking if blocks else advisory).append(_new_finding(finding))
    return blocking, advisory


def findings_report(findings: list[dict]) -> str:
    """One line per finding: its id when it has one, its status (`new` for a
    finding the recheck raised, `open` for one no recheck has assessed), its
    severity and its text."""
    return "\n".join(
        "- "
        + (f"{f['finding_id']} " if f.get("finding_id") else "")
        + f"{f.get('status') or 'open'} [{f.get('severity', '?')}] "
        f"({f.get('category') or '?'}) {f.get('text') or _finding_text(f)}"
        + (f" Evidence: {f['evidence']}" if f.get("evidence") else "")
        for f in findings
    )


def log_findings(step: str, label: str, findings: list[dict]) -> None:
    """Log each finding a recheck acted on, one warning line each."""
    for f in findings:
        log.warning(
            "%s %s finding id=%s status=%s severity=%s text=%s",
            step,
            label,
            f.get("finding_id") or "-",
            f.get("status") or "open",
            f.get("severity", "?"),
            (f.get("text") or _finding_text(f))[:300],
        )


def record_advisory(config, name: str, advisory: list[dict]) -> None:
    """Write a recheck's advisory findings to the log and the progress file,
    under `<STEP>_ADVISORY`. They block nothing."""
    if not advisory:
        return
    log_findings(name, "advisory", advisory)
    from .progress import ProgressWriter

    path = config.progress_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ProgressWriter(path).log_phase(
        name.upper() + "_ADVISORY", findings_report(advisory)
    )


class PlanRepairRejected(ValueError):
    """`rewrite_plan` refused to publish: the rewrite still has blocking
    findings. Carries them, the advisory ones, and the listed findings with
    their statuses."""

    def __init__(
        self,
        message: str,
        *,
        blocking: list[dict],
        advisory: list[dict] | None = None,
        assessments: list[dict] | None = None,
    ):
        super().__init__(message)
        self.blocking = blocking
        self.advisory = advisory or []
        self.assessments = assessments or []


def design_spec_issues(text: str) -> list[tuple[str, str]]:
    """The design step's `SPEC_ISSUE:` lines, as `(title, body)` issues for
    spec-issues.md: the title names the requirements file and requirement the
    line gives, the body is the line. Empty when there is none."""
    from .prompts.spec_generation import SPEC_ISSUE_PREFIX

    issues = []
    for line in text.splitlines():
        stripped = line.strip().strip("`").strip()
        if not stripped.startswith(SPEC_ISSUE_PREFIX):
            continue
        detail = stripped[len(SPEC_ISSUE_PREFIX) :].strip()
        if not detail:
            continue
        parts = [p.strip() for p in detail.split(":", 2)]
        target = ": ".join(parts[:2]) if len(parts) == 3 else parts[0]
        issues.append(
            (f"Design: {target}", f"{detail}\n\nRaised by buildme's design step.")
        )
    return issues


def raise_spec_issues(issues: list[tuple[str, str]], step: str) -> None:
    """Raise `SpecIssuesFound` with *issues*, when there are any."""
    if issues:
        log.error("SPEC_ISSUES step=%s count=%d", step, len(issues))
        raise SpecIssuesFound(issues, step=step)


def _commit_change_dir(config, change_dir: Path, message: str) -> None:
    """Commit the change directory after a regeneration pass.

    No-op unless the pipeline owns a branch (``--no-worktree`` stays
    byte-identical to the pre-git-lifecycle pipeline), and no-op when the
    change directory is not under the project — the commit is explicit-path.
    """
    from . import git_ops

    try:
        rel = str(change_dir.relative_to(config.project_dir))
    except (ValueError, TypeError):
        return
    if rel:
        git_ops.commit_stage(config, message, [rel])


async def _generate_rubric(
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Generate rubric.md using the rubric module."""
    content = await generate_rubric(change_dir, config)
    rubric_path = change_dir / "rubric.md"
    atomic_write(rubric_path, content)
    log.info("Wrote rubric: %s", rubric_path)


# Severities that earn a regeneration pass. A minor gap is a clarification,
# not a reason to spend two more spec-generation calls.
DESIGN_AUDIT_ACTIONABLE = ("critical", "major")


def design_audit_actionable(gaps: list[dict]) -> list[dict]:
    """The findings at critical or major severity: what earns the rewrite."""
    return [
        g
        for g in gaps or []
        if isinstance(g, dict) and g.get("severity") in DESIGN_AUDIT_ACTIONABLE
    ]


def audit_spec_issues(gaps: list[dict]) -> list[tuple[str, str]]:
    """The critical and major plan-check findings (as
    `plan_review_steps.finding_dicts` gives them) whose fix needs a
    requirements change (`needs_requirements_change: true`), as `(title,
    body)` issues for spec-issues.md."""
    issues = []
    for g in design_audit_actionable(gaps):
        if g.get("needs_requirements_change") is not True:
            continue
        target = str(g.get("requirement") or "").strip() or g.get(
            "category", "plan-review"
        )
        body = f"[{g.get('severity', '?')}] {g.get('description', '').strip()}"
        if g.get("suggestion"):
            body += f"\n\nSuggested change: {str(g['suggestion']).strip()}"
        issues.append(
            (f"Plan review: {target}", body + "\n\nRaised by buildme's plan review.")
        )
    return issues


def record_blocked_review(
    config,
    state: BuildState,
    state_path: Path,
    name: str,
    reason: str,
    findings: str,
    *,
    final: bool = False,
    revision: str | None = None,
) -> None:
    """Keep actionable review evidence in both the checkpoint and progress log.

    `final` marks a block on findings: it saves the step's current revision
    as `blocked_revision`, so a resume at that revision refuses and one at
    another (the plan was edited) gets the rewrite back. `revision` keeps the
    record at the revision a rewrite was admitted at, so a resume after a
    published rewrite still rechecks it.
    """
    assert state.spec_gen is not None
    details = reason + ("\n" + findings if findings and findings not in reason else "")
    state.spec_gen.record_step(
        name,
        "blocked",
        details,
        blocking_findings=findings,
        revision=revision,
        blocked_revision=state.spec_gen.step_revision(name) if final else None,
    )
    state.checkpoint(state_path)
    from .progress import ProgressWriter

    path = config.progress_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ProgressWriter(path).log_phase(name.upper() + "_BLOCKED", details)
    log.error("%s blocked: %s", name, details)


class PlanBlocked(RuntimeError):
    """A planning step stopped on findings, already recorded as blocked."""


REOPEN_LINE = (
    "Edit the plan to fix these and resume; the step then gets its rewrites again."
)


def block_on_findings(
    config,
    state: BuildState,
    state_path: Path,
    name: str,
    reason: str,
    blocking: list[dict],
) -> None:
    """Record a final block on these findings, log each one and print them.
    The caller raises."""
    log_findings(name, "blocking", blocking)
    report = findings_report(blocking)
    record_blocked_review(config, state, state_path, name, reason, report, final=True)
    print(f"{name} blocked: {reason}\n{report}", flush=True)


def refusal_text(name: str, step) -> str:
    """What a refused resume prints: the saved findings, then `REOPEN_LINE`."""
    parts = [f"{name} is blocked at this plan revision."]
    if step.repair_findings:
        parts += [
            "Findings the rewrite was asked to fix:",
            findings_report(step.repair_findings),
        ]
    if step.blocking_findings:
        parts += ["Blocking findings:", step.blocking_findings]
    elif step.reason:
        parts.append(step.reason)
    parts.append(REOPEN_LINE)
    return "\n".join(parts)


def refuse_if_blocked(sg: SpecGenState, name: str) -> None:
    """Refuse, with no model call, a step that findings blocked at the current
    revision. A checkpoint from before `blocked_revision` refuses as before:
    on saved blocking findings at its recorded revision."""
    step = sg.steps.get(name)
    if not step or step.outcome != "blocked":
        return
    current = sg.step_revision(name)
    if step.blocked_revision == current or (
        step.blocked_revision is None
        and step.revision == current
        and step.blocking_findings
    ):
        text = refusal_text(name, step)
        print(text, flush=True)
        log.error("%s refused: %s", name, text)
        raise PlanBlocked(text)


def refuses_blocked(config, name: str = "plan_review") -> bool:
    """Whether a build of *config*'s change would now refuse the step *name*
    with no model call (`refuse_if_blocked`, after `reconcile_plan` and
    `reopen_if_edited`): its checkpoint records the step blocked under the
    current blocking rule, at the revision its inputs on disk have now. An
    edit to the plan, the specs or the `planning:` limits makes it False. Reads
    and writes nothing; False with no readable checkpoint."""
    try:
        state = BuildState.load(config.state_file_path())
        now = _plan_hashes(config)
    except (OSError, ValueError):
        return False
    sg = state.spec_gen
    step = sg.steps.get(name) if sg is not None else None
    if not step or step.outcome != "blocked" or step.rules_version < PLAN_RULES_VERSION:
        return False
    current = sg.model_copy(update={"artifact_hashes": now}).step_revision(name)
    if step.blocked_revision:
        return step.blocked_revision == current
    return step.revision == current and bool(step.blocking_findings)


def reopen_if_edited(state: BuildState, name: str, state_path: Path) -> None:
    """A step findings blocked whose inputs changed since (the plan was
    edited, or the `planning:` limits were raised) gets a fresh count of
    rewrites. A step blocked under an older
    blocking rule (`rules_version` below `PLAN_RULES_VERSION`) is reopened the
    same way, once: its saved blocking findings are cleared too, so the fresh
    check is not refused."""
    sg = state.spec_gen
    assert sg is not None
    step = sg.steps.get(name)
    if not step or step.outcome != "blocked":
        return
    if step.rules_version < PLAN_RULES_VERSION:
        step.blocking_findings = ""
        step.rules_version = PLAN_RULES_VERSION
        log.warning("%s: blocked under an older blocking rule; checking it again", name)
    elif not (
        step.blocked_revision and step.blocked_revision != sg.step_revision(name)
    ):
        return
    else:
        log.warning(
            "%s: plan changed since it was blocked; its rewrites are counted again",
            name,
        )
    step.repair_attempts = 0
    step.repair_findings = []
    step.blocking_counts = []
    step.blocked_revision = None
    state.checkpoint(state_path)


def rewrite_published(sg: SpecGenState, name: str) -> bool:
    """The step's last rewrite was admitted with findings and published, and
    nothing has blocked since: the step's revision moved on from the one the
    admission recorded."""
    step = sg.steps.get(name)
    return bool(
        step
        and step.repair_attempts
        and step.repair_findings
        and step.blocked_revision is None
        and step.revision != sg.step_revision(name)
    )


def open_findings(findings: list[dict]) -> list[dict]:
    """Findings of an audit that cannot have a rewrite, for a block record."""
    return [{**f, "finding_id": "", "text": _finding_text(f)} for f in findings]


async def validate_plan(config, state: BuildState, state_path: Path) -> None:
    """Run the one plan check (`run_plan_review_stage`). It passes, or it
    records a blocked outcome and raises: unresolved findings cannot advance
    to implementation."""
    from .llm_steps.plan_review_steps import run_plan_review_stage

    await run_plan_review_stage(config.change_dir, config, state, state_path)


def _load_or_create_state(config) -> BuildState:
    """Load existing state or create new one."""
    state_path = config.state_file_path()
    if state_path.exists():
        state = BuildState.load(state_path)
        log.info("Resumed state from %s", state_path)
        return state

    state = BuildState(
        change_name=config.change_name,
        tier=config.tier,
        state_file=str(state_path),
        phase=BuildPhase.SPEC_GENERATION,
        spec_gen=SpecGenState(),
    )
    state.checkpoint(state_path)
    return state


# The `_plan_hashes` key of config.yaml's `planning:` section. Only the plan
# check's inputs (`state.STEP_INPUTS`) include it: raising `planning.max_usd`
# or `planning.max_rewrites` reopens a blocked check, and marks nothing stale
# and leaves the plan revision as it was (row S8).
PLANNING_KEY = "planning"

# Top-level config.yaml sections no planning step reads: how the TDD loop runs
# its tests, how an epic runs its children, the git switches, `review:`
# (`review.plan_approval`, stop 4), and the settings of the steps after
# planning: the fixer of PR comments (`findings:`), the per-block code review
# (`code_review:`; its `model` is also the planning check's fallback model,
# which picks who checks the plan, not what the plan says) and the test
# quality gate (`test_quality:`). `memory_files:` is not one of them: its
# `stacks` sets the stack that picks the reference docs the design and tasks
# prompts inline (`BuildConfig.reference_docs_text`). They are cut out of the plan's hash and
# stored nowhere, so editing one mid-build changes no plan revision and marks
# nothing stale (rows S3, S4 of plan-buildme-subdir-narrow-tests-2026-10-28.md):
# turning stop 4 on for a planned build asks for an approval of that plan
# instead of writing a new one.
UNPLANNED_SECTIONS = (
    "tdd",
    "epic",
    "git",
    "review",
    "pipeline_viewer",
    "findings",
    "code_review",
    "test_quality",
)


def _split_sections(text: str, keys: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """config.yaml's text without its top-level *keys* sections, and each
    section found. A section runs to the next line that starts at column 0;
    blank lines after it go with it. A file with none of them comes back
    unchanged, so its hash is what it was before the sections existed."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    sections: dict[str, str] = {}
    i = 0
    while i < len(lines):
        match = re.match(r"([A-Za-z_][\w-]*)\s*:", lines[i])
        if match and match.group(1) in keys:
            end = i + 1
            while end < len(lines) and (
                not lines[end].strip() or lines[end][0] in " \t"
            ):
                end += 1
            sections[match.group(1)] = sections.get(match.group(1), "") + "".join(
                lines[i:end]
            )
            i = end
            continue
        kept.append(lines[i])
        i += 1
    return "".join(kept), sections


def _split_planning_section(text: str) -> tuple[str, str]:
    """config.yaml's text without its top-level `planning:` section, and the
    section (`_split_sections`)."""
    rest, sections = _split_sections(text, (PLANNING_KEY,))
    return rest, sections.get(PLANNING_KEY, "")


def _planning_hash(section: str) -> str:
    try:
        value = yaml.safe_load(section)
    except yaml.YAMLError:
        value = section
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _config_hashes(config) -> dict[str, str]:
    """config.yaml's hash without the `planning:` and `UNPLANNED_SECTIONS`
    sections, and the `planning:` section's own (`PLANNING_KEY`)."""
    config_path = config.specs_dir / "config.yaml"
    if not config_path.is_file():
        return {}
    rest, sections = _split_sections(
        config_path.read_bytes().decode("utf-8", "surrogateescape"),
        (PLANNING_KEY, *UNPLANNED_SECTIONS),
    )
    hashes = {
        "config.yaml": hashlib.sha256(
            rest.encode("utf-8", "surrogateescape")
        ).hexdigest()
    }
    if sections.get(PLANNING_KEY):
        hashes[PLANNING_KEY] = _planning_hash(sections[PLANNING_KEY])
    return hashes


def _plan_hashes(config) -> dict[str, str]:
    # A component's proposal and use cases are its change's (`change_root`).
    root = change_root(config.change_dir)
    files = {name: root / name for name in ("proposal.md", "use-cases.md")} | {
        name: config.change_dir / name
        for name in (
            "specs.md",
            "unknowns.md",
            "tasks.md",
            "rubric.md",
            "interview.md",
            "research.md",
            "codebase-analysis.md",
        )
    }
    # The plan is hashed under one key whatever its file is called, so a
    # change planned under the old name keeps its revision (`PLAN_HASH_KEY`).
    files[PLAN_HASH_KEY] = plan_path(config.change_dir)
    files.update(
        {
            "requirements/" + p.name: p
            for p in (config.change_dir / "requirements").glob("*.md")
        }
    )
    files.update(
        {"registry/" + p.name: p for p in (config.specs_dir / "registry").glob("*.md")}
    )
    # The specs.md the prompts' dependency section reads; none for a component
    # with no dependency, so its revision is unchanged.
    from .llm_steps.spec_steps import dependency_spec_files

    files.update(
        {
            f"depends_on/{p.parent.name}/specs.md": p
            for p in dependency_spec_files(config.change_dir)
        }
    )
    hashes = {
        name: hashlib.sha256(p.read_bytes()).hexdigest()
        for name, p in files.items()
        if p.is_file()
    }
    hashes.update(_config_hashes(config))
    hashes["references"] = hashlib.sha256(
        config.reference_docs_text().encode()
    ).hexdigest()
    hashes["granularity"] = hashlib.sha256(config.task_granularity.encode()).hexdigest()
    return hashes


def _revision(hashes: dict[str, str]) -> str:
    """The plan revision of *hashes* (`SpecGenState.revision`)."""
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in hashes.items() if k != PLANNING_KEY}, sort_keys=True
        ).encode()
    ).hexdigest()


def rebound_values(config) -> dict:
    """What the build's checkpoint would hold if only config.yaml's hash were
    computed by this version: its saved hashes with that one replaced, the
    plan revision, and each saved step's revision. For re-binding a build
    planned by an older version by hand; reads, writes nothing."""
    state = BuildState.load(config.state_file_path())
    sg = state.spec_gen or SpecGenState()
    hashes = dict(sg.artifact_hashes)
    hashes["config.yaml"] = _config_hashes(config)["config.yaml"]
    rebound = sg.model_copy(update={"artifact_hashes": hashes})
    return {
        "artifact_hashes.config.yaml": hashes["config.yaml"],
        "revision": _revision(hashes),
        **{
            f"steps.{name}.revision": rebound.step_revision(name)
            for name in sorted(sg.steps)
        },
    }


def reconcile_plan(
    config, state: BuildState, *, regenerated: set[str] | None = None
) -> None:
    """Invalidate the explicit planning dependencies and publish their revision.

    The state is the only evidence ledger. Existing filenames remain the public
    interface; their existence alone cannot validate dependent artifacts. The
    "what" files (proposal, use cases, requirements) are hashed like the rest,
    so an edit to one marks design and the stages after it stale; they are
    never regenerated or moved here.
    """
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    sg = state.spec_gen
    recovered = _finish_plan_publication(config)
    current = _plan_hashes(config)
    previous = sg.artifact_hashes
    changed = {
        key
        for key in previous.keys() | current.keys()
        if previous.get(key) != current.get(key)
    }
    stale = set(sg.stale_artifacts)
    if previous:
        # The proposal, use cases and requirements are pm-spec's inputs: an
        # edit to them marks buildme's artifacts stale, never themselves.
        if "proposal.md" in changed:
            stale.update({"design", "unknowns", "tasks", "rubric"})
        if "specs.md" in changed or any(
            key.startswith("requirements/") for key in changed
        ):
            stale.update({"design", "unknowns", "tasks", "rubric"})
        if "use-cases.md" in changed:
            stale.update({"tasks", "rubric"})
        if PLAN_HASH_KEY in changed:
            stale.update({"unknowns", "tasks", "rubric"})
        if "unknowns.md" in changed:
            stale.update({"tasks", "rubric"})
        if "tasks.md" in changed:
            stale.add("rubric")
        if changed & {"interview.md", "research.md"}:
            stale.update(ARTIFACT_DAG)
        if changed & {
            "config.yaml",
            "references",
            "granularity",
            "codebase-analysis.md",
        } or any(key.startswith(("registry/", "depends_on/")) for key in changed):
            stale.update({"design", "unknowns", "tasks", "rubric"})
    if not ChangeManager(config.specs_dir)._artifact_exists(
        config.change_dir, "requirements/*.md"
    ):
        stale.update({"tasks", "rubric"})
    stale -= (regenerated or set()) | recovered
    sg.stale_artifacts = sorted(stale)
    revision = _revision(current)
    if revision != sg.revision:
        # Evidence for any earlier revision is stale. The explanation stage is
        # advisory and does not authorize implementation.
        sg.revision = revision
        state.verification = None
        state.split_required = False
        state.size_accepted = False
        state.plan_approval = None
    sg.artifact_hashes = current
    if "unknowns" in recovered:
        _record_explainer_outcome(config, state)
    if recovered:
        state.checkpoint(config.state_file_path())
        (config.change_dir / ".plan-publication.json").unlink(missing_ok=True)


def _finish_plan_publication(config) -> set[str]:
    """Complete an interrupted, prevalidated publication, refusing intervening edits."""
    journal = config.change_dir / ".plan-publication.json"
    if not journal.exists():
        return set()
    files = json.loads(journal.read_text())
    for name, item in files.items():
        if Path(name).name != name or not (
            # A journal written before the rename names `OLD_PLAN_FILE`.
            name in {PLAN_FILE, OLD_PLAN_FILE, "tasks.md", "rubric.md", "unknowns.md"}
            or PLAN_EDITS_DIFF.fullmatch(name)
        ):
            raise ValueError("Invalid planning publication journal")
        path = config.change_dir / name
        actual = path.read_text() if path.exists() else None
        if actual not in (item["before"], item["after"]):
            raise RuntimeError(
                f"Planning publication interrupted and {name} changed; preserve edits before recovery"
            )
    for name, item in files.items():
        atomic_write(config.change_dir / name, item["after"])
    return {_artifact_id(name) for name in files}


def _artifact_id(name: str) -> str:
    """The artifact (`ARTIFACT_DAG` key) a published file name belongs to."""
    return "design" if name in (PLAN_FILE, OLD_PLAN_FILE) else Path(name).stem


# The unified diff of one rewrite's edits, `plan-edits-<attempt>.diff` in the
# change dir; that attempt's recheck is shown it (`plan_edits_diff`).
PLAN_EDITS_DIFF = re.compile(r"plan-edits-(\d+)\.diff")


def apply_plan_edits(staged_dir: Path, edits) -> dict[str, str]:
    """Apply a rewrite's edits (`models.PlanEdit`), in order, to the plan
    (`plan_path`) and tasks.md in *staged_dir*, and return the text of each
    file they changed. The edited plan is written as `PLAN_FILE`, and the
    staged copy of an old-name plan is removed, so the staged plan has one
    name.

    Each `old_str` must occur exactly once in its file as the edits before it
    leave it. A miss, no edits at all, or edits that leave both files as
    they were raise `PlanRepairRejected` and write nothing.
    """
    if not edits:
        _reject_edits("The rewrite returned no edits")
    texts: dict[str, str] = {}
    originals: dict[str, str] = {}
    for index, edit in enumerate(edits, 1):
        if edit.file not in texts:
            path = (
                plan_path(staged_dir)
                if edit.file == PLAN_FILE
                else staged_dir / edit.file
            )
            texts[edit.file] = path.read_text() if path.exists() else ""
            originals[edit.file] = texts[edit.file]
        count = texts[edit.file].count(edit.old_str) if edit.old_str else 0
        if count != 1:
            found = "not found" if count == 0 else f"found {count} times"
            _reject_edits(f"Plan edit {index} in {edit.file}: old_str {found}")
        texts[edit.file] = texts[edit.file].replace(edit.old_str, edit.new_str, 1)
    texts = {name: text for name, text in texts.items() if text != originals[name]}
    if not texts:
        _reject_edits("The rewrite's edits change nothing")
    for name, text in texts.items():
        atomic_write(staged_dir / name, text)
    if PLAN_FILE in texts:
        (staged_dir / OLD_PLAN_FILE).unlink(missing_ok=True)
    return texts


def _reject_edits(message: str) -> None:
    blocking, _ = recheck_verdict(
        [],
        [],
        [],
        deterministic=[
            {"category": "plan-edit", "severity": "critical", "description": message}
        ],
    )
    raise PlanRepairRejected(message, blocking=blocking)


def plan_edits_diff(change_dir: Path, attempt: int) -> str:
    """The published diff of rewrite *attempt*; empty when there is none."""
    path = Path(change_dir) / f"plan-edits-{attempt}.diff"
    return path.read_text() if path.exists() else ""


async def rewrite_plan(
    config,
    state: BuildState,
    findings: list[dict],
    *,
    attempt: int = 1,
    step: str | None = None,
) -> int:
    """The sole feedback publisher: stage, validate, then publish one revision.

    Failed generation leaves the original set intact. A crash during publication
    leaves a journal that must reconcile before any consumer accepts the files.

    One call (`run_plan_edit`) returns exact replacements in the plan and
    tasks.md for these numbered findings, and `apply_plan_edits` applies them;
    lines no edit names are kept byte for byte. An edited plan is published as
    `PLAN_FILE` (an `OLD_PLAN_FILE` stays where it is) and gets its
    unknowns.md written again. The rubric is written again whole, and
    `plan-edits-<attempt>.diff` is published with the files. `step` names the
    caller. The staged plan must pass the deterministic scans and the
    requirements check, or it raises `PlanRepairRejected`; the caller's own
    recheck judges the rest.
    """
    from .llm_steps.plan_review_steps import run_plan_edit
    from .tdd_engine import parse_blocks

    reconcile_plan(config, state)
    with tempfile.TemporaryDirectory(prefix="buildme-plan-") as directory:
        staged = copy.copy(config)
        staged.specs_dir = Path(directory) / "specs"
        staged._change_dir_override = ""
        staged.change_dir = staged.specs_dir / Path(config.change_dir).relative_to(
            config.specs_dir
        )
        shutil.copytree(config.change_dir, staged.change_dir)
        # A component's proposal and use cases are its change's.
        root = change_root(config.change_dir)
        if root != config.change_dir:
            for name in ("proposal.md", "use-cases.md"):
                if (root / name).is_file():
                    shutil.copy2(root / name, change_root(staged.change_dir) / name)
        registry = config.specs_dir / "registry"
        if registry.exists():
            shutil.copytree(registry, staged.specs_dir / "registry")
        source_config = config.specs_dir / "config.yaml"
        if source_config.exists():
            shutil.copy2(source_config, staged.specs_dir / "config.yaml")
        cm = ChangeManager(staged.specs_dir)
        result = await run_plan_edit(staged.change_dir, staged, findings)
        if result.not_fixed:
            log.warning(
                "%s: the rewrite could not fix %s",
                step or "plan_review",
                ", ".join(result.not_fixed),
            )
        edited = apply_plan_edits(staged.change_dir, result.edits)
        names = [
            _artifact_id(name) for name in (PLAN_FILE, "tasks.md") if name in edited
        ]
        if PLAN_FILE in edited:
            raise_spec_issues(design_spec_issues(edited[PLAN_FILE]), step or "design")
            await _generate_unknowns(
                cm, staged.change_dir, staged, state.model_copy(deep=True)
            )
            names.append("unknowns")
        label = step or "plan_review"

        def reject(message: str, deterministic: list[dict]) -> None:
            blocking, advisory = recheck_verdict(
                [], [], [], deterministic=deterministic
            )
            log_findings(label, "blocking", blocking)
            raise PlanRepairRejected(message, blocking=blocking, advisory=advisory)

        tasks_path = staged.change_dir / "tasks.md"
        if not parse_blocks(tasks_path):
            reject(
                "Rewritten tasks contain no implementation blocks",
                [
                    {
                        "category": "no-blocks",
                        "severity": "critical",
                        "description": "The rewritten tasks.md has no implementation blocks.",
                    }
                ],
            )
        if found := scan_placeholders(tasks_path.read_text()):
            reject("Rewritten tasks still contain placeholder findings", found)
        if found := scan_no_behaviour_blocks(tasks_path):
            reject("Rewritten tasks still contain blocks with no behavior", found)
        if not cm._artifact_exists(staged.change_dir, "requirements/*.md"):
            reject(
                "Cannot publish a plan with incomplete capability requirements",
                [
                    {
                        "category": "requirements",
                        "severity": "critical",
                        "description": "A capability the proposal names has no requirements file, or specs.md has no valid capability section.",
                    }
                ],
            )
        # The rubric describes this exact task/design set.
        await _generate_rubric(staged.change_dir, staged, state)
        names.append("rubric")
        publication = {}
        for name in names:
            filename = ARTIFACT_DAG[name]["generates"]
            original = config.change_dir / filename
            publication[filename] = {
                "before": original.read_text() if original.exists() else None,
                "after": (staged.change_dir / filename).read_text(),
            }
        # The diff is against the plan as it was read, an old-name plan too.
        shown = {
            PLAN_FILE: plan_path(config.change_dir),
            "tasks.md": config.change_dir / "tasks.md",
        }
        diff_name = f"plan-edits-{attempt}.diff"
        diff_path = config.change_dir / diff_name
        publication[diff_name] = {
            "before": diff_path.read_text() if diff_path.exists() else None,
            "after": "".join(
                "".join(
                    difflib.unified_diff(
                        shown[name].read_text().splitlines(keepends=True)
                        if shown[name].exists()
                        else [],
                        publication[name]["after"].splitlines(keepends=True),
                        fromfile=f"a/{name}",
                        tofile=f"b/{name}",
                    )
                )
                for name in (PLAN_FILE, "tasks.md")
                if name in edited
            ),
        }
        atomic_write(
            config.change_dir / ".plan-publication.json", json.dumps(publication)
        )
        _finish_plan_publication(config)
    reconcile_plan(config, state, regenerated=set(names))
    # Reconciliation records the published unknowns evidence on the live state.
    state.checkpoint(config.state_file_path())
    _commit_change_dir(
        config,
        config.change_dir,
        "docs(specs): publish plan review repair",
    )
    return 2 if PLAN_FILE in edited else 1


def assert_plan_current(config, state: BuildState, *, archived: bool = False) -> None:
    """Check the accepted documents after implementation, without rewriting them.

    Archiving merges these requirements into the registry; that expected output
    is covered by final verification, while the original registry input remains
    in the accepted planning revision.
    """
    if state.spec_gen is None or not state.spec_gen.revision:
        raise RuntimeError(
            "Planning revision evidence is missing. This legacy checkpoint cannot resume automatically; preserve its implementation and start a reviewed build in a separate clean checkout"
        )
    if (config.change_dir / ".plan-publication.json").exists():
        raise RuntimeError("Planning publication is incomplete")
    if not state.spec_gen.step_passed("plan_review"):
        raise RuntimeError("Required planning review evidence is missing or stale")
    current = _plan_hashes(config)
    expected = state.spec_gen.artifact_hashes
    keys = current.keys() | expected.keys()
    changed = [
        key
        for key in keys
        if not (archived and key.startswith("registry/"))
        and key != PLANNING_KEY
        and current.get(key) != expected.get(key)
    ]
    if changed or state.spec_gen.stale_artifacts:
        state.verification = None
        raise RuntimeError(
            "Plan changed after implementation; revalidate before building: "
            + ", ".join(sorted(changed))
        )
