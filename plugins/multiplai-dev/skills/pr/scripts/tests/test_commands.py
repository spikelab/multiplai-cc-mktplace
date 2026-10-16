"""open / edit / render end to end, with `gh` and the model stubbed."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import git, make_bare_origin, make_change, make_repo

from pr_pipeline import commands, github, markers
from pr_pipeline.__main__ import build_parser
from pr_pipeline.template import STANDARD_TEMPLATE

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
    assert "DRY_RUN:git push -u origin refs/heads/feat/x:refs/heads/feat/x" in out
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


def test_open_folds_a_multi_line_gh_error_onto_one_diagnosis_line(tmp_path, gh_stub, capsys):
    # gh puts the existing PR's URL on the line after its message. A caller keeps only
    # lines that start with PR_DIAGNOSIS:, so the URL must be on the same line.
    repo, change = _setup(tmp_path)
    existing = "https://github.com/o/r/pull/9"
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(
        ["gh", "pr", "create"], 1,
        stderr=f'a pull request for branch "feat" into branch "main" already exists:\n{existing}\n')
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), "--no-push"]) == 2
    out = capsys.readouterr().out.splitlines()
    diagnoses = [line for line in out if line.startswith("PR_DIAGNOSIS:")]
    assert len(diagnoses) == 1
    assert diagnoses[0].endswith(f"already exists: {existing}")
    prefixes = ("PUSHED:", "PR:", "PR_EDITED:", "DIFF:", "PR_DIAGNOSIS:")
    assert [line for line in out if not line.startswith(prefixes)] == []


@pytest.mark.parametrize("error, code, expected", [
    (commands.GateFailed("too long\nno summary"), 1,
     "PR_DIAGNOSIS:a gate rejected the body; nothing was written: too long no summary"),
    (commands.Refused("gh failed:\n  first\n\n  second"), 2, "PR_DIAGNOSIS:gh failed: first second"),
    (RuntimeError("first\nsecond"), 2, "PR_DIAGNOSIS:pr render failed: RuntimeError: first second"),
])
def test_every_diagnosis_from_run_is_one_line(tmp_path, monkeypatch, capsys, logged, error, code, expected):
    def boom(args):
        raise error

    monkeypatch.setitem(commands.COMMANDS, "render", boom)
    repo, change = _setup(tmp_path)
    assert run(["render", "--repo", str(repo), "--change-dir", str(change)]) == code
    assert capsys.readouterr().out.splitlines() == [expected]


def test_a_listed_diagnosis_with_newlines_prints_as_one_line(capsys):
    commands._diagnose(["could not fetch: fatal: x\nfatal: y", "plain"])
    assert capsys.readouterr().out.splitlines() == [
        "PR_DIAGNOSIS:could not fetch: fatal: x fatal: y", "PR_DIAGNOSIS:plain"]


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


def test_render_from_a_subproject_reads_its_config_and_the_repo_root_templates(tmp_path, capsys):
    # buildme runs a monorepo subpackage with --repo <subproject>: its specs/config.yaml holds
    # the pr: block buildme setup wrote, while GitHub's templates live at the git root.
    repo, change = _setup(tmp_path)
    tdir = repo / ".github" / "PULL_REQUEST_TEMPLATE"
    tdir.mkdir(parents=True)
    (tdir / "bug.md").write_text("Bug intro.\n\n## Why\n")
    (tdir / "feature.md").write_text("Feature intro.\n\n## Why\n")
    sub = repo / "packages" / "api"
    (sub / "specs").mkdir(parents=True)
    assert run(["render", "--repo", str(sub), "--change-dir", str(change)]) == 0
    captured = capsys.readouterr()
    assert "several templates" in captured.err  # found at the git root, not under the subproject
    (sub / "specs" / "config.yaml").write_text("pr:\n  template: .github/PULL_REQUEST_TEMPLATE/feature.md\n")
    assert run(["render", "--repo", str(sub), "--change-dir", str(change)]) == 0
    captured = capsys.readouterr()
    assert "Feature intro." in captured.out and "several templates" not in captured.err


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
    assert comment.rstrip().endswith(markers.NOTICE), "buildme-findings skips a comment with it"
    for key in ("why", "blocks", "documentation", "companion-artifacts", "ticket", "size"):
        assert f"`{key}`" in comment


REPO_TEMPLATE = ("Thanks for opening a PR.\n<!-- Fill in every section. -->\n- [ ] I ran the tests\n\n"
                 "## Summary\n<!-- What changed. -->\n\n## Why\n<!-- The problem. -->\n\n"
                 "## Notes\n<!-- Anything else. -->\n")


def test_hand_opened_pr_from_the_repo_template_gets_only_the_missing_sections(tmp_path, gh_stub, model_stub,
                                                                               capsys):
    # Opened in the GitHub UI from the repo's own template: the preamble, the hint comments and the
    # headings already in the body must not be appended a second time.
    repo = make_repo(tmp_path / "proj")
    (repo / ".github").mkdir()
    (repo / ".github" / "pull_request_template.md").write_text(REPO_TEMPLATE)
    change = make_change(repo)
    model_stub.answers = [{"sections": {"summary": "Model summary.", "notes": "Model notes."}}] * 2
    existing = REPO_TEMPLATE.replace("## Summary\n<!-- What changed. -->\n",
                                     "## Summary\n<!-- What changed. -->\nI fixed the queue by hand.\n")
    existing = existing.replace("\n## Notes\n<!-- Anything else. -->\n", "")
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(existing)).on(["gh", "repo", "view"], stdout="WRITE\n")
    gh_stub.on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert existing.rstrip("\n") in new, "the existing text is unchanged"
    for once in ("Thanks for opening a PR.", "<!-- Fill in every section. -->", "- [ ] I ran the tests",
                 "## Summary", "<!-- What changed. -->", "## Why", "<!-- The problem. -->", "## Notes", "## Size"):
        assert new.count(once) == 1, f"{once!r} appears {new.count(once)} times"
    assert "Model notes." in new and "Model summary." not in new
    comment = gh_stub.bodies["gh pr comment"]
    assert "`notes`" in comment and "`size`" in comment
    assert "`summary`" not in comment and "`why`" not in comment

    # The next edit sees a managed body and must not append the sections it left out either.
    gh_stub.rules.clear()
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(new)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    again = gh_stub.bodies["gh pr edit"]
    for once in ("## Summary", "## Why", "## Notes", "## Size"):
        assert again.count(once) == 1, f"{once!r} appears {again.count(once)} times on the second edit"


def test_hand_opened_pr_with_a_ticked_preamble_box_does_not_get_the_preamble_again(tmp_path, gh_stub,
                                                                                   model_stub, capsys):
    # The author ticked the template's preamble checkbox in the GitHub UI: the preamble is still there.
    repo = make_repo(tmp_path / "proj")
    (repo / ".github").mkdir()
    (repo / ".github" / "pull_request_template.md").write_text(REPO_TEMPLATE)
    change = make_change(repo)
    model_stub.answers = [{"sections": {"summary": "Model summary.", "notes": "Model notes."}}] * 2
    existing = REPO_TEMPLATE.replace("- [ ] I ran the tests", "- [x] I ran the tests")
    existing = existing.replace("\n## Notes\n<!-- Anything else. -->\n", "")
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(existing)).on(["gh", "repo", "view"], stdout="WRITE\n")
    gh_stub.on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert existing.rstrip("\n") in new, "the existing text is unchanged"
    assert new.count("Thanks for opening a PR.") == 1
    assert new.count("<!-- Fill in every section. -->") == 1
    assert "- [ ] I ran the tests" not in new and new.count("- [x] I ran the tests") == 1


def test_hand_opened_pr_holding_every_section_gets_none_appended(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    existing = STANDARD_TEMPLATE.read_text()
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(existing)).on(["gh", "repo", "view"], stdout="WRITE\n")
    gh_stub.on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    new = gh_stub.bodies["gh pr edit"]
    assert new.splitlines().count("## Why") == 1 and new.splitlines().count("## Size") == 1
    assert "added no sections" in gh_stub.bodies["gh pr comment"]
    assert "PR_DIAGNOSIS:section already in the PR body, not appended: why" in capsys.readouterr().out


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


# --- edit writes only to the PR it was given -----------------------------------


@pytest.mark.parametrize("origin", ["https://github.com/me/repo.git", "git@github.com:me/repo.git"])
def test_edit_of_a_pr_in_another_repository_writes_nothing_and_exits_2(tmp_path, gh_stub, capsys, origin):
    repo, change = _setup(tmp_path)
    git(repo, "remote", "add", "origin", origin)
    pr = json.loads(_pr_json("mine")) | {"url": "https://github.com/other/repo/pull/7"}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "https://github.com/other/repo/pull/7", "--repo", str(repo),
                "--change-dir", str(change)]) == 2
    out = capsys.readouterr().out
    assert "is in other/repo, but this checkout's origin is me/repo" in out
    assert gh_stub.writes() == []
    assert [c[:3] for c in gh_stub.calls] == [["gh", "pr", "view"]], "refused before anything else"


def test_edit_writes_to_the_pr_url_not_its_number(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    git(repo, "remote", "add", "origin", "git@github.com:O/R.git")  # same repository, other case
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json("hand-written")).on(["gh", "repo", "view"], stdout="WRITE\n")
    gh_stub.on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    assert [c[:4] for c in gh_stub.writes()] == [["gh", "pr", "edit", URL], ["gh", "pr", "comment", URL]]


def test_edit_refuses_when_gh_gives_no_pr_url(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    pr = json.loads(_pr_json("mine"))
    del pr["url"]
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"]).on(["gh", "pr", "comment"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 2
    assert "no URL" in capsys.readouterr().out
    assert gh_stub.writes() == []


def _pr_head_on_origin(repo, tmp_path) -> str:
    """A PR head commit (1 file, +1 -0 against main) that exists only as origin's
    `refs/pull/7/head`, as a fork's PR does. Returns its sha."""
    make_bare_origin(repo, tmp_path / "origin.git")
    git(repo, "checkout", "-q", "-b", "contrib", "main")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "contrib")
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "push", "-q", "origin", "contrib:refs/pull/7/head")
    git(repo, "checkout", "-q", "feat/x")
    git(repo, "branch", "-q", "-D", "contrib")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    return sha


def test_edit_measures_the_prs_head_commit_not_a_same_named_local_branch(tmp_path, gh_stub, capsys):
    # The PR's head is a fork's `feat/x`; the local `feat/x` is a different branch. The size and
    # the file list must describe the PR, fetched from origin's refs/pull/<n>/head.
    repo, change = _setup(tmp_path)
    body = _managed_body(repo, change, capsys)
    sha = _pr_head_on_origin(repo, tmp_path)
    assert subprocess.run(["git", "cat-file", "-e", sha], cwd=repo).returncode != 0, "not fetched yet"
    pr = json.loads(_pr_json(body)) | {"headRefOid": sha}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    out = capsys.readouterr().out
    assert "DIFF:1 files, +1 -0" in out and "DIFF:2 files" not in out
    new = gh_stub.bodies["gh pr edit"]
    assert "1 files changed, +1 -0 lines" in new


def test_edit_refuses_to_measure_when_the_prs_head_cannot_be_fetched(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path, origin=True)
    body = _managed_body(repo, change, capsys)
    pr = json.loads(_pr_json(body)) | {"headRefName": "fork-only", "headRefOid": "1" * 40}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    out = capsys.readouterr().out
    assert "DIFF:" not in out
    assert "PR_DIAGNOSIS:could not fetch PR 7's head commit" in out
    assert "PR_DIAGNOSIS:diff size not measured" in out
    new = gh_stub.bodies["gh pr edit"]
    assert "2 files changed" not in new and "_Not measured:" in new


def test_edit_ignores_a_head_oid_that_is_not_a_commit_id(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path)
    body = _managed_body(repo, change, capsys)
    pr = json.loads(_pr_json(body)) | {"headRefOid": "--output=x"}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change)]) == 0
    out = capsys.readouterr().out
    assert "DIFF:2 files, +3 -1" in out and "could not fetch" not in out
    assert not (repo / "x").exists()


def test_edit_push_pushes_before_editing(tmp_path, gh_stub, capsys):
    repo, change = _setup(tmp_path, origin=True)
    body = _managed_body(repo, change, capsys)
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(body)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--push"]) == 0
    assert "PUSHED:feat/x" in capsys.readouterr().out
    assert git(tmp_path / "origin.git", "branch", "--list", "feat/x").strip()


# --- pushes go only to the PR's own branch; a branch name is never an option ------


@pytest.fixture
def pushes(monkeypatch):
    calls: list[str] = []
    real = github.push

    def push(repo, branch):
        calls.append(branch)
        return real(repo, branch)

    monkeypatch.setattr(github, "push", push)
    return calls


def test_push_argv_pushes_exactly_the_named_branch():
    assert github.push_argv("feat") == ["git", "push", "-u", "origin", "refs/heads/feat:refs/heads/feat"]


@pytest.mark.parametrize("head", ["--all", "-f", "bad..name", "@{-1}"])
def test_open_refuses_a_head_that_is_not_a_branch_name(tmp_path, gh_stub, capsys, pushes, head):
    repo, change = _setup(tmp_path, origin=True)
    git(repo, "checkout", "-q", "-b", "other")
    git(repo, "checkout", "-q", "feat/x")
    gh_stub.on(["gh", "repo", "view"], stdout="main\n").on(["gh", "pr", "create"], stdout=URL)
    assert run(["open", "--repo", str(repo), "--change-dir", str(change), f"--head={head}"]) == 2
    assert "is not a valid branch name" in capsys.readouterr().out
    assert pushes == [] and gh_stub.writes() == []
    assert git(tmp_path / "origin.git", "branch", "--list").strip() == "main"


def test_edit_refuses_a_pr_head_that_is_not_a_branch_name(tmp_path, gh_stub, capsys, pushes):
    repo, change = _setup(tmp_path, origin=True)
    pr = json.loads(_pr_json("mine")) | {"headRefName": "--all"}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--push"]) == 2
    assert "is not a valid branch name" in capsys.readouterr().out
    assert pushes == [] and gh_stub.writes() == []


def test_edit_push_of_a_fork_pr_pushes_and_edits_nothing(tmp_path, gh_stub, capsys, pushes):
    repo, change = _setup(tmp_path, origin=True)
    body = _managed_body(repo, change, capsys)
    pr = json.loads(_pr_json(body)) | {"isCrossRepository": True}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--push"]) == 2
    assert "head is on a fork; push it there yourself, then run edit without --push" in capsys.readouterr().out
    assert pushes == [] and gh_stub.writes() == []
    assert not git(tmp_path / "origin.git", "branch", "--list", "feat/x").strip()


def test_edit_push_refuses_when_the_head_branch_is_not_local(tmp_path, gh_stub, capsys, pushes):
    repo, change = _setup(tmp_path, origin=True)
    body = _managed_body(repo, change, capsys)
    pr = json.loads(_pr_json(body)) | {"headRefName": "elsewhere"}
    gh_stub.on(["gh", "pr", "view"], stdout=json.dumps(pr)).on(["gh", "pr", "edit"])
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--push"]) == 2
    assert "no local branch `elsewhere`" in capsys.readouterr().out
    assert pushes == [] and gh_stub.writes() == []


def test_edit_push_dry_run_prints_the_refspec(tmp_path, gh_stub, capsys, pushes):
    repo, change = _setup(tmp_path, origin=True)
    body = _managed_body(repo, change, capsys)
    gh_stub.on(["gh", "pr", "view"], stdout=_pr_json(body))
    assert run(["edit", "7", "--repo", str(repo), "--change-dir", str(change), "--push", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY_RUN:git push -u origin refs/heads/feat/x:refs/heads/feat/x" in out
    assert f"DRY_RUN:gh pr edit {URL} --body-file '<body-file>'" in out
    assert pushes == [] and gh_stub.writes() == []


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


def test_a_multi_line_error_while_starting_up_is_one_diagnosis_line(tmp_path, monkeypatch, capsys):
    import multiplai_core.env

    from pr_pipeline import __main__ as cli

    def boom(*a, **k):
        raise PermissionError("cannot read .env\nline two")

    monkeypatch.setattr(multiplai_core.env, "load_env", boom)
    repo, change = _setup(tmp_path)
    assert cli.main(["render", "--repo", str(repo), "--change-dir", str(change)]) == 2
    assert capsys.readouterr().out.splitlines() == [
        "PR_DIAGNOSIS:pr render failed to start: PermissionError: cannot read .env line two"]
