"""The test writer and the implementers carry buildme's writing rules.

`coding-standards.md` stays out of these agents' context on purpose, so the
rules live in the prompts. Each prompt is built from the constants in
`prompts/writing_rules.py`; rendering it with its real fields shows the rule
reached the agent as a numbered rule and the template still formats.
"""

from __future__ import annotations

import re

import pytest
from build_pipeline.prompts.implementation import (
    IMPLEMENTER_PROMPT_CLEAN,
    IMPLEMENTER_PROMPT_MINIMUM,
)
from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT
from build_pipeline.prompts.writing_rules import COMMENT_RULE, TEST_VALUE_RULE

_IMPLEMENTER_FIELDS = dict(
    block_name="Block", block_description="desc",
    failing_tests="FAILED test_a", context_bundle="ctx", test_command="pytest",
)

CASES = {
    "test writer": (
        TEST_WRITER_PROMPT,
        dict(block_name="Block", block_description="desc", specs="specs",
             context_bundle="ctx", test_command="pytest", max_turns=30, half=15),
        (TEST_VALUE_RULE, COMMENT_RULE),
    ),
    "implementer clean": (IMPLEMENTER_PROMPT_CLEAN, _IMPLEMENTER_FIELDS, (COMMENT_RULE,)),
    "implementer minimum": (IMPLEMENTER_PROMPT_MINIMUM, _IMPLEMENTER_FIELDS, (COMMENT_RULE,)),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_prompt_renders_with_its_writing_rules_as_numbered_rules(name):
    template, fields, rules = CASES[name]
    rendered = template.format(**fields)
    for rule in rules:
        assert re.search(rf"^\d+\. {re.escape(rule)}$", rendered, re.MULTILINE), rule
