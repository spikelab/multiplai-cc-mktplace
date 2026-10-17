# Use cases — who this change is for

## Personas

### Priya, reading a walkthrough of an unfamiliar CLI or skill module
- **Who they are:** A developer who has just joined a repository whose features are CLI commands, scripts, skill commands and hooks rather than web routes. She opens the generated `<name>-walkthrough.md` to learn the module before changing it.
- **The job they are doing:** Finding out which features reach the module, how each one gets there hop by hop, and who or what triggers it.
- **The constraint they are under:** She cannot read the whole codebase. Today's walkthrough of her repository traces nothing, because only web entry points are found, so the walkthrough is the only map she has and it is empty where she needs it.

### Tomás, deciding where end-to-end tests are missing
- **Who they are:** An engineer responsible for the test coverage of a module that he is about to change or ship.
- **The job they are doing:** Working out which entry-to-module paths an end-to-end test actually exercises, and which have none, so he can write the missing tests.
- **The constraint they are under:** He has limited time to write tests and needs to see, per path, which tests count. Tests that mock the next hop look like coverage but do not count, and he cannot tell them apart without reading every test.

### Dana, maintaining a web service that already has walkthroughs
- **Who they are:** A maintainer of a web repository whose walkthroughs today trace webhook routes, cross-repo routes and Celery beat entries. Walkthroughs with an "End-to-end scenarios" section are committed under `docs/walkthroughs/`, and buildme uses `check --at-head` to flag stale ones.
- **The job they are doing:** Regenerating walkthroughs after the upgrade, and keeping the committed ones valid until she does.
- **The constraint they are under:** She cannot afford to lose any path the old walkthrough showed. She also cannot regenerate every committed walkthrough at once, so the old files must keep passing the check while their cited lines have not moved.

### Sam, running the walkthrough pipeline
- **Who they are:** The person who runs `codebase-walkthrough` on a repository, in human mode for readers or in agent mode for automation, and who watches spend.
- **The job they are doing:** Getting a complete walkthrough out of one run, or out of a resumed run when a run stops partway.
- **The constraint they are under:** Each run is limited by the `--max-usd` breaker ($50 by default). Up to 21 paths instead of 6 means more model calls, so a resume must not pay again for answers it already has. He needs to know why an entry was left out without opening the run's state.

### Lee, building tools that read walkthrough outputs
- **Who they are:** A developer who owns tools that read the walkthrough's outputs, such as review-viewer reading `<name>-walkthrough.json`, and who will move later tools onto the shared path format.
- **The job they are doing:** Keeping existing readers working through this change, and getting a machine-readable list of paths to build on.
- **The constraint they are under:** Review and review-viewer are not to be changed in this PR. Any change in the shape of the existing sidecar JSON would break his tools without warning.

## Use cases

- Priya wants the walkthrough of a CLI, script, skill or hook module to list the entry points that reach it so that she sees a Paths section naming each command, script, skill command or hook, its kind, and who invokes it, instead of no traced features at all.
- Priya wants each path's hops shown as `path:line` links so that she can click from the entry point through each hop to the code in the module.
- Priya wants the walkthrough to say where a trace broke and why so that she can tell a path that stops partway from one that reaches the module.
- Priya wants the prose about each path to cite hops by id (`p{n}h{k}`) so that she can match every sentence to the hop it describes.
- Priya wants the Paths section to appear even when nothing reaches the module so that she reads an explicit statement such as "no entry point reaches this module", or that path finding failed and why, instead of finding the section missing.
- Tomás wants each path to list its e2e tests so that he can read, under "E2E tests:", which tests count for that path.
- Tomás wants tests that mock the next hop listed separately as not counted so that he does not mistake them for real end-to-end coverage.
- Tomás wants paths with no test marked plainly so that he can scan the Paths section for "no e2e test" and get his list of tests to write.
- Dana wants every web entry the old walkthrough traced to still be traced so that her regenerated walkthrough shows each webhook route, cross-repo route and Celery beat entry it showed before, or lists it in the coverage table with a reason.
- Dana wants her committed walkthroughs with an "End-to-end scenarios" section to keep passing `check --at-head` so that buildme does not flag them as stale while their cited lines have not moved.
- Dana wants `check --at-head` to re-check the hops in regenerated Paths sections so that buildme flags a new walkthrough as stale when a cited path hop's lines move.
- Sam wants `--scenarios` to cap the number of paths, with a default of 21, so that `--help` tells him it caps paths and a run without the flag traces up to 21 of them.
- Sam wants every entry left out to appear in the coverage table with its stage and reason so that he can see why an entry is missing, for example `seed_not_found`, `unreachable` or `over_cap`, without opening the run's state.
- Sam wants a trace that fails for one entry to show up as a drop rather than stop the run so that he still gets a finished walkthrough, with that entry listed in coverage.
- Sam wants a resumed run to ask only for the answers it is missing so that the resume costs less than the first run and finishes inside the `--max-usd` limit.
- Sam wants the run to stop with a checkpoint when the `--max-usd` breaker trips so that spend never exceeds his limit and he can resume afterwards.
- Sam wants agent-mode runs to behave as they do today so that agent-mode output has no Paths section and no `paths.json`, and costs no more than before.
- Lee wants `<name>-walkthrough.json` to keep its v1 shape so that review-viewer keeps reading it unchanged, with the only difference being a step titled "Paths" instead of "End-to-end scenarios".
- Lee wants a `<name>-paths.json` file beside the walkthrough so that his tools can read every path, hop, break reason and e2e test from a file that validates against code_facts' `paths.v1` schema.