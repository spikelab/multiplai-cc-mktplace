"""Paired, held-out Buildme pilot. Not imported by the normal test suite."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "tasks.json"
VARIANTS = ("current", "candidate", "candidate+plan-review", "candidate+mutation")


def command(argv, cwd, timeout=60):
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def initialize(root: Path, task: dict) -> None:
    root.mkdir(parents=True)
    for name, contents in task["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (root / "user-notes.txt").write_text("working copy\n")
    for argv in (["git", "init", "-q"], ["git", "config", "user.name", "Buildme evaluation"],
                 ["git", "config", "user.email", "buildme-eval@example.invalid"],
                 ["git", "add", "."], ["git", "commit", "-qm", "held-out fixture baseline"]):
        result = command(argv, root)
        if result.returncode:
            raise RuntimeError(result.stderr)


def seed_user_staging(root: Path) -> None:
    """Plant the recovery sentinel only after clean worktree bootstrap."""
    (root / "user-notes.txt").write_text("staged user work\n")
    command(["git", "add", "user-notes.txt"], root)
    (root / "user-notes.txt").write_text("working copy\n")
    marker = command(["git", "rev-parse", "--git-path", "buildme-eval-staging-seeded"], root)
    path = Path(marker.stdout.strip())
    (path if path.is_absolute() else root / path).write_text("seeded\n")


def preserved_user_work(root):
    marker = command(["git", "rev-parse", "--git-path", "buildme-eval-staging-seeded"], root)
    path = Path(marker.stdout.strip())
    seeded = (path if path.is_absolute() else root / path).exists()
    expected = "staged user work\n" if seeded else "working copy\n"
    return ((root / "user-notes.txt").read_text() == "working copy\n"
            and command(["git", "show", ":user-notes.txt"], root).stdout == expected)


def acceptance(task: dict, project: Path) -> bool:
    # Acceptance source is supplied only after the worker exits. It is never
    # written into the candidate checkout or included in its prompt.
    return command([sys.executable, "-c", task["acceptance"]], project).returncode == 0


def decision(rows: list[dict], *, live: bool, task_ids: set[str], repetitions: int) -> dict:
    if not live:
        return {"decision": "pending", "reason": "Dry runs validate measurement, not model effectiveness"}
    current = {(r["task"], r["repeat"]): r for r in rows if r["variant"] == "current"}
    candidate = {(r["task"], r["repeat"]): r for r in rows if r["variant"] == "candidate"}
    expected = {(task, repeat) for task in task_ids for repeat in range(repetitions)}
    if len(task_ids) < 6 or repetitions < 3 or current.keys() != expected or candidate.keys() != expected:
        return {"decision": "retain", "reason": "Incomplete paired observations"}
    paired = [(current[k], candidate[k]) for k in current]
    if any(r["false_success"] or r.get("data_loss") for pair in paired for r in pair):
        return {"decision": "retain", "reason": "Safety failure: investigate individually"}
    for task in {k[0] for k in current}:
        old = sum(r["acceptance"] for k, r in current.items() if k[0] == task)
        new = sum(r["acceptance"] for k, r in candidate.items() if k[0] == task)
        if new < old:
            return {"decision": "retain", "reason": f"Acceptance regressed for {task}"}
    if any(r.get("cost_usd") is None for pair in paired for r in pair):
        return {"decision": "retain", "reason": "Missing cost metadata"}
    costs = [b["cost_usd"] / a["cost_usd"] for a, b in paired if a["cost_usd"] > 0]
    times = [b["elapsed_s"] / a["elapsed_s"] for a, b in paired if a["elapsed_s"] > 0]
    if not costs or not times:
        return {"decision": "retain", "reason": "Insufficient nonzero measurements"}
    cost, latency = statistics.median(costs), statistics.median(times)
    # Predeclared material regression: more than 10% on the other dimension.
    prefer = (cost <= .8 and latency <= 1.1) or (latency <= .8 and cost <= 1.1)
    return {"decision": "prefer-candidate" if prefer else "retain", "median_paired_cost_ratio": cost,
            "median_paired_latency_ratio": latency, "reason": "Pilot only; production defaults require review"}


def measure(task: dict, variant: str, repeat: int, root: Path, args) -> dict:
    checkout = root / f"{task['id']}-{variant}-{repeat}"
    initialize(checkout, task)
    project = checkout / task.get("project", ".")
    start = time.monotonic()
    metrics = {}
    timed_out = False
    if args.dry_run:
        seed_user_staging(checkout)
        # A known solution tests measurement and isolation; the weak-test task
        # deliberately claims success with the wrong code.
        if task["id"] != "weak-tests":
            for name, text in task["solution"].items():
                (checkout / name).write_text(text)
        succeeded = True
        metrics = {"cost_usd": 0, "calls": 0, "tokens": 0, "resume": "dry-run-only"}
    else:
        public = {k: v for k, v in task.items() if k in {"id", "instruction", "test_command", "project"}}
        public_file = root / f"{checkout.name}.public.json"
        public_file.write_text(json.dumps(public))
        argv = [sys.executable, str(Path(__file__).resolve()), "--worker", str(public_file),
                "--checkout", str(checkout), "--variant", variant, "--model", args.model,
                "--per-run-usd", str(args.per_run_usd)]
        try:
            result = command(argv, HERE.parent, args.timeout)
            (root / f"{checkout.name}.log").write_text(result.stdout + result.stderr)
            lines = [line[len("EVAL:"):] for line in result.stdout.splitlines() if line.startswith("EVAL:")]
            metrics = json.loads(lines[-1]) if lines else {}
            project = Path(metrics.get("project", str(project)))
            succeeded = result.returncode == 0 and metrics.get("verified", False)
        except subprocess.TimeoutExpired:
            timed_out, succeeded = True, False
    elapsed = time.monotonic() - start
    try:
        passed = acceptance(task, project)
    except (OSError, subprocess.TimeoutExpired):
        passed = False
    return {"task": task["id"], "variant": variant, "repeat": repeat,
            "acceptance": passed, "claimed_success": succeeded,
            "false_success": bool(succeeded and not passed), "timeout": timed_out,
            "elapsed_s": elapsed, "cost_usd": metrics.get("cost_usd"), "calls": metrics.get("calls"),
            "tokens": metrics.get("tokens"), "manual_intervention": metrics.get("manual_intervention", False),
            "resume": metrics.get("resume", "not-exercised"),
            "verified_corrections": metrics.get("verified_corrections", []),
            "data_loss": not preserved_user_work(checkout)}


def ready_specs(project: Path, change: str, instruction: str) -> None:
    """The pm-spec handoff buildme builds from: a proposal, use cases and one
    requirement stating the task's instruction, marked ready and committed."""
    from multiplai_core.specs import mark_ready

    change_dir = project / "specs" / "changes" / change
    (change_dir / "requirements").mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text(
        f"# {change}\n\n## Why\n{instruction}\n\n## Capabilities\n\n### New Capabilities\n- `{change}`\n")
    (change_dir / "use-cases.md").write_text(
        f"# Use cases\n\n## Personas\n- Developer: maintains this code\n\n## Use cases\n"
        f"- Developer wants this so that: {instruction}\n")
    (change_dir / "requirements" / f"{change}.md").write_text(
        f"## ADDED Requirements\n\n### Requirement: Task\nThe system SHALL do this: {instruction}\n\n"
        "#### Scenario: Done\n- **WHEN** the task's tests run\n- **THEN** they pass\n")
    mark_ready(change_dir)
    for argv in (["git", "add", "specs"], ["git", "commit", "-qm", f"specs: {change} ready"]):
        result = command(argv, project)
        if result.returncode:
            raise RuntimeError(result.stderr)


async def worker(args) -> int:
    sys.path.insert(0, str(HERE.parent))
    from build_pipeline import budget, orchestrator, spec_generator, tdd_engine
    from build_pipeline.config import BuildConfig, GitToggles
    from build_pipeline.state import BuildState
    task = json.loads(args.worker.read_text())
    ready_specs(args.checkout / task.get("project", "."), "pilot", task["instruction"])
    config = BuildConfig(project_dir=args.checkout / task.get("project", "."), change_name="pilot",
                         project_description=task["instruction"], auto=True,
                         skip_research=True, model=args.model, budget_max_usd=args.per_run_usd,
                         git=GitToggles(push=False, pr="none"))
    config.specs_dir = config.project_dir / "specs"
    config.test_command = task["test_command"]
    os.environ["BUILDME_TRUST_REPO"] = "1"
    # Evaluation-only composition of existing steps: identical implementation,
    # independent code review, recovery and final verification in both arms.
    if args.variant != "current":
        config.mutation.enabled = args.variant == "candidate+mutation"
        config.review_panel = []
        async def skip_refactor(*_args):
            return True
        tdd_engine._run_refactor_all = skip_refactor
        def deterministic_plan(cfg, state):
            from build_pipeline.change_manager import ChangeManager
            from build_pipeline.models import ArtifactStatus
            spec_generator.reconcile_plan(cfg, state)
            status = ChangeManager(cfg.specs_dir).artifact_status(cfg.change_dir, state.spec_gen.stale_artifacts)
            if any(value != ArtifactStatus.DONE for value in status.values()):
                raise RuntimeError("Evaluation candidate has an incomplete plan")
            tasks = cfg.change_dir / "tasks.md"
            if not tdd_engine.parse_blocks(tasks) or spec_generator.scan_placeholders(tasks.read_text()) or spec_generator.scan_no_behaviour_blocks(tasks):
                raise RuntimeError("Evaluation candidate failed deterministic plan validation")

        async def advisory(change_dir, cfg, state, state_path):
            deterministic_plan(cfg, state)
            state.spec_gen.record_step("design_audit", "passed", "evaluation: deterministic checks; model audit omitted")
            state.checkpoint(state_path)
            return []
        spec_generator.run_design_audit_stage = advisory
        if args.variant != "candidate+plan-review":
            from build_pipeline.llm_steps import plan_review_steps
            async def skip_plan(change_dir, cfg, state, state_path):
                deterministic_plan(cfg, state)
                state.spec_gen.record_step("plan_review", "passed", "evaluation: deterministic checks; model panel omitted")
                state.checkpoint(state_path)
                return []
            plan_review_steps.run_plan_review_stage = skip_plan
    # The harness cannot publish, even if a future config default drifts.
    def no_publish(*_args, **_kwargs):
        raise AssertionError("Evaluation must never publish")
    orchestrator.git_ops.push_branch = no_publish
    run_args = argparse.Namespace(
        change="pilot", project_dir=str(config.project_dir), auto=True,
        spec_only=False, skip_research=True,
        force=False, accept_size=False, block=None,
        research_path="", session_id="")
    # Exercise one deterministic interruption at an atomic TDD checkpoint in
    # both arms. Resume goes through the same public entry, with saved spend.
    original_create_worktree = orchestrator.git_ops.create_worktree
    user_staging_seeded = False
    def create_worktree(repo, *a, **kw):
        nonlocal user_staging_seeded
        result = original_create_worktree(repo, *a, **kw)
        seed_user_staging(repo)
        user_staging_seeded = True
        return result
    orchestrator.git_ops.create_worktree = create_worktree
    original_checkpoint = BuildState.checkpoint
    interrupted = False
    def checkpoint(state, *a, **kw):
        nonlocal interrupted
        original_checkpoint(state, *a, **kw)
        if state.tdd is not None and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("evaluation checkpoint interruption")
    BuildState.checkpoint = checkpoint
    try:
        try:
            rc = await orchestrator.run_orchestrator(config, run_args)
        except KeyboardInterrupt:
            rc = await orchestrator.run_orchestrator(config, run_args)
    finally:
        BuildState.checkpoint = original_checkpoint
        orchestrator.git_ops.create_worktree = original_create_worktree
    paths = [config.state_file_path(), config.state_file_path().with_name(".build-state.complete.json")]
    saved = next((BuildState.load(p) for p in paths if p.exists()), None)
    verified = bool(saved and saved.verification and saved.verification.get("passed"))
    corrections = []
    if saved and saved.tdd:
        for block in saved.tdd.blocks:
            if block.review_iterations and block.status.value == "done":
                corrections.append({"block": block.number, "review_iterations": block.review_iterations,
                                    "verified": verified})
    spend = budget.get_budget()
    print("EVAL:" + json.dumps({"verified": rc == 0 and verified, "project": str(config.project_dir),
                                "cost_usd": None if spend.unaccounted_calls else spend.cost_usd,
                                "tokens": spend.total_tokens, "calls": spend.calls,
                                "user_staging_seeded": user_staging_seeded,
                                "manual_intervention": rc == 3,
                                "resume": ("resumed-successfully" if rc == 0 else "resume-blocked") if interrupted else "not-reached",
                                "verified_corrections": corrections}))
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "pilot-report.json")
    parser.add_argument("--model", default="")
    parser.add_argument("--per-run-usd", type=float, default=5)
    parser.add_argument("--max-total-usd", type=float)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--ablations", action="store_true")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--variant", choices=VARIANTS, default="current")
    args = parser.parse_args()
    if args.worker:
        return asyncio.run(worker(args))
    if args.self_check:
        args.dry_run = True
    if not args.dry_run and (not args.model or args.max_total_usd is None):
        parser.error("Live pilot requires an explicit model/version and --max-total-usd envelope")
    if args.repetitions < 1 or args.per_run_usd <= 0 or args.timeout <= 0:
        parser.error("Repetitions, per-run cap and timeout must be positive")
    tasks = json.loads(FIXTURES.read_text())
    variants = VARIANTS if args.ablations else VARIANTS[:2]
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    directory = tempfile.mkdtemp(prefix=args.output.stem + "-runs-", dir=args.output.parent)
    try:
        for task in tasks:
            for repeat in range(args.repetitions):
                for variant in variants:
                    spent = sum(r["cost_usd"] or 0 for r in rows)
                    if not args.dry_run and (any(r["cost_usd"] is None for r in rows)
                                            or spent + args.per_run_usd > args.max_total_usd):
                        break
                    rows.append(measure(task, variant, repeat, Path(directory), args))
    finally:
        if args.dry_run:
            import shutil
            shutil.rmtree(directory)
    report = {"runs_directory": None if args.dry_run else directory, "kind": "dry-run" if args.dry_run else "live-pilot", "model": args.model or "none",
              "decision": decision(rows, live=not args.dry_run, task_ids={task["id"] for task in tasks}, repetitions=args.repetitions), "rows": rows,
              "limitations": ["Pilot, not broad statistical proof", "Live runs exercise one atomic TDD-checkpoint interruption; dry runs do not measure model recovery",
                              "Already admitted provider calls can exceed the admission cap"]}
    if args.self_check:
        assert len(rows) == len(tasks) * args.repetitions * len(variants)
        assert all(r["false_success"] for r in rows if r["task"] == "weak-tests")
        assert all(r["acceptance"] for r in rows if r["task"] != "weak-tests")
        assert report["decision"]["decision"] == "pending"
        assert not any(r["data_loss"] for r in rows)
        example = [{"task": task["id"], "repeat": i, "variant": v, "acceptance": True,
                    "false_success": False, "cost_usd": 1 if v == "current" else .7,
                    "elapsed_s": 1, "data_loss": False} for task in tasks for i in range(3) for v in VARIANTS[:2]]
        assert decision(example, live=True, task_ids={task["id"] for task in tasks}, repetitions=3)["decision"] == "prefer-candidate"
        assert decision(example[:6], live=True, task_ids={task["id"] for task in tasks}, repetitions=3)["decision"] == "retain"
        assert decision(example, live=True, task_ids={task["id"] for task in tasks}, repetitions=4)["decision"] == "retain"
        example[1]["cost_usd"] = None
        assert decision(example, live=True, task_ids={task["id"] for task in tasks}, repetitions=3)["reason"] == "Missing cost metadata"
        example[1]["false_success"] = True
        assert "Safety failure" in decision(example, live=True, task_ids={task["id"] for task in tasks}, repetitions=3)["reason"]
        try:
            command([sys.executable, "-c", "import time; time.sleep(5)"], HERE, timeout=.01)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("timeout must be counted as a failed observation")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {args.output}; default decision: {report['decision']['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
