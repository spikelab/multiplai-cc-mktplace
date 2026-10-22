# codebase-walkthrough scripts — orientation

A pipeline that explains one module, or one change (`run --scope pr`: a PR or a
commit range, read at its head against the merge-base). Code finds what the module defines, who
refers to it in every repository nearby, and what the vendor's docs say about
the APIs it calls. Agents read and explain; gates written in Python re-read
the commit with `git show` and drop every claim whose quote is not at the cited
lines. No gate calls a model.

```bash
uv run --directory plugins/multiplai-dev/skills/codebase-walkthrough/scripts python -m walkthrough_pipeline --help
uv run --directory plugins/multiplai-dev/skills/codebase-walkthrough/scripts python -m pytest tests/ -q
# CI form (needs rg and ast-grep on PATH; pyright-langserver is optional)
cd plugins/multiplai-dev/skills/codebase-walkthrough/scripts && \
  uv run --project ../../../../.. --package walkthrough-pipeline --extra dev python -m pytest tests/ -q
```

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `run` (`--scope module|pr`, `--pr`, `--range`, `--repo`, `--fetch`, `--expect-head`, `--commit`; a flag given with the wrong scope is an argparse error), `resume` (`--rewrite`: back to the stage before `write`, dropping its sections, answers and drops, then write and render again), `check`, `render` (rewrites a finished run's four files from its state; no agent, no ledger). Exit codes: 0 done, 1 error/untrusted/stop, 2 budget stop. |
| `models.py` | Pydantic models and `STAGES`. `WalkState` is the checkpoint. |
| `target.py` | Resolves the target, lists its files, snapshots a repo with `git archive` (no `.env*`, binaries or files over 2 MB). `git()` runs only `rev-parse`, `show`, `archive`, `ls-files`, `ls-tree`, `diff`, `merge-base`, `log`, `grep`; `resolve_change` resolves `--pr`/`--range` to shas with `lib/git_targets` before any run directory exists, and `build_change_target` lists the changed files and snapshots the base side under `<key>@base` (`snapshot_paths`); `tree_files()` is the `ls-tree` list `review-viewer serve --tree` shows. |
| `change.py` | A change's scope, code only: `changed_definitions` (Python functions and classes holding an added line), `change_symbols`, `change_spans` (each hunk at head and at base, five lines around), `change_units` (a file's spans stay in one unit). |
| `repos.py` | Finds repos under the search root (depth 4); the default root is the nearest ancestor holding several repos. |
| `symbols.py` | Python `ast` over the target: functions, classes, urls, celery tasks, models, outbound HTTP paths, settings, env vars, cache keys. |
| `references.py` | `rg` picks candidate files (intersected with `git ls-files`); `ast`, `ast-grep` or whole-identifier SQL matching confirms each hit, reading the file at HEAD. For a change, `scan_callers` replaces all of it: `git grep` at each repo's commit picks the files naming a changed function or class, `ast` confirms each call or `from … import` of it. |
| `lsp.py` | `pyright-langserver` references inside the target's own repo, one request at a time (pyright cancels an in-flight one), under one time limit. |
| `gates.py` | Every gate. Must never import or call `sdk`. `citation_gate` checks `split_path` itself, then calls `multiplai_core.citations.check_quote`; `file_at_commit`/`lines_at_commit` wrap core `file_at`/`lines_at`, whose cache is `gates.file_at.cache_clear()`. |
| `partition.py` | Splits the target into units of at most `--unit-lines`; a directory stays whole when it fits. |
| `docs.py`, `fetcher.py`, `netguard.py`, `untrusted.py` | `llms.txt`, page choice, fetch with SSRF and host checks on every redirect, fencing; local docs; the API collection (names, methods, URLs, header names; never `.env*` or `secret` fields). `fetcher.py` is a trimmed copy of deep-research's without trafilatura. |
| `stages/` | `explore`, `docs`, `trace`, `write`. Each gates its agents' answers and does one re-ask for what failed. `trace` hands the whole module to `code_facts.map_paths`, which finds the entry points, traces and gates each path; `render.render_paths_section` writes the Paths section from its result. |
| `prompts.py` | One function per agent task; shared rule blocks. |
| `render.py` | Expands `[[cN]]` / `[[snippet:cN]]` into `path:line` links and snippets from the commit; draws Mermaid from verified data; writes the Markdown and the coverage table. In human mode `human_markdown()` writes the eight sections of `stages/write.HUMAN_SECTIONS` (the structured ones from each `Section.data`, the flow's Mermaid from its gated boxes and arrows), and `render_html()` writes `<name>-walkthrough.html` with `templates/walkthrough.html`, one scrolling page: the structured sections from their data, the rest from the same Markdown through `markdown_html()` (each citation link becomes a numbered `<sup><a class="cite">` with `path:line — quote` as its title, a tag Python-Markdown did not write is shown as text), Reference and How this was made as closed `<details>`, and `hide_locations()` turning any `path:line` left in the visible text into the file name with the location in a tooltip. `render_walkthrough_json()` also writes `<name>-walkthrough.json` for the review-viewer (v1, `review-viewer/schema/walkthrough.v1.schema.json`): base is git's empty tree, one step per H2, and per H3 of Reference, anchored on its links into the module directory (How this was made, the commits and coverage tables, and a section with no such link, go in `overview_md`), every other file under it in `skipped`. `write_paths_output()` writes `<name>-paths.json` (paths.v1) with `code_facts.write_paths_file`, human mode only. `review-viewer/scripts/tests/fixtures/module-walkthrough.json` is a copy the viewer's suite checks; `test_the_viewer_fixture_is_what_run_writes` fails when it drifts. |
| `check.py` | Re-checks a finished walkthrough's links and snippets against the commits in its header. A link on a line with a hop anchor (`<a id="p2h3"></a>`) fails under that hop's id; a `[pNhK](#pNhK)` with no anchor fails; the hop count goes to stderr so the stdout line stays as buildme parses it. |
| `orchestrator.py` | Runs the code stages and agent stages in `STAGES` order, saving state after each; `resume`. |
| `sdk.py`, `budget.py`, `state.py`, `progress.py`, `config.py` | The same shape as review_pipeline's, on multiplai-core's primitives: trust gate, tool allow-list and the one-reformat call loop (answer parsing from `multiplai_core.structured`); `WalkthroughBudget`, a `multiplai_core.budget.SpendLedger` with a $50 default; atomic checkpoint (`multiplai_core.checkpoint`); `progress.log` (`multiplai_core.progress.ProgressWriter` plus the walkthrough's `STARTED` line); a new run on the same target moves the earlier run's log to `progress-<UTC time>.log` instead of deleting it; `walkthrough.yaml` > `multiplai.conf` > session model, read with `multiplai_core.stage_config`. |

## The gates

| Gate | Passes when |
|---|---|
| `citation_gate` | the repo key is known, the path exists at the commit, and the normalised quote is inside the cited lines |
| `boundary_gate` | each symbol and reference passes `citation_gate` |
| `partition_gate` | every target file is in exactly one unit and no unit is over the limit unless it is a single oversize file |
| `file_coverage_gate` / `boundary_coverage_gate` | every file over 30 lines, and every boundary entry, has a surviving fact citing it |
| `doc_quote_gate` | the page is one that was fetched and the normalised quote is in it |
| `write_gate` | every citation id is known, every `path:line` resolves, every backticked identifier is in the corpus, every Mermaid label is a known name |
| `glossary_gate` | the term appears in the repos |
| `explainer_text_gate` | `write_gate`, and no `path:line` typed in the text even when gated, and no code block |
| `usecase_gate` | the case cites a known id, its text passes `explainer_text_gate`, and every flag and command word of its example is in a cited quote or the corpus |
| `flow_gate` | each box cites a known id with a plain label, each arrow joins drawn boxes, at most 14 boxes, one connected graph |
| `output_gate` / `payload_gate` | the item cites a known id; every key of a JSON or YAML example (or a key or identifier of a Markdown or text one) is in the cited quote or the corpus, else the example alone is dropped |
| `config_gate` | the row cites a known id and its name is in the cited quote or the corpus; a default that is not is dropped |
| `sections_gate` | the output has every required H2 |

## Invariants

- Agents get `Read`/`Grep`/`Glob` scoped to `<run>/snap` or `<run>/docs-cache`; write agents get no tools. No agent gets Bash or web tools.
- No stage or test calls a vendor API. Only the `--docs` host and URLs in its `llms.txt` are fetched.
- Nothing writes to a target repo.
- Logs and `progress.log` never carry prompt text.
