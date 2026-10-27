"""The consistency check: one read-only agent reads the proposal, the use cases,
every component's plan and every specs file together.

It reports findings of type `contradiction`, `gap` or `duplicate`, each citing
both places. A specs file whose format is broken (a capability name that is
not kebab-case or is used twice, a requirement with no scenario) is a
critical `format` finding found in code, before the agent reads anything, and
so is a `## MODIFIED` or `## REMOVED Requirements` section in a capability that
has no `specs/registry/<capability>.md` (a critical `registry` finding): there
is no recorded behaviour for it to change. The step follows the recheck rule in
`findings.py`:

1. A first check whose findings are all minor passes; minor findings are
   saved, logged and printed as advisory.
2. Critical and major findings are numbered `CC-F<n>` and saved as the step's
   one rewrite is admitted. The specs files they cite are rewritten
   (`rewrite.rewrite_units`), checked, and published as one change.
3. The recheck is the same agent, shown the listed findings: each must come
   back `resolved`; a new finding blocks only when critical.
4. A block saves the findings with ids and statuses and stops with exit 4
   (`SPEC:BLOCKED`); `.change.yaml` stays `draft`. Running again with the
   files unchanged refuses with no model call and prints them; running after
   an edit gives the step its rewrite back.

After the specs review stop (row S13: `steps.specs` passed, so people have
read and edited the files) the check rewrites nothing (`check_reviewed`): it
reports its findings, and a critical or major one blocks with exit 4 and
`SPEC:BLOCKED`, not ready. A person edits the files, pushes and runs `resume`,
which pulls and checks the edited files again (row S17); unchanged files are
refused with no model call. Minor findings are advisory. A check after a
block follows the recheck rule: it is shown the blocking findings, each must
come back `resolved`, and a new finding blocks only when critical. A person
may accept a blocking finding instead (`resume --accept <id> --accept-reason
<why>`, `accept_findings`): it is saved in the run state and in
`accepted-findings.md`, every later check is told not to raise it again, and
when every blocking finding is accepted and no file changed the step passes
with no model call. After `consistency.max_rounds` blocked checks (default 5)
`resume` refuses to check again (`refuse_at_round_cap`) until each open
finding is accepted, or `--check-again` asks for one more round. A pass that
leaves a critical or major finding unfixed prints it under a loud banner and
`SPEC:WARN` (`findings.warn_unfixed`). The one rewrite above
is for specs no review stop showed anyone: `--auto`, an adopted change, a run
from before the staged flow.

The step's revision is `layout.revision` of the change (the change dir's and
every component's `spec_revision`), taken before each agent call (the first check and the recheck): it is the
revision the check read, and the handoff marks ready only that revision.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from multiplai_core.specs import split_capabilities
from pydantic import BaseModel, Field, field_validator

from . import artifacts, layout, sdk
from . import findings as F
from .prompts import UNTRUSTED_NOTICE
from .prompts.consistency import (
    CONSISTENCY_PROMPT,
    accepted_section,
    prior_findings_section,
)
from .publish import publish
from .rewrite import check_texts, cited_units, rewrite_units
from .run import EXIT_BLOCKED, EXIT_REFUSED, MARKER_BLOCKED, Run, Stop

log = logging.getLogger(__name__)

NAME = "consistency"
BLOCK_REASON = "The specs still disagree after their one rewrite"
REVIEWED_BLOCK_REASON = (
    "The reviewed specs disagree; no file was rewritten after the specs review"
)
ACCEPTED_FILE = "accepted-findings.md"
ACCEPTED_HEADER = """# Accepted consistency findings

The consistency check raised each finding below, and a person accepted the
specs as they are (`resume --accept`). pm-spec writes this file; every later
check is told not to raise these again.
"""


def reviewed_next(run: Run) -> str:
    name = run.config.change_name
    return (
        "Edit the files on the feature branch to fix these, push, and run `resume` "
        "again: it pulls them and checks them again. To hand off with a finding left "
        f'as it is, run `resume {name} --accept <id> --accept-reason "<why>"`.'
    )


class Place(BaseModel):
    file: str
    where: str = ""


class ConsistencyFinding(BaseModel):
    type: Literal["contradiction", "gap", "duplicate"]
    severity: Literal["critical", "major", "minor"]
    description: str = Field(min_length=1)
    places: list[Place] = Field(min_length=2)
    suggestion: str = ""

    @field_validator("type", "severity", mode="before")
    @classmethod
    def _lower(cls, value):
        return str(value or "").strip().lower()


class ConsistencyAnswer(BaseModel):
    findings: list[ConsistencyFinding] = Field(default_factory=list)
    prior_assessments: list[F.PriorAssessment] = Field(default_factory=list)


def as_dicts(answer: ConsistencyAnswer) -> list[dict]:
    return [
        {
            "category": f.type,
            "severity": f.severity,
            "description": f.description,
            "suggestion": f.suggestion,
            "places": [p.model_dump() for p in f.places],
        }
        for f in answer.findings
    ]


def check_prompt(
    run: Run, prior: list[dict] | None = None, *, edited: bool = False
) -> str:
    change_dir = run.config.change_dir
    return CONSISTENCY_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        proposal_content=artifacts.proposal_text(run) or "(missing)",
        use_cases_content=artifacts.use_cases_text(run) or "(missing)",
        plans_content=layout.plans_text(change_dir),
        specs_content=layout.all_specs_text(change_dir),
        prior_section=prior_findings_section(prior, edited=edited),
        accepted_section=accepted_section(run.state.accepted_findings),
    )


def format_findings(run: Run) -> list[dict]:
    """A critical `format` finding for each specs file whose format is broken."""
    change_dir = run.config.change_dir
    return [
        {
            "category": "format",
            "severity": "critical",
            "description": f"{unit} is not a valid specs file: every capability needs a "
            "kebab-case `# Capability:` name used once, and every requirement a name and a "
            "scenario with WHEN and THEN",
            "suggestion": "fix the format, keeping the requirements as they are",
            "places": [{"file": unit, "where": "the whole file"}],
        }
        for unit in layout.units(change_dir)
        if not layout.unit_text_valid(unit, (change_dir / unit).read_text())
    ]


_DELTA_HEADING = re.compile(r"^##\s+(MODIFIED|REMOVED)\s+Requirements\b", re.IGNORECASE)


def _delta_kinds(text: str) -> list[str]:
    """`MODIFIED` / `REMOVED` for each such `## ... Requirements` heading in
    *text*, outside fenced code and HTML comments."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    kinds, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and (m := _DELTA_HEADING.match(line)):
            kind = m.group(1).upper()
            if kind not in kinds:
                kinds.append(kind)
    return kinds


def registry_findings(run: Run) -> list[dict]:
    """A critical `registry` finding for each capability with a `## MODIFIED` or
    `## REMOVED Requirements` section and no `specs/registry/<capability>.md`."""
    change_dir = run.config.change_dir
    registry = artifacts.registry_dir(run)
    found = []
    for unit in layout.units(change_dir):
        text = (change_dir / unit).read_text()
        if Path(unit).name == layout.SPECS_FILE:
            try:
                sections = split_capabilities(text)
            except ValueError:
                continue  # a `format` finding already
        else:
            sections = {Path(unit).stem: text}
        for cap, delta in sections.items():
            kinds = _delta_kinds(delta)
            if not kinds or (registry / f"{cap}.md").is_file():
                continue
            sections_named = " and ".join(f"`## {k} Requirements`" for k in kinds)
            found.append(
                {
                    "category": "registry",
                    "severity": "critical",
                    "description": f"{unit}: capability `{cap}` has {sections_named}, "
                    f"but there is no specs/registry/{cap}.md recording what the code "
                    "does today, so there is nothing for it to modify or remove",
                    "suggestion": "put these requirements under `## ADDED Requirements` "
                    "(or name a capability that has a specs/registry file)",
                    "places": [{"file": unit, "where": f"# Capability: {cap}"}],
                }
            )
    return found


async def check(
    run: Run, prior: list[dict] | None = None, *, edited: bool = False
) -> ConsistencyAnswer:
    """One call of the read-only consistency agent."""
    return await sdk.agent_structured(
        check_prompt(run, prior, edited=edited),
        ConsistencyAnswer,
        config=run.config,
        step="consistency",
        label="consistency",
        cwd=str(run.config.project_dir),
    )


async def recheck(run: Run, name: str, reason: str) -> str:
    """The recheck after a published rewrite: listed findings must be `resolved`,
    a new finding blocks only when critical. Passes, or blocks with exit 4.
    Returns the revision it passed at: the files' revision before the agent
    read them, so an edit made while it reads is never counted as checked."""
    step = run.state.step(name)
    listed = step.repair_findings
    revision = layout.revision(run.config.change_dir)
    answer = await check(run, prior=listed)
    blocking, advisory = F.recheck_verdict(
        listed,
        answer.prior_assessments,
        as_dicts(answer),
        deterministic=format_findings(run) + registry_findings(run),
    )
    step.repair_findings = F.saved_findings(
        F.assess_listed(listed, answer.prior_assessments)
    )
    F.log_findings(name, "listed", step.repair_findings)
    F.record_advisory(run, name, advisory)
    if blocking:
        F.block_on_findings(run, name, revision, reason, blocking)
    F.record_passed(run, name, revision, "Every listed finding resolved")
    run.progress(
        name.upper() + "_PASSED",
        "Recheck: every listed finding resolved.\n"
        + F.findings_report(step.repair_findings),
    )
    F.warn_unfixed(run, advisory)
    return revision


async def rewrite_and_publish(
    run: Run, name: str, listed: list[dict], units: list[str], reason: str
) -> None:
    """Rewrite *units* for the listed findings; publish, or block on a broken text."""
    rewritten = await rewrite_units(run, listed, units, label=name)
    if problems := rewritten.problems + check_texts(run, rewritten.files):
        blocking, _ = F.recheck_verdict(listed, [], [], deterministic=problems)
        F.block_on_findings(
            run, name, layout.revision(run.config.change_dir), reason, blocking
        )
    publish(run, name, rewritten.files)


def reviewed(run: Run) -> bool:
    """The specs passed the review stop after stage 3 (row S13), so people have
    read them: `steps.specs` is `passed` (`--auto` records it `skipped`, and an
    adopted change or a run from before the staged flow has no such stop)."""
    step = run.state.steps.get("specs")
    return bool(step and step.outcome == "passed")


def _open(step) -> list[dict]:
    """The blocking findings nobody accepted."""
    return [f for f in step.blocking_findings if f.get("status") != "accepted"]


def accept_findings(run: Run) -> None:
    """`resume --accept <id> --accept-reason <why>`: mark each named blocking
    finding accepted, save it in the run state and `accepted-findings.md`.
    Refuses (exit 2) with no reason, outside a block after the specs review,
    or for an id that is not an open blocking finding."""
    if not run.accept:
        return
    step = run.state.steps.get(NAME)
    blocked = bool(step and step.outcome == "blocked" and reviewed(run))
    open_ids = {f.get("finding_id"): f for f in _open(step)} if blocked else {}
    unknown = [i for i in run.accept if i not in open_ids]
    problem = ""
    if not run.accept_reason.strip():
        problem = (
            "--accept needs --accept-reason: say why the finding is left as it is."
        )
    elif not blocked:
        problem = (
            "--accept takes a finding the consistency check blocked on after the "
            "specs review, and the check is not blocked."
        )
    elif unknown:
        problem = (
            f"--accept: {', '.join(unknown)} is not an open blocking finding; "
            f"the open ones are {', '.join(open_ids) or 'none'}."
        )
    if problem:
        print(f"ERROR: {problem}", flush=True)
        raise Stop(EXIT_REFUSED, problem)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    for finding_id in dict.fromkeys(run.accept):
        finding = open_ids[finding_id]
        finding["status"] = "accepted"
        run.state.accepted_findings.append(
            {
                **F.saved_findings([finding])[0],
                "reason": run.accept_reason.strip(),
                "accepted_at": stamp,
            }
        )
    _write_accepted(run)
    run.save()
    report = F.findings_report([open_ids[i] for i in dict.fromkeys(run.accept)])
    run.progress("CONSISTENCY_ACCEPTED", f"{run.accept_reason.strip()}\n{report}")
    print(f"Accepted:\n{report}", flush=True)


def _write_accepted(run: Run) -> None:
    parts = [ACCEPTED_HEADER]
    for f in run.state.accepted_findings:
        places = "; ".join(F._place(p) for p in f.get("places") or [])
        parts.append(
            f"## {f.get('finding_id', '?')} [{f.get('severity', '?')}] "
            f"({f.get('category') or '?'})\n\n{f.get('text', '')}\n"
            + (f"\nPlaces: {places}\n" if places else "")
            + f"\nAccepted {f.get('accepted_at', '')}: {f.get('reason', '')}\n"
        )
    (run.config.change_dir / ACCEPTED_FILE).write_text("\n".join(parts))


def passed_by_acceptance(run: Run, revision: str) -> bool:
    """Every blocking finding accepted and the files unchanged since the block:
    the check already read these files, so the step passes with no model call."""
    step = run.state.steps.get(NAME)
    if not (
        step
        and step.outcome == "blocked"
        and step.blocked_revision == revision
        and step.blocking_findings
        and not _open(step)
    ):
        return False
    F.record_passed(run, NAME, revision, "Every blocking finding accepted")
    run.progress("CONSISTENCY_PASSED", "Every blocking finding accepted.")
    F.warn_unfixed(run, step.advisory_findings)
    return True


def refuse_at_round_cap(run: Run) -> None:
    """After `consistency.max_rounds` blocked checks, refuse (exit 4, no model
    call) to check again unless `--check-again` asks for one more round."""
    step = run.state.steps.get(NAME)
    cap = run.config.consistency_max_rounds
    if (
        not step
        or step.outcome != "blocked"
        or step.reviewed_blocks < cap
        or not _open(step)
    ):
        return
    if run.check_again:
        log.warning("consistency: past the round cap (%d); one more round", cap)
        return
    name = run.config.change_name
    text = (
        f"{F.WARN_BAR}\n"
        f"The consistency check blocked {step.reviewed_blocks} rounds (the cap is "
        f"{cap} rounds), and is not run again on its own. These findings are open:\n"
        f"{F.findings_report(_open(step))}\n"
        f'Accept each one left as it is: `resume {name} --accept <id> --accept-reason "<why>"`.\n'
        f"Or, if the edits fix them, check once more: `resume {name} --check-again`.\n"
        "If each round keeps finding new problems, the change may need rethinking "
        f"rather than more edits.\n{F.WARN_BAR}"
    )
    print(text, flush=True)
    print(f"{MARKER_BLOCKED}:{run.config.change_dir}", flush=True)
    run.progress("CONSISTENCY_ROUND_CAP", text)
    raise Stop(EXIT_BLOCKED, "round cap")


async def check_reviewed(run: Run, revision: str) -> None:
    """The check after the specs review: reports, rewrites nothing, and blocks
    (exit 4) on a critical or major finding (rows S13, S17). After a block it
    rechecks the open blocking findings, and a new one blocks only when critical."""
    print("PHASE:CONSISTENCY", flush=True)
    step = run.state.step(NAME)
    later = step.outcome == "blocked" and bool(step.blocking_findings)
    listed = _open(step) if later else []
    answer = await check(run, prior=listed, edited=True)
    deterministic = format_findings(run) + registry_findings(run)
    if later:
        blocking, advisory = F.recheck_verdict(
            listed,
            answer.prior_assessments,
            as_dicts(answer),
            deterministic=deterministic,
        )
        blocking = F.number_new(
            NAME, blocking, [*step.blocking_findings, *run.state.accepted_findings]
        )
        reason = f"{REVIEWED_BLOCK_REASON} ({F.carried_and_new(blocking)})"
    else:
        actionable, advisory = F.first_check_verdict(deterministic + as_dicts(answer))
        blocking = F.number_new(NAME, actionable, run.state.accepted_findings)
        reason = REVIEWED_BLOCK_REASON
    F.record_advisory(run, NAME, advisory)
    if blocking:
        step.reviewed_blocks += 1
        next_step = reviewed_next(run)
        if step.reviewed_blocks >= run.config.consistency_max_rounds:
            next_step += (
                f"\nThis is blocked round {step.reviewed_blocks}; the cap is "
                f"{run.config.consistency_max_rounds}, so the next `resume` checks "
                "again only with --check-again (--accept still works)."
            )
        F.block_on_findings(run, NAME, revision, reason, blocking, next_step=next_step)
    F.record_passed(run, NAME, revision, "No critical or major finding")
    run.progress("CONSISTENCY_PASSED", f"{len(advisory)} advisory finding(s).")
    F.warn_unfixed(run, advisory)


async def run_consistency(run: Run) -> None:
    """The stage. Returns when the specs agree; raises `run.Stop` (exit 4) on a block."""
    change_dir = run.config.change_dir
    revision = layout.revision(change_dir)
    F.restart_if_passed_earlier(run, NAME, revision)
    step = run.state.steps.get(NAME)
    if step and step.outcome == "passed" and step.revision == revision:
        log.info("SKIP consistency: passed at this revision")
        return
    if reviewed(run):
        if passed_by_acceptance(run, revision):
            return
        F.refuse_if_blocked(run, NAME, revision, next_step=reviewed_next(run))
        refuse_at_round_cap(run)
        await check_reviewed(run, revision)
        return
    F.reopen_if_edited(run, NAME, revision)
    F.refuse_if_blocked(run, NAME, revision)
    step = run.state.step(NAME)
    if F.rewrite_pending(run, NAME) and step.rewrite_published:
        await recheck(run, NAME, BLOCK_REASON)
        return
    print("PHASE:CONSISTENCY", flush=True)
    answer = await check(run)
    found = format_findings(run) + registry_findings(run) + as_dicts(answer)
    actionable, advisory = F.first_check_verdict(found)
    F.record_advisory(run, NAME, advisory)
    if not actionable:
        F.record_passed(run, NAME, revision, "No critical or major finding")
        run.progress("CONSISTENCY_PASSED", f"{len(advisory)} advisory finding(s).")
        return
    if step.repair_attempts:
        # The rewrite was spent and never published (a stop part way): no second one.
        F.block_on_findings(
            run, NAME, revision, BLOCK_REASON, [F.new_finding(f) for f in actionable]
        )
    numbered = F.number_findings(NAME, actionable)
    F.admit_repair(run, NAME, revision, numbered)
    # The specs files the findings cite; every file when one cites none
    # (a finding between the proposal and the use cases, say).
    named = [cited_units(change_dir, f) for f in numbered]
    units = (
        layout.units(change_dir)
        if not all(named)
        else sorted({u for us in named for u in us})
    )
    await rewrite_and_publish(
        run, NAME, run.state.step(NAME).repair_findings, units, BLOCK_REASON
    )
    await recheck(run, NAME, BLOCK_REASON)
