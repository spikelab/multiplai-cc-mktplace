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

import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import git_ops
from .config import discover_test_command
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

# cosmic-ray's `timeout` applies to `baseline` too, and a baseline that times
# out reads as a red suite. So the baseline runs under the setup ceiling, and
# each mutant then gets `mutation.timeout` or this many times the measured
# baseline run, whichever is longer: a green suite slower than the configured
# timeout would otherwise time out under every mutant and score as "killed".
_BASELINE_FACTOR = 3.0

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


# What makes a project "Python" for this step: a Python manifest or test
# config at the project root. Not `config.stack`, which cannot answer the
# question — test-command discovery records the FIRST marker it finds, so a
# project with both `pytest.ini` and `pyproject.toml` is stack "pytest" and a
# `setup.py` project is "setup"; a `tdd.test_command` in specs/config.yaml
# skips discovery and leaves stack ""; and `memory_files.stacks` names it
# freely ("python", "django", ...).
PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini",
                  "tox.ini", "requirements.txt")

# The discovery markers (`BuildConfig._discover_test_command`) whose test
# command is pytest. A command discovered from any other marker (`npm test`
# from package.json, `cargo test`, ...) never imports the mutated Python, so
# every mutant would survive and fail the build.
_PYTHON_DISCOVERY_MARKERS = ("pytest.ini", "pyproject.toml", "setup.py")


def mutation_applies(config) -> tuple[bool, str]:
    """Whether the MUTATION step runs for this build, and why not when it doesn't.

    Python projects only — one of `PYTHON_MARKERS` at the project root — and
    only with `mutation.enabled`. A Python project whose manifest lives below
    the root (a monorepo subdirectory) is not detected and skips with a SKIP
    line naming the markers.

    The test command must also be one that runs the Python: a configured
    `tdd.test_command` is taken as the project's stated choice, but a
    discovered one must come from a Python discovery marker. A Node project
    with a helper `requirements.txt` discovers `npm test` and skips. Bootstrap
    writes the discovered command into specs/config.yaml, so a resume or a
    later build loads it as configured; a configured command identical to the
    one discovery picks for this project counts as discovered.
    """
    if not config.mutation.enabled:
        return False, "mutation.enabled is false in specs/config.yaml"
    project_dir = Path(config.project_dir)
    if not any((project_dir / m).is_file() for m in PYTHON_MARKERS):
        return False, ("no Python project file at the project root ("
                       + ", ".join(PYTHON_MARKERS)
                       + ") — mutation testing covers Python projects only")
    if not config.test_command:
        return False, "no test command configured"
    discovered_from = config.test_command_discovered_from
    if not discovered_from:
        found = discover_test_command(project_dir)
        if found and found[0] == config.test_command:
            discovered_from = found[1]
    if discovered_from and discovered_from not in _PYTHON_DISCOVERY_MARKERS:
        return False, (f"the test command `{config.test_command}` was discovered "
                       f"from {discovered_from} and does not run Python tests — "
                       "set tdd.test_command in specs/config.yaml to a command "
                       "that does, or set mutation.enabled: false")
    return True, ""


# Python files a test suite runs but does not import: packaging and task-runner
# scripts (any depth), the Sphinx tree at the project root, and database
# migrations. Every mutant on them survives, and the survivor round may only
# edit tests, so mutating them would only pull the score under `min_score`.
_NOT_IMPORTED_RE = re.compile(
    r"(^|/)(setup|noxfile|fabfile|manage)\.py$"
    r"|^docs?/"
    r"|(^|/)(migrations|alembic)/"
)


def changed_source_files(project_dir: Path, baseline_sha: str) -> list[str] | None:
    """Non-test `.py` files changed since *baseline_sha*, project-relative.

    None when git could not be asked — distinct from `[]` ("asked, and no
    Python source changed"). Deleted files are dropped: there is nothing left
    to mutate. So are files matching `_NOT_IMPORTED_RE`, which the test suite
    never imports.
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
        if _NOT_IMPORTED_RE.search(name):
            log.info("MUTATION not mutating %s: the test suite does not import it", name)
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
                      baseline_sha: str, *, project_dir: Path = Path(".")) -> str:
    """The cosmic-ray TOML for one run. Every value is JSON-encoded, which is
    valid TOML for strings, numbers and arrays of strings."""
    from .gates import _test_invocation
    argv, directory = _test_invocation(test_command, project_dir)
    wrapped = shlex.join(["env", f"PATH={os.environ.get('PATH', '')}", *argv])
    if directory != project_dir:
        wrapped = shlex.join(["sh", "-c", f"cd {shlex.quote(str(directory))} && exec {wrapped}"])
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


def mutant_timeout(configured: float, baseline_seconds: float) -> float:
    """Seconds allowed per test-suite run under one mutant: `mutation.timeout`,
    raised to `_BASELINE_FACTOR` times the unmutated suite's measured run."""
    return max(float(configured), _BASELINE_FACTOR * baseline_seconds)


class _ToolError(Exception):
    pass


_TURN_OFF = "`mutation.enabled: false` in specs/config.yaml"


def _run_tool(argv: list[str], cwd: Path, timeout: float) -> str:
    """Run one cosmic-ray command; return stdout or raise `_ToolError`.

    Own process group, so the terminal's SIGINT never reaches cosmic-ray.
    Whatever ends the wait — the timeout, a KeyboardInterrupt, a SystemExit
    from `_exit_on_termination` — goes through `_kill_group`, which kills
    cosmic-ray and the test run it has in flight (that run sits in a session
    of its own, see `_kill_group`) before the caller restores the tree.
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except OSError as e:
        raise _ToolError(
            f"could not start {argv[0]} ({e}). cosmic-ray runs through uv's "
            "`uvx`: install uv (https://docs.astral.sh/uv/), or turn mutation "
            f"testing off with {_TURN_OFF}"
        ) from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise _ToolError(f"{' '.join(argv[len(_UVX):len(_UVX) + 2])} timed out after "
                         f"{int(timeout)}s. To build without mutation testing, set "
                         f"{_TURN_OFF}")
    except BaseException:
        _kill_group(proc)
        raise
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-1500:]
        raise _ToolError(f"{' '.join(argv[len(_UVX):len(_UVX) + 2])} exited "
                         f"{proc.returncode}: {tail}\nIf cosmic-ray could not be "
                         "fetched (offline, no PyPI access, a private index) or "
                         f"cannot run here, build without it: set {_TURN_OFF}")
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill cosmic-ray's process group and every test run it has in flight.

    cosmic-ray 8.7.0 starts each test command with `start_new_session=True`
    (`cosmic_ray.testing.run_tests`) and kills that session only on its own
    per-mutant timeout. Killing cosmic-ray's group alone would orphan the test
    run: a looping mutant would run on, and could write into the tree after
    the caller restored it. So the group is stopped first, which keeps
    cosmic-ray from starting another test run and keeps the running one its
    child; then every process group below it is killed, then the group.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGSTOP)
    for pgid in _descendant_groups(proc.pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=10)


def _descendant_groups(root: int) -> set[int]:
    """Process group ids of every descendant of *root*, other than *root*'s own
    group and this process's. Empty when the process table cannot be read."""
    children: dict[int, list[tuple[int, int]]] = {}
    for pid, ppid, pgid in _process_table():
        children.setdefault(ppid, []).append((pid, pgid))
    groups: set[int] = set()
    seen: set[int] = set()
    stack = [root]
    while stack:
        for child, pgid in children.get(stack.pop(), ()):
            if child not in seen:
                seen.add(child)
                groups.add(pgid)
                stack.append(child)
    groups -= {root, os.getpgrp()}
    return {g for g in groups if g > 1}


_PROC = Path("/proc")


def _process_table() -> list[tuple[int, int, int]]:
    """(pid, ppid, pgid) of every visible process: from /proc on Linux, from
    `ps` elsewhere (macOS). Empty when neither can be read."""
    rows: list[tuple[int, int, int]] = []
    if (_PROC / "self" / "stat").exists():
        for entry in _PROC.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            # Fields after the parenthesised command: state, ppid, pgrp, ...
            fields = stat[stat.rindex(")") + 2:].split()
            rows.append((int(entry.name), int(fields[1]), int(fields[2])))
        return rows
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return rows
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return rows


# Shared with the skill runs in git_ops, which pass the same signals on.
_exit_on_termination = git_ops._exit_on_termination


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

    result = base
    # Whatever ends the run — an error, Ctrl-C, SIGTERM — a mutant cosmic-ray
    # left on disk is discarded before this returns or the exception goes on,
    # or a resume would offer it to the user as unverified work to commit.
    try:
        with _exit_on_termination():
            result = _run_cosmic_ray(config, baseline_sha, paths, project_dir, base)
    finally:
        _restore_tree(project_dir, restore_to, result)
    result.elapsed_seconds = round(time.monotonic() - started, 1)
    return result


def _run_cosmic_ray(config, baseline_sha: str, paths: list[str], project_dir: Path,
                    base: MutationResult) -> MutationResult:
    settings = config.mutation
    result = base
    test_timeout = float(settings.timeout)
    with tempfile.TemporaryDirectory(prefix="buildme-mutation-") as tmp:
        tmp_dir = Path(tmp)
        cfg = tmp_dir / "cosmic-ray.toml"
        session = tmp_dir / "session.sqlite"
        cfg.write_text(cosmic_ray_config(paths, config.test_command,
                                         settings.timeout, baseline_sha, project_dir=project_dir))
        baseline_cfg = tmp_dir / "baseline.toml"
        baseline_cfg.write_text(cosmic_ray_config(paths, config.test_command,
                                                  _SETUP_TIMEOUT, baseline_sha, project_dir=project_dir))
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
                # Its own config: the per-mutant timeout must not apply to
                # the unmutated run it is derived from.
                baseline_started = time.monotonic()
                _run_tool([*_UVX, "cosmic-ray", "baseline", str(baseline_cfg)],
                          project_dir, _SETUP_TIMEOUT + _EXEC_SLACK)
                test_timeout = mutant_timeout(
                    settings.timeout, time.monotonic() - baseline_started)
                cfg.write_text(cosmic_ray_config(paths, config.test_command,
                                                 test_timeout, baseline_sha, project_dir=project_dir))
                _run_tool([*_UVX, "cosmic-ray", "exec", str(cfg), str(session)],
                          project_dir, test_timeout * (to_run + 1) + _EXEC_SLACK)
            dump = _run_tool([*_UVX, "cosmic-ray", "dump", str(session)],
                             project_dir, _SETUP_TIMEOUT)
            result = parse_dump(dump)
            result.test_timeout = test_timeout
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
    return result


def _restore_tree(project_dir: Path, restore_to: str, result: MutationResult) -> None:
    """Discard a mutated file left in the tree back to *restore_to*."""
    if not git_ops.tree_is_clean(project_dir):
        result.mutant_left_on_disk = True
        if git_ops.discard_to(project_dir, restore_to, "mutation run"):
            log.warning("Mutation run left a mutated file in the tree — "
                        "discarded back to %s", restore_to[:8])
        else:
            result.ok = False
            result.discard_failed = True
            result.error = ((result.error + "; ") if result.error else "") + (
                "a mutated file was left in the tree and could NOT be discarded "
                f"back to {restore_to[:8]} — inspect the tree before shipping")


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
    if final.test_timeout:
        lines.append(
            f"- Time allowed per test run under a mutant: {final.test_timeout:.0f} s "
            f"(`mutation.timeout`, or {_BASELINE_FACTOR:g}× the unmutated suite's "
            "run when that is longer)")
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
