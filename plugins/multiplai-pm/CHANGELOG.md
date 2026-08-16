# Changelog

All notable changes to the **multiplai-pm** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-pm@<version>`.

Recorded history starts at **0.1.0**, the first and so far only version. This
file was written on 2026-07-26 by reading the tag and `git log`, so early
entries are summaries rather than notes taken at the time.

`multiplai-pm@0.1.0` was tagged on 2026-07-09 at the commit that created the
plugin (2026-07-05). Everything under **Unreleased** below landed after that
commit while the marketplace version stayed at `0.1.0` — so an install today
already includes it, under the same version number. That is exactly the
ambiguity the [changelog gate](../../CLAUDE.md#release-convention) now exists
to prevent.

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-08-16

Everything below had already landed while the marketplace version stayed at
`0.1.0` — so it was shipping to installs under the previous version number.
This release gives it a number of its own, which is what the section header
above this one describes as the ambiguity worth removing.

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
