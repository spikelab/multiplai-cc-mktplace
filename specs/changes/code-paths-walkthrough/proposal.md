## Why

codebase-walkthrough has its own trace stage, and that stage only finds web entry points. It looks for webhook routes, routes called from other repositories, Celery beat entries, and then other routes (`stages/trace.py`, `pick_seeds`). It traces at most `--scenarios` of them, which defaults to 6.

On a CLI, script, skill or hook repository the stage traces nothing. The walkthrough then cannot say which features reach the module. A reader needs that information to know what end-to-end tests have to exercise.

PR 1 (#374) added shared path finding to `code_facts`. It covers:

- entry-point discovery for argparse, click and typer commands, `main`, scripts, skill commands, hooks and caller seeds;
- gated tracing;
- end-to-end test listing;
- a `paths.v1` output.

Keeping a second, web-only tracer in the walkthrough duplicates that work and misses most entry kinds. This change is PR 2 of 4 and stacks on PR 1. The walkthrough should use the shared path finder before review and review-viewer are moved onto it in later PRs.

## What Changes

- The walkthrough's trace stage calls `code_facts.map_paths`. Its scope is the whole module being explained: every line of every file in the module is passed as `changed_lines`. It does not pass `None`, because `None` would mean an unscoped run.
- Today's web seeds are passed as caller seeds. Each seed is located at a place that imports into the module, so seeds that reach the module only over HTTP or a queue are not lost. The existing boundary edges (cross-repo references, routes, tasks) are passed as the gate's known places.
- The walkthrough's own seed picking, trace gate and `trace_answers` state are removed. Answers are cached under the run directory, so `resume` asks only for answers that are missing.
- `--scenarios` keeps its name, now caps the number of paths, and defaults to 21. `--help` says that it caps paths.
- Entries are kept in the order `code_facts` ranks them. Every entry left out appears in the coverage table with its stage and reason, including `seed_not_found`, `unreachable` and `over_cap`.
- In human mode, a mandatory **Paths** section replaces "End-to-end scenarios". For each path it gives:
  - the entry point, its kind, and who invokes it;
  - its hops, rendered as `path:line` links with ids `p{n}h{k}`;
  - where the trace stops and why;
  - the e2e tests: those counted, those that mock the next hop (listed as not counted), or "no e2e test".

  Writers cite hops by id. When no path is found, or path finding fails, the section still appears and says why.
- In human mode, `<name>-paths.json` is written beside `<name>-walkthrough.md` using `write_paths_file`.
- `check --at-head` re-checks path hops from the Markdown alone, the same way it re-checks scenario hops today.
- `SKILL.md` stage 5 is updated, and the Paths section and `paths.json` are documented.

**Goals**

- Walkthroughs of CLI, script, skill and hook repositories show the paths that reach the module.
- Every web entry traced today is still traced.
- Each path shows whether an e2e test exercises it.
- Every entry left out is visible, with its reason.
- Already committed walkthroughs, including those with "End-to-end scenarios", still pass `check --at-head` while their cited lines do not move.

**Non-Goals**

- No changes to review or review-viewer.
- No changes to agent mode. It still runs no trace, has no Paths section and writes no `paths.json`.
- No change to the shape of `<name>-walkthrough.json` (v1) beyond the renamed section title.
- `check` and buildme do not read `paths.json`.
- No new cost or time knobs. `trace_limit` stays at 30 and spend stays under the existing `--max-usd` breaker.
- No re-ranking of `code_facts` entries in the walkthrough.
- Agents do not get write access or the ability to run code.

## Capabilities

### New Capabilities

- `walkthrough-path-tracing`
- `walkthrough-paths-section`
- `walkthrough-paths-file`
- `walkthrough-path-hop-check`

### Modified Capabilities

## Impact

- **Code:** `plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/`
  - `stages/trace.py`: seed picking and the trace gate are removed and `map_paths` is wired in.
  - `stages/write.py`: section list and writer prompts.
  - `render.py`: Paths rendering and the `paths.json` path.
  - `models.py`: hop citation ids.
  - `__main__.py`: `--scenarios` default and help.
  - `check.py`: hop re-check.
  - Pipeline state: the `scenarios` and `trace_answers` fields.
- **Dependencies:**
  - `code_facts`: `map_paths`, `write_paths_file`, `Seed`, `Location`, `PathEntry`, `PathHop`, `TracedPath`, `PathDrop`, `E2ETest`, `MockedE2ETest`, and the `paths.v1.schema.json` schema.
  - Calls are wired the way `stages/explore.py` already wires them: `repo_resolver`, `sdk.agent_call_structured`, `sdk.TRACE_TOOLS` and `sdk.AgentCallError`.
- **Errors:**
  - A trace that fails for one entry becomes a drop and shows in coverage.
  - A failure of the whole stage goes to `state.errors` and is reported in the Paths section.
  - `RepoTrustError` and the budget breaker still stop the run, as they do today.
- **Outputs:**
  - A new `<name>-paths.json` file.
  - In `<name>-walkthrough.json`, one step is titled "Paths" instead of "End-to-end scenarios".
  - buildme's stale-walkthrough detection (`buildme/scripts/build_pipeline/walkthroughs.py`) is unaffected, because it reads only `*-walkthrough.md`.
- **Cost:** up to 21 traced paths instead of 6, so a run makes more model calls. The extra spend stays under the existing `--max-usd` breaker ($50 by default). The on-disk answer cache means a resumed run does not pay twice.
- **Docs and release:**
  - `skills/codebase-walkthrough/SKILL.md`.
  - A multiplai-dev version bump after 0.80.0, with a `CHANGELOG.md` entry (`scripts/check_changelog.py`).
- **Tests:** the walkthrough suite covers:
  - fixture repositories with CLI and skill-command entries;
  - web seeds;
  - the cap of 21 and the coverage table for dropped entries;
  - the Paths section text, including "no e2e test" and the empty-section text;
  - `paths.json` against the `paths.v1` schema;
  - `check --at-head` on path hops.

  Suites run with the CI form `uv run --project ... --package ...`, not `--directory`.