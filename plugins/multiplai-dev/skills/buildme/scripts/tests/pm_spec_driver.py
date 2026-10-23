"""pm-spec's real command line with its model replaced, for buildme's
end-to-end test (`test_pm_spec_handoff.py`).

Run in pm-spec's own environment, never imported by buildme:

    uv run --project <repo root> --package pm-spec-pipeline \
        python tests/pm_spec_driver.py <pm-spec argv...>

`pm_spec_pipeline.sdk.run_agent` answers each call by its label with fixed
text, as pm-spec's own tests do (`pm-spec/scripts/tests/conftest.py`), so no
model is called. A label with no answer fails the run.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from pm_spec_pipeline import __main__ as cli
from pm_spec_pipeline import sdk
from pm_spec_pipeline.rewrite import BEGIN, END

CAPABILITY = "fixture"

PROPOSAL = (
    "# Return a value\n\n## Why\n\nCallers need a value.\n\n## What Changes\n\nReturn one.\n\n"
    "## Impact\n\nThe `app` module.\n\n## Sources\n\n"
    '- PRD "Return a value"\n'
)
USE_CASES = (
    "# Use cases\n\n## Personas\n\n### caller\n- **Who they are:** calls the app\n\n"
    "## Use cases\n\n- caller asks for a value so that they can use it\n"
)
PLAN = (
    "# Plan: Return a value\n\n> File: feat.md · Date: 2026-10-23\n\n"
    "**Objective:** Callers get a value.\n\n## Context\nNone is returned.\n\n"
    "## Outcome\nOne is.\n\n## Constraints / out of scope\n- None.\n\n"
    "## Done means\n1. The app returns a value.\n"
)
SPECS = (
    f"# Capability: {CAPABILITY}\n\n## ADDED Requirements\n\n### Requirement: Behavior\nThe system SHALL return a value.\n\n"
    "#### Scenario: Works\n- **WHEN** invoked\n- **THEN** returns a value\n"
)

ANSWERS = {
    "interview:questions": {"questions": ["Which value?"]},
    "interview:answers": {
        "answers": [
            {
                "question": "Which value?",
                "answer": "1.",
                "grounded_in": "PRD: Return a value",
            }
        ]
    },
    "spec:proposal": PROPOSAL,
    "spec:use-cases": USE_CASES,
    "components": {
        "reason": "One pull request carries it.",
        "components": [
            {
                "id": "feat",
                "title": "Return a value",
                "depends_on": [],
                "plan": PLAN,
                "ticket_ref": "",
            }
        ],
    },
    "spec:specs:feat": f"{BEGIN}\n{SPECS}{END}\n",
    "consistency": {"findings": [], "prior_assessments": []},
}


async def fake_run_agent(prompt, *, label="", **kwargs):
    if label not in ANSWERS:
        raise AssertionError(f"pm_spec_driver: no fake answer for the call {label!r}")
    value = ANSWERS[label]
    usage = SimpleNamespace(
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    return SimpleNamespace(
        text=json.dumps(value) if isinstance(value, dict) else value,
        turns=1,
        usage=usage,
        files_changed=[],
        stderr_tail="",
    )


def main() -> int:
    sdk.run_agent = fake_run_agent
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
