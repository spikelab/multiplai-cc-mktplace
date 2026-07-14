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
| `qmd_mode` | `local` | `http` = POST to a resident `qmd mcp --http` daemon (preferred); `local` = qmd binary on PATH; `ssh` = qmd runs on the host over the SSH bridge |
| `qmd_http_url` | `http://host.docker.internal:8181` | daemon base URL for `http` mode |
| `qmd_ssh_host` | `host.docker.internal` | bridge host for `ssh` mode |
| `qmd_collection` | `resources` | collection holding the index |
| `resources_dir` | — | maps result URIs back to absolute file paths |
| `workspace_dir` | — | the project-local `.qmd/` index lives at this root |

## Commands (pick by depth)

### `http` mode — POST an authored, typed query (preferred)

Hit the daemon's REST endpoint directly from the container (plain
`curl`, no SSH bridge). **Author the query — do not paste the raw
sentence.** qmd's lexical arm ANDs its terms and carries 2× weight in the
fusion, so a full sentence (stopwords and all) either matches nothing or
elects a junk doc to rank 1. Send a `lex` arm of only the 2–3 rarest
content words plus a `vec` arm carrying the whole prompt:

```bash
curl -s http://host.docker.internal:8181/query \
  -H 'Content-Type: application/json' -d '{
    "searches": [
      {"type": "lex", "query": "gesha arabica"},
      {"type": "vec", "query": "I want to learn more about the coffee varietal"}
    ],
    "intent": "I want to learn more about the coffee varietal",
    "collections": ["resources"],
    "limit": 5, "candidateLimit": 10, "minScore": 0.0, "rerank": true
  }'
```

- Add `{"type": "hyde", "query": "<a short hypothetical answer passage>"}`
  when the question shares little vocabulary with the target document —
  you are a better query expander than the built-in model.
- Do **not** POST `{"query": "<raw sentence>"}`. That re-enables the
  built-in auto-expansion and the poisoned raw-sentence lexical arm.
- `GET http://host.docker.internal:8181/health` → `{"status":"ok",...}`
  confirms the daemon is up before you rely on it.

### `local` mode — run qmd directly (try `~/.bun/bin/qmd` if not on PATH)

```bash
# Fast semantic (~1-2s) — default choice
qmd vsearch '<query>' -c <collection> -n 5 --json

# Keyword/BM25 (ANDs all terms — use 2-4 rarest content words, not full sentences)
qmd search '<terms>' -c <collection> -n 5 --json

# Deep hybrid with query expansion + rerank (slow, ~25s) — when the above miss
qmd query '<query>' -c <collection> -n 5 --json
```

### `ssh` mode — same subcommands wrapped in the bridge call

The workspace is the cwd (the index is project-local):

```bash
ssh -o BatchMode=yes <qmd_ssh_host> \
  "cd <workspace_dir> && qmd vsearch '<query>' -c <collection> -n 5 --json"
```

## Rules

- **Author the lexical arm; never paste the raw prompt.** In every mode,
  the keyword side ANDs its terms and is weighted heavily in the fusion —
  feed it only the 2–3 rarest content words (drop stopwords and generic
  quantifiers like "more"/"learn"). The full sentence belongs on the
  semantic side (`vec`) and in `intent`, never in `lex`.
- In `http` mode qmd does the fusion + rerank itself, so send one typed
  `searches` array (above) rather than hand-merging `vsearch` + `search`.
  The hand-merge below is only for `local`/`ssh` mode.
- In `ssh` mode the query travels inside single quotes through a
  restricted gateway: strip `` ;|&<>`$()'"\ `` and newlines from the
  query first (the gateway rejects shell metacharacters outright).
- Result URIs look like `qmd://<collection>/<relpath>` → the file is
  `<resources_dir>/<relpath>`. Read the full file before answering
  from it — search snippets are excerpts, not the document.
- Results are **chunk-level**, not just document-level: `line` is where
  the matching chunk starts in the file, and `snippet` shows that chunk
  with a `@@ -start,count @@ (N before, M after)` context header. To
  inspect the match, Read the file with `offset` near that line; to pull
  the whole document through qmd instead, `qmd get qmd://<collection>/<relpath>`
  (same mode wrapping). Multiple hits in one file = multiple chunks;
  the automatic retrieval keeps only the best chunk per file.
- (`local`/`ssh`) When recall matters, run BOTH `vsearch` and `search`
  and merge — that is what the automatic retrieval does (RRF fusion).
- `search` (BM25) ANDs its terms: a full natural-language question
  matches nothing. Ladder down: try 4 content words, then 3, then 2.
- Index health / refresh: `qmd status`, `qmd update`, `qmd embed`. These
  are CLI-only and NOT on the `http` daemon's endpoint (read-only) — run
  them over the SSH bridge (or on the host) even when queries go over
  `http`. The plugin also refreshes incrementally at session start when
  the qmd backend is active.
- Bridge denied/down in `ssh` mode, or the `http` daemon unreachable →
  tell the user (the daemon may not be running, or the host gateway
  allowlist from multiplai-container may not be deployed); do NOT try to
  bypass the gateway.
