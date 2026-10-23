"""The whole run, end to end on the fake model: `new` to `SPEC:READY`."""

from __future__ import annotations

import json

from conftest import change_dir, committed, git, meta, ready_line, run, state
from multiplai_core.specs import is_ready, spec_revision


def test_new_runs_to_ready_and_commits_only_the_change_dir(repo, model, capsys):
    (repo / "other.txt").write_text("staged work\n")
    git(repo, "add", "other.txt")
    assert run(repo, "new", "throttle", "--auto") == 0
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert ready_line(out) == f"SPEC:READY:{cdir}:{spec_revision(cdir)}"
    assert is_ready(cdir) == (True, "")
    assert meta(cdir)["spec_status"] == "ready"
    for name in (
        "proposal.md",
        "use-cases.md",
        "interview.md",
        "plan.md",
        "specs.md",
        "prototype/NOTES.md",
        "tickets.json",
    ):
        assert (cdir / name).is_file(), name
    assert not (cdir / "components").exists() and not (cdir / "components.md").exists()
    # Only the change dir is in the commit, the run folder is not, and the
    # user's staged file stays staged and uncommitted.
    paths = committed(repo, cdir)
    assert paths and all(p.startswith("specs/changes/throttle/") for p in paths)
    assert not any("/.pm-spec/" in p for p in paths)
    assert git(repo, "diff", "--cached", "--name-only") == "other.txt"
    assert git(repo, "status", "--porcelain", "--", "specs") == ""
    assert state(cdir)["handoff"]["committed"] is True
    assert out.splitlines()[-1].startswith("PHASE") or "SPEC:READY" in out


def test_the_order_of_the_calls(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.labels() == [
        "interview:questions",
        "interview:answers",
        "spec:proposal",
        "spec:use-cases",
        "components",
        "prototype",
        "spec:specs:throttle",
        "consistency",
    ]


def test_the_spend_line_is_last(repo, model, capsys):
    from pm_spec_pipeline.__main__ import cli

    assert (
        cli(
            [
                "new",
                "throttle",
                "--project-dir",
                str(repo),
                "--auto",
                "--ticket-ref",
                "ENG-1",
            ]
        )
        == 0
    )
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("SPEND:") and float(last.split(":")[1]) > 0


def test_new_refuses_a_change_that_already_has_a_run(repo, model, capsys):
    assert run(repo, "new", "throttle", "--auto") == 0
    calls = len(model.calls)
    assert run(repo, "new", "throttle", "--auto") == 2
    assert "resume throttle" in capsys.readouterr().out
    assert len(model.calls) == calls


def test_resume_without_a_run_refuses(repo, model, capsys):
    assert run(repo, "resume", "nothing") == 2
    assert "new nothing" in capsys.readouterr().out
    assert model.calls == []


def test_a_finished_run_resumed_prints_ready_again_with_no_call(repo, model, capsys):
    assert run(repo, "new", "throttle", "--auto") == 0
    first = ready_line(capsys.readouterr().out)
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    assert ready_line(capsys.readouterr().out) == first
    assert len(model.calls) == calls


def test_an_untrusted_repository_is_refused_before_any_tool_call(
    repo, model, monkeypatch, capsys
):
    monkeypatch.delenv("PM_SPEC_TRUST_REPO")
    assert run(repo, "new", "throttle", "--auto") == 2
    assert "--trust-repo" in capsys.readouterr().out
    assert model.calls == []
    # --trust-repo is the same as the variable.
    assert run(repo, "resume", "throttle", "--trust-repo") == 0


def test_user_mode_needs_no_trust_until_the_prototype(repo, model, monkeypatch):
    """The user-mode interview, the proposal, the use cases and the components
    hold no tools; the prototype agent reads the repository, so it needs trust."""
    monkeypatch.delenv("PM_SPEC_TRUST_REPO")
    assert run(repo, "new", "throttle", "--interview-summary", "Throttle the API.") == 3
    assert run(repo, "resume", "throttle") == 3
    assert run(repo, "resume", "throttle") == 2
    assert "prototype" not in model.labels()
    assert model.labels()[-1] == "components"


def test_models_come_from_specs_config(repo, model):
    (repo / "specs").mkdir()
    (repo / "specs" / "config.yaml").write_text(
        "models:\n  interview: claude-haiku-4-5\n  spec: claude-sonnet-4-5\n  prototype: claude-opus-4-1\n"
        "efforts:\n  spec: high\n"
    )
    assert run(repo, "new", "throttle", "--auto") == 0
    by_label = {label: kw for label, _, kw in model.calls}
    assert by_label["interview:questions"]["model"] == "claude-haiku-4-5"
    assert by_label["spec:proposal"]["model"] == "claude-sonnet-4-5"
    assert by_label["consistency"]["model"] == "claude-sonnet-4-5"
    assert by_label["spec:proposal"]["effort"] == "high"


def test_the_change_yaml_starts_as_draft(repo, model, capsys):
    model["consistency"] = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
    assert run(repo, "new", "throttle", "--auto") == 1
    assert meta(change_dir(repo))["spec_status"] == "draft"
    assert (
        json.loads((change_dir(repo) / ".pm-spec" / "state.json").read_text())[
            "handoff"
        ]
        is None
    )


# --- the feature branch at handoff, as on the samples page ---------------------------

SAMPLE_NESTED = [
    ".change.yaml",
    "components.md",
    "components/timeout-setting/.change.yaml",
    "components/timeout-setting/plan.md",
    "components/timeout-setting/specs.md",
    "components/token-retry/.change.yaml",
    "components/token-retry/plan.md",
    "components/token-retry/specs.md",
    "interview.md",
    "prototype/NOTES.md",
    "prototype/mockup.html",
    "proposal.md",
    "tickets.json",
    "use-cases.md",
]
SAMPLE_SINGLE = [
    ".change.yaml",
    "interview.md",
    "plan.md",
    "proposal.md",
    "prototype/NOTES.md",
    "prototype/mockup.html",
    "specs.md",
    "tickets.json",
    "use-cases.md",
]


def _sample_run(repo, model, components, tickets) -> tuple[list[str], list[str]]:
    """The samples page's flow for auth-session (ENG-40): four review pushes
    and the handoff push. Origin's files under the change dir, and its log."""
    from conftest import origin, specs

    model["components"] = {
        "reason": "Each is a pull request on its own.",
        "components": components,
    }
    model["spec:specs:token-retry"] = specs(("session-refresh",))
    model["spec:specs:timeout-setting"] = specs(("session-length", "account-settings"))
    new = [
        "new",
        "auth-session",
        "--ticket-ref",
        "ENG-40",
        "--interview-summary",
        "Keep people signed in.",
    ]
    for argv, code in [
        (new, 3),
        (["resume", "auth-session"], 3),
        (["resume", "auth-session"], 3),
    ]:
        assert run(repo, *argv) == code, argv
    assert run(repo, "resume", "auth-session", *tickets) == 3
    assert run(repo, "resume", "auth-session") == 0
    branch = "ENG-40-auth-session"
    files = git(
        origin(repo),
        "ls-tree",
        "-r",
        "--name-only",
        branch,
        "specs/changes/auth-session",
    )
    log = git(origin(repo), "log", "--format=%s", branch).splitlines()
    prefix = "specs/changes/auth-session/"
    return [f.removeprefix(prefix) for f in files.splitlines()], log


def test_the_branch_at_handoff_holds_the_samples_tree_with_components(repo, model):
    from conftest import component

    files, log = _sample_run(
        repo,
        model,
        [component("token-retry"), component("timeout-setting", deps=("token-retry",))],
        ["--ticket", "token-retry=ENG-41", "--ticket", "timeout-setting=ENG-42"],
    )
    assert files == sorted(SAMPLE_NESTED)
    assert log == [
        "specs(auth-session): ready for build",
        "specs(auth-session): specs for review",
        "specs(auth-session): prototype and tickets",
        "specs(auth-session): components for review",
        "specs(auth-session): proposal for review",
        "initial",
    ]


def test_the_branch_at_handoff_holds_the_samples_tree_with_one_component(repo, model):
    from conftest import component

    files, log = _sample_run(repo, model, [component("auth-session")], [])
    assert files == sorted(SAMPLE_SINGLE)
    assert len(log) == 6 and log[0] == "specs(auth-session): ready for build"
