# codebase-walkthrough scripts — orientation

A pipeline that explains one module, a whole tool or plugin (`run --scope codebase`), or one change (`run --scope pr`: a PR or a
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
| `__main__.py` | CLI: `run` (`--scope module|codebase|pr`, `--lib` (codebase only), `--depth` defaulting to `overview` for codebase and `standard` otherwise, `--pr`, `--range`, `--repo`, `--fetch`, `--expect-head`, `--commit`; a flag given with the wrong scope is an argparse error), `resume` (`--rewrite`: back to the stage before `write`, dropping its sections, answers and drops, then write and render again), `check`, `render` (rewrites a finished run's four files from its state; no agent, no ledger). Exit codes: 0 done, 1 error/untrusted/stop, 2 budget stop. |
| `models.py` | Pydantic models and `STAGES`. `WalkState` is the checkpoint. |
| `target.py` | Resolves the target, lists its files, snapshots a repo with `git archive` (no `.env*`, binaries or files over 2 MB). `git()` runs only `rev-parse`, `show`, `archive`, `ls-files`, `ls-tree`, `diff`, `merge-base`, `log`, `grep`; `resolve_change` resolves `--pr`/`--range` to shas with `lib/git_targets` before any run directory exists, and `build_change_target` lists the changed files and snapshots the base side under `<key>@base` (`snapshot_paths`); `tree_files()` is the `ls-tree` list `review-viewer serve --tree` shows; `resolve_libs` resolves each `--lib` to its repository, subpath and sha before any run directory exists. |
| `change.py` | A change's scope, code only: `changed_definitions` (Python functions and classes holding an added line), `change_symbols`, `change_spans` (each hunk at head and at base, five lines around), `change_units` (a file's spans stay in one unit). |
| `repos.py` | Finds repos under the search root (depth 4); the default root is the nearest ancestor holding several repos. |
| `symbols.py` | Python `ast` over the target: functions, classes, urls, celery tasks, models, outbound HTTP paths, settings, env vars, cache keys. |
| `references.py` | `rg` picks candidate files (intersected with `git ls-files`); `ast`, `ast-grep` or whole-identifier SQL matching confirms each hit, reading the file at HEAD. For a change, `scan_callers` replaces all of it: `git grep` at each repo's commit picks the files naming a changed function or class, `ast` confirms each call or `from … import` of it. |
| `lsp.py` | `pyright-langserver` references inside the target's own repo, one request at a time (pyright cancels an in-flight one), under one time limit. |
| `gates.py` | Every gate. Must never import or call `sdk`. `citation_gate` checks `split_path` itself, then calls `multiplai_core.citations.check_quote`; `file_at_commit`/`lines_at_commit` wrap core `file_at`/`lines_at`, whose cache is `gates.file_at.cache_clear()`. |
| `partition.py` | Splits the target into units of at most `--unit-lines`; a directory stays whole when it fits. |
| `docs.py`, `fetcher.py`, `netguard.py`, `untrusted.py` | `llms.txt`, page choice, fetch with SSRF and host checks on every redirect, fencing; local docs; the API collection (names, methods, URLs, header names; never `.env*` or `secret` fields). `fetcher.py` is a trimmed copy of deep-research's without trafilatura. |
| `stages/` | `explore`, `survey`, `core`, `docs`, `trace`, `write`, `page`. `survey` (codebase scope only; a no-op otherwise) asks two agents one question each across the whole snapshot (loops, lifecycle), with the target's own `SKILL.md`, `README.md`, `CHANGELOG.md` and `CLAUDE.md` and the `--lib` directories listed, and gates their answers like explore's under `survey:<question>` keys; `write.CODEBASE_SECTIONS` adds the three tables of `write.CODEBASE_TABLES`. `core` (human mode, module or codebase) asks one no-tools call for the part that does the main job (`CoreChoice`, `core_gate`, one re-ask; answers in `state.core_answers`), then one read-only agent reads its files under `explore_answers["core:read"]`, gated like explore's; `state.core` gives the write stage its section. `page` (human mode, module or codebase) asks one no-tools call for the HTML page's open sections (`PageOutput`, `page_gate`, a 1,200-word limit outside `<details>`, one re-ask; answers in `state.page_answers`) and saves what passes as `state.page`. Each gates its agents' answers and does one re-ask for what failed. `trace` hands the whole module to `code_facts.map_paths`, which finds the entry points, traces and gates each path; `render.render_paths_section` writes the Paths section from its result. |
| `prompts.py` | One function per agent task; shared rule blocks. |
| `render.py` | Expands `[[cN]]` / `[[snippet:cN]]` into `path:line` links and snippets from the commit; draws Mermaid from verified data; writes the Markdown and the coverage table. In human mode `human_markdown()` writes the sections of `stages/write.HUMAN_SECTIONS` (the structured ones from each `Section.data`, the flow's Mermaid from its gated boxes and arrows, under What it is's paragraphs), and `render_html()` writes `<name>-walkthrough.html` with `templates/walkthrough.html`, one scrolling page. When `state.page` is set, each open section is the page writer's HTML (`page_section_html`: `{{flow}}` and `{{flow:core}}` become `flow_picture`'s typed boxes, `[[snippet:]]` the cited lines, citations links grouped by `group_cites` and thinned by `first_cites_only`), wrapped in `<div class="page-body">`, and its CSS goes into the template's `{{PAGE_CSS}}` nested in `.page-body { … }`; otherwise the structured sections are drawn from their data and the rest from the same Markdown through `markdown_html()` (each citation link becomes a numbered `<sup><a class="cite">` with `path:line — quote` as its title, a tag Python-Markdown did not write is shown as text). The sections of `write.APPENDIX_KEYS` (the codebase tables, Reference, How this was made) are closed `<details class="appendix">` at the bottom, and `hide_locations()` turning any `path:line` left in the visible text into the file name with the location in a tooltip. `write_paths_output()` writes `<name>-paths.json` (paths.v1) with `code_facts.write_paths_file`, human mode only. No JSON for the review-viewer: its Summary tab links the HTML page. |
| `design.py` | `--design-system-dir`: `check()` holds the folder to `design_system.check_folder` (the shared `lib/design_system` contract) before `prepare` and at the top of `resume`, so a bad folder costs nothing; `css()` gives `render_html` the folder's `fonts.css` and `tokens.css`, else the neutral design system's (`design_system.NEUTRAL`), the one theme the plugin ships. The path is saved in `state.options.design_system_dir`; `render --design-system-dir` overrides it without saving. |
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
| `flow_gate` | each box cites a known id with a plain label, each arrow joins drawn boxes, at most 14 boxes, one connected graph; a box's `does` line (80 characters), `reads`/`writes` entries or `fan_out` that fails is dropped alone, and a fan-out number must be in the cited quote or the corpus; for a module or a codebase (`steps_follow_boxes`), each box that is not an input or an output has exactly one step, in the order of the boxes |
| `title_gate` | a use case's title, a step's title or an output's name holds no citation |
| `core_gate` / `core_part_gate` | the core choice names only files of the target, cites known ids, its title is plain (no citation, path or backticks, at most 60 characters) and its job passes `explainer_text_gate`; each part of the core section cites a known id and its text passes |
| `page_gate` | the page writer's sections are exactly the open keys in order; `{{flow}}` is in the summary once and nowhere else, `{{flow:core}}` only with a core flow; every `[[id]]` and `[[snippet:id]]` is a known citation, none inside `<h3>`, `<h4>` or `<summary>`; every tag and attribute is on the allow-list (`href` only as `#…`, no `style`, no `on*`, no comment); the CSS passes `css_gate` (no `url(`, `@import`, `expression(`, `</`, colour literal or unbalanced brace); every `<code>` text is in the corpus or the labels; no `path:line` typed in the text. Parsed with `html.parser`. `page_words` counts the words outside `<details>` |
| `output_gate` / `payload_gate` | the item cites a known id; every key of a JSON or YAML example (or a key or identifier of a Markdown or text one) is in the cited quote or the corpus, else the example alone is dropped |
| `config_gate` | the row cites a known id and its name is in the cited quote or the corpus; a default that is not is dropped |
| `loop_gate` | the row cites a known id and its text passes; a cap whose numbers are not in the cited quote, or whose names are in neither the quote nor the corpus, is dropped and the row kept |
| `exit_gate` | the row cites a known id, its text passes, and every number of its signal is in the cited quote and every name in the quote or the corpus |
| `resume_gate` | the row cites a known id and its text passes `explainer_text_gate` |
| `sections_gate` | the output has every required H2 |

## Invariants

- Agents get `Read`/`Grep`/`Glob` scoped to `<run>/snap` or `<run>/docs-cache`; write agents, the core choice and the page writer get no tools. No agent gets Bash or web tools.
- The page writer's HTML reaches a file only after `page_gate` passes it; a page that fails is never written, and code draws the page instead.
- No stage or test calls a vendor API. Only the `--docs` host and URLs in its `llms.txt` are fetched.
- Nothing writes to a target repo.
- Logs and `progress.log` never carry prompt text.
