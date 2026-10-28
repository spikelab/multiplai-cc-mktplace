"""write: restate every shown finding in plain words, after assess and before export.

One agent call with no tools per `prompts.write.BATCH` findings of one file (a file with more is
cut into several calls), on the assess stage's model, budget label `write`. Each answer is a
`title` (at most 12 words) and a `what_happens` text, which `gates.write_gate` checks in code.
The findings whose answer fails are asked again once, with the gate's reasons; one that fails
again keeps no answer, its id goes to `state.write_failed` and one line to `state.errors`, and
the review and the viewer show its `claim` instead. The finding's `claim` is never changed:
repeats, recheck and the decisions read it.

Shown means status `confirmed`, `unverifiable` or `unverified`, and not labelled `repeat`.
Answers are stored by id as each call returns, and the state saved, so a resume asks only for
the ids with neither an answer nor a place in `write_failed`. A call that fails outright (not a
trust stop, a usage limit or a budget stop, which leave the stage) counts as a failed answer for
each of its findings.
"""

from __future__ import annotations

import logging
from itertools import groupby

from .. import checks, sdk
from ..gates import write_gate
from ..models import AgentCheck, Finding, ReviewState, WriteAnswer, WriteOutput
from ..prompts import write as prompt
from . import RunContext, bounded, save
from .repeats import shown_findings

log = logging.getLogger(__name__)


def write_candidates(state: ReviewState) -> list[Finding]:
    """The shown findings, not repeats, with no answer yet and not given up on, in file order."""
    done = set(state.write_answers) | set(state.write_failed)
    return sorted(
        (
            f
            for f in shown_findings(state)
            if f.id not in done
            and getattr(state.assessments.get(f.id), "label", "") != "repeat"
        ),
        key=lambda f: f.file,
    )


def batches(findings: list[Finding]) -> list[list[Finding]]:
    """Up to `prompt.BATCH` findings per call, each call's findings in one file."""
    out = []
    for _, group in groupby(findings, key=lambda f: f.file):
        items = list(group)
        out += [items[i : i + prompt.BATCH] for i in range(0, len(items), prompt.BATCH)]
    return out


def _material(f: Finding, state: ReviewState) -> list[str]:
    m = prompt.material(f, state.verdicts.get(f.id))
    return [
        m["claim"],
        m["failure_scenario"],
        m["expected_behaviour"],
        m["verifier_reason"],
        *m["cited_quotes"],
    ]


async def run_write(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("write"):
        return state
    ctx.counts = await write_findings(state, ctx, write_candidates(state))
    state.stage = "write"
    return state


async def write_findings(
    state: ReviewState, ctx: RunContext, todo: list[Finding]
) -> dict[str, int]:
    """Ask for the plain-language text of *todo*, in `batches`; return the stage's counts."""
    target, cfg = state.target, ctx.config
    failed_calls = 0

    async def ask(
        findings: list[Finding], problems: dict[str, list[str]] | None
    ) -> dict[str, list[str]]:
        """One call; keeps the answers that pass and returns the gate's reasons per failing id."""
        nonlocal failed_calls
        started, error = checks.now(), ""
        with sdk.recording() as rec:
            try:
                answer = await sdk.agent_call_structured(
                    prompt.build(findings, state.verdicts, problems=problems),
                    WriteOutput,
                    allowed_tools=[],
                    model=cfg.merger_model,
                    effort=cfg.effort,
                    max_turns=cfg.max_turns,
                    cwd=str(ctx.snapshot),
                    budget_label="write",
                )
            except (sdk.RepoTrustError, sdk.UsageLimitError):
                raise
            except sdk.AgentCallError as e:
                error = f"write: {str(e).splitlines()[0][:200]}"
                log.warning("write agent failed for %s: %s", target.slug, error)
                answer, failed_calls = WriteOutput(), failed_calls + 1
        given = {item.id: item for item in answer.findings}
        reasons: dict[str, list[str]] = {}
        for f in findings:
            item = given.get(f.id)
            if item is None:
                reasons[f.id] = [error or "no answer for this id"]
                continue
            bad = write_gate(item.title, item.what_happens, _material(f, state))
            if bad:
                reasons[f.id] = bad
            else:
                state.write_answers[f.id] = WriteAnswer(
                    title=item.title.strip(), what_happens=item.what_happens.strip()
                )
        check = AgentCheck(
            stage="write",
            subject=", ".join(f.id for f in findings),
            given=[f"findings ({len(findings)})"]
            + (["the gate's reasons"] if problems else []),
            calls=[],
            turns=rec.turns,
            cost_usd=round(rec.cost_usd, 6),
            started_at=started,
            ended_at=checks.now(),
            outcome=f"{len(findings) - len(reasons)} written, {len(reasons)} refused",
        )
        if error:
            check.error, check.outcome = error, f"failed: {checks.failure_kind(error)}"
        state.checks.append(check)
        return reasons

    async def one(findings: list[Finding]) -> None:
        reasons = await ask(findings, None)
        save(state, ctx)
        if reasons:
            again = [f for f in findings if f.id in reasons]
            reasons = await ask(again, reasons)
        for fid, why in reasons.items():
            state.write_failed.append(fid)
            state.errors.append(
                f"write: no plain-language text for {fid} ({'; '.join(why)}); its claim is shown"
            )
        save(state, ctx)

    await bounded(batches(todo), one, cfg.concurrency)

    return {
        "written": sum(1 for f in todo if f.id in state.write_answers),
        "fallback": sum(1 for f in todo if f.id in state.write_failed),
        "agent_failures": failed_calls,
    }
