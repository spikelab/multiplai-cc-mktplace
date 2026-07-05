---
name: swift-build
description: |
  Build, test, and manage iOS/macOS projects from any environment.
  Handles the SSH bridge when running inside a Docker container —
  Claude calls the script, reads structured output, never touches SSH directly.
triggers:
  - "swift build"
  - "swift test"
  - 'run tests (in a Swift project context)'
  - "simulator"
  - "xcodebuild"
model: opus
effort: high
disable-model-invocation: true
---

# Swift Build Skill

Build and test Swift/iOS/macOS projects transparently, whether running on macOS directly or inside a Docker container that SSHes to a macOS host.

## How It Works

All commands go through a single script: `${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh`

The script handles environment detection, project detection, and output formatting. You call it; it figures out the rest.

### --package-path Flag

Use `--package-path <dir>` to target a Swift package in a different directory without `cd`:

```bash
# From any directory — works in containers without gateway cd issues
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh --package-path /path/to/ios build
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh --package-path /path/to/ios test
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh --package-path /path/to/ios test --filter MyTests
```

This is the **preferred approach in containers** — it uses `swift build --package-path` / `swift test --package-path` over SSH, which matches the gateway allowlist directly (no `cd` pattern needed).

## Environment Detection

The script detects where it's running via `uname -s`:

| `uname -s` | Environment | Behavior |
|-------------|-------------|----------|
| `Darwin` | Local macOS | Commands run directly |
| `Linux` | Docker container | Commands SSH to macOS host |

**You do not need to detect the environment yourself.** Just run the script — it handles routing.

### SSH Configuration (Container Only)

The script reads these environment variables (all have sensible defaults matching `dclaude.sh`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `SWIFT_BUILD_HOST` | `host.docker.internal` | macOS host address |
| `SWIFT_BUILD_USER` | `$SSH_BUILD_USER` from `.env` (required) | SSH username |
| `SWIFT_BUILD_KEY` | `/home/agent/.ssh/build_key` | SSH private key path |

## Project Detection

The script auto-detects project type from files in the current directory:

| Files Found | Project Type | Build Tool |
|-------------|-------------|------------|
| `Package.swift` only | `swiftpm` | `swift build` / `swift test` |
| `*.xcodeproj` or `*.xcworkspace` only | `xcode` | `xcodebuild` (auto-discovers scheme) |
| Both `Package.swift` and `*.xcodeproj` | `hybrid` | Prefers SwiftPM (`swift build` / `swift test`) |

For Xcode projects, the scheme is auto-discovered via `xcodebuild -list`.

## Commands

### Build

```bash
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh build
```

Builds the project. SwiftPM → `swift build`. Xcode → `xcodebuild -scheme <auto> -sdk iphonesimulator build`.

### Test

```bash
# Run all tests
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh test

# Run filtered tests
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh test --filter MyTestClass
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh test --filter MyTestClass/testSpecificMethod
```

For SwiftPM, `--filter` maps to `swift test --filter`. For Xcode, it maps to `-only-testing:`.

### Simulator Management

```bash
# List available simulators
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim list

# Boot a simulator
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim boot "iPhone 16 Pro"

# Open Simulator.app GUI (so the user can see/interact with the device)
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim open

# Install an app on the booted simulator
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim install /path/to/MyApp.app

# Launch an app by bundle identifier
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim launch com.example.MyApp

# Shutdown a simulator
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim shutdown

# Take a screenshot of the booted simulator
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim screenshot /tmp/screen.png
```

For detailed simulator commands beyond what the script wraps, load `references/simulator-management.md`.

## Output Parsing

All build/test output is piped through **xcsift** (if installed on the host) using `--format toon --quiet`.

### TOON Format

TOON is a compact, LLM-friendly format that uses 30-60% fewer tokens than JSON. Example:

```
🔴 ERROR MyApp/ContentView.swift:42
  Cannot convert value of type 'String' to expected argument type 'Int'

⚠️ WARNING MyApp/AppDelegate.swift:15
  Result of call to 'load()' is unused

✅ TEST PASSED MyAppTests/ContentViewTests/testInitialState (0.003s)

🔴 TEST FAILED MyAppTests/ContentViewTests/testButtonTap (0.012s)
  XCTAssertEqual failed: ("Hello") is not equal to ("Goodbye")
  at MyAppTests/ContentViewTests.swift:28
```

### Reading Results

- **Build succeeded:** No output (due to `--quiet`). Exit code 0.
- **Build failed:** Only errors and warnings shown. Exit code non-zero.
- **Tests passed:** No output (due to `--quiet`). Exit code 0.
- **Tests failed:** Only failures shown with file:line and assertion message. Exit code non-zero.
- **xcsift not installed:** Raw xcodebuild/swift output with a `WARNING:` line. Still functional, just noisier.

### Exit Codes

Exit codes propagate through xcsift (`set -o pipefail`). Check `$?`:
- `0` = success
- Non-zero = build/test failure

## Usage Pattern

When working on a Swift project:

1. **First, check the environment works:**
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh sim list
   ```
   If this returns simulators, the SSH bridge (or local toolchain) is working.

2. **Build to check compilation:**
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh build
   ```

3. **Run tests:**
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh test
   ```

4. **Run specific tests during TDD:**
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh test --filter TestClassName/testMethodName
   ```

## Autonomous TDD Integration

When using the `autonomous-tdd` skill on a Swift project, the test command is:

```
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh --package-path /absolute/path/to/project test
```

This is auto-detected by autonomous-tdd's stack detection (Step 2) when it finds `Package.swift` or `*.xcodeproj`.

To filter tests in TDD phases, subagents append `--filter`:

```
${CLAUDE_PLUGIN_ROOT}/skills/swift-build/scripts/swift-host.sh --package-path /absolute/path test --filter FeatureTests
```

**Always use `--package-path` with absolute paths in TDD agents** — subagents may not inherit the working directory.

## Gateway Compatibility

When running from a container, all commands the script sends over SSH are compatible with the `~/.local/bin/container-build-gateway.sh` allowlist:

- `swift build`, `swift test --filter ...`
- `xcodebuild -scheme ... build`, `xcodebuild -scheme ... test`
- `xcrun simctl list devices available`, `xcrun simctl boot ...`, `xcrun simctl io ...`, `xcrun simctl install ...`, `xcrun simctl launch ...`, `xcrun simctl shutdown ...`
- `open -a Simulator` (for opening the Simulator GUI)
- `cd /path && <any of the above>`
- Pipes to `xcsift` work because the gateway uses `zsh -lc` which handles the full shell command

## Constraints

- **NEVER use raw SSH.** Do not call `ssh` directly. All host communication goes through `swift-host.sh`. The script handles hostname, key discovery, and gateway compatibility. Raw SSH will fail — the host uses an SSH gateway that only allows specific commands.
- **NEVER tell the user to run commands manually.** If a command isn't available through the script, add it to the script and the gateway — don't give up and dump shell commands on the user.
- **Path assumption:** The project directory must be at the same path on both the container and the host. This is the case with `dclaude.sh` which mounts at the identical path.
- **No interactive sessions:** The SSH gateway denies interactive shells. All commands must be non-interactive.
- **Scheme discovery:** For Xcode projects, `xcodebuild -list` must return at least one scheme. If the project hasn't been opened in Xcode yet, this may fail — open it once on the host first.
