# Changelog

## 0.6.13 — 2026-07-15

### Fixed
- **Bumped every `multiplai-core` pin to `@v0.8.1`** (across all PEP 723
  scripts and `requirements-dev.txt`). `v0.8.1` floors `claude-agent-sdk`
  to `>=0.2.116,<0.3`; the previously-pinned `v0.6.0` capped the SDK at
  `<0.2`, forcing the 0.1.x line. Against a modern Claude CLI (`>=2.x`)
  that SDK misparses the terminal result message and raises the
  deterministic `Claude Code returned an error result: success` *after* a
  full generation — the failure that silently broke `dream` (and which any
  other `[sdk]` consumer — `extract_learnings`, `backfill`,
  `context_manager`, `generate_catalog`, `checkpoint_writer`,
  `synthesize_now` — would have hit next). Restores pin consistency (the
  `test_core_pin_consistency` drift-guard was red: `dream` had moved to
  `v0.8.0` while every other script lagged at `v0.6.0`).

### Added
- **`dream` SDK-call failures are now diagnosable from persistent logs**
  (folds in the unreleased 0.6.12 work): `multiplai-core` `v0.8.1`'s
  `setup_logging(propagate_loggers=…)` attaches the dream file handler to
  the `multiplai_core` package loggers, and `dream.py` wraps the
  `_generate_proposal` call so a failed report logs an exception and exits
  non-zero instead of vanishing silently.

## 0.6.11 — 2026-07-14

### Added
- **qmd `http` execution mode.** A third `qmd_mode` (`http`) POSTs an
  authored, typed query to a resident `qmd mcp --http` daemon on the host
  instead of shelling out per prompt. The daemon keeps the embedding and
  rerank models warm in VRAM (no ~12s cold start per prompt) and does the
  fusion + rerank itself; the request is a JSON `searches` array, so the
  ssh bridge's shell-quoting and newline limits no longer apply. New
  options: `qmd_http_url` (default `http://host.docker.internal:8181`),
  `qmd_candidate_limit` (rerank latency dial, default 10), and
  `qmd_min_score` (weak-match cutoff, default 0.30, now applies to all
  modes). `qmd_strategy` is ignored in `http` mode.

### Changed
- **Authored typed queries replace raw-prompt pasting (http mode).**
  Rather than sending the user's whole sentence — which qmd's lexical arm
  ANDs (stopwords and all) at 2× RRF weight, electing junk to rank 1 — the
  lexical arm now carries only the IDF-rarest content words (document
  frequency read from the project-local `.qmd/index.sqlite`), with the
  full prompt on the vector arm and passed as `intent`. Degrades to
  stopword-filtered word order when the index is unreadable.
- **Stopword list extended** with intent/quantifier fillers (`learn`,
  `more`, `understand`, `explain` and morphological variants) that were
  leaking into the lexical keyword arm.

## 0.6.10 — 2026-07-14

### Added
- **`/multiplai-context:config-audit` — subtractive config/rules review**
  (gap B1 of the AI-coding-insights analysis). The skill enumerates the
  active config surface (`$CLAUDE_CONFIG_DIR/CLAUDE.md`, workspace
  `CLAUDE.md`s, `settings.json` env/permissions blocks, hook
  registrations, memory-file standing rules), classifies every standing
  rule as *still-serving*, *obsolete*, or *model-constraining*, and
  writes a removals-first proposal to
  `.multiplai/dreams/config-audit-YYYY-MM-DD.md` for user review. It
  never applies changes — same propose-then-review UX as dream. A new
  90-day SessionStart gate (`_config_audit_gate_open`, state file
  `config_audit_state.yaml` beside the dream state) nudges
  `/multiplai-context:config-audit` when the review falls out of
  cadence. The stamp is deterministic: the skill's final step runs
  `scripts/config_audit.py --stamp` (mirroring `dream.py --stamp`),
  which resolves the data dir via the same `get_paths()` cascade the
  gate uses — never hand-written YAML. Gate semantics: a **missing**
  state file (fresh install) is seeded with `last_run: now` and does
  NOT nudge — the 90-day clock starts at install; a stale (>=90d) or
  existing-but-corrupt state opens the gate (fail-open recovery, like
  the dream gate).

## 0.6.9 — 2026-07-14

### Added
- **Near-instant compaction via summarizer steering.** The PreCompact hook
  now prints a directive to stdout when a valid checkpoint exists — Claude
  Code appends PreCompact stdout to the compaction summarization prompt as
  custom instructions (verified in the CLI 2.1.207 binary; the background
  precompute path honors them too), so the native summarizer emits a
  one-sentence stub instead of a multi-KB summary. The checkpoint rebuild
  (SessionStart source=compact) carries the real state. Safety gates: only
  when checkpointing is enabled, the session is not a child, and
  `checkpoint.md` validates; the pending rebuild marker is written first so
  even a manual /compact below the handoff threshold gets its checkpoint
  re-injected. Any doubt → native summary (silent stdout).

## 0.6.8 — 2026-07-12

### Added
- **Hub session registry.** The lifecycle hooks now maintain a per-session
  JSON entry at `<data_dir>/sessions/<session_id>.json` implementing the
  "hub input contract" of the multiplai hub (spikelab/multiplai-gui,
  `docs/api-contract.md`): identity fields (`session_id`, `hostname`,
  `cwd`, `project`, `workspace`, `started_at`) plus a `last_event`
  stamp — `start` (SessionStart), `stop` (turn finished, session idle
  and adoptable), `notification` (waiting for user input — the hub's
  push trigger; a new Notification hook, the plugin's sixth event), and
  `end` (SessionEnd). Writes are atomic (tmp+rename), read-merge-write
  so hub-written keys survive, and best-effort throughout — the hooks
  never raise. SessionStart GCs entries whose session ended more than
  7 days ago (removing orphaned `.adopt` markers with them) and the
  data-dir `*` gitignore keeps registry files untracked by mechanism.
  Degradation: works identically with or without docker or the kit;
  with no hub installed the files are simply never read.

## 0.6.6 — 2026-07-10

### Added
- **Memory-vs-session conflict surfacing.** Every injected `=== MEMORY ===`
  block now opens with a directive requiring the model to cross-check the
  retrieved memory against the rest of the session's context (documents,
  pasted files, other injected context, user statements) and, on any
  disagreement, explicitly surface it to the user — naming the memory
  file, presenting both versions, and stating which source it follows
  (newer/in-session wins by default). Each memory file is additionally
  stamped with its last-updated date so the model has a concrete recency
  signal to judge staleness: the in-content `**Last Updated:**` header
  (maintained by the dream tooling) is preferred, falling back to
  filesystem mtime — mtime alone lies after a re-clone/checkout, which
  would stamp stale facts as fresh. Applies to both injection paths
  (router picks and the recency fallback), fused in a single renderer so
  the directive and the stamps can't ship separately. Opt out via the
  new `memory_conflict_preamble` option (default `true`; ~90 tokens per
  memory-carrying turn). Detection lives in the model rather than the
  hook because only the model sees the full session context; covered by
  unit tests plus an opt-in live-LLM E2E test (`MULTIPLAI_E2E_LLM=1`)
  that demonstrates a stale memory fact being flagged against a
  contradicting in-session document.

### Internal
- Consolidated the context_manager E2E test harness (sandbox layout,
  catalog writer, subprocess hook runner) into `tests/conftest.py` —
  three test suites carried drifting near-copies.

## 0.6.5 — 2026-07-10

### Added
- **Reviewed dream proposals are archived out of the dreams root.**
  `dream.py --stamp` takes `--archive <proposal-path>` (with
  `--archive-as applied|rejected`, default `applied`): after stamping
  dream state it moves the reviewed proposal into `dreams/applied/` or
  `dreams/rejected/`, collision-safe (`-2`/`-3` suffix instead of
  overwriting a previously archived same-name file) and via plain rename
  (`git mv` would fail on the typically-untracked fresh proposal).
  `--auto` runs self-archive their audit-trail proposal after a fully
  successful apply. Previously applied proposals sat in
  `.multiplai/dreams/` indistinguishable from pending ones.

### Changed
- **dream-remember Step 6 runs on every review, including `none`.** The
  skill now stamps with zero counts and archives the proposal as
  `rejected` when the user declines everything — a fully rejected
  proposal was previously left looking pending forever (its source
  learnings already deleted), so the next run re-presented it. Step 1
  explicitly scopes proposal discovery to the dreams root (never
  `applied/`/`rejected/`) and pins the exact proposal path for Step 6 so
  a concurrent session's newer proposal can't be archived by mistake;
  the Step 8 summary no longer claims an archive on runs where none
  happened.

## 0.6.4 — 2026-07-09

### Added
- **Config knobs now do what the README promises.** `catalog_model_diary`
  is wired: the diary catalog generator runs on its own model
  (`effective_diary_model`) instead of inheriting the generic
  `catalog_model`. `catalog_ttl_hours` is wired on the read path:
  `context_manager` emits a once-per-session advisory staleness warning
  when a loaded catalog's `generated_at` exceeds the TTL (a cheap
  timestamp compare — it never regenerates inline; run
  `/refresh-catalogs` for that).
- Drift-guard tests: the `multiplai-core` pin is now asserted consistent
  across every script's PEP 723 metadata and `requirements-dev.txt`, and
  every shipped skill is asserted to have a README command-table row.

### Changed
- **`session_state.json` writes are atomic and key-preserving.**
  `session_start` and `session_stop` now go through the atomic
  temp+rename helper and read-merge rather than overwrite, so a session
  start/stop no longer drops a concurrent session's `turn_index` /
  `recently_injected` cooldown state.
- README command table completed with the `now`, `log-doctor`, and
  `costs` skills; `marketplace.json` version realigned with `plugin.json`
  (had silently lagged at 0.6.1).

### Fixed
- **qmd SSH remote string fully sanitized.** `build_argv` now vets and
  single-quotes the workspace path (from the hook cwd) and the collection
  name (from config), not just the query — an unsafe value now yields no
  command (fail-open) instead of risking shell injection over the bridge.

### Removed
- **`catalog_reasoning_effort`** config option (and its README/schema
  entries). It was validated and documented but never consumable: the
  `multiplai-core` `ModelClient.query()` interface has no reasoning/thinking
  parameter, so honoring it would require an out-of-scope cross-repo
  change. Removed rather than left as a false promise.

### Internal
- Added `pytest-timeout` to the dev toolchain (`--timeout` is assumed by
  docstrings/reviewer commands). Defused ~12 dormant
  `pytest.skip("...does not exist yet")` guards for files that now exist —
  a renamed/removed file now fails loudly. Tidied dispatcher
  signature-introspection tests (behavioral return-value check; dropped a
  tautological assertion) and removed a misleading unused `create_client`
  import from `context_manager`.

## 0.6.3 — 2026-07-09

### Changed
- **qmd retrieval entries now carry the matching chunk's line number.**
  qmd matches chunks, not whole documents; injected resource entries
  render as `(score 0.72, line 5) Title` so the model (and the reader)
  can jump straight to the matching chunk instead of skimming the whole
  file. The `qmd-search` skill documents the chunk semantics (`line`,
  `@@` snippet context headers, best-chunk-per-file dedup, `qmd get`).

## 0.6.2 — 2026-07-09

### Fixed
- **log-doctor injection traces use the embedded prompt.** Decision traces
  now print the `prompt` key that 0.5.3 ROUTING_SCORES payloads carry, and
  the footer note claiming "prompts are not logged by context_manager" —
  stale as of 0.5.3 — only appears when the scanned lines actually predate
  the embedded prompt (and now says so).

## 0.6.1 — 2026-07-09

### Added
- **Cost ledger + `/costs` skill.** A new collector
  (`scripts/collect_costs.py` over `scripts/lib/costing_collector.py`)
  incrementally scans Claude Code session transcripts, prices every API
  call (per-model rates + cache tiers, unknown model → fallback), and
  appends to a monthly append-only JSONL ledger under `<data_dir>/costs/`.
  Span attribution follows Skill / Agent / Workflow invocations (sidechain
  subagent traffic flagged and attributed to the innermost agent span;
  nested/ambiguous spans marked `approx`). Offsets are checkpointed and
  records dedup by `msg_id`, so re-runs append nothing new.
- **`costs` skill + `scripts/costs_report.py`** — reports month-to-date
  totals and breakdowns `--by session|skill|project|model|day|component`,
  with `--session` for a per-chat itemized bill and `--json` output.
- **SDK cost tap.** SDK-driven pipelines tag their runs with a `component`
  (buildme, deep-research, dream) so their spend lands in the same ledger.
- **Automatic collection at session start.** With the new `enable_costs`
  option on, `session_start.py` fires the collector detached (like the qmd
  refresh) so the ledger stays current with no manual step. The collector
  self-guards with an flock, so racing session starts can't double-append.
  Opt-in (default off); local-only, nothing leaves the machine.

### Changed
- Pinned `multiplai-core` to **v0.6.0** plugin-wide (adds the `costing`
  module and the `component` cost-ledger tap in `run_agent`). The
  `buildme` and `deep-research` pipeline `pyproject.toml` pins moved to
  v0.6.0 to match their new `component=` call sites.

## 0.6.0 — 2026-07-08

### Added
- **qmd resources retrieval backend (`resources_retrieval=qmd`).** When
  `enable_resources` is on, resources can now be retrieved through a
  [qmd](https://github.com/tobi/qmd) hybrid index instead of the
  catalog+router path: BM25 keyword ladder + vector search fused by
  reciprocal rank (`scripts/qmd_retrieval.py`), per prompt, no LLM in the
  loop. Results render as path + excerpt entries in the existing
  `=== RESOURCES ===` section and respect the re-recommendation cooldown.
  Fail-open throughout: any qmd error/timeout/missing binary means "no
  resources this turn", never a blocked prompt. `catalog` remains the
  default — nothing changes for existing users.
- **New plugin options** — `resources_retrieval` (`catalog`|`qmd`),
  `qmd_mode` (`local`|`ssh` for container→host bridge execution),
  `qmd_ssh_host`, `qmd_collection`, `qmd_strategy`
  (`fused`|`hybrid`|`fts`).
- **Incremental index refresh at session start.** When the qmd backend is
  active, `session_start.py` fires a detached, per-workspace flock-guarded
  child (`scripts/qmd_refresh.py`): `qmd update` + `qmd embed` retry
  passes (embedding can die mid-run but is incremental).
- **`qmd-search` skill** — manual/deep searches against the same index
  (semantic + keyword + hybrid rerank), config-aware for both `local` and
  `ssh` modes. Ships `scripts/setup_qmd.sh`, the one-shot host setup
  (bun + qmd install, `qmd init`, collection add, index + embed, smoke
  query). Container setups additionally need the qmd allowlist in the
  multiplai-container SSH-bridge gateway.
## 0.5.3 — 2026-07-07

### Fixed
- **Post-cooldown relevance re-floor — weak co-picks no longer injected once
  their anchor is suppressed (injection forensics, session 351388d2).** The
  router admits everything within `KEEP_RATIO` (20%) of a corpus's top score,
  so near-floor files ride in as companions of a strong match; when the
  cooldown then suppressed that top scorer, the weak survivors were injected
  alone (e.g. life.md at 3.335 after its 10.8/9.9 anchors were suppressed —
  perceived as "it injected stuff that makes no sense"). Now, when the
  top-ranked pick is itself cooldown-suppressed, survivors must re-clear
  `POST_COOLDOWN_KEEP_RATIO` (0.5) × the suppressed top score or nothing is
  injected; drops are logged as `COOLDOWN_REFLOOR`. Design: chosen over the
  alternative of running cooldown *before* the floor/cap pick because the
  surviving weak tail then forms a fresh ranking whose top can still clear
  the absolute `MIN_SIGNAL` floor — exactly the observed failure. Behavior is
  unchanged when the top pick survives cooldown, when scores are unavailable
  (LLM router), and for unscored bundle/co_retrieve expansion picks.

### Added
- **`ROUTING_SCORES` lines carry the prompt.** Each line's JSON payload now
  includes a whitespace-collapsed, 80-char-truncated `"prompt"` key so
  score→prompt attribution no longer requires digging through session
  transcripts. Embedded in the JSON (not a trailing `key=value`) so existing
  `memory={...}$`-anchored parsers (/health, log tooling) keep working. The
  prompt is already session context per the logging standard's PII rule.
- **`ROUTING_SCORES` emitted for skills and resources corpora.** Previously
  only `memory=` was logged, leaving skill/resource injections with no score
  trail. Memory still logs unconditionally (the /health contract); skills and
  resources log whenever their corpus is enabled.

## 0.5.2 — 2026-07-07

### Added
- **`/log-doctor` skill + `scripts/log_doctor.py`.** Scans the runtime logs
  directory (`paths.logs_dir()`), clusters ERROR/WARNING/INFO entries by
  normalized signature (with traceback tails, first/last seen, counts), runs
  cross-cutting health checks (oversized append-only logs, format drift,
  missing session ids), and supports per-subsystem focus (`--subsystem`),
  recency windows (`--days`), and JSON output. The skill guides root-cause
  verification against source code before writing a fix-recommendation
  report to `INBOX/`. Read-only; the scanner has no LLM dependency.
- **log-doctor probe mode.** Exercise a functionality and assert its expected
  log entries appeared: `--probe-start` snapshots per-file byte offsets,
  `--probe-check --scenario <name>` evaluates only content appended since the
  baseline. Ships grounded scenarios (session-start/end/stop, routing,
  extract-learnings, generate-catalog, synthesize-now, backfill, dream,
  deep-research) plus ad-hoc `--expect SUBSYSTEM:LEVEL:REGEX` expectations;
  unexpected ERRORs from the involved subsystems fail the probe (exit 1).
  Entries now carry their parsed `[component]`, so errors that only surface
  in `hook-errors.log` are attributed to the right subsystem.
- **log-doctor injection forensics (`--injections`).** Reconstructs each
  context-routing decision by joining `context_manager` ROUTING_SCORES /
  COOLDOWN lines with `activity.jsonl` inject events: per-file
  picked/injected/suppressed counts with score stats, cap-hit and abstain
  rates, and `--trace N` full decision traces (`--file X` to focus on one
  file). Explains "why did it inject that" cases — e.g. cooldown suppressing
  the top scorers so near-floor files fill the slots.

## 0.5.1 — 2026-07-07

### Fixed
- **Learning extraction no longer depends on resolution luck (log-doctor F1).**
  All scripts that create a model client now declare `multiplai-core[sdk]`
  in their PEP 723 headers — uv script envs get no host-injected
  `claude-agent-sdk`, so extraction silently lost every session whenever a
  re-resolved env happened to omit it. Core pin bumped to v0.5.1 across all
  scripts (brings in the pytest log-dir guard and hook-errors.log oversize
  truncation from multiplai-core 0.5.1).
- **Diary catalog now actually regenerates during backfill (log-doctor F2).**
  The post-pass called `generate_catalog.main()`, whose `asyncio.run()`
  always raised `RuntimeError` inside backfill's running event loop — the
  "Regenerated diary catalog" branch had never executed (failure was logged
  as a non-fatal warning). Backfill now awaits `generate_catalogs()`
  directly; regression test added.
- **Tests can no longer write into real workspace logs (log-doctor F3).**
  Scripts configure logging at module import, which pytest runs during
  collection — before any fixture. conftest now pins `WORKSPACE` to a
  throwaway temp dir at import time and also scrubs
  `CLAUDE_CODE_AUTO_COMPACT_*` / `CLAUDE_AUTOCOMPACT_*` (ambient
  autocompact steering flipped checkpoint hooks into silent auto mode,
  failing 5 checkpoint tests on steered hosts).
- **Standard-format log lines carry real session ids (log-doctor F5).**
  Hook entry points re-bind `setup_logging` with the payload's session id
  instead of leaving `[session:--------]` on every WARNING+ line.

## 0.5.0 — 2026-07-07

### Added
- **Context checkpointing & rebuild (MiMo-style long-horizon support).** One
  logical chat can now span many physical context windows. The Stop hook
  measures real context footprint from the transcript tail and spawns a
  detached `checkpoint_writer.py` at token bands (default 100K/200K, tuned
  for 1M-window models) producing an incremental 11-field `checkpoint.md`;
  above the handoff threshold (200K) the checkpoint auto-refreshes every
  25K tokens, the user is advised to `/clear` via `systemMessage`, and a new
  `checkpoint_nudge.py` UserPromptSubmit hook tells Claude to wind down at a
  natural boundary. SessionStart consumes a TTL-gated pending marker and
  re-seeds the fresh session from the checkpoint. Goal-safe by construction:
  no `decision` output ever (cannot block /goal loops), child/subagent
  sessions fully excluded, writer failure never blocks the session. New
  `lib/checkpoint.py` core + config via `checkpoint_*` options; docs in
  README ("Context checkpointing"). Verified by a simulated >700K-token
  multi-rebuild E2E suite plus a live hook-subprocess smoke run.
- **Fully-automatic rebuild via steered auto-compaction.** Setting
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`/`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (README
  "Activation") makes native auto-compaction fire near the handoff threshold;
  `SessionStart(source="compact")` then injects the checkpoint into the
  compacted window — same session id, same terminal, `/goal` loops survive,
  zero user action. Same-session marker consumption is permitted only on the
  compact path; band counters reset after every rebuild so each new physical
  window re-checkpoints. In auto mode the `/clear` nudges are suppressed
  (they return only if compaction is overdue/misconfigured).
## 0.4.3 — 2026-07-06

### Changed
- **Bumped `multiplai-core` pin to `@v0.4.0`.** Picks up the library's security
  fix (the no-tools SDK client now also blocks Read/WebFetch/etc. under
  `bypassPermissions`, closing a prompt-injection exfiltration path) plus
  correctness fixes (malformed-timeout env var no longer crashes at import,
  atomic state writes, robust JSON extraction). All entry-point scripts and
  `requirements*.txt` updated.

## 0.4.2 — 2026-07-05

### Fixed
- **Every dispatcher run crashed the diary generator** — the 0.3.x
  `--only`-override feature made the dispatcher pass `force_enable` to every
  generator, but `DiaryGenerator.run()` still had the old signature
  (`TypeError: unexpected keyword argument`). Signature updated; a new
  contract test asserts every registered generator accepts the dispatcher's
  full run() contract, so future overrides can't regress this.

## 0.4.1 — 2026-07-05

### Fixed
- **refresh-catalogs skill doc contradicted the uv migration** — an Operational
  Note still told the session to invoke `generate_catalog.py` with bare
  `python` via the removed managed-venv self-routing, causing
  `ModuleNotFoundError: multiplai_core`. All skill docs now consistently
  mandate `uv run --no-project`. Also scrubbed stale `venv_dir`/`python
  dream.py` references from the health and dream skill docs.

## 0.4.0 — 2026-07-05

### Changed
- **uv + PEP 723 runtime.** The managed plugin venv (`venv_bootstrap`/`venv_guard`)
  is gone. Every entry-point script carries inline dependency metadata and runs
  via `uv run --no-project`. Shared modules (`paths`, `config`, `log_utils`,
  `model_client`) moved to the `multiplai-core` package, consumed as
  `git+https://github.com/spikelab/multiplai-core@v0.2`.

### Added
- **Installed-plugin skill discovery.** The skills catalog now also indexes
  skills shipped by installed Claude Code plugins (the themed Multiplai packs:
  pm, writing, research, dev, media) via `installed_plugins.json`, in addition
  to `skills_dir`. New `plugins_dir` option (empty = `$CLAUDE_CONFIG_DIR/plugins`).


### Changed
- **`refresh-catalogs --only <gen>` now honors its override contract.** An
  explicit `--only` filter has always been documented as running a generator
  regardless of config gating, but each generator's `run()` still re-checked
  its own `enable_*` flag and silently no-op'd — so `--only resources` did
  nothing when `enable_resources=false`. The dispatcher now threads a
  `force_enable` signal into the gated generators, so an explicitly-named
  generator runs even with its flag off (the `resources_dir` requirement is
  still enforced). This lets you keep a catalog fresh without turning on
  injection. Documented in the `refresh-catalogs` skill and README, with
  operational notes (managed-venv self-routing, `exit 1` = partial errors,
  don't `pkill -f generate_catalog`).
- **Resources catalog indexes Markdown only.** `ResourcesGenerator.discover_sources()`
  now allowlists `.md`/`.markdown` and skips dotfiles and binaries (PDFs,
  images, archives, scripts, raw `.txt`), so the routing surface is no longer
  diluted by placeholder "binary file" entries.

### Added
- **Action Items — a third dream disposition.** A learning that asks the
  toolchain to change its own code/config/file-structure ("split these files",
  "delete this orphan", "use install.sh in the Dockerfile") is no longer
  mis-filed as memory. The dream proposal now has a `## Action Items` section
  (`A{N}` numbering, What/Why/Source). On approval, `/multiplai-context:dream-remember`
  writes them to `PLANS/dream-actions-{date}.md` as unchecked tasks, so they
  survive the learnings cleanup and become durable work. A learning that
  carries a durable general *principle* alongside the change keeps **both** —
  the principle as memory and the change as an action item.
- **Bounded critic second pass over the dream proposal.** After the draft, a
  cheap second LLM pass (over the proposal only, not the raw backlog)
  surgically strips point-in-time residue (commit SHAs, `Decision (date):`
  framing, finished-task imperatives, one-off paths), demotes past-event
  records to Filtered Out, and reroutes mis-filed action items. Falls back to
  the raw draft on failure.
- **`filename:line` provenance.** Each proposal entry ends with a `**Source:**`
  line citing the learnings file and line number it was distilled from
  (pending learnings are now fed to the model with line-number prefixes so the
  citation is accurate, not guessed) — so a report is traceable on re-processing.

### Changed
- **Dream now distills generalized, reusable knowledge — not a session log.**
  The proposal prompt is built around an explicit DIARY-vs-MEMORY distinction:
  the diary already records what happened; memory holds guidance that changes a
  *future, different* task. Entries are generalized ("when X, do Y"), with the
  point-in-time scaffolding stripped. Report noise removed: no per-file
  learning counts, `seen Nx` notes, or trust labels (weak items get a
  `[warning low confidence]` marker instead).
- **`/multiplai-context:dream --auto` uses the same generalization pass as
  report mode.** Auto mode previously ran a thin per-file prompt with none of
  the above discipline. It now generates the same proposal (same prompt +
  critic), writes it to `.multiplai/dreams/` for audit, then mechanically
  applies each file's slice concurrently.
- **`router_model` option for the `llm` router** (default `claude-haiku-4-5`).
  The LLM router now forwards a model to the client; Haiku keeps the
  per-prompt classification cheap. The `UserPromptSubmit` hook timeout is
  raised to 15s (router timeout 12s) so an inline `llm` call can complete.
  See the README "Router latency" note — `llm` runs ~7-10s/prompt via the
  Agent SDK (CLI cold-start per call) and is best treated as a routing-
  quality experiment until routing moves off the blocking hook.
- **Re-recommendation cooldown.** After a file is injected, it is
  suppressed from re-injection for `recommend_cooldown_turns` turns
  (default `4`; `0` disables) — it's already in the conversation, so
  re-injecting wastes context. A turn counter and a `recently_injected`
  map persist in `data/session_state.json`; the `PreCompact` hook clears
  the map so post-compaction every file is eligible again. An
  all-suppressed turn logs `on cooldown, nothing injected` and is
  distinct from router abstention (no recency fallback). New
  `recommend_cooldown_turns` userConfig option.

### Changed
- **Activity-log `[context] inject` lines now group injected files by
  corpus** — `→ memory: … · skills: … · resources: …` instead of one
  flat comma-separated list, so you can tell which files came from which
  corpus. The JSONL mirror gains a `files_by_corpus` field.

### Fixed
- **Skills were routed but never injected.** The loader called
  `read_text()` on the skill *directory* instead of `<name>/SKILL.md`
  (the real Claude Code layout), so the skills count was always 0.
  Skills are now surfaced as lightweight recommendations (catalog
  summary + `/<name>` invocation hint) rather than full SKILL.md bodies.
- **`anti_domains` hard-excluded relevant entries across all corpora.**
  Anti phrases that reuse the entry's own positive vocabulary (e.g.
  "…unrelated to memory routing") nuked the entry on the very tokens
  that made it relevant. Anti tokens that are also the entry's
  `intent_domains` vocabulary are now dropped before the exclusion check.

## 0.3.0 — 2026-05-21

Diary layout aligned with learnings — one file per UTC day.

### Changed (breaking on-disk layout)
- **Diary now uses per-day files**, matching the learnings layout. Each
  ``YYYY-MM-DD.md`` file under your diary dir holds one ``# Diary``
  header plus one ``## Session: <id> — <ts> — <cwd>`` block per session
  that ran on that day. The previous ``YYYY-MM-DD/<sessionId>.md``
  per-session layout is gone.
- Why: easier to browse (`ls diary/` shows ~365 entries/year instead of
  thousands), consistent with learnings, append-only with `fcntl.flock`
  for concurrent SessionStart subprocesses.
- Idempotent on session_id: re-extracting the same session is a no-op.
- The diary catalog generator now iterates ``*.md`` files at the top of
  the diary directory; one catalog entry per day, same schema as before.
- Health check renamed ``diary.entry_count`` → ``diary.day_count`` to
  reflect what it actually measures.

### Migration
- No public users existed on the pre-0.3.0 layout, so no user-facing
  migration tool ships. The internal migration was a one-shot script
  applied to existing on-disk diaries during development and discarded.

## 0.2.1 — 2026-05-21

First public-marketplace-ready release. Focused on safety, transparency,
and onboarding rather than new features.

### Fixed
- **`UserPromptSubmit` hook can no longer crash your session.** The
  context-routing hook now wraps all work in a top-level guard: any
  unhandled error logs to `hook-errors.log` and emits a safe empty
  context. Previously a single failing file read could surface a
  traceback mid-prompt.

### Added
- One-time warning at session start when neither the Agent SDK nor an
  Anthropic API key is configured — so LLM-backed features (extraction,
  dreams, catalogs) silently no-op'ing is no longer a mystery.
- README: **How it works** (4-step lifecycle), **Where your data lives**
  (what gets written where + `.gitignore` snippet), **Quick start: the
  only options you probably need** (cuts the 18-option config wall down
  to 3-4 that matter).
- Platform support note in README — macOS / Linux / WSL on Windows;
  native Windows isn't supported.

### Changed
- `anthropic` dependency pinned to an exact version for reproducible
  installs.

## 0.2.0 — 2026-05-17

Internal release. Highlights for users:

- **Runtime state moved next to your memory.** Logs, catalogs, the
  plugin venv, and dream state now live at `<workspace>/.multiplai/data`
  instead of Claude Code's managed plugin dir. Side effect: routing
  catalogs are now actually loaded by default (they were silently
  unreachable before). New `data_dir` config option to override.
- **Tail-friendly logs.** Every log file rotates daily into
  `<name>-YYYY-MM-DD.log`, with `<name>.log` always pointing at today.
  Retention controlled by `MULTIPLAI_LOG_RETENTION_DAYS` (default 7).
- **`activity.log` curated stream.** One plain-language line per
  meaningful action — context injected, dream nudge, session boundary,
  diary write, learnings capture, catalog rebuild. Tail with
  `tail -f <data>/logs/activity.log`.
- Test suite hardened so it never inherits the host workspace's
  `CLAUDE_PLUGIN_*` / `WORKSPACE` env.

### Observability

- `log_utils.py` rewritten to the project logging standard: UTC ISO-8601
  lines with `[component] [session:xxxxxxxx] LEVEL:` shape, env-driven
  level (`MULTIPLAI_DEBUG=1` or `MULTIPLAI_LOG_LEVEL`), and a shared
  `hook-errors.log` for ERROR+ across all components.
- **Uniform date-rotation across every log.** `<name>.log` is always the
  *current* file; on the first write of a new UTC day it rotates to
  `<name>-YYYY-MM-DD.log` (date infix *before* the extension, per the
  standard). The stdlib `TimedRotatingFileHandler` was emitting the
  rejected `<name>.log.YYYY-MM-DD` form; legacy files in that shape are
  auto-migrated. Retention is `MULTIPLAI_LOG_RETENTION_DAYS` (default 7,
  `0` = keep forever), applied uniformly to every rotated file.
- New `log_event()` curated activity stream — one plain-language line per
  meaningful action in `activity.log` (current), mirrored to
  `activity.jsonl` for tooling, both rotating to `activity-YYYY-MM-DD.*`
  the same way. Previously these were always date-stamped with *today*,
  so there was never a stable current file to `tail -f`. Written
  regardless of log level; never raises into a hook.
- Lifecycle scripts instrumented: context inject/skip/fallback (with the
  exact files loaded), dream nudge, session start/end/pre-compact, diary
  write, learnings capture, catalog rebuild (+entry count and timing),
  deferred-extraction launch.
- README "Observability" section: live-watch command, debug toggle, log
  layout, retention.

### Fixed

- **`data_dir` is now workspace-anchored.** Previously `paths.py`
  resolved `data_dir` from `CLAUDE_PLUGIN_DATA`, so logs/catalogs/venv/
  dream-state landed in Claude Code's per-install managed dir —
  split away from `<workspace>/.multiplai/` where memory/diary/learnings
  live (and contradicting the in-code comment). Now: explicit `data_dir`
  option → `<workspace>/.multiplai/data` → `CLAUDE_PLUGIN_DATA` (only
  when no workspace) → `~/.multiplai/data`. New `data_dir` userConfig
  option. As a side effect this also resolves the router always falling
  back (the managed dir had no `catalogs/`).
- Test suite hardened: an autouse fixture scrubs ambient
  `CLAUDE_PLUGIN_*`/`WORKSPACE` so tests never inherit the host
  workspace.

## 0.1.0 — 2026-05-16

Initial public release of the **multiplai** context-manager plugin,
distributed via the `multiplai` Claude Code marketplace.

### Plugin

- `.claude-plugin/plugin.json` with `userConfig` (workspace/memory/diary/
  now/learnings dirs, sensitive `anthropic_api_key`, catalog & router
  options) and an explicit `hooks` declaration.
- Official Claude Code hooks schema (`hooks/hooks.json`): `SessionStart`
  (venv bootstrap + session init), `UserPromptSubmit` (context routing),
  `Stop` (lightweight checkpoint), `SessionEnd` and `PreCompact`
  (deferred-extraction markers).

### Core

- Path resolver with plugin-env → workspace → standalone cascade; all
  runtime state resolves through it (catalog generators included).
- Model client abstraction: Agent SDK (zero-config) with Anthropic
  API-key fallback; empty-content responses handled gracefully.
- First-run virtualenv bootstrap (`uv` preferred, `pip` fallback).
- Routed, per-prompt memory injection (`token_overlap` or `llm`
  strategy). `SessionStart` no longer dumps the full memory corpus.
- Diary-first learning extraction (brace-safe prompt construction);
  per-session diary, per-day learnings.
- Catalog generation (memory, diary, optional skills/resources) with
  content-hash incremental regeneration.
- Dream consolidation: report mode by default
  (`/multiplai-context:dream` → proposal in `.multiplai/dreams/`), opt-in
  `--auto` with memory-scoped git auto-commit.

### Skills

`/multiplai-context:setup`, `/multiplai-context:dream`, `/multiplai-context:dream-remember`,
`/multiplai-context:health`, `/multiplai-context:memory-health-audit`,
`/multiplai-context:refresh-catalogs`, `/multiplai-context:backfill`.

### Templates

Starter `me.md`, `technical-pref.md`, `preferences.md`.
