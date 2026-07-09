---
name: deep-research
description: Conduct web research using a code-driven pipeline (plan, diverge, challenge, search, triage, read, reassess, synthesize). Asks clarifying questions first, searches via Brave/Tavily/Exa/Serper/You.com, follows links by default, and outputs at three detail levels (gist, structured, detailed).
effort: medium
---

# Deep Research — Thin Dispatcher

You are a thin dispatcher. Your job is to gather context, optionally present a plan for review, invoke the Python pipeline, and return the result. The actual research workflow runs in `scripts/research_pipeline/` as code — you do not execute search, fetch, or synthesis yourself.

## Arguments

Parse the user's invocation for:

| Arg | Description | Default |
|-----|-------------|---------|
| **query** | The research topic/question | *(required)* |
| `--output <path>` | Directory to write results | `./INBOX` if it exists, else cwd |
| `--auto` | Skip discovery and plan review | off (interactive) |
| `--preset <name>` | `micro`, `quick`, `standard`, `thorough` | `standard` |
| `--parallel` | Run N parallel sub-topic pipelines | off |
| `--agents <N>` | Number of parallel agents (2-5) | auto |
| `--deep` | Don't downscale preset for parallel sub-agents | off |
| `--research-type <t>` | `general`, `company`, `job-market`, `fact-check`, `theme` | `general` |
| `--challenge` | Force adversarial review | off (auto-on for `thorough`) |
| `--no-challenge` | Skip adversarial review | — |
| `--no-memory` | Skip memory triage | off |

**Auto-detection of `--auto`:** phrases like "just go ahead", "don't ask me", "run unattended", "full auto" all imply `--auto`.

**Output directory rule:** When no `--output` is specified, default to `./INBOX` if that directory exists (a curated workspace), otherwise the current working directory. If the user explicitly passes `--output`, respect their choice. Always pass an absolute path to `--output` when invoking the pipeline. (The pipeline itself also defaults `--output` to cwd if omitted, so it works either way.)

## Flow

### Step 1: Discovery (skip if `--auto`)

Use the Skill tool to invoke `/interviewer` with args: `"about their research needs: {query}"`. Collect the refined understanding into a refined query and context summary.

### Step 2: Personal context injection

If the query touches anything personal (immigration, jobs, finance, health, relocation, education), check the user's memory files via the Memory File Index in CLAUDE.md (skip this step if there's no memory index / memory files — e.g. a vanilla install). Extract facts that should constrain or expand the research. Carry these facts forward as `personal_context` — they're injected into the pipeline via `--personal-context`.

### Step 3: Prior knowledge scanning

Scan for existing research in the workspace (skip any directory that doesn't exist):
1. `RESOURCES/` — glob for topic-relevant files
2. `INBOX/` — recent research dumps
3. Previous deep research outputs (files with YAML appendix)

Skim matches for known facts, sources already consulted, and open gaps. Format as a summary for `--prior-knowledge`.

Cap this at 2-3 tool calls. If none of those directories exist or nothing is relevant, set prior knowledge to "none".

### Step 4: Plan review (skip if `--auto`)

Invoke the pipeline in `--plan-only` mode. It's a Python module run via `uv run --directory`, which resolves the package and installs its dependencies (declared in `scripts/pyproject.toml`, including `multiplai-core`) into an ephemeral env — no manual venv or `PYTHONPATH` setup:

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT}/skills/deep-research/scripts" \
  python -m research_pipeline \
  --query "{query}" \
  --preset {preset} \
  --research-type {research_type} \
  --date {today} \
  --session-id "{session_id}" \
  --personal-context "{personal_context}" \
  --prior-knowledge "{prior_knowledge}" \
  --plan-only
```

**Important:** Pass `--session-id` with the current session ID for log correlation when it's known (the multiplai-context SessionStart hook prints it). If you don't have a session ID, omit `--session-id` — it's optional.

The pipeline outputs the generated plan as JSON. Present it to the user as human-readable prose:

```
RESEARCH PLAN
=============
Query: ...
Sub-questions:
  1. ...
  2. ...
Query strategy:
  Primary: ...
  Mechanism (DIVERGE): ...
  Contrarian (CHALLENGE): ...
  Directory searches: ...
Authority domains (guaranteed fetch): ...
Target domains: ...
What "good" looks like: ...
```

Ask the user to approve, adjust, or switch modes. Save the approved plan to a temp file.

### Step 5: Execute the pipeline

Run the pipeline with the approved plan (if reviewed) or directly (if `--auto`):

```bash
# Interactive (with approved plan):
uv run --directory "${CLAUDE_PLUGIN_ROOT}/skills/deep-research/scripts" \
  python -m research_pipeline \
  --query "{query}" \
  --output "{output_dir}" \
  --preset {preset} \
  --research-type {research_type} \
  --date {today} \
  --session-id "{session_id}" \
  --personal-context "{personal_context}" \
  --prior-knowledge "{prior_knowledge}" \
  --approved-plan /tmp/approved-plan.json \
  [--parallel] [--agents N] [--deep] [--challenge|--no-challenge]

# Auto:
uv run --directory "${CLAUDE_PLUGIN_ROOT}/skills/deep-research/scripts" \
  python -m research_pipeline \
  --query "{query}" \
  --output "{output_dir}" \
  --preset {preset} \
  --session-id "{session_id}" \
  --auto \
  [other args]
```

The pipeline:
- Handles all gates, state, checkpointing, retries, and timeouts internally
- Writes progress to `{output}/{query-slug}-{date}-progress.md` (tail to monitor)
- Writes final report to `{output}/{query-slug}-{date}.md`
- Prints `PATH: <file>` and `SUMMARY: <text>` on completion
- Returns exit code 0 on success, non-zero on failure (state preserved for resume)

### Step 6: Return summary and filename

Parse the pipeline's stdout for `PATH:` and `SUMMARY:` lines. Tell the user:

```
Research complete: {filename}

{summary text}
```

If the pipeline failed (non-zero exit) or the output file shows `Status: INCOMPLETE`:

1. Read the output file to understand what gaps were identified (look for "Critical Gaps" section)
2. Check for `STATUS: INCOMPLETE` in stdout — the state file path will be printed. The state file contains ALL findings and can be used for retry.
3. Re-invoke the pipeline with a **narrower query** targeting the specific gaps. Use `--preset quick` for gap-filling runs to keep costs down.
4. When the gap-filling run completes, tell the user both files exist and summarize the combined findings.

If the pipeline failed with a non-zero exit and no output file, note that state is preserved at `{state_file_path}` — the user can re-run to resume.

### HARD RULE: No ad-hoc research after pipeline failure

**NEVER use WebSearch or WebFetch directly after a pipeline failure or INCOMPLETE result.** This is a repeatedly corrected failure mode. The pipeline is the research tool. You are a dispatcher. When the pipeline fails:

- **Retry the pipeline** with a narrower query or different flags (`--preset quick`, `--no-claude-tools`)
- **Do NOT** fall back to manual WebSearch/WebFetch calls
- **Do NOT** "do the research more directly" or "pull the actual docs"
- The only acceptable manual fetch is if the user explicitly asks you to fetch a specific URL they provide

### Step 7: Memory triage (skip if `--no-memory` or `--auto`)

Review the summary for persistent decisions or facts (not just informational findings). For each candidate, propose a memory file update using the Memory File Index. Present 3-7 proposed updates for user approval. Write only approved items.

See the previous version of this flow for detailed memory triage rules — the logic hasn't changed.

## Requirements

**Default (a Claude subscription with web tools — e.g. Claude Max, $100 or $200/mo tier):** No setup needed. The pipeline uses Claude Code's built-in WebSearch and WebFetch via the SDK. Zero external API cost.

**Without Claude Max (`--no-claude-tools`):** External API keys required. Loaded from `.env` at the project root:

- `TAVILY_API_KEY` — 1,000 queries/month free (no CC). https://tavily.com
- `EXA_API_KEY` — 1,000 requests/month free (no CC). https://exa.ai
- `BRAVE_API_KEY` (optional) — 1,000 queries/month free (requires CC). https://brave.com/search/api/
- `SERPER_API_KEY` (optional) — 50K one-time free credits, $1/1K paid. https://serper.dev
- `YOU_API_KEY` (optional) — $100 one-time credit. https://api.you.com

**CLI flags:**
- `--no-claude-tools` — disable Claude Agent, use external APIs only
- `--allow-paid-fallback` — use providers beyond their free tier (only needed when all free-tier quota is exhausted; providers with remaining free quota are used automatically)

**Python deps** are declared in `scripts/pyproject.toml` and resolved
automatically by `uv run --directory`:

- `multiplai-core`, `httpx`, `trafilatura`, `tavily-python`, `exa-py`, `pydantic`, `python-dotenv`, `claude-agent-sdk`
- optional `[browser]` extra: `playwright` (JS-rendered fetch fallback; run `playwright install chromium` after installing)

## Architecture

The pipeline is a Python asyncio script with strict timeouts and per-source state checkpointing. It cannot hang on a WebFetch the way prompt-driven workflows can — every network call is under a hard timeout. On the default Claude Agent path (`ClaudeAgentFetcher`) fetches are killed at 60s per request / 180s per batch; the search router kills any single search at 45s (`per_query_timeout`); the legacy httpx fetcher (`--no-claude-tools`) uses 15s per request / 30s per batch. See `scripts/research_pipeline/` for the implementation. See `README.md` (skill root) for setup and `scripts/CLAUDE.md` for extension notes.
