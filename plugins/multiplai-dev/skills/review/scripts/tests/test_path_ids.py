"""Path ids on findings: after merge, a finding lists each mapped path one of its citations contains a line of.

`paths_io.assign_paths` is tested on `PathsResult`s built from the real
`code_facts` models (built without validation where a test needs a hop that
`paths.v1` would refuse: one with no line, or a `./` path). The end-to-end
tests reuse `test_reported_finders.py`'s repositories and fake model.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import jsonschema
import pytest
from code_facts.paths import (
    E2EListing,
    FallbackStatus,
    Handler,
    Location,
    PathDrop,
    PathEntry,
    PathHop,
    PathsResult,
    TracedPath,
)
from conftest import SCHEMA
from review_pipeline.__main__ import main
from review_pipeline.models import Citation, Finding
from test_reported_finders import (  # noqa: F401
    make_repo,
    mapped,
    model,
    path_item,
    resume,
    review,
    saved,
    traced_path,
)

VIEWER_EXAMPLE = (
    SCHEMA.parents[1] / "scripts" / "tests" / "fixtures" / "findings.example.json"
)


def new(module: str, name: str):
    return getattr(importlib.import_module(f"review_pipeline.{module}"), name)


def assign(findings, result):
    return new("paths_io", "assign_paths")(findings, result)


# --- building paths and findings ----------------------------------------------------------------------------


def hop(path: str, line: int | None) -> PathHop:
    return PathHop.model_construct(
        symbol_called="f",
        path=path,
        line_start=line,
        line_end=line,
        quote="x",
        note="",
        side="head",
    )


def entry(
    path: str = "bin/run.py", line: int = 10, handler: tuple[str, int] | None = None
) -> PathEntry:
    return PathEntry.model_construct(
        kind="argparse",
        name="run",
        location=Location.model_construct(path=path, line=line),
        handler=Handler.model_construct(path=handler[0], line=handler[1], symbol="h")
        if handler
        else None,
        invocation=None,
        invoked_by="a CLI command",
        also_invoked_by=[],
    )


def path(n: int, hops: list[PathHop], ent: PathEntry | None = None) -> TracedPath:
    return TracedPath.model_construct(
        id=f"p{n}",
        entry=ent or entry(),
        hops=hops,
        stops_at=None,
        break_reason=None,
        changed_lines_covered=[],
        tests=E2EListing(e2e=[], mocks_hop_2=[]),
    )


def result(*paths: TracedPath, dropped: list[PathDrop] | None = None) -> PathsResult:
    return PathsResult.model_construct(
        schema_version="paths.v1",
        commit="a" * 40,
        scoped=True,
        cap=21,
        fallback=FallbackStatus(status="ok", reason=None),
        paths=list(paths),
        dropped=dropped or [],
        diagnostics=[],
        test_listing_skips=[],
    )


def finding(*cites: tuple[str, int, int], claim: str = "x is wrong") -> Finding:
    citations = [
        Citation(path=p, line_start=a, line_end=b, quote="q") for p, a, b in cites
    ]
    return Finding(
        claim=claim,
        severity="MEDIUM",
        file=cites[0][0],
        line_start=cites[0][1],
        line_end=cites[0][2],
        failure_scenario="it breaks",
        citations=citations,
    )


def ids(findings: list[Finding]) -> list[list[str] | None]:
    return [f.paths for f in findings]


# --- matching -------------------------------------------------------------------------------------------------


def test_a_citation_range_containing_a_hop_line_puts_the_finding_on_the_path():
    paths = result(path(3, [hop("src/cli.py", 40)]))
    assert ids(assign([finding(("src/cli.py", 35, 45))], paths)) == [["p3"]]


def test_a_citation_range_containing_the_entry_or_handler_line_puts_the_finding_on_the_path():
    paths = result(
        path(
            1,
            [hop("lib/x.py", 99)],
            entry("bin/run.py", 10, handler=("bin/handler.py", 4)),
        )
    )
    assert ids(
        assign(
            [finding(("bin/run.py", 10, 10)), finding(("bin/handler.py", 1, 5))], paths
        )
    ) == [["p1"], ["p1"]]


def test_range_boundaries_are_inclusive_and_a_line_outside_or_another_file_does_not_count():
    paths = result(path(1, [hop("a.py", 20), hop("a.py", 50), hop("a.py", 30)]))
    got = assign(
        [
            finding(("a.py", 20, 25)),
            finding(("a.py", 15, 20), claim="y"),
            finding(("a.py", 51, 60), claim="z"),
            finding(("b.py", 1, 100), claim="w"),
        ],
        paths,
    )
    assert ids(got) == [["p1"], ["p1"], None, None]


def test_a_line_just_outside_the_range_does_not_count():
    assert ids(
        assign([finding(("a.py", 10, 49))], result(path(1, [hop("a.py", 50)])))
    ) == [None]


def test_path_spellings_are_normalised_on_both_sides():
    paths = result(
        path(1, [hop("./pkg/mod.py", 8)]), path(2, [hop("lib//util.py/", 3)])
    )
    got = assign(
        [finding(("pkg/mod.py", 5, 9)), finding(("./lib/util.py", 3, 3), claim="y")],
        paths,
    )
    assert ids(got) == [["p1"], ["p2"]]


def test_a_review_deps_citation_never_places_a_finding_but_another_citation_still_does():
    paths = result(
        path(1, [hop(".review-deps/lib/x.py", 12)]), path(2, [hop("src/y.py", 4)])
    )
    only_deps = finding(("src/z.py", 1, 1), (".review-deps/lib/x.py", 10, 15))
    both = finding(("src/y.py", 3, 6), (".review-deps/lib/x.py", 10, 15), claim="y")
    assert ids(assign([only_deps, both], paths)) == [None, ["p2"]]


def test_ids_are_listed_once_in_paths_json_order():
    paths = result(
        path(1, [hop("a.py", 5)]),
        path(2, [hop("b.py", 1), hop("b.py", 2)]),
        path(3, [hop("c.py", 9)]),
    )
    twice = finding(("b.py", 1, 1), ("b.py", 2, 2))
    several = finding(("c.py", 9, 9), ("a.py", 1, 10), claim="y")
    assert ids(assign([twice, several], paths)) == [["p2"], ["p1", "p3"]]


def test_a_hop_without_a_line_is_skipped_and_the_others_still_match():
    paths = result(
        path(4, [hop("c.py", None), hop("", 3), hop("c.py", 0), hop("c.py", 7)])
    )
    assert ids(assign([finding(("c.py", 1, 10))], paths)) == [["p4"]]
    anchors = new("paths_io", "anchors")
    (view,) = new("paths_io", "views")(paths)
    assert anchors(view) == [("bin/run.py", 10), ("c.py", 7)]


def test_a_dropped_entry_is_never_assigned():
    dropped = PathDrop.model_construct(
        stage="cap", reason="over the cap", detail="", entry=entry("d.py", 5)
    )
    paths = result(path(1, [hop("a.py", 1)], entry("a.py", 1)), dropped=[dropped])
    assert ids(assign([finding(("d.py", 5, 5))], paths)) == [None]


def test_assignment_changes_nothing_but_paths_and_keeps_the_order():
    paths = result(path(1, [hop("a.py", 2)]))
    before = [
        finding(("b.py", 1, 1), claim="first"),
        finding(("a.py", 1, 3), claim="second"),
    ]
    after = assign(before, paths)
    assert [f.id for f in after] == [f.id for f in before]
    assert [f.model_dump(exclude={"paths"}) for f in after] == [
        f.model_dump(exclude={"paths"}) for f in before
    ]
    assert ids(after) == [None, ["p1"]]


def test_no_paths_result_leaves_the_findings_as_they_were():
    before = [finding(("a.py", 1, 3))]
    assert assign(before, None) == before and before[0].paths is None


def test_assignment_is_deterministic():
    paths = result(path(1, [hop("a.py", 2)]), path(2, [hop("a.py", 3)]))
    findings = [finding(("a.py", 1, 3))]
    assert (
        ids(assign(findings, paths))
        == ids(assign(assign(findings, paths), paths))
        == [["p1", "p2"]]
    )


# --- findings.json and its schema ---------------------------------------------------------------------------------


def schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def example_with(paths) -> dict:
    data = json.loads(VIEWER_EXAMPLE.read_text(encoding="utf-8"))
    data["findings"][0]["paths"] = paths
    return data


def test_the_findings_schema_takes_an_optional_list_of_unique_non_empty_ids():
    jsonschema.validate(
        json.loads(VIEWER_EXAMPLE.read_text(encoding="utf-8")), schema()
    )  # written before paths
    jsonschema.validate(example_with(["p01", "p03"]), schema())
    for bad in ("p01", ["p01", "p01"], [""], None):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(example_with(bad), schema())
    finding_schema = schema()["$defs"]["Finding"]
    assert (
        "paths" not in finding_schema.get("required", [])
        and "kind" not in finding_schema["properties"]
    )


# --- through a review -------------------------------------------------------------------------------------------


def exported(target_dir: Path) -> list[dict]:
    return json.loads((target_dir / "findings.json").read_text())["findings"]


def test_a_change_reviews_findings_carry_the_ids_of_the_paths_they_lie_on(
    tmp_path,
    model,  # noqa: F811
    mapped,  # noqa: F811
):
    """`traced_path` hops at line 1 of p01..p03/m.py and the entry at p01/m.py:1; the piece findings cite line 2."""
    repo, base, head = make_repo(tmp_path, 3)
    mapped(2)
    model(
        path_reports={"p1": "p01/m.py:1-2 Kind: defect\nx"},
        path_items={
            "p1": [
                {
                    **path_item("p1"),
                    "line_start": 1,
                    "line_end": 2,
                    "citations": [
                        {
                            "path": "p01/m.py",
                            "line_start": 1,
                            "line_end": 2,
                            "quote": "p01_1 = 1\np01_2 = 2",
                        }
                    ],
                }
            ]
        },
    )
    assert review(repo, base, head, tmp_path / "out") == 0
    target_dir, state = saved(tmp_path / "out")
    rows = exported(target_dir)
    on = [r for r in rows if r["claim"].startswith("p1:")]
    assert [r["paths"] for r in on] == [
        ["p1", "p2"]
    ]  # p01/m.py:1 is p1's and p2's entry and first hop
    off = [r for r in rows if not r["claim"].startswith("p1:")]
    assert off and not any(
        "paths" in r for r in off
    )  # line 2 alone is on no path: no key, never []
    every = {p for r in rows for p in r.get("paths", [])}
    assert every <= set(
        json.loads((target_dir / "paths.json").read_text())["paths"][i]["id"]
        for i in (0, 1)
    )
    jsonschema.validate(
        json.loads((target_dir / "findings.json").read_text()), schema()
    )


@pytest.mark.parametrize("how", ["failed", "empty"])
def test_without_paths_no_finding_has_a_paths_key_and_no_error_is_added(
    tmp_path,
    model,  # noqa: F811
    mapped,  # noqa: F811
    how,
):
    repo, base, head = make_repo(tmp_path, 2)
    if how == "failed":
        mapped(raises=RuntimeError("index exploded"))
    else:
        mapped(0)
    model()
    assert review(repo, base, head, tmp_path / "out") == 0
    target_dir, state = saved(tmp_path / "out")
    assert exported(target_dir) and not any("paths" in r for r in exported(target_dir))
    # The paths stage's own line for a failed or empty mapping, and the fake assess's: nothing for assignment.
    assert [e for e in state.errors if not e.startswith(("paths ", "assess:"))] == []


def test_a_tree_review_without_paths_writes_no_paths_key(tmp_path, model, mapped):  # noqa: F811
    repo, _, _ = make_repo(tmp_path, 1)
    mapped(2)
    model()
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("concurrency: 1\n")
    assert (
        main(
            [
                "--session-id",
                "sess-test",
                "--out",
                str(out),
                "review",
                "--repo",
                str(repo),
                "--tree",
                "--trust-repo",
            ]
        )
        == 0
    )
    target_dir, _ = saved(out)
    assert not any("paths" in r for r in exported(target_dir))


def test_a_resumed_review_assigns_the_same_ids_as_one_that_never_stopped(
    tmp_path,
    model,  # noqa: F811
    mapped,  # noqa: F811
):
    repo, base, head = make_repo(tmp_path, 2)
    mapped(2)
    answers = dict(
        path_reports={"p2": "p01/m.py:1 Kind: defect\nx"},
        path_items={
            "p2": [
                {
                    **path_item("p2"),
                    "line_start": 1,
                    "line_end": 1,
                    "citations": [
                        {
                            "path": "p01/m.py",
                            "line_start": 1,
                            "line_end": 1,
                            "quote": "p01_1 = 1",
                        }
                    ],
                }
            ]
        },
    )
    model(**answers)
    assert review(repo, base, head, tmp_path / "whole") == 0
    whole, _ = saved(tmp_path / "whole")
    model(stop_report=2, **answers)
    assert review(repo, base, head, tmp_path / "out") == 4
    target_dir, _ = saved(tmp_path / "out")
    assert resume(target_dir) == 0
    assert sorted(
        ((r["id"], r.get("paths")) for r in exported(target_dir)), key=str
    ) == sorted(((r["id"], r.get("paths")) for r in exported(whole)), key=str)
    assert any(r.get("paths") == ["p1", "p2"] for r in exported(whole))


def test_traced_path_puts_the_entry_and_first_hop_at_line_one():
    """The end-to-end tests above rely on this shape of `test_reported_finders.traced_path`."""
    p = traced_path(1)
    assert (p.entry.location.path, p.entry.location.line) == ("p01/m.py", 1)
    assert [(h.path, h.line_start) for h in p.hops][0] == ("p01/m.py", 1)
