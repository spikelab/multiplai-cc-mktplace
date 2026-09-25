"""Prescriber prompt: one agent per confirmed finding."""

from __future__ import annotations

import json

from ..models import Finding, TargetInfo, Verdict
from . import CITATION_RULES, JSON_ONLY, workspace_block

# Stated to the model verbatim; the gates enforce it.
PREMISE_CONTRACT = (
    "Every fact your fix depends on is a premise. A premise about this repo cites the lines that "
    "establish it, and if it is about what a setting, constant or environment variable means, it "
    "cites a line that *uses* it. A premise about anything outside this repo (a vendor's "
    "configuration, a production value, what a channel is called) is `external` and carries no "
    "citation."
)

SCHEMA = """\
{"description": "the fix, in two or three sentences",
 "patch_sketch": "a short sketch of the change, or null",
 "premises": [{"statement": "one fact the fix depends on",
               "kind": "in_repo" | "external",
               "citation": {"path": "...", "line_start": 1, "line_end": 1, "quote": "exact text"} | null,
               "symbol": "SETTING_NAME the premise is about, or null",
               "question": "for an external premise: what to ask the developer, else null"}],
 "open_questions": ["anything the developer must answer before applying the fix"]}"""


def build(target: TargetInfo, finding: Finding, verdict: Verdict | None, gate_reason: str = "") -> str:
    cited = json.dumps([c.model_dump() for c in finding.citations], indent=1)
    parts = [
        "You are proposing a fix for one confirmed code-review finding.",
        workspace_block(target),
        f"Finding ({finding.severity}) in `{finding.file}` lines {finding.line_start}-{finding.line_end}:\n"
        f"{finding.claim}\n\nFailure scenario: {finding.failure_scenario}\n\nCited lines:\n{cited}",
    ]
    if verdict is not None:
        parts.append(f"The verifier's reason for confirming it: {verdict.reason}")
    parts += [
        PREMISE_CONTRACT,
        "Before you rely on what a setting, constant or environment variable means, Grep for it and "
        "read the lines that use it, not just the line that defines it. Its name is not evidence of "
        "its meaning. Set `symbol` on every premise that names one (an UPPER_CASE name such as a "
        "Django setting, a module constant or an environment variable); leave it null for functions, "
        "variables and fields. When the right value lives "
        "outside this repository, say so with an external premise and a `question`, and do not "
        "pick a value from inside the repo that merely looks related.",
        CITATION_RULES,
    ]
    if gate_reason:
        parts.append(
            "Your previous answer was rejected by a program for this reason:\n"
            f"{gate_reason}\nFix that premise: cite a line that uses the symbol, or mark it external "
            "if what it depends on is outside this repository."
        )
    parts += [f"Schema:\n{SCHEMA}", JSON_ONLY]
    return "\n\n".join(parts)
