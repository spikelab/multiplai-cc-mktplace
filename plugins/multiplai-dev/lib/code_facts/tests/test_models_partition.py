from __future__ import annotations

import pytest
from code_facts import Citation, Scope, Span, partition
from pydantic import ValidationError


def test_citation_defaults_to_head_and_orders_lines():
    c = Citation(path="a.py", line_start=2, line_end=3, quote="x")
    assert c.side == "head"
    with pytest.raises(ValidationError):
        Citation(path="a.py", line_start=3, line_end=2, quote="x")


def test_span_lines_and_whole_files():
    assert Span(path="a.py", start=4, end=9).lines == 6
    assert Span.whole("empty.py", 0).lines == 0
    scope = Scope.whole_files({"a/x.py": 10, "b/y.py": 5})
    assert [(s.path, s.start, s.end, s.whole_file) for s in scope.spans] == [
        ("a/x.py", 1, 10, True),
        ("b/y.py", 1, 5, True),
    ]


def test_partition_keeps_directories_together_and_isolates_big_spans():
    scope = Scope.whole_files(
        {"a/1.py": 5, "a/2.py": 5, "b/3.py": 50, "c/4.py": 8, "c/5.py": 9}
    )
    units = partition(scope, 20)
    assert [u.files for u in units] == [
        ["a/1.py", "a/2.py"],
        ["b/3.py"],
        ["c/4.py", "c/5.py"],
    ]
    assert units[1].oversize and units[1].lines == 50
    assert [u.id for u in units] == ["u1", "u2", "u3"]
    assert all(u.lines <= 20 or (u.oversize and len(u.spans) == 1) for u in units)


def test_partition_splits_a_directory_larger_than_the_limit():
    scope = Scope.whole_files({f"d/{i}.py": 8 for i in range(5)})
    units = partition(scope, 20)
    assert sorted(f for u in units for f in u.files) == [f"d/{i}.py" for i in range(5)]
    assert all(u.lines <= 20 for u in units)


def test_partition_takes_line_ranges_and_names_each_file_once():
    scope = Scope(
        spans=[
            Span(path="a/x.py", start=40, end=49),
            Span(path="a/x.py", start=1, end=5),
            Span(path="a/x.py", start=3, end=4, side="base"),
        ]
    )
    (unit,) = partition(scope, 100)
    assert unit.files == ["a/x.py"]
    assert unit.lines == 10 + 5 + 2
    assert [(s.side, s.start) for s in unit.spans] == [
        ("base", 3),
        ("head", 1),
        ("head", 40),
    ]
