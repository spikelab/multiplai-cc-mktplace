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
  `multiplai-dev` (last shipped there in `multiplai-dev@0.13.0`).
  It is mac-only capability — the Swift/Xcode toolchain, and from the
  multiplai-kit container the opt-in host SSH bridge — so it now ships as an
  explicit add-on pack instead of inside a cross-platform plugin. If you used
  `swift-build` from multiplai-dev, install this plugin to keep it.

### Changed

Both items are relative to the last multiplai-dev release of the skill; the
move itself carried no other behaviour change.

- **The skill now states up front that it needs macOS.** It used to describe
  itself as working "from any environment", which is what Claude reads when
  deciding whether to use it — so on a Linux machine it would be selected and
  then fail at a requirement that was knowable in advance. Its description and
  opening paragraph now name the macOS requirement plainly.

- **On Linux the error names the missing toolchain instead of an SSH bridge.**
  It used to end with "run it from the multiplai container with the host bridge
  configured" and "SSH_BUILD_USER is ignored" — instructions you cannot act on
  unless you happen to run the multiplai kit. It now says the Swift/Xcode
  toolchain is macOS-only and there is no Linux equivalent. Inside a container
  with the bridge unconfigured, the bridge instructions are unchanged.
