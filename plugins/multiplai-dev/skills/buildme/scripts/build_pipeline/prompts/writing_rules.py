"""Writing rules for the agents that write tests and code.

`coding-standards.md` stays out of these agents' context on purpose, so the
rules go into their prompts directly. Each rule is concatenated into a
`.format()` template, so it must hold no braces.
"""

TEST_VALUE_RULE = (
    "**Fewer, more valuable tests.** Each WHEN/THEN scenario still gets a test. "
    "Beyond that, add a test only when removing it would let a real defect through "
    "on a path that matters. Do not add a test another test already covers, and do "
    "not test what the language or a library already guarantees. Prefer one test "
    "that checks a behaviour fully over several that each check a piece of it."
)

COMMENT_RULE = (
    "**Short comments.** Comment only why the code does something a reader would "
    "not expect. Never narrate what the next line does, never describe history "
    '("previously…", "we used to…"), and keep a comment shorter than the code it '
    "explains."
)
