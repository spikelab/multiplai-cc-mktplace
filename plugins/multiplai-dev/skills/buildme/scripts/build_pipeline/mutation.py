"""Python mutation testing over the lines a build added or modified.

No model calls. The TDD engine's MUTATION sub-step (`tdd_engine._run_mutation`)
calls `run_mutation` before and after the survivor round; this module only runs
cosmic-ray and turns its session database into a `MutationResult`.

cosmic-ray runs as a pinned `uvx` tool, never as a buildme dependency: buildme's
`uv.lock` is a runtime environment that ships to users, and a mutation tool has
no business in it. Two facts about running it that way, both checked on 8.7.0:

- `uvx` puts the tool's own `bin/` first on PATH, and cosmic-ray hands that
  environment to the test command. A `python -m pytest` test command would then
  start the tool's interpreter, which has neither pytest nor the project. So the
  test command is wrapped in `env PATH=<buildme's PATH>` — it runs with exactly
  the PATH buildme's own integration gate runs it with.
- `cr-filter-git` runs `git diff --relative -U0 <branch> .` from the working
  directory (cosmic_ray/tools/filters/git.py), so `branch` accepts any revision
  git does — the first block's baseline sha included — and cosmic-ray must run
  with the project directory as its cwd so the diff's paths match the
  `module-path` entries.

cosmic-ray rewrites source files in place while it runs. The caller guarantees
the tree is committed before the run; afterwards this module checks the tree is
clean and, when it is not, discards back to that commit and records that a
mutant was left on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import git_ops
from .gates import _repo_trusted
from .models import MutationResult, SurvivingMutant

log = logging.getLogger(__name__)

COSMIC_RAY_VERSION = "8.7.0"
COSMIC_RAY_SPEC = f"cosmic-ray=={COSMIC_RAY_VERSION}"
_UVX = ["uvx", "--from", COSMIC_RAY_SPEC]

# The first `uvx` call downloads cosmic-ray and its dependencies (minutes on a
# slow link); every later call is a cache hit. The setup commands get the
# generous ceiling, `exec` gets one derived from the mutant count.
_SETUP_TIMEOUT = 900
_EXEC_SLACK = 300

# Sampling marker written into the session for mutants the `max_mutants` cap
# dropped, so `cosmic-ray dump` tells them apart from git-filtered ones.
_SAMPLED_OUT = "Sampled out by buildme (mutation.max_mutants)"

# Runs inside cosmic-ray's own tool environment (`uvx --from ... python -I`),
# because the session database's schema is cosmic-ray's, not ours.
_SKIP_HELPER = """\
import json, sys
from cosmic_ray.work_db import WorkDB, use_db
from cosmic_ray.work_item import WorkResult, WorkerOutcome
session, ids_file, output = sys.argv[1], sys.argv[2], sys.argv[3]
ids = json.loads(open(ids_file).read())
with use_db(session, WorkDB.Mode.open) as db:
    db.set_multiple_results(ids, WorkResult(output=output, worker_outcome=WorkerOutcome.SKIPPED))
"""


def mutation_applies(config) -> tuple[bool, str]:
    """Whether the MUTATION step runs for this build, and why not when it doesn't.

    Python projects only (`stack == "pyproject"`, the value test-command
    discovery records for a `pyproject.toml`), and only with `mutation.enabled`.
    """
    if not config.mutation.enabled:
        return False, "mutation.enabled is false in specs/config.yaml"
    if config.stack != "pyproject":
        return False, (f"stack is {config.stack!r}, not 'pyproject' — "
                       "mutation testing covers Python projects only")
    if not config.test_command:
        return False, "no test command configured"
    return True, ""


def changed_source_files(project_dir: Path, baseline_sha: str) -> list[str] | None:
    """Non-test `.py` files changed since *baseline_sha*, project-relative.

    None when git could not be asked — distinct from `[]` ("asked, and no
    Python source changed"). Deleted files are dropped: there is nothing left
    to mutate.
    """
    from .tdd_engine import _TEST_FILE_RE, _TEST_PATH_RE

    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--relative", baseline_sha],
            cwd=str(project_dir), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Could not list files changed since %s: %s", baseline_sha[:8], e)
        return None
    if proc.returncode != 0:
        log.warning("Could not list files changed since %s: %s",
                    baseline_sha[:8], proc.stderr.strip())
        return None
    paths = []
    for name in (n.strip() for n in proc.stdout.splitlines()):
        if not name.endswith(".py"):
            continue
        if _TEST_FILE_RE.search(name) or _TEST_PATH_RE.search(name):
            continue
        if (project_dir / name).is_file():
            paths.append(name)
    return sorted(paths)


def parse_dump(text: str) -> MutationResult:
    """Turn `cosmic-ray dump` output into counts, survivors and a score.

    One JSON array per line: `[work_item, result]`, where `result` is null for a
    job that never ran. Git-filtered and sampled-out jobs carry
    `worker_outcome == "skipped"` and are not counted. A timed-out test run is
    `test_outcome == "killed"` with output `"timeout"` (cosmic_ray/testing.py);
    it counts as killed and is also reported in `timeouts`.
    """
    killed = survived = timeouts = errors = 0
    survivors: list[SurvivingMutant] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        item, result = json.loads(raw)
        if not result:
            continue
        worker = result.get("worker_outcome")
        if worker == "skipped":
            continue
        test = result.get("test_outcome")
        if worker != "normal" or test not in ("killed", "survived"):
            errors += 1
            continue
        if test == "killed":
            killed += 1
            if (result.get("output") or "").strip() == "timeout":
                timeouts += 1
            continue
        survived += 1
        for m in item.get("mutations") or []:
            survivors.append(SurvivingMutant(
                path=m.get("module_path", ""),
                line=int((m.get("start_pos") or [0])[0]),
                operator=m.get("operator_name", ""),
                mutant_id=item.get("job_id", ""),
                diff=result.get("diff") or "",
            ))
    judged = killed + survived
    return MutationResult(
        ok=True,
        killed=killed, survived=survived, timeouts=timeouts, errors=errors,
        score=(killed / judged) if judged else None,
        survivors=sorted(survivors, key=lambda s: (s.path, s.line, s.operator)),
    )


def pending_jobs(text: str) -> list[tuple[str, str]]:
    """`(job_id, stable key)` for every job in a dump that has no result yet.

    The key is built from what the mutation *is* (file, operator, occurrence,
    position), not from the job id, which cosmic-ray regenerates on every
    `init` — so the `max_mutants` sample picks the same mutants on a re-run.
    """
    jobs = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        item, result = json.loads(raw)
        if result:
            continue
        key = json.dumps([
            [m.get("module_path"), m.get("operator_name"), m.get("occurrence"),
             m.get("start_pos")]
            for m in item.get("mutations") or []
        ])
        jobs.append((item.get("job_id", ""), key))
    return jobs


def sample_out(jobs: list[tuple[str, str]], max_mutants: int) -> list[str]:
    """Job ids to drop so at most *max_mutants* run. Deterministic: ranks jobs by
    a hash of their stable key and keeps the lowest *max_mutants*."""
    if len(jobs) <= max_mutants:
        return []
    ranked = sorted(jobs, key=lambda j: hashlib.sha256(j[1].encode()).hexdigest())
    return [job_id for job_id, _ in ranked[max_mutants:]]


def cosmic_ray_config(paths: list[str], test_command: str, timeout: float,
                      baseline_sha: str) -> str:
    """The cosmic-ray TOML for one run. Every value is JSON-encoded, which is
    valid TOML for strings, numbers and arrays of strings."""
    wrapped = shlex.join(
        ["env", f"PATH={os.environ.get('PATH', '')}", *shlex.split(test_command)])
    return "\n".join([
        "[cosmic-ray]",
        f"module-path = {json.dumps(paths)}",
        f"timeout = {float(timeout)}",
        "excluded-modules = []",
        f"test-command = {json.dumps(wrapped)}",
        "",
        "[cosmic-ray.distributor]",
        'name = "local"',
        "",
        "[cosmic-ray.filters.git-filter]",
        f"branch = {json.dumps(baseline_sha)}",
        "",
    ])


class _ToolError(Exception):
    pass


def _run_tool(argv: list[str], cwd: Path, timeout: float) -> str:
    """Run one cosmic-ray command; return stdout or raise `_ToolError`.

    Own process group, so a timeout kills cosmic-ray's test subprocesses too
    and not just the `uvx` wrapper.
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except OSError as e:
        raise _ToolError(f"could not start {argv[len(_UVX)]}: {e}") from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise _ToolError(f"{' '.join(argv[len(_UVX):len(_UVX) + 2])} timed out after "
                         f"{int(timeout)}s")
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-1500:]
        raise _ToolError(f"{' '.join(argv[len(_UVX):len(_UVX) + 2])} exited "
                         f"{proc.returncode}: {tail}")
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.communicate(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def run_mutation(config, baseline_sha: str, paths: list[str]) -> MutationResult:
    """Mutate *paths* on the lines changed since *baseline_sha* and run the suite.

    The tree must be committed when this is called: the run's restore point is
    HEAD, and a dirty tree is refused rather than put at risk. The cosmic-ray
    config and session live in a temp dir outside the project, so nothing of
    the run can be committed.
    """
    started = time.monotonic()
    project_dir = Path(config.project_dir)
    base = MutationResult(ok=False, tool_version=COSMIC_RAY_VERSION, files=list(paths))

    if not _repo_trusted():
        base.error = ("repo not trusted — refusing to run its test command "
                      "(set --trust-repo or BUILDME_TRUST_REPO=1)")
        return base
    if not git_ops.tree_is_clean(project_dir):
        base.error = "tree has uncommitted changes — refusing to mutate it"
        return base
    restore_to = git_ops.rev_parse_head(project_dir)
    if not restore_to:
        base.error = "could not read HEAD to restore to"
        return base

    settings = config.mutation
    result = base
    with tempfile.TemporaryDirectory(prefix="buildme-mutation-") as tmp:
        tmp_dir = Path(tmp)
        cfg = tmp_dir / "cosmic-ray.toml"
        session = tmp_dir / "session.sqlite"
        cfg.write_text(cosmic_ray_config(paths, config.test_command,
                                         settings.timeout, baseline_sha))
        try:
            _run_tool([*_UVX, "cosmic-ray", "init", str(cfg), str(session)],
                      project_dir, _SETUP_TIMEOUT)
            _run_tool([*_UVX, "cr-filter-git", "--config", str(cfg), str(session)],
                      project_dir, _SETUP_TIMEOUT)
            pending = pending_jobs(_run_tool(
                [*_UVX, "cosmic-ray", "dump", str(session)], project_dir, _SETUP_TIMEOUT))
            dropped = sample_out(pending, settings.max_mutants)
            if dropped:
                ids_file = tmp_dir / "sampled-out.json"
                ids_file.write_text(json.dumps(dropped))
                helper = tmp_dir / "skip_jobs.py"
                helper.write_text(_SKIP_HELPER)
                _run_tool([*_UVX, "python", "-I", str(helper), str(session),
                           str(ids_file), _SAMPLED_OUT], project_dir, _SETUP_TIMEOUT)
            to_run = len(pending) - len(dropped)
            if to_run:
                # Baseline: the unmutated suite must pass under cosmic-ray's
                # own runner, or every mutant would read as "killed".
                _run_tool([*_UVX, "cosmic-ray", "baseline", str(cfg)],
                          project_dir, _SETUP_TIMEOUT + settings.timeout)
                _run_tool([*_UVX, "cosmic-ray", "exec", str(cfg), str(session)],
                          project_dir, settings.timeout * (to_run + 1) + _EXEC_SLACK)
            dump = _run_tool([*_UVX, "cosmic-ray", "dump", str(session)],
                             project_dir, _SETUP_TIMEOUT)
            result = parse_dump(dump)
            result.tool_version = COSMIC_RAY_VERSION
            result.files = list(paths)
            result.candidates = len(pending)
            result.sampled_out = len(dropped)
        except _ToolError as e:
            result = base
            result.error = str(e)
        except (ValueError, TypeError, KeyError, IndexError) as e:
            result = base
            result.error = f"could not parse cosmic-ray output: {e}"

    if not git_ops.tree_is_clean(project_dir):
        result.mutant_left_on_disk = True
        if git_ops.discard_to(project_dir, restore_to, "mutation run"):
            log.warning("Mutation run left a mutated file in the tree — "
                        "discarded back to %s", restore_to[:8])
        else:
            result.ok = False
            result.error = ((result.error + "; ") if result.error else "") + (
                "a mutated file was left in the tree and could NOT be discarded "
                f"back to {restore_to[:8]} — inspect the tree before shipping")
    result.elapsed_seconds = round(time.monotonic() - started, 1)
    return result


def score_text(result: MutationResult | None) -> str:
    """`0.83 (10 killed / 12)` or `n/a (no mutants)`."""
    if result is None:
        return "n/a (not run)"
    if result.score is None:
        return "n/a (no mutants ran)"
    return f"{result.score:.2f} ({result.killed} killed / {result.killed + result.survived})"


def render_report(
    *, first: MutationResult, final: MutationResult, round_note: str,
    min_score: float, passed: bool,
) -> str:
    """The `specs/changes/<name>/mutation.md` body."""
    lines = [
        "# Mutation testing",
        "",
        f"- Tool: cosmic-ray {final.tool_version or first.tool_version}, "
        "lines changed since the build's first block only",
        f"- Score before the survivor round: {score_text(first)}",
        f"- Score after the survivor round: {score_text(final)}",
        f"- Minimum (`mutation.min_score`): {min_score:.2f} — "
        f"{'PASSED' if passed else 'FAILED'}",
        f"- Timed-out test runs (counted as killed): {final.timeouts}",
        f"- Mutants that could not be judged (left out of the score): {final.errors}",
    ]
    if final.sampled_out:
        lines.append(
            f"- Sampled: {final.candidates - final.sampled_out} of {final.candidates} "
            "mutants ran (`mutation.max_mutants`); the sample is deterministic")
    if first.mutant_left_on_disk or final.mutant_left_on_disk:
        lines.append("- A run left the tree changed (a mutated file, or files the "
                     "test command wrote); buildme discarded back to the committed tree")
    lines += ["", "## Files mutated", ""]
    lines += [f"- `{p}`" for p in (final.files or first.files)] or ["- (none)"]
    lines += ["", "## Survivor round", "", round_note, "", "## Surviving mutants", ""]
    if final.survivors:
        lines += ["| File | Line | Operator |", "|---|---|---|"]
        lines += [f"| `{s.path}` | {s.line} | `{s.operator}` |" for s in final.survivors]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"
