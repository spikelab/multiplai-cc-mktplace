# Changelog

## 0.3.0 — 2026-07-19

extract-insights v2 — coherent argument chain, nuance harvest, readable middle.

### Changed
- **Argument Chain redesigned around a handoff contract.** Every link ends with
  `→ therefore:` naming its conclusion, and the next link must open from it.
  Non-linear arguments (the common case for podcasts) are organized as named
  threads (`**Thread A — <label>:**`, 2–4 links each) closed by a mandatory
  `**Convergence:**` block; forcing parallel material into a fake-linear
  numbered list is named a hard failure. Links are 1–2 full prose sentences
  with the speaker attributed in the sentence — the old `→ enables:` annotation
  format is gone.
- **New Pass 2: Nuance Harvest.** For sources > 500 lines, a windowed
  (~300-line) sweep collects hedged reversals, self-undercutting admissions,
  vivid metaphors/examples, content-bearing asides, and host contributions
  *before* any output section is written; the harvest feeds Tensions & Nuances
  and Emergent Insights and is exempt from the length-budget squeeze. Former
  Pass 2/3 renumbered to 3/4.
- **Two new fidelity checks.** Check 9 (chain linkage: every link's successor
  opens from its `→ therefore:` conclusion, or the link closes a labeled
  thread) and Check 10 (quartile coverage: every quarter of the source by line
  number must contribute at least one anchored item).
- **Readability pass on the middle sections.** Key Claims text is a full
  sentence with the speaker named in-sentence; strength tags stay but leading
  bracket-tag pileups are gone. TL;DR and Most Memorable Line are unchanged.

## 0.2.0 — 2026-07-10

Semantic model tiers for the deep-research pipeline (requires multiplai-core ≥ v0.7.0).

### Changed
- **Per-node model tiers instead of a single dated literal.** Reasoning nodes
  run opus via `pick_model("opus", task="deep-research")`; the high-volume
  per-source parse nodes (`triage_relevance`, `extract`) now run sonnet via
  `pick_model("sonnet", task="deep-research.parse")` — cheaper bulk work without
  touching the reasoning quality. The model family lives in
  `multiplai_core.env.CURRENT_MODEL` (no dated literal to go stale), both tiers
  are capped by the `MULTIPLAI_MODEL` ceiling, and each is retunable per task via
  a `[deep-research]` / `[deep-research.parse]` section in `multiplai.conf`.
  `--model` still overrides every node.
