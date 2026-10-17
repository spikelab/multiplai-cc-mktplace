"""A stand-in for `code_facts.map_paths`, for every test that is not about the paths stage.

Every change review maps its paths before find (`stages/paths.py`), and the
real `map_paths` asks a model. The tests written before that stage fake the
model calls by budget label and know nothing of `paths`, so `conftest.py`
installs this stub for each of them, and the subprocess drivers in
`test_data_dir.py` install it themselves. `test_paths_stage.py` replaces it with
its own fakes.

The stub keeps the real function's contract: keyword-only arguments, a real
`PathsResult` back (one path, so the stage records no error), no model call,
and the answer cache directory created under `cache_dir`, as the real one
creates it before its first question.
"""

from __future__ import annotations

import importlib

import code_facts
import code_facts.paths as code_facts_paths
from code_facts.paths import (E2EListing, FallbackStatus, Location, PathEntry, PathHop, PathsResult,
                              TracedPath)

ANSWERS_DIR = "paths-answers"


def stub_result(*, commit: str, scoped: bool, cap: int | None) -> PathsResult:
    entry = PathEntry(kind="main", name="main", location=Location(path="main.py", line=1), handler=None,
                      invocation=None, invoked_by="python main.py", also_invoked_by=[])
    hop = PathHop(symbol_called="main", path="main.py", line_start=1, line_end=1, quote="main()")
    path = TracedPath(id="p1", entry=entry, hops=[hop], stops_at=None, break_reason=None,
                      changed_lines_covered=[Location(path="main.py", line=1)] if scoped else [],
                      tests=E2EListing(e2e=[], mocks_hop_2=[]))
    return PathsResult(schema_version="paths.v1", commit=commit, scoped=scoped, cap=cap if scoped else None,
                       fallback=FallbackStatus(status="ok", reason=None), paths=[path], dropped=[],
                       diagnostics=[], test_listing_skips=[])


async def stub_map_paths(*, root, commit, resolve, call, changed_lines=None, cap=10, cache_dir=None, **kwargs):
    if cache_dir is not None:
        (cache_dir / ANSWERS_DIR).mkdir(parents=True, exist_ok=True)
    return stub_result(commit=commit, scoped=changed_lines is not None, cap=cap)


def install(patch=None) -> None:
    """Put the stub wherever the paths stage may look `map_paths` up.

    *patch* is `monkeypatch.setattr` in a test, so the stub is undone after
    it; the drivers pass none and patch for the life of their process.
    """
    def plain(owner, name, value, raising=True):
        setattr(owner, name, value)

    put = patch or plain
    put(code_facts, "map_paths", stub_map_paths)
    put(code_facts_paths, "map_paths", stub_map_paths)
    try:
        stage = importlib.import_module("review_pipeline.stages.paths")
    except ImportError:  # before the paths stage exists
        return
    put(stage, "map_paths", stub_map_paths, raising=False)
