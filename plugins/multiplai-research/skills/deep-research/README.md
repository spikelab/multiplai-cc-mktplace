# Deep Research Skill

A Claude Code skill for conducting web research using a code-driven Python pipeline.

## Architecture (v9.0 — April 2026)

The skill is now a **thin dispatcher** (`SKILL.md`) that invokes a **Python pipeline** (`scripts/research_pipeline/`). The pipeline orchestrates all 8 research stages in code, calling `claude_agent_sdk.query()` at specific nodes for LLM reasoning (planning, extraction, synthesis). Web search and fetching are direct Python calls, not LLM-driven tool use.

**Why:** The previous prompt-orchestrated version could skip stages, ignore gates, lose state on crash, and hang indefinitely on bad fetches. Code-level orchestration fixes all four problems:

- **Enforced gates** — query diversity, min sources, coverage checks are code assertions the LLM can't bypass
- **Persistent state** — `ResearchState` is checkpointed to disk after every stage with per-source granularity (resume proportional to what was lost, not per-phase)
- **Strict timeouts** — every fetch is wrapped in `asyncio.wait_for` with hard 15s kill. No more hung WebFetch blocking the pipeline.
- **Guaranteed parallelism** — `asyncio.gather` handles parallel search and fetch; no dependence on the LLM voluntarily parallelizing

Split is roughly **55% code / 45% LLM** (following Tunguz's hybrid state machine pattern).

## Setup

**Prerequisite: `uv`** (https://docs.astral.sh/uv/). That's the only setup. The
pipeline is invoked with `uv run --directory`, which reads `scripts/pyproject.toml`
and resolves every Python dependency (`httpx`, `trafilatura`, `tavily-python`,
`exa-py`, `pydantic`, `python-dotenv`, `claude-agent-sdk`, `multiplai-core`) into
an ephemeral environment. No manual `pip install`, no shared venv, no `PYTHONPATH`:

```bash
uv run --directory <path-to>/skills/deep-research/scripts \
  python -m research_pipeline --query "..."
```

(The `SKILL.md` dispatcher uses `${CLAUDE_PLUGIN_ROOT}` for that path.)

**Search providers:**

**Default (a Claude subscription with web tools):** No configuration needed. The
pipeline uses Claude Code's built-in WebSearch and WebFetch via the SDK. Zero
external API cost.

**Optional fallback API keys:** `.env` is optional — only needed for the
`--no-claude-tools` path or when Claude Agent tools are unavailable. Place a
`.env` file next to `pyproject.toml` (in `scripts/`); the pipeline auto-loads it
via `python-dotenv`. Shell-exported variables take precedence, so you can
override a single key inline:

```
TAVILY_API_KEY="tvly-..."
EXA_API_KEY="..."
```

Get keys from:
- **Tavily** — 1,000 queries/month free (no CC). https://tavily.com
- **Exa** — 1,000 requests/month free (no CC). https://exa.ai
- **Brave** (optional) — 1,000 queries/month free (requires CC). https://brave.com/search/api/
- **Serper** (optional) — 50K one-time free credits, $1/1K paid (cheapest). Only provider used beyond free tier. https://serper.dev
- **You.com** (optional) — $100 one-time credit. https://api.you.com

**Relevant CLI flags:**

```bash
# Force external APIs only (requires TAVILY_API_KEY + EXA_API_KEY in .env)
uv run --directory <...>/scripts python -m research_pipeline --query "..." --no-claude-tools

# Allow fallback to paid APIs if free-tier quota is exhausted
uv run --directory <...>/scripts python -m research_pipeline --query "..." --allow-paid-fallback
```

See `references/search-engines.md` for the full provider comparison and ranking rationale.

## Pipeline Architecture

```
SKILL.md (thin wrapper)
  └── invokes scripts/research_pipeline via Bash
        └── Python pipeline using claude_agent_sdk.query()
              ├── PLAN node (LLM)          → sub-questions
              ├── DIVERGE node (LLM)       → mechanism/directory queries
              ├── CHALLENGE node (LLM)     → contrarian queries
              │   [GATE: query diversity check — CODE]
              ├── SEARCH node (CODE)       → parallel Brave/Tavily/Exa/Serper/You via SearchRouter
              ├── TRIAGE node (CODE + LLM) → dedup (code), relevance scoring (LLM)
              │   [GATE: min sources met — CODE]
              ├── READ node (CODE + LLM)   → httpx fetch + trafilatura (code),
              │                              LLM finding extraction per source
              │   [GATE: coverage check — CODE]
              ├── REASSESS node (LLM)      → framing/claims check
              │   [GATE: refinement/verification cycle decision — CODE]
              ├── VERIFY node (LLM)        → per-claim verdicts (confirmed/refuted/unresolved)
              │                              from verification-read evidence
              ├── SYNTHESIZE node (LLM)    → final markdown + YAML appendix
              └── ADVERSARIAL REVIEW (LLM) → stress-test (--challenge, thorough preset,
                                             or auto when REASSESS flags suspect claims)
```

All stages write to a `ResearchState` JSON file after every transition. Per-source tracking in READ means crashing at source 19/20 only re-fetches source 20 on resume, not the entire phase.

## File Structure

```
deep-research/
├── SKILL.md                    # Thin dispatcher (~130 lines)
├── README.md                   # This file
├── references/
│   └── research-types.md       # Per-type guidance injected into prompts
└── scripts/
    ├── pyrightconfig.json
    ├── research_pipeline/
    │   ├── __main__.py         # CLI entry point
    │   ├── pipeline.py         # Orchestrator — sequences stages + gates
    │   ├── config.py           # ResearchConfig + presets
    │   ├── state.py            # ResearchState with per-source tracking
    │   ├── gates.py            # Query diversity, min sources, coverage, reassess
    │   ├── search_router.py    # Multi-API routing with quota tracking
    │   ├── fetcher.py          # httpx + trafilatura, hard timeouts, retry
    │   ├── sdk.py              # claude_agent_sdk.query() wrapper
    │   ├── models.py           # Pydantic models (SearchResult, Source, Finding, ...)
    │   ├── progress.py         # Progress file writer
    │   ├── research_types.py   # Loads references/research-types.md
    │   ├── eval.py             # Quality eval harness
    │   ├── prompts/            # Per-node prompt templates
    │   └── nodes/              # Per-stage node implementations
    └── tests/                  # 86 tests covering gates, state, router, fetcher, sdk, eval
```

## Quick Start

### Presets

| Preset | Trigger | Sources | Links | Output |
|--------|---------|---------|-------|--------|
| **micro** | "one quick fact", smallest scope | 3 | No | gist |
| **quick** | "quick check", "briefly" | 10 | No | gist |
| **standard** | "research", "look into" | 20 | 1 level | structured |
| **thorough** | "deep dive", "comprehensive" | 30 | 2 levels | detailed |

### Examples

```
Quick check: Is Tailwind still popular in 2025?
→ gist output, ~10 sources, minimal questions

Research Anthropic for a job application
→ structured output, ~20 sources, discovery questions first

Deep dive on the AI job market in Toronto
→ detailed output, ~30 sources, extensive discovery

Parallel deep dive: AI regulation — US vs EU vs China, industry impact, compliance
→ parallel mode (4 agents), ~80 sources total, unified synthesis

--parallel --deep research the future of remote work: culture, productivity, tools, and policy
→ parallel mode (4 agents), ~120 sources total, max depth per agent
```

## Key Features

### Discovery Phase
Asks 2-4 targeted questions before researching to understand scope, recency needs, geography, and use case. Quick preset skips most questions; thorough preset asks more.

### Three Output Levels

| Level | Contents |
|-------|----------|
| **gist** | 1 paragraph + 3-5 bullets + top 3 sources |
| **structured** | Executive summary + findings with sources + gaps + source table |
| **detailed** | Everything above + methodology + evidence quotes + contradictions + all sources consulted |

### Source Diversity Enforcement

- Maximum 3 sources from same domain
- Requires mix: authoritative + news + expert analysis
- Seeks different perspectives for theme/job-market research
- Geographic diversity for international topics

### Reputation Assessment

Five-tier system: authoritative → established → emerging → questionable → unreliable

Each source is evaluated on:
- Domain authority (.gov, .edu, recognized orgs)
- Author credibility
- Editorial process evidence
- Citation practices
- Recency and consistency

### Staleness Rules

| Research Type | Max Age |
|---------------|---------|
| job-market | 30 days |
| company | 90 days (facts), 7 days (news) |
| theme | 1-2 years |
| fact-check | varies by claim |

### Parallel Processing

Uses Task tool to spawn subagents for parallel web fetching, significantly reducing research time for thorough searches.

## Parameters

### Core
| Parameter | Default | Options |
|-----------|---------|---------|
| research_type | general | general, company, job-market, fact-check, theme |
| summary_level | structured | gist, structured, detailed |
| preset | standard | quick, standard, thorough |

### Parallel Mode
| Parameter | Default | Options |
|-----------|---------|---------|
| --parallel | off (auto-detected) | Force parallel multi-agent mode |
| --no-parallel | — | Force single-agent mode |
| --agents N | auto (2-5) | Number of parallel sub-agents |
| --deep | off | Each sub-agent runs at parent preset (no downscaling) |

### Breadth
| Parameter | Default | Range |
|-----------|---------|-------|
| max_results | 20 | 10-30 |
| sources_to_read | 10 | 5-15 |
| min_source_diversity | 4 | 3-8 unique domains |

### Depth
| Parameter | Default | Options |
|-----------|---------|---------|
| follow_links | true | true, false |
| max_sub_pages | 3 | 1-5 |
| link_depth | 1 | 1-2 |

### Quality
| Parameter | Default | Options |
|-----------|---------|---------|
| generate_sub_questions | true | true, false |
| max_sub_questions | 3 | 2-5 |
| search_angles | 2 | 1-3 query variations |
| require_authoritative | true | true, false |
| citation_style | inline | inline, numbered, academic |

## Research Types

| Type | Domain Bias | Best For |
|------|-------------|----------|
| **company** | crunchbase, linkedin, glassdoor, news | Job applications, competitive analysis |
| **job-market** | linkedin, glassdoor, industry reports | Career exploration, relocation decisions |
| **fact-check** | authoritative sources, primary data | Verifying claims, checking statistics |
| **theme** | broad web, academic, news | Exploring topics, gathering perspectives |
| **general** | no bias | Mixed queries |

See `references/research-types.md` for detailed configurations.

(For the full file layout, see the **File Structure** section above.)

## Changelog

### v9.0 (2026-04-05) — Code-driven Python pipeline

**Major refactor:** replaced the 2,279-line prompt-orchestrated workflow with a Python pipeline. The `research-prompt.md` methodology file is gone; `SKILL.md` is now a thin dispatcher (~130 lines). All stages now run as Python code in `scripts/research_pipeline/`, with the LLM called via `claude_agent_sdk.query()` at specific nodes for reasoning tasks only.

**Why:**
- Stages can no longer be skipped — the orchestrator enforces them
- Gates are code assertions (diversity, min sources, coverage) that the LLM cannot bypass
- State checkpoints to disk per-source, enabling crash recovery proportional to what was lost
- Web fetching uses `asyncio.wait_for` with hard 15s kills — the pipeline cannot hang indefinitely on a slow URL (the exact failure mode that killed research in v8)
- Parallelism is guaranteed via `asyncio.gather`, not hoped for
- Focused per-node prompts (50-200 lines each) instead of 1,100 lines of methodology carried in the agent's context

**New dependencies:** `httpx`, `trafilatura`, `tavily-python`, `exa-py`, `pydantic` (added to project `requirements.txt`).

**New requirements:** API keys for Tavily and Exa (both free, no CC). Optional keys for Serper and You.com.

**Preserved:** All research methodology knowledge, output formats (gist/structured/detailed + YAML appendix), presets, research types, reputation tiers, staleness rules, and CLI arguments.

**Eval dataset:** 75 fixtures automatically extracted from existing research outputs in `RESOURCES/` for quality regression testing.

### v8.0 (2026-03-30) — Falsifiability, Index Block, Adversarial Review

Three improvements to research output quality and machine-parseability, inspired by [limina](https://github.com/theam/limina):

1. **Falsifiability prompt:** Pre-synthesis checkpoint 4 requires the agent to articulate what evidence would disprove its conclusions. Statement appears in all output levels (gist: 1 sentence, structured: 2-3 sentences, detailed: per-conclusion breakdown). Catches unfalsifiable conclusions before they ship.
2. **YAML index block:** New `index:` section at the top of every YAML appendix. Contains `questions_investigated`, `questions_open`, `sources_consulted`, `total_findings`, `findings_by_confidence`, `sources_by_reputation`, and `falsifiability`. Enables future research sessions to quickly parse coverage from prior research without reading the full document. PLAN progress entry now records actual sub-question text.
3. **Adversarial review:** Devil's advocate pass (`--challenge` flag, auto on thorough preset, and auto-triggered whenever REASSESS flags load-bearing/conflation/convenience-bias claims — `--no-challenge` suppresses). After synthesis, a fresh call reads the completed report plus the extracted findings, spot-checks grounding, identifies weakest claims, and rates robustness on 3 structured dimensions (evidence strength, argument coherence, counter-argument resistance, each 1-5). Writes a `-challenge.md` review file that opens with a code-generated score table, and the pipeline prints a `CHALLENGE: <file> | overall=<score>` line for the dispatcher to surface.

**New arguments:** `--challenge`, `--no-challenge`

### v6.0 (2026-02-21)
- **Auto-detection**: Dispatcher now recognizes natural language cues for unattended mode ("just go ahead", "research this for me", etc.) — no need for explicit `--auto` flag
- **Prior knowledge**: New Step 1.7 scans workspace (RESOURCES/, INBOX/, memory, chat context) for existing research on the topic. Injects condensed prior knowledge into the research brief so the subagent focuses on gaps, not re-researching known facts
- **Chat summary**: Subagent returns substantive summary (3-5 paragraphs, tables if useful) alongside the file path. Dispatcher shows findings in chat
- **Progress protocol**: Subagent writes progress file with timestamps and findings at each phase transition. Serves as heartbeat + checkpoint for recovery
- **Deadman's switch**: Dispatcher launches subagent in background and monitors progress file on a timer (preset-dependent intervals). Detects stalls, kills stalled agents, recovers by launching synthesis-only agent from collected findings
- **Context guarding**: READ phase batches sources (5-7 per batch) and checkpoints findings to progress file after each batch. Extraction prompts are thorough (WebFetch has its own context) — the batching manages accumulated results in the agent's context. For SYNTHESIZE, agent reads findings back from the progress file rather than relying on in-context accumulation
- **Resume-from-checkpoint recovery**: When agent stalls, dispatcher reads progress file to determine last completed phase and resumes from that exact point with a fresh agent — not "synthesize partial results." Max 2 recovery attempts before escalating to user
- **LAUNCHED sentinel**: Dispatcher writes initial progress file entry before spawning agent. Distinguishes "agent never started" (full retry) from "agent started but stalled mid-work" (resume from checkpoint)

### v7.0 (2026-02-21) — Parallel Multi-Agent Research Mode

For broad or multi-faceted queries, the dispatcher splits research into N independent sub-topics and launches N research agents in parallel. A synthesis agent merges all outputs into a unified document.

**New arguments:** `--parallel`, `--no-parallel`, `--agents N`, `--deep`

**Architecture:**
```
Dispatcher
├── Detect parallel mode (explicit flag, language cues, or thorough + multi-faceted query)
├── Decompose query into N sub-topics (2-5 typically)
├── Generate N focused briefs (each with own output file, progress file)
├── Launch N research agents in parallel (single message, all background)
├── Monitor all N via parallel deadman's switch
│   ├── Per-agent stall detection + recovery (same 2-attempt protocol)
│   └── Proceed to synthesis when ≥ ceil(N/2) complete
└── Launch synthesis agent (background, monitored)
    ├── Reads all completed sub-research output files
    ├── Cross-references findings, resolves contradictions
    ├── Deduplicates sources across agents
    ├── Writes unified output with "Sub-research" provenance section
    └── Returns PATH + SUMMARY
```

**When to use:**
- **Single agent:** Quick preset (always), focused single-dimension queries, standard preset unless clearly multi-faceted
- **Parallel agents:** `--parallel` flag, thorough preset + 3+ distinct facets, or user says "parallel" / "multi-agent" / "split into sub-topics"

**Per-agent preset downscaling** (breadth comes from parallelism):

| Parent preset | Per-agent preset | Sources/agent | Total (N=4) |
|--------------|-----------------|---------------|-------------|
| standard | quick (10) | 10 | ~40 |
| thorough | standard (20) | 20 | ~80 |
| thorough + `--deep` | thorough (30) | 30 | ~120 |

**Partial completion:** Synthesis proceeds when ≥ ceil(N/2) agents complete. Failed sub-topics are noted as gaps in the unified output. If < ceil(N/2) complete, research fails — no synthesis attempted.

**Design decisions (resolved):**
1. Dispatcher decomposes (no separate planner) — it has all context from discovery/interview
2. Soft boundaries between agents; synthesis agent deduplicates
3. Simple polling loop for N progress files (N=2-5 is trivial)
4. Synthesis from partial results when enough agents complete
5. Per-agent source budget via preset downscaling
6. Synthesis agent reads output files directly (N×4000 words ≈ 25K tokens — feasible)
7. Self-contained unified output; sub-research files preserved for provenance

**Informal use history (2026-02-21, pre-formalization):**
- Query: "Memory retrieval techniques for intent-based file selection in small-corpus systems"
- 4 agents: pragmatic relevance, contrastive retrieval, hypothesis-driven retrieval, small-corpus techniques
- Results: 3/4 completed (53 sources), contrastive agent stalled with no output
- Manual synthesis into 5-layer architecture → led to description-based routing implementation

### v5.1 (2026-02-16)
- Added claim verification (VERIFY cycle) to REASSESS phase — catches load-bearing factual errors, conflation, and convenience bias
- Claims flagged by heuristic get targeted verification queries against authoritative sources, verdicted as CONFIRMED/CORRECTED/UNVERIFIABLE
- Added `verified_claims` section to structured and detailed output formats; `correction_note` to gist
- Verification impacts confidence: corrected claims lower confidence, unverifiable load-bearing claims cap at medium

### v5.0 (2026-02-15)
- Added URL deduplication in TRIAGE phase (exact + near-duplicate + syndication collapsing)
- Added REASSESS phase between READ and SYNTHESIZE — one optional refinement cycle if sources reveal wrong framing or new terminology
- Upgraded structured output tensions format to use structured disagreement mapping (topic, positions with sources, resolution)
- Enhanced detailed output tensions with significance level, evidence quotes, and "why they differ" analysis

### v2.0 (2025-01)
- Added DISCOVER phase with clarifying questions
- Three output levels: gist, structured, detailed
- Changed defaults: follow_links=true, 20 sources
- Source diversity enforcement (max 3 per domain)
- Explicit reputation assessment protocol (5 tiers)
- Staleness rules by research type
- Presets: quick, standard, thorough
- Parallel fetching via Task subagents
- Multiple search angles per question
- Interactive refinement option
- Citation style parameter
