"""`round-metrics`: how the review's rounds went, from the rounds it kept. Read-only.

For every `<out>/<repo>/<object>/` folder (`target.object_folders`) it reads
the kept rounds (`rounds._round_dirs`, those with a `findings.json`) and the
top-level `findings.json` when that file is not itself a kept round, sorted
by `generated_at`. Per round it counts the listed findings (`render.listed`,
in the round's own mode) and the `repeat`, `still-open` and `low-value`
labels. For each listed finding from round 2 on, it traces the first repo
citation's line (not a web or `.review-deps/` citation): `git blame` at that
round's head names the commit that wrote the line, and when that commit is
an ancestor of round 1's head the line was `present at first review`,
otherwise `written since`. A failed git command, an unknown head or a missing
repository gives `unknown`; a finding with no repo citation is `no line`.

The rule is copied from buildme's `review_metrics.py`, not imported: the two
packages do not import each other.

A round whose recorded `repo_path` no longer exists (a deleted worktree) may
be read in another checkout given with `--repo`, when that checkout's
`origin` names the same repository as the round's `remote_url`.

With `--counts` it also prints, per round, the confirmed findings by
severity, impact and topic, the number of finder reports (`report-*.md` in
the round's directory) and how many of them state that a search returned
nothing (`EMPTY_SEARCH_RE`).

It prints Markdown and writes nothing. It uses no network and calls no model;
the only commands it runs are `git blame`, `git merge-base --is-ancestor` and
`git remote get-url`, all read-only.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import rounds, target
from .models import DEPS_DIR
from .render import assess_label
from .render import listed as listed_findings

PRESENT = "present at first review"
WRITTEN = "written since"
UNKNOWN = "unknown"
NO_LINE = "no line"
ORIGINS = (PRESENT, WRITTEN, UNKNOWN, NO_LINE)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A finder report that states a search returned nothing or did not work: a search word
# (grep, search, rg) and, later in the same sentence, "returned nothing", "came back empty",
# "found no matches", "isn't working", "is blocked" and the like. Chosen by reading the
# 102 finder reports of three PR reviews run on 2026-10-29, whose directory searches all
# came back empty; a report that says a search found nothing for a term that is truly
# absent counts too.
EMPTY_SEARCH_RE = re.compile(
    r"\b(?:grep|greps|search|searches|searching|ripgrep|rg)\b[^.\n]{0,120}?"
    r"(?:\b(?:return(?:s|ed|ing)?|came back|comes back|coming back|finds?|found|gives?|gave"
    r"|lists?|listed)\b[^.\n]{0,30}?"
    r"(?:\bnothing\b|\bno (?:match(?:es)?|results?|hits?|files?)\b|\bempty\b|\banything\b)"
    r"|\b(?:is ?n[o']t (?:working|returning|searching|finding)|unable to search"
    r"|could ?n[o']t run|misses files|is skipping files|did ?n[o']t find|does ?n[o']t work"
    r"|did not work|didn't work|is broken|broken here|is failing|failed\b|blocked"
    r"|keeps? (?:returning|coming back)))",
    re.IGNORECASE,
)
REPORT_GLOB = "report-*.md"
SEVERITIES = ("HIGH", "MEDIUM", "LOW")


@dataclass
class RoundMetrics:
    generated_at: str
    head_sha: str
    listed: int
    repeat: int
    still_open: int
    low_value: int
    repo: Path | None  # where git ran for this round, or None
    repo_stand_in: (
        bool  # True when *repo* is a `--repo` checkout, the recorded one being gone
    )
    origins: Counter = field(default_factory=Counter)
    reports: int = 0  # finder reports in the round's directory
    empty_search: int = 0  # of those, reports that state a search returned nothing
    severity: Counter = field(default_factory=Counter)  # confirmed findings
    impact: Counter = field(default_factory=Counter)
    topic: Counter = field(default_factory=Counter)


@dataclass
class TargetMetrics:
    target_dir: Path
    rounds: list[RoundMetrics]
    stopped: int  # kept runs that stopped before export: no findings.json, left out


def target_rounds(target_dir: Path) -> tuple[list[tuple[Path, dict]], int]:
    """The rounds of *target_dir* that have a `findings.json`, each with its directory, sorted by
    `generated_at`, and the number of kept rounds left out because they have none."""
    found, stopped = [], 0
    for d in rounds._round_dirs(target_dir):
        data = rounds._read_json(d / "findings.json")
        if data is None:
            stopped += 1
            continue
        found.append((d, data))
    top = rounds._read_json(target_dir / "findings.json")
    if top is not None and rounds._kept_already(target_dir, top) is None:
        found.append((target_dir, top))
    found.sort(key=lambda pair: str(pair[1].get("generated_at") or ""))
    return found, stopped


def search_counts(round_dir: Path) -> tuple[int, int]:
    """The finder reports in *round_dir*, and how many state that a search returned nothing."""
    reports = sorted(round_dir.glob(REPORT_GLOB))
    empty = 0
    for path in reports:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        empty += EMPTY_SEARCH_RE.search(text) is not None
    return len(reports), empty


def first_repo_citation(finding: dict) -> tuple[str, int] | None:
    """The path and first line of the finding's first citation in the repository, or None."""
    for c in finding.get("citations") or []:
        if not isinstance(c, dict):
            continue
        path, line = c.get("path"), c.get("line_start")
        if (
            not isinstance(path, str)
            or not path
            or isinstance(line, bool)
            or not isinstance(line, int)
        ):
            continue
        if (
            line < 1
            or path.lower().startswith(("http://", "https://"))
            or path.startswith(DEPS_DIR + "/")
        ):
            continue
        return path, line
    return None


def _git(repo: Path, *argv: str):
    try:
        return target.git(repo, *argv, check=False)
    except OSError:
        return None


def line_origin(
    repo: Path | None,
    path: str,
    line: int,
    head_sha: str | None,
    first_head_sha: str | None,
) -> str:
    """Whether *path*:*line* at *head_sha* was already in round 1's head (`PRESENT`), was
    written after it (`WRITTEN`), or cannot be told (`UNKNOWN`)."""
    if repo is None or not repo.is_dir() or not head_sha or not first_head_sha:
        return UNKNOWN
    blame = _git(
        repo, "blame", "-L", f"{line},{line}", "--porcelain", head_sha, "--", path
    )
    if blame is None or blame.returncode != 0 or not blame.stdout.strip():
        return UNKNOWN
    blamed = blame.stdout.split()[0]
    if not _SHA_RE.match(blamed):
        return UNKNOWN
    ancestor = _git(repo, "merge-base", "--is-ancestor", blamed, first_head_sha)
    if ancestor is None:
        return UNKNOWN
    if ancestor.returncode == 0:
        return PRESENT
    if ancestor.returncode == 1:
        return WRITTEN
    return UNKNOWN


def _same_repo(a: str | None, b: str | None) -> bool:
    pa, pb = target.origin_owner_repo(a), target.origin_owner_repo(b)
    return (
        pa is not None
        and pb is not None
        and tuple(map(str.lower, pa)) == tuple(map(str.lower, pb))
    )


def _repo_for(
    data: dict, stand_ins: list[tuple[Path, str | None]]
) -> tuple[Path | None, bool]:
    """The round's recorded repository when it exists, else the first `--repo` checkout of the
    same remote, else None; and whether the second was used."""
    t = data.get("target") or {}
    recorded = str(t.get("repo_path") or "")
    if recorded and Path(recorded).is_dir():
        return Path(recorded), False
    for repo, url in stand_ins:
        if _same_repo(url, t.get("remote_url")):
            return repo, True
    return None, False


def _listable(data: dict) -> list[dict]:
    return [
        f for f in data.get("findings") or [] if isinstance(f, dict) and "status" in f
    ]


def measure_target(
    target_dir: Path, stand_ins: list[tuple[Path, str | None]] = ()
) -> TargetMetrics:
    found, stopped = target_rounds(target_dir)
    first_head = str(
        ((found[0][1] if found else {}).get("target") or {}).get("head_sha") or ""
    )
    result = []
    for n, (round_dir, data) in enumerate(found, start=1):
        findings = _listable(data)
        listed = listed_findings(findings, data.get("mode"))
        labels = Counter(assess_label(f) for f in findings)
        still_open = sum(assess_label(f) == "still-open" for f in listed)
        head = str((data.get("target") or {}).get("head_sha") or "")
        repo, stand_in = _repo_for(data, list(stand_ins))
        metrics = RoundMetrics(
            generated_at=str(data.get("generated_at") or ""),
            head_sha=head,
            listed=len(listed),
            repeat=labels["repeat"],
            still_open=still_open,
            low_value=labels["low-value"],
            repo=repo,
            repo_stand_in=stand_in,
        )
        metrics.reports, metrics.empty_search = search_counts(round_dir)
        for f in findings:
            if f.get("status") == "confirmed":
                metrics.severity[str(f.get("severity") or "none")] += 1
                metrics.impact[str(f.get("impact") or "none")] += 1
                metrics.topic[str(f.get("topic") or "none")] += 1
        if n >= 2:
            for f in listed:
                where = first_repo_citation(f)
                origin = (
                    NO_LINE
                    if where is None
                    else line_origin(repo, *where, head, first_head)
                )
                metrics.origins[origin] += 1
        result.append(metrics)
    return TargetMetrics(target_dir=target_dir, rounds=result, stopped=stopped)


def measure(out: Path, repos: list[Path] | tuple = ()) -> list[TargetMetrics]:
    """Every target under *out* that has at least one round. *repos* are checkouts to read a round
    in when its recorded repository is gone."""
    stand_ins = [
        (Path(r).resolve(), target.remote_url(Path(r).resolve())) for r in repos
    ]
    measured = (measure_target(d, stand_ins) for d in target.object_folders(out))
    return [m for m in measured if m.rounds]


def _share(part: int, whole: int) -> str:
    return f"{part} of {whole} ({part / whole:.0%})" if whole else f"{part} of 0"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tally(c: Counter) -> str:
    """`name n, name n` by count, most first; `–` when empty."""
    items = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k} {v}" for k, v in items) or "–"


def _counts_table(t: TargetMetrics) -> list[str]:
    lines = [
        "Counts per round (confirmed findings; finder reports):",
        "",
        "| Round | Head | Confirmed | " + " | ".join(SEVERITIES) + " | Impact | Topic "
        "| Reports | Search returned nothing |",
        "|---|---|---|" + "---|" * len(SEVERITIES) + "---|---|---|---|",
    ]
    for n, r in enumerate(t.rounds, start=1):
        severities = " | ".join(str(r.severity[s]) for s in SEVERITIES)
        lines.append(
            f"| {n} | `{r.head_sha[:12]}` | {sum(r.severity.values())} | {severities} "
            f"| {_tally(r.impact)} | {_tally(r.topic)} | {r.reports} "
            f"| {_share(r.empty_search, r.reports)} |"
        )
    lines.append("")
    return lines


def _counts_totals(metrics: list[TargetMetrics]) -> str:
    every = [r for t in metrics for r in t.rounds]
    severity: Counter = Counter()
    for r in every:
        severity.update(r.severity)
    by_severity = ", ".join(f"{s} {severity[s]}" for s in SEVERITIES)
    other = sum(v for k, v in severity.items() if k not in SEVERITIES)
    return (
        f"**Counts** ({_plural(len(every), 'round')}): finder reports that state a search returned "
        f"nothing: {_share(sum(r.empty_search for r in every), sum(r.reports for r in every))}; "
        f"confirmed findings summed over the rounds (a finding confirmed in two rounds counts "
        f"twice): {sum(severity.values())} ({by_severity}"
        + (f", other {other}" if other else "")
        + ")."
    )


def render(metrics: list[TargetMetrics], out: Path, counts: bool = False) -> str:
    lines = ["# Review round metrics", "", f"Scanned: `{out}`", ""]
    if not metrics:
        lines.append("No review rounds found.")
        return "\n".join(lines) + "\n"
    for t in metrics:
        name = f"{t.target_dir.parent.name}/{t.target_dir.name}"
        left_out = (
            f" ({_plural(t.stopped, 'kept run')} that stopped before export left out)"
            if t.stopped
            else ""
        )
        lines += [
            f"## `{name}`",
            "",
            f"{_plural(len(t.rounds), 'round')}{left_out}.",
            "",
            "| Round | Head | Generated | Listed | still-open | repeat | low-value "
            "| Round 2+: present at first review | Round 2+: written since | Round 2+: unknown "
            "| Round 2+: no line | Repository |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for n, r in enumerate(t.rounds, start=1):
            origins = " | ".join(str(r.origins[o]) if n >= 2 else "–" for o in ORIGINS)
            repo = (
                "none"
                if r.repo is None
                else f"`{r.repo}`" + (" (from --repo)" if r.repo_stand_in else "")
            )
            lines.append(
                f"| {n} | `{r.head_sha[:12]}` | {r.generated_at} | {r.listed} | {r.still_open} "
                f"| {r.repeat} | {r.low_value} | {origins} | {repo} |"
            )
        lines.append("")
        if counts:
            lines += _counts_table(t)
    multi = [t for t in metrics if len(t.rounds) >= 2]
    mean = f"{sum(len(t.rounds) for t in multi) / len(multi):.2f}" if multi else "n/a"
    origins: Counter = Counter()
    later_listed = later_still = 0
    for t in metrics:
        for r in t.rounds[1:]:
            origins.update(r.origins)
            later_listed += r.listed
            later_still += r.still_open
    known = origins[PRESENT] + origins[WRITTEN]
    lines.append(
        f"**Totals** ({_plural(len(metrics), 'target')}, "
        f"{_plural(sum(len(t.rounds) for t in metrics), 'round')}): "
        f"mean rounds per target with 2 or more rounds: {mean} (over {_plural(len(multi), 'target')}); "
        f"round-2+ listed findings present at first review: {_share(origins[PRESENT], known)} "
        f"with a known origin ({origins[WRITTEN]} written since, {origins[UNKNOWN]} unknown, "
        f"{origins[NO_LINE]} with no repo citation); "
        f"still-open: {_share(later_still, later_listed)} round-2+ listed findings."
    )
    if counts:
        lines += ["", _counts_totals(metrics)]
    return "\n".join(lines) + "\n"
