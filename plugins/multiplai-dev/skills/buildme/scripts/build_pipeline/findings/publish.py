"""Push a round's commits, refresh the PR body, and reply on the PR.

Order is the guarantee: the branch is pushed first, and nothing is written to
GitHub unless the push succeeded, so no reply names a commit the remote does
not have. The PR body is refreshed only through the pr skill's `edit`
(`pr_pipeline edit <pr> --section <file>`), with a "Review rounds" section
covering every round so far. Each reply is recorded in the round's state
(`save()`) before the next one is sent, so a resume never posts twice.

Review threads are never resolved: the pipeline only replies. Every reply ends
with `fetch.REPLY_MARKER`, so the next fetch skips it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from multiplai_core.untrusted import defang

from .. import git_ops
from ..state import FindingsRound, FindingsState
from .fetch import REPLY_MARKER
from .models import PRComment, TriageRecord, Verdict
from .triage import should_triage

log = logging.getLogger(__name__)

SECTION_FILE = "review-rounds.md"
SECTION_HEADING = "## Review rounds"


class PublishError(RuntimeError):
    """The push or a reply failed. The round's publish step stays unfinished,
    so a resume retries from the first reply not yet sent."""


def _line(text: str, limit: int = 600) -> str:
    return " ".join(defang(text or "", limit).split())


def _short(sha: str | None) -> str:
    return (sha or "")[:12]


def records_of(rnd: FindingsRound) -> list[TriageRecord]:
    """The round's comments as `TriageRecord`s, rebuilt from its state."""
    out = []
    for c in rnd.comments:
        act, note = should_triage(c)
        t = rnd.triaged.get(c.id) if act else None
        out.append(
            TriageRecord(
                round=rnd.number, comment=c, triage=t, note=note if t is None else ""
            )
        )
    return out


def _spec_lines(rnd: FindingsRound, key: str) -> list[str]:
    """What the spec step recorded for *key*: buildme never edits
    requirements, so a requirements change waits on `pm-spec revise`, and so
    does a code fix that contradicts the current requirement."""
    lines = []
    for s in rnd.spec_issues:
        if s.finding_key != key:
            continue
        what = _line(s.requirement, 200) or "the requirement"
        commit = f" (commit `{_short(rnd.spec_commit)}`)" if rnd.spec_commit else ""
        recorded = (
            f"{'Recorded' if s.appended else 'Already recorded'} as an issue in "
            f"`{_line(s.issues_file, 200)}`{commit}"
        )
        waits = f"`{_line(s.command, 200)}` makes the edit; buildme does not edit requirements."
        if s.defers_fix:
            lines.append(
                f"Not fixed this round: the fix contradicts {what}, so it waits on "
                f"the requirements change. {recorded}; {waits}"
            )
        else:
            lines.append(
                f"The requirements change for {what} waits on `pm-spec revise`. "
                f"{recorded}; {waits}"
            )
    return lines


def outcome(rnd: FindingsRound, record: TriageRecord, *, searched: bool = True) -> str:
    """What happened to one acted-on comment, in one or two sentences. With
    *searched* False (the PR body's section, which lists every round under the
    pr skill's word limit), the fixer's search is left out."""
    t = record.triage
    assert t is not None
    key = record.comment.key
    parts = _spec_lines(rnd, key)
    if t.verdict == Verdict.CODE_FIX and t.spec_conflict:
        if not parts:
            parts.append(
                "Not fixed: the fix contradicts the current requirements, which "
                "buildme does not edit, and no requirements issue was recorded."
            )
    elif t.verdict == Verdict.CODE_FIX:
        fix = rnd.fixes.get(key)
        if fix and fix.get("impl"):
            test = (
                f" (failing test first in `{_short(fix.get('test'))}`)"
                if fix.get("test")
                else ""
            )
            parts.append(f"Fixed in `{_short(fix['impl'])}`{test}.")
            if searched and fix.get("searched"):
                parts.append(
                    f"Searched for the same mistake: {_line(fix['searched'], 600)}"
                )
        elif fix and fix.get("failed"):
            parts.append(f"Not fixed: {fix['failed']}.")
        else:
            parts.append("Not fixed: the fix did not produce a commit.")
    elif t.verdict == Verdict.SPEC_CHANGE and not parts:
        parts.append("No requirements issue was recorded.")
    elif t.verdict == Verdict.QUESTION:
        parts.append("Question; nothing was changed.")
    elif t.verdict == Verdict.NOT_DEFECT:
        parts.append("Not a defect; nothing was changed.")
    return " ".join(parts)


def reply_body(rnd: FindingsRound, record: TriageRecord) -> str:
    t = record.triage
    assert t is not None
    return (
        f"{outcome(rnd, record)}\n\n{_line(t.reason, 1500)}\n\n"
        f"_buildme findings, round {rnd.number}._\n\n{REPLY_MARKER}"
    )


def issue_reply_body(rnd: FindingsRound, records: list[TriageRecord]) -> str:
    lines = [
        f"buildme findings, round {rnd.number}: the comments outside review threads.",
        "",
    ]
    for r in records:
        c = r.comment
        ident = f"finding {c.finding_id}" if c.finding_id else c.label
        link = f"[{ident}]({c.url})" if c.url else ident
        lines.append(
            f"- {link} by @{c.author}: {outcome(rnd, r)} {_line(r.triage.reason, 600)}"
        )
    return "\n".join(lines) + f"\n\n{REPLY_MARKER}"


def render_section(fs: FindingsState) -> str:
    """The PR body's "Review rounds" section: every round, each comment's
    verdict and outcome, and every requirements issue it recorded."""
    out = [SECTION_HEADING, ""]
    for rnd in fs.rounds:
        records = records_of(rnd)
        acted = [r for r in records if r.acted_on]
        out += [f"### Round {rnd.number}", ""]
        if acted:
            out += ["| Comment | Verdict | Outcome |", "|---|---|---|"]
            for r in acted:
                c = r.comment
                ident = f"finding {c.finding_id}" if c.finding_id else c.label
                link = f"[{ident}]({c.url})" if c.url else ident
                out.append(
                    f"| {link} | {r.triage.verdict.value} | "
                    f"{outcome(rnd, r, searched=False).replace('|', '/')} |"
                )
        check = rnd.self_check or {}
        if check.get("block"):
            # Not a comment, so no reply answers it: this is where it shows.
            fix = rnd.fixes.get(f"block:{check['block']}") or {}
            if fix.get("impl"):
                done = f"Fixed in `{_short(fix['impl'])}`."
            elif fix.get("failed"):
                done = f"Not fixed: {fix['failed']}."
            else:
                done = "Not fixed: the fix did not produce a commit."
            out += [
                "",
                f"The check of this round's fixes found: "
                f"{_line(check.get('report', ''), 600)} {done}",
            ]
        skipped = len(records) - len(acted)
        if skipped:
            out += [
                "",
                f"{skipped} comment(s) not acted on; `findings-r{rnd.number}.md` "
                "in the change directory says why.",
            ]
        if rnd.spec_issues:
            first = rnd.spec_issues[0]
            commit = f" (commit `{_short(rnd.spec_commit)}`)" if rnd.spec_commit else ""
            out += [
                "",
                f"Requirements issues recorded in `{_line(first.issues_file, 200)}`"
                f"{commit}, waiting on `{_line(first.command, 200)}`:",
            ]
            out += [
                f"- {_line(s.requirement, 160) or '(requirement not named)'}, for "
                f"{s.finding_key}"
                + (" (its code fix is deferred)" if s.defers_fix else "")
                for s in rnd.spec_issues
            ]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_section(change_dir: Path, fs: FindingsState) -> Path:
    """Under `pr-sections/`, which no commit ever includes."""
    path = Path(change_dir) / "pr-sections" / SECTION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_section(fs))
    return path


def pr_edit_argv(
    project_dir: Path, change_dir: Path, pr: int, section: Path
) -> list[str]:
    """`pr_pipeline edit`, run as a subprocess the way publish runs `open`."""
    from ..orchestrator import _pr_skill_dir

    return [
        "uv",
        "run",
        "--directory",
        str(_pr_skill_dir()),
        "python",
        "-m",
        "pr_pipeline",
        "edit",
        str(pr),
        "--repo",
        str(project_dir),
        "--change-dir",
        str(change_dir),
        "--section",
        str(section),
    ]


def update_body(
    project_dir: Path, change_dir: Path, pr: int, fs: FindingsState
) -> tuple[bool, list[str]]:
    """(edited, its PR_DIAGNOSIS lines). The pr skill prints PR_DIAGNOSIS
    lines after a successful edit too (a section it kept as it was), so the
    lines say why the refresh failed only when *edited* is False. Never raises."""
    section = write_section(change_dir, fs)
    argv = pr_edit_argv(project_dir, change_dir, pr, section)
    res = git_ops._run_pr_skill(argv, cwd=project_dir)
    diagnoses = [
        line[len("PR_DIAGNOSIS:") :].strip()
        for line in (res.stdout or "").splitlines()
        if line.startswith("PR_DIAGNOSIS:")
    ]
    if not res.ok:
        diagnoses.append(
            f"pr skill edit exited {res.returncode}: "
            f"{' '.join((res.stderr or '').split())[-400:]}"
        )
    return res.ok, diagnoses


def _reply_review(
    repo: str, pr: int, comment: PRComment, body: str, cwd: Path
) -> git_ops.GitResult:
    target = comment.reply_to or comment.id
    return git_ops._run_gh(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/pulls/{pr}/comments/{target}/replies",
            "-f",
            f"body={body}",
            "--jq",
            ".id",
        ],
        cwd=cwd,
    )


def _post_issue_comment(repo: str, pr: int, body: str, cwd: Path) -> git_ops.GitResult:
    return git_ops._run_gh(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/issues/{pr}/comments",
            "-f",
            f"body={body}",
            "--jq",
            ".id",
        ],
        cwd=cwd,
    )


def publish_round(
    fs: FindingsState,
    rnd: FindingsRound,
    *,
    project_dir: Path,
    change_dir: Path,
    save: Callable[[], None],
    emit: Callable[[str], None] = print,
) -> None:
    """Push, refresh the body, reply. Raises `PublishError` when the push or a
    reply fails; a body refresh that fails is reported and does not stop the
    replies (the commits they name are already pushed)."""
    head = git_ops.rev_parse_head(project_dir)
    if not head:
        raise PublishError(f"cannot read HEAD in {project_dir}")
    if rnd.pushed_head != head:
        res = git_ops.push_branch(project_dir, fs.branch)
        if not res.ok:
            raise PublishError(
                f"push of {fs.branch} failed, so nothing was posted: "
                f"{' '.join((res.stderr or res.stdout).split())[:400]}"
            )
        rnd.pushed_head = head
        save()
        emit(f"FINDINGS:PUSHED:{fs.branch}:{_short(head)}")

    if not rnd.body_updated:
        ok, diagnoses = update_body(project_dir, change_dir, fs.pr, fs)
        # FINDINGS_DIAGNOSIS means the description was not refreshed; a
        # successful edit's lines are notes about what it kept.
        for d in diagnoses:
            emit(f"FINDINGS_NOTE:{d}" if ok else f"FINDINGS_DIAGNOSIS:{d}")
        if ok:
            rnd.body_updated = True
            save()

    records = [r for r in records_of(rnd) if r.acted_on]
    for r in records:
        c = r.comment
        if c.kind != "review":
            continue
        key = f"review:{c.id}"
        if key in rnd.replies_sent:
            continue
        res = _reply_review(fs.repo, fs.pr, c, reply_body(rnd, r), project_dir)
        if not res.ok:
            raise PublishError(
                f"reply to review comment {c.id} failed: "
                f"{' '.join((res.stderr or res.stdout).split())[:400]}"
            )
        rnd.replies_sent.append(key)
        save()
    # Top-level comments and review bodies have no thread to reply in.
    issue = [r for r in records if r.comment.kind != "review"]
    key = f"issue:r{rnd.number}"
    if issue and key not in rnd.replies_sent:
        res = _post_issue_comment(
            fs.repo, fs.pr, issue_reply_body(rnd, issue), project_dir
        )
        if not res.ok:
            raise PublishError(
                f"the round's top-level reply failed: "
                f"{' '.join((res.stderr or res.stdout).split())[:400]}"
            )
        rnd.replies_sent.append(key)
        save()
    emit(f"FINDINGS:REPLIED:{rnd.number}:{len(rnd.replies_sent)}")
