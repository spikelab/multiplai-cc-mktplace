"""The consistency check: one read-only agent reads the proposal, the use cases,
every component's plan and every specs file together.

It reports findings of type `contradiction`, `gap` or `duplicate`, each citing
both places. A specs file whose format is broken (a capability name that is
not kebab-case or is used twice, a requirement with no scenario) is a
critical `format` finding found in code, before the agent reads anything, and
so is a `## MODIFIED` or `## REMOVED Requirements` section in a capability that
has no `specs/registry/<capability>.md` (a critical `registry` finding): there
is no recorded behaviour for it to change. A mention in a component's
`specs.md` of another component's id, where neither is in the other's
transitive `depends_on`, is a major `gap` finding found in code
(`citation_findings`); on a later check code, not the agent, says whether a
listed one is resolved (`settle_citations`). The step follows the recheck rule
in `findings.py`:

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

The component check (`component_check.py`) runs before the agent, one agent
per component: its findings, tagged with their component, join the list the
verdict reads and block like the agent's. A listed one gets its status from
its component's next check, not from the agent. In `recheck` it runs on the
components the rewrite was given and those that depend on them, and a new
critical or major finding from it blocks, as the rewrite caused it.

A table in a specs file whose header row is a table's header row in
`ticket.md` must match that table cell for cell, whitespace aside; a mismatch
is a critical `format` finding found in code (`table_findings`).

The first check reads every document in full. Each finished check saves the
sha256 of what it read in `RunState.checked_digests` (`document_digests`),
and the next check reads in full only the specs files changed since, those a
changed one names (by component id or capability name), those whose plan
changed and those a listed finding cites; every other specs file goes in by
its capability names and requirement titles (`what_to_read`). A change to the
proposal or the use cases, or no saved digests, makes a full check. On such a
check a new major finding the agent raises that cites only unchanged files,
and that no earlier finding of the step had, is advisory (`quiet_unchanged`);
a new critical one still blocks. The digests are cleared before each check
and saved with its outcome, so a check stopped before then leaves none, and
the next check is a full one (row S40).

Before each check, `counts.py` appends one advisory line of regex counts
(refusals with no status, "as before" THENs with no baseline, "as today"
statements with no named function, commit pins) to the progress file.

The step's revision is `layout.revision` of the change (the change dir's and
every component's `spec_revision`), taken before each agent call (the first check and the recheck): it is the
revision the check read, and the handoff marks ready only that revision.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from multiplai_core.specs import component_dirs, read_change_meta, split_capabilities
from pydantic import BaseModel, Field, field_validator

from . import artifacts, counts, layout, sdk
from . import findings as F
from .prompts import UNTRUSTED_NOTICE
from .prompts.consistency import (
    CONSISTENCY_PROMPT,
    accepted_section,
    prior_findings_section,
)
from .publish import publish
from .rewrite import check_texts, cited_units, rewrite_units, target_units
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
    type: Literal["contradiction", "gap", "duplicate", "infeasible"]
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


CHANGE_WIDE = ("proposal.md", "use-cases.md")
UNCHANGED_MARK = (
    "(unchanged since the last check: capability names and requirement titles only)"
)
UNCHANGED_NOTE = (
    " [Advisory: it cites only files unchanged since the last check, and no "
    "earlier check raised it.]"
)


class Reading(NamedTuple):
    """What one check reads. *digests*: the sha256 of each document now
    (`document_digests`). *changed*: the documents whose digest differs from
    the last finished check's, or that it did not read; None when no digests
    are saved, which makes a full check. *full*: the spec files read in full."""

    digests: dict[str, str]
    changed: set[str] | None
    full: set[str]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def document_digests(run: Run) -> dict[str, str]:
    """The sha256 of each document the check reads, by its path in the change
    dir: `proposal.md`, `use-cases.md`, each `plan.md` and each spec file."""
    change_dir = run.config.change_dir
    out = {
        "proposal.md": _sha(artifacts.proposal_text(run)),
        "use-cases.md": _sha(artifacts.use_cases_text(run)),
    }
    for d in layout.spec_dirs(change_dir):
        if (d / layout.PLAN_FILE).is_file():
            out[layout.rel(change_dir, d / layout.PLAN_FILE)] = _sha(
                (d / layout.PLAN_FILE).read_text()
            )
    for unit in layout.units(change_dir):
        out[unit] = _sha((change_dir / unit).read_text())
    return out


def _plan_of(unit: str) -> str:
    """The `plan.md` beside a spec file (beside its `requirements/` dir, in the old layout)."""
    parent = PurePosixPath(unit).parent
    if parent.name == layout.OLD_DIR:
        parent = parent.parent
    return str(parent / layout.PLAN_FILE)


def _word(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")


def _cited_by(change_dir: Path, text: str, units: list[str]) -> set[str]:
    """The spec files *text* names: by their component's id, or by one of
    their capability names, each as a whole word."""
    out = set()
    for unit in units:
        parent = PurePosixPath(_plan_of(unit)).parent
        names = layout.unit_capabilities(unit, (change_dir / unit).read_text())
        if parent.parent.name == "components":
            names = [parent.name, *names]
        if any(_word(n).search(text) for n in names):
            out.add(unit)
    return out


def what_to_read(run: Run, prior: list[dict] | None = None) -> Reading:
    """The documents a check reads in full. With no saved digests, every spec
    file. Otherwise, when the proposal or the use cases changed, every spec
    file too, as each spec may rest on them; else each spec file that changed,
    each one whose component's plan changed, each one a changed spec file names
    (its component id or a capability name), each one that names the id of a
    component whose spec or plan was deleted, and each one a finding in *prior*
    cites. A document deleted since the last check counts as changed. Every other spec file goes in by its capability names and
    requirement titles."""
    change_dir = run.config.change_dir
    digests = document_digests(run)
    units = layout.units(change_dir)
    saved = run.state.checked_digests
    if not saved:
        return Reading(digests, None, set(units))
    changed = {
        k for k in digests.keys() | saved.keys() if saved.get(k) != digests.get(k)
    }
    if changed & set(CHANGE_WIDE):
        return Reading(digests, changed, set(units))
    edited = [u for u in units if u in changed]
    full = set(edited) | {u for u in units if _plan_of(u) in changed}
    for unit in edited:
        full |= _cited_by(change_dir, (change_dir / unit).read_text(), units)
    for gone in changed - digests.keys():
        parent = PurePosixPath(_plan_of(gone)).parent
        if parent.parent.name == "components":
            word = _word(parent.name)
            full |= {u for u in units if word.search((change_dir / u).read_text())}
    for finding in prior or []:
        full |= set(cited_units(change_dir, finding))
    return Reading(digests, changed, full)


def specs_section(run: Run, reading: Reading | None) -> str:
    """The `## Specs` text of a check: every spec file in full, or, when
    *reading* leaves some out, a note and their outlines."""
    change_dir = run.config.change_dir
    if reading is None or set(layout.units(change_dir)) <= reading.full:
        return layout.all_specs_text(change_dir)
    try:
        where = change_dir.relative_to(run.config.project_dir).as_posix()
    except ValueError:
        where = str(change_dir)
    note = (
        f"The last check read every file below in full. A file marked "
        f"{UNCHANGED_MARK} has not changed since then; its whole text is at "
        f"`{where}/<path>` in the repository, if a problem needs it.\n\n"
    )
    return note + layout.specs_text(
        change_dir, reading.full, placeholder=UNCHANGED_MARK
    )


def _names_changed(change_dir: Path, finding: dict, changed: set[str]) -> bool:
    """A place of *finding* is a document in *changed*, by path or capability name."""
    for place in finding.get("places") or []:
        raw = str(place.get("file", "") if isinstance(place, dict) else place)
        raw = raw.strip().strip("`")
        if any(raw == k or raw.endswith("/" + k) for k in changed):
            return True
    return bool(set(cited_units(change_dir, finding)) & changed)


def _place_files(finding: dict) -> frozenset[str]:
    return frozenset(
        str(p.get("file", "") if isinstance(p, dict) else p).strip().strip("`")
        for p in finding.get("places") or []
    )


def quiet_unchanged(
    run: Run, found: list[dict], reading: Reading, earlier: list[dict]
) -> tuple[list[dict], list[dict]]:
    """(kept, advisory) of the agent's findings on a check that read only what
    changed: a major finding whose places are all documents unchanged since
    the last check, and that no earlier finding of the step had (the same
    category and the same files), is advisory. A critical one still blocks."""
    if reading.changed is None:
        return found, []
    before = {(f.get("category", ""), _place_files(f)) for f in earlier}
    change_dir = run.config.change_dir
    kept, quiet = [], []
    for f in found:
        if (
            f.get("severity") == "major"
            and not _names_changed(change_dir, f, reading.changed)
            and (f.get("category", ""), _place_files(f)) not in before
        ):
            quiet.append(
                F.new_finding({**f, "description": f["description"] + UNCHANGED_NOTE})
            )
        else:
            kept.append(f)
    return kept, quiet


def _earlier(run: Run, name: str) -> list[dict]:
    step = run.state.step(name)
    return [
        *step.blocking_findings,
        *step.advisory_findings,
        *step.repair_findings,
        *run.state.accepted_findings,
    ]


def check_prompt(
    run: Run,
    prior: list[dict] | None = None,
    *,
    edited: bool = False,
    reading: Reading | None = None,
) -> str:
    change_dir = run.config.change_dir
    return CONSISTENCY_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        proposal_content=artifacts.proposal_text(run) or "(missing)",
        use_cases_content=artifacts.use_cases_text(run) or "(missing)",
        plans_content=layout.plans_text(change_dir),
        specs_content=specs_section(run, reading),
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


_NO_PATH = re.compile(r"no path to `([^`]+)`")


def _closure(deps: dict[str, list[str]], cid: str) -> set[str]:
    """Every component *cid* depends on, directly or through others."""
    seen: set[str] = set()
    todo = list(deps.get(cid, []))
    while todo:
        dep = todo.pop()
        if dep not in seen:
            seen.add(dep)
            todo += deps.get(dep, [])
    return seen


def citation_findings(run: Run) -> list[dict]:
    """A major `gap` finding for each mention, in a component's `specs.md`, of
    another component's id as a whole word, where neither component is in the
    other's transitive `depends_on`: the citing one may be built before the
    cited one, or without it. One finding per mention."""
    change_dir = run.config.change_dir
    comps = component_dirs(change_dir)
    deps = {
        d.name: [str(x) for x in read_change_meta(d).get("depends_on") or []]
        for d in comps
    }
    reach = {cid: _closure(deps, cid) for cid in deps}
    words = {
        cid: re.compile(r"(?<![\w-])" + re.escape(cid) + r"(?![\w-])") for cid in deps
    }
    found = []
    for d in comps:
        a, path = d.name, d / layout.SPECS_FILE
        if not path.is_file():
            continue
        unit = layout.rel(change_dir, path)
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for b in deps:
                if b == a or b in reach[a] or a in reach[b]:
                    continue
                hits = len(words[b].findall(line))
                for k in range(hits):
                    which = (
                        f" (mention {k + 1} of {hits} on this line)" if hits > 1 else ""
                    )
                    found.append(
                        {
                            "category": "gap",
                            "severity": "major",
                            "description": f"{unit}:{n} cites `{b}`{which}, but neither "
                            f"`{a}` nor `{b}` depends on the other, so `{a}` can be built "
                            f'before `{b}` or without it: "{line.strip()[:200]}"',
                            "suggestion": f"take the reliance on `{b}` out of this spec, "
                            f"or add `{b}` to `depends_on` in components/{a}/.change.yaml "
                            f"and to `{a}`'s row in components.md",
                            "places": [
                                {"file": unit, "where": f"line {n}"},
                                {
                                    "file": f"components/{a}/.change.yaml",
                                    "where": f"depends_on: no path to `{b}`",
                                },
                            ],
                        }
                    )
    return found


def citation_pair(finding: dict) -> tuple[str, str] | None:
    """(citing, cited) for a finding `citation_findings` raised; None for any other."""
    places = finding.get("places") or []
    if len(places) < 2 or not str(places[1].get("file", "")).endswith(".change.yaml"):
        return None
    m = _NO_PATH.search(str(places[1].get("where", "")))
    return (Path(places[1]["file"]).parent.name, m.group(1)) if m else None


def settle_citations(
    listed: list[dict], assessments, accepted: list[dict], current: list[dict]
) -> tuple[list, list[dict]]:
    """The agent cannot see `depends_on`, so code gives each listed citation
    finding its status: `resolved` when no current mention has its pair, else
    `still_present`. Returns the assessments with those in place of the agent's,
    and the current citation findings whose pair no listed or accepted finding
    carries (the new ones)."""
    now = {citation_pair(f) for f in current}
    ours = {}
    for f in listed:
        if (pair := citation_pair(f)) is not None:
            ours[f["finding_id"]] = F.PriorAssessment(
                finding_id=f["finding_id"],
                status="still_present" if pair in now else "resolved",
                evidence="checked in code against depends_on and the specs files",
            )
    out = [a for a in assessments or [] if a.finding_id not in ours]
    carried = {citation_pair(f) for f in [*listed, *accepted]} - {None}
    return out + list(ours.values()), [
        f for f in current if citation_pair(f) not in carried
    ]


TICKET_FILE = "ticket.md"
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")


def _cells(line: str) -> tuple[str, ...]:
    """The cells of a markdown table row, each with its whitespace collapsed."""
    inner = line.strip().removeprefix("|").removesuffix("|")
    return tuple(" ".join(c.split()) for c in inner.split("|"))


def markdown_tables(text: str) -> list[tuple[int, list[tuple[str, ...]]]]:
    """Each markdown table in *text*, outside fenced code: (the line number of
    its header row, its rows as cells, the separator row left out)."""
    tables, rows, start, fenced = [], [], 0, False
    lines = text.splitlines()
    for n, line in enumerate([*lines, ""], 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        is_row = not fenced and line.strip().startswith("|")
        if is_row:
            if not rows:
                start = n
            rows.append(_cells(line))
            continue
        if len(rows) >= 2 and all(_SEPARATOR_CELL.match(c) for c in rows[1] if c):
            tables.append((start, [rows[0], *rows[2:]]))
        rows = []
    return tables


def _row_text(row: tuple[str, ...]) -> str:
    return "| " + " | ".join(row) + " |"


def table_findings(run: Run) -> list[dict]:
    """A critical `format` finding for each table in a spec file whose header
    row is a table's header row in `ticket.md` and whose cells differ from
    every ticket table with that header, whitespace aside: a table copied from
    the ticket must be copied exactly (G14). Found in code, with no model call."""
    change_dir = run.config.change_dir
    ticket = change_dir / TICKET_FILE
    if not ticket.is_file():
        return []
    copied: dict[tuple[str, ...], list[tuple[int, list]]] = {}
    for n, rows in markdown_tables(ticket.read_text()):
        copied.setdefault(rows[0], []).append((n, rows))
    found = []
    for unit in layout.units(change_dir):
        for n, rows in markdown_tables((change_dir / unit).read_text()):
            if rows[0] not in copied or any(rows == t for _, t in copied[rows[0]]):
                continue
            tn, expected = copied[rows[0]][0]
            found.append(
                {
                    "category": "format",
                    "severity": "critical",
                    "description": f"{unit}:{n} copies the table at {TICKET_FILE}:{tn} "
                    "(the same header row) but its cells differ. The ticket's table "
                    "is:\n" + "\n".join(_row_text(r) for r in expected)[:4000],
                    "suggestion": f"copy the table from {TICKET_FILE} cell for cell",
                    "places": [
                        {"file": unit, "where": f"the table at line {n}"},
                        {"file": TICKET_FILE, "where": f"the table at line {tn}"},
                    ],
                }
            )
    return found


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


def _not_component(listed: list[dict]) -> list[dict]:
    """The listed findings the consistency agent gives a status: all but the
    component check's, which their component's check settles."""
    return [f for f in listed if not f.get("component")]


async def _component_checks(run: Run, listed: list[dict]):
    """The component check over every component (`component_check`)."""
    from . import component_check as CK

    return await CK.run_component_checks(run, listed=listed)


def _new_component_findings(run: Run, checked, listed: list[dict]) -> list[dict]:
    """The component check's findings that are not a listed finding (the same
    category and text: a reused result raises it again) and that no person
    accepted."""
    keys = {(f.get("category", ""), F.finding_text(f)) for f in listed}
    return _not_accepted(
        run,
        [
            f
            for f in checked.findings
            if (f.get("category", ""), F.finding_text(f)) not in keys
        ],
    )


async def check(
    run: Run,
    prior: list[dict] | None = None,
    *,
    edited: bool = False,
    reading: Reading | None = None,
) -> ConsistencyAnswer:
    """One call of the read-only consistency agent, reading what *reading*
    (default `what_to_read`) says. The saved digests are cleared before the
    call, and the digests of what it read are put in the state after it, for
    the caller's next save with the check's outcome (row S40)."""
    reading = reading or what_to_read(run, prior)
    if run.state.checked_digests is not None:
        run.state.checked_digests = None
        run.save()
    answer = await sdk.agent_structured(
        check_prompt(run, prior, edited=edited, reading=reading),
        ConsistencyAnswer,
        config=run.config,
        step="consistency",
        label="consistency",
        cwd=str(run.config.project_dir),
    )
    run.state.checked_digests = reading.digests
    return answer


async def recheck(run: Run, name: str, reason: str) -> str:
    """The recheck after a published rewrite: listed findings must be `resolved`,
    a new finding blocks only when critical. The component check runs first, on
    each component the rewrite was given to rewrite and each one that depends
    on one of them (row S10: a stop part way re-runs only those with no saved
    result); a new critical or major finding from it blocks, as the rewrite
    caused it. Listed component-check findings get their status from it, the
    others from the consistency agent. Passes, or blocks with exit 4.
    Returns the revision it passed at: the files' revision before the agent
    read them, so an edit made while it reads is never counted as checked."""
    from . import component_check as CK

    step = run.state.step(name)
    listed = step.repair_findings
    revision = layout.revision(run.config.change_dir)
    touched = CK.with_dependents(
        run,
        CK.of_units(run, target_units(run.config.change_dir, listed))
        | {f["component"] for f in listed if f.get("component")},
    )
    checked = await CK.run_component_checks(run, touched, listed)
    answer = await check(run, prior=_not_component(listed))
    assessments, citations = settle_citations(
        listed,
        answer.prior_assessments,
        run.state.accepted_findings,
        citation_findings(run),
    )
    raised = _new_component_findings(run, checked, listed)
    caused = [f for f in raised if f.get("severity") in F.ACTIONABLE]
    blocking, advisory = F.recheck_verdict(
        listed,
        [*assessments, *checked.assessments],
        as_dicts(answer) + [f for f in raised if f not in caused],
        deterministic=format_findings(run)
        + table_findings(run)
        + registry_findings(run)
        + citations
        + caused,
    )
    step.repair_findings = F.saved_findings(F.assess_listed(listed, assessments))
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
        and not F.dependencies_changed(run, step)
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


def _not_accepted(run: Run, found: list[dict]) -> list[dict]:
    """*found* without each finding a person accepted: the same category and
    the same text, as `--accept` saved it."""
    accepted = {
        (f.get("category", ""), F.finding_text(f)) for f in run.state.accepted_findings
    }
    return [
        f for f in found if (f.get("category", ""), F.finding_text(f)) not in accepted
    ]


async def check_reviewed(run: Run, revision: str) -> None:
    """The check after the specs review: reports, rewrites nothing, and blocks
    (exit 4) on a critical or major finding (rows S13, S17). The component
    check runs first, over every component, and its findings count as the
    agent's. After a block it rechecks the open blocking findings (a component
    check's by its component's check), and a new one blocks only when critical."""
    print("PHASE:CONSISTENCY", flush=True)
    step = run.state.step(NAME)
    later = step.outcome == "blocked" and bool(step.blocking_findings)
    listed = _open(step) if later else []
    in_code = _not_accepted(
        run, format_findings(run) + table_findings(run) + registry_findings(run)
    )
    earlier = _earlier(run, NAME)
    checked = await _component_checks(run, listed)
    reading = what_to_read(run, listed)
    answer = await check(
        run, prior=_not_component(listed), edited=True, reading=reading
    )
    assessments, citations = settle_citations(
        listed,
        answer.prior_assessments,
        run.state.accepted_findings,
        citation_findings(run),
    )
    deterministic = in_code + citations
    raised = _new_component_findings(run, checked, listed)
    if later:
        blocking, advisory = F.recheck_verdict(
            listed,
            [*assessments, *checked.assessments],
            as_dicts(answer) + raised,
            deterministic=deterministic,
        )
        blocking = F.number_new(
            NAME, blocking, [*step.blocking_findings, *run.state.accepted_findings]
        )
        reason = f"{REVIEWED_BLOCK_REASON} ({F.carried_and_new(blocking)})"
    else:
        kept, quiet = quiet_unchanged(run, as_dicts(answer), reading, earlier)
        actionable, advisory = F.first_check_verdict(deterministic + raised + kept)
        advisory += quiet
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
    run.progress(
        "SPEC_COUNTS", "Advisory: " + counts.counts_line(counts.count_specs(change_dir))
    )
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
    in_code = _not_accepted(
        run, format_findings(run) + table_findings(run) + registry_findings(run)
    )
    earlier = _earlier(run, NAME)
    checked = await _component_checks(run, [])
    reading = what_to_read(run)
    answer = await check(run, reading=reading)
    kept, quiet = quiet_unchanged(run, as_dicts(answer), reading, earlier)
    actionable, advisory = F.first_check_verdict(
        in_code
        + citation_findings(run)
        + _new_component_findings(run, checked, [])
        + kept
    )
    advisory += quiet
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
    units = target_units(change_dir, numbered)
    with F.released_on_spend_stop(run, NAME):
        await rewrite_and_publish(
            run, NAME, run.state.step(NAME).repair_findings, units, BLOCK_REASON
        )
    await recheck(run, NAME, BLOCK_REASON)
