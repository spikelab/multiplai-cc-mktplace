"""What the review's own files say about this PR's findings, round by round.

Every review of the PR writes `findings.json` in its target directory, and
the next review of that directory first copies it to
`rounds/<NN>-<sha12>/findings.json` (`review_pipeline/rounds.py`,
`keep_round`). The round loop reads them all, oldest first, to give triage
the earlier verdicts on the same lineage (`earlier_rows`), to list a
finding's `other_locations` in the tasks file, to fill the escalation text
of the last round (`history_text`), and to name the lineages that recurred
when the round cap is reached (`recurring`).

The target directories are those the review ledger records for this PR
(`ledger_dirs`: the review before the first round is one) and each round's
`review_run_dir`. buildme does not import `review_pipeline`
(`decisions.py`), so the few rules read here are copies: a finding's
lineage is its `lineage`, else its own id (a file written before
multiplai-dev 0.94 has none), and a lineage is open in a round when that
round lists one of its findings or gives it an `earlier` status other than
`resolved` (`review_pipeline.render.open_lineages`). A file that cannot be
read is left out, with a warning: it can only remove history, never a
finding.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from multiplai_core.untrusted import defang, fence

from .decisions import _listed
from .models import PRComment, TriageRecord

log = logging.getLogger(__name__)

FINDINGS_FILE = "findings.json"
ROUNDS_DIR = "rounds"


def ledger_dirs(project_dir: Path, pr: int) -> list[str]:
    """The target directory of every finished review of PR *pr* in the review
    ledger, in ledger order, under the slug `loop.review_spend` reads
    (`<owner>-<repo>--pr-<n>` from the repository's origin)."""
    from .. import git_ops
    from ..epic import review, store

    root = git_ops.repo_root(project_dir) or project_dir
    slug = store.sanitize_slug(f"{store.slug_repo_name(Path(root))}--pr-{pr}")
    out: list[str] = []
    for line in review.ledger_lines():
        target = line.get("target_dir")
        if line.get("event") == "finished" and line.get("slug") == slug and target and target not in out:
            out.append(str(target))
    return out


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        log.warning("cannot read the review's %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _key(data: dict) -> tuple[str, str]:
    """What tells two copies of one review apart (`review_pipeline.rounds._round_key`)."""
    return str(data.get("generated_at") or ""), str((data.get("run") or {}).get("started_at") or "")


def review_files(dirs: Iterable[str | None]) -> list[dict]:
    """Every review `findings.json` in *dirs* and their kept `rounds/`, each
    review once, oldest first (`generated_at`)."""
    seen: dict[tuple[str, str], dict] = {}
    for d in dict.fromkeys(str(d) for d in dirs if d):
        root = Path(d)
        rounds = root / ROUNDS_DIR
        kept = (sorted(p for p in rounds.iterdir() if p.is_dir() and not p.name.startswith("."))
                if rounds.is_dir() else [])
        for path in [*(p / FINDINGS_FILE for p in kept), root / FINDINGS_FILE]:
            data = _read(path)
            if data is not None:
                seen.setdefault(_key(data), data)
    return sorted(seen.values(), key=lambda d: str(d.get("generated_at") or ""))


def _findings(data: dict) -> list[dict]:
    return [f for f in data.get("findings") or [] if isinstance(f, dict) and f.get("id")]


def _earlier(data: dict) -> list[dict]:
    return [e for e in data.get("earlier") or [] if isinstance(e, dict) and e.get("lineage")]


def lineage(finding: dict) -> str:
    return str(finding.get("lineage") or finding["id"])


def lineages(files: list[dict]) -> dict[str, str]:
    """Finding id → its lineage, from every finding and `earlier` entry."""
    out: dict[str, str] = {}
    for data in files:
        for f in _findings(data):
            out[str(f["id"])] = lineage(f)
        for e in _earlier(data):
            if e.get("earlier_id"):
                out.setdefault(str(e["earlier_id"]), str(e["lineage"]))
    return out


def _lineage_of(comment: PRComment, ids: dict[str, str]) -> str | None:
    fid = comment.finding_id
    return ids.get(fid, fid) if fid else None


def earlier_rows(earlier: list[TriageRecord], comments: list[PRComment],
                 files: list[dict]) -> dict[int, list[TriageRecord]]:
    """Comment id → the *earlier* rounds' triaged records on the same lineage,
    oldest first. A comment without a review finding id has none."""
    ids = lineages(files)
    out: dict[int, list[TriageRecord]] = {}
    for c in comments:
        lin = _lineage_of(c, ids)
        if lin is None:
            continue
        rows = [r for r in earlier if r.triage is not None and _lineage_of(r.comment, ids) == lin]
        if rows:
            out[c.id] = sorted(rows, key=lambda r: (r.round, r.comment.id))
    return out


def earlier_text(rows: list[TriageRecord]) -> str:
    """The triage prompt's earlier verdicts: each row fenced as data."""
    if not rows:
        return "None: no earlier round triaged a finding of this lineage."
    parts = []
    for r in rows:
        assert r.triage is not None
        ident = r.comment.finding_id or r.comment.label
        text = f"Round {r.round}, finding {ident}: {r.triage.verdict.value}. {r.triage.reason}"
        parts.append("\n".join(fence(text, source=f"earlier triage, round {r.round}", limit=2000)))
    return "\n\n".join(parts)


def other_locations(files: list[dict], finding_id: str | None) -> list[str]:
    """The `other_locations` the latest review that holds *finding_id* gave it."""
    if not finding_id:
        return []
    for data in reversed(files):
        for f in _findings(data):
            if str(f["id"]) == finding_id:
                return [str(x) for x in f.get("other_locations") or [] if str(x).strip()]
    return []


def _statuses(data: dict) -> dict[str, list[str]]:
    """Lineage → what this review said about it: `listed` for a finding it
    lists, and each `earlier` status."""
    out: dict[str, list[str]] = {}
    for f in _findings(data):
        if _listed(f, data.get("mode")):
            out.setdefault(lineage(f), []).append("listed")
    for e in _earlier(data):
        out.setdefault(str(e["lineage"]), []).append(str(e.get("status") or "cannot_verify"))
    return out


def history_text(files: list[dict]) -> str:
    """Every lineage of the PR's reviews, with its latest claim and what each
    review said about it: the `{findings}` of `REVIEW_FIX_ESCALATION`."""
    claims: dict[str, str] = {}
    said: dict[str, list[str]] = {}
    for n, data in enumerate(files, start=1):
        head = str((data.get("target") or {}).get("head_sha") or "")[:12]
        for f in _findings(data):
            claims[lineage(f)] = str(f.get("claim") or "")
        for lin, statuses in _statuses(data).items():
            at = f" at `{head}`" if head else ""
            said.setdefault(lin, []).append(f"review {n}{at}: {', '.join(statuses)}")
    if not said:
        return "(the review files of this pull request hold no findings)"
    lines = []
    for lin, rounds in said.items():
        claim = " ".join(defang(claims.get(lin, ""), 300).split()) or "(no claim kept)"
        lines.append(f"- `{lin}` {claim} — " + "; ".join(rounds))
    return "\n".join(lines)


def recurring(files: list[dict], min_rounds: int = 2) -> list[tuple[str, int]]:
    """(lineage, rounds) for each lineage open in *min_rounds* or more
    reviews, the most rounds first."""
    counts: dict[str, int] = {}
    for data in files:
        open_ = {lineage(f) for f in _findings(data) if _listed(f, data.get("mode"))}
        open_ |= {str(e["lineage"]) for e in _earlier(data) if e.get("status") != "resolved"}
        for lin in open_:
            counts[lin] = counts.get(lin, 0) + 1
    return sorted(((k, v) for k, v in counts.items() if v >= min_rounds), key=lambda kv: (-kv[1], kv[0]))
