"""The explain stage: the diff scope, the resolver, facts.json and the stage with canned answers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import jsonschema
import pytest

from code_facts import Citation, ExploreOutput, Fact, FilePurpose
from review_pipeline import sdk, target
from review_pipeline.config import ReviewConfig, load_config
from review_pipeline.facts import (CONTEXT_LINES, SCHEMA_PATH, FactsFile, diff_ranges, diff_scope, resolver,
                                   schema_text, tree_scope)
from review_pipeline.models import ReviewState
from review_pipeline.stages import RunContext
from review_pipeline.stages.explain import BASE_DIR, run_explain

_ENV = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-10-01T10:00:00Z", "GIT_COMMITTER_DATE": "2026-10-01T10:00:00Z",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **_ENV}).stdout.strip()


def _lines(n: int, word: str) -> list[str]:
    return [f"{word} {i}" for i in range(1, n + 1)]


def _write(repo: Path, files: dict[str, str | bytes]) -> None:
    for path, body in files.items():
        p = repo / path
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")


@pytest.fixture(scope="module")
def change_repo(tmp_path_factory) -> tuple[Path, str, str]:
    """base -> head: app.py gains a line, changes one and loses three; gone.py is deleted;
    old_name.py is renamed with one line changed; added.py is new; logo.bin is binary."""
    repo = tmp_path_factory.mktemp("change") / "shop"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    app = _lines(200, "line")
    app[189] = "-- not a header"   # removed below; the diff writes it as "--- not a header"
    _write(repo, {"app.py": "\n".join(app) + "\n", "gone.py": "\n".join(_lines(5, "gone")) + "\n",
                  "old_name.py": "\n".join(_lines(10, "kept")) + "\n", "logo.bin": b"\x00\x01\x02binary\x00"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    head_app = app[:10] + ["inserted"] + app[10:]
    head_app[100] = "changed 100"     # base line 100 is head line 101
    del head_app[190:193]             # base lines 190-192
    (repo / "gone.py").unlink()
    _git(repo, "mv", "old_name.py", "new_name.py")
    renamed = _lines(10, "kept")
    renamed[4] = "renamed 5"
    _write(repo, {"app.py": "\n".join(head_app) + "\n", "new_name.py": "\n".join(renamed) + "\n",
                  "added.py": "a\nb\nc\n", "logo.bin": b"\x00\x03\x04binary\x00"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def change_target(change_repo, tmp_path):
    repo, base, head = change_repo
    resolved = target.resolve(repo, range_=f"{base}..{head}")
    info = target.build_target(resolved)
    return target.write_target_files(info, target.diff_text(resolved.repo, base, head), tmp_path / info.slug)


@pytest.fixture
def change_ctx(change_target, tmp_path) -> RunContext:
    target_dir = tmp_path / change_target.slug
    snapshot = target.snapshot_head(change_target, target_dir / "tree")
    return RunContext(config=ReviewConfig(concurrency=1, explain=True), snapshot=snapshot,
                      diff=Path(change_target.diff_path).read_text(), target_dir=target_dir)


# --- G1: the scope ----------------------------------------------------------------------


def test_diff_scope_widens_head_hunks_clips_them_and_keeps_removed_lines_at_base(change_target, change_ctx):
    assert CONTEXT_LINES == 20
    scope = diff_scope(change_ctx.diff, change_ctx.snapshot)
    got = {(s.path, s.side, s.start, s.end) for s in scope.spans}
    assert got == {
        # head hunks 8-14, 98-104 and 188-193, each widened by 20 and clipped to the 198-line file
        ("app.py", "head", 1, 34), ("app.py", "head", 78, 124), ("app.py", "head", 168, 198),
        # the changed line and the three removed ones, the "--- not a header" line among them
        ("app.py", "base", 100, 100), ("app.py", "base", 190, 192),
        ("gone.py", "base", 1, 5),                                   # a deleted file: base only
        ("new_name.py", "head", 1, 10), ("old_name.py", "base", 5, 5),  # a rename: new path at head, old at base
        ("added.py", "head", 1, 3),                                  # a new file: head only
    }
    assert not any(s.path == "logo.bin" for s in scope.spans)       # binary: no hunk, left out


def test_overlapping_widened_hunks_merge_into_one_span():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -10,1 +10,1 @@\n-a\n+b\n@@ -40,1 +40,1 @@\n-c\n+d\n")
    head, base = diff_ranges(diff)
    assert head == {"x.py": [(10, 10), (40, 40)]} and base == {"x.py": [(10, 10), (40, 40)]}
    spans = [s for s in diff_scope(diff, Path("/nonexistent")).spans if s.side == "head"]
    assert [(s.start, s.end) for s in spans] == [(1, 60)]          # 1-30 and 20-60 overlap; no file to clip to


def test_tree_scope_is_every_file_under_review_whole_at_head(change_repo, tmp_path):
    repo, _, head = change_repo
    info = target.build_target(target.resolve(repo, tree=head))
    snapshot = target.snapshot_head(info, tmp_path / "tree")
    scope = tree_scope(info, snapshot)
    assert {(s.path, s.end, s.whole_file, s.side) for s in scope.spans} == {
        ("app.py", 198, True, "head"), ("new_name.py", 10, True, "head"), ("added.py", 3, True, "head")}
    assert resolver(info)("app.py", "base") is None                 # a tree has no base side


def test_resolver_places_head_and_base_lines_at_their_commits(change_target):
    resolve = resolver(change_target)
    assert resolve("app.py", "head") == (change_target.repo_path, change_target.head_sha, "app.py")
    assert resolve("gone.py", "base") == (change_target.repo_path, change_target.base_sha, "gone.py")
    assert resolve("", "head") is None


# --- G2: the stage and facts.json --------------------------------------------------------


class Canned:
    """Stands in for sdk.agent_call_structured: answers in order, every call recorded."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append({"prompt": prompt, "label": budget_label, **kwargs})
        assert schema is ExploreOutput
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _cite(path: str, line: int, quote: str, side: str = "head") -> Citation:
    return Citation(path=path, line_start=line, line_end=line, quote=quote, side=side)


def _first_answer(base_dir: Path) -> ExploreOutput:
    return ExploreOutput(
        facts=[
            Fact(claim="new_name.py renames a line", citations=[_cite("new_name.py", 5, "renamed 5")]),
            Fact(claim="made up", citations=[_cite("new_name.py", 5, "nothing like this")]),
            # read from the base copy, cited by that path and with the side left at head
            Fact(claim="gone.py held five lines", citations=[_cite(str(base_dir / "gone.py"), 3, "gone 3")]),
        ],
        interfaces=[Fact(claim="added.py starts with a", citations=[_cite("added.py", 1, "a")])],
    )


def _reask_answer() -> ExploreOutput:
    return ExploreOutput(
        facts=[Fact(claim="app.py changes line 100", citations=[_cite("app.py", 101, "changed 100")])],
        files=[FilePurpose(path="app.py", purpose="numbered lines", citation=_cite("app.py", 1, "line 1"))],
    )


async def test_explain_writes_checked_facts_that_validate_against_the_schema(change_target, change_ctx, monkeypatch):
    base_dir = change_ctx.snapshot.parent / BASE_DIR
    canned = Canned([_first_answer(base_dir), _reask_answer()])
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    state = await run_explain(ReviewState(target=change_target), change_ctx)

    # one unit, then one re-ask for app.py: it has more than 30 lines in scope and no fact
    assert len(canned.calls) == 2
    first, reask = canned.calls
    assert {c["label"] for c in canned.calls} == {"explain"}
    assert first["allowed_tools"] == sdk.READ_ONLY_TOOLS and first["cwd"] == str(change_ctx.snapshot)
    assert f"read it at {base_dir}/gone.py" in first["prompt"]
    assert "- app.py, lines 78-124\n" in first["prompt"]
    assert "An earlier pass left these files" in reask["prompt"] and "- app.py\n" in reask["prompt"]
    assert not base_dir.exists()   # the base copy is deleted when the stage ends

    path = change_ctx.target_dir / "facts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    assert (data["schema_version"], data["kind"]) == (1, "change")
    assert (data["base_sha"], data["head_sha"]) == (change_target.base_sha, change_target.head_sha)
    claims = {f["claim"]: f for f in data["facts"]}
    assert "made up" not in claims and [d["what"] for d in data["dropped"]] == ["made up"]
    gone = claims["gone.py held five lines"]["citations"][0]
    assert (gone["path"], gone["side"]) == ("gone.py", "base")
    assert [f["claim"] for f in data["interfaces"]] == ["added.py starts with a"]
    assert data["interfaces"][0]["id"] == claims["added.py starts with a"]["id"]
    assert data["files"] == [{"path": "app.py", "purpose": "numbered lines",
                              "citation": claims["app.py: numbered lines"]["citations"][0]}]
    assert data["uncovered"] == []
    assert state.stage == "explain" and state.facts is not None
    assert change_ctx.counts["facts"] == len(data["facts"]) and change_ctx.counts["dropped"] == 1


async def test_a_resumed_explain_asks_no_unit_twice(change_target, change_ctx, monkeypatch):
    base_dir = change_ctx.snapshot.parent / BASE_DIR
    monkeypatch.setattr(sdk, "agent_call_structured", Canned([_first_answer(base_dir), _reask_answer()]))
    done = await run_explain(ReviewState(target=change_target), change_ctx)

    again = Canned([])
    monkeypatch.setattr(sdk, "agent_call_structured", again)
    resumed = done.model_copy(update={"stage": "target"})          # as if stopped before the checkpoint
    await run_explain(resumed, change_ctx)
    assert again.calls == []
    assert resumed.facts == done.facts


async def test_a_failed_unit_is_an_error_line_not_a_failed_review(change_target, change_ctx, monkeypatch):
    # the unit fails, and so does the one re-ask its uncovered files get
    monkeypatch.setattr(sdk, "agent_call_structured",
                        Canned([sdk.AgentCallError("no answer"), sdk.AgentCallError("still none")]))
    state = await run_explain(ReviewState(target=change_target), change_ctx)
    assert state.stage == "explain" and state.facts.facts == []
    assert state.errors == ["explain unit:u1: no answer", "explain unit:u1:reask: still none"]
    assert change_ctx.counts["failed_units"] == 2
    assert (change_ctx.target_dir / "facts.json").is_file()


async def test_explain_off_makes_no_call_and_writes_nothing(change_target, change_ctx, monkeypatch):
    canned = Canned([])
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    change_ctx.config.explain = False
    state = await run_explain(ReviewState(target=change_target), change_ctx)
    assert canned.calls == [] and state.stage == "target" and state.facts is None
    assert not (change_ctx.target_dir / "facts.json").exists()


def test_the_committed_schema_is_generated_from_the_model():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text(), \
        "regenerate review/schema/facts.v1.schema.json from review_pipeline.facts.schema_text()"
    assert FactsFile.model_fields["schema_version"].is_required()


# --- config ---------------------------------------------------------------------------


def test_explain_and_find_with_facts_default_off(tmp_path):
    cfg = load_config(tmp_path)
    assert (cfg.explain, cfg.find_with_facts) == (False, False)


def test_explain_comes_from_review_yaml_or_multiplai_conf(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "multiplai.conf").write_text("review_explain=true\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(home))
    assert load_config(tmp_path).explain is True
    (tmp_path / "review.yaml").write_text("explain: false\n", encoding="utf-8")
    assert load_config(tmp_path).explain is False          # the project's file wins


def test_find_with_facts_needs_explain(tmp_path):
    (tmp_path / "review.yaml").write_text("find_with_facts: true\n", encoding="utf-8")
    assert load_config(tmp_path).find_with_facts is False
    (tmp_path / "review.yaml").write_text("explain: true\nfind_with_facts: true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert (cfg.explain, cfg.find_with_facts) == (True, True)


# --- in the run ------------------------------------------------------------------------


def test_a_review_with_explain_on_runs_it_before_find_and_writes_facts_json(fixture_repo, tmp_path, monkeypatch,
                                                                            capsys):
    from test_orchestrator import FakeAgents, _review_args

    from review_pipeline.__main__ import main

    fake = FakeAgents()

    async def agents(prompt, schema, *, budget_label="", **kwargs):
        if budget_label == "explain":
            fake.calls.append(budget_label)
            return ExploreOutput(facts=[Fact(claim="the keyword is fixed",
                                             citations=[_cite("rateplan_service.py", 1, "KEYWORD = 'dolcebot'")])])
        return await fake(prompt, schema, budget_label=budget_label, **kwargs)

    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("explain: true\n", encoding="utf-8")
    assert main(_review_args(repo, base, head, out)) == 0
    target_dir = out / f"booking-engine--{base}..{head}"
    assert fake.calls[0] == "explain" and all(c != "explain" for c in fake.calls[1:])
    assert "explain done: 1 units, 1 facts" in capsys.readouterr().out
    data = json.loads((target_dir / "facts.json").read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    assert [f["claim"] for f in data["facts"]] == ["the keyword is fixed"]
    assert (target_dir / "findings.json").is_file()
    stages = [s["name"] for s in json.loads((target_dir / "findings.json").read_text())["run"]["stages"]]
    assert stages[0] == "explain"


def test_a_review_with_explain_off_writes_no_facts_json(fixture_repo, tmp_path, monkeypatch):
    from test_orchestrator import FakeAgents, _review_args

    from review_pipeline.__main__ import main

    fake = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    assert main(_review_args(repo, base, head, out)) == 0
    assert "explain" not in fake.calls
    assert not (out / f"booking-engine--{base}..{head}" / "facts.json").exists()
