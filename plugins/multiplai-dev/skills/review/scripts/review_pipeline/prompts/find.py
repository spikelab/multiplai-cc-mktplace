"""Finder prompt: one per dimension.

A change review shows the agent the diff. A tree review (a whole repository or
a directory, not a change) has no diff: the prompt lists one group of files the
agent must open with Read, and each dimension's task is worded for code that
exists rather than code that changed (`DIMENSION_TASKS_TREE`).
"""

from __future__ import annotations

from ..models import TargetInfo
from . import (CITATION_RULES, JSON_ONLY, commits_block, description_block, diff_block, files_block,
               settings_block, workspace_block)

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
        "is a finding with `dimension` \"pre-existing\", citing the stale line and the rule that says the "
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
               "dimension": "" }]}"""


FILE_RULE = (
    "Every finding cites the lines that show the problem, with the exact quote. A finding "
    "without a quote is discarded by a program, not a person. `file` must be one of the changed "
    "files. If the problem is in an unchanged file that the change makes reachable, executes, or "
    "makes wrong (code the change now calls or builds, documentation the change makes stale), set "
    "`dimension` to \"pre-existing\" and `file` to that file; otherwise leave `dimension` empty. "
    "Report only what you can show from the code; an empty list is a valid answer."
)

FILE_RULE_TREE = (
    "Every finding cites the lines that show the problem, with the exact quote. A finding "
    "without a quote is discarded by a program, not a person. `file` must be a file under review: "
    "one listed above, or another file in the reviewed directory. If the problem is in a file outside "
    "the review that these files call, build or make wrong, set `dimension` to \"pre-existing\" and "
    "`file` to that file; otherwise leave "
    "`dimension` empty. Report only what you can show from the code; an empty list is a valid answer."
)


def tree_files_block(files: list[str]) -> str:
    listed = "\n".join(f"- {f}" for f in files)
    return (f"This is not a change: you are reviewing code as it stands. The files under review in this "
            f"call ({len(files)}) are below. Open each one with Read; their text is not in this prompt.\n"
            f"{listed}")


def build(target: TargetInfo, dimension: str, diff: str, conventions: str = "",
          files: list[str] | None = None) -> str:
    """The finder prompt. For a tree target, *files* is the group this call reviews."""
    if target.is_tree:
        return _build_tree(target, dimension, conventions, files if files is not None else list(target.files))
    parts = [
        f"You are one reviewer in a code review, looking at one aspect: {dimension}. Your final "
        f"message is one JSON object listing defects (schema at the end), read by a program; it is "
        f"not a report.",
        workspace_block(target, web=True),
        DIMENSION_TASKS[dimension],
        commits_block(target),
        description_block(target),
        settings_block(target),
        files_block(target),
        diff_block(target, diff),
    ]
    parts = [p for p in parts if p]
    if dimension == "conventions":
        parts.append("The repository's rules:\n\n" + (conventions or "(no coding-standards.md or CLAUDE.md files found)"))
    parts += [
        SEVERITY_GUIDE,
        CITATION_RULES,
        FILE_RULE,
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ]
    return "\n\n".join(parts)


def _build_tree(target: TargetInfo, dimension: str, conventions: str, files: list[str]) -> str:
    parts = [
        f"You are one reviewer in a code review, looking at one aspect: {dimension}. Your final "
        f"message is one JSON object listing defects (schema at the end), read by a program; it is "
        f"not a report.",
        workspace_block(target, web=True),
        DIMENSION_TASKS_TREE[dimension],
        tree_files_block(files),
    ]
    if dimension == "conventions":
        parts.append("The repository's rules:\n\n" + (conventions or "(no coding-standards.md or CLAUDE.md files found)"))
    parts += [
        SEVERITY_GUIDE,
        CITATION_RULES,
        FILE_RULE_TREE,
        "Schema:\n" + SCHEMA.replace("path of the changed file the bug is in", "path of the file under review the bug is in"),
        JSON_ONLY,
    ]
    return "\n\n".join(parts)
