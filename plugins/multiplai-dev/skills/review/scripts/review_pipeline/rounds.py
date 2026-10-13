"""Earlier rounds of a review: kept under `<out>/<slug>/rounds/`, read by repeats and assess.

A PR is reviewed into the same `<out>/<slug>/` each time, so a new run would
write over the last round's files. Before it does, `keep_round` copies every
file and directory of that round (`findings.json`, the review, `checks.json`,
`progress.log`, `review-state.json`, `diff.patch`, `commits.txt`, ... but not
`rounds/`, `viewer/` or the snapshots git can rebuild: `tree/`, `base-tree/`,
`assess-tree/`) to `rounds/<NN>-<old head_sha[:12]>/`, also when the new run is
on the same head (a rerun), so the repeat and assess stages have that run to
compare with. `<NN>` is the two-digit round number, one more than the
directories already in `rounds/`, so a second run on one head gets the next
number. A run that stopped before export is kept the same way, without the
earlier round's exported files. The copy is made under a hidden name and renamed when complete.
Nothing there is ever deleted or overwritten. Rounds kept before multiplai-dev 0.46 are
named `<sha12>/` and `<sha12>-2/`; they are still read, ordered by
`generated_at`.

A finding the repeats stage matched to a rejected one gets no entry of its
own in `decisions.json`; its `repeat` assessment records the earlier
decision. `load_rounds` reads that as the finding's own decision, so a later
round's copy of the reworded finding is matched by id as well.

The person's decisions live in `viewer/decisions.json` (written by
review-viewer), one entry per finding id, and persist across rounds. Because
they are keyed by id alone, a decision is applied to an earlier round's copy
of a finding only when it was made before a later round replaced that one
(its `ts` is not after the next round's `generated_at`). A decision made while
the current round was shown is never read back as an earlier one. A decision
or round with no readable time is applied, as before 0.27.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ROUNDS_DIR = "rounds"
SHOWN_STATUSES = ("confirmed", "unverifiable")


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if path.exists():
            log.warning("Unreadable %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


# Not copied into a kept round: the rounds themselves, the viewer's state (it
# persists across rounds where it is), head/base snapshots git rebuilds, and a
# `--dir` review's imported checkout `source/` (its git keeps every commit an
# earlier round names, and by the time a round is kept it holds the next import).
# These are every directory the pipeline writes at the top of a target folder.
# `keep_round` skips every other top-level directory too (a review an older
# version nested inside this folder, with its own rounds), and the rollups such a
# version wrote beside it (`ROLLUP_FILES`): a kept round holds this run's files only.
NOT_KEPT = frozenset({ROUNDS_DIR, "viewer", "tree", "base-tree", "assess-tree", "source"})
ROLLUP_FILES = frozenset({"HIGH-only.md", "MEDIUM-only.md", "LOW-only.md", "runs.jsonl"})
PARTIAL_PREFIX = ".partial-"


def folder_target(folder: Path) -> dict:
    """The `target` object of *folder*'s `review-state.json`, else of its `findings.json`, else {}:
    whose review the folder holds (`remote_url`, `ref`, `slug`)."""
    for name in (STATE_FILE, "findings.json"):
        t = (_read_json(folder / name) or {}).get("target")
        if isinstance(t, dict):
            return t
    return {}


def out_root(target_dir: Path, slug: str | None = None) -> Path:
    """The `<out>` a review folder was written under.

    A folder named by its own slug was written by multiplai-dev 0.46 or
    earlier (`<out>/<slug>/`), and so is one whose slug cannot be read: its
    parent. Any other folder is `<out>/<repo>/<object>/`: its grandparent. An
    object name starts with its kind (`pr-`, `branch-`, ...) and a slug with
    `<owner>-<repo>--`, so the two never meet. With *slug* None it is read
    with `folder_target`.
    """
    if slug is None:
        slug = str(folder_target(target_dir).get("slug") or "")
    if not slug or slug == target_dir.name:
        return target_dir.parent
    return target_dir.parent.parent


def _round_dirs(target_dir: Path) -> list[Path]:
    """Every kept round's directory, partial copies left out."""
    root = target_dir / ROUNDS_DIR
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))


def _round_key(data: dict) -> tuple[str, str]:
    """What tells two rounds apart: `generated_at` (whole seconds) and the run's start (milliseconds)."""
    return str(data.get("generated_at") or ""), str((data.get("run") or {}).get("started_at") or "")


def round_number(target_dir: Path) -> int:
    """The number the next kept round gets, which is also the current run's: one more than the rounds kept."""
    dirs = _round_dirs(target_dir)
    n = len(dirs) + 1
    while any(d.name.startswith(f"{n:02d}-") for d in dirs):
        n += 1
    return n


def _kept_already(target_dir: Path, data: dict) -> Path | None:
    """The round directory that already holds the round in *data*, if one does."""
    key = _round_key(data)
    for d in _round_dirs(target_dir):
        kept = _read_json(d / "findings.json")
        if kept is not None and _round_key(kept) == key:
            return d
    return None


def _name_head(name: str) -> str:
    """The 12-character head a round directory's name gives, or ""."""
    m = re.fullmatch(r"(?:\d{2,}-)?([0-9a-f]{12})(?:-\d+)?", name)
    return m.group(1) if m else ""


def round_name(target_dir: Path, findings_file: dict) -> str:
    """The round id `post` puts in its PR markers: the round's head sha cut to 12, `-k` for its k-th round.

    Read-only. The id does not change when a later run keeps the round under
    `rounds/<NN>-<sha12>/`: the first round of a head is `<sha12>`, a later
    one on the same head `<sha12>-2`, `-3`, ... (the v1 marker format), counted
    in `generated_at` order over the kept rounds of that head.
    """
    head = str((findings_file.get("target") or {}).get("head_sha") or "")
    key = _round_key(findings_file)
    keys = []
    for d in _round_dirs(target_dir):
        kept = _read_json(d / "findings.json")
        if kept is None:
            continue
        kept_head = str((kept.get("target") or {}).get("head_sha") or "")
        # A round whose findings.json names no head: its directory name does (`<sha12>`, `<sha12>-2`, `<NN>-<sha12>`).
        same = kept_head == head if kept_head else _name_head(d.name) == head[:12]
        if same:
            keys.append(_round_key(kept))
    keys.sort()
    k = keys.index(key) + 1 if key in keys else len(keys) + 1
    return head[:12] if k == 1 else f"{head[:12]}-{k}"


STATE_FILE = "review-state.json"
# What a run writes only when it finishes (export and render). In the directory
# of a run that stopped before export they are the earlier run's, kept already.
EXPORTED = ("findings.json", "checks.json", "review-*.md", "summary-*.md", "checks-*.md")


def _run_start(state: dict | None) -> str:
    """When the run a saved review state belongs to started (its first `run` interval), or ""."""
    runs = ((state or {}).get("timings") or {}).get("run") or []
    first = runs[0] if runs and isinstance(runs[0], dict) else {}
    return str(first.get("started_at") or "")


def _state_kept_already(target_dir: Path) -> Path | None:
    """The round directory whose `review-state.json` is the top-level one, byte for byte, if one is."""
    try:
        current = (target_dir / STATE_FILE).read_bytes()
    except OSError:
        return None
    for d in _round_dirs(target_dir):
        try:
            if (d / STATE_FILE).read_bytes() == current:
                return d
        except OSError:
            continue
    return None


def keep_round(target_dir: Path, slug: str, new_head_sha: str) -> Path | None:
    """Copy the last run in full to `rounds/<NN>-<sha12>/` before a new run writes over it.

    The new run may be on the same head (a rerun): that round is kept too.
    The last run is the one `review-state.json` belongs to. When it finished,
    `findings.json` is its own (the same run start) and the round is copied
    whole; a round already kept (same `generated_at` and run start) is not
    copied again, so a run that started, stopped and was started again keeps
    one copy. When it stopped before export (a budget stop, a verify stop, a
    failure, or a run killed and then restarted), its state, progress and diff
    are kept without the earlier run's exported files (`EXPORTED`), named by
    the state's head; a state already kept byte for byte is not copied again.
    Returns the round directory, or None when there was nothing to keep.
    `resume` does not call this.
    """
    data = _read_json(target_dir / "findings.json")
    state = _read_json(target_dir / STATE_FILE)
    skip_files: tuple[str, ...] = ()
    if data is not None and (state is None or _run_start(state) == _round_key(data)[1]):
        old_head = str((data.get("target") or {}).get("head_sha") or "")
        if not old_head:
            return None
        kept = _kept_already(target_dir, data)
    elif state is not None:
        old_head = str((state.get("target") or {}).get("head_sha") or "")
        if not old_head:
            return None
        kept = _state_kept_already(target_dir)
        skip_files = EXPORTED
    else:
        return None
    if kept is not None:
        return kept
    name = f"{round_number(target_dir):02d}-{old_head[:12]}"
    root = target_dir / ROUNDS_DIR
    root.mkdir(parents=True, exist_ok=True)
    partial = root / f"{PARTIAL_PREFIX}{name}"
    shutil.rmtree(partial, ignore_errors=True)  # a copy an earlier run did not finish

    def skip(directory: str, names: list[str]) -> set[str]:
        if Path(directory) != target_dir:
            return set()
        return {n for n in names if n in NOT_KEPT or n in ROLLUP_FILES or (target_dir / n).is_dir()
                or any(fnmatch.fnmatchcase(n, p) for p in skip_files)}

    shutil.copytree(target_dir, partial, symlinks=True, ignore=skip)
    round_dir = root / name
    partial.rename(round_dir)
    return round_dir


def load_decisions(target_dir: Path) -> dict[str, dict]:
    """`viewer/decisions.json`: {finding id: {decision, note, ts}}, or {}."""
    data = _read_json(target_dir / "viewer" / "decisions.json")
    return {k: v for k, v in (data or {}).items() if isinstance(v, dict)}


@dataclass
class EarlierFinding:
    """A finding from an earlier round, with the person's decision on it."""
    finding: dict  # the findings.json v1 row
    head_sha: str  # the round it came from
    decision: str  # "accept", "reject", "defer", or "" when none was recorded
    note: str

    @property
    def id(self) -> str:
        return self.finding["id"]


def _when(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _made_by(decision: dict, until: datetime | None) -> dict:
    """*decision* when it was made no later than *until*, else {} (made in a later round)."""
    ts = _when(decision.get("ts") or "")
    if until is None or ts is None or ts <= until:
        return decision
    return {}


def _carried(f: dict) -> dict:
    """The rejection a `repeat` assessment records, as a decision: {} for any other finding."""
    a = f.get("assessment") or {}
    if a.get("label") == "repeat" and a.get("earlier_decision") == "reject":
        return {"decision": "reject", "note": a.get("earlier_note") or ""}
    return {}


# Not walked when looking for a target's history under other names: snapshots,
# imported checkouts and git internals hold no findings.json of a round.
_NOT_WALKED = frozenset({"viewer", "tree", "base-tree", "assess-tree", "source", ".git", ".locks"})


def _walked(here: Path, name: str, depth: int | None) -> bool:
    """Whether the history walk enters *here*/*name*.

    *depth* is *here*'s depth under `<out>`, or None under the other roots,
    where every depth skips `_NOT_WALKED` and partial copies, as before 0.48.
    Under `<out>`: `<out>` skips locks, git and partial copies (and
    `_NOT_WALKED` when it is itself a review folder); a review folder one level
    down skips `_NOT_WALKED`; a repo folder skips the names no object can have
    (`tree` stays: it is the whole-tree object).
    """
    from .target import NOT_OBJECT_NAMES, is_review_folder

    if name.startswith(PARTIAL_PREFIX):
        return False
    if depth == 0:
        return name not in {".locks", ".git"} and not (name in _NOT_WALKED and is_review_folder(here))
    if depth == 1 and not is_review_folder(here):
        return name not in NOT_OBJECT_NAMES
    return name not in _NOT_WALKED


def _history_roots(target_dir: Path, slug: str) -> list[Path]:
    """Where a target's rounds written under another directory name can be: the `<out>`
    *target_dir* is in (`out_root`), migrate's copies (`<data_dir>/reviews/legacy/`) and
    the default output directory before multiplai-dev 0.46 (`INBOX/reviews`)."""
    from . import ledger

    roots = [out_root(target_dir, slug)]
    for root in (lambda: ledger.reviews_root() / ledger.LEGACY_DIR, ledger.legacy_reviews_root):
        try:
            roots.append(root())
        except Exception:  # noqa: BLE001 — no data directory or workspace: nothing there to read
            log.debug("cannot resolve a review history root", exc_info=True)
    seen, out = set(), []
    for r in roots:
        key = r.resolve()
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def history_elsewhere(target_dir: Path, slug: str, remote_url: str | None) -> list[Path]:
    """The `findings.json` of every round of this target written under another directory name.

    Before multiplai-dev 0.46 a slug started with the local checkout's
    directory name (`<dirname>--pr-<n>`); now it starts with `<owner>-<repo>`
    from `origin`. A PR's earlier rounds, and the person's decisions on them,
    are still in those old directories (or migrate's copies of them). A file
    counts when its path holds this slug's part after the repository
    (`--pr-283`, also inside `x--pr-283__rounds__<sha>`, never `--pr-2830`)
    and the file itself names the same `origin` and a slug ending in that part.
    Nothing is found that way for a slug that does not start with its
    origin's name (no `origin`: the slug is the directory name, as before).

    Under `<out>`, what a folder is depends on where it is
    (`_walked_children`), so a repo called `tree` and a whole-tree review
    `<repo>/tree/` are read. The target's own folder is walked too, without
    its own `findings.json` and `rounds/`, for a review an older version
    nested in it. This target's 0.46 folder `<out>/<slug>/` is always read,
    whatever its origin.
    """
    from .target import origin_name

    first = out_root(target_dir, slug)
    own = target_dir.resolve()
    found: dict[Path, Path] = {}
    flat = first / slug
    if flat.is_dir() and flat.resolve() != own:
        for path in [flat / "findings.json", *(d / "findings.json" for d in _round_dirs(flat))]:
            if path.is_file():
                found.setdefault(path.resolve(), path)
    origin = origin_name(remote_url)
    matching = bool(origin) and slug.startswith(f"{origin}--")
    suffix = slug[len(origin) + 2:] if matching else ""
    in_path = re.compile(rf"--{re.escape(suffix)}(?![0-9A-Za-z])")
    for root in _history_roots(target_dir, slug) if matching else []:
        positional = root.resolve() == first.resolve()
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            if here.resolve() == own:
                dirnames[:] = sorted(d for d in dirnames if d not in _NOT_WALKED and d != ROUNDS_DIR)
                continue
            depth = len(here.relative_to(root).parts)
            dirnames[:] = sorted(d for d in dirnames if _walked(here, d, depth if positional else None))
            if "findings.json" not in filenames or not in_path.search(str(here.relative_to(root))):
                continue
            path = here / "findings.json"
            data = _read_json(path) or {}
            t = data.get("target") or {}
            if (origin_name(t.get("remote_url")) == origin and str(t.get("slug") or "").endswith(f"--{suffix}")
                    and path.resolve() not in found):
                found[path.resolve()] = path
    if found:
        log.info("%s: reading %d earlier round(s) kept under other directory names", slug, len(found))
    return list(found.values())


def _decisions_beside(findings: Path) -> dict[str, dict]:
    """The decisions of the target directory a `findings.json` belongs to (its own, or a kept round's)."""
    target_dir = findings.parent.parent.parent if findings.parent.parent.name == ROUNDS_DIR else findings.parent
    return load_decisions(target_dir)


def load_rounds(target_dir: Path, current_head: str = "", elsewhere: list[Path] | tuple = ()) -> list[EarlierFinding]:
    """Every finding in `rounds/*/findings.json`, the latest round's copy of each id.

    Rounds are ordered by `generated_at`; a round on *current_head* counts
    (a rerun keeps one). Each copy gets the person's decision only when it was
    made before the next round was generated; for the last earlier round, the
    next round is the top-level `findings.json` when it is *current_head*'s
    and not itself a kept round (a finished review, as `assess-only` reads
    it), else no limit (a run that has not exported). A copy with no decision
    of its own takes the one its `repeat` assessment carried over.

    *elsewhere* adds rounds of this target kept under other directory names
    (`history_elsewhere`), with the decisions recorded beside them; a round
    found twice (a migrated copy) counts once, and a decision recorded in
    *target_dir* wins over one recorded there.
    """
    rounds = []
    seen = set()
    own = [d / "findings.json" for d in _round_dirs(target_dir)]
    decisions: dict[str, dict] = {}
    for path in [*own, *elsewhere]:
        data = _read_json(path)
        if data is None:
            continue
        head = str((data.get("target") or {}).get("head_sha") or "")
        key = (*_round_key(data), head)
        if key in seen and path not in own:  # a copy of a round already read
            continue
        seen.add(key)
        rounds.append((str(data.get("generated_at") or ""), head, data.get("findings") or []))
    for path in elsewhere:
        for fid, d in _decisions_beside(path).items():
            if str(d.get("ts") or "") >= str((decisions.get(fid) or {}).get("ts") or ""):
                decisions[fid] = d
    rounds.sort(key=lambda r: r[0])
    current = _read_json(target_dir / "findings.json") or {}
    current_at = None
    if (current_head and str((current.get("target") or {}).get("head_sha") or "") == current_head
            and str(current.get("generated_at") or "") not in {r[0] for r in rounds}):
        current_at = _when(current.get("generated_at") or "")
    ends = [_when(r[0]) for r in rounds[1:]] + [current_at]
    decisions.update(load_decisions(target_dir))
    by_id: dict[str, EarlierFinding] = {}
    for (_, head, findings), until in zip(rounds, ends):
        for f in findings:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            d = _made_by(decisions.get(f["id"]) or {}, until) or _carried(f)
            by_id[f["id"]] = EarlierFinding(finding=f, head_sha=head, decision=str(d.get("decision") or ""),
                                            note=str(d.get("note") or ""))
    return list(by_id.values())


def rejected(earlier: list[EarlierFinding]) -> list[EarlierFinding]:
    """Earlier findings the person rejected."""
    return [e for e in earlier if e.decision == "reject"]


def accepted_or_open(earlier: list[EarlierFinding]) -> list[EarlierFinding]:
    """Earlier shown findings the person accepted or has not decided (deferred counts as not decided)."""
    return [e for e in earlier
            if e.decision != "reject" and e.finding.get("status") in SHOWN_STATUSES]
