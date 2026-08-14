# multiplai-apple

Apple development pack for Claude Code: **build, test, and drive iOS/macOS
projects**. Part of the [`multiplai`](../../README.md) marketplace.

**macOS only, and not part of the default install.** The Swift/Xcode toolchain
exists only on macOS, so this pack is an explicit add-on for people building
Apple software — install it alongside whatever other packs you use. From the
[multiplai-kit](https://github.com/spikelab/multiplai-kit) container it
additionally needs the kit's **opt-in container→host SSH bridge**; on plain
Linux these skills do not run, and say so.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-apple@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `swift-build` | Build, test, and manage iOS/macOS projects — SwiftPM and Xcode, simulator management, XCUITest runs with screenshot export. On a Mac it runs the toolchain directly; inside the multiplai-kit container it routes commands over the host SSH bridge. |

## Compatibility

- `swift-build` — macOS only (Swift/Xcode toolchain). From the multiplai-kit
  container it needs the opt-in container→host SSH bridge. On plain Linux it
  fails with the real constraint (Swift/Xcode builds need macOS), not a bridge
  error.

Full details: [compatibility matrix](../../README.md#compatibility-matrix) and
the [degradation contract](../../docs/degradation-contract.md).
