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

Nothing yet.

## [0.6.2] - 2026-08-05

### Security

- **deep-research no longer installs `cryptography` 49.0.0 (CVE-2026-69247,
  high).** If you have deep-research installed, updating to 0.6.2 is the fix —
  the next run resolves `cryptography` 50.0.0. There is nothing to change in
  your own config.

  What went wrong: `skills/deep-research/scripts/` shipped its own `uv.lock`,
  left behind when this repo consolidated onto a single workspace lock. After
  that consolidation nothing could regenerate it — `uv lock` run from inside
  the skill resolves the whole workspace and rewrites the *root* lock, never
  the nested one — so it stayed frozen on the day's versions while the root
  lock moved on. In this repo that was invisible, because in-repo runs use the
  root lock. On *your* machine there is no workspace above the plugin, so
  `uv run --project` found the frozen lock and used it. Dependabot filed the
  advisory correctly and opened patches; those patches edited the nested lock,
  which is the one file that could not be updated.

  The nested lock is now deleted, so an installed deep-research resolves
  against the declared dependency ranges and picks up patched versions on its
  own. A new repo gate (`lint_workspace.py` → nested lockfiles) fails CI if a
  lockfile ever reappears outside the workspace root.

## [0.6.1] - 2026-08-04

### Fixed

- **`deep-research`'s pipeline resolves on an installed copy of the plugin.**
  Its `scripts/pyproject.toml` now declares its own git source for
  `multiplai-core`, so `uv run --project <skill>/scripts` works standalone —
  an install is a copy of the plugin subtree only, with no marketplace
  workspace above it to resolve through.

### Added
- **A plugin README** (`plugins/multiplai-research/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.

## [0.6.0] - 2026-08-04

### Changed
- **`deep-research` takes its dependencies from the repo-root uv workspace.**
  Its own `scripts/uv.lock` is gone, replaced by one lock covering every plugin
  in the marketplace. Nothing changes about what the pipeline does; if you
  invoke it by hand, use `uv run --project <repo-root>` rather than running
  from inside `scripts/`, which no longer resolves a project of its own.

  This also removes a whole class of the problem that caused 0.5.2: one plugin
  can no longer sit on a stale lock while its siblings are patched, because
  there is only one lock to keep current.

## [0.5.2] - 2026-07-30

### Fixed

- **deep-research was resolving a `claude-agent-sdk` old enough to fail every
  run against a current Claude CLI.** If you have seen the pipeline die with
  `Claude Code returned an error result: success` *after* a full generation —
  the work done, then thrown away — this is why. The 0.1.x SDK line misparses
  the terminal result message emitted by Claude CLI 2.x, and it is
  deterministic, so the retry wrapper could never rescue it.

  The pipeline reaches the SDK only through `multiplai_core.run_agent()`, and
  `multiplai-core` has long pinned `claude-agent-sdk>=0.2.116,<0.3` in its
  `[sdk]` extra. deep-research never asked for that extra — it listed
  `claude-agent-sdk>=0.1.0` beside core as a dependency of its own, which
  silently undercut core's floor and let the resolver settle on **0.1.56**.

  The dependency is now `multiplai-core[sdk]`, so the floor is stated once, in
  the package that owns it, and cannot drift again. Resolves to 0.2.128. No
  configuration change and no API change on your side.

  This is the only package that moved; the pin on `multiplai-core` itself
  stays at `v0.10.0` deliberately, per the repo's rule that pins are bumped
  per-consumer and only when tested.

## [0.5.1] - 2026-07-27

### Changed
- **`deep-research`'s page-text defanging now comes from `multiplai-core`.**
  `research_pipeline/untrusted.py` stays as the pipeline's seam — `nodes/read.py`
  still imports `defang_untrusted` from it — but the regexes behind it are the
  shared ones. Behaviour is unchanged: fetched pages keep `markdown_fences=False`
  and no injection marking, so a page about shell scripting is not corrupted and
  the extractor still sees an injection attempt in the page's own words, which is
  what it is asked to report. Core pin moves `v0.7.0` → `v0.10.0`
  (`pyproject.toml` + `uv.lock`).

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
