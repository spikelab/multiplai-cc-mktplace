"""`revise`: requirements issues raised after the handoff, fixed and handed off again.

It reads `spec-issues.md`, or `respec.md` with `--from-respec`, in the change
dir or else in the first component dir that holds one, and treats each issue
as a listed finding (`RV-F<n>`):

1. the step's one rewrite: each specs file an issue names is rewritten (for
   an issue that names none, the specs of the component the issues file is
   in, or every specs file for the change's own), checked, and published as
   one change: the same files stage 3 writes;
2. the recheck: the consistency agent, shown the listed issues, gives each a
   status; each must be `resolved`, and a new finding blocks only when critical;
3. then, with every issue resolved: `spec_issues` in the `.change.yaml` of
   the change and of the component the file is in is cleared, the issues file is moved to `spec-issues.resolved-<revision>.md`
   (or `respec.resolved-<revision>.md`), so a later issue starts a new file,
   and the change is handed off again.

`spec-issues.md` holds one issue per `## ` section (a file with none is one
issue). An issue names its specs file by its path, a capability by its name
in backticks, or a requirement by its name. `respec.md` has buildme's delta shape: each
`### Requirement:` block under `## ADDED`, `## MODIFIED` or `## REMOVED
Requirements` is one issue; `## Notes considered and not proposed` is not read.

Stopped after the rewrite published and before the recheck: running
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

from multiplai_core.specs import clear_spec_issues, component_dirs, parse_requirements

from . import consistency, handoff, layout
from . import findings as F
from .artifacts import sha
from .rewrite import check_texts, cited_units, rewrite_units
from .run import EXIT_REFUSED, Run, Stop
from .state import StepOutcome

log = logging.getLogger(__name__)

NAME = "revise"
ISSUES_FILE = "spec-issues.md"
RESPEC_FILE = "respec.md"
BLOCK_REASON = "The requirements issues are not all resolved after their one rewrite"

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def in_flight(run: Run) -> bool | None:
    """`from_respec` of a revise whose rewrite is admitted and whose recheck has
    not passed; None when there is none."""
    step = run.state.steps.get(NAME)
    if not (step and F.rewrite_pending(run, NAME)):
        return None
    return Path(step.source.rsplit(":", 1)[0]).name == RESPEC_FILE


def issues_path(run: Run, from_respec: bool) -> Path:
    """The issues file in the change dir, else in the first component dir that has one."""
    name = RESPEC_FILE if from_respec else ISSUES_FILE
    change_dir = run.config.change_dir
    for d in [change_dir, *component_dirs(change_dir)]:
        if (d / name).is_file():
            return d / name
    return change_dir / name


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


def issue_units(change_dir: Path, text: str) -> list[str]:
    """The specs files an issue names: by path or capability (`rewrite.cited_units`),
    or by a requirement name it quotes."""
    found = cited_units(change_dir, {"description": text})
    for unit in layout.units(change_dir):
        if unit in found:
            continue
        names = [r.name for r in parse_requirements((change_dir / unit).read_text())]
        if any(name and name in text for name in names):
            found.append(unit)
    return found


def as_findings(change_dir: Path, issues: list[str], default: list[str]) -> list[dict]:
    """Each issue as a finding whose places are the specs files it names, else *default*."""
    out = []
    for issue in issues:
        units = issue_units(change_dir, issue) or default
        out.append(
            {
                "category": "spec-issue",
                "severity": "major",
                "description": issue,
                "places": [{"file": u, "where": ""} for u in units],
            }
        )
    return out


def default_units(change_dir: Path, path: Path) -> list[str]:
    """The specs files of the dir an issues file is in: a component's, or every one."""
    if path.parent == change_dir:
        return []
    prefix = layout.rel(change_dir, path.parent) + "/"
    return [u for u in layout.units(change_dir) if u.startswith(prefix)]


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
        os.replace(path, path.parent / f"{path.stem}.resolved-{checked[:12]}.md")
    clear_spec_issues(change_dir)
    if path.parent != change_dir:
        clear_spec_issues(path.parent)
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
    source = f"{layout.rel(change_dir, path)}:{sha(text)}"
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
    revision = layout.revision(change_dir)
    F.reopen_if_edited(run, NAME, revision)
    F.refuse_if_blocked(run, NAME, revision)
    step = run.state.step(NAME)
    listed = F.number_findings(
        NAME, as_findings(change_dir, issues, default_units(change_dir, path))
    )
    if step.repair_attempts:
        F.block_on_findings(
            run,
            NAME,
            revision,
            "The rewrite for these issues was already attempted",
            step.repair_findings or listed,
        )
    layout.mark_all_draft(change_dir)
    run.state.handoff = None
    print("PHASE:REVISE", flush=True)
    F.admit_repair(run, NAME, revision, listed)
    run.state.step(NAME).source = source
    run.save()
    named = [cited_units(change_dir, f) for f in listed]
    units = (
        layout.units(change_dir)
        if not all(named)
        else sorted({u for us in named for u in us})
    )
    files = await rewrite_units(run, run.state.step(NAME).repair_findings, units, label=NAME)
    if problems := check_texts(run, files):
        blocking, _ = F.recheck_verdict(listed, [], [], deterministic=problems)
        F.block_on_findings(run, NAME, revision, BLOCK_REASON, blocking)
    from .publish import publish

    publish(run, NAME, files)
    await recheck(run, from_respec)
