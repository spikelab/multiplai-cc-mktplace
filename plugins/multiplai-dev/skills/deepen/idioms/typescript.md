# TypeScript idioms for `deepen`

How the glossary terms in [../LANGUAGE.md](../LANGUAGE.md) map to TypeScript constructs. The vocabulary doesn't change — only the constructs used to express it.

## Mapping

| Glossary term | TypeScript construct |
|---|---|
| **Module** | A file, a package (`package.json`), or a feature folder. Scale-agnostic. |
| **Interface** | An `interface` or `type` alias, plus the documented invariants (JSDoc), error union, ordering rules. Branded types (`Brand<string, "OrderId">`) often *are* the interface for value modules. |
| **Implementation** | The code inside — functions, classes, the closure captured by a factory function. |
| **Adapter** | A concrete object/class satisfying an `interface`. `FetchHttpAdapter` (real) vs `InMemoryHttpAdapter` (test). |
| **Seam** | Where the interface is plugged in — a function parameter, a constructor argument, a React context, a DI container key, a barrel re-export. |
| **Leverage** | Callers import one symbol and get a lot. `@tanstack/react-query`'s `useQuery` is leverage. `utils/formatDate` is not. |
| **Locality** | Bugs concentrate in one module. Shallow TS spreads logic across `helpers/`, `utils/`, `lib/` with no single owner. |

## Shallow-module signals (TypeScript-specific)

1. **Barrel re-exports as the module.** `export * from './foo'` everywhere, where the "module" is the barrel and the actual code is one level down with no other entry point. Deletion test usually says delete the barrel.
2. **Single-implementation interfaces.** `interface UserRepo {}` with one `class PostgresUserRepo implements UserRepo`. Hypothetical seam. Inline until you have a second implementation.
3. **Type-only files paired with logic files.** `types.ts` next to `service.ts`, used together everywhere. If `types.ts` is the interface of `service.ts`, just export the types from `service.ts` and delete the split.
4. **Util modules.** A `utils/` folder is a code-smell cluster — usually shallow modules that should belong to a deeper feature module.
5. **Class-and-factory pairs.** `class Foo` + `createFoo()` factory + `IFoo` interface, with one production wiring. Collapse to a function or a single class.
6. **`any` at the seam.** If the seam's type is `any` or `unknown` cast at the call site, the seam isn't really doing anything. Tighten the interface or remove the seam.
7. **Effect.ts / fp-ts pipelines exposed as the public API.** If callers must understand `Effect.gen`/`pipe` to use a module, the interface is leaking implementation. Consider a thin wrapper that hides the effect runtime.

## Worked examples

### Example 1 — shallow → deep (in-process)

**Before (shallow):**

```typescript
// order/validate.ts
export function validate(p: OrderPayload): ValidOrder { ... }

// order/enrich.ts
export function enrich(v: ValidOrder): EnrichedOrder { ... }

// order/persist.ts
export function persist(e: EnrichedOrder, db: Database): Order { ... }

// order/handle.ts
import { validate } from "./validate";
import { enrich } from "./enrich";
import { persist } from "./persist";

export function handleOrder(p: OrderPayload, db: Database): Order {
  return persist(enrich(validate(p)), db);
}
```

**After (deep, in-process):**

```typescript
// order/intake.ts

/**
 * Accept raw order payloads and persist as Orders.
 *
 * Invariants:
 *   - validation errors throw OrderInvalid before any persistence
 *   - enrichment is idempotent across retries
 */
export interface OrderIntake {
  accept(payload: OrderPayload): Promise<Order>;
}

export function createOrderIntake(db: Database): OrderIntake {
  // validate/enrich/persist live here as private functions
  return {
    async accept(payload) { ... }
  };
}
```

One interface. One test surface.

Dependency category: **in-process**.

### Example 2 — ports & adapters (remote-but-owned)

```typescript
// pricing/port.ts
export interface PricingPort {
  priceFor(sku: string): Promise<number>;
}

// pricing/adapters/http.ts
export const httpPricingAdapter = (baseUrl: string, fetch: Fetch): PricingPort => ({
  async priceFor(sku) { ... }
});

// pricing/adapters/in-memory.ts
export const inMemoryPricingAdapter = (prices: Record<string, number>): PricingPort => ({
  async priceFor(sku) { return prices[sku] ?? 0; }
});

// order/intake.ts
export function createOrderIntake(deps: { pricing: PricingPort; db: Database }): OrderIntake { ... }
```

Two adapters justify the seam. Tests pass `inMemoryPricingAdapter({...})`. Production passes `httpPricingAdapter(...)`.

Dependency category: **ports-and-adapters**.

### Example 3 — when *not* to deepen

```typescript
const orderKey = (id: OrderId) => `order:${id}`;
```

Used inside one module. Inline-readable. Don't extract it to a `keys.ts` module for testability — there's nothing to test.

## Testing across the seam

| Dependency category | Test strategy |
|---|---|
| in-process | Vitest/Jest against the interface directly. No mocks. |
| local-substitutable | `msw` for HTTP, in-memory SQLite (`better-sqlite3` for tests, Postgres for prod), `@testing-library/jest-dom` for DOM. |
| ports-and-adapters | Inject in-memory adapter in tests, real adapter in prod. |
| mock | `msw` is usually the right answer over hand-rolled mocks — record traffic, replay it. |

Tests assert observable outcomes through the deepened module's interface — never on internal state.
