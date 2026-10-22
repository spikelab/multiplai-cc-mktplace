from __future__ import annotations

from review_pipeline.pr_marker import (
    MARKER_RE,
    MARKER_VERSION,
    format_marker,
    parse_markers,
)


def test_the_marker_format_is_pinned():
    # buildme-findings copies the regex; changing this string must be deliberate.
    assert (
        format_marker("0123456789", "abcdef012345")
        == "<!-- multiplai-review:finding id=0123456789 round=abcdef012345 v=1 -->"
    )
    assert MARKER_VERSION == 1


def test_a_marker_round_trips_through_parse_markers():
    body = f"**HIGH** — a claim\n\nwhat breaks\n\n{format_marker('0123456789', 'abcdef012345-2')}"
    assert parse_markers(body) == [
        {"id": "0123456789", "round": "abcdef012345-2", "v": 1}
    ]


def test_every_marker_in_a_body_is_found_in_order():
    body = "\n".join(
        [
            "1. first",
            format_marker("aaaaaaaaaa", "111111111111"),
            "",
            "2. second",
            format_marker("bbbbbbbbbb", "222222222222-3"),
        ]
    )
    assert [m["id"] for m in parse_markers(body)] == ["aaaaaaaaaa", "bbbbbbbbbb"]
    assert [m["round"] for m in parse_markers(body)] == [
        "111111111111",
        "222222222222-3",
    ]


def test_ordinary_text_and_other_html_comments_do_not_match():
    for body in (
        "",
        "Looks good to me.",
        "<!-- a note -->",
        "multiplai-review:finding id=0123456789",
        "<!-- multiplai-review:finding id=XYZ round=abc v=1 -->",
    ):
        assert parse_markers(body) == []
        assert MARKER_RE.search(body) is None
    assert parse_markers(None) == []


def test_the_regex_documented_in_skill_md_is_the_one_in_the_code():
    from pathlib import Path

    skill = (Path(__file__).resolve().parents[2] / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert MARKER_RE.pattern in skill
    assert format_marker("<finding_id>", "<round>") in skill
