# Changelog

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
