---
name: qmd-search
description: "Manually search the user's resources knowledge base via qmd (semantic + keyword). Use when asked to 'search my resources', 'find that research about X', or when the automatic resources retrieval missed something."
---

# qmd-search — manual resources retrieval

Search the user's resources knowledge base through its qmd index. This is
the manual companion to the automatic per-prompt retrieval that
multiplai-context performs when `resources_retrieval=qmd` — use it for
deeper digging, different phrasings, or when the automatic injection
missed a document you expect to exist.

## Resolve the configuration first

The plugin options (in `settings.json` → `pluginConfigs` →
`multiplai-context@<marketplace>` → `options`, or the matching
`CLAUDE_PLUGIN_OPTION_*` env vars) decide how to reach qmd:

| Option | Default | Meaning |
|---|---|---|
| `qmd_mode` | `local` | `local` = qmd binary on PATH; `ssh` = qmd runs on the host over the SSH bridge |
| `qmd_ssh_host` | `host.docker.internal` | bridge host for `ssh` mode |
| `qmd_collection` | `resources` | collection holding the index |
| `resources_dir` | — | maps result URIs back to absolute file paths |
| `workspace_dir` | — | the project-local `.qmd/` index lives at this root |

## Commands (pick by depth)

`local` mode — run qmd directly (try `~/.bun/bin/qmd` if not on PATH):

```bash
# Fast semantic (~1-2s) — default choice
qmd vsearch '<query>' -c <collection> -n 5 --json

# Keyword/BM25 (ANDs all terms — use 2-4 content words, not full sentences)
qmd search '<terms>' -c <collection> -n 5 --json

# Deep hybrid with query expansion + rerank (slow, ~25s) — when the above miss
qmd query '<query>' -c <collection> -n 5 --json
```

`ssh` mode — same subcommands, wrapped in the bridge call, with the
workspace as cwd (the index is project-local):

```bash
ssh -o BatchMode=yes <qmd_ssh_host> \
  "cd <workspace_dir> && qmd vsearch '<query>' -c <collection> -n 5 --json"
```

## Rules

- In `ssh` mode the query travels inside single quotes through a
  restricted gateway: strip `` ;|&<>`$()'"\ `` and newlines from the
  query first (the gateway rejects shell metacharacters outright).
- Result URIs look like `qmd://<collection>/<relpath>` → the file is
  `<resources_dir>/<relpath>`. Read the full file before answering
  from it — search snippets are excerpts, not the document.
- When recall matters, run BOTH `vsearch` and `search` and merge —
  that is what the automatic retrieval does (RRF fusion).
- `search` (BM25) ANDs its terms: a full natural-language question
  matches nothing. Ladder down: try 4 content words, then 3, then 2.
- Index health / refresh: `qmd status`, `qmd update`, `qmd embed`
  (same mode wrapping). The plugin also refreshes incrementally at
  session start when the qmd backend is active.
- Bridge denied/down in `ssh` mode → tell the user (the host gateway
  allowlist from multiplai-container may not be deployed); do NOT try
  to bypass the gateway.
