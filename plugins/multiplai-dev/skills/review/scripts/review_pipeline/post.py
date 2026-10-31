"""`post`: one PR review with one inline comment per finding.

Which findings: with `--decisions`, every finding whose recorded decision is
`accept` (and whose status is shown), whatever its severity or section.
Without it, the findings `render.listed` keeps for the review's mode (so no
`repeat` or `low-value` finding), filtered by section: Code at every
severity, Tests HIGH and MEDIUM, Docs HIGH (`POSTED_SEVERITIES_BY_SECTION`).
The viewer keeps one entry per finding id, so the entry is the latest
decision.

Where: a finding whose lines are all in the PR's diff (`commentable_lines`,
from the same base..head diff GitHub shows) becomes an inline comment on those
lines, `side: RIGHT`, since citations are head lines only. Any other finding
goes in the review's body with a link to its lines.

Each finding carries the hidden `pr_marker` marker. Before posting, the PR's
review comments and review bodies are read; a finding whose id is already
marked there, or a `still-open` finding whose `earlier_id` is, is skipped. If
either read fails, nothing is posted. Everything goes in one
`POST .../pulls/<n>/reviews` with event `COMMENT`; a failed POST is not
retried comment by comment. The skill runs this only on an explicit yes typed
in the terminal.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from multiplai_core.log_utils import log_event

from .pr_marker import format_marker, parse_markers
from .render import SHOWN_STATUSES, class_line, listed, section
from .rounds import round_name
from .state import load_state
from .target import _DIFF_FLAGS, TargetError, git, github_web_base

log = logging.getLogger(__name__)

# Keyed by the names `render.section` returns. Applies only without --decisions.
# A section left out (`trim`, advisory) posts nothing unless accepted in a decisions file.
POSTED_SEVERITIES_BY_SECTION: dict[str, tuple[str, ...]] = {
    "code": ("HIGH", "MEDIUM", "LOW"),
    "tests": ("HIGH", "MEDIUM"),
    "docs": ("HIGH",),
}
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PostError(Exception):
    """Refused or failed; the message says why. Exit code 2."""


def select(
    findings: list[dict], decisions: dict | None, mode: str | None = None
) -> list[dict]:
    """With *decisions*, every shown finding accepted there; else what `render.listed` lists, at the
    severities `POSTED_SEVERITIES_BY_SECTION` posts for its section. Sorted by severity, path, line."""
    if decisions is not None:
        chosen = [
            f
            for f in findings
            if f["status"] in SHOWN_STATUSES
            and (decisions.get(f["id"]) or {}).get("decision") == "accept"
        ]
    else:
        chosen = [
            f
            for f in listed(findings, mode)
            if f["severity"] in POSTED_SEVERITIES_BY_SECTION.get(section(f), ())
        ]
    return sorted(
        chosen,
        key=lambda f: (
            SEVERITY_ORDER.get(f["severity"], len(SEVERITY_ORDER)),
            f.get("file") or "",
            f.get("line_start") or 0,
        ),
    )


def _unquote(path: str) -> str:
    """A path as git prints it: C-quoted (octal escapes for non-ASCII) when it holds special characters."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        return (
            path[1:-1]
            .encode("latin-1")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8")
        )
    return path


def _new_path(line: str) -> str | None:
    """The head path from a `+++ ` header, or None for `/dev/null` (a deleted file)."""
    name = line[4:]
    if name.endswith("\t"):  # git appends a tab when the name holds a space
        name = name[:-1]
    name = _unquote(name)
    if name == "/dev/null":
        return None
    return name.removeprefix("b/")


# GitHub's PR diff, whatever the user's diff.* settings say: 3 lines of context,
# hunks merged only when they touch, the default algorithm and indent heuristic,
# renames found, a/ and b/ prefixes. A wider local hunk would send a comment
# GitHub refuses (422), and the one POST would then post nothing.
_PR_DIFF_FLAGS = [
    "-U3",
    "--inter-hunk-context=0",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]


def pr_diff(repo: str | Path, base: str, head: str) -> str:
    """The base..head diff with GitHub's hunks, for `commentable_lines`. The review finders keep `diff_text`."""
    return git(repo, "diff", *_DIFF_FLAGS, *_PR_DIFF_FLAGS, f"{base}..{head}").stdout


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Per head path, every head-side line number inside a hunk: added and context lines.

    Removed lines have no head line. A deleted file, a binary file and a
    rename without content changes have no hunk on the head side, so no entry.
    Hunk lines are counted from the `@@` header, so a removed line that reads
    `--- ...` is never taken for a file header.
    """
    out: dict[str, set[int]] = {}
    current: str | None = None
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            current = _new_path(line)
        elif (m := _HUNK_RE.match(line)) is not None:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_line = int(m.group(2))
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            seen = out.setdefault(current, set()) if current is not None else set()
            while (old_left > 0 or new_left > 0) and i < len(lines):
                body = lines[i]
                i += 1
                tag = body[:1]
                if tag == "\\":  # "\ No newline at end of file"
                    continue
                if tag == "-":
                    old_left -= 1
                    continue
                if tag == "+":
                    new_left -= 1
                else:  # context: " " (or an empty line from a stripped trailing space)
                    old_left -= 1
                    new_left -= 1
                seen.add(new_line)
                new_line += 1
            if current is not None and not seen:
                del out[current]
    return out


def link(web_base: str, head_sha: str, path: str, start: int, end: int) -> str:
    """Full-sha link with one line of context each side, as the code-review plugin requires."""
    return f"{web_base}/blob/{head_sha}/{path}#L{max(1, start - 1)}-L{end + 1}"


def _text(s: str | None) -> str:
    return " ".join((s or "").split())


def _span(f: dict) -> tuple[int, int]:
    start = int(f["line_start"])
    return start, max(start, int(f.get("line_end") or start))


def inline_comment(f: dict, lines: dict[str, set[int]], round_id: str) -> dict | None:
    """The `comments[]` entry for *f*, or None when any of its lines is outside the diff."""
    start, end = _span(f)
    have = lines.get(f["file"], set())
    if any(n not in have for n in range(start, end + 1)):
        return None
    body = [f"**{f['severity']}** — {_text(f['claim'])}", ""]
    if class_line(f):
        body += [class_line(f), ""]
    body += [_text(f["failure_scenario"]), ""]
    if f.get("expected_behaviour"):
        body += [f"Expected behaviour: {_text(f['expected_behaviour'])}", ""]
    body.append(format_marker(f["id"], round_id))
    comment = {"path": f["file"], "line": end, "side": "RIGHT", "body": "\n".join(body)}
    if end != start:
        comment["start_line"] = start
        comment["start_side"] = "RIGHT"
    return comment


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def review_body(
    web: str, head_sha: str, total: int, inline: int, outside: list[dict], round_id: str
) -> str:
    """The review's top-level body: a count line, then each finding outside the diff with its link and marker."""
    out = [
        f"Code review: {_plural(total, 'finding')}, {inline} inline, "
        f"{len(outside)} on lines outside this PR's diff",
        "",
    ]
    for i, f in enumerate(outside, 1):
        out.append(f"{i}. {_text(f['claim'])} ({f['severity']})")
        out.append("")
        if class_line(f):
            out.append(class_line(f))
            out.append("")
        out.append(_text(f["failure_scenario"]))
        out.append("")
        if f.get("expected_behaviour"):
            out.append(f"Expected behaviour: {_text(f['expected_behaviour'])}")
            out.append("")
        c = f["citations"][0]
        out.append(link(web, head_sha, c["path"], c["line_start"], c["line_end"]))
        out.append(format_marker(f["id"], round_id))
        out.append("")
    out.append("🤖 Generated with multiplai-dev:review")
    return "\n".join(out) + "\n"


def build_payload(
    findings_file: dict, chosen: list[dict], lines: dict[str, set[int]], round_id: str
) -> dict:
    """The JSON for `POST .../pulls/<n>/reviews`: event COMMENT on the head commit."""
    t = findings_file["target"]
    web = github_web_base(t.get("remote_url"))
    if not web:
        raise PostError(
            f"the review's remote ({t.get('remote_url')}) is not on GitHub; post needs a GitHub PR"
        )
    comments, outside = [], []
    for f in chosen:
        c = inline_comment(f, lines, round_id)
        if c is None:
            outside.append(f)
        else:
            comments.append(c)
    return {
        "commit_id": t["head_sha"],
        "event": "COMMENT",
        "body": review_body(
            web, t["head_sha"], len(chosen), len(comments), outside, round_id
        ),
        "comments": comments,
    }


def _json_values(text: str) -> list:
    """Every JSON value in *text*: `gh api --paginate` prints one array per page, back to back."""
    dec, values, i = json.JSONDecoder(), [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return values
        value, i = dec.raw_decode(text, i)
        values.append(value)


def posted_ids(gh: str, repo_path: str, pr: int) -> set[str]:
    """Finding ids already marked in the PR's review comments and review bodies. Raises PostError on failure."""
    ids: set[str] = set()
    for what in ("comments", "reviews"):
        proc = subprocess.run(
            [gh, "api", "--paginate", f"repos/{{owner}}/{{repo}}/pulls/{pr}/{what}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            raise PostError(
                f"could not read the PR's existing review {what} (gh api failed: "
                f"{proc.stderr.strip()}); nothing was posted"
            )
        try:
            pages = _json_values(proc.stdout)
        except ValueError as e:
            raise PostError(
                f"could not read the PR's existing review {what}: {e}; nothing was posted"
            ) from e
        for page in pages:
            for item in page if isinstance(page, list) else [page]:
                if isinstance(item, dict):
                    ids.update(m["id"] for m in parse_markers(item.get("body") or ""))
    return ids


def drop_posted(
    chosen: list[dict], posted: set[str]
) -> tuple[list[dict], list[str], list[str]]:
    """(to post, ids already posted, ids of still-open findings whose earlier finding was posted)."""
    keep, again, earlier = [], [], []
    for f in chosen:
        a = f.get("assessment") or {}
        if f["id"] in posted:
            again.append(f["id"])
        elif a.get("label") == "still-open" and a.get("earlier_id") in posted:
            earlier.append(f["id"])
        else:
            keep.append(f)
    return keep, again, earlier


def _skip_note(again: list[str], earlier: list[str]) -> str:
    parts = []
    if again:
        parts.append(f"{len(again)} already posted ({', '.join(again)})")
    if earlier:
        parts.append(
            f"{len(earlier)} still open from an earlier round whose finding was already posted "
            f"({', '.join(earlier)})"
        )
    return f"skipped {' and '.join(parts)}" if parts else ""


def post(target_dir: Path, decisions_path: Path | None, *, session_id: str = "") -> str:
    """Post the review; return a one-line summary. Raises PostError to refuse."""
    target_dir = target_dir.resolve()
    state = load_state(target_dir / "review-state.json")
    if state is None:
        raise PostError(f"no review-state.json in {target_dir}; run a review first")
    t = state.target
    if t.pr is None:
        raise PostError(
            f"post needs a PR target; this review is of {t.kind} {t.ref}. "
            f"Re-run the review with --pr <number> to post it."
        )
    decisions = None
    if decisions_path is not None:
        if not decisions_path.is_file():
            raise PostError(f"decisions file not found: {decisions_path}")
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    findings_file = json.loads(
        (target_dir / "findings.json").read_text(encoding="utf-8")
    )
    reviewed = findings_file["target"]
    if (reviewed.get("base_sha"), reviewed.get("head_sha")) != (t.base_sha, t.head_sha):
        # A later run rewrote review-state.json and stopped before writing its findings.json.
        raise PostError(
            f"review-state.json ({t.head_sha[:12]}) and findings.json "
            f"({str(reviewed.get('head_sha'))[:12]}) belong to different runs; "
            f"finish the review with `resume {target_dir}` before posting"
        )
    chosen = select(findings_file["findings"], decisions, findings_file.get("mode"))
    if not chosen:
        return f"nothing to post: no {'accepted ' if decisions is not None else ''}findings to post"
    if not github_web_base(findings_file["target"].get("remote_url")):
        raise PostError(
            f"the review's remote ({findings_file['target'].get('remote_url')}) is not on GitHub; "
            f"post needs a GitHub PR"
        )

    gh = shutil.which("gh")
    if gh is None:
        raise PostError(
            "posting needs the GitHub CLI (`gh`), which is not installed. Install it from "
            "https://cli.github.com and run `gh auth login`."
        )
    try:
        lines = commentable_lines(pr_diff(t.repo_path, t.base_sha, t.head_sha))
    except TargetError as e:
        raise PostError(
            f"could not read the PR's diff to place the comments: {e}"
        ) from e
    to_post, again, earlier = drop_posted(chosen, posted_ids(gh, t.repo_path, t.pr))
    skipped = len(again) + len(earlier)
    note = _skip_note(again, earlier)
    if not to_post:
        return f"nothing to post: {note}"

    payload = build_payload(
        findings_file, to_post, lines, round_name(target_dir, findings_file)
    )
    proc = subprocess.run(
        [
            gh,
            "api",
            "-X",
            "POST",
            f"repos/{{owner}}/{{repo}}/pulls/{t.pr}/reviews",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        cwd=t.repo_path,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise PostError(
            f"gh api POST .../pulls/{t.pr}/reviews failed; nothing was posted: {proc.stderr.strip()}"
        )
    inline = len(payload["comments"])
    outside = len(to_post) - inline
    summary = (
        f"posted {len(to_post)} {'accepted ' if decisions is not None else ''}findings to PR #{t.pr} "
        f"as one review: {inline} inline, {outside} outside the diff"
    )
    if note:
        summary += f"; {note}"
    log_event(
        "review",
        "post",
        summary,
        session_id=session_id,
        target=t.slug,
        pr=t.pr,
        count=len(to_post),
        inline=inline,
        outside_diff=outside,
        skipped=skipped,
    )
    return summary
