# Changelog

All notable changes to the **multiplai-writing** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-writing@<version>`.

Recorded history starts at **0.1.0**, the first and so far only version. This
file was written on 2026-07-26 by reading the tag and `git log`, so early
entries are summaries rather than notes taken at the time.

`multiplai-writing@0.1.0` was tagged on 2026-07-09 at the commit that created
the plugin (2026-07-05). Everything under **Unreleased** below landed after
that commit while the marketplace version stayed at `0.1.0` — so an install
today already includes it, under the same version number. That is exactly the
ambiguity the [changelog gate](../../CLAUDE.md#release-convention) now exists
to prevent.

## [Unreleased]

### Added
- **A plugin README** (`plugins/multiplai-writing/README.md`) — what the pack
  contains, what each mode does, and what it needs.

### Changed
- **`draft`, `editor` and `linkedin` no longer stall on missing voice files.**
  The voice guides under `$CLAUDE_CONFIG_DIR/memory/` (`core-voice`,
  `*-voice-guide`, `write-like-a-human`, `how-to-write-well`) are personal and
  are deliberately not shipped. Each is now loaded only if present, and when
  none are found the skill asks you for your voice preferences instead of
  blocking on step 1.

### Fixed
- Dropped `user_invocable: true` from the SKILL.md. The underscore spelling is
  not a recognized frontmatter key, so the line never did anything; the
  behaviour it was meant to request is the default anyway.

## [0.1.0] - 2026-07-09

### Added
- **First release of the writing pack**, moved out of `multiplai-kit` and
  adapted to plugin-relative paths. One `writing` skill with six modes:
  `brief` (structure out of a braindump), `cmd-brief` (resolve commands inside
  a brief), `draft` (brief into full draft), `editor` (copy edit for style and
  AI tells), `linkedin` (LinkedIn posts), `imagen` (image prompts).
