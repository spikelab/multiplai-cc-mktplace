"""The apply step: `run … --apply [--specs-dir <dir>]`, after render (plan, item 21).

Copies each `<run>/registry/<capability>.md` into `<registry>` =
`<specs-dir>/registry/` when that file is absent there, and commits nothing.
A file already there with the same bytes counts as applied; one with other
bytes is left untouched (`SPEC_EXTRACT:SKIPPED_EXISTING`, Decided 4).

Before any copy, buildme's uncommitted-work check runs on `<registry>` as a
subprocess (skills do not import each other in an installed plugin): with no
stop record on the first apply, so any uncommitted change there is refused
(row R17); with `state.apply_stop` on a resume, so only what an earlier apply
wrote passes (row R11). Each copy saves the fingerprint of the bytes it is
about to write into `apply_stop` first, then writes
`<registry>/.spec-extract-tmp/<file>` and `os.replace`s it into place. Under
`<run>` this step writes only `apply_stop` and `steps_done`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from . import state as state_mod
from .render import REGISTRY_DIR
from .state import ApplyStop, State
from .target import Target, git

APPLY_TMP = ".spec-extract-tmp"
CHECK_TIMEOUT = 300  # seconds; the first run may build buildme's environment (judgment)
NEEDS_SPECS_DIR = "SPEC_EXTRACT:APPLY_NEEDS_SPECS_DIR"


class ApplyRefused(Exception):
    """`--apply` will not write (exit 2). The message says why."""

    def __init__(self, message: str, line: str = ""):
        super().__init__(message)
        self.line = line  # a `SPEC_EXTRACT:` line for stdout, or ""


def check_argv() -> list[str]:
    """buildme's `stop_record_cli`, run from buildme's own scripts directory.
    `<skills>` is `parents[3]` of this file (spec_extract_pipeline → scripts →
    spec-extract → skills), in the repo and in an installed copy of the plugin."""
    buildme = Path(__file__).resolve().parents[3] / "buildme" / "scripts"
    return ["uv", "run", "--directory", str(buildme), "python", "-m", "build_pipeline.stop_record_cli"]


def refuse_early(apply: bool, dir: str | None, specs_dir: str | None) -> None:
    """The refusals that need no target, checked before anything is written."""
    if specs_dir is not None and not apply:
        raise ApplyRefused("--specs-dir is used only with --apply")
    if apply and dir is not None and specs_dir is None:
        raise ApplyRefused("--apply with --dir needs --specs-dir: a directory has no "
                           "repository to default to; nothing was written", line=NEEDS_SPECS_DIR)


def registry_dir(target: Target, specs_dir: str | None, run_dir: Path) -> Path:
    """`<specs-dir>/registry/`, refused unless the nearest existing directory at or above
    `<specs-dir>` is in a git work tree and not under `<run>`. Writes nothing.
    `<specs-dir>` defaults to `<Target.repo>/specs` (not for `--dir`: `refuse_early`)."""
    if specs_dir is None:
        assert target.kind != "dir"
        specs = Path(target.repo) / "specs"
    else:
        specs = Path(specs_dir).expanduser()
    specs = specs.resolve()
    if specs == run_dir or run_dir in specs.parents:
        raise ApplyRefused(f"--specs-dir {specs} is inside the run folder {run_dir}")
    nearest = next(p for p in (specs, *specs.parents) if p.exists())
    inside = nearest.is_dir() and git(nearest, "rev-parse", "--is-inside-work-tree",
                                      check=False).stdout.strip() == "true"
    if not inside:
        raise ApplyRefused(f"--specs-dir {specs} is not in a git work tree ({nearest} is not); "
                           "--apply copies only into a repository, where git shows what it added")
    return specs / "registry"


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def uncommitted_work(registry: Path, stop: ApplyStop | None) -> str | None:
    """buildme's `stop_record.others_work(<registry>, stop, ignore=.spec-extract-tmp)`:
    None when apply may go on, else why not. ApplyRefused when the check cannot run."""
    payload = json.dumps({"project_dir": str(registry), "ignore": APPLY_TMP, "head": None,
                          "stop": stop.model_dump() if stop is not None else None})

    def cannot(why: str, err: str = "") -> ApplyRefused:
        tail = "\n".join(err.strip().splitlines()[-5:])
        return ApplyRefused("the uncommitted-work check could not run: " + why + (f"\n{tail}" if tail else ""))

    try:
        # A session of its own, so a timeout kills `uv` and the check it started.
        proc = subprocess.Popen(check_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        raise cannot(str(e)) from None
    try:
        out, err = proc.communicate(payload, timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise cannot(f"no answer within {CHECK_TIMEOUT} seconds") from None
    except BaseException:
        _kill(proc)
        raise
    if proc.returncode != 0:
        raise cannot(f"exit {proc.returncode}", err)
    lines = out.splitlines()
    try:
        answer = json.loads(lines[0]) if len(lines) == 1 else None
    except ValueError:
        answer = None
    if not (isinstance(answer, dict) and set(answer) == {"reason"}
            and (answer["reason"] is None or isinstance(answer["reason"], str))):
        raise cannot(f"its output is not one JSON line: {out[:200]!r}", err)
    return answer["reason"]


def _put(tmp: Path, dest: Path, data: bytes) -> None:
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def run(state: State, run_dir: Path, registry: Path) -> tuple[int, list[str]]:
    """Copy the run's registry files into *registry*; the files that count as applied
    and the `SKIPPED_EXISTING` lines. ApplyRefused when the check refuses or cannot run."""
    registry.mkdir(parents=True, exist_ok=True)
    resumed = state.apply_stop is not None
    reason = uncommitted_work(registry, state.apply_stop)
    if reason is not None:
        raise ApplyRefused(f"--apply refused, nothing was written: {reason}")
    tmp_dir = registry / APPLY_TMP
    if resumed:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    applied, skipped = 0, []
    for src in sorted((run_dir / REGISTRY_DIR).glob("*.md")):
        data, dest = src.read_bytes(), registry / src.name
        if dest.exists() or dest.is_symlink():
            if dest.is_file() and dest.read_bytes() == data:
                applied += 1
            else:
                skipped.append(f"SPEC_EXTRACT:SKIPPED_EXISTING {src.stem} {src}")
            continue
        stop = state.apply_stop = state.apply_stop or ApplyStop()
        stop.edits[src.name] = "sha256:" + hashlib.sha256(data).hexdigest()
        state_mod.save(state, run_dir)
        tmp_dir.mkdir(exist_ok=True)
        _put(tmp_dir / src.name, dest, data)
        applied += 1
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if "apply" not in state.steps_done:
        state.steps_done.append("apply")
        state_mod.save(state, run_dir)
    return applied, skipped
