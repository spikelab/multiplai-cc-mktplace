# Changelog

All notable changes to the **multiplai-research** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-research@<version>`.

Recorded history starts at **0.2.0**; anything earlier is in `git log` only.

Of the 4 versions recorded here, `0.5.0` carries a git tag — the tagging
convention started partway through. Dates on untagged versions are the release
dates recorded at the time, not derived from a tag.

`multiplai-research@0.1.1` predates this file and has no section here.

## [Unreleased]

### Added
- **A plugin README** (`plugins/multiplai-research/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.
  Not yet in a released version.

## [0.5.0] - 2026-07-26

### Security
- **Fetched page text is now treated as untrusted input**
  (`research_pipeline/untrusted.py`). Every page this pipeline reads was written
  by someone who is not the user, and it goes into a prompt inside an
  `<untrusted-content>` fence — which is only a boundary as long as the content
  cannot close it. `defang_untrusted()` strips control/ANSI/zero-width/bidi
  characters and HTML-escapes the fence markers, so a page embedding
  `</untrusted-content>` (or a code fence, or a chat-role prefix) cannot promote
  itself from data to instruction. Wording is otherwise untouched — the extractor
  has to see what the page actually said, including an injection attempt it is
  asked to report. Applied in `nodes/read.py`; the instruction half lives in
  `prompts/extract.py` and the `deep-research` / `extract-insights` SKILL.md
  files. Full convention:
  [`docs/untrusted-content.md`](../../docs/untrusted-content.md).

### Added
- **Model × effort as two config axes** (`research_pipeline/config.py`). Per-node
  model tier *and* reasoning effort are both settable from `multiplai.conf` with
  no code edit — `[deep-research]` for the whole pipeline, `[deep-research.<node>]`
  (`parse`, `extract`, …) to override one node. The `MULTIPLAI_MODEL` /
  `MULTIPLAI_EFFORT` ceilings still cap the result, so a budget run forces
  everything down and a conf override cannot escape it.

## [0.4.0] - 2026-07-19

Released without a CHANGELOG entry at the time; see
[#51](https://github.com/spikelab/multiplai-cc-mktplace/pull/51) —
deep-research hardening: verification loop, adversarial review, cost and cache
visibility.

## [0.3.0] - 2026-07-19

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

## [0.2.0] - 2026-07-10

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
