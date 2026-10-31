"""Incremental re-review: a re-pushed PR or branch reviews only the files whose patch changed.

`decide` runs once, in `orchestrator.prepare` right after `rounds.keep_round`, and its `Scope`
is saved in the state, which `resume` reuses. It compares each file's patch in the latest
finished kept round's `diff.patch` with its patch in this round's diff, ignoring `index` lines
and the line numbers of `@@` headers, so a rebase onto a moved base changes nothing. A file is
re-reviewed when its patch differs or it is new to the change; the finders always see a
re-reviewed file's whole patch, never part of it.

The findings that round left open (`rounds.open_findings`) on a file still in the change, none
of whose citations is in a re-reviewed file and whose cited files are the same at both heads, are copied into this round with their id, verdict,
severity and plain-language text, marked `carried_from`; they make no agent call. Decisions are
keyed by id, so they keep theirs. An open finding on a re-reviewed file is rechecked as before,
and one whose file left the change is `resolved` by the recheck stage with no call.

`incremental` needs all of: a PR or branch target, a kept round with a `findings.json`, its
`diff.patch` and `review-state.json`, the same mode, a different head (a rerun of the same
commit is a fresh review), no `--full`, and `review.yaml` `incremental` not false. Otherwise the scope is `full`, with the reason.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import rounds
from . import target as target_mod
from .chunks import diff_files
from .models import Finding, ReviewState, Scope, TargetInfo, Verdict, WriteAnswer
from .state import load_state

log = logging.getLogger(__name__)

_HUNK_HEAD = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
LEFT_THE_CHANGE = "file no longer in the change"


def normalised_patches(diff: str) -> dict[str, str]:
    """Each file's patch, by its path, without `index` lines and with `@@` headers' numbers removed.

    Inside a hunk every line starts with a space, `+`, `-` or `\\`, so a line starting with `index `
    or `@@` is always a header line.
    """
    out = {}
    for f in diff_files(diff):
        lines = []
        for line in f.text.split("\n"):
            if line.startswith("index "):
                continue
            lines.append(_HUNK_HEAD.sub("@@", line))
        out[f.path] = "\n".join(lines)
    return out


def changed_files(old_diff: str, new_diff: str) -> list[str]:
    """The paths of *new_diff* whose patch differs from *old_diff*'s or is not in it, in diff order."""
    old = normalised_patches(old_diff)
    return [p for p, text in normalised_patches(new_diff).items() if old.get(p) != text]


def limit_diff(diff: str, files: list[str]) -> str:
    """*diff* with only the sections of *files*."""
    keep = set(files)
    return "".join(f.text for f in diff_files(diff) if f.path in keep)


def latest_finished_round(target_dir: Path) -> Path | None:
    """The kept round with a `findings.json` whose `generated_at` is latest (the one
    `rounds.open_findings` reads)."""
    best: tuple[str, Path] | None = None
    for d in rounds._round_dirs(target_dir):
        try:
            data = json.loads((d / "findings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = str(data.get("generated_at") or "")
        if best is None or key >= best[0]:
            best = (key, d)
    return best[1] if best else None


def _unchanged(repo: str, old_head: str, new_head: str, paths: set[str]) -> bool:
    """True when every one of *paths* is the same at both heads; False when git cannot tell.

    A cited file can change outside the new diff: it left the change, or a moved base rewrote it.
    """
    proc = target_mod.git(
        repo,
        "diff",
        "--name-only",
        old_head,
        new_head,
        "--",
        *sorted(paths),
        check=False,
    )
    return proc.returncode == 0 and not proc.stdout.strip()


@dataclass
class Carried:
    findings: list[Finding] = field(default_factory=list)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    write_answers: dict[str, WriteAnswer] = field(default_factory=dict)
    original_severity: dict[str, str] = field(default_factory=dict)
    lineage: dict[str, str] = field(default_factory=dict)


def _full(reason: str, total: int) -> tuple[Scope, Carried]:
    return Scope(kind="full", reason=reason, total_files=total), Carried()


def decide(
    target_dir: Path,
    info: TargetInfo,
    diff: str,
    *,
    mode: str,
    full: bool,
    enabled: bool,
) -> tuple[Scope, Carried]:
    """This round's scope and the findings it carries, from the kept rounds in *target_dir*."""
    total = len(diff_files(diff))
    if info.kind not in ("pr", "branch"):
        return _full(f"a {info.kind} review always reviews every file", total)
    if full:
        return _full("--full was given", total)
    if not enabled:
        return _full("review.yaml sets incremental: false", total)
    since = latest_finished_round(target_dir)
    if since is None:
        return _full("no earlier round finished", total)
    try:
        old_diff = (since / "diff.patch").read_text(encoding="utf-8")
    except OSError:
        return _full(f"round {since.name} has no diff.patch", total)
    earlier = load_state(since / "review-state.json")
    if earlier is None:
        return _full(f"round {since.name} has no readable review-state.json", total)
    if earlier.mode != mode:
        return _full(f"round {since.name} ran in {earlier.mode} mode", total)
    if earlier.target.head_sha == info.head_sha:
        # A rerun of the same commit asks for a fresh review: nothing would be re-reviewed otherwise.
        return _full(f"round {since.name} reviewed this same head (a rerun)", total)

    reviewed = changed_files(old_diff, diff)
    in_change = {f.path for f in diff_files(diff)}
    by_id = {f.id: f for f in earlier.findings}
    carried = Carried()
    for o in rounds.open_findings(target_dir, info.head_sha):
        f = by_id.get(o.id)
        if f is None or f.file not in in_change:
            continue  # the recheck stage gives it a status
        cited = {f.file} | {
            c.path for c in f.citations if not c.is_web and not c.is_dependency
        }
        if cited & set(reviewed) or not _unchanged(
            info.repo_path, earlier.target.head_sha, info.head_sha, cited
        ):
            continue
        carried.findings.append(f.model_copy(update={"carried_from": since.name}))
        if o.id in earlier.verdicts:
            carried.verdicts[o.id] = earlier.verdicts[o.id]
        if o.id in earlier.write_answers:
            carried.write_answers[o.id] = earlier.write_answers[o.id]
        if o.id in earlier.original_severity:
            carried.original_severity[o.id] = earlier.original_severity[o.id]
        carried.lineage[o.id] = o.lineage
    numbers = [d.name for d in rounds._round_dirs(target_dir)]
    scope = Scope(
        kind="incremental",
        since_round=since.name,
        since_number=numbers.index(since.name) + 1,
        since_head=earlier.target.head_sha,
        since_base=earlier.target.base_sha,
        reviewed_files=reviewed,
        total_files=total,
        carried_ids=[f.id for f in carried.findings],
    )
    log.info(
        "incremental scope for %s: %d of %d files, %d findings carried from %s",
        info.slug,
        len(reviewed),
        total,
        len(carried.findings),
        since.name,
    )
    return scope, carried


def apply(state: ReviewState, scope: Scope, carried: Carried) -> None:
    """Put the scope and the carried findings into a fresh state."""
    state.scope = scope
    state.findings = list(carried.findings)
    state.verdicts.update(carried.verdicts)
    state.write_answers.update(carried.write_answers)
    state.original_severity.update(carried.original_severity)
    state.lineage.update(carried.lineage)


def is_incremental(state: ReviewState) -> bool:
    return state.scope is not None and state.scope.kind == "incremental"


def nothing_changed(state: ReviewState) -> bool:
    return is_incremental(state) and not state.scope.reviewed_files
