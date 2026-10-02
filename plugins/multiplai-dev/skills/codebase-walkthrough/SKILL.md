---
name: codebase-walkthrough
description: Explains a module end to end with a Python pipeline — every repository that calls it, everything it calls, its data stores and config, and the vendor's docs — and keeps only claims whose cited lines are at the commit. Writes a Markdown walkthrough and a step-through HTML page, or compact Markdown for coding agents.
when_to_use: 'Triggers: walk me through this module, explain this codebase, onboard me, codebase tour, walkthrough, /multiplai-dev:codebase-walkthrough'
model: opus
effort: medium
disable-model-invocation: true
---

# Codebase Walkthrough

Run a walkthrough as a pipeline: code finds and checks, agents read and write.

1. **target** — the module's directory at its repository's HEAD commit,
   snapshotted with `git archive`. Migrations are counted, not read.
2. **repos, symbols, references** — code finds every repository under the
   search root, lists what the module defines and calls (Python `ast`), and
   finds who refers to it in every repository: `rg` picks files, then Python
   `ast`, `ast-grep` (TypeScript/JavaScript) or whole-identifier matching
   (SQL) confirms each hit, and `pyright-langserver` adds references inside
   the module's own repository. Everything goes to `boundary.json`.
3. **explore** — one agent per slice of the module (at most `--unit-lines`
   lines) and one per group of boundary entries. Each returns claims with
   citations.
4. **docs** — code fetches the vendor's `llms.txt` and the pages it lists that
   name a resource the code calls, and maps each API path to a request in the
   API collection. One agent per resource links the docs to the code.
5. **trace** — one agent per entry point (webhooks first, then routes called
   from other repositories) follows the code to its last effect, hop by hop.
6. **write** — one agent per section, with no tools, writes from the checked
   claims only and refers to code by citation id. Code turns the ids into
   `path:line` links and inserts the code snippets from the commit.

Between the stages, gates written in Python re-read every cited line range
with `git show <sha>:<path>` and drop what is not there. A claim, a doc quote,
a trace hop or a paragraph that fails is dropped and counted in the coverage
table; a paragraph that fails twice is listed under "Cut by the checks". The
gates never ask a model.

## What this skill does on the machine

- **Sends the module's repository, and every repository that refers to it, to
  a model.** Agents read snapshots of those commits with `Read`, `Grep` and
  `Glob` only; the writers get no tools. No agent gets a shell, file edits, or
  web access. This is why it needs `--trust-repo`.
- **Reads git repositories** with `git rev-parse`, `git show`, `git archive`
  and `git ls-files` only. It never checks out, fetches, commits or writes to
  them. It runs `rg` in each repository's working tree to pick which files to
  parse; what it parses is the commit.
- **Never reads credentials.** `.env*` files are left out of every search and
  snapshot. From an API collection it reads request names, methods, URLs and
  header *names*, and from its environment files only the `server` value.
- **Uses the network** for the model calls and, with `--docs`, to fetch
  `<docs>/llms.txt` and pages it lists on the same host, each checked against
  private and local addresses. Fetched pages are stored inside
  `<untrusted-content>` fences and treated as data. **It never calls the
  vendor's API.**
- **Runs local programs**: `git`, `rg`, `ast-grep`, `pyright-langserver`.
- **Writes files** to the run directory, the output directory and the log
  files.

## Prerequisites

- **`uv`** (https://docs.astral.sh/uv/). The pipeline runs via `uv run`.
- **`git`** and **`rg`** (ripgrep).
- **`ast-grep`** for references from TypeScript/JavaScript (without it those
  are not found, and the coverage table does not list them).
- **`pyright-langserver`** (optional; without it the coverage table says LSP
  was skipped).
- **`--trust-repo`** (or `WALKTHROUGH_TRUST_REPO=1`) on every `run` and `resume`.

If `uv` is missing, tell the user: "this skill runs its pipeline with `uv`,
which is not installed; install it from https://docs.astral.sh/uv/ and re-run."
If the pipeline prints `Not run:` and exits 1, the repositories were not
marked trusted: ask the user whether they trust the module's repository and
the repositories next to it, and re-run with `--trust-repo` only on a yes.

## Arguments

| Arg | Meaning | Default |
|-----|---------|---------|
| target | Directory of the module, inside a git repository | required |
| `--depth` | `overview` (skips test files), `standard`, `deep` | `standard` |
| `--mode` | `human` (Markdown + HTML) or `agent` (compact Markdown for coding agents; no references, docs or scenarios) | `human` |
| `--name` | Base file name | the module directory, lower-cased |
| `--output` | Directory for the walkthrough files | `INBOX/` if it exists, else the current directory |
| `--search-root` / `--repos` | Where to look for references | the nearest ancestor holding several repositories |
| `--docs` | Vendor docs base URL with an `llms.txt` | none |
| `--docs-dir` | Local docs directory (repeatable) | a directory next to the repos named after the vendor |
| `--api-collection` | Bruno/OpenCollection directory | a directory next to the repos holding `opencollection.yml` |
| `--max-usd` | Budget circuit breaker | 50 |

## Steps

### 1. Start the run in the background

Use the Bash tool with `run_in_background: true`. Pass absolute paths.

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/codebase-walkthrough/scripts \
  python -m walkthrough_pipeline --session-id "{session_id}" \
  run /abs/path/to/module --trust-repo [--docs https://docs.vendor.example] [--output /abs/out]
```

The first stdout line, `run: <dir>`, is the run directory (state,
`boundary.json`, `progress.log`); tell the user. While the run is going, check
`progress.log` with a bounded loop, never a bare `tail -f`:

```bash
until grep -qE '^\[[^]]*\] (DONE|FAILED)' <run dir>/progress.log; do sleep 30; done; tail -5 <run dir>/progress.log
```

(run through the Monitor tool, or in the background with a timeout.) The
background task's own completion notice also ends the wait.

Exit codes: `0` done; `1` an error, an untrusted repository, `rg` missing
(the message names ripgrep and how to install it), or a stop
(`STOP (docs gate): …` when `llms.txt` does not answer 200 or lists no `.md`
pages). On a docs-gate stop, ask the user how to proceed, then resume with
their decision; the explore answers already paid for are kept:
`python -m walkthrough_pipeline resume <run dir> --trust-repo --docs <other url>`
or `… --no-docs`. `2` the budget circuit breaker stopped
the run at `--max-usd`. On `2`, report the spend and the checkpoint; resume
only if the user raises the ceiling:
`python -m walkthrough_pipeline resume <run dir> --trust-repo --max-usd <n>`.

### 2. Report

The run prints `walkthrough: <path>` for each file written. Read the
`## Coverage` section of the Markdown file and paste its table into chat as it
is, with the list of files that have no surviving fact. Then give the paths of
the Markdown and HTML files.

### 3. Check (any time later)

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/codebase-walkthrough/scripts \
  python -m walkthrough_pipeline check /abs/path/to/name-walkthrough.md [--at-head]
```

It re-checks every `path:line` link and snippet against the commits in the
file's header and prints `N checked, M failed`; it exits 1 on any failure.
With `--at-head` it checks against each repository's current HEAD, which says
whether the code has moved since.

## Configuration

`walkthrough.yaml` in the run directory sets `concurrency`, `explore_model`,
`docs_model`, `trace_model`, `write_model`, `effort` and `max_turns`.
`multiplai.conf` keys `walkthrough_explore_model`, `walkthrough_docs_model`,
`walkthrough_trace_model`, `walkthrough_write_model` and `walkthrough_effort`
apply when the file does not set them. By default every stage runs on the
session's model.
