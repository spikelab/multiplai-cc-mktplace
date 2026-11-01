"""The component check: one read-only agent per component, in parallel, asks
whether each of its requirements can hold.

Each agent reads its component's `plan.md` and `specs.md` in full, the
`specs.md` of every component it depends on (directly or through others,
`consistency._closure`) in full, `proposal.md`, the capability names and
requirement titles of every other component, and the repository. It reports
each requirement that contradicts another requirement of the same spec, a
dependency's spec or the code as it is, and each one that cannot be met as
written (`contradiction`, `gap`, `infeasible`), worded as a change to the
requirement for a PM to decide. It reports; it never rewrites a file.

At most `component_check: {max_parallel: N}` agents run at once (default 4),
as step `component_check`, so `--max-usd` and the model settings apply. A
change with no components is one check, over the change dir's specs.

Each result is saved in `RunState.component_checks` as it arrives, by
component id, with the sha256 over the files it read (`digest`: its plan, its
specs, its dependencies' specs, `proposal.md`), and one line
`COMPONENT_CHECK <id> findings=<n> cost=<usd>` goes to the progress file. A
saved result whose digest still matches is reused with no call and no line,
so a stopped check resumes with the components that have no result or a
stale one (rows S1-S3, S5, S7), and an edit re-runs that component and every
component that depends on it.

The findings go to the consistency step, which treats them as its own
(`consistency.run_consistency`, `consistency.check_reviewed`,
`consistency.recheck`): each is tagged with its component (`component`) and
numbered within it. A listed finding (one a block saved) is shown again to
its component's next check, which gives it a status; a component whose result
is reused has the same files, so a listed finding it raised is still present.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from multiplai_core.specs import component_dirs, read_change_meta

from . import artifacts, layout, sdk
from . import findings as F
from .consistency import ConsistencyAnswer, _closure, accepted_section, as_dicts
from .prompts import UNTRUSTED_NOTICE
from .prompts.component_check import COMPONENT_CHECK_PROMPT, PRIOR_SECTION
from .prompts.consistency import _lines
from .rewrite import cited_units
from .run import Run
from .state import ComponentCheck

log = logging.getLogger(__name__)

NAME = "component_check"


class Target(NamedTuple):
    """One component to check: its id, its dir and its spec files (relative
    to the change dir)."""

    id: str
    dir: Path
    units: list[str]


class Checked(NamedTuple):
    """What `run_component_checks` returns. *findings*: every finding of the
    components checked, each tagged with `component`. *assessments*: a status
    for each listed finding of those components."""

    findings: list[dict]
    assessments: list[F.PriorAssessment]


def targets(run: Run) -> list[Target]:
    """Each component of the change, or the change itself when it has none."""
    change_dir = run.config.change_dir
    comps = component_dirs(change_dir)
    if not comps:
        return [Target(run.config.change_name, change_dir, layout.units(change_dir))]
    out = []
    for d in comps:
        prefix = layout.rel(change_dir, d) + "/"
        out.append(
            Target(
                d.name, d, [u for u in layout.units(change_dir) if u.startswith(prefix)]
            )
        )
    return out


def dependencies(run: Run) -> dict[str, list[str]]:
    return {
        d.name: [str(x) for x in read_change_meta(d).get("depends_on") or []]
        for d in component_dirs(run.config.change_dir)
    }


def with_dependents(run: Run, ids: Iterable[str]) -> set[str]:
    """*ids*, and every component whose dependency closure holds one of them."""
    ids = set(ids)
    deps = dependencies(run)
    return ids | {cid for cid in deps if _closure(deps, cid) & ids}


def of_units(run: Run, units: Iterable[str]) -> set[str]:
    """The ids of the components that hold *units*."""
    units = set(units)
    return {t.id for t in targets(run) if units & set(t.units)}


def _read(change_dir: Path, rel: str) -> str:
    path = change_dir / rel
    return path.read_text() if path.is_file() else ""


def read_list(run: Run, target: Target) -> list[str]:
    """The files one check reads, relative to the change dir: its plan, its
    spec files, each dependency's spec files, then `proposal.md`."""
    change_dir = run.config.change_dir
    by_id = {t.id: t for t in targets(run)}
    closure = sorted(_closure(dependencies(run), target.id) & by_id.keys())
    out = [layout.rel(change_dir, target.dir / layout.PLAN_FILE), *target.units]
    for dep in closure:
        out += by_id[dep].units
    return [*out, "proposal.md"]


def digest(run: Run, target: Target) -> str:
    """The sha256 over the files the check of *target* reads, each by its path."""
    change_dir = run.config.change_dir
    h = hashlib.sha256()
    for rel in read_list(run, target):
        text = (
            artifacts.proposal_text(run)
            if rel == "proposal.md"
            else _read(change_dir, rel)
        )
        h.update(f"{rel}\0{text}\0".encode())
    return h.hexdigest()


def check_prompt(run: Run, target: Target, listed: list[dict]) -> str:
    change_dir = run.config.change_dir
    by_id = {t.id: t for t in targets(run)}
    closure = sorted(_closure(dependencies(run), target.id) & by_id.keys())
    dep_specs = "\n\n".join(
        f"### {u}\n{_read(change_dir, u)}" for dep in closure for u in by_id[dep].units
    )
    others = "\n\n".join(
        f"### {u}\n{layout.outline(_read(change_dir, u))}"
        for t in by_id.values()
        if t.id != target.id and t.id not in closure
        for u in t.units
    )
    plan_rel = layout.rel(change_dir, target.dir / layout.PLAN_FILE)
    return COMPONENT_CHECK_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        proposal_content=artifacts.proposal_text(run) or "(missing)",
        component=target.id,
        plan_path=plan_rel,
        plan_content=_read(change_dir, plan_rel) or "(missing)",
        specs_path=", ".join(target.units) or "(no specs file)",
        specs_content="\n\n".join(
            f"#### {u}\n{_read(change_dir, u)}" for u in target.units
        )
        or "(missing)",
        dependency_specs=dep_specs or "(it depends on no other component)",
        other_outlines=others or "(none)",
        prior_section=PRIOR_SECTION.format(prior_findings=_lines(listed))
        if listed
        else "",
        accepted_section=accepted_section(run.state.accepted_findings),
    )


def tagged(run: Run, target: Target, result: ComponentCheck) -> list[dict]:
    """The findings of *result*, each tagged with its component and numbered
    within it, with the component's spec file as a place when none is."""
    change_dir = run.config.change_dir
    out = []
    for n, f in enumerate(result.findings, 1):
        places = list(f.get("places") or [])
        if target.units and not set(cited_units(change_dir, {"places": places})) & set(
            target.units
        ):
            places.insert(0, {"file": target.units[0], "where": ""})
        out.append(
            {
                **f,
                "description": f"[component `{target.id}`, finding {n}] "
                + str(f.get("description", "")),
                "places": places,
                "component": target.id,
            }
        )
    return out


def settle(
    result: ComponentCheck, mine: list[dict], current: list[dict]
) -> list[F.PriorAssessment]:
    """A status for each listed finding of one component: the agent's, when it gave
    one; else `still_present` when the result raised it (same category and
    text), else `cannot_verify`."""
    given = {a.get("finding_id"): a for a in result.prior_assessments}
    raised = {(f.get("category", ""), F.finding_text(f)) for f in current}
    out = []
    for f in mine:
        fid = f.get("finding_id", "")
        if fid in given:
            out.append(F.PriorAssessment(**{**given[fid], "finding_id": fid}))
        elif (f.get("category", ""), F.finding_text(f)) in raised:
            out.append(F.PriorAssessment(finding_id=fid, status="still_present"))
        else:
            out.append(F.PriorAssessment(finding_id=fid, status="cannot_verify"))
    return out


async def _check_one(
    run: Run, target: Target, listed: list[dict], gate: asyncio.Semaphore
) -> None:
    async with gate:
        spend = sdk.track_spend()
        read = digest(run, target)
        answer = await sdk.agent_structured(
            check_prompt(run, target, listed),
            ConsistencyAnswer,
            config=run.config,
            step=NAME,
            label=f"{NAME}:{target.id}",
            cwd=str(run.config.project_dir),
        )
    result = ComponentCheck(
        digest=read,
        findings=as_dicts(answer),
        prior_assessments=[a.model_dump() for a in answer.prior_assessments],
        cost_usd=round(spend.cost_usd, 6),
    )
    run.state.component_checks[target.id] = result
    run.save()
    run.progress(
        NAME.upper(),
        f"COMPONENT_CHECK {target.id} findings={len(result.findings)} "
        f"cost={result.cost_usd:.4f}",
    )
    log.info(
        "%s %s findings=%d cost=%.4f",
        NAME,
        target.id,
        len(result.findings),
        result.cost_usd,
    )


async def run_component_checks(
    run: Run, components: Iterable[str] | None = None, listed: Iterable[dict] = ()
) -> Checked:
    """Check each of *components* (default: every one), reusing each saved
    result whose digest still matches. *listed*: the findings a block saved;
    each one tagged with a component checked here is shown to its check again
    and given a status."""
    wanted = None if components is None else set(components)
    todo = [t for t in targets(run) if wanted is None or t.id in wanted]
    listed = [f for f in listed if f.get("component")]
    mine = {t.id: [f for f in listed if f["component"] == t.id] for t in todo}
    stale = [
        t
        for t in todo
        if (saved := run.state.component_checks.get(t.id)) is None
        or saved.digest != digest(run, t)
    ]
    if stale:
        print(f"PHASE:COMPONENT_CHECK ({len(stale)} of {len(todo)})", flush=True)
        gate = asyncio.Semaphore(run.config.component_check_max_parallel)
        tasks = [
            asyncio.create_task(_check_one(run, t, mine[t.id], gate)) for t in stale
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    found: list[dict] = []
    assessments: list[F.PriorAssessment] = []
    for t in todo:
        result = run.state.component_checks[t.id]
        current = tagged(run, t, result)
        found += current
        assessments += settle(result, mine[t.id], current)
    return Checked(found, assessments)
