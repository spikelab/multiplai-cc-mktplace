"""Scoring for the replay eval (`test_replay.py`), with no model calls, so the
counts are tested in the ordinary suite (`test_eval_score.py`).

A run maps each finding of a component check to one labelled finding of the
same commit and component (`labels.yaml`), or to none, by one judge call per
check. `assign` turns that answer into a label id per finding; `summarize`
counts P1-P10 caught, labelled-real findings kept, labelled-false findings
raised and unlabelled findings.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

TRIAGE = "triage-2026-11-02"


class LabelMatch(BaseModel):
    finding: int
    label: str | None = None
    reason: str = ""


class LabelMap(BaseModel):
    matches: list[LabelMatch] = []


LABEL_PROMPT = """\
A requirements check reported the new findings below on one component's
specs. Earlier checks of the same specs raised the labelled findings below.
For each new finding, name the labelled finding that describes the same
problem: the same requirement, and the same conflict, gap or impossibility (it
may word it differently, cite other lines or propose another fix). A finding
about the same requirement but a different conflict is not the same problem.
Name none when no labelled finding is the same problem. Two new findings may
name the same labelled one.

## Labelled findings
{labels}

## New findings
{findings}

Return ONLY JSON with one entry per new finding:
{{"matches": [{{"finding": <the new finding's number>, "label": "<L-number, or null>",
  "reason": "<one sentence>"}}]}}
"""


def load_labels(path: Path) -> list[dict]:
    """The labels in *path*; a bare `verdict: false` (a YAML boolean) is the
    verdict "false"."""
    out = []
    for lab in yaml.safe_load(path.read_text())["labels"]:
        v = lab["verdict"]
        lab["verdict"] = "false" if v is False else str(v)
        if lab["verdict"] not in ("real", "false"):
            raise ValueError(f"{lab['id']}: verdict {v!r} is not real or false")
        out.append(lab)
    return out


def of_check(labels: list[dict], commit: str, component: str) -> list[dict]:
    return [
        lab
        for lab in labels
        if lab["commit"] == commit and lab["component"] == component
    ]


def label_prompt(labels: list[dict], findings_text: str) -> str:
    lines = [
        f"L{i}. [{lab.get('severity', '')}] {lab['description']}"
        for i, lab in enumerate(labels, 1)
    ]
    return LABEL_PROMPT.format(
        labels="\n".join(lines) or "(none)", findings=findings_text
    )


def assign(
    n_findings: int,
    answer: LabelMap | None,
    labels: list[dict],
    run: str,
    problems: dict[int, str] | None = None,
) -> list[str | None]:
    """A label id (or None) for each of a check's *n_findings* findings.

    A label adjudicated from this *run* (`run` and `n` set) names its finding
    directly. Otherwise the judge's L-number decides; a finding a P judge
    matched (*problems*: finding number -> P id) takes that problem's label
    when the judge named none."""
    by_n = {lab["n"]: lab["id"] for lab in labels if lab.get("run") == run}
    judged = {}
    for m in answer.matches if answer else []:
        if m.label and m.label.upper().startswith("L") and m.label[1:].isdigit():
            i = int(m.label[1:])
            if 0 < i <= len(labels):
                judged[m.finding] = labels[i - 1]["id"]
    by_problem = {lab.get("problem"): lab["id"] for lab in labels if lab.get("problem")}
    out = []
    for n in range(1, n_findings + 1):
        if n in by_n:
            out.append(by_n[n])
        elif n in judged:
            out.append(judged[n])
        elif problems and n in problems:
            out.append(by_problem.get(problems[n], f"problem:{problems[n]}"))
        else:
            out.append(None)
    return out


def summarize(labels: list[dict], assigned: dict[tuple[str, str], list]) -> dict:
    """The run's counts. *assigned*: (commit, component) -> label id or None
    per finding, for every check that ran."""
    by_id = {lab["id"]: lab for lab in labels}
    ran = set(assigned)
    matched = {i for ids in assigned.values() for i in ids if i}
    in_scope = [lab for lab in labels if (lab["commit"], lab["component"]) in ran]

    def count(pred):
        pool = [lab for lab in in_scope if pred(lab)]
        return sum(lab["id"] in matched for lab in pool), len(pool)

    real = count(lambda lab: lab["verdict"] == "real")
    triage_real = count(
        lambda lab: lab["verdict"] == "real" and lab.get("source") == TRIAGE
    )
    false = count(lambda lab: lab["verdict"] == "false")
    triage_false = count(
        lambda lab: lab["verdict"] == "false" and lab.get("source") == TRIAGE
    )
    unlabelled = sum(i is None for ids in assigned.values() for i in ids)
    findings = sum(len(ids) for ids in assigned.values())
    unknown = sorted(
        i for i in matched if i not in by_id and not i.startswith("problem:")
    )
    return {
        "findings": findings,
        "real_kept": real[0],
        "real_labelled": real[1],
        "triage_real_kept": triage_real[0],
        "triage_real": triage_real[1],
        "false_raised": false[0],
        "false_labelled": false[1],
        "triage_false_raised": triage_false[0],
        "unlabelled": unlabelled,
        "unknown_labels": unknown,
    }
