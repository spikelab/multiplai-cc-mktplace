# Swift idioms for `deepen`

How the glossary terms in [../LANGUAGE.md](../LANGUAGE.md) map to Swift constructs. The vocabulary doesn't change — only the constructs used to express it.

## Mapping

| Glossary term | Swift construct |
|---|---|
| **Module** | A Swift module (SwiftPM target / framework) at the large end; a `struct`/`class`/`actor` with its extensions at the small end. Use "module" for both — the glossary is scale-agnostic. |
| **Interface** | A `protocol` (with or without associated types) plus its documented invariants. For value-type modules, often a struct with a small set of `public` methods is the interface — the rest is internal. |
| **Implementation** | The conforming `struct`/`class`/`actor` and its private helpers. |
| **Adapter** | A concrete type conforming to a `protocol` at a seam. `URLSessionHTTPClient` (real) vs `MockHTTPClient` (test) both conform to `HTTPClient`. |
| **Seam** | Where a `protocol` parameter is injected — initializer parameter, function parameter, `@Environment` value in SwiftUI. The seam is *where* the protocol plugs in, not the protocol itself. |
| **Leverage** | Callers import one symbol and get a lot. `Combine.AnyPublisher`, `SwiftData.ModelContext` are leverage. |
| **Locality** | Bugs concentrate in one `struct`/`actor`. Compare: shallow Swift code spreads logic across helper extensions on `String`/`Array`/etc. with no single owner. |

## Shallow-module signals (Swift-specific)

1. **Single-method protocols with one conformer.** A `protocol` declared in the same file as its only concrete implementation. Hypothetical seam. Inline until a second conformer is real.
2. **Extension sprawl.** `extension String { func myFormat() -> String { ... } }` scattered across files, each adding one helper. Concentrate into a typed module with a clear interface.
3. **Sibling implementations that must agree.** A `iOSAuthFlow` and `macOSAuthFlow` with parallel logic where the shared rule (e.g. token refresh ordering) is untested. Two adapters = real seam — promote the shared logic to a deep module with platform-specific adapters.
4. **Pass-through view models.** A SwiftUI `ObservableObject` whose only job is to forward calls to a service. Deletion test: collapse the view model into the view, or push the work into a deeper service.
5. **`AnyView`-heavy abstractions.** Type-erased wrappers often mean the interface was over-generalised. Worth deepening into a typed seam.
6. **Combine pipelines as the public API.** Exposing `AnyPublisher<Order, Error>` as the interface forces every caller to learn Combine. Consider an `async`/`actor` interface that hides Combine behind the seam.

## Worked examples

### Example 1 — shallow → deep (in-process)

**Before (shallow):**

```swift
// OrderValidator.swift
struct OrderValidator { func validate(_ p: OrderPayload) throws -> ValidOrder { ... } }

// OrderEnricher.swift
struct OrderEnricher { func enrich(_ v: ValidOrder) -> EnrichedOrder { ... } }

// OrderPersister.swift
struct OrderPersister { func persist(_ e: EnrichedOrder) throws -> Order { ... } }

// OrderHandler.swift
struct OrderHandler {
    let validator = OrderValidator()
    let enricher = OrderEnricher()
    let persister = OrderPersister()
    func handle(_ p: OrderPayload) throws -> Order {
        try persister.persist(enricher.enrich(try validator.validate(p)))
    }
}
```

Four modules, four tests, three pass-throughs.

**After (deep, in-process):**

```swift
// OrderIntake.swift
public actor OrderIntake {
    /// Accept raw order payloads and persist them as Orders.
    /// - Invariant: validation errors throw before any persistence.
    /// - Invariant: enrichment is idempotent across retries.
    public init(db: Database) { ... }
    public func accept(_ payload: OrderPayload) async throws -> Order { ... }
}
```

One interface. `validator`/`enricher`/`persister` become private methods or local functions inside `OrderIntake`. Tests assert outcomes via `accept`.

Dependency category: **in-process**.

### Example 2 — ports & adapters (remote-but-owned)

**Before (shallow + leaky):**

```swift
struct PricingClient {
    let session: URLSession
    func price(for sku: String) async throws -> Decimal { ... }
}

actor OrderIntake {
    let pricing = PricingClient(session: .shared)  // network mid-logic
    ...
}
```

**After (deep, ports & adapters):**

```swift
public protocol PricingPort {
    func price(for sku: String) async throws -> Decimal
}

public struct HTTPPricingAdapter: PricingPort {
    let baseURL: URL
    let session: URLSession
    public func price(for sku: String) async throws -> Decimal { ... }
}

public struct InMemoryPricingAdapter: PricingPort {
    let prices: [String: Decimal]
    public func price(for sku: String) async throws -> Decimal {
        prices[sku] ?? .zero
    }
}

public actor OrderIntake {
    public init(pricing: PricingPort, db: Database) { ... }
}
```

Two adapters (HTTP for prod, in-memory for tests) justify the seam.

Dependency category: **ports-and-adapters**.

### Example 3 — actor as a deep module

A common Swift deepening is collapsing a `class` + `DispatchQueue` + a `protocol` for testability into one `actor` whose interface *is* the seam. The actor's isolation is what makes the deepening safe.

## Testing across the seam

| Dependency category | Test strategy |
|---|---|
| in-process | XCTest the actor/struct directly. No mocks. |
| local-substitutable | In-memory store (`NSManagedObjectContext` with in-memory `NSPersistentStoreDescription`, or SQLite via `GRDB.swift`), `URLProtocol` stubs for network-via-`URLSession`. |
| ports-and-adapters | Inject an in-memory adapter in tests. Production wires the HTTP/gRPC adapter. |
| mock | Inject a mock adapter. For Apple frameworks you can't conform (StoreKit), use the system's official sandboxes / test configurations rather than hand-rolled mocks. |

Tests assert observable outcomes through the deepened module's interface — never on internal state.
