"""The paths stage: `map_paths` over a review's changed lines, `paths.json` beside `findings.json`, before find.

Real tmp git repositories; `code_facts.map_paths` and the model calls are faked.
The fakes keep `map_paths`' contract: keyword-only arguments, a real
`PathsResult` back, the answer cache under `<cache_dir>/paths-answers/`, and a
call that raises one of `recoverable` handled as a drop rather than raised.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import code_facts
import code_facts.paths as code_facts_paths
import jsonschema
import pytest
from code_facts.paths import (
    E2EListing,
    FallbackStatus,
    Location,
    PathDrop,
    PathEntry,
    PathHop,
    PathsResult,
    TracedPath,
    write_paths_file,
)
from pydantic import BaseModel
from review_pipeline import budget, sdk, target
from review_pipeline.__main__ import main
from review_pipeline.config import load_config
from review_pipeline.export import write_checks_file
from review_pipeline.models import (
    STAGES,
    AssessOutput,
    FinderOutput,
    MergeOutput,
    RepeatsOutput,
    ReviewState,
    TargetInfo,
)
from review_pipeline.stages import RunContext

ENV = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
PATHS_SCHEMA = Path(code_facts.__file__).parent / "schema" / "paths.v1.schema.json"
CHECKS_SCHEMA = Path(__file__).resolve().parents[3] / "review-viewer" / "schema" / "checks.v1.schema.json"
SKILL_MD = Path(__file__).resolve().parents[2] / "SKILL.md"
NEW_FIELDS = ("paths_enabled", "paths_status", "paths_failure", "path_ids", "coverage")


def new(module: str, name: str):
    """A name this block adds, looked up per test so its absence fails that test rather than collection."""
    return getattr(importlib.import_module(f"review_pipeline.{module}"), name)


async def run_paths(state, ctx):
    return await new("stages.paths", "run_paths")(state, ctx)


# --- repositories ------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


def lines(prefix: str, n: int) -> str:
    return "".join(f"{prefix}_{i} = {i}\n" for i in range(1, n + 1))


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


@pytest.fixture
def change_repo(tmp_path) -> tuple[Path, str, str]:
    """base -> head: a.py gains lines 10-12, b.py line 40 changes, c.py loses line 3, old.py is deleted,
    x/old.py is renamed to x/new.py with line 5 changed."""
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, {"a.py": lines("a", 9), "b.py": lines("b", 50), "c.py": lines("c", 5), "old.py": lines("o", 3),
                 "x/old.py": lines("x", 20)})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    write(repo, {"a.py": lines("a", 12),
                 "b.py": lines("b", 50).replace("b_40 = 40\n", "b_40 = 4000\n"),
                 "c.py": lines("c", 5).replace("c_3 = 3\n", "")})
    (repo / "old.py").unlink()
    git(repo, "mv", "x/old.py", "x/new.py")
    write(repo, {"x/new.py": lines("x", 20).replace("x_5 = 5\n", "x_5 = 500\n")})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


HEAD_CHANGED = {"a.py": [10, 11, 12], "b.py": [40], "x/new.py": [5]}
TREE_FILES = {"pkg/a.py": lines("a", 30), "pkg/b.py": lines("b", 12).rstrip("\n"),
              "uv.lock": "version = 1\n"}


@pytest.fixture
def tree_repo(tmp_path) -> Path:
    repo = tmp_path / "treeproj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, TREE_FILES)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tree")
    return repo


def _ctx(info: TargetInfo, diff: str, tmp_path: Path) -> tuple[RunContext, Path]:
    target_dir = tmp_path / "out" / info.slug
    info = target.write_target_files(info, diff, target_dir)
    snapshot = target.snapshot_head(info, target_dir / "tree")
    ctx = RunContext(config=load_config(None), snapshot=snapshot, diff=diff, target_dir=target_dir)
    return ctx, info


@pytest.fixture
def trusted(monkeypatch):
    """A real run has trust before any stage starts (`review --trust-repo`); the stop tests take it away."""
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")


@pytest.fixture
def change_review(change_repo, tmp_path, trusted):
    """(state at the start of a change review, its RunContext) for `run_paths` called directly."""
    repo, base, head = change_repo
    resolved = target.resolve(repo, range_=f"{base}..{head}")
    ctx, info = _ctx(target.build_target(resolved), target.diff_text(resolved.repo, base, head), tmp_path)
    return ReviewState(target=info), ctx


def tree_review(repo: Path, tmp_path: Path, *, paths_enabled: bool):
    """Like `change_review`, for a `--tree` review; the tests that call it take `trusted`."""
    ctx, info = _ctx(target.build_target(target.resolve(repo, tree="HEAD")), "", tmp_path)
    return ReviewState(target=info, paths_enabled=paths_enabled), ctx


# --- PathsResult built through the real code_facts models ---------------------------------------------


def entry(name: str, line: int = 1) -> PathEntry:
    return PathEntry(kind="argparse", name=name, location=Location(path="a.py", line=line), handler=None,
                     invocation=None, invoked_by="a CLI command", also_invoked_by=[])


def traced(n: int, *, stops_at: int | None = None) -> TracedPath:
    hops = [PathHop(symbol_called=f"step{k}", path="a.py", line_start=k, line_end=k, quote=f"a_{k} = {k}")
            for k in (1, 2, 3)]
    return TracedPath(id=f"p{n}", entry=entry(f"cmd{n}"), hops=hops, stops_at=stops_at,
                      break_reason="text_mismatch" if stops_at else None,
                      changed_lines_covered=[Location(path="a.py", line=10)],
                      tests=E2EListing(e2e=[], mocks_hop_2=[]))


def drop(stage: str, reason: str, name: str, detail: str = "left out") -> PathDrop:
    return PathDrop(stage=stage, reason=reason, detail=detail, entry=entry(name, line=2))


def result(commit: str, paths=(), dropped=(), fallback: FallbackStatus | None = None) -> PathsResult:
    return PathsResult(schema_version="paths.v1", commit=commit, scoped=True, cap=21,
                       fallback=fallback or FallbackStatus(status="ok", reason=None), paths=list(paths),
                       dropped=list(dropped), diagnostics=[], test_listing_skips=[])


class Answer(BaseModel):
    value: int


async def ask(kw: dict, prompt: str):
    """One mapping question the way code_facts asks it: read-only tools, its own budget label."""
    return await kw["call"](prompt, Answer, allowed_tools=["Read", "Grep", "Glob"], cwd=str(kw["root"]),
                            budget_label="paths:trace")


class FakeMapPaths:
    """`map_paths`, keyword-only. Returns *produce(kwargs)*, or raises *raises*; with *questions*, asks
    each question not yet stored under `<cache_dir>/paths-answers/` and stores its answer as it returns."""

    def __init__(self, produce=None, *, raises: BaseException | None = None, questions: int = 0, events=None):
        self.produce = produce or (lambda kw: result(kw["commit"], [traced(1)]))
        self.raises, self.questions, self.events = raises, questions, events
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        if self.events is not None:
            self.events.append("map_paths")
        if self.raises is not None:
            raise self.raises
        answers = kw["cache_dir"] / "paths-answers"
        for q in range(self.questions):
            stored = answers / f"{q:064x}.json"
            if not stored.exists():
                value = (await ask(kw, f"question {q}")).value
                answers.mkdir(parents=True, exist_ok=True)
                stored.write_text(json.dumps({"value": value}))
        return self.produce(kw)


@pytest.fixture
def mapper(monkeypatch):
    """Install a fake `map_paths` wherever the stage may look it up; the real one is never called."""
    def install(fake):
        monkeypatch.setattr(code_facts, "map_paths", fake)
        monkeypatch.setattr(code_facts_paths, "map_paths", fake)
        try:
            stage = importlib.import_module("review_pipeline.stages.paths")
        except ImportError:
            return fake
        monkeypatch.setattr(stage, "map_paths", fake, raising=False)
        return fake
    return install


def changed(kw: dict) -> dict[str, list[int]]:
    return {path: sorted(v) for path, v in kw["changed_lines"].items()}


def paths_errors(state: ReviewState) -> list[str]:
    return [e for e in state.errors if e.startswith("paths")]


def validate_paths_json(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(doc, json.loads(PATHS_SCHEMA.read_text(encoding="utf-8")))
    return doc


# --- the stage list and the state ----------------------------------------------------------------------


def test_paths_is_the_stage_immediately_before_find():
    assert STAGES.index("paths") == STAGES.index("find") - 1


async def test_an_old_checkpoint_saved_at_find_loads_and_keeps_paths_not_run(change_review, mapper):
    state, ctx = change_review
    fake = mapper(FakeMapPaths())
    old = ReviewState(target=state.target, stage="find").model_dump(mode="json")
    for key in NEW_FIELDS:
        old.pop(key, None)
    loaded = ReviewState.model_validate(old)
    assert (loaded.paths_enabled, loaded.paths_status, loaded.paths_failure, loaded.path_ids, loaded.coverage) == (
        False, "not-run", None, [], [])
    after = await run_paths(loaded, ctx)
    assert fake.calls == [] and after.paths_status == "not-run"
    assert not (ctx.target_dir / "paths.json").exists()


async def test_an_old_checkpoint_saved_before_find_runs_the_paths_stage_on_resume(change_review, mapper):
    state, ctx = change_review
    fake = mapper(FakeMapPaths())
    old = ReviewState(target=state.target, stage="explain").model_dump(mode="json")
    for key in NEW_FIELDS:
        old.pop(key, None)
    after = await run_paths(ReviewState.model_validate(old), ctx)
    assert len(fake.calls) == 1 and (after.paths_status, after.path_ids) == ("ok", ["p1"])
    assert after.past("paths") and not after.past("find")
    validate_paths_json(ctx.target_dir / "paths.json")


# --- head-side changed lines -----------------------------------------------------------------------------


def test_head_changed_lines_are_head_side_added_lines_under_the_head_path(change_review):
    _, ctx = change_review
    assert new("chunks", "head_changed_lines")(ctx.diff) == HEAD_CHANGED


def test_head_changed_lines_count_hunk_lines_not_header_lookalikes_or_form_feeds():
    diff = (
        "diff --git a/m.py b/m.py\nindex 1..2 100644\n--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,4 @@\n"
        " one\n-two\n+++ b/evil\n+form\x0cfeed\n three\n\\ No newline at end of file\n"
        "diff --git a/img.png b/img.png\nnew file mode 100644\nBinary files /dev/null and b/img.png differ\n"
        "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+x\n+y\n"
    )
    head_changed_lines = new("chunks", "head_changed_lines")
    assert head_changed_lines(diff) == {"m.py": [2, 3], "n.py": [1, 2]}
    assert head_changed_lines("") == {}


# --- the call to map_paths -----------------------------------------------------------------------------------


async def test_a_change_review_calls_map_paths_once_with_head_lines_and_the_fixed_limits(change_review, mapper):
    state, ctx = change_review
    fake = mapper(FakeMapPaths())
    state = await run_paths(state, ctx)
    (kw,) = fake.calls
    assert kw["root"] == ctx.snapshot and kw["commit"] == state.target.head_sha
    assert changed(kw) == HEAD_CHANGED
    assert (kw["cap"], kw["trace_limit"], kw["cache_dir"]) == (21, 30, ctx.target_dir / "paths-cache")
    # The explain stage's resolver: head reads at the reviewed commit.
    assert kw["resolve"]("a.py", "head") == (state.target.repo_path, state.target.head_sha, "a.py")
    assert state.past("paths")


async def test_extra_finders_do_not_turn_the_paths_stage_off(change_review, mapper):
    state, ctx = change_review
    fake = mapper(FakeMapPaths())
    state.extra_finders = True
    await run_paths(state, ctx)
    assert len(fake.calls) == 1


async def test_mapping_calls_are_charged_to_the_ledger_under_paths(change_review, mapper, monkeypatch):
    state, ctx = change_review
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")

    async def run_agent(prompt, **kwargs):
        return SimpleNamespace(text='{"value": 1}', tool_calls=(), turns=1,
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cost_usd=1.60))

    monkeypatch.setattr(sdk, "run_agent", run_agent)

    async def two_questions(kw):
        return result(kw["commit"], [traced(1)])

    class Asking(FakeMapPaths):
        async def __call__(self, **kw):
            self.calls.append(kw)
            await ask(kw, "first")
            await ask(kw, "second")
            return await two_questions(kw)

    mapper(Asking())
    ledger = budget.get_budget()
    await run_paths(state, ctx)
    assert ledger.cost_usd == pytest.approx(3.20)
    assert ledger.by_label == {"paths": pytest.approx(3.20)}


# --- paths.json -------------------------------------------------------------------------------------------


async def test_a_result_is_written_as_schema_valid_paths_json_and_its_ids_kept(change_review, mapper):
    state, ctx = change_review
    mapper(FakeMapPaths(lambda kw: result(kw["commit"], [traced(n) for n in range(1, 5)])))
    state = await run_paths(state, ctx)
    doc = validate_paths_json(ctx.target_dir / "paths.json")
    assert [p["id"] for p in doc["paths"]] == ["p1", "p2", "p3", "p4"]
    assert (state.paths_status, state.path_ids) == ("ok", ["p1", "p2", "p3", "p4"])
    assert state.errors == [] and state.coverage == []


async def test_an_empty_result_is_still_written_and_recorded_as_mapping_empty(change_review, mapper):
    state, ctx = change_review
    mapper(FakeMapPaths(lambda kw: result(kw["commit"])))
    state = await run_paths(state, ctx)
    assert validate_paths_json(ctx.target_dir / "paths.json")["paths"] == []
    assert (state.paths_status, state.path_ids) == ("empty", [])
    assert [(c.stage, c.level, c.reason) for c in state.coverage] == [("paths", "error", "mapping-empty")]
    assert state.errors == ["paths mapping-empty: no paths were mapped"]


# --- drops, broken paths and failures ----------------------------------------------------------------------


async def test_scope_drops_are_info_coverage_and_never_reach_errors(change_review, mapper):
    state, ctx = change_review
    scope = [drop("import_reachability", "unreachable", "unreached"),
             drop("trace_limit", "over_trace_limit", "too_many"),
             drop("no_changed_line", "no_changed_line_covered", "untouched"),
             drop("cap", "over_cap", "capped")]
    mapper(FakeMapPaths(lambda kw: result(kw["commit"], [traced(1)], scope)))
    state = await run_paths(state, ctx)
    assert sorted((c.level, c.reason) for c in state.coverage) == [
        ("info", "out-of-scope"), ("info", "out-of-scope"), ("info", "over-cap"), ("info", "over-cap")]
    assert state.errors == []
    assert (ctx.target_dir / "paths.json").exists()


async def test_a_trace_drop_is_recorded_as_an_error_and_the_review_continues(change_review, mapper):
    state, ctx = change_review
    mapper(FakeMapPaths(lambda kw: result(kw["commit"], [traced(1)],
                                          [drop("trace", "call_failed", "review_cmd", "the model call failed")])))
    state = await run_paths(state, ctx)
    (record,) = state.coverage
    assert (record.stage, record.level, record.reason) == ("paths", "error", "trace-failed")
    assert "review_cmd" in record.subject
    (line,) = state.errors
    assert line.startswith("paths trace ") and "review_cmd" in line and line.endswith(": the model call failed")
    assert state.past("paths") and state.path_ids == ["p1"]


async def test_a_broken_path_is_kept_and_recorded_with_the_hop_it_stops_at(change_review, mapper):
    state, ctx = change_review
    mapper(FakeMapPaths(lambda kw: result(kw["commit"], [traced(1), traced(2, stops_at=2)])))
    state = await run_paths(state, ctx)
    assert [(c.subject, c.level, c.reason) for c in state.coverage] == [("p2", "error", "broken-trace")]
    assert state.errors == ["paths broken p2: stops at hop 2"]
    assert [p["id"] for p in validate_paths_json(ctx.target_dir / "paths.json")["paths"]] == ["p1", "p2"]


async def test_a_failed_model_fallback_is_recorded_as_an_error(change_review, mapper):
    state, ctx = change_review
    failed = FallbackStatus(status="failed", reason="the fallback call failed")
    mapper(FakeMapPaths(lambda kw: result(kw["commit"], [traced(1)], fallback=failed)))
    state = await run_paths(state, ctx)
    assert [(c.level, c.reason) for c in state.coverage] == [("error", "fallback-failed")]
    assert state.errors == ["paths fallback-failed: the fallback call failed"]


async def test_a_mapping_exception_is_recorded_and_the_stage_still_completes(change_review, mapper):
    state, ctx = change_review
    mapper(FakeMapPaths(raises=RuntimeError("index exploded")))
    state = await run_paths(state, ctx)
    assert (state.paths_status, state.paths_failure, state.path_ids) == ("failed", "index exploded", [])
    assert [(c.stage, c.level, c.reason) for c in state.coverage] == [("paths", "error", "mapping-failed")]
    assert state.errors == ["paths mapping-failed: index exploded"]
    assert not (ctx.target_dir / "paths.json").exists()
    assert state.past("paths")


def _trust_stop(monkeypatch):
    monkeypatch.delenv("REVIEW_TRUST_REPO", raising=False)
    return sdk.RepoTrustError


def _budget_stop(monkeypatch):
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")
    budget.start(1.0).record(SimpleNamespace(input_tokens=0, output_tokens=0, cost_usd=1.0), label="find")
    return budget.BudgetExceededError


def _limit_stop(monkeypatch):
    """multiplai-core reports a hit usage limit from the mapping call's run_agent."""
    from multiplai_core import UsageLimitError

    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")

    async def run_agent(prompt, **kwargs):
        raise UsageLimitError("You've hit your session limit", limit="session")

    monkeypatch.setattr(sdk, "run_agent", run_agent)
    return sdk.UsageLimitError


@pytest.mark.parametrize("stop", [_trust_stop, _budget_stop, _limit_stop], ids=["trust", "budget", "usage-limit"])
@pytest.mark.parametrize("route", ["raised", "from-a-call"])
async def test_trust_budget_and_usage_limit_stops_propagate_and_record_nothing(change_review, mapper, monkeypatch, stop,
                                                                   route):
    """Raised by map_paths itself, or by a mapping call: map_paths catches only `recoverable`, so neither stop
    may be in it (RepoTrustError is an AgentCallError)."""
    state, ctx = change_review
    error = stop(monkeypatch)

    class Recovering(FakeMapPaths):
        async def __call__(self, **kw):
            self.calls.append(kw)
            try:
                await ask(kw, "trace")
            except tuple(kw.get("recoverable", ())):
                return result(kw["commit"], [], [drop("trace", "call_failed", "cmd")])
            return result(kw["commit"], [traced(1)])

    mapper(Recovering() if route == "from-a-call" else FakeMapPaths(raises=error("stop")))
    with pytest.raises(error):
        await run_paths(state, ctx)
    assert (state.paths_status, state.coverage, paths_errors(state)) == ("not-run", [], [])
    assert not (ctx.target_dir / "paths.json").exists()
    assert not state.past("paths")


# --- tree and dir reviews ------------------------------------------------------------------------------------


async def test_a_tree_review_without_paths_skips_the_stage_silently(tree_repo, tmp_path, mapper, trusted):
    state, ctx = tree_review(tree_repo, tmp_path, paths_enabled=False)
    fake = mapper(FakeMapPaths())
    state = await run_paths(state, ctx)
    assert fake.calls == [] and state.paths_status == "skipped" and state.past("paths")
    assert (state.errors, state.coverage, state.path_ids) == ([], [], [])
    assert not (ctx.target_dir / "paths.json").exists() and not (ctx.target_dir / "paths-cache").exists()
    assert budget.get_budget().cost_usd == 0


async def test_a_tree_review_with_paths_passes_every_line_of_every_reviewed_file(tree_repo, tmp_path, mapper, trusted):
    state, ctx = tree_review(tree_repo, tmp_path, paths_enabled=True)
    fake = mapper(FakeMapPaths())
    state = await run_paths(state, ctx)
    (kw,) = fake.calls
    # uv.lock is a skipped tree file, so none of its lines are passed.
    assert changed(kw) == {"pkg/a.py": list(range(1, 31)), "pkg/b.py": list(range(1, 13))}
    assert kw["cap"] == 21 and state.paths_status == "ok"
    validate_paths_json(ctx.target_dir / "paths.json")


# --- resume -------------------------------------------------------------------------------------------------


async def test_a_completed_paths_stage_is_never_entered_again(change_review, mapper):
    state, ctx = change_review
    mapper(FakeMapPaths())
    state = await run_paths(state, ctx)
    written = (ctx.target_dir / "paths.json").read_bytes()
    again = mapper(FakeMapPaths(raises=AssertionError("asked again")))
    state = await run_paths(state, ctx)
    assert again.calls == [] and (ctx.target_dir / "paths.json").read_bytes() == written


# --- the coverage helpers, paths_io and stale outputs ---------------------------------------------------------


@pytest.mark.parametrize("stage, reason, level, coverage_reason", [
    ("import_reachability", "unreachable", "info", "out-of-scope"),
    ("no_changed_line", "no_changed_line_covered", "info", "out-of-scope"),
    ("trace_limit", "over_trace_limit", "info", "over-cap"),
    ("cap", "over_cap", "info", "over-cap"),
    ("trace", "call_failed", "error", "trace-failed"),
])
def test_classify_drop_maps_each_drop_stage_to_a_level_and_reason(stage, reason, level, coverage_reason):
    record = new("paths_io", "classify_drop")(drop(stage, reason, "review_cmd"))
    assert (record.stage, record.level, record.reason) == ("paths", level, coverage_reason)
    assert "review_cmd" in record.subject


def _state() -> ReviewState:
    return ReviewState(target=TargetInfo(repo_path="/r", base_sha="a" * 40, head_sha="b" * 40, slug="s"))


def test_add_coverage_writes_an_errors_line_for_error_records_only():
    record_cls, add = new("models", "CoverageRecord"), new("checks", "add_coverage")
    state = _state()
    add(state, record_cls(stage="paths", subject="entry capped", level="info", reason="over-cap"))
    add(state, record_cls(stage="paths", subject="p3", level="error", reason="duplicate-id"))
    add(state, new("paths_io", "classify_broken")(traced(4, stops_at=3)))
    assert [c.reason for c in state.coverage] == ["over-cap", "duplicate-id", "broken-trace"]
    assert state.errors == ["paths duplicate-id p3", "paths broken p4: stops at hop 3"]


@pytest.mark.parametrize("status, loaded", [("ok", True), ("empty", True), ("failed", False),
                                            ("skipped", False), ("not-run", False)])
def test_load_paths_reads_paths_json_only_when_the_stage_produced_it(tmp_path, status, loaded):
    written = result("b" * 40, [traced(1), traced(2)])
    write_paths_file(written, tmp_path / "paths.json")  # a stale file when the status says no result
    state = _state().model_copy(update={"paths_status": status})
    got = new("paths_io", "load_paths")(tmp_path, state)
    assert got == (written if loaded else None)


def test_clear_stale_outputs_removes_only_the_last_runs_reports_and_paths_files(tmp_path):
    stale = ["report-piece-1.md", "report-piece-12.md", "report-path-p3.md", "paths.json",
             "paths-cache/paths-answers/k.json"]
    kept = ["findings.json", "review-state.json", "notes-report-piece-1.md", "report-piece-1.txt",
            "rounds/01-abc/paths.json", "rounds/01-abc/report-path-p1.md"]
    write(tmp_path, {rel: "x" for rel in stale + kept})
    new("rounds", "clear_stale_outputs")(tmp_path)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()) == sorted(kept)
    assert not (tmp_path / "paths-cache").exists()


def test_checks_json_exports_coverage_as_a_top_level_list(tmp_path, canned_state):
    record_cls = new("models", "CoverageRecord")
    canned_state.coverage = [
        record_cls(stage="paths", subject="entry capped", level="info", reason="over-cap", detail="cap 21"),
        record_cls(stage="paths", subject="p2", level="error", reason="broken-trace", detail="stops at hop 2")]
    data = json.loads(write_checks_file(canned_state, tmp_path).read_text(encoding="utf-8"))
    assert data["coverage"] == [
        {"stage": "paths", "subject": "entry capped", "level": "info", "reason": "over-cap", "detail": "cap 21"},
        {"stage": "paths", "subject": "p2", "level": "error", "reason": "broken-trace", "detail": "stops at hop 2"}]
    jsonschema.validate(data, json.loads(CHECKS_SCHEMA.read_text(encoding="utf-8")))


def test_skill_md_documents_the_paths_stage_and_the_paths_flag():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "--paths" in text and "paths.json" in text


# --- whole runs through the CLI ---------------------------------------------------------------------------------


class Agents:
    """Every other stage's agent, by budget label; `paths` answers the fake mapper's questions and raises a
    budget stop on the *stop_on_paths_call*-th one."""

    def __init__(self, events: list[str], stop_on_paths_call: int | None = None):
        self.events, self.stop_on_paths_call, self.paths_calls = events, stop_on_paths_call, 0

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        self.events.append(budget_label)
        if budget_label == "paths":
            self.paths_calls += 1
            if self.paths_calls == self.stop_on_paths_call:
                self.stop_on_paths_call = None
                raise budget.BudgetExceededError("circuit breaker stopped the run during paths")
            return schema.model_validate({"value": self.paths_calls})
        stage = budget_label.split(":")[0]
        outputs = {"find": FinderOutput, "merge": MergeOutput, "assess": AssessOutput, "repeats": RepeatsOutput}
        if stage in outputs:
            return outputs[stage]()
        raise AssertionError(budget_label)


@pytest.fixture
def events(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(sdk, "agent_call_structured", Agents(seen))
    return seen


def cli(out: Path, *args: str) -> int:
    out.mkdir(exist_ok=True)
    (out / "review.yaml").write_text("concurrency: 1\n")
    return main(["--session-id", "sess-test", "--out", str(out), "review", "--trust-repo", *args])


def saved(out: Path) -> tuple[Path, ReviewState]:
    (path,) = [p for p in out.rglob("review-state.json") if "rounds" not in p.parts]
    return path.parent, ReviewState.model_validate_json(path.read_text())


def first_find(events: list[str]) -> int:
    return next(i for i, e in enumerate(events) if e.startswith("find"))


def test_a_change_review_maps_paths_before_find_and_writes_paths_json_beside_findings(change_repo, tmp_path,
                                                                                      events, mapper):
    repo, base, head = change_repo
    mapper(FakeMapPaths(events=events))
    assert cli(tmp_path / "out", "--repo", str(repo), "--range", f"{base}..{head}") == 0
    target_dir, state = saved(tmp_path / "out")
    assert events.count("map_paths") == 1 and events.index("map_paths") < first_find(events)
    assert state.timings["paths"][0].ended_at <= state.timings["find"][0].started_at
    assert (target_dir / "findings.json").exists() and validate_paths_json(target_dir / "paths.json")
    assert state.paths_status == "ok" and state.path_ids == ["p1"]


def test_a_mapping_failure_does_not_fail_the_review_and_find_still_runs(change_repo, tmp_path, events, mapper):
    repo, base, head = change_repo
    mapper(FakeMapPaths(raises=RuntimeError("index exploded")))
    assert cli(tmp_path / "out", "--repo", str(repo), "--range", f"{base}..{head}") == 0
    target_dir, state = saved(tmp_path / "out")
    assert any(e.startswith("find") for e in events)
    assert not (target_dir / "paths.json").exists()
    assert paths_errors(state) == ["paths mapping-failed: index exploded"]


def test_a_tree_review_without_paths_runs_no_mapping_and_writes_no_paths_json(tree_repo, tmp_path, events, mapper):
    fake = mapper(FakeMapPaths())
    assert cli(tmp_path / "out", "--repo", str(tree_repo), "--tree") == 0
    target_dir, state = saved(tmp_path / "out")
    assert fake.calls == [] and not (target_dir / "paths.json").exists()
    assert state.paths_status == "skipped" and paths_errors(state) == []


def test_a_dir_review_with_paths_maps_before_find_and_keeps_the_flag(tmp_path, events, mapper):
    src = tmp_path / "src"
    write(src, {"pkg/a.py": TREE_FILES["pkg/a.py"]})
    mapper(FakeMapPaths(events=events))
    assert cli(tmp_path / "out", "--dir", str(src), "--paths") == 0
    target_dir, state = saved(tmp_path / "out")
    assert state.paths_enabled is True
    assert events.index("map_paths") < first_find(events)
    assert (target_dir / "findings.json").exists() and validate_paths_json(target_dir / "paths.json")


def test_a_budget_stop_while_mapping_keeps_paid_answers_and_resume_asks_only_the_rest(change_repo, tmp_path,
                                                                                     monkeypatch, mapper):
    repo, base, head = change_repo
    seen: list[str] = []
    agents = Agents(seen, stop_on_paths_call=6)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    mapper(FakeMapPaths(lambda kw: result(kw["commit"], [traced(n) for n in range(1, 10)]), questions=9))
    assert cli(tmp_path / "out", "--repo", str(repo), "--range", f"{base}..{head}") == 4
    target_dir, state = saved(tmp_path / "out")
    answers = target_dir / "paths-cache" / "paths-answers"
    assert len(list(answers.iterdir())) == 5
    assert not any(e.startswith("find") for e in seen) and not (target_dir / "paths.json").exists()
    assert (state.coverage, paths_errors(state), state.past("paths")) == ([], [], False)

    before = agents.paths_calls
    assert main(["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo"]) == 0
    assert agents.paths_calls - before == 4
    assert len(list(answers.iterdir())) == 9
    _, done = saved(tmp_path / "out")
    assert done.path_ids == [f"p{n}" for n in range(1, 10)]
    assert len(validate_paths_json(target_dir / "paths.json")["paths"]) == 9


def test_a_kept_round_holds_paths_json_and_a_fresh_run_starts_without_the_old_cache(change_repo, tmp_path,
                                                                                    events, mapper):
    repo, base, head = change_repo
    out = tmp_path / "out"

    class Caching(FakeMapPaths):
        async def __call__(self, **kw):
            self.calls.append(sorted(p.name for p in kw["cache_dir"].rglob("*")) if kw["cache_dir"].exists()
                              else [])
            (kw["cache_dir"] / "paths-answers").mkdir(parents=True, exist_ok=True)
            (kw["cache_dir"] / "paths-answers" / f"{len(self.calls):064x}.json").write_text("{}")
            return result(kw["commit"], [traced(1), traced(2)])

    fake = mapper(Caching())
    assert cli(out, "--repo", str(repo), "--range", f"{base}..{head}") == 0
    target_dir, _ = saved(out)
    first = (target_dir / "paths.json").read_bytes()
    (target_dir / "report-path-p1.md").write_text("last round's report")
    assert cli(out, "--repo", str(repo), "--range", f"{base}..{head}") == 0
    (kept,) = (target_dir / "rounds").glob("*/paths.json")
    assert kept.read_bytes() == first
    assert fake.calls[1] == []  # the first run's answers were cleared before this run mapped
    # The last round's report was cleared; the file there now is this round's path finder's.
    assert (target_dir / "report-path-p1.md").read_text() != "last round's report"
    assert (kept.parent / "report-path-p1.md").read_text() == "last round's report"


def test_plan_only_neither_maps_nor_clears_the_last_rounds_paths_outputs(change_repo, tmp_path, events, mapper):
    repo, base, head = change_repo
    out = tmp_path / "out"
    mapper(FakeMapPaths(questions=0))
    assert cli(out, "--repo", str(repo), "--range", f"{base}..{head}") == 0
    target_dir, _ = saved(out)
    cache = target_dir / "paths-cache" / "paths-answers" / "k.json"
    write(target_dir, {"paths-cache/paths-answers/k.json": "{}"})
    written = (target_dir / "paths.json").read_bytes()
    again = mapper(FakeMapPaths(raises=AssertionError("plan-only mapped")))
    assert cli(out, "--repo", str(repo), "--range", f"{base}..{head}", "--plan-only") == 0
    assert again.calls == [] and cache.exists()
    assert (target_dir / "paths.json").read_bytes() == written
