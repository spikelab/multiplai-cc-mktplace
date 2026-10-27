# Buildme paired pilot

Production defaults remain **unchanged; the live decision is pending**. No live
provider run was authorized for this repair. `dry-run-report.json` records three
paired repetitions of seven fixtures, including JavaScript and a nested Python
project. The deliberately wrong weak-test solution is counted as false success;
these synthetic results validate detection, not pipeline effectiveness.

From this scripts directory, run the offline harness self-check:

```sh
python evals/pipeline_compare.py --self-check --output evals/dry-run-report.json
```

A live pilot requires credentials, an explicit model/version, and an approved
spend envelope. For example, after those prerequisites are supplied:

```sh
uv run --project . python evals/pipeline_compare.py \
  --model '<approved-model-version>' --max-total-usd 210 \
  --per-run-usd 5 --repetitions 3 --timeout 1200 --output /tmp/buildme-pilot.json
```

The example envelope covers 42 runs at $5 admission limits. Already admitted
provider calls can exceed a limit; this is not a guaranteed billing ceiling.
Missing usage metadata stops further runs. Live run logs and checkouts are retained
beside the report, whose `runs_directory` points to them. Child skills retain their separately
accounted budgets. Add `--ablations` only with an envelope covering its extra runs.

Both variants use the production orchestrator, isolated worktrees, complete
requirements, TDD, code review with bounded fixes, recovery and final verification.
Each worker first writes the ready specs buildme builds from (pm-spec's handoff:
a proposal, use cases and one requirement stating the task's instruction).
The candidate omits the whole-change refactor, mutation and the extra
model design/plan panels; required deterministic plan validation remains. The
two ablations add plan review or mutation back individually. No
production configuration flags or defaults were introduced for this experiment.
Publication is disabled and any attempted push raises an error.

Acceptance programs stay outside candidate checkouts and prompts and run only
after workers stop. Fresh repositories isolate every task/repetition/variant.
After clean worktree creation, an unrelated file in the original checkout gets
different staged and working contents; both are compared
after the run to detect source-checkout loss. This sentinel does not exercise
protection of edits inside the active build worktree; real-Git regression tests
cover those operations separately. Each live arm injects one interruption immediately
after an atomic TDD checkpoint, then resumes through the public build entry.
The report distinguishes resumed success, a blocked resume and a checkpoint that
was never reached. Dry runs explicitly do not measure model resume behavior.

Timeouts kill the worker process group and remain failed observations in the
denominator. Each row reports held-out acceptance, claimed success, false success,
data loss, cost, tokens, calls, elapsed time, manual intervention and resume outcome.
Review correction rows name blocks with review iterations that reached DONE and
whether the final revision verified; this is not a claim that every review comment
was causally necessary. Provider exceptions without usable spend remain unknown.

The decision rule is declared before live runs: at least three complete paired
repetitions per task, no observed false success or data loss, no task losing
acceptance successes, and at least 20% improvement in median paired cost or time,
with no more than 10% regression in the other dimension. Otherwise retain the
current flow. Any safety failure requires individual investigation. This is a
small pilot, not broad statistical proof or a calibrated quality score.

Bootstrap regression coverage is separate from the synthetic self-check:
`tests/test_eval_worker.py` executes the actual worker and orchestrator against
an offline provider double. It asserts that a clean source creates a worktree,
reaches the pipeline SDK adapter (replaced by the double), and preserves staged/working user versions planted
in the source checkout after worktree creation. This checks harness startup; it does not execute the installed core runner or
provider CLI and does not establish model effectiveness. No live pilot has been run.

# Notes-forward benchmark

`notes_forward_compare.py` measures `carry_block_notes`. Each run copies one
of five fixtures under `fixtures/multiblock/` into a fresh git repository and
runs `python -m build_pipeline tdd` on its frozen change directory, with the
option off (`current`) or on (`notes`). Planning is not run, so both arms
build the same four blocks; mutation testing is off in both. Four fixtures
hide a fact about the starting code that the spec does not state, which an
early block must find and a later block must respect; `control-text` has none.
The held-out tests in each fixture's `acceptance/` run after the build, from
outside the checkout.

Per run it reports acceptance, claimed and false success, cost and tokens
(the `SPEND:` line), elapsed time, and per block the review rounds, the size of
the saved notes, and the implementer's turns and end-of-call context, read
from the Claude Code transcripts of the checkout. The report's first line is
`COMPARABLE` or `NOT COMPARABLE`, and it ends with the two verdicts the plan's
decision rules give: the option's default, and whether to propose session
resume.

```sh
python evals/notes_forward_compare.py --self-check
uv run --project . python evals/notes_forward_compare.py --control-run \
  --model '<model id>' --effort '<effort>' --per-run-usd 50 --output /tmp/nf-control.json
uv run --project . python evals/notes_forward_compare.py \
  --model '<model id>' --effort '<effort>' --window-tokens '<window>' \
  --per-run-usd '<3x control>' --max-total-usd '<10x control>' --output /tmp/nf.json
```

The fixtures are small, so an agent can often find a planted fact by reading
the code; expect the benefit to show more in implementer turns than in
acceptance. No live run has been made.
