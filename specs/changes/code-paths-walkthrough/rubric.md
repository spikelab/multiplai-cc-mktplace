# Evaluation Rubric

## Code Architecture (weight: 2)

Score against the project's documented standards (CONTEXT.md, CONVENTIONS.md, ADRs, style guides) where they exist. Only where none exist, use the twelve-smell baseline: Mysterious Name, Duplicated Code, Feature Envy, Data Clumps, Primitive Obsession, Repeated Switches, Shotgun Surgery, Divergent Change, Speculative Generality, Message Chains, Middle Man, Refused Bequest. Treat each smell as a judgment call, phrased as "possible X", and never as pass/fail on its own. A documented repo standard always overrides the baseline. Skip anything the formatter, linter or type checker already enforces.

Change-specific structural expectations:
- `stages/trace.py` delegates path finding entirely to `code_facts.map_paths` and does not import `orchestrator`.
- `pick_seeds`, the walkthrough `trace_gate`, `prompts.trace_scenario`, the scenario answer models, `s<N>h<M>` id generation, and the `trace_answers` / `scenarios` fields are actually deleted, not left as dead code.
- The Paths section body comes only from `render_paths_section`, built from `paths_lead`, `render_stop_line`, `render_e2e_lines` and `path_hop_id`. It is never produced by a writer agent.
- `p{n}h{k}` formatting lives in one place (`path_hop_id`) and is reused by render, `citation_index`, `hop_ids_block` and check. It is not re-derived with ad hoc f-strings.
- No walkthrough code builds `paths.v1` JSON. `code_facts.write_paths_file` is the only serializer.
- `check.py` keeps the existing link verifier as the only verifier and extends its section set to {"End-to-end scenarios", "Paths"}. It does not add a parallel hop verifier.
- `CoverageRow` is a typed model in `models.py`, not a bare tuple or dict. Cell escaping reuses `render._cell`, and invoker escaping reuses the existing Markdown escaping.
- `ENTRY_KIND_LABELS` is a single dispatch table, not if-chains repeated across modules.
- `rewind_trace` lives in `orchestrator.py`. Cache deletion is in `prepare` only and is never reachable from `resume`.

| Score | Criteria |
|-------|----------|
| 5 | Every structural expectation above holds. The trace stage is a thin adapter over `map_paths` (it builds the arguments, catches errors and stores state). Hop-id, escaping and serialization logic each have one owner. Legacy scenario machinery is fully removed, with no orphaned imports or dead branches. New helpers have clear names and narrow, typed signatures. Repo conventions are followed, or the baseline flags at most minor, defensible items. |
| 3 | The overall shape is right (`map_paths` is delegated to, the renderer owns the section, check reuses the verifier), but there are noticeable issues. Examples: hop-id formatting duplicated in two or three places, a leftover unused legacy helper or field, `trace.py` pulling in an orchestrator utility, coverage rows passed as dicts, or the agent/human mode branch repeated in several modules where one gate would do. Several possible smells exist (possible Duplicated Code, possible Primitive Obsession), but none compromise correctness. |
| 1 | The walkthrough keeps its own seed picking, trace gate or tracer next to `map_paths`, or it builds `paths.v1` JSON itself. `check.py` adds a second, divergent link verifier. Writer output can supply or override the Paths body. Legacy `s<N>h<M>` / `trace_answers` code paths remain live. Logic is scattered so that changing the hop-id format would need edits in many modules. Circular or inverted dependencies exist, such as `trace.py` importing `orchestrator`. |

## Test Quality (weight: 1)

Tests run with `uv run --project plugins/multiplai-dev/skills/codebase-walkthrough/scripts --package walkthrough-pipeline python -m pytest` and use real `tmp_path` git fixture repos (click CLI, skill command, web, 25-entry and 35-entry argparse). Fixtures must:
- use a fixed author and committer
- set `GIT_CONFIG_GLOBAL=os.devnull` and `GIT_CONFIG_NOSYSTEM=1`
- compare `.resolve()`d paths on both sides

Score down for these anti-patterns:
- Assertions that only interrogate mocks (`.called`, `assert_called_*`, call counts) without an observable-outcome assertion. Examples of outcomes: rendered Markdown text, files under `paths-cache/`, `state.errors` contents, `paths.json` schema validity, `check` report lines.
- Mock or fake setup that outweighs the assertions.
- Fakes for `sdk.agent_call_structured` or `map_paths` that don't honor the real contract. For example, not raising `RepoTrustError` as an `AgentCallError` subclass, returning shapes `PathsResult` wouldn't, or ignoring `budget_label` and cost accounting.
- Production methods or parameters added only so tests can reach internals.
- Fixed sleeps instead of condition polling.

| Score | Criteria |
|-------|----------|
| 5 | Tests check observable behavior end to end. The rendered `## Paths` text is checked line by line: lead sentence, entry line, hop anchors, stop line and E2E lines. The `paths-cache/` files on disk, the `--max-usd` ledger, the checkpoint round-trip, and the validity and consistency of `out/billing-paths.json` with `p{n}h{k}` citations are all asserted. Edge cases are covered: 0-byte, `"\n"`-only, CR-only, non-UTF-8 and >2 MiB files; `TimeoutError()` with an empty message; `BaseExceptionGroup` unwrapping; symlink and regular file at `paths-cache`; truncated cache file; `p10h1` vs `p2h1` ordering; zero-padded ids (`p01h02`, `ship1h2`). Check's failing-first tests (moved line, deleted file, truncated file, dangling `([p4h1](#p4h1))`) fail if Paths-section links are skipped. Recording fakes assert both inputs (`allowed_tools`, `cap`, `changed_lines` unchanged by deep copy) and resulting outputs. |
| 3 | The main scenarios have meaningful outcome assertions, but some edge cases are missing. Examples: no CR-only or non-UTF-8 test, no exception-group test, no truncated-cache test, or resume tests that count fake calls without checking the files written. A few tests rely mainly on call-count assertions. Fakes are mostly faithful, with minor contract shortcuts. |
| 1 | Tests mostly assert that mocks were called. Rendered output, files on disk, and `check` report contents are not inspected. Fixtures depend on the user's global git config or unresolved temp paths. Fakes violate the SDK or `code_facts` contracts, so error-path tests pass vacuously. Regression guards are mislabeled as failing-first, or the moved-hop test would still pass if Paths links were skipped. Sleeps or test-only production hooks are present. |

## Spec Compliance (weight: 3)

Check every WHEN/THEN scenario across `walkthrough-path-tracing`, `walkthrough-paths-section`, `walkthrough-paths-file` and `walkthrough-path-hop-check`, including the corrected scenarios.

Areas with the highest risk of failure:

**`map_paths` call**
- It is called exactly once per human-mode run.
- Its arguments are exactly: `root`, `commit`, `resolve`, `call=_trace_call`, `call_kwargs`, `changed_lines`, `seeds`, `places`, `cap=state.path_cap`, `trace_limit=30`, `cache_dir=run_dir/"paths-cache"`, `fallback=True`.

**`module_changed_lines`**
- It counts lines from bytes.
- 0-byte files are omitted.
- Keys are repo-relative POSIX paths.
- It returns `{}`, never `None`.

**`_trace_call`**
- It re-raises `RepoTrustError` and `BudgetExceededError` before the `AgentCallError` clause.
- It wraps any other `AgentCallError` as `_TraceCallFailed(first line[:200])`.

**Rendering**
- Header, entry and hop lines are byte-exact, including the `unknown` invoker.
- Stop line forms:
  - `` - Stops at `p{n}h{len}`: `<reason>` ``
  - `` - Stops at `p{n}h{len}`: trace complete ``
  - zero hops: `- Stops at entry: …`
- E2E lines use the counted and mocked formats. The `no e2e test` line appears whenever there are zero counted tests.
- `paths_lead` strings are exact for every case: paths present, left-out entries, zero paths, none found, fallback failed, and path finding failed.

**Coverage**
- `paths_coverage_rows` produces rows with the specified stage values (drop stage unchanged, `discovery`, `e2e_listing`, `seed`).
- `left_out_count` counts only drops and `seed_not_found` diagnostics.
- `seed_rows` are not counted.

**`--scenarios`**
- Default 21, `_positive_int`, and the exact help text.
- Invalid values exit with status 2 before a run directory is created.

**Whole-stage failure**
- `trace_error_message` produces the specified format.
- `BaseExceptionGroup` unwrapping re-raises trust, budget, cancel and interrupt exceptions.
- The run still writes and renders.

**Web seeds and places**
- `web_seeds` applies import-line placement and `seed_outside_root` rows.
- `known_places` returns de-duplicated, `(path, line)`-sorted places and skips edges with no target-side location.

**Cache and rewind**
- Cache deletion on a fresh run handles a missing directory, a regular file and a symlink.
- `rewind_trace` runs only under its conditions, clears write outputs and `trace:` errors, and never runs in agent mode.

**Citations**
- `citation_index` adds hop citations.
- `hop_ids_block` sits outside the 60,000-character truncation.
- Unknown `[[p9h1]]` is invalid, and a valid hop citation renders in the specified format.

**Paths file**
- It is written once, a stale file is removed on failure, and write errors go to `state.errors` without escaping `render`.
- Agent mode touches nothing.
- `walkthrough.json` keeps the v1 shape, with the step renamed `Paths`.

**`check --at-head`**
- It uses `HOP_ANCHOR_RE` and `HOP_REF_RE` exactly.
- Anchors are searched only in Paths.
- It reports `p{n}h{k} <key>/path:line: <kind>`, `unresolved hop citation {id}` and `hops checked: N`.
- It never reads `paths.json`, the run directory or the cache, and makes no model calls.

**Docs and release**
- SKILL.md is updated.
- multiplai-dev is bumped `0.80.0` → `0.81.0` with a CHANGELOG entry, so `check_changelog.py` passes.

| Score | Criteria |
|-------|----------|
| 5 | Every listed scenario is implemented and has a test, including all corrected scenarios and agent-mode negatives (no `map_paths`, no `paths-cache/`, no Paths heading, no `p\d+h\d+`, no paths file). Output strings match the spec exactly. The paths file and Markdown agree on path count, hop order and E2E `node_id`s. A fully cached resume produces a file identical to an uninterrupted run's. Docs and the version bump are present. |
| 3 | Core behavior works: `map_paths` delegation, the Paths section with hop ids, the paths file, and the check extension. But two to five scenarios are missing, untested or slightly off. Examples: lead wording drift, `seed_rows` wrongly counted as left out, stale-file removal missing, exception groups not unwrapped, the `Stops at entry` case unhandled, or the CHANGELOG/version bump missing. |
| 1 | Major requirements are missing or wrong. Examples: the Paths section can be omitted, hop ids are absent or non-deterministic, `check` skips Paths links, the paths file is not written or is hand-serialized, a whole-stage failure aborts the run, `RepoTrustError` or a budget stop is swallowed as a per-entry failure, agent mode is affected, or many scenarios lack tests. |

## Integration Coherence (weight: 2)

This dimension covers how the pipeline stages, persisted state, rendered artifacts and `check` fit together as one contract:

```
prepare → trace → write → render → paths.json → check --at-head
```

What to check:
- The same `PathsResult` drives every consumer: the Paths section, `citation_index`, `hop_ids_block`, `paths_coverage_rows` and `write_paths_file`. So path n, hop k means the same location everywhere.
- The hop line format emitted by the renderer is exactly what `HOP_ANCHOR_RE` and the existing link verifier parse.
- The rendered hop-citation format `([p{n}h{k}](#p{n}h{k}))` is exactly what `HOP_REF_RE` matches.
- Backticked ids in stop and mocked-test lines are deliberately not matched as references.
- `WalkState` fields (`paths`, `path_cap`, `paths_error`, `seed_rows`) survive checkpoint round-trips with types intact (`E2ETest` vs `MockedE2ETest`).
- Pre-change checkpoints load and rewind cleanly.
- `--scenarios` flows from CLI → `RunOptions` → `prepare` → `WalkState.path_cap` → `map_paths(cap=…)` as an int.
- `_trace_call` honors the `code_facts` contract: `**kw` passthrough, Read/Grep/Glob tools, a `paths:` budget label, and spend landing in the shared `--max-usd` ledger.
- Error states propagate consistently. A per-entry failure becomes a `call_failed` drop, a coverage row and a paths-file drop. A whole-stage failure sets `paths_error`, produces the failure lead and no hop ids, removes the stale paths file, and still passes `check`.
- Legacy "End-to-end scenarios" walkthroughs still check correctly. buildme's stale-walkthrough scan ignores `*-paths.json`.

| Score | Criteria |
|-------|----------|
| 5 | All producer/consumer formats line up exactly, and tests prove it by running render → check on the same fixtures. Both happy and failure paths stay consistent across state, Markdown, coverage table, paths file and check report. Resume and rewind preserve explore and docs answers while invalidating stale write answers. Legacy and agent-mode artifacts keep working unchanged. There is no hidden coupling: `check` stays independent of run artifacts. |
| 3 | Main flows connect correctly, but there are seams. Examples: an edge case where Markdown and paths file could disagree (such as re-render ordering or drop vs diagnostic placement), `seed_rows` lost on checkpoint round-trip, `path_cap` stored as a string in one path, or legacy checkpoint handling that works but replays stale write answers. Cross-stage tests exist only for the happy path. |
| 1 | Components disagree. Examples: rendered hop lines that `check` regexes don't match, citations resolving to different hops than the paths file lists, checkpoints that fail to load or lose types, `check` reading `paths.json` or the run directory, or failure states that leave the Markdown, coverage table and paths file contradicting each other. |

## Failure Handling & Determinism (weight: 1)

This dimension covers the change's many failure paths and its guarantees of reproducible output.

Exception classification and messages:
- `RepoTrustError` and `BudgetExceededError` (including inside exception groups), `CancelledError` and `KeyboardInterrupt` always propagate.
- Only `_TraceCallFailed` is recoverable per entry.
- Everything else in the trace stage becomes a recorded `paths_error`, and only the exception type is logged.
- `trace_error_message` never raises, including on empty messages.

File operations:
- A truncated cache file ends either as a cache miss or as a recorded error, never as a traceback.
- Paths-file `OSError` or `ValueError` and stale-file unlink errors are recorded in `state.errors`, and the Markdown is kept.
- A trust or budget stop writes no new paths file.
- Cache deletion never follows symlinks.

Robustness and determinism:
- `seed_outside_root` prevents `discover_entries` from raising `ValueError`.
- The renderer does not raise on zero-hop paths or unknown reasons.
- Ids are deterministic across re-renders and across identical paths.
- Ordering follows `map_paths` rank order, with no walkthrough-side filtering.
- `known_places` is sorted.
- `changed_lines` is not mutated.

| Score | Criteria |
|-------|----------|
| 5 | Every failure class is handled exactly as specified, with clear, bounded messages (first line, ≤200 characters). No exception escapes `render`. Fatal errors are never swallowed. Re-runs and resumes produce byte-identical outputs from identical inputs. Tests exercise each failure branch, including exception groups and filesystem edge cases. |
| 3 | The main failure paths are correct, but a few are rough. Examples: empty-message handling relies on luck, exception groups are not unwrapped, a symlinked cache is followed, or an unwritable target raises instead of being recorded in only one case. Output is deterministic in practice, but this is not tested. |
| 1 | `except Exception` swallows trust or budget errors, or `AgentCallError` is caught before `RepoTrustError`. Whole-stage failures crash the run. Corrupt cache files produce tracebacks. Paths-file errors abort rendering. Ids or ordering vary between renders. |