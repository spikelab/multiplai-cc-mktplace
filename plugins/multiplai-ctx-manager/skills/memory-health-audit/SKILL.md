---
name: memory-health-audit
description: "Full memory system health audit — cross-correlates retrieval logs, diary, learnings, and memory file structure to assess what's working, what's wasteful, and what to change. Broader than the /multiplai:health check (which focuses on infrastructure). Use when the user asks 'how's memory doing', 'audit the memory system', 'what patterns have you noticed about memory', 'memory health check', or periodically (monthly) to track system health over time."
model: opus
effort: high
---

# Memory Health Audit

Full cross-source analysis of the memory system. Produces a dated assessment snapshot saved to `$CLAUDE_PLUGIN_DATA/memory-health/` for longitudinal tracking. Covers four data sources: retrieval logs, diary, learnings, and the memory corpus itself.

**Distinct from `/multiplai:health`:** That skill checks infrastructure (files exist, not stale, config wired). This skill cross-correlates ALL four data sources to surface systemic patterns — file overload, corpus bloat, learning gaps, project coverage, and actionable restructuring recommendations.

## Key Paths

| Source | Path | What It Contains |
|--------|------|-----------------|
| Retrieval logs | `$CLAUDE_PLUGIN_DATA/logs/context_manager-*.log` | Per-prompt routing decisions (`ROUTING` lines), fallback events (`FALLBACK` lines), no-context events (7-day retention) |
| Diary | `.multiplai/diary/*.md` | Session diary — what happened, which projects, commits |
| Learnings | `.multiplai/learnings/*.md` | Extracted insights — types, trust levels, target files |
| Memory corpus | `.multiplai/memory/*.md` | The actual memory files — structure, size, staleness |
| Memory catalog | `$CLAUDE_PLUGIN_DATA/catalogs/memory_catalog.json` | Routing descriptions, intent_domains, anti_domains |
| Previous assessments | `$CLAUDE_PLUGIN_DATA/memory-health/*.md` | Past audit snapshots for delta comparison |

## Workflow

**CRITICAL: Delegate all heavy log parsing to background agents.** Each data source can be 1000s of lines. Never load raw logs into the main conversation. Launch 3-4 parallel agents, each returning aggregated metrics.

### Phase 1: Gather (parallel agents)

Launch these agents simultaneously:

#### Agent 1: Retrieval Log Analysis
Parse ALL `context_manager-*.log` files in `$CLAUDE_PLUGIN_DATA/logs/`. Return:
- Total routing decisions, NONE rate %, actual file load rate %, fallback rate %
- Per-file retrieval frequency from `ROUTING` lines
- Co-retrieval patterns (which files appear together, with co-occurrence counts)
- Fallback count (lines containing `FALLBACK`)

**Log line formats:**
- `ROUTING memory=["file1.md","file2.md"] skills=[] resources=[]` — router-selected files
- `FALLBACK memory=["file1.md",...]` — router picked nothing; metadata-ranked fallback used
- `No context to inject` — NONE: no memory, skills, resources, or project state

**Extraction commands:**
```bash
LOGS="$CLAUDE_PLUGIN_DATA/logs"

# File retrieval frequency (router picks only)
grep "ROUTING memory=" "$LOGS"/context_manager-*.log \
  | sed 's/.*ROUTING memory=\(\[.*\]\) skills=.*/\1/' \
  | python3 -c "
import sys, json, collections
counts = collections.Counter()
for line in sys.stdin:
    try:
        files = json.loads(line.strip())
        counts.update(files)
    except: pass
for f, n in counts.most_common(): print(n, f)
"

# NONE rate
total=$(grep -c "ROUTING memory=\|No context to inject" "$LOGS"/context_manager-*.log 2>/dev/null || echo 0)
none=$(grep -c "No context to inject" "$LOGS"/context_manager-*.log 2>/dev/null || echo 0)
echo "Total: $total  NONE: $none"

# Fallback rate
grep -c "FALLBACK memory=" "$LOGS"/context_manager-*.log 2>/dev/null || echo 0

# Co-retrieval patterns
grep "ROUTING memory=" "$LOGS"/context_manager-*.log \
  | sed 's/.*ROUTING memory=\(\[.*\]\) skills=.*/\1/' \
  | python3 -c "
import sys, json, collections
patterns = collections.Counter()
for line in sys.stdin:
    try:
        files = json.loads(line.strip())
        if len(files) > 1:
            patterns[tuple(sorted(files))] += 1
    except: pass
for p, n in patterns.most_common(10): print(n, list(p))
"
```

#### Agent 2: Diary Analysis
Read ALL `.multiplai/diary/*.md` files. Return:
- Session count and sessions/day
- Project mention frequency (top 15)
- Work type distribution (fix/build/research/write — estimate from keywords)
- Auto-commit count and commits/day
- Session clustering patterns (dates with abnormal session counts)
- Skill usage mentions (which skills appear in diary)

#### Agent 3: Learnings Analysis
Read ALL `.multiplai/learnings/*.md` files. Return:
- Total learnings count (processed vs pending)
- Type distribution (OBSERVATION, PATTERN, RULE-PROPOSAL, CORRECTION)
- Trust distribution (verified, high, medium)
- Target file frequency (which memory files get the most learnings directed at them)
- List ALL corrections found (full text) — these are highest signal

#### Agent 4: Memory Corpus Analysis
Read ALL `.multiplai/memory/*.md` files and `$CLAUDE_PLUGIN_DATA/catalogs/memory_catalog.json`. Return:
- Per-file: name, line count, section headers, density assessment (high/medium/low)
- Staleness: last modified date per file, flag anything >30 days
- Overlap identification: content that appears in multiple files
- Signal:noise estimate per file (% of lines that are actionable vs filler)
- Catalog quality: which files have specific vs vague intent_domains
- Files with no retrieval demand in logs (never loaded — dead weight or routing gap?)

### Phase 2: Cross-Correlate

After all agents return, correlate findings:

1. **Overloaded files** — High retrieval demand + low signal:noise + many learnings targeting it = needs splitting
2. **Underloaded files** — Never retrieved + fresh content = routing gap (domains too narrow)
3. **Wasteful retrievals** — High retrieval demand but content rarely actionable = loaded too eagerly
4. **Learning gaps** — Project with high diary activity but zero learnings = extraction not capturing
5. **Stale but active** — File loaded frequently but not updated in 30+ days = content may be outdated
6. **Correction clusters** — Multiple corrections targeting same file = systematic accuracy problem
7. **Fallback-heavy** — High fallback rate = router not selecting relevant files (catalog quality issue)

### Phase 3: Delta Comparison

Read the most recent previous assessment from `$CLAUDE_PLUGIN_DATA/memory-health/`. Compare:

| Metric | Previous | Current | Delta | Direction |
|--------|----------|---------|-------|-----------|
| NONE rate | | | | |
| File load rate | | | | |
| Fallback rate | | | | |
| Top file demand | | | | |
| Corpus size (lines) | | | | |
| Correction count | | | | |
| Stale files (>30d) | | | | |

If no previous assessment exists, skip delta and establish baseline.

### Phase 4: Recommendations

Based on the cross-correlation, produce prioritized recommendations:

1. **Files to split** — name the file, what to extract, where it goes
2. **Routing to tighten** — name the file, current domains, suggested narrower domains
3. **Routing to broaden** — name the never-loaded file, evidence it should be loaded
4. **Content to move** — research material in memory/ that belongs in RESOURCES/
5. **Files to merge** — small files with overlapping content
6. **Staleness to address** — files needing review with specific sections flagged

Each recommendation should include:
- **Evidence** — the specific metrics that justify the change
- **Expected impact** — what improves and by how much (estimate)
- **Effort** — trivial / moderate / significant

### Phase 5: Save Assessment

Write the full assessment to `$CLAUDE_PLUGIN_DATA/memory-health/YYYY-MM-DD-assessment.md` with:
- All metrics from Phase 1 (tabular format)
- Cross-correlation findings from Phase 2
- Delta comparison from Phase 3 (if available)
- Prioritized recommendations from Phase 4
- Method notes

Report key findings in the conversation. Keep the conversation output concise — the full data lives in the assessment file.

## Guidelines

- **Monthly cadence recommended.** Run after at least 50 routing decisions have accumulated since last assessment.
- **Always delegate to agents.** Raw logs are too large for the main context. Each agent returns only aggregated metrics.
- **Honest grading.** Don't inflate signal:noise ratios. If a file is bloated, say so.
- **Actionable over comprehensive.** 5 concrete recommendations beat 20 vague observations.
- **Track longitudinally.** The `memory-health/` directory is the longitudinal record. Each assessment is a dated snapshot. Deltas show whether changes improved the system.
- **Relationship to `/multiplai:health`:** That skill can run anytime for quick infrastructure checks. This skill runs monthly for the full systemic view. They complement each other.
