from __future__ import annotations

import re

from code_facts import ExploreOutput, Span, citation_rules, explore_unit


def test_whole_files_prompt_explains_a_module():
    p = explore_unit("bookings", [Span.whole("e/a.py", 12)], ExploreOutput, "standard")
    assert "explaining the module `bookings`" in p
    assert "Files in your part of the module:" in p
    assert re.search(r"^- e/a\.py \(12 lines\)$", p, re.MULTILINE)
    assert "`conventions`:" not in p.split("Schema:")[0]


def test_range_prompt_names_ranges_and_the_base_side():
    p = explore_unit(
        "the change",
        [
            Span(path="e/a.py", start=3, end=9),
            Span(path="e/a.py", start=2, end=2, side="base"),
        ],
    )
    assert "Line ranges in your part of the code:" in p
    assert "- e/a.py, lines 3-9\n" in p
    assert "- e/a.py, lines 2-2 (before the change)" in p


def test_base_dir_says_where_the_lines_before_the_change_are_and_how_to_cite_them():
    spans = [
        Span(path="e/a.py", start=3, end=9),
        Span(path="e/a.py", start=2, end=4, side="base"),
    ]
    p = explore_unit("the change", spans, base_dir="/tmp/review/base-tree/")
    assert "- e/a.py, lines 3-9\n" in p
    assert (
        "- e/a.py, lines 2-4 (before the change; read it at /tmp/review/base-tree/e/a.py, "
        'and cite it with `side` "base")'
    ) in p


def test_missing_files_and_conventions_are_asked_for_only_when_given():
    p = explore_unit(
        "m", [Span.whole("e/a.py", 40)], missing=["e/a.py"], conventions=True
    )
    assert "An earlier pass left these files" in p
    assert "- `conventions`: the rules new code" in p


def test_the_caller_says_what_form_paths_take():
    rule = "- `path` starts with the repository name."
    assert rule in citation_rules(rule)
    assert rule in explore_unit("m", [Span.whole("e/a.py", 1)], path_rule=rule)


def test_the_callers_notes_follow_the_listing_and_are_absent_by_default():
    spans = [Span.whole("e/a.py", 12)]
    note = "\nAlso read tests/test_a.py for how tests are written.\n"
    p = explore_unit("m", spans, conventions=True, notes=note)
    listing, rest = p.split("- e/a.py (12 lines)\n", 1)
    assert rest.index("Also read tests/test_a.py") < rest.index(
        "Return ONE JSON object"
    )
    assert explore_unit("m", spans, conventions=True) == p.replace(note, "")
