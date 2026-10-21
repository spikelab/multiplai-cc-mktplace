"""The consistency check: one read-only agent reads the proposal, the use cases
and every requirements file together (item 12, row S5).

It reports findings of type `contradiction`, `gap` or `duplicate`, each citing
both places. The step follows the recheck rule in `findings.py`:

1. A first check whose findings are all minor passes; minor findings are
   saved, logged and printed as advisory.
2. Critical and major findings are numbered `CC-F<n>` and saved as the step's
   one rewrite is admitted. The requirements files they cite are rewritten
   (`rewrite.rewrite_requirements`), checked, and published as one change.
3. The recheck is the same agent, shown the listed findings: each must come
   back `resolved`; a new finding blocks only when critical.
4. A block saves the findings with ids and statuses and stops with exit 4
   (`SPEC:BLOCKED`); `.change.yaml` stays `draft`. Running again with the
   files unchanged refuses with no model call and prints them; running after
   an edit gives the step its rewrite back.

The step's revision is `multiplai_core.specs.spec_revision` of the change dir.
"""

from __future__ import annotations

import logging
from typing import Literal

from multiplai_core.specs import spec_revision
from pydantic import BaseModel, Field, field_validator

from . import artifacts, sdk
from . import findings as F
from .prompts import UNTRUSTED_NOTICE
from .prompts.consistency import CONSISTENCY_PROMPT, prior_findings_section
from .publish import publish
from .rewrite import change_caps, check_texts, cited_caps, rewrite_requirements
from .run import Run

log = logging.getLogger(__name__)

NAME = "consistency"
BLOCK_REASON = "The specs still disagree after their one rewrite"


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
    return [{"category": f.type, "severity": f.severity, "description": f.description,
             "suggestion": f.suggestion, "places": [p.model_dump() for p in f.places]} for f in answer.findings]


def check_prompt(run: Run, prior: list[dict] | None = None) -> str:
    change_dir = run.config.change_dir
    use_cases = change_dir / "use-cases.md"
    return CONSISTENCY_PROMPT.format(
        notice=UNTRUSTED_NOTICE, project_context=run.config.project_context,
        proposal_content=artifacts.proposal_text(run) or "(missing)",
        use_cases_content=use_cases.read_text() if use_cases.is_file() else "(missing)",
        requirements_content=artifacts.all_requirements_text(change_dir),
        prior_section=prior_findings_section(prior))


async def check(run: Run, prior: list[dict] | None = None) -> ConsistencyAnswer:
    """One call of the read-only consistency agent."""
    return await sdk.agent_structured(check_prompt(run, prior), ConsistencyAnswer, config=run.config,
                                      step="consistency", label="consistency", cwd=str(run.config.project_dir))


async def recheck(run: Run, name: str, reason: str) -> None:
    """The recheck after a published rewrite: listed findings must be `resolved`,
    a new finding blocks only when critical. Passes, or blocks with exit 4."""
    step = run.state.step(name)
    listed = step.repair_findings
    answer = await check(run, prior=listed)
    blocking, advisory = F.recheck_verdict(listed, answer.prior_assessments, as_dicts(answer))
    step.repair_findings = F.saved_findings(F.assess_listed(listed, answer.prior_assessments))
    F.log_findings(name, "listed", step.repair_findings)
    F.record_advisory(run, name, advisory)
    revision = spec_revision(run.config.change_dir)
    if blocking:
        F.block_on_findings(run, name, revision, reason, blocking)
    F.record_passed(run, name, revision, "Every listed finding resolved")
    run.progress(name.upper() + "_PASSED", "Recheck: every listed finding resolved.\n"
                 + F.findings_report(step.repair_findings))


async def rewrite_and_publish(run: Run, name: str, listed: list[dict], caps: list[str], reason: str) -> None:
    """Rewrite *caps* for the listed findings; publish, or block on a broken text."""
    files = await rewrite_requirements(run, listed, caps, label=name)
    if problems := check_texts(run, files):
        blocking, _ = F.recheck_verdict(listed, [], [], deterministic=problems)
        F.block_on_findings(run, name, spec_revision(run.config.change_dir), reason, blocking)
    publish(run, name, files)


async def run_consistency(run: Run) -> None:
    """The stage. Returns when the specs agree; raises `run.Stop` (exit 4) on a block."""
    change_dir = run.config.change_dir
    revision = spec_revision(change_dir)
    F.restart_if_passed_earlier(run, NAME, revision)
    step = run.state.steps.get(NAME)
    if step and step.outcome == "passed" and step.revision == revision:
        log.info("SKIP consistency: passed at this revision")
        return
    F.reopen_if_edited(run, NAME, revision)
    F.refuse_if_blocked(run, NAME, revision)
    step = run.state.step(NAME)
    if F.rewrite_pending(run, NAME) and step.rewrite_published:
        await recheck(run, NAME, BLOCK_REASON)
        return
    print("PHASE:CONSISTENCY", flush=True)
    answer = await check(run)
    found = as_dicts(answer)
    actionable, advisory = F.first_check_verdict(found)
    F.record_advisory(run, NAME, advisory)
    if not actionable:
        F.record_passed(run, NAME, revision, "No critical or major finding")
        run.progress("CONSISTENCY_PASSED", f"{len(advisory)} advisory finding(s).")
        return
    if step.repair_attempts:
        # The rewrite was spent and never published (a stop part way): no second one.
        F.block_on_findings(run, NAME, revision, BLOCK_REASON, [F.new_finding(f) for f in actionable])
    numbered = F.number_findings(NAME, actionable)
    F.admit_repair(run, NAME, revision, numbered)
    # The requirements files the findings cite; every file when one cites none
    # (a finding between the proposal and the use cases, say).
    named = [cited_caps(change_dir, f) for f in numbered]
    caps = change_caps(change_dir) if not all(named) else sorted({c for cs in named for c in cs})
    await rewrite_and_publish(run, NAME, run.state.step(NAME).repair_findings, caps, BLOCK_REASON)
    await recheck(run, NAME, BLOCK_REASON)
