"""Rows S38 and S39 of plan-factory-merged-2026-10-29.md (items 46 and 47), and the pieces they rest on.

S38: `review --dimensions` and `--finder-model` choose a run's dimension finders and their model; a
stopped run resumes with the ones it saved, whatever the settings say at resume, and a new head starts
a new review with the current settings. S39: a piece that changes more than `CALLERS_MAX_SYMBOLS`
existing definitions is cut in two for the callers finder when the review plans its pieces; `resume`
uses the saved pieces and cuts nothing.

Each row's case stops a review at the row's point, resumes it, and compares the result with a run that
never stopped (`test_resume_finders.shape`). The model is `test_reported_finders.Model`.
"""

from __future__ import annotations

import importlib

import pytest
import test_reported_finders
from review_pipeline import sdk
from review_pipeline.__main__ import main
from review_pipeline.chunks import changed_symbols
from review_pipeline.models import Piece, ReviewState
from review_pipeline.orchestrator import DIMENSIONS_NOTICE, FINDER_MODEL_NOTICE
from review_pipeline.stages import find
from test_reported_finders import DIMENSIONS, git, make_repo, name, review, saved
from test_resume_finders import asked, assert_asked_once, shape

model = test_reported_finders.model

SETTINGS = ("--dimensions", "diff-bugs,callers", "--finder-model", "model-a")


def finder_models(monkeypatch, fake) -> list:
    """Each report call's `model`, recorded on the way to *fake*."""
    seen: list = []

    async def text(prompt, **kwargs):
        seen.append(kwargs.get("model"))
        return await fake.text(prompt, **kwargs)

    monkeypatch.setattr(sdk, "agent_call_text", text, raising=False)
    return seen


def resume(target_dir, *extra: str) -> int:
    return main(
        ["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo", *extra]
    )


def kill_find(monkeypatch):
    """Stop every review at the find stage, before any finder; returns a function that undoes it."""
    orchestrator = importlib.import_module("review_pipeline.orchestrator")
    stages = list(orchestrator.STAGE_FUNCTIONS)

    async def killed(state, ctx):
        raise RuntimeError("killed before find")

    monkeypatch.setattr(
        orchestrator,
        "STAGE_FUNCTIONS",
        [(n, killed if n == "find" else f) for n, f in stages],
    )
    return lambda: monkeypatch.setattr(orchestrator, "STAGE_FUNCTIONS", stages)


# --- the flags ----------------------------------------------------------------------------------------------


def test_dimensions_and_finder_model_set_the_run_over_review_yaml_and_model(
    tmp_path, model, monkeypatch
):
    repo, base, head = make_repo(tmp_path, 2)
    fake = model()
    seen = finder_models(monkeypatch, fake)
    out = tmp_path / "out"
    yaml = "dimensions: [diff-bugs, history]\nfinder_model: model-y\n"
    assert (
        review(repo, base, head, out, *SETTINGS, "--model", "model-m", yaml=yaml) == 0
    )
    _, state = saved(out)
    assert state.dimensions == ["diff-bugs", "callers"]
    stages = state.run_config["stages"]
    assert stages["find"]["model"] == "model-a"
    assert stages["verify"]["model"] == "model-m"  # --model still sets the others
    assert set(seen) == {"model-a"}
    assert {k.partition("@")[0] for k in asked(fake)} == {"diff-bugs", "callers"}


def test_without_the_flags_review_yaml_still_decides(tmp_path, model, monkeypatch):
    repo, base, head = make_repo(tmp_path, 2)
    fake = model()
    seen = finder_models(monkeypatch, fake)
    out = tmp_path / "out"
    assert review(repo, base, head, out, yaml="finder_model: model-y\n") == 0
    _, state = saved(out)
    assert state.dimensions == list(DIMENSIONS)
    assert set(seen) == {"model-y"}


def test_dimensions_naming_trim_turns_the_trim_finder_on(tmp_path, model):
    repo, base, head = make_repo(tmp_path, 1)
    model()
    out = tmp_path / "out"
    assert review(repo, base, head, out, "--dimensions", "callers, trim") == 0
    _, state = saved(out)
    assert state.dimensions == ["callers", "trim"]


def test_an_unknown_dimension_is_refused_before_anything_runs(tmp_path, model, capsys):
    repo, base, head = make_repo(tmp_path, 1)
    fake = model()
    out = tmp_path / "out"
    assert review(repo, base, head, out, "--dimensions", "diff-bugs,speed") == 2
    err = capsys.readouterr().err
    assert "--dimensions: unknown dimension 'speed'" in err
    assert fake.report_calls == []


# --- S38 ----------------------------------------------------------------------------------------------------


def test_s38_resume_keeps_the_saved_dimensions_and_finder_model_when_the_config_changed(
    tmp_path, model, monkeypatch, capsys
):
    repo, base, head = make_repo(tmp_path, 3)
    whole_out = tmp_path / "whole"
    finder_models(monkeypatch, model())
    assert review(repo, base, head, whole_out, *SETTINGS) == 0
    whole = shape(whole_out)

    fake = model(stop_report=2)
    seen = finder_models(monkeypatch, fake)
    out = tmp_path / "out"
    assert review(repo, base, head, out, *SETTINGS) == 4
    target_dir, stopped = saved(out)
    assert stopped.dimensions == ["diff-bugs", "callers"]
    assert stopped.run_config["stages"]["find"]["model"] == "model-a"
    # Every setting changes before the resume: review.yaml, and the resume's own --model.
    (out / "review.yaml").write_text("concurrency: 1\nfinder_model: model-b\n")
    capsys.readouterr()
    assert resume(target_dir, "--model", "model-c") == 0
    err = capsys.readouterr().err
    assert FINDER_MODEL_NOTICE.format(saved="model-a", new="model-c") in err, err
    assert (
        DIMENSIONS_NOTICE.format(saved="diff-bugs, callers", new=", ".join(DIMENSIONS))
        in err
    )
    assert shape(out) == whole
    assert set(seen) == {"model-a"}
    _, state = saved(out)
    assert state.run_config["stages"]["find"]["model"] == "model-a"
    assert state.run_config["stages"]["verify"]["model"] == "model-c"
    assert_asked_once(fake, "diff-bugs@2")


def test_s38_a_new_head_starts_a_new_review_with_the_current_settings(
    tmp_path, model, monkeypatch
):
    repo, base, head = make_repo(tmp_path, 3)
    git(repo, "remote", "add", "origin", "https://github.com/acme/proj.git")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "update-ref", "refs/remotes/origin/feature/x", head)
    out = tmp_path / "out"
    out.mkdir()

    def branch_review(*extra: str) -> int:
        (out / "review.yaml").write_text("concurrency: 1\nfinder_model: model-b\n")
        return main(
            ["--session-id", "sess-test", "--out", str(out), "review", "--repo"]
            + [str(repo), "--branch", "feature/x", "--trust-repo", *extra]
        )

    finder_models(monkeypatch, model(stop_report=2))
    assert branch_review(*SETTINGS) == 4
    target_dir, stopped = saved(out)
    assert stopped.dimensions == ["diff-bugs", "callers"]

    (repo / name(1) / "m.py").write_text(f"{name(1)}_1 = 1\n{name(1)}_2 = 2\nx = 3\n")
    git(repo, "commit", "-q", "-am", "a second head")
    head2 = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/feature/x", head2)
    seen = finder_models(monkeypatch, model())
    assert branch_review() == 0
    again, state = saved(out)
    assert again == target_dir
    assert state.target.head_sha == head2
    assert state.dimensions == list(DIMENSIONS)
    assert state.run_config["stages"]["find"]["model"] == "model-b"
    assert set(seen) == {"model-b"}


# --- S39: the callers split -----------------------------------------------------------------------------------


def symbol_repo(tmp_path):
    """As `make_repo(tmp_path, 3)`, but `p02/` has two modules of seven functions each, and head changes
    every one's signature: fourteen changed definitions, over `CALLERS_MAX_SYMBOLS`."""
    repo, _, _ = make_repo(tmp_path, 3)
    base = git(repo, "rev-parse", "HEAD~1")
    git(repo, "checkout", "-q", "-b", "symbols", base)

    def module(prefix: str, args: str) -> str:
        return "".join(
            f"\n\ndef {prefix}{i}({args}):\n    return a\n" for i in range(1, 8)
        )

    (repo / "p02" / "m.py").write_text("p02_1 = 1\n" + module("m", "a"))
    (repo / "p02" / "n.py").write_text("p02_n1 = 1\n" + module("n", "a"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base with functions")
    base = git(repo, "rev-parse", "HEAD")
    for i in (1, 3):
        (repo / name(i) / "m.py").write_text(f"{name(i)}_1 = 1\n{name(i)}_2 = 2\n")
    (repo / "p02" / "m.py").write_text("p02_1 = 1\np02_2 = 2\n" + module("m", "a, b"))
    (repo / "p02" / "n.py").write_text("p02_n1 = 1\n" + module("n", "a, b"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_a_piece_over_the_threshold_is_cut_for_callers_before_any_finder(
    tmp_path, model
):
    repo, base, head = symbol_repo(tmp_path)
    fake = model()
    out = tmp_path / "out"
    assert review(repo, base, head, out) == 0
    _, state = saved(out)
    by_id = {p.id: p for p in state.pieces}
    assert by_id[2].files == ["p02/m.py", "p02/n.py"]
    assert by_id[2].split_into == [4, 5]
    assert [by_id[4].files, by_id[5].files] == [["p02/m.py"], ["p02/n.py"]]
    assert by_id[4].split_for == by_id[5].split_for == ["callers"]
    keys = asked(fake)
    assert "callers@2" not in keys
    assert keys.count("callers@4") == keys.count("callers@5") == 1
    assert keys.count("diff-bugs@2") == 1  # the other dimensions read the whole piece


def test_a_review_without_callers_cuts_nothing(tmp_path, model):
    repo, base, head = symbol_repo(tmp_path)
    model()
    out = tmp_path / "out"
    assert review(repo, base, head, out, "--dimensions", "diff-bugs") == 0
    _, state = saved(out)
    assert [p.id for p in state.pieces] == [1, 2, 3]


def test_s39_a_stop_after_the_pieces_are_planned_resumes_with_the_saved_halves(
    tmp_path, model, monkeypatch
):
    repo, base, head = symbol_repo(tmp_path)
    model()
    assert review(repo, base, head, tmp_path / "whole") == 0
    whole = shape(tmp_path / "whole")

    restore = kill_find(monkeypatch)
    fake = model()
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="killed"):
        review(repo, base, head, out)
    target_dir, stopped = saved(out)
    assert stopped.finder_reports == {}
    assert [p.model_dump() for p in stopped.pieces] == whole["pieces"]
    restore()
    assert resume(target_dir) == 0
    assert shape(out) == whole
    assert "callers@2" not in asked(fake)
    assert_asked_once(fake)


def test_s39_resume_does_not_split_the_saved_pieces_again(tmp_path, model, monkeypatch):
    """Pieces saved unsplit (a run started before this release, or with another threshold) stay as
    saved: resume plans nothing."""
    repo, base, head = symbol_repo(tmp_path)
    monkeypatch.setattr(find, "CALLERS_MAX_SYMBOLS", 100)
    model()
    assert review(repo, base, head, tmp_path / "whole") == 0
    whole = shape(tmp_path / "whole")

    restore = kill_find(monkeypatch)
    fake = model()
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="killed"):
        review(repo, base, head, out)
    target_dir, stopped = saved(out)
    assert [p.id for p in stopped.pieces] == [1, 2, 3]
    restore()
    monkeypatch.setattr(find, "CALLERS_MAX_SYMBOLS", 12)
    assert resume(target_dir) == 0
    assert shape(out) == whole
    _, state = saved(out)
    assert [p.id for p in state.pieces] == [1, 2, 3]
    assert asked(fake).count("callers@2") == 1
    assert_asked_once(fake)


# --- changed_symbols --------------------------------------------------------------------------------------------

DIFF = """\
diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,4 +1,4 @@ class Owner:
-def load(a):
+def load(a, b):
     return a
@@ -10,3 +10,4 @@ def save(x):
     y = 1
+    z = 2
-async def gone():
diff --git a/b.ts b/b.ts
index 1..2 100644
--- a/b.ts
+++ b/b.ts
@@ -1,2 +1,2 @@
-export async function fetchIt(u) {
+export async function fetchIt(u, v) {
diff --git a/c.go b/c.go
index 1..2 100644
--- a/c.go
+++ b/c.go
@@ -1,2 +1,2 @@
-func (s *Server) Handle(w) {
+func (s *Server) Handle(w, r) {
diff --git a/new.py b/new.py
new file mode 100644
index 0..1
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+def fresh():
+    return 1
"""


def test_changed_symbols_counts_existing_definitions_only():
    assert changed_symbols(DIFF) == {
        ("a.py", "Owner"),
        ("a.py", "load"),
        ("a.py", "save"),
        ("a.py", "gone"),
        ("b.ts", "fetchIt"),
        ("c.go", "Handle"),
    }


def test_a_piece_split_piece_cannot_cut_is_left_whole(monkeypatch):
    """One hunk of one file: nothing to cut, so the callers finder reads it whole."""
    diff = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n-def f(a):\n+def f(a, b):\n"
    )
    piece = Piece(id=1, group="", files=["a.py"], chars=len(diff))
    state = ReviewState.model_construct(dimensions=["callers"], pieces=[piece])
    monkeypatch.setattr(find, "CALLERS_MAX_SYMBOLS", 0)
    assert find.split_for_callers(state, diff) == []
    assert [p.id for p in state.pieces] == [1]
