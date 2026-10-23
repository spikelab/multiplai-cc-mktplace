"""The one-rewrite rule every checked step follows, and the findings it acts on.

Restated from buildme's plan recheck (`spec_generator.py`: `number_findings`,
`recheck_verdict`, `reopen_if_edited`, `refuse_if_blocked`), not imported:

- A step gets one rewrite. The findings it is asked to fix are numbered
  (`<PREFIX>-F<n>`) and saved in `StepOutcome.repair_findings` in the same
  checkpoint that admits the rewrite (`admit_repair`).
- The recheck after the rewrite is shown those findings and answers a status
  for each (`PriorAssessment`). `recheck_verdict`: a listed finding that is not
  `resolved` blocks (no status counts as `cannot_verify`); a new finding blocks
  only when it is critical; every other new finding is advisory.
- Every finding acted on is saved in the state, logged one line each, written
  to the progress file and printed.
- A block on findings saves `blocked_revision`. A resume at that revision
  refuses with no model call and prints the findings (`refuse_if_blocked`);
  one at another revision means the user edited the files, and the step gets
  its rewrite back (`reopen_if_edited`).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, field_validator

from .run import EXIT_BLOCKED, MARKER_BLOCKED, Run, Stop
from .state import StepOutcome

log = logging.getLogger(__name__)

PRIOR_STATUSES = ("resolved", "cannot_verify", "partially_resolved", "still_present")
SEVERITIES = ("critical", "major", "minor")
ACTIONABLE = ("critical", "major")
FINDING_ID_PREFIXES = {"consistency": "CC", "revise": "RV"}

REOPEN_LINE = (
    "Edit the files to fix these and run again; the step then gets one rewrite."
)


class PriorAssessment(BaseModel):
    """A recheck's status for one finding the rewrite was asked to fix."""

    finding_id: str
    status: Literal["resolved", "cannot_verify", "partially_resolved", "still_present"]
    evidence: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _known_status(cls, value):
        """An unknown status is `cannot_verify`, never `resolved`."""
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        return text if text in PRIOR_STATUSES else "cannot_verify"


def finding_text(finding: dict) -> str:
    if finding.get("text"):
        return str(finding["text"])
    return str(finding.get("description", "")) + (
        f" Fix: {finding['suggestion']}" if finding.get("suggestion") else ""
    )


def number_findings(step: str, findings: list[dict], *, start: int = 1) -> list[dict]:
    """Copies of *findings*, each with an id `<prefix>-F<n>` and the text the
    rewrite and the recheck are shown."""
    prefix = FINDING_ID_PREFIXES[step]
    return [
        {
            **f,
            "finding_id": f"{prefix}-F{n}",
            "severity": f.get("severity", "major"),
            "category": f.get("category", ""),
            "text": finding_text(f),
        }
        for n, f in enumerate(findings, start)
    ]


SAVED_KEYS = (
    "finding_id",
    "severity",
    "category",
    "text",
    "places",
    "status",
    "evidence",
)


def saved_findings(findings: list[dict]) -> list[dict]:
    return [{k: f[k] for k in SAVED_KEYS if k in f} for f in findings]


def assess_listed(listed: list[dict], assessments) -> list[dict]:
    """Each listed finding with the recheck's status; none given is `cannot_verify`."""
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


def new_finding(finding: dict) -> dict:
    out = {
        "finding_id": "",
        "severity": finding.get("severity", "major"),
        "category": finding.get("category", ""),
        "text": finding_text(finding),
        "status": "new",
    }
    if finding.get("places"):
        out["places"] = finding["places"]
    return out


def recheck_verdict(
    listed: list[dict],
    assessments,
    new_findings: list[dict],
    *,
    deterministic: list[dict] = (),
) -> tuple[list[dict], list[dict]]:
    """(blocking, advisory) after a rewrite. Blocking: a listed finding not
    `resolved`, a new critical finding, and every deterministic finding (a
    rewrite that broke a file's format). Advisory: every other new finding."""
    blocking = [
        f for f in assess_listed(listed, assessments) if f["status"] != "resolved"
    ]
    blocking += [new_finding(f) for f in deterministic]
    advisory = []
    for finding in new_findings:
        (blocking if finding.get("severity") == "critical" else advisory).append(
            new_finding(finding)
        )
    return blocking, advisory


def first_check_verdict(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """(actionable, advisory) for a first check: critical and major earn the rewrite."""
    actionable = [f for f in findings if f.get("severity") in ACTIONABLE]
    advisory = [new_finding(f) for f in findings if f.get("severity") not in ACTIONABLE]
    return actionable, advisory


def findings_report(findings: list[dict]) -> str:
    """One line per finding: id, status, severity, category, text, places, evidence."""
    lines = []
    for f in findings:
        places = "; ".join(_place(p) for p in f.get("places") or [])
        lines.append(
            "- "
            + (f"{f['finding_id']} " if f.get("finding_id") else "")
            + f"{f.get('status') or 'open'} [{f.get('severity', '?')}] ({f.get('category') or '?'}) "
            + finding_text(f)
            + (f" Places: {places}." if places else "")
            + (f" Evidence: {f['evidence']}" if f.get("evidence") else "")
        )
    return "\n".join(lines)


def _place(place) -> str:
    if isinstance(place, dict):
        where = place.get("file", "?")
        return f"{where} ({place['where']})" if place.get("where") else str(where)
    return str(place)


def log_findings(step: str, label: str, findings: list[dict]) -> None:
    for f in findings:
        log.warning(
            "%s %s finding id=%s status=%s severity=%s text=%s",
            step,
            label,
            f.get("finding_id") or "-",
            f.get("status") or "open",
            f.get("severity", "?"),
            finding_text(f)[:300],
        )


def record_advisory(run: Run, name: str, advisory: list[dict]) -> None:
    """Save, log and write a step's advisory findings. They block nothing."""
    step = run.state.step(name)
    step.advisory_findings = saved_findings(advisory)
    if advisory:
        log_findings(name, "advisory", advisory)
        run.progress(name.upper() + "_ADVISORY", findings_report(advisory))
        print(
            f"{name}: advisory findings (not blocking)\n{findings_report(advisory)}",
            flush=True,
        )


def admit_repair(run: Run, name: str, revision: str, numbered: list[dict]) -> None:
    """Spend the step's one rewrite on *numbered* findings, in one checkpoint."""
    step = run.state.step(name)
    if step.repair_attempts:
        raise RuntimeError(f"{name} rewrite already attempted")
    step.outcome = "blocked"
    step.reason = "Rewrite admitted; recheck pending"
    step.revision = revision
    step.repair_attempts = 1
    step.repair_findings = saved_findings(numbered)
    step.rewrite_published = False
    step.blocked_revision = None
    step.blocking_findings = []
    run.save()
    log_findings(name, "listed", numbered)
    run.progress(
        name.upper() + "_REWRITE",
        "One rewrite admitted for:\n" + findings_report(numbered),
    )


def record_passed(run: Run, name: str, revision: str, reason: str = "") -> None:
    step = run.state.step(name)
    step.outcome = "passed"
    step.reason = reason
    step.revision = revision
    step.blocking_findings = []
    step.blocked_revision = None
    run.save()


def record_skipped(run: Run, name: str, revision: str, reason: str) -> None:
    step = run.state.step(name)
    step.outcome = "skipped"
    step.reason = reason
    step.revision = revision
    run.save()


def record_error(run: Run, name: str, reason: str) -> None:
    """A step stopped by an error, not by findings: no `blocked_revision`, so a resume runs it again."""
    step = run.state.step(name)
    if step.outcome != "blocked" or step.reason != "Rewrite admitted; recheck pending":
        step.outcome = "blocked"
        step.reason = reason
    run.save()
    run.progress(name.upper() + "_ERROR", reason)


def block_on_findings(
    run: Run,
    name: str,
    revision: str,
    reason: str,
    blocking: list[dict],
    *,
    next_step: str = REOPEN_LINE,
) -> None:
    """Record a final block on *blocking*, save, log and print them with
    *next_step*, and stop with exit 4."""
    step = run.state.step(name)
    step.outcome = "blocked"
    step.reason = reason
    step.revision = revision
    step.blocking_findings = saved_findings(blocking)
    step.blocked_revision = revision
    run.save()
    log_findings(name, "blocking", blocking)
    report = findings_report(blocking)
    run.progress(name.upper() + "_BLOCKED", f"{reason}\n{report}")
    print(f"{name} blocked: {reason}\n{report}\n{next_step}", flush=True)
    print(f"{MARKER_BLOCKED}:{run.config.change_dir}", flush=True)
    raise Stop(EXIT_BLOCKED, reason)


def refusal_text(name: str, step: StepOutcome, next_step: str = REOPEN_LINE) -> str:
    parts = [f"{name} is blocked at this revision of the files."]
    if step.repair_findings:
        parts += [
            "Findings the rewrite was asked to fix:",
            findings_report(step.repair_findings),
        ]
    if step.blocking_findings:
        parts += ["Blocking findings:", findings_report(step.blocking_findings)]
    elif step.reason:
        parts.append(step.reason)
    parts.append(next_step)
    return "\n".join(parts)


def refuse_if_blocked(
    run: Run, name: str, revision: str, *, next_step: str = REOPEN_LINE
) -> None:
    """Refuse, with no model call, a step findings blocked at *revision* (exit 4)."""
    step = run.state.steps.get(name)
    if step and step.outcome == "blocked" and step.blocked_revision == revision:
        text = refusal_text(name, step, next_step)
        print(text, flush=True)
        print(f"{MARKER_BLOCKED}:{run.config.change_dir}", flush=True)
        log.error("%s refused: %s", name, text.replace("\n", " | ")[:1000])
        raise Stop(EXIT_BLOCKED, f"{name} blocked")


def reopen_if_edited(run: Run, name: str, revision: str) -> None:
    """A step findings blocked whose files changed since gets its one rewrite back."""
    step = run.state.steps.get(name)
    if not (
        step
        and step.outcome == "blocked"
        and step.blocked_revision
        and step.blocked_revision != revision
    ):
        return
    step.repair_attempts = 0
    step.repair_findings = []
    step.rewrite_published = False
    step.blocked_revision = None
    log.warning(
        "%s: the files changed since it was blocked; one rewrite allowed again", name
    )
    run.progress(
        name.upper() + "_REOPENED",
        "The files changed since the block; one rewrite allowed again.",
    )
    run.save()


def restart_if_passed_earlier(run: Run, name: str, revision: str) -> None:
    """A step that passed at another revision checks the new files as a first
    check, with its one rewrite (rows S7 and S6's re-check)."""
    step = run.state.steps.get(name)
    if step and step.outcome in ("passed", "skipped") and step.revision != revision:
        run.state.steps[name] = StepOutcome()
        run.save()


def rewrite_pending(run: Run, name: str) -> bool:
    """The step's rewrite was admitted and its recheck has not finished."""
    step = run.state.steps.get(name)
    return bool(
        step
        and step.repair_attempts
        and step.outcome == "blocked"
        and step.blocked_revision is None
        and step.repair_findings
    )
