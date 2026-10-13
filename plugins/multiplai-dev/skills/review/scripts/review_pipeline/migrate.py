"""`migrate --from <dir>`: copy reviews written before multiplai-dev 0.46 into the data directory.

Each directory directly under the source is copied whole to
`<out>/legacy/<its name>/` (by default `<data_dir>/reviews/legacy/`), and one
`migrated` line per `findings.json` found in it, at any depth (top level,
`rounds/`, or an archive's own layout), goes to the review ledger with
`migrated_from` naming the directory it was read from. Files directly under
the source (rollups, `runs.jsonl`) are not reviews and are not copied, except
`review.yaml`, the run configuration, which is copied to `<out>/review.yaml`
when that does not exist yet (one already there is never replaced).

The source is only read: nothing in it is moved, changed or deleted. Each
source directory gets two lines in `<out>/legacy/sources.jsonl`: `copying`
(source path, copy's name) before anything is written, and `done` once the
copy and all its ledger lines are written. A source whose last line is
`done` is skipped, so running it twice copies nothing the second time. A
source left at `copying` by a run that stopped is finished under the name
that line gave it: copied if its copy is missing, and given only the ledger
lines it does not have yet (matched by `migrated_from` and `target_dir`).
Two sources can hold a directory of one name (`INBOX/reviews/x` and an
archive's `x`): the second is copied to `<name>~<6 hex of its source
path>/`, never taken for the first. A copy is made under a hidden name and
renamed when complete, so a copy cut short is redone, never taken for a
finished one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger
from .config import CONFIG_FILE

log = logging.getLogger(__name__)

PARTIAL_PREFIX = ".partial-"
SOURCES_FILE = "sources.jsonl"


class MigrateError(Exception):
    """The source cannot be migrated. Exit code 2."""


@dataclass
class MigrateResult:
    dest: Path
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)  # copied by a run that stopped before recording it
    lines: int = 0
    config: Path | None = None  # the review.yaml copied to <out>, if one was


def _ledger_line(findings: Path, source_dir: Path, copy_dir: Path, session_id: str) -> dict | None:
    try:
        data = json.loads(findings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("not migrating a ledger line for %s: %s", findings, e)
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("target") if isinstance(data.get("target"), dict) else {}
    in_rounds = findings.parent.parent.name == "rounds"
    return {
        "event": "migrated", "generated_at": data.get("generated_at"), "producer": data.get("producer"),
        "session_id": session_id, "slug": t.get("slug"), "target_dir": str(copy_dir),
        "round": findings.parent.name if in_rounds else None, "head_sha": t.get("head_sha"),
        "base_sha": t.get("base_sha"), "kind": t.get("kind"), "pr": t.get("pr"), "stopped": None,
        "run": data.get("run"), "migrated_from": str(source_dir),
    }


def _sources(dest_root: Path) -> dict[str, dict]:
    """{source path: {"dest": copy's name, "done": bool}} from `sources.jsonl`; a source's last line wins.

    A line with no `state` (none is written now) counts as done."""
    try:
        text = (dest_root / SOURCES_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    sources: dict[str, dict] = {}
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("source") and entry.get("dest"):
            sources[str(entry["source"])] = {"dest": str(entry["dest"]),
                                             "done": entry.get("state", "done") == "done"}
    return sources


def _record(dest_root: Path, src: Path, name: str, state: str) -> None:
    with (dest_root / SOURCES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"source": str(src), "dest": name, "state": state}) + "\n")


def _logged() -> set[tuple[str, str]]:
    """(migrated_from, target_dir) of every `migrated` line already in the ledger."""
    return {(str(e.get("migrated_from")), str(e.get("target_dir"))) for e in ledger.read()
            if e.get("event") == "migrated"}


def _copy_config(source: Path, out: Path) -> Path | None:
    """Copy `<source>/review.yaml` to `<out>/review.yaml` when *out* has none; return the copy, or None."""
    src, dest = source / CONFIG_FILE, out / CONFIG_FILE
    if not src.is_file() or src.is_symlink() or dest.exists():
        return None
    out.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        raise MigrateError(f"cannot copy {src} to {dest}: {e}") from e
    return dest


def migrate(source: Path, out: Path, *, session_id: str = "") -> MigrateResult:
    """Copy every directory under *source* to `<out>/legacy/`; see the module docstring."""
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise MigrateError(f"{source} is not a directory")
    dest_root = out / ledger.LEGACY_DIR
    if dest_root == source or source in dest_root.parents or dest_root in source.parents:
        raise MigrateError(f"{source} and the copy's directory {dest_root} overlap; pass another --out")
    result = MigrateResult(dest=dest_root)
    sources = _sources(dest_root)
    claimed = {e["dest"] for e in sources.values()}
    logged = _logged()
    result.config = _copy_config(source, out)
    for src in sorted(p for p in source.iterdir() if p.is_dir() and not p.is_symlink()):
        entry = sources.get(str(src))
        if entry and entry["done"]:
            result.skipped.append(src.name)
            continue
        if entry:  # a run stopped part way through this source: finish it under the same name
            name = entry["dest"]
        else:
            name = src.name
            if (dest_root / name).exists() or name in claimed:  # another source's directory of the same name
                name = f"{src.name}~{hashlib.sha256(str(src).encode()).hexdigest()[:6]}"
            dest_root.mkdir(parents=True, exist_ok=True)
            _record(dest_root, src, name, "copying")
            claimed.add(name)
        dest = dest_root / name
        if dest.exists():  # renamed into place by a run that stopped before its `done` line
            result.finished.append(src.name)
        else:
            partial = dest_root / f"{PARTIAL_PREFIX}{name}"
            shutil.rmtree(partial, ignore_errors=True)
            try:
                shutil.copytree(src, partial, symlinks=True)
            except OSError as e:  # shutil.Error is an OSError
                shutil.rmtree(partial, ignore_errors=True)
                raise MigrateError(f"cannot copy {src} to {dest}: {e}") from e
            partial.rename(dest)
            result.copied.append(src.name)
        for findings in sorted(src.rglob("findings.json")):
            rel = findings.parent.relative_to(src)
            line = _ledger_line(findings, findings.parent, dest / rel, session_id)
            if line is None or (line["migrated_from"], line["target_dir"]) in logged:
                continue
            ledger.append(line)
            logged.add((line["migrated_from"], line["target_dir"]))
            result.lines += 1
        _record(dest_root, src, name, "done")
    return result
