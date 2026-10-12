"""`migrate --from <dir>`: copy reviews written before 0.33 into the data directory.

Each directory directly under the source is copied whole to
`<out>/legacy/<its name>/` (by default `<data_dir>/reviews/legacy/`), and one
`migrated` line per `findings.json` found in it, at any depth (top level,
`rounds/`, or an archive's own layout), goes to the review ledger with
`migrated_from` naming the directory it was read from. Files directly under
the source (rollups, `runs.jsonl`) are not reviews and are not copied.

The source is only read: nothing in it is moved, changed or deleted. A
directory already copied is skipped, so running it twice copies nothing the
second time. A copy is made under a hidden name and renamed when complete, so
a copy cut short is redone, never taken for a finished one.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger

log = logging.getLogger(__name__)

PARTIAL_PREFIX = ".partial-"


class MigrateError(Exception):
    """The source cannot be migrated. Exit code 2."""


@dataclass
class MigrateResult:
    dest: Path
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    lines: int = 0


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


def migrate(source: Path, out: Path, *, session_id: str = "") -> MigrateResult:
    """Copy every directory under *source* to `<out>/legacy/`; see the module docstring."""
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise MigrateError(f"{source} is not a directory")
    dest_root = out / ledger.LEGACY_DIR
    if dest_root == source or source in dest_root.parents or dest_root in source.parents:
        raise MigrateError(f"{source} and the copy's directory {dest_root} overlap; pass another --out")
    result = MigrateResult(dest=dest_root)
    for src in sorted(p for p in source.iterdir() if p.is_dir() and not p.is_symlink()):
        dest = dest_root / src.name
        if dest.exists():
            result.skipped.append(src.name)
            continue
        dest_root.mkdir(parents=True, exist_ok=True)
        partial = dest_root / f"{PARTIAL_PREFIX}{src.name}"
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
            if line is not None:
                ledger.append(line)
                result.lines += 1
    return result
