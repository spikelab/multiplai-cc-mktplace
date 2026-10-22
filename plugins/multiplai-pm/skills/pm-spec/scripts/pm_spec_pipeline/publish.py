"""Publish a rewrite of several spec files as one change, through a journal.

A rewrite is computed in full first (every new text validated), then written
to `.pm-spec/publication.json` with each file's text before and after, then
applied file by file. The checkpoint that follows records the new files as the
run's own (`artifacts.record_current`) and sets the step's
`rewrite_published`; only then is the journal deleted. A run that finds a
journal finishes it first (`finish`), so a kill part way through the files
never leaves a mix of old and new: the same idea as buildme's
`.plan-publication.json`, restated. A file that is neither its "before" nor its
"after" text was edited by someone since, and the run stops rather than
overwrite it.

An "after" of None moves the file aside into `superseded-requirements/`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from .run import Run

log = logging.getLogger(__name__)

JOURNAL = "publication.json"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def supersede(change_dir: Path, rel: str) -> Path | None:
    """Move `<change_dir>/<rel>` to `superseded-requirements/<stem>-<sha256>.md`."""
    path = change_dir / rel
    if not path.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    dest = change_dir / "superseded-requirements" / f"{path.stem}-{digest}.md"
    dest.parent.mkdir(exist_ok=True)
    os.replace(path, dest)
    log.info("Moved superseded %s to %s", rel, dest)
    return dest


def _read(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


def publish(run: Run, step: str, files: dict[str, str | None]) -> None:
    """Write *files* (paths relative to the change dir) as one publication of *step*'s rewrite."""
    change_dir = run.config.change_dir
    entries = {
        rel: {"before": _read(change_dir / rel), "after": text}
        for rel, text in sorted(files.items())
    }
    journal = run.config.run_dir / JOURNAL
    atomic_write(journal, json.dumps({"step": step, "files": entries}, indent=1))
    finish(run)


def finish(run: Run) -> bool:
    """Finish a publication the journal records; False when there is none."""
    from .artifacts import record_current

    journal = run.config.run_dir / JOURNAL
    if not journal.is_file():
        return False
    data = json.loads(journal.read_text())
    change_dir = run.config.change_dir
    files = data.get("files") or {}
    for rel, item in files.items():
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError(f"Invalid publication journal entry: {rel}")
        actual = _read(change_dir / rel)
        if actual not in (item["before"], item["after"]):
            raise RuntimeError(
                f"A rewrite was interrupted while it was being written and {rel} changed "
                f"since; save your edit, then delete {journal} to go on"
            )
    for rel, item in files.items():
        if item["after"] is None:
            supersede(change_dir, rel)
        else:
            atomic_write(change_dir / rel, item["after"])
    record_current(run)
    step = data.get("step")
    if step:
        run.state.step(step).rewrite_published = True
    run.save()
    journal.unlink(missing_ok=True)
    log.info("Published the %s rewrite: %s", step, ", ".join(sorted(files)))
    return True
