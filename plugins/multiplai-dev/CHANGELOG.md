# Changelog

## 0.3.3 — 2026-07-18

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

## 0.3.2 — 2026-07-17

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

## 0.3.1 — 2026-07-15

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

## 0.3.0 — 2026-07-14

### Added
- **New skill: `plan`** — author self-contained, executable implementation
  plans with a mandatory completion contract: verifiable "Done means"
  criteria, explicit "Constraints / out of scope" (including stop-and-ask
  gates), and a fresh-session self-containedness test. Plan files are
  directly consumable by "implement the plan", goal/autonomous runners,
  or buildme — no parallel goal document needed. Prompt-only, no scripts.

## 0.2.0 — 2026-07-10

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

## 0.1.1 — 2026-07-09

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
