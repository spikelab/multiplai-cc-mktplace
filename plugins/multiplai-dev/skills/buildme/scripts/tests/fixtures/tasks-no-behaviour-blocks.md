## 1. Core `summarize` with delegated word count and always-plural format

Create `src/textkit/summary.py` with a module docstring and a fully type-annotated `summarize(text: str) -> str` that returns `f"{count_words(text)} words, {len(text.splitlines())} lines"`, importing the word count via `from textkit.words import count_words` (no reimplementation of splitting). The output always uses the plural nouns "words" and "lines" regardless of count. Exercise end-to-end with `uv run pytest -q tests/test_summary.py`, asserting exact strings: `summarize("hello") == "1 words, 1 lines"`, `summarize("hello world\nfoo bar baz") == "5 words, 2 lines"`, `summarize("word") == "1 words, 1 lines"`, and `summarize("  the quick   brown fox  ")` yields `n == 4` (whitespace collapsed via `str.split`). Guard the test import with a `try/except ImportError` sentinel so collection succeeds and tests fail on behavior.

Interfaces:
- Produces: `textkit.summary.summarize(text: str) -> str`
- Consumes: `textkit.words.count_words(text: str) -> int`

Satisfies: text-summary — Summary Output Format (all scenarios), Word Count Delegation (all scenarios)

## 2. Line-count and empty-input edge cases

Confirm the `len(text.splitlines())` expression handles every line-count and empty-string scenario with no special-casing of blank or whitespace-only lines. Add tests to `tests/test_summary.py` asserting exact strings: `summarize("  \n  ") == "0 words, 2 lines"`, `summarize("one\n") == "1 words, 1 lines"`, `summarize("\n\n\n") == "0 words, 3 lines"`, and `summarize("") == "0 words, 0 lines"`. These are satisfied by the implementation from Block 1; this slice pins the edge behavior so any future drift (e.g. a trailing newline adding a line) surfaces as a test failure. Run via `uv run pytest -q tests/test_summary.py`.

Interfaces:
- Produces: (none)
- Consumes: Block 1: `textkit.summary.summarize(text: str) -> str`

Satisfies: text-summary — Line Count Computation (all scenarios), Empty Input Handling

## 3. Module conventions and no package-level re-export

Verify `summarize` is reachable only as `textkit.summary.summarize` (standard-library-only, docstrings and type hints present) and is NOT re-exported from the package root. Leave `src/textkit/__init__.py` unchanged — no new import, no `__all__`, no version bump — and make no change to `pyproject.toml`. Add tests asserting `textkit.summary.summarize` is callable and returns a formatted summary, plus `not hasattr(textkit, "summarize")` to prove there is no top-level attribute. Also document/verify that non-string input (e.g. `None`) raises `AttributeError` from `.splitlines()`/`.split()` rather than a friendly error, matching `count_words`. Run via `uv run pytest -q`.

Interfaces:
- Produces: (none)
- Consumes: Block 1: `textkit.summary.summarize(text: str) -> str`

Satisfies: text-summary — Module Conventions (both scenarios)