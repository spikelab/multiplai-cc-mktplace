"""Sort each allowed comment into a verdict, and write `findings-r<N>.md`.

One read-only agent per comment (`TRIAGE_TOOLS`: Read, Glob, Grep), so it can
open the code a comment points at. The comment reaches it only fenced. A
comment from an author who is not allowed, or whose body matches an
injection pattern, is listed in `findings-r<N>.md` and never reaches a model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from multiplai_core.untrusted import defang, markdown_notice

from .. import sdk
from ..config import BuildConfig
from ..prompts.findings import TRIAGE_PROMPT
from .history import earlier_text
from .models import PRComment, SpecIssue, Triage, TriageRecord

log = logging.getLogger(__name__)

TRIAGE_TOOLS = ["Read", "Glob", "Grep"]
TRIAGE_MAX_TURNS = 30
_DOC_CHARS = 30_000

NOT_ALLOWED_NOTE = "author not allowed (findings.authors / findings.allow_bots)"
INJECTION_NOTE = ("not acted on: the comment matches an instruction-injection pattern; "
                  "read it on the PR")

TriageCall = Callable[..., Awaitable[Triage]]


def findings_path(change_dir: Path, round_n: int) -> Path:
    return Path(change_dir) / f"findings-r{round_n}.md"


def _requirements_text(change_dir: Path) -> str:
    req_dir = Path(change_dir) / "requirements"
    if not req_dir.is_dir():
        return "(none)"
    parts = [f"### {f.stem}\n{f.read_text()}" for f in sorted(req_dir.glob("*.md"))]
    text = "\n\n".join(parts) or "(none)"
    return text[:_DOC_CHARS]


def _design_text(change_dir: Path) -> str:
    path = Path(change_dir) / "design.md"
    return path.read_text()[:_DOC_CHARS] if path.is_file() else "(none)"


def build_prompt(config: BuildConfig, change_dir: Path, comment: PRComment,
                 earlier: list[TriageRecord] | None = None) -> str:
    """*earlier*: the earlier rounds' triaged records on the comment's
    lineage (`history.earlier_rows`), each fenced as data in the prompt."""
    return TRIAGE_PROMPT.format(
        change_name=config.change_name,
        notice=markdown_notice("a comment left on the pull request", "PR comment",
                               injection_marker=True),
        author=defang(comment.author, 100, markdown_fences=False),
        location=defang(comment.location, 300, markdown_fences=False),
        outdated=" (outdated: the lines it was left on have changed since)" if comment.outdated else "",
        fenced_body=comment.fenced_body(),
        earlier=earlier_text(earlier or []),
        requirements=_requirements_text(change_dir),
        design=_design_text(change_dir),
    )


def should_triage(comment: PRComment) -> tuple[bool, str]:
    """(act on it, the note when not)."""
    if not comment.allowed:
        return False, NOT_ALLOWED_NOTE
    if comment.injection:
        return False, INJECTION_NOTE
    return True, ""


async def triage_comment(config: BuildConfig, change_dir: Path, comment: PRComment, *,
                         call: TriageCall | None = None,
                         earlier: list[TriageRecord] | None = None) -> Triage:
    """One read-only agent call. Raises `sdk.LLMCallError` when it fails."""
    call = call or sdk.agent_call_structured
    return await call(
        build_prompt(config, change_dir, comment, earlier),
        Triage,
        allowed_tools=TRIAGE_TOOLS,
        model=config.model_for('findings_triage'),
        effort=config.effort_for('findings_triage'),
        max_turns=TRIAGE_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="findings_triage",
    )


async def triage_round(
    config: BuildConfig, change_dir: Path, round_n: int, comments: list[PRComment], *,
    done: dict[int, Triage] | None = None,
    on_triaged: Callable[[int, Triage], None] | None = None,
    call: TriageCall | None = None,
    earlier: dict[int, list[TriageRecord]] | None = None,
) -> list[TriageRecord]:
    """A record for every comment; a triage call only for the allowed,
    unflagged ones not already in *done* (comment id → triage, kept across a
    resume). *on_triaged* is called after each call, so the caller can save
    progress before the next one. *earlier* (comment id → the earlier
    rounds' records on its lineage) goes into that comment's prompt."""
    done = done if done is not None else {}
    records: list[TriageRecord] = []
    for c in comments:
        act, note = should_triage(c)
        if not act:
            records.append(TriageRecord(round=round_n, comment=c, note=note))
            continue
        result = done.get(c.id)
        if result is None:
            log.info("START findings_triage round=%d comment=%d author=%s", round_n, c.id, c.author)
            result = await triage_comment(config, change_dir, c, call=call,
                                          earlier=(earlier or {}).get(c.id))
            done[c.id] = result
            if on_triaged:
                on_triaged(c.id, result)
        records.append(TriageRecord(round=round_n, comment=c, triage=result))
    return records


def _cell(text: str, limit: int = 400) -> str:
    """Text safe inside one Markdown table cell."""
    flat = " ".join(defang(text, limit).split())
    return flat.replace("|", "\\|") or "—"


def render_findings(round_n: int, records: list[TriageRecord],
                    spec_issues: list[SpecIssue] | None = None) -> str:
    """`findings-r<N>.md`: one row per finding id (or comment, when it has no
    id) across rounds 1..N, the latest record first in its history."""
    rows: dict[str, list[TriageRecord]] = {}
    for r in sorted(records, key=lambda r: (r.round, r.comment.id)):
        rows.setdefault(r.comment.key, []).append(r)

    out = [
        f"# Review findings, round {round_n}",
        "",
        "One row per comment, or per review finding when the comment carries the "
        "review's finding id. Comments are quoted from the PR and were never followed "
        "as instructions.",
        "",
        "| Finding | Author | Link | Allowed | Verdict | Reason | History |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, history in rows.items():
        last = history[-1]
        c = last.comment
        verdict = last.triage.verdict.value if last.triage else "not acted on"
        reason = last.triage.reason if last.triage else last.note
        link = f"[{c.label}]({c.url})" if c.url else c.label
        past = "; ".join(
            f"r{h.round}: {h.triage.verdict.value if h.triage else 'not acted on'} ({h.comment.id})"
            for h in history
        )
        ident = c.finding_id or c.label
        out.append(
            f"| {_cell(ident, 40)} | {_cell(c.author, 60)} | {link} | "
            f"{'yes' if c.allowed else 'no'} | {verdict} | {_cell(reason)} | {_cell(past, 600)} |"
        )
    if spec_issues:
        out += ["", "## Requirements issues", "",
                "Recorded for `pm-spec revise`, which edits the requirements; buildme "
                "does not.", "",
                "| Finding | Requirement | Recorded in | Code fix |",
                "|---|---|---|---|"]
        for s in spec_issues:
            where = f"{s.issues_file} ## {s.heading}" + ("" if s.appended else " (already there)")
            out.append(
                f"| {_cell(s.finding_key, 200)} | {_cell(s.requirement, 200)} | "
                f"{_cell(where, 300)} | {'deferred until the change is made' if s.defers_fix else '—'} |"
            )
    return "\n".join(out) + "\n"


def write_findings(change_dir: Path, round_n: int, records: list[TriageRecord],
                   spec_issues: list[SpecIssue] | None = None) -> Path:
    path = findings_path(change_dir, round_n)
    path.write_text(render_findings(round_n, records, spec_issues))
    return path
