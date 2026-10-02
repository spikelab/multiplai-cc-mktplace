# codebase-walkthrough scripts — orientation

A pipeline that explains one module. Code finds what the module defines, who
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
| `__main__.py` | CLI: `run`, `resume`, `check`. Exit codes: 0 done, 1 error/untrusted/stop, 2 budget stop. |
| `models.py` | Pydantic models and `STAGES`. `WalkState` is the checkpoint. |
| `target.py` | Resolves the target, lists its files, snapshots a repo with `git archive` (no `.env*`, binaries or files over 2 MB). `git()` runs only `rev-parse`, `show`, `archive`, `ls-files`. |
| `repos.py` | Finds repos under the search root (depth 4); the default root is the nearest ancestor holding several repos. |
| `symbols.py` | Python `ast` over the target: functions, classes, urls, celery tasks, models, outbound HTTP paths, settings, env vars, cache keys. |
| `references.py` | `rg` picks candidate files (intersected with `git ls-files`); `ast`, `ast-grep` or whole-identifier SQL matching confirms each hit, reading the file at HEAD. |
| `lsp.py` | `pyright-langserver` references inside the target's own repo, one request at a time (pyright cancels an in-flight one), under one time limit. |
| `gates.py` | Every gate. Must never import or call `sdk`. |
| `partition.py` | Splits the target into units of at most `--unit-lines`; a directory stays whole when it fits. |
| `docs.py`, `fetcher.py`, `netguard.py`, `untrusted.py` | `llms.txt`, page choice, fetch with SSRF and host checks on every redirect, fencing; local docs; the API collection (names, methods, URLs, header names; never `.env*` or `secret` fields). `fetcher.py` is a trimmed copy of deep-research's without trafilatura. |
| `stages/` | `explore`, `docs`, `trace`, `write`. Each gates its agents' answers and does one re-ask for what failed. |
| `prompts.py` | One function per agent task; shared rule blocks. |
| `render.py` | Expands `[[cN]]` / `[[snippet:cN]]` into `path:line` links and snippets from the commit; draws Mermaid from verified data; writes the Markdown, the HTML and the coverage table. |
| `check.py` | Re-checks a finished walkthrough's links and snippets against the commits in its header. |
| `orchestrator.py` | Runs the code stages and agent stages in `STAGES` order, saving state after each; `resume`. |
| `sdk.py`, `budget.py`, `state.py`, `progress.py`, `config.py` | Copied from review_pipeline: trust gate and tool allow-list, budget breaker, atomic checkpoint, `progress.log`, `walkthrough.yaml` > `multiplai.conf` > session model. |

## The gates

| Gate | Passes when |
|---|---|
| `citation_gate` | the repo key is known, the path exists at the commit, and the normalised quote is inside the cited lines |
| `boundary_gate` | each symbol and reference passes `citation_gate` |
| `partition_gate` | every target file is in exactly one unit and no unit is over the limit unless it is a single oversize file |
| `file_coverage_gate` / `boundary_coverage_gate` | every file over 30 lines, and every boundary entry, has a surviving fact citing it |
| `doc_quote_gate` | the page is one that was fetched and the normalised quote is in it |
| `trace_gate` | hop 1 is the seed; each next hop's lines define or hold the name the previous hop called, or hold a boundary entry |
| `write_gate` | every citation id is known, every `path:line` resolves, every backticked identifier is in the corpus, every Mermaid label is a known name |
| `glossary_gate` | the term appears in the repos |
| `sections_gate` | the output has every required H2 |

## Invariants

- Agents get `Read`/`Grep`/`Glob` scoped to `<run>/snap` or `<run>/docs-cache`; write agents get no tools. No agent gets Bash or web tools.
- No stage or test calls a vendor API. Only the `--docs` host and URLs in its `llms.txt` are fetched.
- Nothing writes to a target repo.
- Logs and `progress.log` never carry prompt text.
