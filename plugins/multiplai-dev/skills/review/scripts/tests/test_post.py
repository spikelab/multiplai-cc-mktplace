"""`post`: which findings, where on the PR, what is skipped, and when nothing is posted."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import CLAIM_HIGH, CLAIM_MEDIUM
from review_pipeline import post as post_mod
from review_pipeline.__main__ import main
from review_pipeline.pr_marker import format_marker, parse_markers

# --- select ------------------------------------------------------------------------


def fd(
    fid: str,
    severity: str,
    topic: str | None = None,
    *,
    status: str = "confirmed",
    label: str | None = None,
    file: str = "a.py",
    line: int = 1,
) -> dict:
    row = {
        "id": fid,
        "severity": severity,
        "status": status,
        "claim": f"claim {fid}",
        "file": file,
        "line_start": line,
        "line_end": line,
        "failure_scenario": "f",
        "citations": [],
    }
    if topic is not None:
        row["topic"] = topic
    if label is not None:
        row["assessment"] = {"label": label}
    return row


def ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows]


def test_with_decisions_every_accepted_finding_posts_whatever_its_section_or_severity():
    findings = [
        fd("docslow000", "LOW", "docs"),
        fd("codehigh00", "HIGH", "code"),
        fd("testslow00", "LOW", "tests"),
    ]
    decisions = {
        "docslow000": {"decision": "accept"},
        "codehigh00": {"decision": "reject"},
        "testslow00": {"decision": "accept"},
    }
    assert ids(post_mod.select(findings, decisions)) == ["docslow000", "testslow00"]


def test_with_decisions_a_refuted_finding_is_not_posted_even_if_accepted():
    findings = [fd("refuted000", "HIGH", status="refuted")]
    assert post_mod.select(findings, {"refuted000": {"decision": "accept"}}) == []


@pytest.mark.parametrize(
    ("severity", "topic", "posted"),
    [
        ("HIGH", "code", True),
        ("MEDIUM", "code", True),
        ("LOW", "code", True),
        ("HIGH", "tests", True),
        ("MEDIUM", "tests", True),
        ("LOW", "tests", False),
        ("HIGH", "docs", True),
        ("MEDIUM", "docs", False),
        ("LOW", "docs", False),
        ("LOW", None, True),  # no topic is Code
        ("LOW", "process", True),  # any other topic is Code
    ],
)
def test_without_decisions_the_section_sets_the_severities(severity, topic, posted):
    assert bool(post_mod.select([fd("x000000000", severity, topic)], None)) is posted


@pytest.mark.parametrize("topic", [None, "tests", "docs"])
@pytest.mark.parametrize(
    "row",
    [
        {"label": "low-value"},
        {"label": "repeat"},
        {"status": "refuted"},
        {"status": "rejected"},
    ],
    ids=["low-value", "repeat", "refuted", "rejected"],
)
def test_without_decisions_what_the_review_does_not_list_is_never_posted(topic, row):
    assert post_mod.select([fd("x000000000", "HIGH", topic, **row)], None) == []


def test_posted_findings_are_sorted_by_severity_then_path_then_line():
    findings = [
        fd("l", "LOW", file="a.py"),
        fd("m2", "MEDIUM", file="b.py", line=9),
        fd("h", "HIGH", file="z.py"),
        fd("m1", "MEDIUM", file="b.py", line=2),
        fd("m0", "MEDIUM", file="a.py"),
    ]
    assert ids(post_mod.select(findings, None)) == ["h", "m0", "m1", "m2", "l"]


def test_the_section_table_is_the_one_constant():
    assert post_mod.POSTED_SEVERITIES_BY_SECTION == {
        "code": ("HIGH", "MEDIUM", "LOW"),
        "tests": ("HIGH", "MEDIUM"),
        "docs": ("HIGH",),
    }
    assert not hasattr(post_mod, "POSTED_SEVERITIES")


# --- commentable_lines ---------------------------------------------------------------

MODIFIED = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,5 @@
 one
-two
+TWO
+two and a half
 three
 four
@@ -20,3 +21,2 @@ def tail():
 twenty
--- a removed line that looks like a header
 twenty-two
\\ No newline at end of file
"""

RENAMED = """\
diff --git a/old/name.py b/new/name.py
similarity index 90%
rename from old/name.py
rename to new/name.py
index 3333333..4444444 100644
--- a/old/name.py
+++ b/new/name.py
@@ -2 +2 @@
-x = 1
+x = 2
"""

NO_HEAD_SIDE = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 5555555..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..6666666
Binary files /dev/null and b/logo.png differ
diff --git a/moved.py b/renamed.py
similarity index 100%
rename from moved.py
rename to renamed.py
"""

ADDED = """\
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..7777777
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+a
+b
+c
"""


def test_added_and_context_lines_are_commentable_and_removed_ones_are_not():
    lines = post_mod.commentable_lines(MODIFIED)
    # hunk 1: one(1) TWO(2) two-and-a-half(3) three(4) four(5); hunk 2: twenty(21) twenty-two(22)
    assert lines == {"app.py": {1, 2, 3, 4, 5, 21, 22}}


def test_a_removed_line_that_reads_like_a_header_does_not_change_the_file():
    assert "a removed line that looks like a header" not in post_mod.commentable_lines(
        MODIFIED
    )


def test_a_renamed_file_is_keyed_by_its_head_path():
    assert post_mod.commentable_lines(RENAMED) == {"new/name.py": {2}}


def test_deleted_binary_and_pure_rename_files_have_no_head_lines():
    assert post_mod.commentable_lines(NO_HEAD_SIDE) == {}


def test_several_files_in_one_diff():
    lines = post_mod.commentable_lines(NO_HEAD_SIDE + ADDED + MODIFIED)
    assert lines == {"new.py": {1, 2, 3}, "app.py": {1, 2, 3, 4, 5, 21, 22}}


def test_a_quoted_path_is_unquoted():
    diff = ADDED.replace("b/new.py", '"b/caf\\303\\251.py"')
    assert post_mod.commentable_lines(diff) == {"café.py": {1, 2, 3}}


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def pr_repo(tmp_path_factory):
    """`b/app.py` (40 lines) gets lines 5, 14 and 30 changed; `old.py` (30 lines) is renamed with one line changed."""
    repo = tmp_path_factory.mktemp("pr-repo")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "b").mkdir()
    (repo / "b" / "app.py").write_text("".join(f"line {n}\n" for n in range(1, 41)))
    (repo / "old.py").write_text("".join(f"kept {n}\n" for n in range(1, 31)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    app = (repo / "b" / "app.py").read_text().splitlines(keepends=True)
    for n in (5, 14, 30):
        app[n - 1] = f"changed {n}\n"
    (repo / "b" / "app.py").write_text("".join(app))
    _git(repo, "mv", "old.py", "new.py")
    moved = (repo / "new.py").read_text().splitlines(keepends=True)
    moved[9] = "changed 10\n"
    (repo / "new.py").write_text("".join(moved))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return repo, base, head


# GitHub's PR diff: 3 lines of context, hunks merged only when they touch, renames found.
GITHUB_LINES = {
    "b/app.py": set(range(2, 9)) | set(range(11, 18)) | set(range(27, 34)),
    "new.py": set(range(7, 14)),
}


def test_the_pr_diff_has_githubs_hunks(pr_repo):
    repo, base, head = pr_repo
    assert (
        post_mod.commentable_lines(post_mod.pr_diff(repo, base, head)) == GITHUB_LINES
    )


@pytest.mark.parametrize(
    "key, value",
    [
        ("diff.context", "10"),
        ("diff.interHunkContext", "5"),
        ("diff.algorithm", "patience"),
        ("diff.indentHeuristic", "false"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.renames", "false"),
    ],
)
def test_a_users_diff_setting_does_not_change_the_commentable_lines(
    pr_repo, key, value
):
    repo, base, head = pr_repo
    _git(repo, "config", key, value)
    try:
        assert (
            post_mod.commentable_lines(post_mod.pr_diff(repo, base, head))
            == GITHUB_LINES
        )
    finally:
        _git(repo, "config", "--unset", key)


# --- the review payload --------------------------------------------------------------


def _edit(target_dir, fid: str, **update) -> None:
    """Change one finding in the review's findings.json (post reads the findings from there)."""
    path = target_dir / "findings.json"
    data = json.loads(path.read_text())
    for row in data["findings"]:
        if row["id"] == fid:
            row.update(update)
    path.write_text(json.dumps(data))


def _accept(target_dir, *fids: str):
    path = target_dir / "viewer" / "decisions.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                f: {"decision": "accept", "note": "", "ts": "2026-10-11T10:00:00Z"}
                for f in fids
            }
        )
    )
    return path


def _post(target_dir, decisions=None) -> int:
    argv = ["post", str(target_dir)] + (
        ["--decisions", str(decisions)] if decisions else []
    )
    return main(argv)


def test_one_review_on_the_head_commit_with_event_comment(pr_review, fake_gh, capsys):
    state, target_dir = pr_review
    high = state.findings[0]
    assert _post(target_dir, _accept(target_dir, high.id)) == 0
    gets = [argv for argv, _ in fake_gh if "--paginate" in argv]
    assert [a[-1] for a in gets] == [
        "repos/{owner}/{repo}/pulls/812/comments",
        "repos/{owner}/{repo}/pulls/812/reviews",
    ]
    ((argv, payload),) = fake_gh.posts()
    assert argv[1:] == [
        "api",
        "-X",
        "POST",
        "repos/{owner}/{repo}/pulls/812/reviews",
        "--input",
        "-",
    ]
    assert payload["commit_id"] == state.target.head_sha
    assert payload["event"] == "COMMENT"
    assert "Generated with multiplai-dev:review" in payload["body"]
    assert "as one review: 1 inline, 0 outside the diff" in capsys.readouterr().out


def test_a_single_line_in_the_diff_is_an_inline_comment_on_the_right_side(
    pr_review, fake_gh
):
    state, target_dir = pr_review
    high = state.findings[0]
    assert _post(target_dir, _accept(target_dir, high.id)) == 0
    ((_, payload),) = fake_gh.posts()
    (comment,) = payload["comments"]
    assert (
        comment["path"] == "rateplan_service.py"
        and comment["line"] == 1
        and comment["side"] == "RIGHT"
    )
    assert "start_line" not in comment and "start_side" not in comment
    assert comment["body"].startswith(f"**HIGH** — {CLAIM_HIGH}")
    assert comment["body"].splitlines()[-1] == format_marker(
        high.id, state.target.head_sha[:12]
    )
    assert payload["body"].startswith(
        "Code review: 1 finding, 1 inline, 0 on lines outside this PR's diff"
    )
    assert parse_markers(payload["body"]) == []


def test_a_range_in_the_diff_gets_start_line_and_start_side(pr_review, fake_gh):
    state, target_dir = pr_review
    high = state.findings[0]
    _edit(target_dir, high.id, line_start=1, line_end=6)
    assert _post(target_dir, _accept(target_dir, high.id)) == 0
    ((_, payload),) = fake_gh.posts()
    (comment,) = payload["comments"]
    assert (
        comment["start_line"],
        comment["start_side"],
        comment["line"],
        comment["side"],
    ) == (1, "RIGHT", 6, "RIGHT")


def test_a_finding_with_any_line_outside_the_diff_goes_in_the_review_body(
    pr_review, fake_gh
):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    _edit(
        target_dir, high.id, line_start=6, line_end=40
    )  # rateplan_service.py has 6 lines
    _edit(target_dir, medium.id, file="README.md")  # unchanged by the PR
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    ((_, payload),) = fake_gh.posts()
    assert payload["comments"] == []
    body = payload["body"]
    assert body.startswith(
        "Code review: 2 findings, 0 inline, 2 on lines outside this PR's diff"
    )
    assert f"1. {CLAIM_HIGH} (HIGH)" in body and f"2. {CLAIM_MEDIUM} (LOW)" in body
    head = state.target.head_sha
    assert (
        f"https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L1-L2"
        in body
    )
    lines = body.splitlines()
    link_at = lines.index(
        f"https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L1-L2"
    )
    assert lines[link_at + 1] == format_marker(high.id, head[:12])
    assert [m["id"] for m in parse_markers(body)] == [high.id, medium.id]


def test_inline_and_outside_findings_share_one_review(pr_review, fake_gh, capsys):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    _edit(target_dir, medium.id, file="README.md")
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    ((_, payload),) = fake_gh.posts()
    assert [c["path"] for c in payload["comments"]] == ["rateplan_service.py"]
    assert [m["id"] for m in parse_markers(payload["body"])] == [medium.id]
    assert "1 inline, 1 outside the diff" in capsys.readouterr().out


def test_the_marker_names_the_round_this_review_will_be_kept_as(pr_review, fake_gh):
    state, target_dir = pr_review
    high = state.findings[0]
    sha12 = state.target.head_sha[:12]
    kept = target_dir / "rounds" / sha12
    kept.mkdir(parents=True)
    (kept / "findings.json").write_text(
        json.dumps({"generated_at": "2026-01-01T00:00:00Z"})
    )
    assert _post(target_dir, _accept(target_dir, high.id)) == 0
    ((_, payload),) = fake_gh.posts()
    assert parse_markers(payload["comments"][0]["body"])[0]["round"] == f"{sha12}-2"


# --- skipping what is already on the PR ----------------------------------------------


def _marked(fid: str, round_id: str = "111111111111") -> str:
    return f"An earlier finding\n\n{format_marker(fid, round_id)}"


def test_a_finding_already_in_a_review_comment_is_skipped(pr_review, fake_gh, capsys):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    fake_gh.respond(
        "pulls/812/comments",
        json.dumps([{"body": _marked(high.id)}, {"body": "a human comment"}]),
    )
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    assert (
        CLAIM_HIGH not in fake_gh.posted_text()
        and CLAIM_MEDIUM in fake_gh.posted_text()
    )
    assert f"skipped 1 already posted ({high.id})" in capsys.readouterr().out


def test_a_finding_already_in_a_review_body_is_skipped(pr_review, fake_gh):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    fake_gh.respond(
        "pulls/812/reviews", json.dumps([{"body": None}, {"body": _marked(medium.id)}])
    )
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    assert (
        CLAIM_HIGH in fake_gh.posted_text()
        and CLAIM_MEDIUM not in fake_gh.posted_text()
    )


def test_markers_are_read_from_every_page(pr_review, fake_gh):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    pages = (
        json.dumps([{"body": "page one"}])
        + "\n"
        + json.dumps([{"body": _marked(medium.id)}])
    )
    fake_gh.respond("pulls/812/comments", pages)
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    assert CLAIM_MEDIUM not in fake_gh.posted_text()


def test_a_still_open_finding_whose_earlier_finding_was_posted_is_skipped(
    pr_review, fake_gh, capsys
):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    _edit(
        target_dir,
        medium.id,
        assessment={
            "label": "still-open",
            "reason": "r",
            "earlier_id": "abcdef0123",
            "earlier_round": "111111111111",
        },
    )
    fake_gh.respond("pulls/812/comments", json.dumps([{"body": _marked("abcdef0123")}]))
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    assert CLAIM_MEDIUM not in fake_gh.posted_text()
    assert (
        f"1 still open from an earlier round whose finding was already posted ({medium.id})"
        in capsys.readouterr().out
    )


def test_a_still_open_finding_whose_earlier_finding_was_not_posted_is_posted(
    pr_review, fake_gh
):
    state, target_dir = pr_review
    medium = state.findings[1]
    _edit(
        target_dir,
        medium.id,
        assessment={
            "label": "still-open",
            "reason": "r",
            "earlier_id": "abcdef0123",
            "earlier_round": "111111111111",
        },
    )
    assert _post(target_dir, _accept(target_dir, medium.id)) == 0
    assert CLAIM_MEDIUM in fake_gh.posted_text()


def test_when_everything_was_posted_already_nothing_is_posted(
    pr_review, fake_gh, capsys
):
    state, target_dir = pr_review
    high, medium = state.findings[0], state.findings[1]
    fake_gh.respond(
        "pulls/812/comments",
        json.dumps([{"body": _marked(high.id)}, {"body": _marked(medium.id)}]),
    )
    assert _post(target_dir, _accept(target_dir, high.id, medium.id)) == 0
    assert fake_gh.posts() == []
    out = capsys.readouterr().out
    assert out.startswith("nothing to post: skipped 2 already posted")


# --- refusals: exit 2 and no POST ----------------------------------------------------


@pytest.mark.parametrize("what", ["comments", "reviews"])
def test_a_failed_read_of_the_pr_posts_nothing(pr_review, fake_gh, capsys, what):
    state, target_dir = pr_review
    fake_gh.respond(f"pulls/812/{what}", returncode=1, stderr="HTTP 502")
    assert _post(target_dir, _accept(target_dir, state.findings[0].id)) == 2
    assert fake_gh.posts() == []
    err = capsys.readouterr().err
    assert "HTTP 502" in err and "nothing was posted" in err


def test_an_unreadable_answer_from_the_pr_posts_nothing(pr_review, fake_gh):
    state, target_dir = pr_review
    fake_gh.respond("pulls/812/comments", "<html>not json</html>")
    assert _post(target_dir, _accept(target_dir, state.findings[0].id)) == 2
    assert fake_gh.posts() == []


def test_a_failed_post_exits_2_and_is_not_retried(pr_review, fake_gh, capsys):
    state, target_dir = pr_review
    fake_gh.respond("-X POST", returncode=1, stderr="HTTP 422: Validation Failed")
    assert (
        _post(
            target_dir, _accept(target_dir, state.findings[0].id, state.findings[1].id)
        )
        == 2
    )
    assert len(fake_gh.posts()) == 1
    assert "HTTP 422: Validation Failed" in capsys.readouterr().err


def test_a_remote_not_on_github_is_refused_before_any_gh_call(
    pr_review, fake_gh, capsys
):
    state, target_dir = pr_review
    path = target_dir / "findings.json"
    data = json.loads(path.read_text())
    data["target"]["remote_url"] = "https://gitlab.example.com/x/y.git"
    path.write_text(json.dumps(data))
    assert _post(target_dir, _accept(target_dir, state.findings[0].id)) == 2
    assert fake_gh == []
    assert "not on GitHub" in capsys.readouterr().err
