# Changelog

All notable changes to the **multiplai-apple** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-apple@<version>`.

## [Unreleased]

## [0.1.0] - 2026-08-14

### Added

- **Initial release.** The `swift-build` skill moved here from
  `multiplai-dev` (last shipped there in `multiplai-dev@0.13.0`), unchanged.
  It is mac-only capability — the Swift/Xcode toolchain, and from the
  multiplai-kit container the opt-in host SSH bridge — so it now ships as an
  explicit add-on pack instead of inside a cross-platform plugin. If you used
  `swift-build` from multiplai-dev, install this plugin to keep it.
