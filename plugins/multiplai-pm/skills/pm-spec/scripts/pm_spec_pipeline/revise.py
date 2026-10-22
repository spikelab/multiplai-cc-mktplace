"""`revise`: requirements issues raised after the handoff, fixed and handed off again (item 14, row S9).

It reads `spec-issues.md` in the change dir, or `respec.md` with
`--from-respec`, and treats each issue as a listed finding (`RV-F<n>`):

1. the step's one rewrite: each requirements file an issue names is rewritten
   (every file, for an issue that names none), checked, and published as one
   change;
2. the recheck: the consistency agent, shown the listed issues, gives each a
   status; each must be `resolved`, and a new finding blocks only when critical;
3. then, with every issue resolved: `spec_issues` in `.change.yaml` is
   cleared, the issues file is moved to `spec-issues.resolved-<revision>.md`
   (or `respec.resolved-<revision>.md`), so a later issue starts a new file,
   and the change is handed off again.

`spec-issues.md` holds one issue per `## ` section (a file with none is one
issue). An issue names its requirements file as `requirements/<cap>.md`, or
the requirement by name. `respec.md` has buildme's delta shape: each
`### Requirement:` block under `## ADDED`, `## MODIFIED` or `## REMOVED
Requirements` is one issue; `## Notes considered and not proposed` is not read.

Row S9, stopped after the rewrite published and before the recheck: running
`revise` again runs only the recheck against the listed issues; there is no
second rewrite. A rewrite admitted and never published is spent too: the run
stops on the listed issues until the files are edited (as the other steps).
A new issues file (other text), or any issues file after a passed revise,
starts a new round with its one rewrite. The step's `source` is
`<file name>:<sha256 of its text>`, which is also how `resume` knows a revise
is in flight and which file it read.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from multiplai_core.specs import (
    clear_spec_issues,
    mark_draft,
    parse_requirements,
    spec_revision,
)

from . import consistency, handoff
from . import findings as F
from .artifacts import sha
from .rewrite import change_caps, check_texts, cited_caps, rewrite_requirements
from .run import EXIT_REFUSED, Run, Stop
from .state import StepOutcome

log = logging.getLogger(__name__)

NAME = "revise"
ISSUES_FILE = "spec-issues.md"
RESPEC_FILE = "respec.md"
BLOCK_REASON = "The requirements issues are not all resolved after their one rewrite"

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CAP_PATH_RE = re.compile(r"requirements/([a-z0-9]+(?:-[a-z0-9]+)*)\.md")


def in_flight(run: Run) -> bool | None:
    """`from_respec` of a revise whose rewrite is admitted and whose recheck has
    not passed; None when there is none."""
    step = run.state.steps.get(NAME)
    if not (step and F.rewrite_pending(run, NAME)):
        return None
    return step.source.startswith(RESPEC_FILE + ":")


def issues_path(run: Run, from_respec: bool) -> Path:
    return run.config.change_dir / (RESPEC_FILE if from_respec else ISSUES_FILE)


def _sections(text: str) -> list[tuple[str, str]]:
    marks = list(_H2_RE.finditer(text))
    return [
        (
            m.group(1),
            text[
                m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(text)
            ].strip(),
        )
        for i, m in enumerate(marks)
    ]


def parse_spec_issues(text: str) -> list[str]:
    sections = _sections(text)
    if not sections:
        body = re.sub(r"^#\s+.*$", "", text, count=1, flags=re.MULTILINE).strip()
        return [body] if body else []
    return [f"{title}\n{body}".strip() for title, body in sections if body or title]


def parse_respec(text: str) -> list[str]:
    issues = []
    for title, body in _sections(text):
        m = re.match(r"(ADDED|MODIFIED|REMOVED)\s+Requirements", title, re.IGNORECASE)
        if not m:
            continue
        blocks = re.split(r"(?=^###\s+Requirement:)", body, flags=re.MULTILINE)
        for block in blocks:
            if block.strip().startswith("### Requirement:"):
                issues.append(
                    f"{m.group(1).upper()} requirement (proposed by the build):\n{block.strip()}"
                )
    return issues


def requirement_caps(change_dir: Path, text: str) -> list[str]:
    """The capabilities an issue names: by `requirements/<cap>.md`, or by a requirement name it quotes."""
    caps = change_caps(change_dir)
    found = [c for c in _CAP_PATH_RE.findall(text) if c in caps]
    for cap in caps:
        if cap in found:
            continue
        names = [
            r.name
            for r in parse_requirements(
                (change_dir / "requirements" / f"{cap}.md").read_text()
            )
        ]
        if any(name and name in text for name in names):
            found.append(cap)
    return found


def as_findings(change_dir: Path, issues: list[str]) -> list[dict]:
    out = []
    for issue in issues:
        caps = requirement_caps(change_dir, issue)
        out.append(
            {
                "category": "spec-issue",
                "severity": "major",
                "description": issue,
                "places": [{"file": f"requirements/{c}.md", "where": ""} for c in caps],
            }
        )
    return out


def read_issues(run: Run, from_respec: bool) -> tuple[Path, str, list[str]]:
    path = issues_path(run, from_respec)
    if not path.is_file():
        print(
            f"ERROR: revise: {path} does not exist; there is nothing to revise",
            flush=True,
        )
        raise Stop(EXIT_REFUSED, "no issues file")
    text = path.read_text()
    issues = parse_respec(text) if from_respec else parse_spec_issues(text)
    if not issues:
        print(f"ERROR: revise: {path} holds no issue", flush=True)
        raise Stop(EXIT_REFUSED, "no issues")
    return path, text, issues


async def finish(run: Run, from_respec: bool, checked: str) -> None:
    """Every issue resolved at revision *checked*: clear `spec_issues`, move the
    issues file aside, hand off (which checks again if the files changed since)."""
    change_dir = run.config.change_dir
    path = issues_path(run, from_respec)
    if path.is_file():
        os.replace(path, change_dir / f"{path.stem}.resolved-{checked[:12]}.md")
    clear_spec_issues(change_dir)
    # The recheck was the consistency agent over every spec file, at *checked*.
    F.record_passed(run, consistency.NAME, checked, "Checked by revise")
    run.progress("REVISE_PASSED", "Every issue resolved.")
    await handoff.check_and_hand_off(run)


async def recheck(run: Run, from_respec: bool) -> None:
    checked = await consistency.recheck(run, NAME, BLOCK_REASON)
    await finish(run, from_respec, checked)


async def run_revise(run: Run, *, from_respec: bool = False) -> None:
    change_dir = run.config.change_dir
    step = run.state.steps.get(NAME)
    if step and F.rewrite_pending(run, NAME) and step.rewrite_published:
        # Row S9: the rewrite published; the recheck alone, against the listed issues.
        await recheck(run, from_respec)
        return
    if (
        step
        and step.outcome == "passed"
        and run.state.handoff
        and not (run.state.handoff.committed and run.state.handoff.pushed)
    ):
        if handoff.pending_commit(run):
            handoff.retry_commit(run)
            return
    path, text, issues = read_issues(run, from_respec)
    source = f"{path.name}:{sha(text)}"
    step = run.state.steps.get(NAME)
    if (
        step
        and (step.source != source or step.outcome == "passed")
        and not F.rewrite_pending(run, NAME)
    ):
        # Another issues file, or one raised again after a passed revise: a new
        # round, with its one rewrite.
        run.state.steps[NAME] = StepOutcome()
        run.save()
    revision = spec_revision(change_dir)
    F.reopen_if_edited(run, NAME, revision)
    F.refuse_if_blocked(run, NAME, revision)
    step = run.state.step(NAME)
    listed = F.number_findings(NAME, as_findings(change_dir, issues))
    if step.repair_attempts:
        F.block_on_findings(
            run,
            NAME,
            revision,
            "The rewrite for these issues was already attempted",
            step.repair_findings or listed,
        )
    mark_draft(change_dir)
    run.state.handoff = None
    print("PHASE:REVISE", flush=True)
    F.admit_repair(run, NAME, revision, listed)
    run.state.step(NAME).source = source
    run.save()
    named = [cited_caps(change_dir, f) for f in listed]
    caps = (
        change_caps(change_dir)
        if not all(named)
        else sorted({c for cs in named for c in cs})
    )
    files = await rewrite_requirements(
        run, run.state.step(NAME).repair_findings, caps, label=NAME
    )
    if problems := check_texts(run, files):
        blocking, _ = F.recheck_verdict(listed, [], [], deterministic=problems)
        F.block_on_findings(run, NAME, revision, BLOCK_REASON, blocking)
    from .publish import publish

    publish(run, NAME, files)
    await recheck(run, from_respec)
