"""Markdown: the per-target review, and severity rollups across targets.

Both render from `findings.json` v1 dicts, so a rollup never scrapes markdown.
The per-target review adds what only the pipeline state has: the severity a
finding had before it was lowered, the finders that reported it, and the
findings merged into it. Its `## Paths` section (`render_paths_section`) is
read from the run's `paths.json`: rendering maps nothing and asks no model.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

from . import rounds
from .export import to_checks_file, to_findings_file
from .models import (
    CRITICAL_IMPACTS,
    DEPS_DIR,
    SECTIONS,
    SEVERITIES,
    CoverageRecord,
    ReviewState,
    section_of,
)
from .paths_io import load_paths, views
from .target import github_web_base, object_folders, review_children

if TYPE_CHECKING:
    from code_facts.paths import PathsResult

log = logging.getLogger(__name__)

SHOWN_STATUSES = ("confirmed", "unverifiable")
# Assess labels whose findings are never listed or counted: low-value, and a
# repeat of a finding the person rejected in an earlier round. They go to the
# review's appendix with the refuted and gate-rejected ones; findings.json
# keeps them all. `important`: a trim finding about a test that must stay.
HIDDEN_LABELS = ("repeat", "low-value", "important")


def assess_label(fd: dict) -> str:
    """The finding's assess label, or "" when the file has none (written before the stage)."""
    return str((fd.get("assessment") or {}).get("label") or "")


SECTION_TITLES = {"code": "Code", "tests": "Tests", "docs": "Docs", "trim": "Trim"}
# Said after a section's title in the review and the summary.
SECTION_NOTES = {"trim": " (advisory: never blocks a merge)"}


def listed(findings: list[dict], mode: str | None = None) -> list[dict]:
    """The findings the markdown lists and counts: confirmed or unverifiable,
    and not labelled low-value, repeat or important; in `critical` mode, also rated
    `breaks-users` or `breaks-business`."""
    keep = [
        f
        for f in findings
        if f["status"] in SHOWN_STATUSES and assess_label(f) not in HIDDEN_LABELS
    ]
    if mode == "critical":
        keep = [f for f in keep if f.get("impact") in CRITICAL_IMPACTS]
    return keep


def not_listed(findings: list[dict], mode: str | None = None) -> list[dict]:
    """Everything `listed` leaves out, for the review's appendix."""
    keep = {id(f) for f in listed(findings, mode)}
    return [f for f in findings if id(f) not in keep]


def section(fd: dict) -> str:
    """The finding's `section`; a file written before 0.71 has none, and then it is
    `code`, `tests` or `docs` from the topic (no topic is `code`)."""
    if fd.get("section") in SECTIONS:
        return fd["section"]
    return section_of(fd.get("topic"))


def count_line(findings: list[dict], mode: str | None = None) -> str:
    """`Code 1 HIGH, 0 MEDIUM, 2 LOW; Tests none; Docs none`, over the listed findings.

    Severity is read within a section: a HIGH test finding is not a HIGH code finding.
    Trim, which is advisory, is named only when it lists something.
    """
    shown = listed(findings, mode)
    parts = []
    for name in SECTIONS:
        group = [f for f in shown if section(f) == name]
        if name == "trim" and not group:
            continue
        counts = ", ".join(
            f"{sum(1 for f in group if f['severity'] == s)} {s}" for s in SEVERITIES
        )
        parts.append(f"{SECTION_TITLES[name]} {counts if group else 'none'}")
    return "; ".join(parts)


def mode_line(mode: str | None) -> str:
    """One sentence for a critical review, else ""."""
    if mode != "critical":
        return ""
    return (
        "Critical mode: only findings the verifier rates as breaking users or the business after merge "
        "are listed; the rest are in the full review's appendix."
    )


def assessment_line(fd: dict) -> str:
    """One line saying how the assess stage labelled the finding, or "" for `useful` and none."""
    a = fd.get("assessment") or {}
    label = a.get("label")
    if label in (None, "", "useful"):
        return ""
    reason = _one_line(a.get("reason") or "")
    if label in ("repeat", "still-open"):
        earlier = (
            f"`{a.get('earlier_id')}`" if a.get("earlier_id") else "an earlier finding"
        )
        if a.get("earlier_round"):
            earlier += f" (round {str(a['earlier_round'])[:12]})"
        decision = a.get("earlier_decision") or "no decision"
        note = f": {_one_line(a['earlier_note'])}" if a.get("earlier_note") else ""
        what = "repeats" if label == "repeat" else "still open from"
        return f"**Assessment:** {label} — {what} {earlier}, your decision {decision}{note}. {reason}".rstrip()
    return f"**Assessment:** {label} — {reason}".rstrip()


# --- earlier rounds: statuses, recurring lineages, the summary line ----------------

# A lineage open in this many rounds or more is listed under `## Recurring`.
RECURRING_ROUNDS = 3
STILL_OPEN_STATUSES = ("still_present", "partially_resolved")


def lineage(fd: dict) -> str:
    """The id of the finding's first appearance: its `lineage`, else its own id (a file before 0.94 has none)."""
    return str(fd.get("lineage") or fd["id"])


def open_lineages(data: dict) -> dict[str, set[str]]:
    """The lineages one round's findings file left open, each with its files: those it listed
    (`still-open` ones included) and its `earlier` statuses other than `resolved`."""
    out: dict[str, set[str]] = {}
    for f in listed(
        [f for f in data.get("findings") or [] if isinstance(f, dict) and f.get("id")],
        data.get("mode"),
    ):
        out.setdefault(lineage(f), set()).add(str(f.get("file") or ""))
    for e in data.get("earlier") or []:
        if isinstance(e, dict) and e.get("lineage") and e.get("status") != "resolved":
            out.setdefault(str(e["lineage"]), set()).update(
                str(c.get("path") or "")
                for c in e.get("citations") or []
                if not str(c.get("path") or "")
                .lower()
                .startswith(("http://", "https://"))
            )
    return {k: {p for p in v if p} for k, v in out.items()}


def recurring(earlier_files: list[dict], data: dict) -> list[dict]:
    """Each lineage open in `RECURRING_ROUNDS` or more rounds (the kept ones, then *data*), in code.

    `{"lineage", "rounds", "heads", "files"}`, the most rounds first.
    """
    seen: dict[str, dict] = {}
    for d in [*earlier_files, data]:
        head = str((d.get("target") or {}).get("head_sha") or "")
        for lin, files in open_lineages(d).items():
            entry = seen.setdefault(
                lin, {"lineage": lin, "rounds": 0, "heads": [], "files": set()}
            )
            entry["rounds"] += 1
            entry["heads"].append(head)
            entry["files"] |= files
    out = [
        {**e, "files": sorted(e["files"])}
        for e in seen.values()
        if e["rounds"] >= RECURRING_ROUNDS
    ]
    return sorted(out, key=lambda e: (-e["rounds"], e["lineage"]))


def _claims(earlier_files: list[dict], data: dict) -> dict[str, str]:
    """Every claim by finding id and by lineage, the latest round's copy winning."""
    out: dict[str, str] = {}
    for d in [*earlier_files, data]:
        for f in d.get("findings") or []:
            if isinstance(f, dict) and f.get("id"):
                out[f["id"]] = out[lineage(f)] = str(f.get("claim") or "")
    return out


def earlier_counts(data: dict) -> dict[str, int]:
    """`new`: listed findings not `still-open`; `still`: earlier findings still open, each once (an
    `earlier` status still present or partly resolved, or the earlier id a listed `still-open`
    finding names); `resolved` and `unknown` (`cannot_verify`): the `earlier` statuses."""
    shown = listed(data.get("findings") or [], data.get("mode"))
    earlier = [e for e in data.get("earlier") or [] if isinstance(e, dict)]
    still = {
        e.get("earlier_id") for e in earlier if e.get("status") in STILL_OPEN_STATUSES
    }
    still |= {
        (f.get("assessment") or {}).get("earlier_id") or f["id"]
        for f in shown
        if assess_label(f) == "still-open"
    }
    return {
        "new": sum(1 for f in shown if assess_label(f) != "still-open"),
        "still": len(still),
        "resolved": sum(1 for e in earlier if e.get("status") == "resolved"),
        "unknown": sum(1 for e in earlier if e.get("status") == "cannot_verify"),
    }


def earlier_line(data: dict) -> str:
    """`2 new, 1 still open, 3 earlier resolved, 0 earlier cannot verify`, or "" when the round
    has no earlier statuses and no `still-open` finding."""
    c = earlier_counts(data)
    if not data.get("earlier") and not c["still"]:
        return ""
    return (
        f"{c['new']} new, {c['still']} still open, {c['resolved']} earlier resolved, "
        f"{c['unknown']} earlier cannot verify"
    )


def render_earlier_sections(
    data: dict, earlier_files: list[dict], *, web_base: str | None, head_sha: str
) -> list[str]:
    """`## Findings from earlier rounds` and `## Recurring`, each only when it has a row. States what
    the code shows and what recurred; proposes no fix."""
    out: list[str] = []
    claims = _claims(earlier_files, data)
    earlier = [e for e in data.get("earlier") or [] if isinstance(e, dict)]
    if earlier:
        out += [
            "## Findings from earlier rounds",
            "",
            "The status at this head of each finding the last round left open and nobody rejected, with "
            "the lines that show it. One no agent could check is `cannot_verify`, never resolved.",
            "",
        ]
        for e in earlier:
            fid = str(e.get("earlier_id") or "")
            claim = (
                claims.get(fid)
                or claims.get(str(e.get("lineage") or ""))
                or "(claim not in a kept round)"
            )
            out.append(
                f"- **{e.get('status')}** `{fid}` (round {str(e.get('round') or '')[:12] or 'unknown'}) — "
                f"{_one_line(claim)}  "
            )
            if e.get("reason"):
                out.append(f"  {_one_line(e['reason'])}")
            out += [
                f"  - {code_link(web_base, head_sha, c['path'], c['line_start'], c['line_end'])}"
                for c in e.get("citations") or []
                if not str(c.get("path") or "")
                .lower()
                .startswith(("http://", "https://"))
            ]
        out.append("")
    repeated = recurring(earlier_files, data)
    if repeated:
        out += [
            "## Recurring",
            "",
            f"Defects open in {RECURRING_ROUNDS} or more rounds of this review (listed, still open, or an "
            "earlier status other than resolved), counted in code from the kept rounds and this one.",
            "",
        ]
        for r in repeated:
            heads = ", ".join(f"`{h[:12]}`" for h in r["heads"] if h)
            files = ", ".join(f"`{p}`" for p in r["files"]) or "no file cited"
            claim = _one_line(claims.get(r["lineage"]) or "")
            out.append(
                f"- `{r['lineage']}` open in {r['rounds']} rounds ({heads}) — {claim}  "
            )
            out.append(f"  Files: {files}")
        out.append("")
    return out


def code_link(
    web_base: str | None, head_sha: str, path: str, start: int, end: int
) -> str:
    """A full-sha GitHub link for a GitHub remote, else `path:lines`.

    A dependency's line (`.review-deps/...`) is not in the reviewed repository, so it is never linked.
    """
    lines = f"{start}" if start == end else f"{start}-{end}"
    if web_base and not path.startswith(DEPS_DIR + "/"):
        return f"[{path}:{lines}]({web_base}/blob/{head_sha}/{path}#L{start}-L{end})"
    return f"`{path}:{lines}`"


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def class_line(fd: dict) -> str:
    """`class: <defect_class>; also at: <locations>`, the parts present, or "" (buildme's `class_line` wording)."""
    parts = []
    if fd.get("defect_class"):
        parts.append(f"class: {_one_line(fd['defect_class'])}")
    if fd.get("other_locations"):
        parts.append(
            f"also at: {', '.join(_one_line(loc) for loc in fd['other_locations'])}"
        )
    return "; ".join(parts)


def finding_section(
    fd: dict,
    *,
    web_base: str | None,
    head_sha: str,
    original_severity: str | None = None,
    finders: list[str] | None = None,
) -> str:
    """One finding. *finders* are the finders that reported it, from the pipeline state."""
    lines_ = (
        f"{fd['line_start']}"
        if fd["line_start"] == fd["line_end"]
        else f"{fd['line_start']}-{fd['line_end']}"
    )
    out = [
        f"### {fd['severity']} — {fd['file']}:{lines_} — {_one_line(fd['claim'])}",
        "",
    ]
    if class_line(fd):
        out += [class_line(fd), ""]
    if assessment_line(fd):
        out += [assessment_line(fd), ""]
    if fd.get("impact"):
        out += [f"**Impact:** {fd['impact']}", ""]

    status = fd["status"]
    if original_severity and original_severity != fd["severity"]:
        status += f" (lowered from {original_severity})"
    out += [
        f"**Verdict:** {status}. {_one_line(fd.get('verdict_reason') or '')}".rstrip(),
        "",
    ]
    if finders and len(finders) > 1:
        out += [f"**Reported by:** {', '.join(finders)}", ""]

    out.append("**Evidence:**")
    out.append("")
    for c in fd["citations"]:
        if c["path"].lower().startswith(("http://", "https://")):
            out.append(f"- web source: <{c['path']}>")
        else:
            out.append(
                f"- {code_link(web_base, head_sha, c['path'], c['line_start'], c['line_end'])}"
            )
        out.append("")
        out.append(_fence(c["quote"]))
        out.append("")
    out += [f"**Failure scenario:** {_one_line(fd['failure_scenario'])}", ""]
    if fd.get("expected_behaviour"):
        out += [f"**Expected behaviour:** {_one_line(fd['expected_behaviour'])}", ""]
    if fd.get("needs"):
        out += [
            "**Needs you** (each with a command the review suggests, a place to look, or neither: read a "
            "command before running it):",
            "",
        ]
        out += need_lines(fd["needs"], blocks=False) + [""]
    return "\n".join(out)


CAUSE_WORDS = {
    "no-access": "the review has no access",
    "lookup-failed": "a lookup failed",
    "unreachable": "not reachable on the web",
}


def need_lines(
    needs: list[dict], findings: list[dict] | None = None, *, blocks: bool = True
) -> list[str]:
    """One markdown bullet per need: what is missing, what it blocks, why, the command and where to look.

    With *findings*, a need that blocks a finding names its file and line.
    *blocks* False leaves out what it blocks (under the finding itself).
    """
    where = {f["id"]: f"`{f['file']}:{f['line_start']}`" for f in findings or []}
    out = []
    for n in needs:
        what = _one_line(n["what"]).rstrip(".") + "."
        cause = CAUSE_WORDS.get(n["cause"], n["cause"])
        if blocks:
            on = (
                "the review"
                if n["blocks"] == "review"
                else where.get(n["blocks"], f"finding `{n['blocks']}`")
            )
            line = f"- {what} Blocks {on}; {cause}."
        else:
            line = f"- {what} Why: {cause}."
        if n.get("command"):
            line += f" Run: `{n['command']}`"
        if n.get("where"):
            line += f" Where to look: {_one_line(n['where']).rstrip('.')}."
        if not n.get("command") and not n.get("where"):
            line += " The review named no command and no place to look."
        out.append(line)
    return out


def duration(seconds: float) -> str:
    """`Xm Ys`, or `Ys` under a minute."""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def cost_line(run: dict) -> str:
    """Cost, calls, tokens and wall time, all from `findings.json`'s `run` object."""
    calls = run["calls"]
    return (
        f"${run['cost_usd']:.2f} over {calls} agent call{'' if calls == 1 else 's'}, "
        f"{run['tokens']['total']:,} tokens, {duration(run['wall_seconds'])} wall time"
    )


# --- the Paths section ------------------------------------------------------------


def inline_code(text: str) -> str:
    """*text* as one inert code span: control characters and line breaks become spaces, and the
    backtick run around it is one longer than the longest inside it, padded when it starts or ends
    with a backtick, so no value can open a heading, link, tag or list item."""
    flat = "".join(
        " " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in text or ""
    )
    longest = max((len(run) for run in re.findall(r"`+", flat)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") or not flat else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


# Which paths-stage coverage reasons the gap line counts, and as what.
GAP_DROPPED = {"over-cap": "over cap", "out-of-scope": "out of scope"}
GAP_BROKEN = ("trace-failed", "broken-trace", "fallback-failed")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def gap_line(records: list[CoverageRecord]) -> str:
    """`Gaps: <n> entries dropped (<a> over cap, <b> out of scope); <c> broken traces.`, or "" for none."""
    paths = [r for r in records if r.stage == "paths"]
    dropped = {
        label: sum(r.reason == reason for r in paths)
        for reason, label in GAP_DROPPED.items()
    }
    broken = sum(r.reason in GAP_BROKEN for r in paths)
    clauses = []
    total = sum(dropped.values())
    if total:
        parts = ", ".join(f"{n} {label}" for label, n in dropped.items() if n)
        clauses.append(f"{_plural(total, 'entry', 'entries')} dropped ({parts})")
    if broken:
        clauses.append(_plural(broken, "broken trace", "broken traces"))
    return f"Gaps: {'; '.join(clauses)}." if clauses else ""


def render_paths_section(
    state: ReviewState, result: PathsResult | None, findings: list[dict]
) -> str:
    """The report's `## Paths` section, or "" when the paths stage was skipped or did not run.

    One block per path of *result*, in `paths.json` order: its entry, who
    invokes it, the *findings* (`findings.json` rows) whose `paths` list it,
    and its e2e tests; then the gap line. A failed or empty mapping gives the
    heading and the recorded reason. Every value from `paths.json` is shown as
    `inline_code`.
    """
    if state.paths_status in ("skipped", "not-run"):
        return ""
    out = ["## Paths", ""]
    shown = views(result) if result is not None and state.paths_status == "ok" else []
    if not shown:
        reason = state.paths_failure or (
            "the paths file could not be read"
            if state.paths_status == "ok"
            else "the mapping found none"
        )
        out += [f"No paths were mapped: {_one_line(reason).rstrip('.')}.", ""]
    for view in shown:
        where = f"{view.file}:{view.line}" if view.line else view.file
        out.append(
            f"### Path {view.id}: {inline_code(view.name)} at {inline_code(where)}"
        )
        invokers = [i for i in view.invokers if i]
        out.append(
            "- Invoked by: "
            + (
                ", ".join(inline_code(i) for i in invokers)
                if invokers
                else "unknown invoker"
            )
        )
        on = [fd["id"] for fd in findings if view.id in (fd.get("paths") or [])]
        out.append(
            f"- Findings on this path: {len(on)}"
            + (f" ({', '.join(inline_code(i) for i in on)})" if on else "")
        )
        out.append("- E2E tests:")
        out += [
            f"  - {inline_code(t.id)} — {'mocked (not counted)' if t.mocked else 'counted'}"
            for t in view.e2e
        ]
        if not any(not t.mocked for t in view.e2e):
            out.append("  - no e2e test")
        if view.broken_at is not None:
            out.append(f"- Trace stops at hop {view.broken_at}.")
        out.append("")
    gaps = gap_line(state.coverage)
    if gaps:
        out += [gaps, ""]
    return "\n".join(out)


def render_review(
    state: ReviewState,
    *,
    deployed: str | None = None,
    findings_file: dict | None = None,
    paths: PathsResult | None = None,
    earlier_rounds: list[dict] | None = None,
) -> str:
    """The full review. *earlier_rounds* are the kept rounds' findings files (`rounds.kept_findings_files`),
    for the `## Recurring` section; without them it counts this round only."""
    data = findings_file or to_findings_file(state)
    t = state.target
    web = github_web_base(t.remote_url)
    head = t.head_sha
    findings = data["findings"]
    mode = data.get("mode")

    commit_link = (
        (lambda sha: f"[{sha[:10]}]({web}/commit/{sha})")
        if web
        else (lambda sha: f"`{sha[:10]}`")
    )
    out = [f"# Review — {t.label or t.slug}", ""]
    out.append(
        f"- **Repo:** {Path(t.repo_path).name}"
        + (f" ({web})" if web else f" (`{t.repo_path}`)")
    )
    if t.tickets:
        out.append(f"- **Tickets:** {', '.join(t.tickets)}")
    if t.is_tree:
        # Not a change: no base commit and no commits, only the files as they stand at head.
        where = "the whole tree" if t.ref in ("", ".") else f"`{t.ref}`"
        out.append(f"- **Reviewed:** {where} at {commit_link(head)}, not a change")
        out.append(
            f"- **Files reviewed:** {len(t.files)}; {len(t.skipped)} skipped (`skipped.txt`)"
        )
    else:
        out.append(f"- **Base commit:** {commit_link(t.base_sha)}")
        out.append(f"- **Head commit:** {commit_link(head)}")
        out.append(f"- **Commits:** {len(t.commits)}")
        out += [
            f"  - {commit_link(sha)} {_one_line(subject)}" for sha, subject in t.commits
        ]
        out.append(f"- **Files changed:** {len(t.files)}")
        out += [f"  - `{f}`" for f in t.files]
        if t.excluded:
            globs = ", ".join(f"`{g}`" for g in t.excluded)
            out.append(
                f"- **Left out by --exclude ({globs}):** {len(t.excluded_files)} files"
            )
            out += [f"  - `{f}`" for f in t.excluded_files]
    if t.deployed_in:
        out.append(f"- **Deployed in {t.deployed_in}:** {deployed or 'unknown'}")
    hidden = not_listed(findings, mode)
    if mode == "critical":
        out.append(f"- **Mode:** critical. {mode_line(mode)}")
    out.append(
        f"- **Findings:** {count_line(findings, mode)}"
        + (" (what was left out is only in the appendix)" if hidden else "")
    )
    if earlier_line(data):
        out.append(f"- **Against earlier rounds:** {earlier_line(data)}")
    if data.get("run"):
        out.append(f"- **Model cost:** {cost_line(data['run'])}")
    if state.errors:
        out.append(
            "- **Agent failures:** " + "; ".join(_one_line(e) for e in state.errors)
        )
    review_needs = [n for n in data.get("needs", []) if n["blocks"] == "review"]
    if review_needs:
        out += [
            "",
            "## Needs you",
            "",
            "What the review could not check, each with a command the review suggests, a place to look, "
            "or neither. Read a command before running it.",
            "",
        ]
        out += need_lines(review_needs)
    out += ["", "## Findings", ""]

    finders = {f.id: f.finders for f in state.findings}

    shown = listed(findings, mode)
    if not shown:
        out += [
            "No findings to act on."
            + (" The appendix lists what was left out." if hidden else ""),
            "",
        ]
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    for name in SECTIONS:
        group = sorted(
            (f for f in shown if section(f) == name), key=lambda f: rank[f["severity"]]
        )
        if not group:
            continue
        out += [f"## {SECTION_TITLES[name]}{SECTION_NOTES.get(name, '')}", ""]
        for fd in group:
            out.append(
                finding_section(
                    fd,
                    web_base=web,
                    head_sha=head,
                    original_severity=state.original_severity.get(fd["id"]),
                    finders=finders.get(fd["id"]),
                )
            )

    out += render_earlier_sections(
        data, earlier_rounds or [], web_base=web, head_sha=head
    )
    paths_section = render_paths_section(state, paths, findings)
    if paths_section:
        out += [paths_section]
    out += [
        "## Appendix — not listed above",
        "",
        "The record of what the review left out: findings a gate rejected, the verifier refuted, the "
        "assess stage labelled low-value or a repeat of one you rejected, trim suggestions about an "
        "important test, duplicates merged into another"
        + (
            ", and in critical mode those not rated as breaking users or the business"
            if mode == "critical"
            else ""
        )
        + ". None is counted above.",
        "",
    ]
    if not hidden and not state.merged:
        out += ["Nothing was left out.", ""]
    for fd in hidden:
        lines_ = f"{fd['line_start']}-{fd['line_end']}"
        if fd["status"] not in SHOWN_STATUSES:
            kind = fd["status"]
        elif assess_label(fd) in HIDDEN_LABELS:
            kind = assess_label(fd)
        else:
            kind = "not critical"
        out.append(
            f"- **{kind}** ({fd['severity']}) `{fd['file']}:{lines_}` — {_one_line(fd['claim'])}  "
        )
        if fd["status"] not in SHOWN_STATUSES:
            who = "the verifier" if fd["status"] == "refuted" else "a gate"
            out.append(
                f"  Reason from {who}: {_one_line(fd.get('verdict_reason') or '')}"
            )
        elif kind == "not critical":
            out.append(
                f"  Impact: {fd.get('impact') or 'not rated'}; {SECTION_TITLES[section(fd)]} section."
            )
        else:
            out.append(f"  {assessment_line(fd).removeprefix('**Assessment:** ')}")
        # Its needs are printed nowhere else: the summary leaves them out with the finding.
        out += [
            f"  - Needs you: {line[2:]}"
            for line in need_lines(fd.get("needs", []), blocks=False)
        ]
    for m in state.merged:
        f = m.finding
        out.append(
            f"- **merged** ({f.severity}) `{f.file}:{f.line_start}-{f.line_end}` — {_one_line(f.claim)}  "
        )
        out.append(f"  Merged into `{m.into}`: {_one_line(m.reason)}")
    out.append("")
    return "\n".join(out)


def _write(path: Path, text: str) -> None:
    """Write *text* beside *path*, then rename it over *path*, so a kill mid-write leaves the old file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_review(
    state: ReviewState, target_dir: Path, *, deployed: str | None = None
) -> Path:
    """Write the full review, its short summary and the checks record; return the full review's path."""
    path = target_dir / f"review-{state.target.slug}.md"
    _write(
        path,
        render_review(
            state,
            deployed=deployed,
            paths=load_paths(target_dir, state),
            earlier_rounds=rounds.kept_findings_files(target_dir),
        ),
    )
    _write(summary_path(state, target_dir), render_summary(state))
    _write(target_dir / f"checks-{state.target.slug}.md", render_checks(state))
    return path


# --- summary -------------------------------------------------------------------

SUMMARY_CLAIM_CHARS = 120


def _short(text: str, limit: int = SUMMARY_CLAIM_CHARS) -> str:
    """The first sentence of `text`, cut to `limit` characters."""
    text = _one_line(text)
    end = re.search(r"\. (?=[A-Z])", text)
    if end:
        text = text[: end.start() + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def summary_path(state: ReviewState, target_dir: Path) -> Path:
    return target_dir / f"summary-{state.target.slug}.md"


def render_summary(state: ReviewState, *, findings_file: dict | None = None) -> str:
    """How the review went, short enough to read in chat.

    Per section (Code, Tests, Docs), one line per HIGH and MEDIUM finding and
    a count for LOW. Refuted, gate-rejected, low-value and repeat findings,
    and in critical mode those not rated `breaks-*`, are neither listed nor
    counted; the full review's appendix has them, each with the needs that
    block it, and the summary says how many such needs it left out.
    """
    data = findings_file or to_findings_file(state)
    t = state.target
    findings = data["findings"]
    mode = data.get("mode")
    shown = listed(findings, mode)
    shown_ids = {f["id"] for f in shown}

    out = [f"# Review summary — {t.label or t.slug}", ""]
    if t.is_tree:
        out.append(
            f"{len(t.files)} files reviewed as they stand at {t.head_sha[:10]}, {len(t.skipped)} skipped."
        )
    else:
        out.append(
            f"{len(t.commits)} commits, {len(t.files)} files, {t.base_sha[:10]}..{t.head_sha[:10]}."
        )
    if data.get("run"):
        out.append(f"Cost {cost_line(data['run'])}.")
    if mode_line(mode):
        out.append(mode_line(mode))
    out.append(
        f"Findings: {count_line(findings, mode)}."
        + (
            f" {len(state.merged)} merged into another finding as duplicates."
            if state.merged
            else ""
        )
    )
    if earlier_line(data):
        out.append(f"Against earlier rounds: {earlier_line(data)}.")
    needs = [
        n
        for n in data.get("needs", [])
        if n["blocks"] == "review" or n["blocks"] in shown_ids
    ]
    left_out = len(data.get("needs", [])) - len(needs)
    if needs:
        out += [
            "",
            "## Needs you",
            "",
            "The review could not get these, each with a command the review suggests, a place to look, "
            "or neither. Read a command before running it.",
            "",
        ]
        out += need_lines(needs, findings)
        out.append("")
    if left_out:
        out.append(
            f"{left_out} more need{'' if left_out == 1 else 's'} block{'s' if left_out == 1 else ''} "
            "findings not listed; the full review's appendix has them under each finding."
        )
    if state.errors:
        out.append("Agent failures: " + "; ".join(_short(e) for e in state.errors))

    for name in SECTIONS:
        group = [f for f in shown if section(f) == name]
        if not group:
            continue
        out += ["", f"## {SECTION_TITLES[name]}{SECTION_NOTES.get(name, '')}", ""]
        for severity in ("HIGH", "MEDIUM"):
            for fd in (f for f in group if f["severity"] == severity):
                lines_ = (
                    f"{fd['line_start']}"
                    if fd["line_start"] == fd["line_end"]
                    else f"{fd['line_start']}-{fd['line_end']}"
                )
                still = ", still open" if assess_label(fd) == "still-open" else ""
                out.append(
                    f"- {severity} `{fd['file']}:{lines_}` — {_short(fd['claim'])} ({fd['status']}{still})"
                )
        low = sum(1 for f in group if f["severity"] == "LOW")
        if low:
            out.append(f"- {low} LOW in the full review.")
    out += [
        "",
        f"Full review, with every reason and what was left out: `review-{t.slug}.md`",
        "",
    ]
    return "\n".join(out)


# --- checks ----------------------------------------------------------------------


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


CONVERT_SUFFIX = " convert"


def finder_rows(agents: list[dict]) -> list[dict]:
    """One row per finder: ran or failed, files read, findings returned and their fates.

    A piece or path finder's convert call (`<key> convert`) is folded into its
    finder's row: one row per key, with the convert's findings and the files
    either call read.
    """
    rows: list[dict] = []
    by_subject: dict[str, dict] = {}
    for a in agents:
        if a["stage"] != "find":
            continue
        subject = a["subject"].removesuffix(CONVERT_SUFFIX)
        fates = [f["fate"] for f in a["findings"]]
        read = {c["target"] for c in a["calls"] if c["tool"] == "Read"}
        row = by_subject.get(subject)
        if row is None or not a["subject"].endswith(CONVERT_SUFFIX):
            row = {
                "subject": subject,
                "ran": "failed" if a["error"] else "ran",
                "_read": read,
                "returned": len(fates),
                **{
                    k: fates.count(k) for k in ("kept", "deduped", "merged", "rejected")
                },
            }
            if (
                subject in by_subject
            ):  # a resumed key's later report call replaces the earlier row
                rows[rows.index(by_subject[subject])] = row
            else:
                rows.append(row)
            by_subject[subject] = row
            continue
        # The convert call: its findings are the key's; its failure fails the row.
        row["_read"] |= read
        row["ran"] = "failed" if a["error"] else "ran"  # the last convert call decides
        row["returned"] = len(fates)
        row.update(
            {k: fates.count(k) for k in ("kept", "deduped", "merged", "rejected")}
        )
    for row in rows:
        row["files_read"] = len(row.pop("_read"))
    return [
        {
            k: r[k]
            for k in (
                "subject",
                "ran",
                "files_read",
                "returned",
                "kept",
                "deduped",
                "merged",
                "rejected",
            )
        }
        for r in rows
    ]


def finding_rows(agents: list[dict]) -> list[dict]:
    """One row per finding a finder returned (deduped copies left out): its verdict and gate outcome."""
    verdicts = {a["subject"]: a for a in agents if a["stage"] == "verify"}
    rows, seen = [], set()
    for a in agents:
        for f in a["findings"] if a["stage"] == "find" else []:
            if f["fate"] == "deduped" or f["id"] in seen:
                continue
            seen.add(f["id"])
            v = verdicts.get(f["id"])
            verdict = (
                (v["verdict"] or {}).get("status")
                if v and v["verdict"]
                else ("failed" if v else "")
            )
            if f["fate"] == "rejected":
                gate = f"rejected: {f.get('rule') or 'other'}"
            elif v and v["verdict"] and v["verdict"]["lowered"]:
                gate = "lowered by verdict_gate"
            else:
                gate = "passed"
            rows.append(
                {
                    "id": f["id"],
                    "finder": a["subject"].removesuffix(CONVERT_SUFFIX),
                    "severity": f["severity"],
                    "claim": f["claim"],
                    "verdict": verdict or "not verified",
                    "gate": gate,
                    "fate": f["fate"] + (f" into {f['into']}" if f.get("into") else ""),
                }
            )
    return rows


def _citation_lines(c: dict, web: str | None, head: str) -> list[str]:
    where = (
        f"web source: <{c['path']}>"
        if c["gate"] == "web"
        else code_link(web, head, c["path"], c["line_start"], c["line_end"])
    )
    warn = " ⚠" if c["gate"] == "fail" or c["seen"] == "not-seen" else ""
    return [
        f"- {where} — gate: {c['gate']}, seen: {c['seen']}{warn}",
        "",
        _fence(c["quote"]),
        "",
    ]


def render_checks(state: ReviewState, *, checks_file: dict | None = None) -> str:
    """`checks-<slug>.md`: the checklist, then one section per agent in the order it started."""
    data = checks_file or to_checks_file(state)
    t = state.target
    web, head = github_web_base(t.remote_url), t.head_sha
    agents = data["agents"]
    out = [
        f"# Checked — {t.label or t.slug}",
        "",
        f"{len(agents)} agent calls, {len(data['gates'])} gate results, "
        f"{t.base_sha[:10]}..{t.head_sha[:10]}.",
        "",
        "## Checklist",
        "",
        "### Finders",
        "",
    ]
    finders = finder_rows(agents)
    if finders:
        out += [
            "| finder | ran | files read | returned | kept | deduped | merged | rejected |",
            "|---|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {r['subject']} | {r['ran']} | {r['files_read']} | {r['returned']} | {r['kept']} | "
            f"{r['deduped']} | {r['merged']} | {r['rejected']} |"
            for r in finders
        ]
    else:
        out.append("No finder ran.")
    out += ["", "### Findings", ""]
    rows = finding_rows(agents)
    if rows:
        out += [
            "| finding | finder | severity | verdict | gate | fate | claim |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| `{r['id']}` | {r['finder']} | {r['severity']} | {r['verdict']} | {r['gate']} | "
            f"{_cell(r['fate'])} | {_cell(_short(r['claim'], 90))} |"
            for r in rows
        ]
    else:
        out.append("No finder returned a finding.")
    merges = [a for a in agents if a["stage"] == "merge"]
    if merges:
        out += ["", "### Merge groups", "", "| group | outcome |", "|---|---|"]
        out += [f"| {_cell(a['subject'])} | {_cell(a['outcome'])} |" for a in merges]
    out += ["", "## Agents", ""]
    for a in agents:
        out += [f"### {a['stage']}: {a['subject']} — {_one_line(a['outcome'])}", ""]
        out.append(f"- **Given:** {', '.join(a['given']) or '(nothing listed)'}")
        out.append(
            f"- **Turns:** {a['turns']}; **cost:** ${a['cost_usd']:.2f}; "
            f"**ran:** {a['started_at']} → {a['ended_at']}"
        )
        if a["error"]:
            out.append(f"- **Error:** {_one_line(a['error'])}")
        out += ["", f"**Tool calls ({len(a['calls'])}):**", ""]
        out += [
            f"- {c['tool']} `{c['target']}`"
            + (f" — {c['detail']}" if c["detail"] else "")
            for c in a["calls"]
        ] or ["- none"]
        out.append("")
        for f in a["findings"]:
            fate = (
                f["fate"]
                + (f" into `{f['into']}`" if f.get("into") else "")
                + (f" ({f['rule']})" if f.get("rule") else "")
            )
            out += [
                f"#### `{f['id']}` {f['severity']} — {_one_line(f['claim'])}",
                "",
                f"**Fate:** {fate}",
                "",
                f"**Failure scenario:** {_one_line(f['failure_scenario'])}",
                "",
            ]
            for c in f["citations"]:
                out += _citation_lines(c, web, head)
        if a["verdict"]:
            v = a["verdict"]
            lowered = " (lowered by verdict_gate)" if v["lowered"] else ""
            out += [
                f"**Verdict:** {v['status']}{lowered}. {_one_line(v['reason'])}",
                "",
            ]
            for c in v["citations"]:
                out += _citation_lines(c, web, head)
    if data["gates"]:
        failed = [g for g in data["gates"] if not g["passed"]]
        out += [
            "## Gates",
            "",
            f"{len(data['gates'])} results, {len(failed)} failed.",
            "",
        ]
        out += [f"- `{g['finding_id']}` {g['gate']}: {g['rule']}" for g in failed]
        out.append("")
    return "\n".join(out)


# --- rollups -------------------------------------------------------------------


def load_findings_files(paths: list[Path]) -> list[dict]:
    loaded = []
    for p in paths:
        try:
            loaded.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log.warning("Skipping unreadable %s: %s", p, e)
    return loaded


def render_rollup(severity: str, files: list[dict]) -> str:
    sections, total, targets = [], 0, 0
    for data in files:
        t = data["target"]
        group = [
            f
            for f in listed(data["findings"], data.get("mode"))
            if f["severity"] == severity
        ]
        if not group:
            continue
        targets += 1
        total += len(group)
        web = github_web_base(t.get("remote_url"))
        sections.append(f"## {t['label']} (`{t['slug']}`)\n")
        for name in SECTIONS:
            part = [f for f in group if section(f) == name]
            if part:
                sections.append(
                    f"**{SECTION_TITLES[name]}{SECTION_NOTES.get(name, '')}**\n"
                )
                sections += [
                    finding_section(fd, web_base=web, head_sha=t["head_sha"])
                    for fd in part
                ]
    head = [
        f"# {severity} findings — {total} across {targets} of {len(files)} targets",
        "",
    ]
    if not sections:
        head.append(f"No {severity} findings to act on.")
    return "\n".join(head + sections) + "\n"


def _object_files(out_dir: Path, pattern: str) -> list[Path]:
    """The files matching *pattern* in each `<out>/<repo>/<object>/` folder (`target.object_folders`)."""
    return [
        p
        for folder in object_folders(out_dir)
        for p in folder.glob(pattern)
        if p.is_file()
    ]


def _child_files(out_dir: Path, pattern: str) -> list[Path]:
    """The files matching *pattern* in each `<out>/<name>/` folder that may hold a review
    (`target.review_children`): the 0.46 layout, and a 0.42 review nested in *out_dir*."""
    return [
        p
        for folder in review_children(out_dir)
        for p in folder.glob(pattern)
        if p.is_file()
    ]


def _latest_rounds(out_dir: Path) -> list[dict]:
    """Each target's latest round once, sorted by path: every `<out>/<repo>/<object>/findings.json`,
    and a 0.46 `<out>/<its slug>/findings.json` only when no new-layout folder holds that slug:
    once one does, a review of the target writes there, and the 0.46 copy is an older round."""
    new = set(_object_files(out_dir, "findings.json"))
    pairs = [
        (p, d)
        for p in sorted([*_child_files(out_dir, "findings.json"), *new])
        for d in load_findings_files([p])
    ]
    newer = {_slug(d) for p, d in pairs if p in new} - {None}
    return [d for p, d in pairs if p in new or _slug(d) not in newer]


def _slug(data: dict) -> str | None:
    return (data.get("target") or {}).get("slug")


def write_rollups(out_dir: Path, paths: list[Path] | None = None) -> list[Path]:
    """`<SEV>-only.md` for each severity, from *paths* (default: every review's latest round,
    `<out>/<repo>/<object>/findings.json` and the 0.46 `<out>/<its slug>/findings.json` of a
    slug no new-layout folder holds, sorted)."""
    files = _latest_rounds(out_dir) if paths is None else load_findings_files(paths)
    written = []
    for severity in SEVERITIES:
        path = out_dir / f"{severity}-only.md"
        _write(path, render_rollup(severity, files))
        written.append(path)
    return written


# --- runs.jsonl ----------------------------------------------------------------

RUNS_FILE = "runs.jsonl"


def write_runs(out_dir: Path, paths: list[Path] | None = None) -> tuple[Path, int, int]:
    """`runs.jsonl` in *out_dir*: one line per run, from every findings.json that has a `run`.

    Each line holds the target's label, slug and head sha, `generated_at`,
    `producer` and the whole `run` object, so one `jq -s` answers questions
    across reviews. By default it reads each target's latest round
    (`<out>/<repo>/<object>/findings.json`, and the 0.46 `<out>/<its slug>/findings.json`)
    and every earlier round kept under its `rounds/*/`, so a target reviewed three times has three lines. A
    round read twice (kept, and still at the top) gets one line. Lines are
    sorted by `generated_at`. Returns (path, lines written, files skipped for
    having no `run`). Unreadable files are skipped with a warning and not
    counted.
    """
    if paths is None:
        kept = "rounds/*/findings.json"
        paths = sorted(
            [
                *_child_files(out_dir, "findings.json"),
                *_object_files(out_dir, "findings.json"),
            ]
        ) + sorted(
            p
            for p in [*_child_files(out_dir, kept), *_object_files(out_dir, kept)]
            if not p.parent.name.startswith(".")
        )
    rows, seen, skipped = [], set(), 0
    for data in load_findings_files(paths):
        if not data.get("run"):
            skipped += 1
            continue
        t = data.get("target") or {}
        key = (
            t.get("slug"),
            t.get("head_sha"),
            data.get("generated_at"),
            data["run"].get("started_at"),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            (
                str(data.get("generated_at") or ""),
                str(data["run"].get("started_at") or ""),
                json.dumps(
                    {
                        "target": {
                            "label": t.get("label"),
                            "slug": t.get("slug"),
                            "head_sha": t.get("head_sha"),
                        },
                        "generated_at": data.get("generated_at"),
                        "producer": data.get("producer"),
                        "run": data["run"],
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ),
            )
        )
    rows.sort(key=lambda r: (r[0], r[1]))
    path = out_dir / RUNS_FILE
    _write(path, "".join(line + "\n" for _, _, line in rows))
    return path, len(rows), skipped
