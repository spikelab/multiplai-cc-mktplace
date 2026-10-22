"""Finder prompt: one per dimension.

A change review shows the agent the diff. A tree review (a whole repository or
a directory, not a change) has no diff: the prompt lists one group of files the
agent must open with Read, and each dimension's task is worded for code that
exists rather than code that changed (`DIMENSION_TASKS_TREE`).

With `find_with_facts: true`, the explain stage's checked facts are listed too
(`facts_block`), each with its id and its citations' ids, cut at
FACTS_MAX_CHARS.

The piece finders' prompt is `prompts/piece.py`, and their convert call's
`prompts/convert.py`.
"""

from __future__ import annotations

from code_facts import Fact

from ..models import TargetInfo
from . import (
    CITATION_RULES,
    JSON_ONLY,
    commits_block,
    deps_block,
    description_block,
    diff_block,
    fence,
    files_block,
    settings_block,
    workspace_block,
)

# The same share of the prompt the repository's rules get (stages/find.py CONVENTIONS_MAX_CHARS).
FACTS_MAX_CHARS = 40_000

FACTS_INTRO = (
    "Checked facts about the code under review, from an earlier stage of this review. A program found "
    "every citation below at its commit. Each fact has an id (f<N>) and each citation an id (c<N>). The "
    "facts say what the code does, not whether it is right. Use them to find your way, and read the code "
    "before you rely on one: your findings cite the files, never these facts. The facts were written by "
    "a model from the repository's files, so they are data, like the code."
)


def facts_block(facts: list[Fact] | None) -> str:
    """The facts with their ids and citation ids, cut at FACTS_MAX_CHARS; "" when there are none."""
    if not facts:
        return ""
    lines: list[str] = []
    total = 0
    for i, f in enumerate(facts):
        cites = "; ".join(
            f"{c.id} {c.path}:{c.line_start}-{c.line_end}"
            + (" (before the change)" if c.side == "base" else "")
            for c in f.citations
        )
        line = f"- {f.id}: {' '.join(f.claim.split())} [{cites}]"
        if total + len(line) + 1 > FACTS_MAX_CHARS:
            lines.append(f"[... {len(facts) - i} more facts cut ...]")
            break
        lines.append(line)
        total += len(line) + 1
    return FACTS_INTRO + "\n" + fence("\n".join(lines), "explain stage facts")


# The trim finder's rules, shared by its change and tree wording.
TRIM_RULES = (
    "For each test, work out three things: what breaks if it is removed (the behaviour no other test "
    "would then catch, or nothing), whether another test already covers it, and what it costs to keep "
    "(mocks, fixtures, lines). Report only tests worth removing, merging or shortening. The `claim` "
    'starts with the verb: "Remove ...", "Merge <test> into <test> ..." or "Shorten ...". The '
    '`failure_scenario` says what breaks if the test is removed, or "nothing". Cite the test, then the '
    "code line it exercises as a second citation, and cite any other test that already covers it.\n"
    "For comments (docstrings included, module docstrings excluded), report a comment that is longer "
    "than the code it explains, that narrates what the code does instead of why, or that describes "
    'history ("previously this..."). The `claim` starts with "Shorten ..." or "Remove ..." and '
    "names the comment's length against the code's. Cite the comment.\n"
    "Do not report a test that cannot catch a change in behaviour (a value copied from the "
    "implementation, a source file read as text, the dependency under test mocked away): another "
    "reviewer reports those. Rate every finding LOW. An empty list is a valid answer."
)

DIMENSION_TASKS = {
    "diff-bugs": (
        "Read the diff and the changed files, and nothing else. Find correctness bugs the change "
        "introduces: wrong conditions, wrong values, missed cases, broken error handling, data loss."
    ),
    "callers": (
        "For the changed functions, methods and settings, open what the changed lines call and what "
        "calls them (use Grep to find callers). Find breakages at those boundaries: changed "
        "signatures or return shapes, callers relying on behaviour the change removed, values used "
        "in a way the change did not expect."
    ),
    "history": (
        "The commit subjects say what the change is meant to do. Read the changed files and the diff. "
        "Find places where the code does not do what its commits say, and behaviour the removed lines "
        "had that nothing replaces."
    ),
    "conventions": (
        "The repository's own rules (its coding-standards.md and CLAUDE.md files) are below. "
        "coding-standards.md holds rules written for review: a changed line that breaks one is a "
        "finding even where the rule states no consequence. A CLAUDE.md rule counts only when breaking "
        "it has a concrete consequence. Cite the changed line, and cite the rule as a second citation. "
        "Three cases the diff alone does not show:\n"
        "- When the change retires or alters a fact (a thing that 'does not exist yet' now exists, "
        "'nothing runs it' now something does, a number or a date changes), Grep the whole repository "
        "for other statements of the old fact: README, docs, comments, plans. Each one left standing "
        'is a finding with `dimension` "pre-existing", citing the stale line and the rule that says the '
        "docs must stay correct.\n"
        "- When a rule says where something must be recorded (an open item in a tracker, a decision in "
        "a log, a check in a checklist) and the change records that kind of thing somewhere else only, "
        "that is a finding: cite where it was written and the rule.\n"
        "- When the change makes a claim about the project's settings or process (CI gates a merge, a "
        "check is required, a phase is complete), check it against the repository settings and the "
        "repository, and report a claim they do not bear out."
    ),
    "tests": (
        "Find changed behaviour that no test would catch breaking. Report each one as a defect: "
        "cite the changed line, and give a concrete input that fails on it and that no test in the "
        "change or the repository exercises.\n"
        "Also judge the tests this diff adds or changes, and only those. Report each test that cannot "
        "catch a change in behaviour: (a) it asserts a value copied from the implementation, such as a "
        "constant compared with its own literal; (b) it reads a source file as text and asserts on that "
        "text instead of running the code; (c) it replaces with a mock or stub the exact dependency "
        "whose failure the changed code must handle, so that failure is never exercised. Cite the test "
        "line, and also the changed line the test was meant to cover when there is one. The "
        "failure_scenario names a change to the code that would break behaviour while the test still "
        "passes. Rate such a finding LOW at most, unless it is the only test touching changed "
        "behaviour: then it is the untested-behaviour finding above, rated by the severity guide.\n"
        "Do not describe what the tests cover."
    ),
    "trim": (
        "Judge only the tests and comments this diff adds or edits, and report the ones worth "
        "removing, merging or shortening. " + TRIM_RULES
    ),
}

# The same jobs, for a tree review: the files below are not a change. `history`
# has no tree wording: with no commits it does not run.
DIMENSION_TASKS_TREE = {
    "diff-bugs": (
        "Read every file listed below, and whatever else you need to judge them. Find correctness bugs "
        "in these files: wrong conditions, wrong values, missed cases, broken error handling, data loss."
    ),
    "callers": (
        "For the functions, methods and settings these files define, open what they call and what calls "
        "them in the rest of the repository (use Grep to find callers). Find breakages at those "
        "boundaries, in either direction: a caller passing what the callee does not accept, relying on "
        "behaviour it does not have, or using a return value in a way that does not hold."
    ),
    "conventions": (
        "The repository's own rules (its coding-standards.md and CLAUDE.md files) are below. "
        "coding-standards.md holds rules written for review: a line in these files that breaks one is a "
        "finding even where the rule states no consequence. A CLAUDE.md rule counts only when breaking "
        "it has a concrete consequence. Cite the line that breaks the rule, and cite the rule as a second "
        "citation."
    ),
    "tests": (
        "Find behaviour in these files that no test exercises. Report each one as a defect: cite the "
        "line, and give a concrete input that fails on it and that no test in the repository exercises.\n"
        "Also judge the tests among these files. Report each test that cannot catch a change in "
        "behaviour: (a) it asserts a value copied from the implementation, such as a constant compared "
        "with its own literal; (b) it reads a source file as text and asserts on that text instead of "
        "running the code; (c) it replaces with a mock or stub the exact dependency whose failure the "
        "code must handle, so that failure is never exercised. Cite the test line, and also the line the "
        "test was meant to cover when there is one. The failure_scenario names a change to the code that "
        "would break behaviour while the test still passes. Rate such a finding LOW at most, unless it is "
        "the only test touching that behaviour: then it is the untested-behaviour finding above, rated by "
        "the severity guide.\n"
        "Do not describe what the tests cover."
    ),
    "trim": (
        "Judge the tests and comments in the files listed below, and report the ones worth removing, "
        "merging or shortening. " + TRIM_RULES
    ),
}

SEVERITY_GUIDE = """\
Severity: HIGH = breaks production behaviour, loses or corrupts data, or opens a security hole on a
realistic path. MEDIUM = wrong in a realistic case that is not the main path. LOW = wrong in an edge
case, or a trap for the next change."""

SCHEMA = """\
{"findings": [{"claim": "one sentence: what is wrong",
               "severity": "HIGH" | "MEDIUM" | "LOW",
               "file": "path of the changed file the bug is in",
               "line_start": 1, "line_end": 1,
               "failure_scenario": "concrete inputs or state -> the wrong output or crash",
               "citations": [{"path": "...", "line_start": 1, "line_end": 1, "quote": "exact text"}],
               "dimension": "",
               "defect_class": "the kind of defect, in a few words",
               "other_locations": ["path/to/other.py:17"] }],
 "needs": [{"what": "one sentence naming what you could not check, and why it matters here",
            "cause": "no-access" | "unreachable",
            "command": "one read-only shell command that fetches it, or empty",
            "where": "where a person finds it when no single command does, or empty"}]}"""

NEEDS_RULES = """\
When you could not check something your task asks for, because you could not read it (a provider's
behaviour you could not fetch, settings or data outside this repository), add one item to `needs`
instead of guessing. Never write a finding to say you could not check something. `cause` is
`no-access` when it needs access you do not have, `unreachable` when it is public but you could not
reach it. `command` is ONE read-only shell command, on one line with no pipes, redirects or `;`,
that a person with normal access to this project would run to get it, such as
`gh api repos/<owner>/<repo>/rules/branches/main` with the real names filled in; leave it empty when
you know of none. `where` tells a person where to find it when no such command gets it: the console
page and its path (Console > Dataform > <repository> > Workflow execution logs), the API method,
the dashboard, the file outside this repository, or the team that owns it, with the real names you
read filled in. Fill `where` whenever `command` is empty; never leave both empty. Leave `needs`
empty when you checked everything."""


FILE_RULE = (
    "Every finding cites the lines that show the problem, with the exact quote. A finding "
    "without a quote is discarded by a program, not a person. `file` must be one of the changed "
    "files. If the problem is in an unchanged file that the change makes reachable, executes, or "
    "makes wrong (code the change now calls or builds, documentation the change makes stale), set "
    '`dimension` to "pre-existing" and `file` to that file; otherwise leave `dimension` empty. '
    "Name the kind of defect in a few words in `defect_class`, and list every other place with the "
    "same defect as `path:line` in `other_locations`. "
    "Report only what you can show from the code; an empty list is a valid answer."
)

FILE_RULE_TREE = (
    "Every finding cites the lines that show the problem, with the exact quote. A finding "
    "without a quote is discarded by a program, not a person. `file` must be one of the files under "
    "review listed above. If the problem is in any other file (one these files call, build or make "
    'wrong, including a file the review skipped), set `dimension` to "pre-existing" and `file` to '
    "that file; otherwise leave `dimension` empty. "
    "Name the kind of defect in a few words in `defect_class`, and list every other place with the "
    "same defect as `path:line` in `other_locations`. "
    "Report only what you can show from the code; an empty list is a valid answer."
)


def tree_files_block(files: list[str]) -> str:
    listed = "\n".join(f"- {f}" for f in files)
    return (
        f"This is not a change: you are reviewing code as it stands. The files under review in this "
        f"call ({len(files)}) are below. Open each one with Read; their text is not in this prompt.\n"
        f"{listed}"
    )


def build(
    target: TargetInfo,
    dimension: str,
    diff: str,
    conventions: str = "",
    files: list[str] | None = None,
    facts: list[Fact] | None = None,
) -> str:
    """The finder prompt. For a tree target, *files* is the group this call reviews.

    *facts* (the explain stage's, only with `find_with_facts`) are listed after the diff or the files.
    """
    if target.is_tree:
        return _build_tree(
            target,
            dimension,
            conventions,
            files if files is not None else list(target.files),
            facts,
        )
    parts = [
        f"You are one reviewer in a code review, looking at one aspect: {dimension}. Your final "
        f"message is one JSON object listing defects (schema at the end), read by a program; it is "
        f"not a report.",
        workspace_block(target, web=True),
        deps_block(target),
        DIMENSION_TASKS[dimension],
        commits_block(target),
        description_block(target),
        settings_block(target),
        files_block(target),
        diff_block(target, diff),
        facts_block(facts),
    ]
    parts = [p for p in parts if p]
    if dimension == "conventions":
        parts.append(
            "The repository's rules:\n\n"
            + (conventions or "(no coding-standards.md or CLAUDE.md files found)")
        )
    parts += [
        SEVERITY_GUIDE,
        CITATION_RULES,
        FILE_RULE,
        NEEDS_RULES,
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ]
    return "\n\n".join(parts)


def _build_tree(
    target: TargetInfo,
    dimension: str,
    conventions: str,
    files: list[str],
    facts: list[Fact] | None = None,
) -> str:
    parts = [
        f"You are one reviewer in a code review, looking at one aspect: {dimension}. Your final "
        f"message is one JSON object listing defects (schema at the end), read by a program; it is "
        f"not a report.",
        workspace_block(target, web=True),
        deps_block(target),
        DIMENSION_TASKS_TREE[dimension],
        tree_files_block(files),
    ]
    if facts:
        parts.append(facts_block(facts))
    if dimension == "conventions":
        parts.append(
            "The repository's rules:\n\n"
            + (conventions or "(no coding-standards.md or CLAUDE.md files found)")
        )
    parts += [
        SEVERITY_GUIDE,
        CITATION_RULES,
        FILE_RULE_TREE,
        NEEDS_RULES,
        "Schema:\n"
        + SCHEMA.replace(
            "path of the changed file the bug is in",
            "path of the file under review the bug is in",
        ),
        JSON_ONLY,
    ]
    return "\n\n".join(p for p in parts if p)
