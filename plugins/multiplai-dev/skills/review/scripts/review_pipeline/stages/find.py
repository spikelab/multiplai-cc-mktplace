"""find: one agent per dimension, then dedupe, then `finding_gate`.

A tree target (a whole repository or a directory, not a change) does not fit
in one prompt, so its files are split into groups by `file_groups` and each
dimension runs once per group, keyed `"<dimension>@<group index>"`. It has no
commits, so `history` does not run.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from .. import sdk
from ..gates import file_at_head, finding_gate
from ..config import ReviewConfig
from ..models import Finding, FinderOutput, FinderResult, Rejected, ReviewState, TargetInfo
from ..prompts import find as prompt
from . import RunContext, bounded, fix_citation, relative_path

log = logging.getLogger(__name__)

CONVENTIONS_MAX_CHARS = 40_000
# A tree review's files go to the finders in groups of at most this many
# characters, so each group's files fit in one agent's reading.
GROUP_MAX_CHARS = 100_000
# Dimensions a tree review does not run: there are no commits to compare against.
TREE_SKIPPED_DIMENSIONS = ("history",)


def file_chars(target: TargetInfo, path: str) -> int:
    text = file_at_head(target, path)
    return len(text) if text is not None else 0


def _top_dir(path: str, root: str) -> str:
    """The first directory of *path* below *root* ("" for a file directly in *root*)."""
    rel = path[len(root) + 1:] if root and path.startswith(root + "/") else path
    return rel.split("/", 1)[0] if "/" in rel else ""


def file_groups(target: TargetInfo, max_chars: int = GROUP_MAX_CHARS) -> list[list[str]]:
    """The files under review split into groups for the finders.

    A change review is one group holding every changed file, so its path is
    unchanged. A tree review walks the files in path order within each
    top-level directory (below the reviewed path; files directly in it come
    first), and starts a new group when the next file would pass *max_chars*
    or when the top-level directory changes. A file larger than *max_chars*
    gets a group of its own.
    """
    if not target.is_tree:
        return [list(target.files)]
    root = "" if target.ref in ("", ".") else target.ref
    groups: list[list[str]] = []
    current: list[str] = []
    size, top = 0, None
    for here, path in sorted((_top_dir(p, root), p) for p in target.files):
        chars = file_chars(target, path)
        if current and (size + chars > max_chars or here != top):
            groups.append(current)
            current, size = [], 0
        current.append(path)
        size += chars
        top = here
    if current:
        groups.append(current)
    return groups


def finder_dimensions(target: TargetInfo, config: ReviewConfig) -> tuple[str, ...]:
    """The dimensions the finders run for this target."""
    if not target.is_tree:
        return tuple(config.dimensions)
    return tuple(d for d in config.dimensions if d not in TREE_SKIPPED_DIMENSIONS)


def conventions_chain(target: TargetInfo, files: list[str] | None = None) -> str:
    """Every coding-standards.md, then every CLAUDE.md, from the repo root down to each file's directory, at head.

    *files* defaults to every changed file; a tree review passes one group's.
    coding-standards.md holds rules written for the reviewer only, so its blocks
    come first: when the text passes CONVENTIONS_MAX_CHARS, CLAUDE.md is skipped first.
    """
    dirs: list[str] = [""]
    for f in (target.files if files is None else files):
        parts = PurePosixPath(f).parent.parts
        for i in range(1, len(parts) + 1):
            dirs.append("/".join(parts[:i]))
    chain = list(dict.fromkeys(dirs))
    blocks, total = [], 0
    for name in ("coding-standards.md", "CLAUDE.md"):
        for d in chain:
            path = f"{d}/{name}" if d else name
            text = file_at_head(target, path)
            if text is None:
                continue
            block = f"### {path}\n\n{text.strip()}\n"
            if total + len(block) > CONVENTIONS_MAX_CHARS:
                blocks.append(f"### {path}\n\n[skipped: the rules above already fill the prompt budget]\n")
                continue
            blocks.append(block)
            total += len(block)
    return "\n".join(blocks)


def dedupe_key(finding: Finding) -> tuple[str, int, str]:
    return (finding.file, finding.line_start, " ".join(finding.claim.lower().split())[:160])


def _normalise(finding: Finding, dimension: str, target: TargetInfo, ctx: RunContext) -> Finding:
    citations = [fix_citation(c, target, ctx.snapshot) for c in finding.citations]
    return finding.with_location(
        file=relative_path(finding.file, target, ctx.snapshot),
        citations=[c.model_dump() for c in citations if c is not None],
        dimension="pre-existing" if finding.dimension == "pre-existing" else dimension,
        finder=dimension,
        finders=[dimension],
    )


async def run_find(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("find"):
        return state
    target, cfg = state.target, ctx.config
    dims = finder_dimensions(target, cfg)
    if target.is_tree:
        groups = file_groups(target)
        # (key, dimension, group index): "<dimension>@<group>" so a resumed run asks only the missing pairs.
        pairs = [(f"{d}@{i}", d, i) for i in range(len(groups)) for d in dims]
    else:
        groups = [None]  # the change path: every changed file, one prompt per dimension, as before
        pairs = [(d, d, 0) for d in dims]
    conventions: dict[int, str] = {}
    if "conventions" in dims:
        for i, group in enumerate(groups):
            if any(key not in state.finder_results for key, d, g in pairs if g == i and d == "conventions"):
                conventions[i] = conventions_chain(target, group)
    todo = [p for p in pairs if p[0] not in state.finder_results]

    async def _one(key: str, dimension: str, index: int) -> FinderResult:
        try:
            out = await sdk.agent_call_structured(
                prompt.build(target, dimension, ctx.diff, conventions.get(index, ""), files=groups[index]),
                FinderOutput,
                allowed_tools=sdk.FINDER_TOOLS, model=cfg.finder_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label=f"find:{dimension}",
            )
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("finder %s failed for %s", key, target.slug, exc_info=True)
            return FinderResult(error=f"finder {key}: {str(e).splitlines()[0][:200]}")
        if ctx.progress:
            ctx.progress.line(f"  finder {key}: {len(out.findings)} findings")
        return FinderResult(findings=[_normalise(f, dimension, target, ctx) for f in out.findings])

    async def one(pair: tuple[str, str, int]) -> None:
        # Stored as each finder returns: a budget stop mid-stage keeps what
        # was already paid for, and the checkpoint saved then carries it.
        state.finder_results[pair[0]] = await _one(*pair)

    await bounded(todo, one, cfg.concurrency)

    results = [state.finder_results[key] for key, _, _ in pairs]
    failures = [r.error for r in results if r.error]
    if len(failures) == len(results):
        for key, _, _ in pairs:
            del state.finder_results[key]  # a later run tries every finder again
        raise sdk.AgentCallError("every finder failed: " + "; ".join(failures))

    seen: dict[tuple[str, int, str], Finding] = {}
    kept: list[Finding] = []
    for result in results:
        for finding in result.findings:
            finding = finding.model_copy(deep=True)  # `finders` grows below; the stored result stays as returned
            key = dedupe_key(finding)
            if key in seen:
                first = seen[key]
                if finding.finder not in first.finders:
                    first.finders.append(finding.finder)
                continue
            seen[key] = finding
            gate = finding_gate(target, finding)
            if gate.passed:
                kept.append(finding)
            else:
                state.rejected.append(Rejected(finding=finding, reason=gate.reason, stage="find"))
                ctx.gate_reasons.append(gate.reason)

    state.findings = kept
    ctx.counts = {"found": len(kept) + len([r for r in state.rejected if r.stage == "find"]),
                  "kept": len(kept), "rejected": len([r for r in state.rejected if r.stage == "find"]),
                  "finder_failures": len(failures)}
    state.errors.extend(failures)
    state.stage = "find"
    return state
