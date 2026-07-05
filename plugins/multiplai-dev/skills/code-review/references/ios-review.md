# iOS / Swift / SwiftUI Code Review Reference

Load when reviewing iOS/macOS codebases (Swift, SwiftUI, UIKit).

## Issue Types

| Category | What to Check |
|----------|--------------|
| **Memory Management** | Retain cycles, weak references, closure captures, deinit verification |
| **Concurrency** | Swift Concurrency (async/await, actors), MainActor usage, data races |
| **SwiftUI Patterns** | View composition, state management, performance, previews |
| **Error Handling** | Proper `do/catch`, `Result` types, no force unwraps on uncertain data |
| **App Lifecycle** | Background handling, scene phases, state restoration |
| **Platform Conventions** | HIG compliance, accessibility, localization readiness |

## Common Mistakes

| Mistake | Why It Matters | Fix |
|---------|---------------|-----|
| Force unwrap `!` on optional from external data | Crash in production | Use `guard let` or `if let` |
| `try!` on failable operation | Crash on failure | Use `do/catch` or `try?` with fallback |
| Strong reference cycle in closure | Memory leak | Use `[weak self]` or `[unowned self]` |
| Updating UI from background thread | Crash or undefined behavior | Dispatch to `@MainActor` or `DispatchQueue.main` |
| Massive view body (100+ lines) | Unreadable, recomposition issues | Extract subviews, use `ViewBuilder` |
| `@State` for shared data | State is view-local | Use `@ObservedObject`, `@EnvironmentObject`, or `@Binding` |
| `ObservableObject` with many published props | Every change recomputes all dependent views | Split into focused observable objects or use `@Observable` (iOS 17+) |
| Blocking main thread with sync I/O | UI freeze, watchdog kill | Use async/await or background queue |
| Hardcoded strings | No localization, hard to maintain | Use `String(localized:)` or `.localizable` |
| Missing `Sendable` conformance | Data race with Swift Concurrency | Mark types as `Sendable` or use actors |

## Memory Management

| Pattern | Risk | Fix |
|---------|------|-----|
| Closure capturing `self` strongly | Retain cycle → memory leak | `[weak self] in guard let self else { return }` |
| Delegate property without `weak` | Retain cycle | `weak var delegate: SomeDelegate?` |
| Timer without invalidation | Retains target, never deallocates | Invalidate in `deinit` or use `Task` with cancellation |
| `NotificationCenter` observer without removal | Leak + ghost callbacks | Use `Task` or store `AnyCancellable`, remove in `deinit` |
| `URLSession` with strong delegate | Session retains delegate until invalidated | Use `finishTasksAndInvalidate()` or completion handlers |

**Verification:** Check that classes with delegates/closures/timers implement `deinit` and clean up.

## Swift Concurrency (async/await)

| Issue | Fix |
|-------|-----|
| `Task { }` without storing reference | Can't cancel, potential leak | Store in `@State` or property, cancel in `onDisappear`/`deinit` |
| Missing `@MainActor` on UI-updating code | Compiler warning or data race | Annotate view models with `@MainActor` |
| `nonisolated` used to silence warnings | Hides real concurrency issue | Fix the isolation properly |
| `Task.detached` when `Task` suffices | Loses structured concurrency benefits | Use `Task` unless you specifically need detachment |
| Actor reentrancy assumption | Code after `await` may see different state | Re-check state after every `await` inside actor |
| `@Sendable` closure captures mutable state | Data race | Capture only `Sendable` values, or use actor |

## SwiftUI-Specific

### State Management

| Source of Truth | Use When |
|----------------|----------|
| `@State` | Private view-local value types |
| `@Binding` | Child view modifying parent's state |
| `@StateObject` | View owns the observable object's lifecycle |
| `@ObservedObject` | View receives object from parent (doesn't own lifecycle) |
| `@EnvironmentObject` | Shared across view hierarchy (dependency injection) |
| `@Observable` (iOS 17+) | Modern replacement for `ObservableObject` — finer-grained updates |
| `@AppStorage` | UserDefaults-backed persistence |

| Issue | Fix |
|-------|-----|
| `@ObservedObject` for object created in same view | Use `@StateObject` — `@ObservedObject` doesn't own lifecycle |
| State modification during view update | Causes "Modifying state during view update" warning | Defer with `DispatchQueue.main.async` or `Task` |
| Complex logic in `body` | Recomputed on every state change | Move to view model or computed properties |
| `onAppear` for one-time setup | Can fire multiple times | Use `.task` modifier (auto-cancelled) or `@State` flag |
| GeometryReader overuse | Performance cost, layout complexity | Use only when truly needed, prefer alignment guides |

### View Performance

| Issue | Fix |
|-------|-----|
| `List` with `id: \.self` on non-unique values | Incorrect diffing, glitches | Use unique `Identifiable` ID |
| Large `ForEach` without `LazyVStack` | All views computed upfront | Use `LazyVStack`/`LazyHStack` for scrollable content |
| Image without `.resizable()` before `.frame()` | Image doesn't respect frame | Always `.resizable()` first, then modifiers |
| Missing `@ViewBuilder` on helper methods returning views | Compile errors or `AnyView` | Annotate with `@ViewBuilder` |
| `AnyView` type erasure | Breaks SwiftUI diffing, poor performance | Use `@ViewBuilder`, `Group`, or `some View` |

## UIKit-Specific (if applicable)

| Issue | Fix |
|-------|-----|
| Missing `prepareForReuse()` in cells | Stale data in recycled cells | Reset all dynamic content |
| `addSubview` without constraints | Autolayout ambiguity | Always add constraints after adding subview |
| No `[weak self]` in view controller closures | VC leaked after dismissal | Weak capture in completion handlers, delegates |
| Force-cast `as!` on dequeued cells | Crash if registration wrong | Use `as?` with fallback |
| Missing `DispatchQueue.main` for UI updates | Undefined behavior, crashes | Ensure all UI on main thread |

## Networking & Data

| Issue | Fix |
|-------|-----|
| `URLSession` without error handling | Silent network failures | Handle all error cases, check status codes |
| Codable without `CodingKeys` for API mismatch | Crash on decode | Add `CodingKeys` when API names differ from Swift property names |
| Missing `keyDecodingStrategy` | Can't decode `snake_case` API | `.convertFromSnakeCase` or explicit `CodingKeys` |
| Sensitive data in `UserDefaults` | Not encrypted, visible in backups | Use Keychain via `Security` framework |
| No request timeout | Hangs indefinitely | Set `timeoutInterval` on `URLRequest` |

## Accessibility

| Issue | Fix |
|-------|-----|
| Images without `accessibilityLabel` | VoiceOver reads nothing or filename | Add `.accessibilityLabel("description")` |
| Custom controls without proper traits | VoiceOver can't convey purpose | Add `.accessibilityAddTraits(.isButton)` etc. |
| Small tap targets | Hard to use for motor impairments | Minimum 44x44pt touch target |
| Color-only information | Invisible to colorblind users | Add text labels, icons, or patterns |
| Missing Dynamic Type support | Text doesn't scale | Use system fonts or `.dynamicTypeSize()` |

## Do NOT Flag (iOS/Swift)

- `guard let self` in closures (standard Swift pattern since 5.7)
- Trailing closure syntax on single-closure APIs
- `@Published` without `private(set)` when external mutation is intentional
- `some View` return type on `body` (required by SwiftUI)
- Single-expression computed properties without explicit `return`
- Protocol extensions providing default implementations
- `final class` on view models (good practice, not over-engineering)
- `@frozen` on enums in app code (only matters for library/framework code)
- `Task { @MainActor in ... }` pattern for UI work from background
- `#Preview` macros without exhaustive state combinations (previews are development aids)
- `fileprivate` vs `private` when `fileprivate` enables extensions in same file
