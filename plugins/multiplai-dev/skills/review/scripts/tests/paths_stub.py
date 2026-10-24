"""Stand-ins for `code_facts.map_paths` and `sdk.agent_call_text`, for the tests not about them.

Every change review maps its paths before find (`stages/paths.py`), and the
real `map_paths` asks a model. The tests written before that stage fake the
model calls by budget label and know nothing of `paths`, so `conftest.py`
installs this stub for each of them, and the subprocess drivers in
`test_data_dir.py` install it themselves. `test_paths_stage.py` replaces it with
its own fakes.

Every change review also runs each dimension finder once per piece of its
diff, as a plain-text report (`sdk.agent_call_text`) and a convert call, and
the older fakes answer only the JSON call each dimension made before
(`sdk.agent_call_structured` with `FinderOutput`, by budget label).
`stub_agent_call_text` checks trust as the real call does, then asks that
fake for a dimension's report call and writes its answer into the report,
encoded; it reports `NO FINDINGS` for any other text call, and when the fake
finds nothing, so no convert call follows. The first time it encodes an
answer it wraps the installed `sdk.agent_call_structured` so the convert
call of that report decodes it into items (`with_reports`); the test's
monkeypatch undoes the wrapper with the fake. `test_reported_finders.py`
replaces the stub with its own fake.

The map_paths stub keeps the real function's contract: keyword-only
arguments, a real `PathsResult` back (one path, so the stage records no
error), no model call, and the answer cache directory created under
`cache_dir`, as the real one creates it before its first question.
"""

from __future__ import annotations

import base64
import importlib
import re

import code_facts
import code_facts.paths as code_facts_paths
from code_facts.paths import (
    E2EListing,
    FallbackStatus,
    Location,
    PathEntry,
    PathHop,
    PathsResult,
    TracedPath,
)
from review_pipeline import sdk
from review_pipeline.models import ConvertItem, ConvertOutput, FinderOutput

ANSWERS_DIR = "paths-answers"
# The real text call, saved before any test replaces it, for the tests of that call.
REAL_AGENT_CALL_TEXT = getattr(sdk, "agent_call_text", None)


def stub_result(*, commit: str, scoped: bool, cap: int | None) -> PathsResult:
    entry = PathEntry(
        kind="main",
        name="main",
        location=Location(path="main.py", line=1),
        handler=None,
        invocation=None,
        invoked_by="python main.py",
        also_invoked_by=[],
    )
    hop = PathHop(
        symbol_called="main", path="main.py", line_start=1, line_end=1, quote="main()"
    )
    path = TracedPath(
        id="p1",
        entry=entry,
        hops=[hop],
        stops_at=None,
        break_reason=None,
        changed_lines_covered=[Location(path="main.py", line=1)] if scoped else [],
        tests=E2EListing(e2e=[], mocks_hop_2=[]),
    )
    return PathsResult(
        schema_version="paths.v1",
        commit=commit,
        scoped=scoped,
        cap=cap if scoped else None,
        fallback=FallbackStatus(status="ok", reason=None),
        paths=[path],
        dropped=[],
        diagnostics=[],
        test_listing_skips=[],
    )


async def stub_map_paths(
    *, root, commit, resolve, call, changed_lines=None, cap=10, cache_dir=None, **kwargs
):
    if cache_dir is not None:
        (cache_dir / ANSWERS_DIR).mkdir(parents=True, exist_ok=True)
    return stub_result(commit=commit, scoped=changed_lines is not None, cap=cap)


REPORT_MARK = "ENCODED-FINDER-OUTPUT:"
_ENCODED = re.compile(re.escape(REPORT_MARK) + r"([A-Za-z0-9+/=]+)")
_DIMENSION_REPORT = re.compile(
    r"looking at one aspect: (\S+)\. Each reviewer of this aspect"
)


def _items(out: FinderOutput) -> ConvertOutput:
    return ConvertOutput(
        items=[
            ConvertItem(
                kind="defect",
                severity=f.severity,
                file=f.file,
                line_start=f.line_start,
                line_end=f.line_end,
                claim=f.claim,
                failure_scenario=f.failure_scenario,
                citations=f.citations,
                defect_class=f.defect_class,
                other_locations=f.other_locations,
                pre_existing=f.dimension == "pre-existing",
            )
            for f in out.findings
        ],
        needs=out.needs,
    )


def decoded(prompt: str) -> ConvertOutput:
    """The convert answer to a report `stub_agent_call_text` encoded; no items when the prompt holds none."""
    m = _ENCODED.search(prompt)
    if m is None:
        return ConvertOutput()
    return _items(FinderOutput.model_validate_json(base64.b64decode(m.group(1))))


def with_reports(fake):
    """*fake*, answering the convert call of a report `stub_agent_call_text` encoded, and delegating the rest."""
    if getattr(fake, "decodes_reports", False):
        return fake

    async def structured(prompt, schema, **kwargs):
        if schema is ConvertOutput and _ENCODED.search(prompt):
            return decoded(prompt)
        return await fake(prompt, schema, **kwargs)

    structured.decodes_reports = True
    return structured


async def stub_agent_call_text(prompt, *, budget_label="", **kwargs) -> str:
    sdk.require_trusted_repo()
    if not _DIMENSION_REPORT.search(prompt):
        return "NO FINDINGS"
    if kwargs.get("cwd") is not None:
        kwargs["cwd"] = str(kwargs["cwd"])  # as the JSON finder call was given it
    out = await sdk.agent_call_structured(
        prompt, FinderOutput, budget_label=budget_label, **kwargs
    )
    if not isinstance(out, FinderOutput) or not (out.findings or out.needs):
        return "NO FINDINGS"
    sdk.agent_call_structured = with_reports(sdk.agent_call_structured)
    return REPORT_MARK + base64.b64encode(out.model_dump_json().encode()).decode()


def install(patch=None) -> None:
    """Put the stubs wherever the stages look them up.

    *patch* is `monkeypatch.setattr` in a test, so the stub is undone after
    it; the drivers pass none and patch for the life of their process.
    """

    def plain(owner, name, value, raising=True):
        setattr(owner, name, value)

    put = patch or plain
    put(code_facts, "map_paths", stub_map_paths)
    put(code_facts_paths, "map_paths", stub_map_paths)
    put(sdk, "agent_call_text", stub_agent_call_text, raising=False)
    try:
        stage = importlib.import_module("review_pipeline.stages.paths")
    except ImportError:  # before the paths stage exists
        return
    put(stage, "map_paths", stub_map_paths, raising=False)
