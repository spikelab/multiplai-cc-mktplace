"""The prompt blocks added for PR context, branch rules and the web, and the finders' report prompt."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import high_finding
from review_pipeline.config import DIMENSIONS, TRIM_DIMENSION
from review_pipeline.models import Piece
from review_pipeline.prompts import (
    JSON_ONLY,
    description_block,
    find,
    merge,
    settings_block,
    verify,
    workspace_block,
)
from review_pipeline.stages.find import shared_blocks

ALL_DIMENSIONS = (*DIMENSIONS, TRIM_DIMENSION)
PACKAGE = Path(__file__).resolve().parents[1] / "review_pipeline"


def report(target, dimension: str, diff: str = "diff", **kwargs) -> str:
    """A dimension finder's report prompt on one piece, with the shared blocks the find stage gives it."""
    piece = kwargs.pop("piece", Piece(id=1, group="", files=["settings.py"]))
    return find.build_report(
        target,
        dimension,
        piece,
        diff,
        workspace=shared_blocks(target, web=True),
        **kwargs,
    )


RULES_NO_CHECKS = [
    {"type": "pull_request", "parameters": {"required_approving_review_count": 1}}
]
RULES_WITH_CHECKS = [
    {
        "type": "required_status_checks",
        "parameters": {
            "required_status_checks": [{"context": "build"}],
            "strict_required_status_checks_policy": True,
        },
    }
]


def pr_target(target_info, **extra):
    return target_info.model_copy(
        update={
            "pr": 14,
            "title": "Add retention",
            "description": "CI gates the merge.",
            "base_ref": "main",
            "branch_rules": RULES_NO_CHECKS,
            **extra,
        }
    )


def test_range_targets_add_no_description_or_settings(target_info):
    assert description_block(target_info) == "" and settings_block(target_info) == ""
    prompt = report(target_info, "diff-bugs")
    assert "untrusted-content" not in prompt.replace(
        '<untrusted-content source="diff of piece 1">', ""
    ).replace("</untrusted-content>", "")
    assert "Repository settings" not in prompt


def test_description_is_fenced_and_called_a_claim(target_info):
    block = description_block(pr_target(target_info))
    assert '<untrusted-content source="pull request #14 description">' in block
    assert "Title: Add retention" in block and "CI gates the merge." in block
    assert "claim to check" in block and "</untrusted-content>" in block


def test_description_is_cut_when_long(target_info):
    block = description_block(pr_target(target_info, description="x" * 20_000))
    assert "[... description cut ...]" in block and len(block) < 13_000


def test_settings_say_when_no_check_blocks_a_merge(target_info):
    block = settings_block(pr_target(target_info))
    assert "pull_request: 1 approval required" in block
    assert (
        "no `required_status_checks` rule" in block
        and "a red run can be merged" in block
    )


def test_settings_name_the_required_checks(target_info):
    block = settings_block(pr_target(target_info, branch_rules=RULES_WITH_CHECKS))
    assert "required_status_checks: build (branch must be up to date)" in block
    assert "a red run can be merged" not in block


def test_settings_when_rules_could_not_be_read(target_info):
    block = settings_block(pr_target(target_info, branch_rules=None))
    assert "could not be read" in block and "Do not assume CI blocks a merge" in block


def test_settings_when_github_reports_no_rules_at_all(target_info):
    block = settings_block(pr_target(target_info, branch_rules=[]))
    assert (
        "GitHub reports no rules on this branch" in block
        and "a red run can be merged" in block
    )


def test_finders_and_verifier_get_both_blocks_and_the_web(target_info):
    t = pr_target(target_info)
    for dimension in find.DIMENSION_TASKS:
        prompt = report(t, dimension)
        assert "WebFetch and WebSearch" in prompt
        assert (
            "pull request #14 description" in prompt and "Repository settings" in prompt
        )
    prompt = verify.build(t, high_finding())
    assert "WebFetch and WebSearch" in prompt and "Repository settings" in prompt
    assert "`unverifiable`, not `confirmed` with a caveat" in prompt


def test_merger_has_no_web(target_info):
    prompt = merge.build(pr_target(target_info), [high_finding()])
    assert "WebFetch" not in prompt
    assert workspace_block(target_info) == workspace_block(target_info, web=False)


def test_conventions_finder_is_told_to_look_outside_the_diff(target_info):
    prompt = report(target_info, "conventions", conventions="# rules")
    assert "Grep the whole repository for other statements of the old fact" in prompt
    assert "where something must be recorded" in prompt
    assert "documentation the change makes stale" in prompt


def test_citation_rules_put_the_web_second(target_info):
    prompt = find.build(target_info.model_copy(update={"kind": "tree"}), "diff-bugs")
    assert "the first\ncitation is always a file at this commit" in prompt


def test_conventions_finder_reads_coding_standards(target_info):
    prompt = report(target_info, "conventions", conventions="# rules")
    assert "coding-standards.md" in prompt
    assert "even where the rule states no consequence" in prompt


def test_conventions_finder_says_when_there_are_no_rules(target_info):
    prompt = report(target_info, "conventions")
    assert "(no coding-standards.md or CLAUDE.md files found)" in prompt


def test_tests_finder_reports_tests_that_cannot_fail(target_info):
    prompt = report(target_info, "tests")
    assert "asserts a value copied from the implementation" in prompt  # (a)
    assert "reads a source file as text" in prompt  # (b)
    assert "replaces with a mock or stub the exact dependency" in prompt  # (c)
    assert "Rate such a finding LOW at most" in prompt
    assert "Do not describe what the tests cover." in prompt


STRICT_IMPACT = (
    "Rate `breaks-users` or `breaks-business` only when all three hold: (1) default settings, or a setting or "
    "workflow the plugin's or project's own docs present as normal use, reach the path; a rare host, file "
    "encoding or opt-out setting does not; (2) the harm follows from this defect alone, with no second failure "
    "needed (a model call failing, an agent breaking its prompt, a person's separate mistake); a crash or stop "
    "the code promises to survive counts as ordinary; (3) no other check in the code, CI or the documented "
    "workflow stops the harm before it lands. Otherwise rate `correctness-only`."
)


def test_verifier_rates_breaking_impact_only_when_all_three_conditions_hold(
    target_info,
):
    """plan-review-rounds-and-finders-2026-10-15.md, item 2: the strict
    reading, verbatim, after the `breaks-business` bullet and before
    `correctness-only`."""
    prompt = " ".join(verify.build(target_info, high_finding()).split())
    assert STRICT_IMPACT in prompt
    assert (
        prompt.index("- `breaks-business`:")
        < prompt.index(STRICT_IMPACT)
        < prompt.index("- `correctness-only`:")
    )


def _with_deps(target_info):
    from review_pipeline.models import DepEntry

    entries = [
        DepEntry(
            name=n,
            url=f"https://example.com/{n}",
            commit=c * 40,
            cache="/c",
            status="ok",
        )
        for n, c in (("multiplai-core", "a"), ("other-lib", "b"))
    ]
    return target_info.model_copy(update={"deps": entries})


def test_finders_and_verifier_are_told_where_the_dependency_source_is(target_info):
    target = _with_deps(target_info)
    prompts = [
        report(target, "diff-bugs"),
        verify.build(target, high_finding()),
    ]
    for prompt in prompts:
        assert f".review-deps/multiplai-core@{'a' * 12}/" in prompt
        assert f".review-deps/other-lib@{'b' * 12}/" in prompt
        assert "never report a defect that lies only in a dependency" in prompt.lower()
        assert "only after a line of the repository" in prompt


def test_without_dependencies_the_prompts_name_none(target_info):
    for prompt in (
        report(target_info, "diff-bugs"),
        verify.build(target_info, high_finding()),
    ):
        assert ".review-deps" not in prompt


# --- the report prompt of one dimension on one piece ---------------------------------------------


def big_piece_diff(chars: int = 60_000) -> str:
    lines = [f"+line_{i:06d} = {i}\n" for i in range(chars // 15)]
    body = "".join(lines)
    return (
        "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


@pytest.mark.parametrize("dimension", ALL_DIMENSIONS)
def test_every_dimension_sees_the_whole_piece_diff_and_answers_a_report(
    target_info, dimension
):
    diff = big_piece_diff()
    assert len(diff) > 60_000
    prompt = report(
        target_info,
        dimension,
        diff,
        piece=Piece(id=3, group="", files=["big.py"], chars=len(diff)),
    )
    assert diff in prompt
    assert "diff cut at" not in prompt
    assert find.REPORT_FORMAT in prompt and find.CLASS_AND_PLACES in prompt
    assert JSON_ONLY not in prompt and "Schema:" not in prompt
    assert f"looking at one aspect: {dimension}" in prompt
    assert find.READING in prompt and find.OWNERSHIP in prompt


def test_diff_bugs_reviews_broadly_and_the_others_keep_their_task(target_info):
    assert find.REVIEW_BRIEF in report(target_info, "diff-bugs")
    assert find.DIMENSION_TASKS["diff-bugs"] not in report(target_info, "diff-bugs")
    for dimension in ALL_DIMENSIONS[1:]:
        prompt = report(target_info, dimension)
        assert find.DIMENSION_TASKS[dimension] in prompt
        assert find.REVIEW_BRIEF not in prompt


def test_only_the_conventions_finder_gets_the_rules(target_info):
    assert "# the rules" in report(
        target_info, "conventions", conventions="# the rules"
    )
    assert "# the rules" not in report(
        target_info, "callers", conventions="# the rules"
    )


def test_a_report_can_mark_a_pre_existing_defect_and_what_it_could_not_check(
    target_info,
):
    prompt = report(target_info, "conventions")
    assert "`Pre-existing: yes`" in prompt and "`Could not check:`" in prompt


def test_a_half_says_which_piece_it_was_cut_from(target_info):
    half = Piece(id=7, group="", files=["settings.py"], split_from=2, hunks=[0, 1])
    assert "Your piece is piece 7, part 2.7 of piece 2" in report(
        target_info, "callers", piece=half
    )


def test_the_piece_prompt_builder_is_gone():
    found = subprocess.run(
        ["git", "grep", "-n", "build_piece", "--", str(PACKAGE)],
        check=False,  # git grep exits 1 when nothing matches
        capture_output=True,
        text=True,
    )
    assert found.stdout == ""
    assert not (PACKAGE / "prompts" / "piece.py").exists()
