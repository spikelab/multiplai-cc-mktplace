"""answers: apply the outputs a person saved for the Needs you commands to a finished review.

The review lists each command it needs a person to run (`export.need_commands`, `N1`, `N2`, …).
The person saves each output as `answers/<id>.txt` in the review's directory and runs
`python -m review_pipeline answers <dir>`. Under the review's run lock it then:

- A1 reads each output whole (at most `MAX_CHARS`; a longer one, or one whose id is not a
  command of this review, is refused by name) and hashes it. Outputs whose hash is in
  `state.answers_applied` are done.
- A2 matches each new output to the findings it bears on. An output whose command already
  names findings keeps them with no call; the others (commands that bear on the review as a
  whole) go to one no-tools call, and finding ids it invents are dropped. Each match is saved
  in `state.answer_matches` with each finding's verdict status before it, and those findings'
  verdicts are dropped in the same save. A saved match whose hash is unchanged is kept.
- A3 runs the verifier again for each matched finding with no verdict, given every saved
  output that bears on it as fenced evidence. Its severity is then set as the verify stage
  sets it, from the finder's severity (`original_severity` when recorded) and the new verdict.
- A4 asks the write stage again for each shown finding whose verdict status changed and that
  is not in `state.answers_rewritten`.
- A5 writes `findings.json`, `checks.json` and the markdown again, then records the hashes in
  `state.answers_applied`. Merge, repeats and assess are not run, so finding ids, and the
  decisions keyed by them, stay as they were. No finding is added: what an output shows that
  no finding covers is printed only.

The state is saved after each step, and each step skips what it already has, so running the
command again after any stop continues it. Outputs written before this round started, or a
saved match made at another head, are refused: they were made for an earlier round.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import budget, checks, sdk, timings
from . import target as target_mod
from .export import (
    exported_needs,
    need_commands,
    need_coverage,
    write_checks_file,
    write_findings_file,
)
from .models import (
    SEVERITIES,
    AgentCheck,
    AnswerMatch,
    AnswerMatchOutput,
    NeedCommand,
    ReviewState,
    is_trim,
)
from .orchestrator import ReviewError, _hold, _with_deps
from .prompts import answers as prompt
from .render import write_review
from .stages import RunContext, save
from .stages.repeats import shown_findings
from .stages.verify import (
    VerifyIncomplete,
    important_assessments,
    keep_verdict,
    verify_one,
)
from .stages.verify import settle_severity as settle_from_verdict
from .stages.write import write_findings
from .state import load_state, save_state

log = logging.getLogger(__name__)

ANSWERS_DIR = "answers"
MAX_CHARS = 200_000


@dataclass
class Output:
    """One saved output: its command's id, the command, the text and its hash."""

    id: str
    command: NeedCommand
    text: str
    sha256: str


@dataclass
class AnswersResult:
    lines: list[str] = field(default_factory=list)  # one per output applied, then notes
    cost_usd: float = 0.0
    findings_path: Path | None = None


def read_outputs(target_dir: Path, state: ReviewState) -> list[Output]:
    """A1: every `answers/<id>.txt`, in id order. Refuses an unknown id, a file over `MAX_CHARS`,
    and a file older than this round's first run."""
    folder = target_dir / ANSWERS_DIR
    if not folder.is_dir():
        raise ReviewError(f"no {ANSWERS_DIR}/ folder in {target_dir}")
    commands = {c.id: c for c in need_commands(exported_needs(state))}
    paths = sorted(folder.glob("*.txt"), key=lambda p: (len(p.stem), p.stem))
    started = (
        timings.parse(state.runs[0].started_at).timestamp() if state.runs else None
    )
    for p in paths:
        if started is not None and p.stat().st_mtime < started:
            raise ReviewError(
                f"{ANSWERS_DIR}/ was written for an earlier round; move it aside or re-run the commands"
            )
    unknown = [p.name for p in paths if p.stem not in commands]
    if unknown:
        raise ReviewError(
            f"{', '.join(unknown)} in {folder} name{'s' if len(unknown) == 1 else ''} no command of "
            f"this review (its commands are {', '.join(commands) or 'none'})"
        )
    out = []
    for p in paths:
        with p.open(encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_CHARS + 1)  # one more, to tell a longer file
        if len(text) > MAX_CHARS:
            raise ReviewError(
                f"{p} has more than the {MAX_CHARS:,} characters an output may have; "
                "save only the part that answers the command"
            )
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out.append(Output(id=p.stem, command=commands[p.stem], text=text, sha256=sha))
    return out


async def match(
    state: ReviewState, ctx: RunContext, new: list[Output]
) -> dict[str, AnswerMatch]:
    """A2: the findings each output in *new* bears on. One no-tools call for the outputs whose
    command bears on the review as a whole; none when there are none."""
    known = {f.id for f in state.findings}
    asked = [o for o in new if o.command.blocks == ["review"]]
    answer = AnswerMatchOutput()
    if asked:
        started, error = checks.now(), ""
        with sdk.recording() as rec:
            try:
                answer = await sdk.agent_call_structured(
                    prompt.build(
                        [
                            (o.id, o.command.command, o.command.why, o.text)
                            for o in asked
                        ],
                        state.findings,
                    ),
                    AnswerMatchOutput,
                    allowed_tools=[],
                    model=ctx.config.merger_model,
                    effort=ctx.config.effort,
                    max_turns=ctx.config.max_turns,
                    cwd=str(ctx.snapshot),
                    budget_label="answers",
                )
            except (sdk.RepoTrustError, sdk.UsageLimitError):
                raise
            except sdk.AgentCallError as e:
                error = f"answers: {str(e).splitlines()[0][:200]}"
                log.warning("answers match call failed: %s", error)
        state.checks.append(
            AgentCheck(
                stage="answers",
                subject=", ".join(o.id for o in asked),
                given=[f"outputs ({len(asked)})", f"findings ({len(state.findings)})"],
                calls=[],
                turns=rec.turns,
                cost_usd=round(rec.cost_usd, 6),
                started_at=started,
                ended_at=checks.now(),
                outcome=f"failed: {checks.failure_kind(error)}" if error else "matched",
                error=error,
            )
        )
        if error:
            # No match is saved, so running the command again asks again.
            raise ReviewError(f"the match call failed: {error}")
    given = {item.id: item for item in answer.outputs}
    out = {}
    for o in new:
        if o.command.blocks != ["review"]:
            ids, note = [b for b in o.command.blocks if b in known], ""
        else:
            item = given.get(o.id)
            ids = (
                [i for i in dict.fromkeys(item.finding_ids) if i in known]
                if item
                else []
            )
            note = item.note.strip() if item else ""
        out[o.id] = AnswerMatch(
            sha256=o.sha256,
            head_sha=state.target.head_sha,
            finding_ids=ids,
            before={i: getattr(state.verdicts.get(i), "status", "") for i in ids},
            note=note,
        )
    return out


def evidence(outputs: list[Output], state: ReviewState, finding_id: str) -> str:
    """Every saved output whose match names *finding_id*, each fenced, for the verifier."""
    return "\n\n".join(
        prompt.output_block(o.id, o.command.command, o.text)
        for o in outputs
        if o.id in state.answer_matches
        and finding_id in state.answer_matches[o.id].finding_ids
    )


def settle_severity(state: ReviewState, finding_id: str) -> None:
    """The verify stage's severity rules for one finding with a new verdict, applied again from the
    finder's severity (`state.original_severity` when the stage recorded one)."""
    i = next(n for n, f in enumerate(state.findings) if f.id == finding_id)
    f = state.findings[i]
    waiting = {n.blocks for n in state.needs}
    settle_from_verdict(
        state, i, state.original_severity.get(finding_id, f.severity), waiting
    )
    if is_trim(f):
        state.findings[i] = state.findings[i].model_copy(
            update={"severity": SEVERITIES[-1]}
        )


async def apply_answers(target_dir: Path, config) -> AnswersResult:
    """Apply the saved outputs in *target_dir*/answers/ to its finished review (A1–A5)."""
    with contextlib.ExitStack() as lock:
        _hold(lock, target_dir)
        state = load_state(target_dir / "review-state.json")
        if state is None:
            raise ReviewError(f"no readable review-state.json in {target_dir}")
        if not state.past("export"):
            raise ReviewError(
                f"the review in {target_dir} has not finished; run resume first"
            )
        moved = [
            oid
            for oid, m in state.answer_matches.items()
            if m.head_sha != state.target.head_sha
        ]
        if moved:
            raise ReviewError(
                f"{ANSWERS_DIR}/ was written for an earlier round; move it aside or re-run the commands"
            )
        outputs = read_outputs(target_dir, state)
        new = [o for o in outputs if o.sha256 not in state.answers_applied]
        if not new:
            return AnswersResult(lines=["no new output to apply"])

        ledger = budget.start(config.max_cost_usd, state.budget)
        spent_before = ledger.cost_usd
        snapshot = target_mod.snapshot_head(state.target, target_dir / "tree")
        try:
            _with_deps(state, snapshot, config)
            ctx = RunContext(
                config=config, snapshot=snapshot, diff="", target_dir=target_dir
            )
            timings.open_interval(state, "run")
            timings.open_interval(state, "answers")
            try:
                result = await _apply(state, ctx, outputs, new, target_dir)
            finally:
                timings.close_interval(state, "answers")
                timings.close_interval(state, "run")
                state.budget = ledger.to_state()
                save_state(state, target_dir)
        finally:
            shutil.rmtree(snapshot, ignore_errors=True)
        result.cost_usd = ledger.cost_usd - spent_before
        return result


async def _apply(
    state: ReviewState,
    ctx: RunContext,
    outputs: list[Output],
    new: list[Output],
    target_dir: Path,
) -> AnswersResult:
    # A2: match the outputs not matched at this hash yet, and drop their findings' verdicts.
    unmatched = [
        o
        for o in new
        if getattr(state.answer_matches.get(o.id), "sha256", None) != o.sha256
    ]
    if unmatched:
        matches = await match(state, ctx, unmatched)
        for oid, m in matches.items():
            state.answer_matches[oid] = m
            for fid in m.finding_ids:
                state.verdicts.pop(fid, None)
                if fid in state.answers_rewritten:
                    state.answers_rewritten.remove(fid)
        save(state, ctx)

    matched = list(
        dict.fromkeys(
            fid for o in new for fid in state.answer_matches[o.id].finding_ids
        )
    )
    before: dict[str, str] = {}
    for o in new:
        for fid, status in state.answer_matches[o.id].before.items():
            before.setdefault(fid, status)

    # A3: verify again each matched finding with no verdict, with the outputs as evidence.
    failed: dict[str, str] = {}
    for f in [
        f for f in state.findings if f.id in matched and f.id not in state.verdicts
    ]:
        verdict, check, gate_check = await verify_one(
            state, ctx, f, evidence=evidence(outputs, state, f.id)
        )
        if verdict is None:
            state.checks.append(check)
            failed[f.id] = check.error
            save(state, ctx)
            continue
        keep_verdict(state, f, verdict, check, gate_check)
        settle_severity(state, f.id)
        state.assessments.update(important_assessments(state))
        save(state, ctx)
    if failed:
        raise VerifyIncomplete(sorted(failed), next(iter(failed.values())))

    # A4: the plain-language text of each shown finding whose verdict changed, once.
    changed = [
        fid for fid in matched if state.verdicts[fid].status != before.get(fid, "")
    ]
    shown = {f.id: f for f in shown_findings(state)}
    for fid in changed:
        if fid not in shown or fid in state.answers_rewritten:
            continue
        state.write_answers.pop(fid, None)
        if fid in state.write_failed:
            state.write_failed.remove(fid)
        await write_findings(state, ctx, [shown[fid]])
        state.answers_rewritten.append(fid)
        save(state, ctx)

    # A5: export again; the hashes are recorded only once the files are written.
    need_coverage(state)
    findings_path = write_findings_file(state, target_dir)
    write_checks_file(state, target_dir)
    write_review(state, target_dir, deployed=target_mod.deployed(state.target))
    state.answers_applied += [
        o.sha256 for o in new if o.sha256 not in state.answers_applied
    ]
    save(state, ctx)

    lines = []
    for o in new:
        m = state.answer_matches[o.id]
        moves = [
            f"{fid} {m.before.get(fid) or 'none'}→{state.verdicts[fid].status}"
            for fid in m.finding_ids
            if state.verdicts[fid].status != m.before.get(fid)
        ]
        if not m.finding_ids:
            lines.append(f"{o.id} → bears on no finding")
        else:
            lines.append(
                f"{o.id} → {', '.join(moves) if moves else 'no verdict changed'}"
            )
        if m.note:
            lines.append(f"  {o.id} note (no finding covers it): {m.note}")
    return AnswersResult(lines=lines, findings_path=findings_path)
