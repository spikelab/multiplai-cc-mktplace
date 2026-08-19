"""Reviewer prose borrowed from Anthropic's `pr-review-toolkit` plugin.

Every constant below is adapted from a file in
`anthropics/claude-plugins-official`, Apache-2.0. Each carries its own
attribution header naming the source path, the source blob SHA, the `agents`
tree SHA it was taken at, the licence, and the fact that it was modified —
Apache-2.0 section 4 requires the modification notice, it is not politeness.
`SOURCES.json` beside this file is the machine-readable form of the same
record, and `LICENSE` is the upstream licence text unchanged.

Three adaptations were applied to every block:

1.  **The YAML frontmatter is dropped entirely.** It carried `name:`,
    `description:`, `color:` and `model:`. buildme resolves the model per step
    through `conf_model()`, so a vendored `model:` line would silently override
    it; and the only internal author name in any of these files lives inside
    the frontmatter `description:` examples, so dropping it removes that too.
2.  **Stack-specific house rules are cut, not translated.** The project's own
    `CLAUDE.md` chain and `reference/dev/` supply that layer at run time.
3.  **The upstream rating scales are normalised to one.** Three sources use
    three incompatible scales (0-100, 1-10 twice, named tiers). Only
    `code-reviewer`'s 0-100 comes with an explicit suppression rule, so that
    is the one kept; the 1-10 axes and the named tiers are dropped.

No brace characters may appear in these strings: `CODE_REVIEW_PROMPT` is
composed from them and then run through `str.format()`.
"""

# ---------------------------------------------------------------------------
# Vendored from: anthropics/claude-plugins-official
# Source path:   plugins/pr-review-toolkit/agents/code-reviewer.md
# Blob SHA:      834b70c21f1f1bd4d01b8025bc830bf00887f2e7
# Tree SHA:      d409052c02b7f3a894ae315a665b88df3d8a677c  (the `agents` tree)
# Licence:       Apache-2.0 — full text in ./LICENSE
# Modified:      yes. Frontmatter dropped. The "When to invoke" scenarios, the
#                "Review Scope" git-diff default and the closing summary
#                instruction are dropped — buildme supplies the diff and the
#                output format. The 0-100 confidence scale and its report
#                threshold are kept in substance; the threshold is scoped to
#                the reported `issues` list rather than to every finding,
#                because buildme adjudicates low-confidence findings instead of
#                discarding them.
# ---------------------------------------------------------------------------
CODE_REVIEWER_CONVENTIONS_BLOCK = """\
Review the change against the project's explicit written rules — import
patterns, framework conventions, language style, function and type
declarations, error handling, logging, testing practice, platform
compatibility, and naming. Alongside that, look for bugs that will actually
affect behavior: logic errors, unhandled null or missing values, race
conditions, resource leaks, security vulnerabilities, and performance
problems. And for the quality issues worth raising at review time: duplicated
code, missing critical error handling, accessibility problems, and inadequate
test coverage.

Rate every issue 0-100 on how confident you are that it is real:

- **0-25** — likely a false positive, or a pre-existing issue this change did
  not introduce.
- **26-50** — a minor nitpick that no written project rule asks for.
- **51-75** — valid, but low impact.
- **76-90** — an important issue that needs attention.
- **91-100** — a critical bug, or an explicit violation of a written project
  rule.

Be thorough, then filter aggressively — quality over quantity. Focus on the
issues that truly matter.

For each one give all four of: a clear description carrying its confidence
score; the file path and line number; the specific rule it breaks or an
explanation of the bug; and a concrete fix.
"""

# ---------------------------------------------------------------------------
# Vendored from: anthropics/claude-plugins-official
# Source path:   plugins/pr-review-toolkit/agents/silent-failure-hunter.md
# Blob SHA:      b8a8dfa41e18ef6ac801ae64be38b2508aa04f44
# Tree SHA:      d409052c02b7f3a894ae315a665b88df3d8a677c  (the `agents` tree)
# Licence:       Apache-2.0 — full text in ./LICENSE
# Modified:      yes. Frontmatter dropped. The entire "Special Considerations"
#                section is cut — it hardcoded one project's logging helpers,
#                its error-id module, and two SaaS vendors — and the same
#                house-style references interleaved into the "Logging Quality"
#                checklist are cut there too. The "Your Tone" section is
#                dropped. The named CRITICAL/HIGH/MEDIUM severity tier is
#                dropped in favour of buildme's own severity calibration and
#                the single 0-100 confidence scale. The removed-behavior
#                question this block sits under is NOT from upstream: this file
#                audits error handling in code that is present and never asks
#                what a deleted line used to guarantee. That question is
#                written by hand in `prompts/review.py`; only the process shape
#                and the output fields are borrowed.
# ---------------------------------------------------------------------------
SILENT_FAILURE_PROCESS_BLOCK = """\
Work the change in five passes. A silent failure is one that costs someone
hours later precisely because nothing recorded it at the time.

1.  **Locate every error handler.** Systematically find: every try/catch or
    try/except or Result-style branch; every error callback and error event
    handler; every conditional that handles an error state; every fallback or
    default value used on failure; every place an error is recorded and
    execution continues anyway; every null-safe access that might be skipping
    a failing operation.

2.  **Interrogate each one on five axes.**
    - *Logging quality* — is the failure recorded at a severity that matches
      its seriousness, with enough context to identify what operation failed
      and on what data? Would this line help someone debug the problem six
      months from now?
    - *Caller and user feedback* — does whoever is affected learn what went
      wrong and what they can do about it, or does the message say nothing
      that distinguishes this failure from any other?
    - *Catch specificity* — does the handler catch only the error types it
      expects? Name every kind of unexpected error this handler could swallow.
      Should it be several narrower handlers instead?
    - *Fallback behavior* — is the fallback explicitly asked for by the spec,
      or does it mask the underlying problem? Is it a fallback to a stub or
      fake outside test code?
    - *Propagation* — should this error travel to a higher-level handler
      rather than stop here? Does catching here skip cleanup or resource
      release that a propagated error would have triggered?

3.  **Examine the error messages themselves.** Each should say what went wrong
    in terms its reader understands, give an actionable next step, carry the
    relevant context such as the file or operation name, and be specific
    enough to tell this failure apart from a similar one.

4.  **Hunt the patterns that hide failures.** Empty catch blocks. Handlers
    that record the error and continue as if nothing happened. Returning null,
    a default, or an empty result on error with nothing recorded. Null-safe
    access used to silently skip an operation that was supposed to run.
    Fallback chains that try one approach after another without saying why the
    first failed. Retry logic that exhausts its attempts and tells no one.

5.  **Check against the project's own standards.** Whatever the project's
    written rules say about error handling — how failures are recorded, what
    context they must carry, where they are allowed to be swallowed — is the
    bar, not your own preference.
"""

# ---------------------------------------------------------------------------
# Vendored from: anthropics/claude-plugins-official
# Source path:   plugins/pr-review-toolkit/agents/silent-failure-hunter.md
# Blob SHA:      b8a8dfa41e18ef6ac801ae64be38b2508aa04f44
# Tree SHA:      d409052c02b7f3a894ae315a665b88df3d8a677c  (the `agents` tree)
# Licence:       Apache-2.0 — full text in ./LICENSE
# Modified:      yes. This is the upstream seven-field "Your Output Format"
#                list. Field 2 upstream is a CRITICAL/HIGH/MEDIUM tier; it is
#                replaced here by buildme's own severity calibration plus the
#                0-100 confidence score, so that one scale governs the whole
#                prompt.
# ---------------------------------------------------------------------------
SILENT_FAILURE_OUTPUT_BLOCK = """\
Describe each such finding with all seven of:

1.  **Location** — file path and line number.
2.  **Severity and confidence** — from the calibration below, plus the 0-100
    confidence score.
3.  **Issue description** — what is wrong and why it is a problem.
4.  **Hidden errors** — the specific kinds of failure that can now pass
    unnoticed.
5.  **Impact** — how this shows up for whoever hits it, and what it costs
    someone debugging it later.
6.  **Recommendation** — the specific change that fixes it.
7.  **Example** — what the corrected code should look like.
"""

# ---------------------------------------------------------------------------
# Vendored from: anthropics/claude-plugins-official
# Source path:   plugins/pr-review-toolkit/agents/type-design-analyzer.md
# Blob SHA:      9c17fec6b276cbe42f80b5e96e21e016b59c8e06
# Tree SHA:      d409052c02b7f3a894ae315a665b88df3d8a677c  (the `agents` tree)
# Licence:       Apache-2.0 — full text in ./LICENSE
# Modified:      yes. Structure only. Frontmatter dropped. Taken: step 1
#                "Identify Invariants" and the "Common Anti-patterns to Flag"
#                list. Dropped: the four 1-10 rating axes and their output
#                template, which collide with the single 0-100 scale, and the
#                per-type report format, which buildme does not emit.
# ---------------------------------------------------------------------------
TYPE_INVARIANT_BLOCK = """\
To name an invariant, look for: data-consistency requirements; which state
transitions are valid and which are not; constraints that hold between two or
more fields; business rules encoded in a type; and preconditions or
postconditions a function relied on.

These are the shapes an invariant most often dissolves into, and each is worth
flagging on its own:

- a type that exposes its internals for mutation;
- an invariant now enforced only by a comment or a docstring;
- validation missing at a construction boundary;
- enforcement applied inconsistently across the places that mutate;
- a type that depends on the code around it to keep its own invariant true.
"""

# ---------------------------------------------------------------------------
# Vendored from: anthropics/claude-plugins-official
# Source path:   plugins/pr-review-toolkit/agents/pr-test-analyzer.md
# Blob SHA:      05b342b9175af85c5d7404bac67f5c62da375aa2
# Tree SHA:      d409052c02b7f3a894ae315a665b88df3d8a677c  (the `agents` tree)
# Licence:       Apache-2.0 — full text in ./LICENSE
# Modified:      yes. Structure only. Frontmatter dropped. Taken: the "adequate
#                coverage without pedantry about 100%" posture, the four test-
#                quality questions, and the critical-gaps list. Dropped: the
#                1-10 criticality scale and its band definitions, which collide
#                with the single 0-100 scale, and the five-section report
#                format, which buildme does not emit.
# ---------------------------------------------------------------------------
TEST_COVERAGE_BLOCK = """\
Judge coverage behaviorally, not by lines. The bar is adequate coverage of the
functionality that matters, without pedantry about hitting 100% — a test that
prevents a real bug is worth raising, one that chases a metric is not.

A gap is worth reporting when it is one of: an untested error path that could
fail silently; a missing boundary case; an uncovered branch of real logic; an
absent negative case for validation; or missing coverage of concurrent or
asynchronous behavior where that behavior matters.

Ask four things of the tests that are there:

- do they test behavior and contracts, or implementation details?
- would they catch a meaningful regression from a future change?
- do they survive a reasonable refactoring?
- do their names describe, in meaningful phrases, what they actually assert?
"""
