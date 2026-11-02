# The component check's replay eval

This eval measures pm-spec's component check
(`pm_spec_pipeline/component_check.py`, prompt `COMPONENT_CHECK_PROMPT` in
`pm_spec_pipeline/prompts/component_check.py`) against requirements problems
buildme really raised after a pm-spec handoff. It runs the check on the commit
each build read, then counts four things:

- **Problems caught.** These are the problems in `fixtures.yaml`, P1-P10 from
  the DFACCMGMT-1 epic. For each one, a judge call with no tools says whether a
  finding of the check describes the same problem.
- **Real kept.** These are findings in `labels.yaml` labelled `real` that a
  finding of this run repeats. A second judge call per check maps each finding
  to a labelled finding of the same commit and component, or to none.
- **False raised.** These are findings labelled `false` that a finding of this
  run repeats.
- **Unlabelled.** These are findings that repeat no labelled finding. The
  report lists them with a blank verdict, and the eval counts them as neither
  real nor false until someone labels them.

The problems in `heldout.yaml` run too, and the report shows them in a section
of their own. They never count toward the numbers above.

`eval_score.py` computes the counts. `test_eval_score.py` tests them in the
ordinary suite, with no model call.

## Running it

It makes real model calls, so it runs only when `PM_SPEC_EVAL=1` is set:

```bash
cd plugins/multiplai-pm/skills/pm-spec/scripts
PM_SPEC_EVAL=1 PM_SPEC_EVAL_TAG=<name> uv run --project ../../../../.. \
    --package pm-spec-pipeline --extra dev \
    python -m pytest tests/eval_component_check/test_replay.py -q -s
```

- **Inputs.** It reads the clone named by `repo` in each YAML file (for
  `fixtures.yaml`, `$PM_SPEC_EVAL_REPO` overrides it). It reads local commits
  only, so each fixture's commit must be in that clone.
- **Worktrees.** It adds a temporary detached worktree per commit and removes
  it afterwards. It writes nothing else to the repository and pushes nothing.
- **Output.** It writes `$WORKSPACE/INBOX/pm-spec-component-check-eval-<date>[-<tag>].md`,
  which starts with the summary table, and a `.json` beside it with the raw
  findings and the judge answers. `$PM_SPEC_EVAL_OUT` overrides the folder. A
  run with the same date and tag overwrites the earlier one.
- **Cost.** One run of the 10 DFACCMGMT-1 checks and the held-out check cost
  about $9-12 in November 2026 (each report gives its own total). It stops
  after the first check if that check cost more than $8, or if its cost times
  the number of checks is more than $60.
- **Never run two at once.** Two runs share the clone.

After you label a run's unlabelled findings (see below), recount that run
with no model call:

```bash
uv run --project ../../../../.. --package pm-spec-pipeline \
    python tests/eval_component_check/test_replay.py --rescore <the run's .json>
```

## Adding a problem from a new epic

When buildme raises a `spec-issues.md` after a pm-spec handoff, that issue is
a new problem. Add it to the eval as follows.

1. **Find the commit the build read.** Use either of these:
   - the build worktree's merge-base with the feature branch:
     `git -C <repo> merge-base <build branch> origin/<feature branch>`;
   - or, if a revise resolved the issue, the parent of that revise commit.
     The file is renamed to `spec-issues.resolved-<id>.md`, and
     `git log -- <that path>` finds the commit that renamed it.

   Check that the commit exists in the local clone (`git cat-file -t
   <commit>`), and write in `why` how you found it.
2. **Copy the full text.** Copy the whole `spec-issues.md` into `text`, the
   heading line as it is into `heading`, and its path into `source`. Use
   `fixtures.yaml` for a problem of the epic it already covers. Use
   `heldout.yaml` for a problem from another change, and give it its own
   `repo` and `change`. Follow the rule in the next section about which file
   comes first.
3. **Run the eval.**
4. **Label each unlabelled finding of that run.** Read the code and the specs
   at the finding's commit, read-only (`git show <commit>:<path>`,
   `git grep <pattern> <commit>`), and decide whether it is real. Add one
   entry to `labels.yaml` per finding, with these fields:
   - `id` (`<commit>/<component>/A-<run>-<n>`), `commit`, `component`;
   - `source: adjudicated-<run>`, `run: <run>` (the report's name, for example
     `2026-11-02-baseline`) and `n` (the finding's number in its check);
   - `severity` and `description`, copied from the report;
   - `verdict: real` or `verdict: false`;
   - `reason`: one sentence;
   - `evidence`: the files and lines you read at the commit.

   A label needs a reason backed by code or spec text read at the commit. A
   finding that only looks plausible is not labelled real. Then recount the
   run with `--rescore`.

## Problems found after a prompt change go in `heldout.yaml` first

A problem found after the prompt last changed goes into `heldout.yaml`, never
straight into `fixtures.yaml`. Measure it once, caught or missed, on the
prompt as it is. Only after that may it be used to edit the prompt, and then
it moves to `fixtures.yaml`. Otherwise the eval only measures problems the
prompt was edited to catch, and that says nothing about the next epic.

The prompt also never names an endpoint, file, component or problem from
these files.

## When to run it

- After every change to `COMPONENT_CHECK_PROMPT` or to what the check reads.
  Compare the report's table with the last one.
- After each new epic adds problems to `fixtures.yaml` or `heldout.yaml`.
