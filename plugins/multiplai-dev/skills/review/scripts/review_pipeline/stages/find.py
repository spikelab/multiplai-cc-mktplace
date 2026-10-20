"""find: one agent per dimension, then dedupe, then `finding_gate`.

A tree target (a whole repository or a directory, not a change) does not fit
in one prompt, so its files are split into groups by `file_groups` and each
dimension runs once per group, keyed `"<dimension>@<group index>"`. It has no
commits, so `history` does not run.

A change review also runs one piece finder per stored piece of its diff
(`state.pieces`, from `chunks.split_pieces`), keyed `"piece@<id>"`, after the
dimension finders, and every review with mapped paths one path finder per
path (`state.path_ids`, read back from `paths.json`), keyed `"path@<id>"`,
last. Each writes a plain-text report
(`sdk.agent_call_text`), stored in `state.finder_reports` and written as
`report-piece-<id>.md` or `report-path-<id>.md` before a second call converts it into findings
(`run_reported`). Its findings then go through the same dedupe and
`finding_gate` as the dimension finders'. Only `RepoTrustError`, a hit usage
limit and a budget stop end the stage; a failed report or convert call is recorded in coverage
and the others go on, and `resume` asks only for the missing report or convert.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path, PurePosixPath

from code_facts import Fact
from multiplai_core.citations import file_at
from multiplai_core.untrusted import defang
from pydantic import ValidationError

from .. import budget, checks, sdk, timings
from ..checks import add_coverage, drop_coverage
from ..chunks import piece_diff_text
from ..config import TRIM_DIMENSION, ReviewConfig
from ..gates import file_at_head, finding_gate, gated_locations, gated_need, reason_kind
from ..linked_docs import LinkSource, linked_docs, not_included_line
from ..models import (AgentCheck, ConvertedItem, ConvertOutput, CoverageRecord, Finding, FinderOutput,
                      FinderReport, FinderResult, GateCheck, Need, Rejected, ReviewState, TargetInfo)
from ..paths_io import load_paths, views
from ..prompts import DIFF_PROMPT_CHARS, commits_block, deps_block, description_block, settings_block, workspace_block
from ..prompts import find as prompt
from ..prompts.convert import build_convert
from ..prompts.path import build_path
from ..prompts.piece import build_piece
from ..state import save_state
from . import RunContext, bounded, fix_citation, relative_path

log = logging.getLogger(__name__)

CONVENTIONS_MAX_CHARS = 40_000
# A tree review's files go to the finders in groups of at most this many
# characters, so each group's files fit in one agent's reading.
GROUP_MAX_CHARS = 100_000
# Dimensions a tree review does not run: there are no commits to compare against.
TREE_SKIPPED_DIMENSIONS = ("history",)
# The piece and path finders' tools, for the report and the convert call alike: no web.
READ_ONLY = ["Read", "Grep", "Glob"]
# A report that is one of these (stripped) has no item to convert.
EMPTY_REPORTS = ("", "NO FINDINGS")


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


def finder_dimensions(target: TargetInfo, config: ReviewConfig, *, tree_trim: bool = False) -> tuple[str, ...]:
    """The dimensions the finders run for this target, the trim finder last when it runs:
    on a change unless `config.trim` is off, on a tree only with *tree_trim* (`--trim`)."""
    if not target.is_tree:
        dims = tuple(config.dimensions)
    else:
        dims = tuple(d for d in config.dimensions if d not in TREE_SKIPPED_DIMENSIONS)
    if tree_trim if target.is_tree else config.trim:
        dims += (TRIM_DIMENSION,)
    return dims


def _dir_chain(target: TargetInfo, files: list[str] | None) -> list[str]:
    """The repository root, then every directory down to each of *files* (default: every changed file)."""
    dirs: list[str] = [""]
    for f in (target.files if files is None else files):
        parts = PurePosixPath(f).parent.parts
        for i in range(1, len(parts) + 1):
            dirs.append("/".join(parts[:i]))
    return list(dict.fromkeys(dirs))


BINDING_INSTRUCTION = (
    "The documents below are rules of this repository. Report a finding for any changed line that breaks a "
    "rule in one of them. Cite the changed line, and name the document whose rule it breaks in the finding text."
)


def binding_instruction(included: list[str]) -> str:
    """The instruction before the first linked doc, listing every included one ("" for none)."""
    if not included:
        return ""
    listed = "\n".join(f"- {' '.join(defang(p, markdown_fences=False).split())}" for p in included)
    return f"{BINDING_INSTRUCTION}\n\n{listed}\n\n"


def conventions_blocks(target: TargetInfo, files: list[str] | None = None, *,
                       coverage: list[CoverageRecord] | None = None) -> list[tuple[str, bool, str]]:
    """(path, included, block) for every coding-standards.md, then every CLAUDE.md, from the repo
    root down to each file's directory, at head; then the Markdown docs they link (`linked_docs`).

    *files* defaults to every changed file; a tree review passes one group's.
    coding-standards.md holds rules written for the reviewer only, so its blocks
    come first: when the text passes CONVENTIONS_MAX_CHARS, CLAUDE.md is skipped first.
    The linked docs share what is left of CONVENTIONS_MAX_CHARS; the first one
    included carries `binding_instruction`, and one left out is a
    (path, False, block) entry and a record appended to *coverage*: the block
    is `not_included_line` when it was left out for length, else "".
    """
    chain = _dir_chain(target, files)
    blocks, total = [], 0
    sources: list[LinkSource] = []
    base = None if target.is_tree else target.base_sha
    for name in ("coding-standards.md", "CLAUDE.md"):
        for d in chain:
            path = f"{d}/{name}" if d else name
            text = file_at_head(target, path)
            if text is None:
                continue
            sources.append(LinkSource(path=path, head_text=text,
                                      base_text=file_at(target.repo_path, base, path) if base else None))
            block = f"### {path}\n\n{text.strip()}\n"
            if total + len(block) > CONVENTIONS_MAX_CHARS:
                blocks.append((path, False,
                               f"### {path}\n\n[skipped: the rules above already fill the prompt budget]\n"))
                continue
            blocks.append((path, True, block))
            total += len(block)
    docs = linked_docs(sources, repo=Path(target.repo_path), head=target.head_sha, base=base,
                       remaining=CONVENTIONS_MAX_CHARS - total)
    lead = binding_instruction(docs.included)
    for i, (path, block) in enumerate(zip(docs.included, docs.blocks)):
        blocks.append((path, True, lead + block if i == 0 else block))
    blocks += [(record.subject, False, not_included_line(record.subject) if record.reason == "budget" else "")
               for record in docs.skipped]
    if coverage is not None:
        coverage.extend(docs.skipped)
    return blocks


_CRITICAL_HEADING = re.compile(r"^##\s+critical paths\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,2}\s")
_BULLET_GLOB = re.compile(r"^\s*[-*+]\s.*?`([^`]+)`")


def critical_paths(target: TargetInfo, files: list[str] | None = None) -> list[tuple[str, str]]:
    """(glob, coding-standards.md path) for each bullet under a `## Critical paths` heading, in every
    coding-standards.md `conventions_blocks` reads for *files*. One glob per bullet, the first text in
    backticks; globs are relative to the repository root."""
    found: list[tuple[str, str]] = []
    for d in _dir_chain(target, files):
        path = f"{d}/coding-standards.md" if d else "coding-standards.md"
        text = file_at_head(target, path)
        if text is None:
            continue
        inside = False
        for line in text.splitlines():
            if _CRITICAL_HEADING.match(line.strip()):
                inside = True
            elif _HEADING.match(line):
                inside = False
            elif inside and (m := _BULLET_GLOB.match(line)):
                found.append((m.group(1).strip(), path))
    return found


def conventions_chain(target: TargetInfo, files: list[str] | None = None) -> str:
    """The text of `conventions_blocks`, as the conventions finder's prompt holds it."""
    return "\n".join(block for _, _, block in conventions_blocks(target, files) if block)


def dedupe_key(finding: Finding) -> tuple[str, int, str]:
    return (finding.file, finding.line_start, " ".join(finding.claim.lower().split())[:160])


def _normalise(finding: Finding, dimension: str, target: TargetInfo, ctx: RunContext) -> Finding:
    citations = [fix_citation(c, target, ctx.snapshot) for c in finding.citations]
    return finding.with_location(
        file=relative_path(finding.file, target, ctx.snapshot),
        citations=[c.model_dump() for c in citations if c is not None],
        other_locations=[relative_path(loc, target, ctx.snapshot) for loc in finding.other_locations],
        dimension="pre-existing" if finding.dimension == "pre-existing" else dimension,
        finder=dimension,
        finders=[dimension],
    )


def finding_entry(finding: Finding, calls: list, hunks: dict, target: TargetInfo, ctx: RunContext) -> dict:
    """A finding as the record holds it: its evidence, each citation marked, fate `kept` until judged."""
    return {
        "id": finding.id, "claim": finding.claim, "severity": finding.severity,
        "failure_scenario": finding.failure_scenario,
        "citations": [checks.marked_citation(c, calls, hunks, target, ctx.snapshot) for c in finding.citations],
        "fate": "kept",
    }


def group_facts(facts: list[Fact], group: list[str] | None) -> list[Fact]:
    """The facts a finder is given: all of them for a change; for a tree group, those citing one of its files."""
    if group is None:
        return list(facts)
    files = set(group)
    return [f for f in facts if any(c.path in files for c in f.citations)]


# --- report-then-convert: the piece and path finders -----------------------------------------------


def report_file_name(key: str) -> str:
    """`piece@3` -> `report-piece-3.md`, `path@p7` -> `report-path-p7.md`."""
    kind, _, ident = key.partition("@")
    return f"report-{kind}-{ident}.md"


def write_report_file(target_dir: Path, key: str, text: str) -> Path:
    """The report as the finder returned it, at the top level of *target_dir* (so `keep_round` keeps it).

    Written to a temporary name and renamed, so a kill mid-write leaves no half report.
    """
    path = Path(target_dir) / report_file_name(key)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def map_converted(key: str, out: ConvertOutput) -> tuple[list[ConvertedItem], list[Finding]]:
    """Every converted item as a `ConvertedItem`, and a `Finding` for each one that has a location.

    A missing severity is LOW. A finding's dimension and finder are the key's
    kind (`piece` or `path`). An item with no file, line or citation, or one
    whose location does not validate, stays an item with `finding_id` None.
    """
    kind = key.partition("@")[0]
    items: list[ConvertedItem] = []
    findings: list[Finding] = []
    for it in out.items:
        severity = it.severity or "LOW"
        finding = None
        if it.file and it.line_start and it.citations:
            try:
                finding = Finding(claim=it.claim, severity=severity, dimension=kind, file=it.file,
                                  line_start=it.line_start, line_end=it.line_end or it.line_start,
                                  failure_scenario=it.failure_scenario, citations=it.citations,
                                  finder=kind, finders=[kind], defect_class=it.defect_class,
                                  other_locations=it.other_locations)
            except ValidationError:
                finding = None
        if finding is not None:
            findings.append(finding)
        items.append(ConvertedItem(kind=it.kind, severity=severity, finding_id=finding.id if finding else None))
    return items, findings


def _relative(out: ConvertOutput, target: TargetInfo, snapshot: Path) -> ConvertOutput:
    """*out* with every path repo-relative, as the finders' findings are, before the ids are computed."""
    items = []
    for it in out.items:
        items.append(it.model_copy(update={
            "file": relative_path(it.file, target, snapshot) if it.file else it.file,
            "citations": [fix_citation(c, target, snapshot) for c in it.citations],
            "other_locations": [relative_path(loc, target, snapshot) for loc in it.other_locations]}))
    return ConvertOutput(items=items)


def _save(state: ReviewState, ctx: RunContext) -> None:
    if ctx.target_dir is not None:
        save_state(state, ctx.target_dir)


def _first_line(e: BaseException) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:300]


async def run_reported(key: str, prompt_text: str, state: ReviewState, ctx: RunContext, *,
                       given: list[str] | None = None, hunks: dict | None = None) -> None:
    """One piece or path finder: its report call, then its convert call, each skipped when already done.

    The report is stored in `state.finder_reports`, saved and written to its
    file before the convert call starts, so a stop between the two calls loses
    nothing paid for. Both calls are charged to the ledger (`find:<kind>` and
    `find:<kind>-convert`). A failed call is recorded in coverage (`finder-failed`
    or `convert-failed`) and the stage goes on; a failed convert stays pending
    for `resume`.
    """
    target, cfg = state.target, ctx.config
    kind = key.partition("@")[0]
    common = {"allowed_tools": READ_ONLY, "model": cfg.finder_model, "effort": cfg.effort,
              "max_turns": cfg.max_turns}
    rep = state.finder_reports.get(key)
    if rep is None:
        label = f"find:{kind}"
        interval = timings.open_interval(state, label)
        started, error, text = checks.now(), "", ""
        with sdk.recording() as rec:
            try:
                text = await sdk.agent_call_text(prompt_text, cwd=ctx.snapshot, budget_label=label,
                                                 call_timeout=sdk.DEFAULT_CALL_TIMEOUT_S, **common)
            except (sdk.RepoTrustError, sdk.UsageLimitError, budget.BudgetExceededError):
                raise
            except Exception as e:  # noqa: BLE001 — one failed finder must not stop the others
                log.error("finder %s failed for %s", key, target.slug, exc_info=True)
                error = _first_line(e)
            finally:
                interval.ended_at = timings.now()
        check = AgentCheck(stage="find", subject=key, given=list(given or []),
                           calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
                           turns=rec.turns, cost_usd=round(rec.cost_usd, 6), started_at=started,
                           ended_at=checks.now())
        if error:
            check.error, check.outcome = f"{key}: {error}", f"failed: {checks.failure_kind(error)}"
            state.checks.append(check)
            add_coverage(state, CoverageRecord(stage="find", subject=key, level="error", reason="finder-failed",
                                               detail=error))
            return
        empty = text.strip() in EMPTY_REPORTS
        check.outcome = "no findings" if empty else f"report ({len(text)} characters)"
        state.checks.append(check)
        drop_coverage(state, stage="find", subject=key, reason="finder-failed")
        rep = FinderReport(key=key, text=text)
        state.finder_reports[key] = rep
        _save(state, ctx)
        if ctx.target_dir is not None:
            write_report_file(ctx.target_dir, key, text)
        if ctx.progress:
            ctx.progress.line(f"  finder {key}: {check.outcome}")
    if rep.converted:
        return
    if rep.text.strip() in EMPTY_REPORTS:
        rep.converted, rep.convert_error = True, None
        _save(state, ctx)
        return

    label = f"find:{kind}-convert"
    interval = timings.open_interval(state, label)
    started, error, out = checks.now(), "", None
    with sdk.recording() as rec:
        try:
            out = await sdk.agent_call_structured(build_convert(key, rep.text), ConvertOutput,
                                                  cwd=str(ctx.snapshot), budget_label=label, **common)
        except (sdk.RepoTrustError, sdk.UsageLimitError, budget.BudgetExceededError):
            raise
        except Exception as e:  # noqa: BLE001 — the report is kept and the convert left for resume
            log.error("convert %s failed for %s", key, target.slug, exc_info=True)
            error = _first_line(e)
        finally:
            interval.ended_at = timings.now()
    check = AgentCheck(stage="find", subject=f"{key} convert", given=[f"report of {key}"],
                       calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
                       turns=rec.turns, cost_usd=round(rec.cost_usd, 6), started_at=started, ended_at=checks.now())
    if error:
        rep.convert_error = error
        check.error, check.outcome = f"{key} convert: {error}", f"failed: {checks.failure_kind(error)}"
        state.checks.append(check)
        add_coverage(state, CoverageRecord(stage="find", subject=key, level="error", reason="convert-failed",
                                           detail=error))
        _save(state, ctx)
        return
    rep.items, rep.findings = map_converted(key, _relative(out, target, ctx.snapshot))
    rep.converted, rep.convert_error = True, None
    drop_coverage(state, stage="find", subject=key, reason="convert-failed")
    check.items = [i.model_dump(mode="json") for i in rep.items]
    check.findings = [finding_entry(f, rec.tool_calls, hunks or {}, target, ctx) for f in rep.findings]
    check.outcome = (f"{len(rep.items)} item{'' if len(rep.items) == 1 else 's'}, "
                     f"{len(rep.findings)} finding{'' if len(rep.findings) == 1 else 's'}")
    state.checks.append(check)
    _save(state, ctx)
    if ctx.progress:
        ctx.progress.line(f"  convert {key}: {check.outcome}")


def shared_blocks(target: TargetInfo) -> str:
    """What every piece and path finder is told about the snapshot and the change, before its own part."""
    return "\n\n".join(p for p in (workspace_block(target), deps_block(target), commits_block(target),
                                    description_block(target), settings_block(target)) if p)


def reported_finders(state: ReviewState, ctx: RunContext) -> list[tuple[str, str, list[str], dict]]:
    """(key, prompt, given labels, diff hunks) of each piece finder, then each path finder.

    Pieces run on a change review only, in piece order; a piece with no file
    (a meta piece holding only omitted lockfiles) has nothing to review. Path
    finders run for each id in `state.path_ids`, in `paths.json` order, when
    the paths stage produced the file (`paths_io.load_paths`); a resumed run
    reads the stored file and maps nothing again.
    """
    target = state.target
    shared = shared_blocks(target)
    out = []
    for p in ([] if target.is_tree else state.pieces or []):
        if not p.files:
            continue
        diff = piece_diff_text(ctx.diff, p)
        labels = [f"piece {p.id} ({len(p.files)} file{'' if len(p.files) == 1 else 's'}, {p.chars} characters)"]
        given = checks.prompt_labels(target, extra=labels)
        out.append((f"piece@{p.id}", build_piece(p, diff, workspace=shared), given, checks.diff_hunks(diff)))
    result = load_paths(ctx.target_dir, state) if ctx.target_dir is not None and state.path_ids else None
    if result is not None:
        wanted = set(state.path_ids)
        hunks = checks.diff_hunks(ctx.diff[:DIFF_PROMPT_CHARS]) if ctx.diff else {}
        for view in views(result):
            if view.id not in wanted:
                continue
            given = checks.prompt_labels(target, extra=[f"path {view.id} ({len(view.hops)} hops)"])
            out.append((f"path@{view.id}", build_path(view, workspace=shared), given, hunks))
    return out


def _reported_result(state: ReviewState, key: str) -> FinderResult:
    """A reported finder's outcome as the dedupe below reads it: its findings, or why there are none."""
    rep = state.finder_reports.get(key)
    if rep is None:
        return FinderResult(error=f"{key}: no report")
    if not rep.converted:
        return FinderResult(error=f"{key} convert: {rep.convert_error or 'not converted'}")
    return FinderResult(findings=list(rep.findings))


async def run_find(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("find"):
        return state
    target, cfg = state.target, ctx.config
    dims = finder_dimensions(target, cfg, tree_trim=state.trim)
    if target.is_tree:
        groups = file_groups(target)
        # (key, dimension, group index): "<dimension>@<group>" so a resumed run asks only the missing pairs.
        pairs = [(f"{d}@{i}", d, i) for i in range(len(groups)) for d in dims]
    else:
        groups = [None]  # the change path: every changed file, one prompt per dimension, as before
        pairs = [(d, d, 0) for d in dims]
    # After the dimensions, so a budget stop mid-pieces has already asked every dimension.
    reported = reported_finders(state, ctx)
    rules: dict[int, list[tuple[str, bool, str]]] = {}
    if "conventions" in dims:
        for i, group in enumerate(groups):
            if any(key not in state.finder_results for key, d, g in pairs if g == i and d == "conventions"):
                skipped_docs: list[CoverageRecord] = []
                rules[i] = conventions_blocks(target, group, coverage=skipped_docs)
                for record in skipped_docs:  # once, however many groups or resumed runs link it
                    if record not in state.coverage:
                        add_coverage(state, record)
    hunks = checks.diff_hunks(ctx.diff[:DIFF_PROMPT_CHARS])
    todo = [p for p in pairs if p[0] not in state.finder_results]
    facts = state.facts.facts if cfg.find_with_facts and state.facts else []

    async def _one(key: str, dimension: str, index: int) -> tuple[FinderResult, AgentCheck]:
        shown = hunks
        group_rules = rules.get(index, [])
        conventions = "\n".join(block for _, _, block in group_rules if block)
        extra = [f"files ({len(groups[index])}, group {index + 1} of {len(groups)})"] if target.is_tree else []
        given_facts = group_facts(facts, groups[index])
        if given_facts:
            extra.append(f"checked facts ({len(given_facts)})")
        given = checks.prompt_labels(
            target, diff=None if target.is_tree else ctx.diff, diff_limit=DIFF_PROMPT_CHARS,
            rules=[(path, included) for path, included, _ in group_rules] if dimension == "conventions" else None,
            extra=extra or None)
        text = prompt.build(target, dimension, ctx.diff, conventions, files=groups[index], facts=given_facts)
        started = checks.now()
        with sdk.recording() as rec:
            try:
                out = await sdk.agent_call_structured(
                    text, FinderOutput,
                    allowed_tools=sdk.FINDER_TOOLS, model=cfg.finder_model, effort=cfg.effort,
                    max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label=f"find:{dimension}",
                )
                error = ""
            except (sdk.RepoTrustError, sdk.UsageLimitError):
                raise
            except sdk.AgentCallError as e:
                log.error("finder %s failed for %s", key, target.slug, exc_info=True)
                error = f"finder {key}: {str(e).splitlines()[0][:200]}"
        check = AgentCheck(
            stage="find", subject=key, given=given,
            calls=[checks.summarise_call(c, target, ctx.snapshot) for c in rec.tool_calls],
            turns=rec.turns, cost_usd=round(rec.cost_usd, 6), started_at=started, ended_at=checks.now(),
        )
        if error:
            check.error, check.outcome = error, f"failed: {checks.failure_kind(error)}"
            return FinderResult(error=error), check
        if ctx.progress:
            ctx.progress.line(f"  finder {key}: {len(out.findings)} findings")
        findings = [_normalise(f, dimension, target, ctx) for f in out.findings]
        check.outcome = f"{len(findings)} finding{'' if len(findings) == 1 else 's'}"
        check.findings = [finding_entry(f, rec.tool_calls, shown, target, ctx) for f in findings]
        return FinderResult(findings=findings, needs=[n for n in out.needs if n.what.strip()]), check

    async def one(pair: tuple[str, str, int]) -> None:
        # Stored as each finder returns: a budget stop mid-stage keeps what
        # was already paid for, and the checkpoint saved then carries it.
        # A tree review runs one dimension's groups at once, so each call ends
        # the interval it opened; the row's time is then the sum over its groups.
        interval = timings.open_interval(state, f"find:{pair[1]}")
        try:
            result, check = await _one(*pair)
        finally:
            interval.ended_at = timings.now()  # a budget stop still ends the interval
        state.finder_results[pair[0]] = result
        state.checks.append(check)

    async def one_reported(job: tuple[str, str, list[str], dict]) -> None:
        key, text, given, piece_hunks = job
        await run_reported(key, text, state, ctx, given=given, hunks=piece_hunks)

    def is_done(key: str) -> bool:
        rep = state.finder_reports.get(key)
        return rep is not None and rep.converted

    jobs = [(one, p) for p in todo] + [(one_reported, r) for r in reported if not is_done(r[0])]

    async def run_job(job) -> None:
        fn, arg = job
        await fn(arg)

    await bounded(jobs, run_job, cfg.concurrency)

    # (key, dimension, result) of every finder: the dimensions', then the reported ones'.
    outcomes = [(key, d, state.finder_results[key]) for key, d, _ in pairs]
    outcomes += [(key, key.partition("@")[0], _reported_result(state, key)) for key, _, _, _ in reported]
    results = [r for _, _, r in outcomes]
    failures = [state.finder_results[key].error for key, _, _ in pairs if state.finder_results[key].error]
    failed = [r.error for r in results if r.error]
    # The trim finder is advisory: its answer alone is not a review.
    defects = [r for _, d, r in outcomes if d != TRIM_DIMENSION] or results
    if all(r.error for r in defects):
        for key, _, _ in pairs:
            del state.finder_results[key]  # a later run tries every finder again
        raise sdk.AgentCallError("every finder failed: " + "; ".join(failed))

    # The record's entry for each finder's stored result (keyed like
    # finder_results): the last successful call (a failed earlier attempt has
    # no findings). A reported finder's findings are on its convert entry.
    entries = {c.subject: c.findings for c in state.checks if c.stage == "find" and not c.error}
    reported_keys = {key for key, _, _, _ in reported}

    seen: dict[tuple[str, int, str], Finding] = {}
    kept: list[Finding] = []
    for pair_key, _, result in outcomes:
        recorded = entries.get(f"{pair_key} convert" if pair_key in reported_keys else pair_key, [])
        for i, finding in enumerate(result.findings):
            entry = recorded[i] if i < len(recorded) else {}
            finding = finding.model_copy(deep=True)  # `finders` grows below; the stored result stays as returned
            key = dedupe_key(finding)
            if key in seen:
                first = seen[key]
                if finding.finder not in first.finders:
                    first.finders.append(finding.finder)
                entry.update(fate="deduped", into=first.id)
                continue
            # Every exported location is one at head, a rejected finding's too; the finding itself stays.
            finding, dropped = gated_locations(target, finding)
            state.errors.extend(f"locations_gate dropped a location of finding {finding.id}: {reason}"
                                for reason in dropped)
            seen[key] = finding
            gate = finding_gate(target, finding)
            state.gate_checks.append(GateCheck(finding_id=finding.id, gate="finding_gate", passed=gate.passed,
                                               rule="" if gate.passed else reason_kind(gate.reason)))
            if gate.passed:
                kept.append(finding)
                entry.update(fate="kept")
            else:
                state.rejected.append(Rejected(finding=finding, reason=gate.reason, stage="find"))
                ctx.gate_reasons.append(gate.reason)
                entry.update(fate="rejected", rule=reason_kind(gate.reason))

    state.findings = kept
    # What the finders could not check blocks no one finding: it is a gap in the review.
    state.needs.extend(gated_need(Need(what=n.what, blocks="review", cause=n.cause, command=n.command,
                                       where=n.where, source="finder"))
                       for r in results for n in r.needs)
    ctx.counts = {"found": len(kept) + len([r for r in state.rejected if r.stage == "find"]),
                  "kept": len(kept), "rejected": len([r for r in state.rejected if r.stage == "find"]),
                  "finder_failures": len(failed)}
    # A reported finder's failure is already in state.errors, through its coverage record.
    state.errors.extend(failures)
    state.stage = "find"
    return state
