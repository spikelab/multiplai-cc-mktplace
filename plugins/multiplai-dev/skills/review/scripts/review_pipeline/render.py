"""Markdown: the per-target review, and severity rollups across targets.

Both render from `findings.json` v1 dicts, so a rollup never scrapes markdown.
The per-target review adds what only the pipeline state has: the severity a
finding had before it was lowered, the finders that reported it, and the
findings merged into it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .export import to_checks_file, to_findings_file
from .models import SEVERITIES, ReviewState
from .target import github_web_base

log = logging.getLogger(__name__)

SHOWN_STATUSES = ("confirmed", "unverifiable")
# Assess labels whose findings are listed after the others, in their own section.
FOLDED_LABELS = ("repeat", "low-value")


def assess_label(fd: dict) -> str:
    """The finding's assess label, or "" when the file has none (written before the stage)."""
    return str((fd.get("assessment") or {}).get("label") or "")


def assessment_line(fd: dict) -> str:
    """One line saying how the assess stage labelled the finding, or "" for `useful` and none."""
    a = fd.get("assessment") or {}
    label = a.get("label")
    if label in (None, "", "useful"):
        return ""
    reason = _one_line(a.get("reason") or "")
    if label in ("repeat", "still-open"):
        earlier = f"`{a.get('earlier_id')}`" if a.get("earlier_id") else "an earlier finding"
        if a.get("earlier_round"):
            earlier += f" (round {str(a['earlier_round'])[:12]})"
        decision = a.get("earlier_decision") or "no decision"
        note = f": {_one_line(a['earlier_note'])}" if a.get("earlier_note") else ""
        what = "repeats" if label == "repeat" else "still open from"
        return f"**Assessment:** {label} — {what} {earlier}, your decision {decision}{note}. {reason}".rstrip()
    return f"**Assessment:** {label} — {reason}".rstrip()


def code_link(web_base: str | None, head_sha: str, path: str, start: int, end: int) -> str:
    """A full-sha GitHub link for a GitHub remote, else `path:lines`."""
    lines = f"{start}" if start == end else f"{start}-{end}"
    if web_base:
        return f"[{path}:{lines}]({web_base}/blob/{head_sha}/{path}#L{start}-L{end})"
    return f"`{path}:{lines}`"


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def finding_section(fd: dict, *, web_base: str | None, head_sha: str,
                    original_severity: str | None = None,
                    finders: list[str] | None = None) -> str:
    """One finding. *finders* are the finders that reported it, from the pipeline state."""
    lines_ = f"{fd['line_start']}" if fd["line_start"] == fd["line_end"] else f"{fd['line_start']}-{fd['line_end']}"
    out = [f"### {fd['severity']} — {fd['file']}:{lines_} — {_one_line(fd['claim'])}", ""]
    if assessment_line(fd):
        out += [assessment_line(fd), ""]

    status = fd["status"]
    if original_severity and original_severity != fd["severity"]:
        status += f" (lowered from {original_severity})"
    out += [f"**Verdict:** {status}. {_one_line(fd.get('verdict_reason') or '')}".rstrip(), ""]
    if finders and len(finders) > 1:
        out += [f"**Reported by:** {', '.join(finders)}", ""]

    out.append("**Evidence:**")
    out.append("")
    for c in fd["citations"]:
        if c["path"].lower().startswith(("http://", "https://")):
            out.append(f"- web source: <{c['path']}>")
        else:
            out.append(f"- {code_link(web_base, head_sha, c['path'], c['line_start'], c['line_end'])}")
        out.append("")
        out.append(_fence(c["quote"]))
        out.append("")
    out += [f"**Failure scenario:** {_one_line(fd['failure_scenario'])}", ""]
    if fd.get("expected_behaviour"):
        out += [f"**Expected behaviour:** {_one_line(fd['expected_behaviour'])}", ""]
    if fd.get("needs"):
        out += ["**Needs you** (commands suggested by the review: read each before running it):", ""]
        out += need_lines(fd["needs"], blocks=False) + [""]
    return "\n".join(out)


CAUSE_WORDS = {
    "no-access": "the review has no access",
    "lookup-failed": "a lookup failed",
    "unreachable": "not reachable on the web",
}


def need_lines(needs: list[dict], findings: list[dict] | None = None, *, blocks: bool = True) -> list[str]:
    """One markdown bullet per need: what is missing, what it blocks, why, and the command.

    With *findings*, a need that blocks a finding names its file and line.
    *blocks* False leaves out what it blocks (under the finding itself).
    """
    where = {f["id"]: f"`{f['file']}:{f['line_start']}`" for f in findings or []}
    out = []
    for n in needs:
        what = _one_line(n["what"]).rstrip(".") + "."
        cause = CAUSE_WORDS.get(n["cause"], n["cause"])
        if blocks:
            on = "the review" if n["blocks"] == "review" else where.get(n["blocks"], f"finding `{n['blocks']}`")
            line = f"- {what} Blocks {on}; {cause}."
        else:
            line = f"- {what} Why: {cause}."
        line += f" Run: `{n['command']}`" if n.get("command") else " No command is known."
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
    return (f"${run['cost_usd']:.2f} over {calls} agent call{'' if calls == 1 else 's'}, "
            f"{run['tokens']['total']:,} tokens, {duration(run['wall_seconds'])} wall time")


def _counts(findings: list[dict]) -> dict[str, int]:
    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    return {s: sum(1 for f in shown if f["severity"] == s) for s in SEVERITIES}


def render_review(state: ReviewState, *, deployed: str | None = None,
                  findings_file: dict | None = None) -> str:
    data = findings_file or to_findings_file(state)
    t = state.target
    web = github_web_base(t.remote_url)
    head = t.head_sha
    findings = data["findings"]
    counts = _counts(findings)

    commit_link = (lambda sha: f"[{sha[:10]}]({web}/commit/{sha})") if web else (lambda sha: f"`{sha[:10]}`")
    out = [f"# Review — {t.label or t.slug}", ""]
    out.append(f"- **Repo:** {Path(t.repo_path).name}" + (f" ({web})" if web else f" (`{t.repo_path}`)"))
    if t.tickets:
        out.append(f"- **Tickets:** {', '.join(t.tickets)}")
    if t.is_tree:
        # Not a change: no base commit and no commits, only the files as they stand at head.
        where = "the whole tree" if t.ref in ("", ".") else f"`{t.ref}`"
        out.append(f"- **Reviewed:** {where} at {commit_link(head)}, not a change")
        out.append(f"- **Files reviewed:** {len(t.files)}; {len(t.skipped)} skipped (`skipped.txt`)")
    else:
        out.append(f"- **Base commit:** {commit_link(t.base_sha)}")
        out.append(f"- **Head commit:** {commit_link(head)}")
        out.append(f"- **Commits:** {len(t.commits)}")
        out += [f"  - {commit_link(sha)} {_one_line(subject)}" for sha, subject in t.commits]
        out.append(f"- **Files changed:** {len(t.files)}")
        out += [f"  - `{f}`" for f in t.files]
    if t.deployed_in:
        out.append(f"- **Deployed in {t.deployed_in}:** {deployed or 'unknown'}")
    refuted = sum(1 for f in findings if f["status"] == "refuted")
    rejected = sum(1 for f in findings if f["status"] == "rejected")
    out.append(f"- **Findings:** {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW; "
               f"{refuted} refuted, {rejected} rejected by the gates (see the appendix)")
    if data.get("run"):
        out.append(f"- **Model cost:** {cost_line(data['run'])}")
    if state.errors:
        out.append("- **Agent failures:** " + "; ".join(_one_line(e) for e in state.errors))
    review_needs = [n for n in data.get("needs", []) if n["blocks"] == "review"]
    if review_needs:
        out += ["", "## Needs you", "",
                "What the review could not check. Each command was suggested by the review: read it before "
                "running it.", ""]
        out += need_lines(review_needs)
    out += ["", "## Findings", ""]

    finders = {f.id: f.finders for f in state.findings}

    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    folded = [f for f in shown if assess_label(f) in FOLDED_LABELS]
    shown = [f for f in shown if assess_label(f) not in FOLDED_LABELS]
    if not shown:
        out += ["No confirmed or unverifiable findings." if not folded else
                "Every confirmed or unverifiable finding is a repeat or low-value; see below.", ""]
    for severity in SEVERITIES:
        group = [f for f in shown if f["severity"] == severity]
        if not group:
            continue
        out += [f"## {severity}", ""]
        for fd in group:
            out.append(finding_section(fd, web_base=web, head_sha=head,
                                       original_severity=state.original_severity.get(fd["id"]),
                                       finders=finders.get(fd["id"])))
    if folded:
        out += ["## Repeats and low-value findings", "",
                "Each is still a confirmed or unverifiable finding: the assess stage marked it as a repeat of "
                "one you rejected in an earlier round, or as not worth acting on, with its reason. Nothing "
                "was deleted.", ""]
        for fd in folded:
            out.append(finding_section(fd, web_base=web, head_sha=head,
                                       original_severity=state.original_severity.get(fd["id"]),
                                       finders=finders.get(fd["id"])))

    out += ["## Appendix — rejected, refuted and merged", ""]
    dropped = [f for f in findings if f["status"] in ("refuted", "rejected")]
    if not dropped and not state.merged:
        out += ["Nothing was rejected, refuted or merged.", ""]
    for fd in dropped:
        lines_ = f"{fd['line_start']}-{fd['line_end']}"
        who = "the verifier" if fd["status"] == "refuted" else "a gate"
        out.append(f"- **{fd['status']}** ({fd['severity']}) `{fd['file']}:{lines_}` — {_one_line(fd['claim'])}  ")
        out.append(f"  Reason from {who}: {_one_line(fd.get('verdict_reason') or '')}")
    for m in state.merged:
        f = m.finding
        out.append(f"- **merged** ({f.severity}) `{f.file}:{f.line_start}-{f.line_end}` — {_one_line(f.claim)}  ")
        out.append(f"  Merged into `{m.into}`: {_one_line(m.reason)}")
    out.append("")
    return "\n".join(out)


def write_review(state: ReviewState, target_dir: Path, *, deployed: str | None = None) -> Path:
    """Write the full review, its short summary and the checks record; return the full review's path."""
    path = target_dir / f"review-{state.target.slug}.md"
    path.write_text(render_review(state, deployed=deployed), encoding="utf-8")
    summary_path(state, target_dir).write_text(render_summary(state), encoding="utf-8")
    (target_dir / f"checks-{state.target.slug}.md").write_text(render_checks(state), encoding="utf-8")
    return path


# --- summary -------------------------------------------------------------------

SUMMARY_CLAIM_CHARS = 120


def _short(text: str, limit: int = SUMMARY_CLAIM_CHARS) -> str:
    """The first sentence of `text`, cut to `limit` characters."""
    text = _one_line(text)
    end = re.search(r"\. (?=[A-Z])", text)
    if end:
        text = text[:end.start() + 1]
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def summary_path(state: ReviewState, target_dir: Path) -> Path:
    return target_dir / f"summary-{state.target.slug}.md"


def render_summary(state: ReviewState, *, findings_file: dict | None = None) -> str:
    """How the review went, short enough to read in chat.

    One line per HIGH and MEDIUM finding, a count for LOW, and one line per
    finding the verifier refuted or a gate rejected.
    """
    data = findings_file or to_findings_file(state)
    t = state.target
    findings = data["findings"]
    counts = _counts(findings)
    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    dropped = [f for f in findings if f["status"] in ("refuted", "rejected")]

    out = [f"# Review summary — {t.label or t.slug}", ""]
    if t.is_tree:
        out.append(f"{len(t.files)} files reviewed as they stand at {t.head_sha[:10]}, {len(t.skipped)} skipped.")
    else:
        out.append(f"{len(t.commits)} commits, {len(t.files)} files, {t.base_sha[:10]}..{t.head_sha[:10]}.")
    if data.get("run"):
        out.append(f"Cost {cost_line(data['run'])}.")
    out.append(f"Findings: {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW. Dropped: "
               f"{sum(1 for f in dropped if f['status'] == 'refuted')} refuted by the verifier, "
               f"{sum(1 for f in dropped if f['status'] == 'rejected')} rejected by the gates"
               + (f", {len(state.merged)} merged into another finding as duplicates." if state.merged else "."))
    labels = [assess_label(f) for f in shown]
    if any(labels):
        out.append(f"Assessed: {labels.count('repeat')} repeats of rejected findings, "
                   f"{labels.count('low-value')} low-value, {labels.count('still-open')} still open from "
                   f"earlier rounds (repeats and low-value are listed last in the full review).")
    if data.get("needs"):
        out += ["", "## Needs you", "",
                "The review could not get these. Each command was suggested by the review: read it before "
                "running it.", ""]
        out += need_lines(data["needs"], findings)
        out.append("")
    if state.errors:
        out.append("Agent failures: " + "; ".join(_short(e) for e in state.errors))

    listed = [f for f in shown if assess_label(f) not in FOLDED_LABELS]
    for severity in ("HIGH", "MEDIUM"):
        group = [f for f in listed if f["severity"] == severity]
        if not group:
            continue
        out += ["", f"## {severity}", ""]
        for fd in group:
            lines_ = f"{fd['line_start']}" if fd["line_start"] == fd["line_end"] else f"{fd['line_start']}-{fd['line_end']}"
            still = ", still open" if assess_label(fd) == "still-open" else ""
            out.append(f"- `{fd['file']}:{lines_}` — {_short(fd['claim'])} ({fd['status']}{still})")
    low = sum(1 for f in listed if f["severity"] == "LOW")
    if low:
        out += ["", f"{low} LOW findings are in the full review."]
    if dropped:
        out += ["", "## Dropped", ""]
        for fd in dropped:
            # A verifier's reason often opens by conceding part of the claim, so
            # its first sentence misleads; the full reason is in the review's appendix.
            # A gate's reason is one mechanical sentence and fits.
            why = "" if fd["status"] == "refuted" else f" ({_one_line(fd.get('verdict_reason') or '')})"
            who = "refuted" if fd["status"] == "refuted" else "rejected by a gate"
            out.append(f"- {who}: `{fd['file']}:{fd['line_start']}` — {_short(fd['claim'], 90)}{why}")
    out += ["", f"Full review, with every reason: `review-{t.slug}.md`", ""]
    return "\n".join(out)


# --- checks ----------------------------------------------------------------------


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def finder_rows(agents: list[dict]) -> list[dict]:
    """One row per finder call: ran or failed, files read, findings returned and their fates."""
    rows = []
    for a in agents:
        if a["stage"] != "find":
            continue
        fates = [f["fate"] for f in a["findings"]]
        rows.append({
            "subject": a["subject"], "ran": "failed" if a["error"] else "ran",
            "files_read": len({c["target"] for c in a["calls"] if c["tool"] == "Read"}),
            "returned": len(fates), **{k: fates.count(k) for k in ("kept", "deduped", "merged", "rejected")},
        })
    return rows


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
            verdict = (v["verdict"] or {}).get("status") if v and v["verdict"] else ("failed" if v else "")
            if f["fate"] == "rejected":
                gate = f"rejected: {f.get('rule') or 'other'}"
            elif v and v["verdict"] and v["verdict"]["lowered"]:
                gate = "lowered by verdict_gate"
            else:
                gate = "passed"
            rows.append({"id": f["id"], "finder": a["subject"], "severity": f["severity"], "claim": f["claim"],
                         "verdict": verdict or "not verified", "gate": gate,
                         "fate": f["fate"] + (f" into {f['into']}" if f.get("into") else "")})
    return rows


def _citation_lines(c: dict, web: str | None, head: str) -> list[str]:
    where = (f"web source: <{c['path']}>" if c["gate"] == "web"
             else code_link(web, head, c["path"], c["line_start"], c["line_end"]))
    warn = " ⚠" if c["gate"] == "fail" or c["seen"] == "not-seen" else ""
    return [f"- {where} — gate: {c['gate']}, seen: {c['seen']}{warn}", "", _fence(c["quote"]), ""]


def render_checks(state: ReviewState, *, checks_file: dict | None = None) -> str:
    """`checks-<slug>.md`: the checklist, then one section per agent in the order it started."""
    data = checks_file or to_checks_file(state)
    t = state.target
    web, head = github_web_base(t.remote_url), t.head_sha
    agents = data["agents"]
    out = [f"# Checked — {t.label or t.slug}", "",
           f"{len(agents)} agent calls, {len(data['gates'])} gate results, "
           f"{t.base_sha[:10]}..{t.head_sha[:10]}.", "", "## Checklist", "", "### Finders", ""]
    finders = finder_rows(agents)
    if finders:
        out += ["| finder | ran | files read | returned | kept | deduped | merged | rejected |",
                "|---|---|---|---|---|---|---|---|"]
        out += [f"| {r['subject']} | {r['ran']} | {r['files_read']} | {r['returned']} | {r['kept']} | "
                f"{r['deduped']} | {r['merged']} | {r['rejected']} |" for r in finders]
    else:
        out.append("No finder ran.")
    out += ["", "### Findings", ""]
    rows = finding_rows(agents)
    if rows:
        out += ["| finding | finder | severity | verdict | gate | fate | claim |", "|---|---|---|---|---|---|---|"]
        out += [f"| `{r['id']}` | {r['finder']} | {r['severity']} | {r['verdict']} | {r['gate']} | "
                f"{_cell(r['fate'])} | {_cell(_short(r['claim'], 90))} |" for r in rows]
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
        out.append(f"- **Turns:** {a['turns']}; **cost:** ${a['cost_usd']:.2f}; "
                   f"**ran:** {a['started_at']} → {a['ended_at']}")
        if a["error"]:
            out.append(f"- **Error:** {_one_line(a['error'])}")
        out += ["", f"**Tool calls ({len(a['calls'])}):**", ""]
        out += [f"- {c['tool']} `{c['target']}`" + (f" — {c['detail']}" if c["detail"] else "")
                for c in a["calls"]] or ["- none"]
        out.append("")
        for f in a["findings"]:
            fate = f["fate"] + (f" into `{f['into']}`" if f.get("into") else "") + \
                (f" ({f['rule']})" if f.get("rule") else "")
            out += [f"#### `{f['id']}` {f['severity']} — {_one_line(f['claim'])}", "",
                    f"**Fate:** {fate}", "", f"**Failure scenario:** {_one_line(f['failure_scenario'])}", ""]
            for c in f["citations"]:
                out += _citation_lines(c, web, head)
        if a["verdict"]:
            v = a["verdict"]
            lowered = " (lowered by verdict_gate)" if v["lowered"] else ""
            out += [f"**Verdict:** {v['status']}{lowered}. {_one_line(v['reason'])}", ""]
            for c in v["citations"]:
                out += _citation_lines(c, web, head)
    if data["gates"]:
        failed = [g for g in data["gates"] if not g["passed"]]
        out += ["## Gates", "", f"{len(data['gates'])} results, {len(failed)} failed.", ""]
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
        group = [f for f in data["findings"] if f["severity"] == severity and f["status"] in SHOWN_STATUSES]
        if not group:
            continue
        targets += 1
        total += len(group)
        web = github_web_base(t.get("remote_url"))
        sections.append(f"## {t['label']} (`{t['slug']}`)\n")
        for fd in group:
            sections.append(finding_section(fd, web_base=web, head_sha=t["head_sha"]))
    head = [f"# {severity} findings — {total} across {targets} of {len(files)} targets", ""]
    if not sections:
        head.append(f"No confirmed or unverifiable {severity} findings.")
    return "\n".join(head + sections) + "\n"


def write_rollups(out_dir: Path, paths: list[Path] | None = None) -> list[Path]:
    """`<SEV>-only.md` for each severity, from *paths* (default: `<out>/*/findings.json`, sorted)."""
    if paths is None:
        paths = sorted(out_dir.glob("*/findings.json"))
    files = load_findings_files(paths)
    written = []
    for severity in SEVERITIES:
        path = out_dir / f"{severity}-only.md"
        path.write_text(render_rollup(severity, files), encoding="utf-8")
        written.append(path)
    return written


# --- runs.jsonl ----------------------------------------------------------------

RUNS_FILE = "runs.jsonl"


def write_runs(out_dir: Path, paths: list[Path] | None = None) -> tuple[Path, int, int]:
    """`runs.jsonl` in *out_dir*: one line per findings.json that has a `run`.

    Each line holds the target's label, slug and head sha, `generated_at`,
    `producer` and the whole `run` object, so one `jq -s` answers questions
    across reviews. Returns (path, lines written, files skipped for having no
    `run`). Unreadable files are skipped with a warning and not counted.
    """
    if paths is None:
        paths = sorted(out_dir.glob("*/findings.json"))
    lines, skipped = [], 0
    for data in load_findings_files(paths):
        if not data.get("run"):
            skipped += 1
            continue
        t = data.get("target") or {}
        lines.append(json.dumps({
            "target": {"label": t.get("label"), "slug": t.get("slug"), "head_sha": t.get("head_sha")},
            "generated_at": data.get("generated_at"),
            "producer": data.get("producer"),
            "run": data["run"],
        }, sort_keys=True, ensure_ascii=False))
    path = out_dir / RUNS_FILE
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path, len(lines), skipped
