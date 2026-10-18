# Use cases — who this change is for

## Personas

### walkthrough-migrator
- **Who they are:** A developer on this repository who will move codebase-walkthrough onto the shared `code_facts` library in PR 2. Today the walkthrough traces only from webhooks, routes and Celery tasks. It traces across several repositories, using `<repo key>/<path>` paths and its own `boundary_places`.
- **The job they are doing:** Replacing the walkthrough's own path tracing with the library's. They want to do this without changing the library's API, and without the walkthrough's output getting worse on the web services it already handles.
- **The constraint they are under:** PR 1 must not change any skill's behaviour, so they can only adopt what PR 1 ships as it is. Their multi-repo snapshots and boundary places have to fit the API as delivered. The two copies of the gate logic coexist until their PR lands.

### review-skill-author
- **Who they are:** A developer who will make the review skill run one finder agent per call path, in PR 3. They are building on the PR #295 benchmark. Per-file finders missed R-009, R-017, R-042 and R-056 in 3 of 3 runs. One finder per path found all four high-severity bugs in every run.
- **The job they are doing:** Getting a ranked, capped list of the call paths that cross a pull request's changed lines, each with cited hops, ready to hand one path to each finder.
- **The constraint they are under:** Each run already costs about $24, and they will pass a cap of 21. They need to know why entries were left out, without paying for a trace agent on every subcommand in the repository. The library never budgets or retries, so all spending control sits in their own `CallFn`.

### review-viewer-author
- **Who they are:** A developer who will build the review-viewer's view of how a change is reached, in PR 4. Today the viewer has nothing to show a reader on this.
- **The job they are doing:** Showing a reader of a review each entry point that reaches the change, who invokes it, the path from it, and whether any end-to-end test exercises that path.
- **The constraint they are under:** They consume only the `paths.json` file and its schema, with no access to the agents' reasoning. The same path must keep the same id across reruns and resumed runs, or links in the viewer break.

### pr1-reviewer
- **Who they are:** The maintainer reviewing and merging this PR (PR 1 of 4) into the shared library that other skills depend on.
- **The job they are doing:** Confirming that the new path-mapping capability works on fixture repositories, that existing `code_facts` exports behave as before, and that no skill changed.
- **The constraint they are under:** The bar for done is fixture tests plus green CI, with no real model calls in the tests. There is no pass/fail check against a real repository. They have only the PR description's one-off PR #295 note to judge real-world fit.

## Use cases

- walkthrough-migrator wants to pass the walkthrough's existing boundary places to `map_paths` unchanged, so that hops into routes, tasks and cross-repo references show as passing the gate in `paths.json`, rather than as broken paths with a `break_reason`.
- walkthrough-migrator wants to run `map_paths` on a snapshot holding one directory per repository, so that every entry and hop path in `paths.json` appears as `<repo key>/<path>`, matching the paths the walkthrough already prints.
- walkthrough-migrator wants to supply the walkthrough's web entry points as seeds, so that those routes, webhooks and tasks appear in `paths.json` next to the CLI, script, hook and skill-command entries that discovery found.
- walkthrough-migrator wants discovery to find the entry points of a CLI or skill repository such as this one, so that running the walkthrough on it in PR 2 lists the `pr_pipeline` `open`, `edit` and `render` subcommands instead of finding nothing.
- review-skill-author wants to pass a pull request's changed head lines and a cap of 21, so that `paths.json` holds at most 21 paths, ordered by how many changed lines each covers, most first.
- review-skill-author wants every entry and path left out to carry a reason, so that they can read in `paths.json` why each one was dropped, for example `cannot import a changed file` or `over the cap of 21`.
- review-skill-author wants entries that cannot reach a changed file to be dropped before any trace agent runs, so that the number of model calls their `CallFn` logs for a run stays well below the number of subcommands in the repository.
- review-skill-author wants a change that only deletes lines to count, once the deletion is mapped to its neighbouring head line, so that the path through the deleted code appears in `paths.json` instead of being dropped as outside the change.
- review-skill-author wants a handler reached from a script, a subcommand, a `SKILL.md` command and a hook to be traced once, so that it fills one of the 21 slots, with the other invokers listed under `also_invoked_by`. *(Assumption to confirm.)*
- review-skill-author wants a trace that breaks to keep its hops up to the last good one, so that `paths.json` shows the hop number where the path stopped and the reason, and finders are not handed uncited hops.
- review-skill-author wants a resumed run to reuse cached agent answers, so that the second run's `CallFn` log shows calls only for entries that were not answered before, and its `paths.json` matches the first.
- review-viewer-author wants each path to carry the same `p<N>` id when the run is repeated or resumed with the same inputs, so that a link to `p3` in the viewer still opens the same path after a rerun.
- review-viewer-author wants each path to list the end-to-end tests that invoke its entry as `file::Class::test_name` with a line, so that a reader sees which tests exercise the path, or sees an empty list when none do.
- review-viewer-author wants tests that mock a path's second hop to be listed separately, so that a reader can tell a test that runs the real path from one that replaces it with a mock.
- review-viewer-author wants each entry to state its kind, who invokes it and where it is declared, so that a reader sees whether the path starts with a human at the CLI, an agent following a `SKILL.md`, or a hook. For a skill command or hook, that place is the line in `SKILL.md` or `hooks.json`. *(Assumption to confirm.)*
- review-viewer-author wants `paths.json` to validate against `schema/paths.v1.schema.json`, so that they can build the viewer against the schema and reject a malformed file before it reaches a reader.
- pr1-reviewer wants the library's test suite to pass with a fake `CallFn`, so that they can run `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/` without a model key and see it go green.
- pr1-reviewer wants a deterministic test that runs discovery on `pr_pipeline`, so that they see `open`, `edit` and `render` reported at their `add_parser` lines without any model call.
- pr1-reviewer wants fixture tests for each discovery kind, the trace gate, the scope filter and cap, the e2e search and the schema, so that each requirement in "Done means" has a named test they can point to in the test run.
- pr1-reviewer wants the full repository CI suite to stay green, with no files changed outside `plugins/multiplai-dev/lib/code_facts/`, so that they can confirm from the diff and the CI run that no skill's behaviour changed.
- pr1-reviewer wants a one-off run against PR #295 recorded in the PR description, so that they can read how the capability behaves on a real change before PRs 2 to 4 depend on it.