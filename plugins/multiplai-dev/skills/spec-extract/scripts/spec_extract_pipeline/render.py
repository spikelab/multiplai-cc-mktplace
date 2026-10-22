"""The render step: registry files, `report.md` and `summary.json` (plan, item 17).

`<run>/registry/<capability>.md` is in buildme's format (Decided 13): a first
line naming the run and the commit, `## ADDED Requirements`, then
`### Requirement:` blocks with `#### Scenario:` and `- **WHEN**` /
`- **THEN**` lines, each scenario followed by its `- **CITES**` lines and one
`- **EVIDENCE**` line. buildme's readers parse only `### Requirement:` blocks,
so the extra lines are safe. Every `#` in model text is written `\\#`: the
text is on one line already (the gate), and this keeps it from starting a
heading or holding a `### Requirement:` that buildme's `ChangeManager` finds
anywhere in a line.

Every file is rewritten from state, so a resume before `render` is in
`steps_done` writes the same bytes (row R9). Then `render` is added, and
`snapshot/` is deleted, its marker first (row R10). No timestamp, no spend.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import report as report_mod
from . import state as state_mod
from .gate import Checked, Spec
from .group import Grouping, md, symbol_id
from .report import Section
from .state import State
from .target import SNAPSHOT_DIR, SNAPSHOT_MARKER

REGISTRY_DIR = "registry"
SUMMARY_FILE = "summary.json"


def capability_md(run_label: str, sha12: str, spec: Spec) -> str:
    out = [
        f"<!-- spec-extract {run_label}; cited lines are at commit {sha12} -->",
        "",
        "## ADDED Requirements",
    ]
    for r in spec.requirements:
        out += ["", f"### Requirement: {md(r.name)}"] + ([md(r.text)] if r.text else [])
        for sc in r.scenarios:
            out += [
                "",
                f"#### Scenario: {md(sc.name)}",
                f"- **WHEN** {md(sc.when)}",
                f"- **THEN** {md(sc.then)}",
            ]
            out += [
                f"- **CITES** `{c.path}:{c.line_start}-{c.line_end}`" for c in sc.cites
            ]
            out.append(
                f"- **EVIDENCE** {'pinned by test' if sc.pinned else 'read from code only'}"
            )
    return "\n".join(out) + "\n"


def _counts(spec: Spec) -> tuple[int, int, int]:
    scenarios = [sc for r in spec.requirements for sc in r.scenarios]
    return len(spec.requirements), len(scenarios), sum(sc.pinned for sc in scenarios)


def sections(grouping: Grouping, checked: Checked, symbols: int) -> list[Section]:
    """Render's sections of `report.md`, after the scope sections; every list in group order."""
    totals = [sum(x) for x in zip(*(_counts(s) for s in checked.specs))] or [0, 0, 0]
    counts = [
        f"- Capability files: {len(checked.specs)}",
        f"- Requirements: {totals[0]}",
        f"- Scenarios: {totals[1]} ({totals[2]} pinned by test)",
        f"- Public symbols specified: {symbols - len(checked.not_specified)} of {symbols}",
    ]
    caps = []
    for s in checked.specs:
        r, n, p = _counts(s)
        caps.append(
            f"- {s.name}: {r} requirement(s), {n} scenario(s), {p} pinned by test "
            f"(`{REGISTRY_DIR}/{s.name}.md`)"
        )
    # The gate may rename the bucket (`unassigned-2`) or split it by module.
    bucket = {sid: c.name for c in grouping.capabilities for sid in c.symbols}
    return [
        Section("Counts", counts),
        Section("Capabilities", caps),
        Section("Dropped", [f"- {d}" for d in grouping.dropped + checked.dropped]),
        Section(
            "Not specified", [f"- `{sid}`: {why}" for sid, why in checked.not_specified]
        ),
        Section(
            "Not grouped",
            [
                f"- `{sid}`: the group answer did not place it; it is in "
                f"`{REGISTRY_DIR}/{bucket[sid]}.md`"
                for sid in grouping.unassigned
            ],
        ),
        Section(
            "Not written",
            [
                f"- {c}: not written: write gave no answer in 2 attempts"
                for c in checked.not_written
            ],
        ),
    ]


def summary(state: State, grouping: Grouping, checked: Checked) -> dict:
    caps = []
    for s in checked.specs:
        r, n, p = _counts(s)
        caps.append(
            {
                "name": s.name,
                "summary": s.summary,
                "file": f"{REGISTRY_DIR}/{s.name}.md",
                "requirements": r,
                "scenarios": n,
                "pinned_by_test": p,
            }
        )
    return {
        "target": report_mod.describe(state.target),
        "commit": state.target.head_sha,
        "capabilities": caps,
        "dropped": grouping.dropped + checked.dropped,
        "not_specified": [
            {"symbol": sid, "reason": why} for sid, why in checked.not_specified
        ],
        "not_grouped": grouping.unassigned,
        "not_written": checked.not_written,
    }


def drop_snapshot(run_dir: Path) -> None:
    """Delete `snapshot/`, its marker first, so a stop part-way leaves no marker (row R10)."""
    snap = run_dir / SNAPSHOT_DIR
    (snap / SNAPSHOT_MARKER).unlink(missing_ok=True)
    shutil.rmtree(snap, ignore_errors=True)


def render(
    state: State,
    run_dir: Path,
    grouping: Grouping,
    checked: Checked,
    scope_sections: list[Section],
) -> int:
    """Write every output from state, add `render`, delete the snapshot. The number of files."""
    assert state.scope is not None
    run_label = f"{run_dir.parent.name}/{run_dir.name}"
    sha12 = str(state.target.head_sha)[:12]
    for spec in checked.specs:
        report_mod.write_output(
            run_dir,
            f"{REGISTRY_DIR}/{spec.name}.md",
            capability_md(run_label, sha12, spec),
        )
    report_mod.write_report(
        run_dir,
        state.target,
        scope_sections
        + sections(grouping, checked, len({symbol_id(s) for s in state.scope.symbols})),
    )
    report_mod.write_output(
        run_dir,
        SUMMARY_FILE,
        json.dumps(summary(state, grouping, checked), indent=2, ensure_ascii=False)
        + "\n",
    )
    state.steps_done.append("render")
    state_mod.save(state, run_dir)
    drop_snapshot(run_dir)
    return len(checked.specs)
