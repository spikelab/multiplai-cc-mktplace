"""open / edit / render end to end, with `gh` and the model stubbed."""

from __future__ import annotations

import json

import pytest
from conftest import git, make_bare_origin, make_change, make_repo

from pr_pipeline import commands, markers
from pr_pipeline.__main__ import build_parser

URL = "https://github.com/o/r/pull/7"


def run(argv: list[str]) -> int:
    return commands.run(build_parser().parse_args(argv))


@pytest.fixture
def logged(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(commands, "log_event", lambda comp, event, msg, **f: events.append((comp, event, msg, f)))
    return events


def _setup(tmp_path, *, origin=False):
    repo = make_repo(tmp_path / "proj")
    if origin:
        make_bare_origin(repo, tmp_path / "origin.git")
    return repo, make_change(repo, "feat")


# --- open ---------------------------------------------------------------------


def test_open_pushes_and_opens_a_draft_pr_from_a_body_file(tmp_path, gh_stub, capsys, logged):
    repo, change = _setup(tmp_path, origin=True)
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(["gh", "pr", "create"], stdout=URL + "\n")
    assert run(["open", "--repo", str(repo), "--change-dir", str(change)]) == 0
    out = capsys.readouterr().out
    assert "PUSHED:feat/x" in out and f"PR:{URL}" in out and "DIFF:2 files, +3 -1" in out
    create = gh_stub.writes()[0]
    assert create[create.index("--title") + 1] == "buildme: feat"
    assert "--draft" in create and "--body" not in create
    assert create[create.index("--head") + 1] == "feat/x"
    body = gh_stub.bodies["gh pr create"]
    assert body.startswith("<!-- pr:managed v1 -->\n") and "Because the queue drops jobs." in body
    assert git(tmp_path / "origin.git", "branch", "--list", "feat/x").strip()  # pushed
    assert [e[1] for e in logged] == ["start", "done"]
    done = logged[-1][3]
    assert done["subcommand"] == "open" and done["pr"] == URL and done["template"] == "standard"
    assert done["diff"] == "2 files, +3 -1" and done["gate"] == "passed"
    assert all("Because the queue" not in json.dumps(e) for e in logged), "no body text in the log"


def test_open_ready_and_config_draft_false_open_ready_for_review(tmp_path, gh_stub):
    repo, change = _setup(tmp_path)
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(["gh", "pr", "create"], stdout=URL)
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push", "--ready"]) == 0
    assert "--draft" not in gh_stub.writes()[-1]
    (repo / "specs" / "config.yaml").write_text("pr:\n  draft: false\n")
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push"]) == 0
    assert "--draft" not in gh_stub.writes()[-1]


def test_open_draft_flag_overrides_config_draft_false(tmp_path, gh_stub):
    # buildme passes its own git.pr choice: --draft must win over pr.draft: false.
    repo, change = _setup(tmp_path)
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(["gh", "pr", "create"], stdout=URL)
    (repo / "specs" / "config.yaml").write_text("pr:\n  draft: false\n")
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push", "--draft"]) == 0
    assert "--draft" in gh_stub.writes()[-1]


def test_open_draft_and_ready_together_are_refused():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["open", "--repo", "r", "--change-dir", "c", "--draft", "--ready"])


def test_open_dry_run_prints_the_commands_and_body_and_writes_nothing(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path, origin=True)
    gh_stub.on(["gh", "repo", "view"], stdout="main\n")
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--dry-run",
                "--title", "My title"]) == 0
    out = capsys.readouterr().out
    assert "DRY_RUN:git push -u origin feat/x" in out
    assert "DRY_RUN:gh pr create --title 'My title' --body-file '<body-file>' --head feat/x --draft" in out
    assert out.split("DRY_RUN_BODY:\n", 1)[1].startswith("<!-- pr:managed v1 -->")
    assert gh_stub.writes() == []
    assert not git(tmp_path / "origin.git", "branch", "--list", "feat/x").strip()


def test_open_gh_failure_exits_2_with_a_diagnosis(tmp_path, gh_stub, capsys, logged):
    repo, change = _setup(tmp_path)
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(["gh", "pr", "create"], 1, stderr="gh auth login")
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push"]) == 2
    assert "PR_DIAGNOSIS:`gh pr create` failed (exit 1): gh auth login" in capsys.readouterr().out
    assert logged[-1][1] == "failed"


def test_open_gate_failure_exits_1_and_writes_nothing(tmp_path, gh_stub, model_stub, capsys):
    repo, change = _setup(tmp_path)
    (repo / "pull_request_template.md").write_text("## Summary\n")
    gh_stub.on(["gh", "repo", "view"], stdout="main\n")
    model_stub.answers = [{"sections": {}}, {"sections": {}}]
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push"]) == 1
    assert "a gate rejected the body; nothing was written" in capsys.readouterr().out
    assert gh_stub.writes() == []


# --- render -------------------------------------------------------------------


def test_render_prints_only_the_body_and_calls_no_gh(tmp_path, capsys):
    repo, change = _setup(tmp_path)  # the autouse fixture fails any gh call
    assert run(["render", "--repo", str(repo), "--change-dir", str(change)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("<!-- pr:managed v1 -->") and "PR:" not in captured.out
    assert "DIFF:2 files, +3 -1" in captured.err


# --- edit ---------------------------------------------------------------------


def _pr_json(body: str, author: str = "someone") -> str:
    return json.dumps({"body": body, "author": {"login": author}, "number": 7, "url": URL,
                       "baseRefName": "main", "headRefName": "feat/x"})


def _managed_body(repo, change, capsys) -> str:
    assert run(["render", "--repo", str(repo), "--change-dir", str(change)]) == 0
    return capsys.readouterr().out


def test_edit_preserves_a_human_change_outside_markers(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    body = _managed_body(repo, change, capsys)
    human = body.replace("## Blocks\n", "## Blocks\nA reviewer's note.\n")
    (change / "proposal.md").write_text("## Why\n\nA sharper reason.\n")
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(human)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert "A reviewer's note." in new and "A sharper reason." in new
    assert "Because the queue drops jobs." not in new
    assert f"PR_EDITED:{URL}" in capsys.readouterr().out


def test_edit_of_a_managed_body_posts_no_comment(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    body = _managed_body(repo, change, capsys)
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(body)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    assert [c[:3] for c in gh_stub.writes()] == [["gh", "pr", "edit"]]


def test_edit_does_not_restore_a_deleted_marker_pair(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    body = _managed_body(repo, change, capsys)
    a = body.index(markers.begin("why"))
    b = body.index(markers.end("why")) + len(markers.end("why"))
    deleted = body[:a] + body[b:]
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(deleted)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    assert "PR_DIAGNOSIS:section removed by a human, not restored: why" in capsys.readouterr().out
    assert markers.begin("why") not in gh_stub.bodies["gh pr edit"]


def test_edit_keeps_a_ticket_link_given_only_to_open(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)  # no ticket.md, so the link exists only in the PR body
    ticket = "https://tickets.example.com/T-42"
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(["gh", "pr", "create"], stdout=URL)
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push",
                "--ticket-url", ticket]) == 0
    opened = gh_stub.bodies["gh pr create"]
    assert markers.wrap("ticket", ticket) in opened
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(opened)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    assert markers.wrap("ticket", ticket) in gh_stub.bodies["gh pr edit"]


BUILT_STATE = {"docs_impact": ["README.md"],
               "tdd": {"blocks": [{"number": 1, "name": "First block", "status": "done"}]}}
NO_DOCS_TEMPLATE = "## Summary\n<!-- What does this change do? -->\n"


@pytest.mark.parametrize("template", [None, NO_DOCS_TEMPLATE], ids=["standard", "repo-template"])
def test_edit_after_buildme_deleted_its_state_keeps_docs_and_block_statuses(tmp_path, gh_stub, model_stub,
                                                                             capsys, template):
    repo = make_repo(tmp_path / "proj")
    if template:
        (repo / ".github").mkdir()
        (repo / ".github" / "pull_request_template.md").write_text(template)
        model_stub.answers = [{"sections": {"summary": "Stops the queue dropping jobs."}}] * 2
    change = make_change(repo, state=BUILT_STATE)
    body = _managed_body(repo, change, capsys)
    assert "`README.md`" in body and "Block 1: First block — done" in body
    (change / ".build-state.json").unlink()  # buildme removes it after a successful publish
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(body)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert "`README.md`" in new and "Block 1: First block — done" in new
    assert "PR_DIAGNOSIS:no .build-state.json" in capsys.readouterr().out


def test_edit_refuses_nested_markers_with_exit_2(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    bad = f"{markers.MANAGED}\n{markers.begin('a')}\n{markers.begin('b')}\n{markers.end('b')}\n{markers.end('a')}\n"
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(bad))
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 2
    assert "nested markers" in capsys.readouterr().out
    assert gh_stub.writes() == []


def test_hand_opened_pr_with_edit_rights_gets_sections_appended_and_one_comment(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    existing = "I opened this by hand.\n\n- [x] my own checklist\n"
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(existing)).on(["gh", "repo", "view"], stdout="WRITE\n")
    gh_stub.on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert new.startswith(markers.MANAGED + "\n")
    assert existing.rstrip("\n") in new, "the existing text is unchanged"
    assert new.index("I opened this by hand.") < new.index("## Why")
    assert [c[:3] for c in gh_stub.writes()] == [["gh", "pr", "edit"], ["gh", "pr", "comment"]]
    comment = gh_stub.bodies["gh pr comment"]
    assert "edited this PR's description" in comment
    for key in ("why", "blocks", "documentation", "companion-artifacts", "ticket", "size"):
        assert f"`{key}`" in comment


def test_hand_opened_pr_by_the_token_owner_counts_as_edit_rights(tmp_path, gh_stub):
    repo, change = _setup(tmp_path)
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json("mine", author="spike"))
    gh_stub.on(["gh", "repo", "view"], stdout="READ\n").on(["gh", "api", "user"], stdout="spike\n")
    gh_stub.on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0


def test_hand_opened_pr_without_edit_rights_writes_nothing_and_exits_2(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json("theirs", author="other"))
    gh_stub.on(["gh", "repo", "view"], stdout="READ\n").on(["gh", "api", "user"], stdout="spike\n")
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 2
    assert "PR_DIAGNOSIS:no rights to edit PR 7 (viewerPermission=READ)" in capsys.readouterr().out
    assert gh_stub.writes() == []


def test_edit_appends_a_new_extra_section(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    body = _managed_body(repo, change, capsys)
    extra = tmp_path / "w.md"
    extra.write_text("## Findings fixed\n\n- round 1\n")
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(body)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--section", str(extra)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert new.startswith(body.rstrip("\n").split("\n", 1)[0])
    assert new.rstrip().endswith(f"## Findings fixed\n{markers.wrap('extra:findings-fixed', '- round 1')}")


def test_edit_push_pushes_before_editing(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path, origin=True)
    body = _managed_body(repo, change, capsys)
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(body)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--push"]) == 0
    assert "PUSHED:feat/x" in capsys.readouterr().out
    assert git(tmp_path / "origin.git", "branch", "--list", "feat/x").strip()


def test_an_unexpected_error_exits_2_not_1(tmp_path, monkeypatch, capsys, logged):
    repo, change = _setup(tmp_path)

    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(commands, "render_body", boom)
    assert run(["render", "--repo", str(repo), "--change-dir", str(change)]) == 2
    assert "PR_DIAGNOSIS:pr render failed: UnicodeDecodeError" in capsys.readouterr().out
    assert logged[-1][1] == "failed"


def test_an_error_while_starting_up_exits_2_not_1(tmp_path, monkeypatch, capsys):
    import multiplai_core.env

    from pr_pipeline import __main__ as cli

    def boom(*a, **k):
        raise PermissionError("cannot read .env")

    monkeypatch.setattr(multiplai_core.env, "load_env", boom)
    repo, change = _setup(tmp_path)
    assert cli.main(["render", "--repo", str(repo), "--change-dir", str(change)]) == 2
    assert "PR_DIAGNOSIS:pr render failed to start: PermissionError: cannot read .env" in capsys.readouterr().out
