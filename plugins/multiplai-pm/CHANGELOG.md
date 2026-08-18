# Changelog

All notable changes to the **multiplai-pm** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-pm@<version>`.

Recorded history starts at **0.1.0**. This file was written on 2026-07-26 by
reading the tag and `git log`, so the `0.1.0` entries are summaries rather than
notes taken at the time.

`multiplai-pm@0.1.0` was tagged on 2026-07-09 at the commit that created the
plugin (2026-07-05). Everything now recorded under **0.2.0** landed after that
commit while the marketplace version stayed at `0.1.0`, so for a while it was
shipping to installs under the previous version number — exactly the ambiguity
the [changelog gate](../../CLAUDE.md#release-convention) exists to prevent.
Cutting `0.2.0` on 2026-08-16 closed it; only `0.1.0` carries a git tag so far.

## [Unreleased]

Nothing yet.

## [0.4.0] - 2026-08-18

### Added
- **`plane comment-edit` — fix a comment instead of adding a correction under
  it.** `comments` now prints each comment's id, and `comment-edit SPK-12 <id>
  "new text"` replaces that comment's body. The first 8 characters of an id are
  usually enough; if a prefix matches more than one comment the command refuses
  and lists the candidates, because overwriting the wrong comment destroys text
  nobody kept a copy of. Like `update --body`, it replaces the whole body — it
  is not an append.
- **`plane attachments --upload FILE` — put a file on a ticket.** Repeat the
  flag for several files. Each upload prints the filename, its size, and the new
  attachment id. `--upload` and `--download` cannot be combined.

  Where the bytes go is worth knowing: Plane hands out a presigned form for its
  object store (`*.amazonaws.com` on Plane Cloud) and the file is POSTed
  straight there over HTTPS, so it does not travel through the Plane API host.
  That request is anonymous by design — your Plane token is never attached to
  it, redirects are refused rather than followed, and no host besides the API
  host or `*.amazonaws.com` is accepted. Files over 32 MB are refused, and
  `--dry-run` prints what would be requested without asking Plane for an upload
  form at all.

  If the file fails to upload after Plane has created the attachment record,
  the error says exactly that and names the attachment id, so you can delete
  the half-uploaded record before retrying rather than wondering why an empty
  attachment appeared.

### Changed
- `plane comments` prints `[<comment-id>]` on each header line. Plane's own UI
  does not show comment ids, so this is the only place to get one.

## [0.3.0] - 2026-08-17

### Added
- **`plane` — Plane ticket management, the pack's first skill with a script.**
  Talk to Claude about tickets ("what's on my board", "move SPK-12 to done",
  "open a ticket for the login bug") and it drives a stdlib-only CLI against
  the Plane API (Cloud or self-hosted). Every request passes a **project
  allowlist** (`PLANE_ALLOWED_PROJECTS`, required, no default): writes to any
  project you did not explicitly list are refused, and issue reads, listings
  and search results are filtered to the allowlist. Two commands are
  workspace-scoped by nature and disclosed as such in SKILL.md: `members`
  returns the whole workspace roster, and `search` executes server-side across
  the workspace before its results are filtered back down. Issues, comments,
  states, labels, members, cycles, estimates, attachments (download), search;
  no deletes and no project mutation, by design. Run
  `python3 skills/plane/scripts/plane.py check` first — it prints the resolved
  config and self-tests the guardrail. Needs `PLANE_API_TOKEN`,
  `PLANE_WORKSPACE` and `PLANE_ALLOWED_PROJECTS` in the environment; see the
  README's configuration section.

  Ported from the `dolce-plane` plugin
  (DolceTech/DolceClaudeMarketplace, commit `7154f85`), then hardened in a
  pre-release review (the fixes below); its test suite (allowlist parsing,
  adversarial guardrail coverage, markdown→HTML escaping, search filtering)
  ships with it — 207 tests as of this release.

### Fixed
All found by review of the ported code, before its first release here:

- **Guardrail hardening.** A trailing double slash (`/projects/<uuid>//`) no
  longer slips a PATCH/DELETE past the "project object is read-only" rule —
  consecutive slashes are collapsed before judging, the way a fronting proxy
  would merge them. A path still percent-encoded after three decode passes is
  refused instead of waved through. And every query parameter carrying a UUID
  is checked against the allowlist whatever its key is called, so a
  cross-project filter no longer has to spell "project" to be caught
  (`search` free text alone is exempt; its results are re-filtered
  client-side).
- **`check` no longer prints a token prefix.** It printed the first 10
  characters of `PLANE_API_TOKEN` into every transcript and CI log that
  followed the documented "run `check` first" step; now it prints the length
  only.
- **Errors no longer masquerade as "not found".** A 401/5xx/network failure
  while resolving an issue by UUID surfaces instead of being reported as
  "issue not found in any allowed project" — an expired token now says so.
- **Sturdier edges.** A non-numeric `X-RateLimit-Reset` header (some proxies
  send an HTTP-date) falls back to exponential backoff instead of crashing
  the 429 retry, and a delta-seconds value no longer degrades to a 1-second
  spin. Date-only cycle bounds (legacy self-hosted) no longer crash `cycles`
  and `--cycle active`. `attachments --download` suffixes colliding filenames
  instead of silently overwriting. A create/update answered with an empty
  2xx body is confirmed as real instead of being mistaken for a dry-run.
  `PLANE_ENV_FILE` accepts `export KEY=VALUE` lines. Issue refs whose project
  identifier contains digits (`WEB3-12`) parse.

## [0.2.0] - 2026-08-16

Everything below had already landed while the marketplace version stayed at
`0.1.0` — so it was shipping to installs under the previous version number.
This release gives it a number of its own.

### Fixed
- **`pm-jtbd-synthesis` no longer names a directory that does not exist.** Its
  output guidance described a curated workspace as having `RESOURCES/` and
  `PLANS/`; `PLANS/` was retired on 2026-08-11 and the convention is now
  `ARTIFACTS/`. Text only — the instruction's actual write target was already
  `INBOX/`, so nothing behaved differently.

### Added
- **A plugin README** (`plugins/multiplai-pm/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.

### Changed
- **The four Opus-tier skills** (`landing-page`, `pm-jtbd-synthesis`,
  `pm-pr-faq`, `pm-strategy-memo`) now declare `effort: medium` instead of a
  per-skill spread of low/medium/high. Frontmatter is the only lever Claude
  Code offers for model tier, so it is now the single source of truth for it.
- **The skills work on a vanilla install.** Personal memory files are loaded
  only if present: `job-application` asks you for your career history instead
  of failing on a missing voice overlay, and `landing-page` treats its voice
  files as optional. Output paths soften from a hard `INBOX/` write to
  `./INBOX` if it exists, else the current directory.
- **Cross-plugin prerequisites are declared** rather than assumed —
  `pm-jtbd-synthesis` names `transcribe` (multiplai-media),
  `landing-page`/`pm-*` name `interviewer` and `extract-insights`
  (multiplai-research). Skills that were only ever planned (`pm-prd`,
  `pm-roadmap`, `pm-opportunity-tree`) are marked as not shipped instead of
  being referenced as if they existed.
- **`job-application` documents its PDF dependency** (weasyprint plus
  Pango/Cairo) and degrades with a message naming it rather than failing
  mid-run.

### Removed
- **The author's personal data is out of the shipped templates.**
  `job-application`'s `ats-checks.md` carried a real career narrative and
  `assets/application-template.html` carried real education rows as
  "examples" — both would have been copied verbatim into a stranger's resume.
  They are now structural placeholders that teach the technique without the
  PII. `landing-page`'s `voice-calibration.md` lost its owner-specific
  spelling and default-project guidance in favour of brand-neutral wording.

### Fixed
- Dropped `user_invocable: true` from all six SKILL.md files. The underscore
  spelling is not a recognized frontmatter key, so the line never did
  anything; the behaviour it was meant to request is the default anyway.

## [0.1.0] - 2026-07-09

### Added
- **First release of the product-management pack**, six skills moved out of
  `multiplai-kit` and adapted to plugin-relative paths:
  - `pm-jtbd-synthesis` — Jobs-to-be-Done from interview transcripts, with
    Forces of Progress and quote-level attribution.
  - `pm-persona-codifier` — raw archetype notes into source-attributed
    persona docs.
  - `pm-pr-faq` — Amazon-style Working Backwards press release plus an
    adversarial internal FAQ.
  - `pm-strategy-memo` — Minto-pyramid leadership memos with a
    fresh-reader test.
  - `job-application` — tailored resume and cover letter, rendered to PDF.
  - `landing-page` — landing-page copy in create, audit and iterate modes.
