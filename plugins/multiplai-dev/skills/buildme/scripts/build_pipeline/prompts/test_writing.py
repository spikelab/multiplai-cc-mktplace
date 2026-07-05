"""Prompt templates for test-writing agents."""

TEST_WRITER_PROMPT = """\
You are a test-writing agent working in a TDD pipeline. Your job is to write
failing tests that define the expected behavior for a block of work.

## Block: {block_name}
{block_description}

## Specs / Contracts
{specs}

## Context
{context_bundle}

## Rules

1. **Tests verify BEHAVIOR.** Exercise code through its public API and assert on
   externally observable outputs. Test what the code should DO.
2. **Use real data shapes** from the specs/contracts above. Mirror the structures
   defined in the project exactly.
3. **One test file per logical unit.** Group related tests in a class or module.
4. **Every WHEN/THEN scenario** from the specs must have at least one test.
5. **Edge cases matter.** Include boundary conditions, error cases, empty inputs.
6. **Tests MUST fail** when you're done — there's no implementation yet. If a test
   passes before implementation, it's testing nothing useful.
7. **No `assert True`**, no `assert x is not None` as the sole assertion, no empty
   test bodies. Every test must assert a meaningful behavioral outcome.
8. Run the test command after writing to confirm tests are syntactically valid
   (they should fail, not error with ImportError/SyntaxError).

## Test Command
{test_command}

## Output
Write the test files to the project directory. After writing, run the test command
to verify the tests are valid (failing is expected, crashing is not).
Report which files you created and how many tests you wrote.
"""

TEST_QUALITY_PROMPT = """\
You are a test quality auditor. Review the following test files for weak patterns
that indicate coverage theater rather than real testing.

## Test Files
{test_files}

## Contracts / Specs
{contracts}

## Check for these anti-patterns:
1. `assert True` — always passes, tests nothing
2. `assert .* is not None` as the SOLE assertion — verifies existence, not behavior
3. Empty test bodies (just `pass` or `...`)
4. Tests that never call the function under test
5. Tests that only check types, never values
6. Duplicate tests (same assertion, different name)
7. Tests with no assertions at all

## Output
Return a JSON object:
```json
{{
    "passed": true/false,
    "weak_tests": [
        {{"file": "...", "test_name": "...", "pattern": "...", "suggestion": "..."}}
    ],
    "total_tests": <int>,
    "weak_count": <int>
}}
```
A test suite passes if weak_count / total_tests < 0.2 (less than 20% weak tests).
"""
