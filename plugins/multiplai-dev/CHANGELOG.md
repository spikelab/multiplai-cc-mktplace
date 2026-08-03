# Changelog

All notable changes to the **multiplai-dev** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-dev@<version>`.

Recorded history starts at **0.1.1**; anything earlier is in `git log` only.

Of the 13 versions recorded here, `0.1.1` and `0.5.0`–`0.5.3` carry a git tag —
the tagging convention started partway through. Dates on untagged versions are
the release dates recorded at the time, not derived from a tag.

## [Unreleased]

### Added
- **A plugin README** (`plugins/multiplai-dev/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.
  Not yet in a released version.

## [0.6.0] - 2026-08-03

### Added

- **`/buildme` now updates your documentation as part of the build, so it
  arrives in the same pull request as the code.** After the TDD build and
  before the respec proposal, a new documentation phase reads the whole build
  diff plus the implementation notes, takes inventory of the documents your
  project actually keeps (`README*`, `CHANGELOG*`, `docs/**`), and updates
  whatever the change made stale — described behavior, flags, defaults, file
  layouts, and usage examples that would no longer work. Where your project
  keeps a changelog it adds a [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
  entry written from the user's point of view; it creates a `CHANGELOG.md` only
  when your project's conventions show one is expected. Previously a build
  shipped code and left the docs for whoever reviewed the PR.

  What it writes lands in its own `docs(<change>): update documentation` commit,
  and the files are named in the PR body under **Documentation** so a reviewer
  knows the docs were rewritten rather than assuming they were untouched.

  The phase is **always on** — there is no flag and no `specs/config.yaml`
  toggle — and it is **non-fatal**: if the model call fails, the build still
  completes with the code it produced. It also does not pad. "Nothing needed
  documenting" is a legitimate outcome for a change with no user-visible delta,
  and the agent says so explicitly.

- **A new `DOCS_WARNING:` line when the documentation may be stale.** When the
  build changed source files, your project keeps a changelog, and the phase
  updated nothing, `/buildme` prints a warning naming the changed files and the
  changelog to check before merging. It is a **warning and never a failure** —
  a finished build is not failed over a judgment call about documentation.

### Changed

- **`PHASE:DOCS_UPDATE:COMPLETE` joins the progress protocol**, and the kanban
  card stays in **In Development** through the documentation phase (updating the
  docs is part of producing the change, not a separate review stage).
- Builds resumed from a checkpoint written by an earlier version load and run
  the new phase; a build already past the respec proposal does not go back for
  it.

## [0.5.4] - 2026-07-30

### Security

- **buildme's locked dependencies carried 21 known vulnerabilities (9 high);
  all are now patched.** If you run `/buildme`, the pipeline resolves its
  environment from `skills/buildme/scripts/uv.lock`, so these were the
  versions actually executing on your machine. Update the pack to pick up the
  fix — there is nothing else for you to do.

  Every advisory was in a *transitive* dependency, reached through
  `claude-agent-sdk → mcp` and its sub-tree; no dependency buildme declares was
  itself affected. The lock had simply gone a long time without regeneration.

  Worth knowing when judging urgency: most of these advisories describe
  **server-side** attack surface — MCP's HTTP and WebSocket transports,
  Starlette request handling, multipart parsing of untrusted uploads. buildme
  is a local CLI that acts as an MCP *client* and never binds a socket, so
  that surface was not reachable in normal use. The two with local relevance
  were a vulnerable OpenSSL bundled in `cryptography`'s wheels and a PyJWT
  algorithm-confusion flaw (CVE-2026-48526). Patch promptly; no emergency.

  Upgraded: `click` 8.3.2→8.4.2, `cryptography` 46.0.7→49.0.0, `idna`
  3.11→3.18, `mcp` 1.27.0→1.29.0, `pydantic-settings` 2.13.1→2.14.2, `pyjwt`
  2.12.1→2.13.0, `python-multipart` 0.0.26→0.0.32, `starlette` 1.0.0→1.3.1,
  `sse-starlette` 3.3.4→3.4.6, `uvicorn` 0.44.0→0.52.0.

  Deliberately *not* upgraded in the same change: `anthropic`,
  `claude-agent-sdk`, `pydantic` and `pytest`. A blanket refresh resolves
  cleanly but moves the pipeline's own runtime, which is a separate risk from
  a security patch and should be reviewable separately. No behaviour change
  and no API change in this release.

## [0.5.3] - 2026-07-27

### Changed
- **buildme validates effort names against `multiplai-core`'s table instead of
  a copy of it.** 0.5.2 moved the effort *logic* into core's `pick_effort` but
  left the list of valid names hand-mirrored in `build_pipeline/config.py`.
  Core v0.11.0 exports that table, so the copy is gone. No behaviour change
  today — the two lists agreed — but the drift this removes had a nasty shape:
  had they ever disagreed, a `[buildme] EFFORT=<name>` that buildme thought
  valid and core did not would have been resolved to `high` instead of falling
  back to your default, with no warning. Nothing to do on your side.
- **Pin: `multiplai-core` v0.9.0 → v0.11.0** (`pyproject.toml` + `uv.lock`) for
  the exported table. buildme is the only skill in this pack that pins core.

## [0.5.2] - 2026-07-26

### Changed
- **buildme: conf-file effort overrides now delegate to `multiplai-core`'s
  `pick_effort`** instead of a local duplicate, and the pipeline's core pin
  moves `v0.7.0` → `v0.9.0` (additive releases only; the vendored `uv.lock`
  moves with it). One behavior change: a `MULTIPLAI_EFFORT` *global in
  `multiplai.conf`* now caps `[buildme] EFFORT=` overrides the same way the
  `MULTIPLAI_EFFORT` env var always did — the two ceilings no longer disagree.
  Defaults are untouched: with no conf `EFFORT=`, effort stays unset and the
  SDK decides, uncapped, exactly as before.

## [0.5.1] - 2026-07-26

### Added
- **swift-build: `uitest` + `screenshots` verbs** for GUI self-verification
  (#55) — run XCUITest bundles and capture app screenshots from the container,
  with rm-rf guards on result-bundle and screenshot output paths.

### Fixed
- **buildme: explainer detector precision** (#74) — dotted module paths
  collapse to their declared distribution prefix, and builtins/stdlib members
  no longer surface as "new dependencies".

## [0.5.0] - 2026-07-26

Verification overhaul for buildme. The theme: reviewers **propose** and the
orchestrator **disposes**; the tests that gate a block must still be the tests
that gated it; and spend is bounded by money, not just by iteration counts.

The same release carries the skill-engineering gate (#61) — a proposed skill is
executed before it is installed — and the model × effort config axis for buildme
(#59).

### Added
- **Test-integrity gate** (`gates.unchanged_tests_gate`). The implementer's
  tool grant is per-tool, not per-path, so test files stay writable for the
  whole implement phase — and the review-fix agent is the same implementer, so
  every fix iteration re-opens the window. Buildme now sha256-hashes the
  block's test files when the RED gate passes and re-checks them at both
  windows. A silent change fails the block. `TEST CHANGE REQUIRED: <reason>`
  in the implementer's report downgrades it to a flag, re-baselines the
  hashes, and hands the reason to the reviewer as an unverified claim.
- **Structured findings + orchestrator adjudication.** Reviews emit discrete
  `ReviewFinding` claims (claim, severity, confidence, evidence, location).
  `_adjudicate_review_findings` re-judges each one on the main model with the
  full build context that a fresh-context reviewer structurally lacks;
  rejected findings are recorded and never reach a fix agent. Fails open — an
  adjudicator error keeps every finding. `code_review.adjudicate: false`
  **drops** findings rather than applying them blind.
- **Reviewer panel** (`code_review.panel`). Each member reviews the diff in
  its own fresh context, concurrently, and the results merge: dimension scores
  average with confidence scaled down by panel disagreement; identical
  findings combine confidence (noisy-or) and keep the harshest severity
  anyone assigned; spec verdicts are unioned, not intersected.
- **Graded review gate** (`code_review.gate` → `ReviewGatePolicy`). Dimension
  scores are discounted toward neutral by reviewer confidence, so an unsure
  reviewer moves the needle less. Spec compliance stays a hard floor. The
  3.5/1.0 thresholds now live in one place instead of being duplicated
  between `review_score_gate` and `ReviewResult.passed`.
- **Trajectory judgment in the final review.** The final reviewer receives a
  per-block history (review iterations, final weighted scores, rejected
  findings, declared test changes) and is asked to judge drift, scope creep,
  erosion, and test gaming across the whole build — the failures that are
  invisible to per-step review because they are spread thin.
- **Per-build budget** (`budget.max_tokens` / `budget.max_usd`). Every other
  loop bound is an iteration count, which does not bound spend. Spend is
  recorded per phase on every SDK call, checked at block boundaries, warns
  once at 80%, and stops with a breakdown of where the tokens went. It rides
  in `.build-state.json`, so a resumed build does not get a fresh budget.

- **Promotion gate for `/propose-skill` — run the draft, don't vouch for it**
  (`skill-creator/scripts/promote_skill.py`, #61). An approved skill is now
  written to a **draft** directory (`/tmp/skill-draft/<name>/`) and installed
  only after the gate passes. The gate checks frontmatter (required keys, known
  `model`/`effort` values — a typo there is silently ignored at runtime, so the
  skill quietly runs on the wrong tier) and **executes every bundled entry point
  with `--help`, expecting exit 0**. Until this existed, the first person to
  discover a bad import or a raising `--help` was whoever hit it mid-task, days
  later, with the authoring context gone.
  - `--contract` mode runs a skill's `CONTRACT.md` assertions: each case is a
    shell command plus a substring that must appear in its output. Pilots ship
    with multiplai-context's `costs` and `log-doctor`.
  - `quick_validate.py`, `init_skill.py` and `package_skill.py` updated to the
    same frontmatter rules; bundled `.sh` entry points across the marketplace
    now answer `--help` with exit 0 because the gate executes them.
- **`/propose-skill --from-session` — draft from a real trajectory** (#61).
  Diary entries and learnings are *summaries*: they record that a workflow
  happened, not the commands, flag values and dead ends it went through, so a
  skill drafted from them reads plausibly and fails on first use. This mode reads
  the raw transcript and splits what it finds into **stable steps** (literal
  commands) and **judgment points** (criteria for deciding, never a hardcoded
  answer). A step counts as stable only if it was seen done the same way for a
  *different* input — one occurrence is an anecdote. Dead ends are recorded too:
  the command that failed, and why, cannot be reconstructed from the successful
  path.
- **Model × effort as two config axes for buildme** (`config.py`, #59). Both are
  set from `multiplai.conf` with no code edit: `[buildme]` (`MODEL=`, `EFFORT=`)
  for the whole pipeline, plus `[buildme.spec]`, `[buildme.review]` and
  `[buildme.agent]` for per-step `EFFORT=`. A step section falls back to
  `[buildme]`, which falls back to the SDK default. The `MULTIPLAI_MODEL` /
  `MULTIPLAI_EFFORT` ceilings still cap the result, so a conf override cannot
  escape a budget run.
- **Model-upgrade re-test checklist** (`e2e-test/SKILL.md`, #61). Skills are
  prompts, and a prompt tuned against one model is not automatically correct
  against the next — a bump can change output format, verbosity or how literally
  an instruction is followed, none of which raises an error. Run on any
  `MULTIPLAI_MODEL` ceiling change, `model:`/`effort:` frontmatter change, or
  Claude Code default-model move: smoke-invoke every script, run the `CONTRACT.md`
  assertions, re-check the frontmatter tier against the cost report.

### Changed
- `ReviewScore` gained `confidence` (default 1.0) and `effective_score()`.
  Confidence shrinks a score *toward neutral* rather than scaling it down —
  scaling would make an unsure reviewer look harsher, inverting the meaning of
  low confidence. Existing fixtures at the default confidence score exactly as
  before.
- All `llm_call`/`agent_call` sites pass a `budget_label`, so a budget stop can
  name the phase that spent the money.

### Fixed
- **Test-integrity claims are scoped to one window.** `block.implementer_report`
  accumulates every agent's output, so a `TEST CHANGE REQUIRED:` declared during
  the implement phase kept passing the claim parse at every later review-fix
  iteration — one declared change authorized every later silent one, defeating
  the re-baseline it sits next to. Each window now sees only its own agent's
  report.
- **An unavailable test-file list no longer fails the block.** `git` failing
  (index.lock contention, timeout on a large repo) made every snapshotted file
  read as deleted and accused the agent of deleting the suite. The snapshot now
  distinguishes "listed nothing" from "could not list" and reports the gate as
  not-checked.
- **The integrity gate covers non-Python projects.** The path pattern was
  Python-only, so on a Swift/Go/TS repo nothing matched, the snapshot was empty,
  and the gate reported "not checked" for every block. Hashing now uses a
  language-agnostic pattern; the `def test_*` weak-test scan keeps the
  Python-only one.
- **A failing reviewer panel member no longer fails the block.** The panel
  gathered without `return_exceptions`, so N members meant N× the chance that
  one unreachable backend marked the block FAILED. Failed members are dropped
  with a warning and only an all-members-failed panel raises.
- **Failed `llm_call`s are charged to the budget.** Only `agent_call` recorded
  partial usage on failure, so a review that timed out after a 150k-char prompt
  spent real money invisibly.
- `ReviewResult.passed_with(policy)` added; the bare `passed` property is
  documented as the default-policy view and `run_code_review` now logs the
  configured verdict. With `code_review.gate` set, the logged verdict and the
  gate's decision no longer disagree.
- **`EFFORT=xhigh` is accepted.** `config.KNOWN_EFFORTS` mirrors core's private
  `_EFFORT_TIERS` and omitted `xhigh`, which sits between `high` and `max` — so a
  valid SDK effort was rejected with a warning and silently downgraded to the
  default. (Core's own table carries a comment warning against exactly this
  omission when the set is copied.)
- `_merge_scores` rounds half-up explicitly (bare `round()` is banker's
  rounding, so a 2/3 split went to 2 and a 3/4 split to 4) and discounts a
  dimension only one panel member scored — zero spread otherwise read as
  unanimous agreement.

### Notes
- Every new behavior is opt-in via `specs/config.yaml`. With no `panel`, no
  `gate`, and no `budget:` section, the pipeline behaves as it did in 0.4.0.

## [0.4.0] - 2026-07-20

Ports enforcement mechanisms from the `superpowers` plugin's methodology skills
into buildme's code-driven pipeline. The theme: buildme structurally enforces
red-green TDD, never marks failing work done, and reviews against a hardened
two-verdict rubric.

### Added
- **RED gate** (`gates.red_gate`, from *test-driven-development*). Between the
  test-writer and implementer phases the pipeline runs the suite and requires a
  non-zero exit failing for the right reason. A passing suite means the tests
  prove nothing (`rewrite_tests`); a collection/syntax error means broken test
  files (`fix_tests`). One retry each, then the block fails. RED and GREEN
  output are captured as block evidence and fed to the reviewer.
- **Two-verdict review** (from *subagent-driven-development*'s task reviewer).
  `ReviewResult` gained a spec-compliance verdict (`missing`/`extra`/
  `misunderstood`) alongside the scored dimensions; passing requires both.
  `CODE_REVIEW_PROMPT` now treats the implementer's report as unverified
  claims, verifies against the ground-truth diff, cites file:line with
  why-it-matters and how-to-fix, and acknowledges strengths first.
- **Integration circuit breaker** with escalation (from *systematic-debugging*).
  Three fix attempts; the third switches to a question-the-architecture prompt
  and escalates to `config.review_model`. The fix prompt carries the four-phase
  debugging protocol (read the complete error, reproduce, one hypothesis, one
  variable at a time). Exhaustion writes a diagnosis to `build-progress.md`.
- **Global Constraints + Interfaces threading.** `design.md` carries a REQUIRED
  `## Global Constraints` section; task blocks carry `Interfaces:`
  (`Produces:`/`Consumes:` exact signatures). Both are injected verbatim into
  agent and review prompts, plus earlier blocks' Produces signatures, so
  implementers use exact signatures rather than re-deriving them. Generated
  tasks are scanned deterministically for placeholders (TBD/TODO/"add
  appropriate error handling"/"similar to block N"), and the tasks audit gained
  spec-coverage traceability and cross-block signature-consistency checks.
- **REQUIRED report slots.** Test-writer and implementer reports close with
  `STATUS:`/`TESTS_RUN:`/GREEN evidence, plus explicit permission to stop
  ("bad work is worse than no work"). `NEEDS_CONTEXT`/`BLOCKED` fails the block
  with the agent's stated reason surfaced.
- **Testing anti-patterns** (from *testing-anti-patterns*). Mechanical
  detection of mock-assertion-only and mock-setup-dominant tests; the
  test-writer prompt and rubric Test Quality dimension gained the five
  anti-pattern checks as positive gate-function criteria.
- `--lenient-review` restores accept-and-continue on review exhaustion for
  unattended overnight runs.

### Changed
- **No silent DONE.** Confirmed-weak tests now fail the block instead of
  warning (the previously-unwired `TEST_QUALITY_PROMPT` auditor adjudicates the
  static scan, with one test-writer retry). Review exhaustion fails the block
  rather than marking it done regardless. `_run_final_review` uses a structured
  verdict over the full-build diff and fails closed — a FAILED verdict fails
  the build, and so does an unverifiable review (an exception yields
  `passed=False` with the error surfaced); neither is marked done, so a resume
  re-runs the review. `_verify_entry_point` actually smoke-runs the detected
  entry point under the repo-trust guard instead of reporting an unverified
  pass.

## [0.3.3] - 2026-07-18

### Added
- **swift-build: `swift`/`xcodebuild`/`xcrun` passthrough.** `swift-host.sh`
  now accepts these as top-level commands and forwards shell-quoted args to
  the host (from the optional `--package-path` dir). Enables host diagnostics
  and repair (`xcodebuild -runFirstLaunch`, `-version`, `-showBuildSettings`)
  and xcodebuild-free simulator builds (e.g. a SwiftPM cross-compile for
  `arm64-apple-ios17.0-simulator`) without leaving the gateway.
  An opt-in `--xcsift` flag (first passthrough arg) pipes the host output
  through the same trusted `2>&1 | xcsift --format toon --quiet` suffix as
  build/test — errors/warnings survive, build noise is dropped. Off by
  default because diagnostic output (`-version`, `-showBuildSettings`,
  `simctl list`) is the answer, not noise, and xcsift would filter it.

## [0.3.2] - 2026-07-17

Fixes from the 07-12→16 PR audit (`INBOX/pr-audit-multiplai-2026-07-12-to-16.md`).

### Fixed
- **buildme: resumed pre-baseline checkpoints no longer mis-baseline the
  review diff.** `run_block_tdd` stamps `baseline_commit` only when the block
  is genuinely starting (PENDING/TESTING). Resuming an old mid-block
  checkpoint (IMPLEMENTING/REVIEWING with no baseline) previously stamped
  current HEAD — hiding the block's own commits from the quality reviewer;
  now it keeps the documented `git diff HEAD` fallback and logs a warning.
- **buildme: one unreadable standards file no longer fails the block.**
  `BuildConfig.standards_text()` now catches per-file read errors
  (OSError/UnicodeDecodeError), logs, and skips — as its docstring always
  promised.
- **buildme: tasks-shape audit completion is recorded in checkpoint state,
  not inferred from file existence.** A crash mid-audit used to leave
  tasks.md DONE and silently skip the audit on resume; resume now re-runs it
  (idempotent) and logs when it is skipped as recorded-complete. A non-list
  JSON audit response (e.g. object-wrapped findings) now logs a warning
  instead of silently passing as "no findings".
- **buildme: `claude-agent-sdk` floored+capped** (`>=0.2.116,<0.3`) — 0.1.x
  crashed at import (same bug class as the dream hook crash).
- **swift-build: plain Linux (no container markers) now refuses the SSH
  bridge even when `SSH_BUILD_USER` is set**, matching the SKILL.md support
  matrix — the bridge assumes the container↔host identical-path mount.
  Also fixed the false "caches result" comment (the xcsift probe is now
  actually memoized per invocation).
- **skill-creator: the degradation-contract reference is now resolvable for
  installed plugins** — `docs/degradation-contract.md` is vendored into the
  skill's `references/` (repo copy stays canonical) and SKILL.md points at
  it plus the marketplace GitHub URL.

### Changed
- swift-build SKILL.md documents two current gateway limitations (paths with
  spaces / schemes with parens until the container-side unquote fix ships)
  and the `sim screenshot` host-path caveat.

## [0.3.1] - 2026-07-15

### Fixed
- **`swift-build` was unusable over the container→host SSH bridge.** The script
  appends `2>&1 | xcsift --format toon --quiet` to build/test commands, which
  the host gateway rejected as a shell metacharacter (`DENIED: shell
  metacharacter in command`) whenever the host had `xcsift` installed — so
  every containerized `swift build` / `swift test` failed. Paired with a gateway
  change (multiplai-container) that recognizes this one fixed, trusted suffix.
- **`discover_scheme` sent `2>/dev/null` over the bridge** — a latent `>`
  redirect that the gateway also denied, breaking scheme discovery (and thus
  `build`/`test`) for Xcode-project layouts. Removed; the sed/grep parse
  tolerates stderr.
- Corrected the SKILL.md "Gateway Compatibility" note that falsely claimed pipes
  work because the gateway runs `zsh -lc` on the full command.

### Changed
- **`swift-build` is now model-invocable** (`disable-model-invocation: false`),
  so Claude reaches for the skill instead of improvising raw `swift --version` /
  `ssh` calls that the gateway denies.

## [0.3.0] - 2026-07-14

### Added
- **New skill: `plan`** — author self-contained, executable implementation
  plans with a mandatory completion contract: verifiable "Done means"
  criteria, explicit "Constraints / out of scope" (including stop-and-ask
  gates), and a fresh-session self-containedness test. Plan files are
  directly consumable by "implement the plan", goal/autonomous runners,
  or buildme — no parallel goal document needed. Prompt-only, no scripts.

## [0.2.0] - 2026-07-10

Semantic model tiers for the buildme pipeline (requires multiplai-core ≥ v0.7.0).

### Changed
- **buildme now resolves its model from a semantic tier, not a dated literal.**
  `config.DEFAULT_MODEL` is now `pick_model("opus", task="buildme")` — the model
  family lives in `multiplai_core.env.CURRENT_MODEL` (one place to bump per
  quarter), still capped by the `MULTIPLAI_MODEL` ceiling, and retunable per
  task via a `[buildme] MODEL=...` section in `multiplai.conf` with no code edit.

### Fixed
- **Tier detection (DEV-3).** `detect_tier()` now derives advanced/standard from
  the resolved `DEFAULT_MODEL` instead of the `CLAUDE_MODEL` env var, which
  Claude Code never exports to Bash subprocesses — so the tier was permanently
  stuck on `standard` in production regardless of the pinned model. buildme now
  correctly runs the advanced (per-block) TDD path under an opus ceiling.

## [0.1.1] - 2026-07-09

Correctness fixes for the buildme pipeline from the 2026-07-08 code review.
All ~190 tests pass; each fix ships with a regression test.

### Fixed
- **Design/tasks/audit/rubric are grounded in the generated requirements
  again.** Four readers still globbed the never-written legacy
  `change_dir/specs/*/spec.md` while the only writer emits flat
  `change_dir/requirements/<capability>.md`, so every one of those prompts was
  built against an empty directory (`(no specs yet)`). `_read_specs`,
  `run_design_audit`, and the two rubric gatherers now read `requirements/*.md`
  (stem = capability name), mirroring `tdd_engine.assemble_context`. The
  vestigial `"specs"` artifact-id alias in `_build_prompt` is gone.
- **Agent timeouts are observable.** `agent_call` catches `AgentRunTimeout` and
  returns `AgentResult(success=False)` without raising, so every
  `except LLMCallTimeoutError` in the TDD engine was dead — `block.timed_out`
  never flipped and `EXIT_AGENT_TIMEOUT` (3) was unreachable (real timeouts
  exited 1). `AgentResult` gains a `timed_out` flag, the fatal
  test-writer/implementer paths propagate it to `block.timed_out`, and the five
  unreachable except blocks are removed.
- **Tier detection recognizes newer Opus models.** `detect_tier` used a literal
  allowlist (`opus-4-5/4-6/5/6`) that silently downgraded the skill-pinned
  `opus-4-7` to `standard`; replaced with an Opus `>= 4.5` version-range check.
  See the caveat below.
- **Block state is indexed by list position, not `block.number - 1`**, so
  non-contiguous LLM-generated `tasks.md` numbering can't silently no-op a
  status write.
- **Per-block commits no longer leak buildme bookkeeping.**
  `_git_commit_block_phase` excludes `build-progress.md` and `.build-state.json`
  from staging instead of `git add -A`.
- **`--change` can't escape `specs/changes/`.** The change name is normalized
  when resolving `config.change_dir` (shared `normalize_change_name`), so a
  traversal value can't send `archive()`'s `shutil.move` out of tree.

### Known limitation
- Tier detection is **inert in production**: Claude Code v2.1.x does not export
  `CLAUDE_MODEL` to Bash subprocesses (it exports `CLAUDE_EFFORT` but not
  `CLAUDE_MODEL`), and `SKILL.md` invokes the pipeline via a plain `uv run` with
  no `CLAUDE_MODEL=` prefix — so `detect_tier` always sees an empty model and
  returns `standard`, for every model, not just `opus-4-7`. The version-range
  fix is correct in isolation and future-proofs the day the model is plumbed
  through, but the tier stays inert until the skill propagates the model (e.g.
  `CLAUDE_MODEL="{model}" uv run …`) or the pipeline grows an explicit
  `--tier`/`--model` flag. Documented in the `detect_tier` docstring.
