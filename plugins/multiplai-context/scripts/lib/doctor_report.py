"""The memory doctor's report — three passes, one markdown file, zero writes.

Composes :mod:`lib.doctor_duplication`, :mod:`lib.doctor_contradiction` and
:mod:`lib.doctor_deadweight` into ``.multiplai/dreams/doctor-YYYY-MM-DD.md``.

## What this file is, and what it deliberately is not

It is a set of **suggestions with evidence**. Every claim cites ``file:line``,
every dead-weight number is labelled an estimate with its estimator and its
sample size, and every proposed merge is text a human retypes if they agree.

It is **not** an instruction, and no existing tooling may machine-apply it. Two
things enforce that rather than asking for it:

* it lives under ``dreams/`` as ``doctor-*.md``, and the maintainer's dream and
  triage passes both key off ``processed-learnings-*.md``, so nothing looks for
  it;
* it must **never** contain a ``## Routing Warnings`` heading. ``dream --triage``
  refuses outright — with exit 1 and no writes — any proposal lacking that
  section, so a doctor report pointed at the triage path bounces off the first
  guard instead of being partially interpreted. :func:`assert_not_appliable` and
  a test both assert this, because the property is one careless heading away
  from being lost.

Contract C5 is the reason all of this exists. P4 writes *additions* that a
receipt records and ``git revert`` undoes. The doctor would be proposing
*deletions and merges*, where a wrong call destroys something no receipt can
reconstruct — so it proposes, and a human decides. There is no flag, mode or
config key that changes that, and adding one is not a feature request this
phase can accept.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Mapping, Optional

from lib import doctor_contradiction, doctor_deadweight, doctor_duplication

__all__ = [
    "FORBIDDEN_HEADINGS",
    "report_name",
    "render_report",
    "assert_not_appliable",
]

#: Headings that would make an existing applier treat this file as actionable.
#: ``dream --triage`` requires ``## Routing Warnings`` before it will apply
#: anything, so its absence is what makes the refusal deterministic.
FORBIDDEN_HEADINGS: tuple[str, ...] = ("## Routing Warnings", "## Updates for")


def report_name(day: Optional[date] = None) -> str:
    return f"doctor-{(day or date.today()).isoformat()}.md"


HEADER_NOTE = """\
This is a **report, not a proposal**. Nothing here has been applied, and no
tooling in this repository can apply it: the doctor's three passes only ever
write this file. Every finding is a suggestion with its evidence attached —
verify it, then edit memory yourself if you agree.

Why the asymmetry with `/dream-remember`: that path writes *additions*, which a
receipt records and `git revert` undoes cleanly. These findings are *deletions
and merges*, where a wrong call destroys something no receipt can reconstruct.
"""


def render_report(
    duplication: Mapping,
    contradiction: Mapping,
    deadweight: Mapping,
    *,
    day: Optional[date] = None,
    memory_dir: Optional[Path] = None,
) -> str:
    """The whole report. Pure — takes three pass results, returns markdown."""
    day = day or date.today()
    out: list[str] = [
        f"# Memory doctor — {day.isoformat()}",
        "",
        HEADER_NOTE,
        "",
        "## Scope and limits",
        "",
    ]
    if memory_dir is not None:
        out.append(f"- **Corpus:** `{memory_dir}`")
    floor = deadweight.get("min_observations", 5)
    out.append(
        f"- **Sample-size floor (dead weight): {floor} estimator observations.** "
        f"Rows below it are not evidence and are never reported."
    )
    out.append(
        "- **Cross-file contradiction was NOT run.** Only within-file conflicts "
        "were looked for, so the absence of cross-file findings is not evidence "
        "that there are none."
    )
    basis = [name for name in ("self_report", "judge")
             if (deadweight.get("estimator_notes") or {}).get(name)]
    out.append(
        f"- **Dead weight is backed by these utilisation estimators:** "
        f"{', '.join(f'`{b}`' for b in basis) or '_none available_'}. Both are "
        f"*estimates*, never measurements, and they are reported side by side "
        f"rather than averaged."
    )
    out.append(
        f"- **Duplication similarity threshold:** ≥"
        f"{duplication.get('threshold', doctor_duplication.DEFAULT_RATIO)} "
        f"(`difflib`, stdlib), with every shortlisted pair confirmed by a model "
        f"before it is reported."
    )
    out.append("")
    out.append("---")
    out.append("")
    out.append(doctor_duplication.render_section(duplication))
    out.append("")
    out.append("---")
    out.append("")
    out.append(doctor_contradiction.render_section(contradiction))
    out.append("")
    out.append("---")
    out.append("")
    out.append(doctor_deadweight.render_section(deadweight))
    out.append("")
    return "\n".join(out)


def assert_not_appliable(report: str) -> None:
    """Raise if *report* carries a heading an existing applier would act on.

    Called on the way to disk. A doctor report that grew a ``## Routing
    Warnings`` heading — by a copy-paste, or by someone quoting a proposal in a
    finding — would clear ``dream --triage``'s first guard, and this is the last
    place to notice before it is a file on disk.
    """
    for line in report.splitlines():
        for heading in FORBIDDEN_HEADINGS:
            if line.startswith(heading):
                raise ValueError(
                    f"doctor report contains {heading!r}, which an existing "
                    f"applier keys off. The doctor writes suggestions only."
                )
