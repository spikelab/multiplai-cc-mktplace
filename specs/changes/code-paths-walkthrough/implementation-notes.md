# Implementation notes: code-paths-walkthrough

Where the implementation departs from `design.md` or `tasks.md`, and why. Each
item names the code that carries it.

## Against the real code

- **No `ctx.snapshot_dir` or `ctx.repo_resolver`.** `RunContext` has
  `snap_root`, so `run_trace` passes `root=ctx.snap_root / state.target.repo_key`.
  The resolver is `stages/trace.py:_resolver`: it takes the repo-relative paths
  `map_paths` cites and resolves them in the target repo at its commit. The
  walkthrough's `gates.repo_resolver` takes workspace paths, so it is not reused.
- **`options` is a dict.** `cap=int(state.options.get("scenarios") or 21)`.
- **`PathsResult.dropped`, not `.drops`.** Everything that counts drops reads `dropped`.
- **Drop stages and reasons are code_facts' own.** An over-cap drop has stage
  `cap`, reason `over_cap`; the trace limit is stage `trace_limit`, reason
  `over_trace_limit`; unreachable is stage `import_reachability`. The Left out
  table prints them verbatim, as the design says, so these are what a reader sees.
- **`map_paths` runs its model fallback by default.** It is left on; its calls
  go through the same `_trace_call` and are counted under the `trace` label.
- **`RepoTrustError` subclasses `AgentCallError`.** `map_paths` gets
  `recoverable=(sdk.AgentCallError,)`, which on its own would turn a trust stop
  into a `call_failed` drop. The fatal latch (`_FatalLatch`) is what keeps it a
  stop; `test_a_trust_stop_inside_a_call_is_not_turned_into_a_drop` pins it.
  A trust or budget stop raised outside a call (the resolver) is re-raised by
  `run_trace` directly.
- **`_trace_call(ctx, root, latch)`** takes the snapshot root as well as the
  context, because `RunContext` has no target repo key. It forces
  `budget_label="trace"`, overriding code_facts' per-key `paths:trace:<key>`
  label, so all trace spend lands under one ledger label.
- **Django routes have no method.** Web seed names are `HTTP <route>`,
  `<other repo>: HTTP <route>` and `<task> (beat)`, not `<METHOD> <path>`.
- **Seed location fallback.** Routes and tasks are only collected from the
  module's own files, so a route's registration line is always under the root.
  The fallback order is: the handler named by the route's view (a function or
  class in the module, matched by name and, when ambiguous, by module file
  stem); else the registration line. A beat entry whose task is not defined in
  the module is seeded at the beat entry's line, when that is in the target repo.
  The middle fallback ("the call line into the module") has no source in the
  boundary data and is not built.
- **Known places** are: the target-repo side of each cross-repository reference
  (the route an `http-route` reference names, the task a `celery-name` one
  names), every route, every task, and every beat entry in the target repo. Same-
  repo imports are not known places, so a repository with only a CLI passes
  `places=[]`.
- **Write gate regex not narrowed.** `gates.CITE_ID` already accepts any
  lower-case id, including `p1h2`. The proposed `c\d+|f\d+|p\d+h\d+` would
  reject the existing `d<N>` ids of docs disagreements, so no `CITATION_ID` was
  added; an unknown hop id is rejected by the existing "is not a gated
  citation" rule.
- **`citation_index` returns `Citation`s for hops** (repo-key-prefixed path), not
  `PathHop`s, because `Linker` and `write_gate` take `Citation`s.
- **Per-path writer sections.** The plan keeps "one or two paragraphs per path
  from the checked hops". Each path gets a writer spec `path:p<n>` whose material
  is `hop_prompt_block` for that path; its paragraphs render under the path's
  heading, before the Entry bullet. The rest of the block is rendered by code.
- **Left out Entry cell.** A drop's Entry is `` `<repo>/<path>:<line>` (`<entry name>`) ``:
  the location alone does not say which entry was dropped. Still plain text,
  never a link. A `ListingSkip` has no test id, so its Entry is the test file.
- **Lead and progress wording.** `1 entry was left out` in the singular. The
  stage summary goes through the orchestrator's existing format, so the
  progress line reads `trace done: <N> paths, <C> cut short, <M> left out`; a
  stage failure logs `trace: failed (<ExceptionType>)` and records `failed 1`.
- **`paths_error` wins in the lead.** When it is set, the Paths body is
  `Path finding failed: …` whatever `paths` holds.
- **Legacy-state rewind** (`rewind_legacy_trace`) runs in `orchestrator.resume`
  for every saved stage past `trace`, including `done`, and rewinds to `docs`.
- **`paths.json` position in the outputs.** It is appended after
  `<name>-walkthrough.json`, so the Markdown line is still printed first.

## Tests changed outside this change's own files

- `lib/code_facts/tests/test_paths_models.py`: two tests compared `PathHop`
  with the walkthrough's `Hop` model field by field. This change deletes that
  model, so the parity tests were replaced by a pinned `PathHop` field order
  and the same defaults and constraints checks.
- `review-viewer/scripts/tests/fixtures/module-walkthrough.json` is regenerated
  from `run` output (`WALKTHROUGH_WRITE_VIEWER_FIXTURE=1`), as its test asks.
  No review-viewer code changed.
- `test_pipeline.py::test_walkthrough_content`: the old `urls.py:6` link came
  from scenario hop 1 at the route line. Seeds now start at the handler, so the
  restored assertion checks the webhook and beat seeds are traced, with links to
  `views.py:5` and `tasks.py:5`.
