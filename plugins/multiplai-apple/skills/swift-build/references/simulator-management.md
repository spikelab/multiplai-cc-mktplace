# Simulator Management Reference

Quick reference for `xcrun simctl` commands. All commands work through `swift-host.sh` (which handles SSH bridging) or directly on macOS.

## Device Lifecycle

```bash
# List available devices
xcrun simctl list devices available

# List all runtimes
xcrun simctl list runtimes

# Boot a device
xcrun simctl boot "iPhone 16 Pro"

# Shutdown a device
xcrun simctl shutdown "iPhone 16 Pro"

# Shutdown all running simulators
xcrun simctl shutdown all

# Erase (factory reset) a device
xcrun simctl erase "iPhone 16 Pro"

# Delete a device
xcrun simctl delete "iPhone 16 Pro"

# Create a new device
xcrun simctl create "My iPhone" "iPhone 16 Pro" "iOS-18-2"
```

## Screenshots & Video

```bash
# Screenshot of booted simulator
xcrun simctl io booted screenshot /tmp/screen.png

# Screenshot of specific device
xcrun simctl io <UDID> screenshot /tmp/screen.png

# Record video (Ctrl+C to stop)
xcrun simctl io booted recordVideo /tmp/recording.mp4
```

## App Management

```bash
# Install an app
xcrun simctl install booted /path/to/MyApp.app

# Launch an app
xcrun simctl launch booted com.example.MyApp

# Terminate an app
xcrun simctl terminate booted com.example.MyApp

# Uninstall an app
xcrun simctl uninstall booted com.example.MyApp
```

## Permissions & Privacy

```bash
# Grant permission (camera, photos, location, contacts, microphone, etc.)
xcrun simctl privacy booted grant photos com.example.MyApp

# Revoke permission
xcrun simctl privacy booted revoke camera com.example.MyApp

# Reset all permissions
xcrun simctl privacy booted reset all com.example.MyApp
```

## Push Notifications

```bash
# Send a push notification (requires payload JSON file)
xcrun simctl push booted com.example.MyApp /path/to/payload.json
```

Payload format:
```json
{
  "aps": {
    "alert": { "title": "Test", "body": "Hello from simctl" },
    "sound": "default"
  }
}
```

## Status Bar Overrides

```bash
# Set clean status bar (useful for screenshots)
xcrun simctl status_bar booted override \
  --time "9:41" \
  --batteryState charged \
  --batteryLevel 100 \
  --cellularBars 4 \
  --wifiBars 3

# Clear overrides
xcrun simctl status_bar booted clear
```

## Useful Patterns

```bash
# Get UDID of booted device
xcrun simctl list devices booted -j | python3 -c "import sys,json; devs=[d for r in json.load(sys.stdin)['devices'].values() for d in r if d['state']=='Booted']; print(devs[0]['udid'] if devs else 'none')"

# Open a URL in the simulator
xcrun simctl openurl booted "https://example.com"

# Add photos to simulator
xcrun simctl addmedia booted /path/to/photo.jpg
```
