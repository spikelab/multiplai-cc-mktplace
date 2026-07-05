# Python idioms for `deepen`

How the glossary terms in [../LANGUAGE.md](../LANGUAGE.md) map to Python constructs. The vocabulary doesn't change — only the constructs used to express it.

## Mapping

| Glossary term | Python construct |
|---|---|
| **Module** | A package (directory with `__init__.py`), a single `.py` file, or a class that owns its own state. The scale-agnosticism of "module" is the point — don't say "file" or "class" unless that's what's being deepened. |
| **Interface** | A `typing.Protocol` (structural) or `abc.ABC` (nominal), plus the docstring invariants. Includes type signature, ordering rules, error modes (`raises:` in docstring), required config. Not just `def foo(x: int) -> str`. |
| **Implementation** | The body of the module — code inside the package/file/class. |
| **Adapter** | A concrete class implementing a `Protocol`/`ABC`. Example: `PostgresOrderRepo` (real) vs `InMemoryOrderRepo` (test) both satisfy `OrderRepo: Protocol`. |
| **Seam** | The injection point — a constructor parameter, a function parameter, a pytest fixture, a `Depends(...)` in FastAPI. **Not** the `Protocol` itself (the Protocol is the interface; the seam is *where* it's plugged in). |
| **Leverage** | Callers import one symbol and get a lot. `polars.DataFrame` is leverage. `helpers.format_date` is not. |
| **Locality** | Bugs in the deepened module are fixed in one file. Compare: bugs in a shallow module are fixed in the one file *plus* the N callers that worked around its quirks. |

## Shallow-module signals (Python-specific)

When exploring with the Agent tool, treat these as friction signals, not proofs:

1. **Pass-through modules.** A file whose entire content is `from .impl import foo` or a function that calls one inner function and returns its result. Apply the deletion test.
2. **Re-export barrels.** An `__init__.py` that only re-exports — fine for public API surface, suspicious if it's the only place a "module" lives.
3. **Single-call helpers.** A function called from exactly one place, where the call site is itself small. Often a misguided "for testability" extraction with no locality.
4. **Wide-and-thin dataclasses.** A `@dataclass` with 12 fields used by every caller, where each caller picks a different subset. The interface is the whole dataclass; the implementation is nothing. Candidate for deepening into a typed-method API.
5. **Sibling implementations that must agree.** Two modules with parallel logic (e.g. validation in `api/` and `worker/`) whose seam (the rule they share) is untested. One adapter = hypothetical seam; two = real seam — promote it.
6. **`Protocol` with one implementation.** A `Protocol` defined alongside a single concrete class. Hypothetical seam. Inline the class until a second implementation justifies the indirection.

## Worked examples

### Example 1 — shallow → deep (in-process)

**Before (shallow):**

```python
# order/handler.py
def handle_order(payload: dict) -> Order:
    validated = validate(payload)
    enriched = enrich(validated)
    return persist(enriched)

# order/validator.py
def validate(payload: dict) -> dict: ...

# order/enricher.py
def enrich(payload: dict) -> dict: ...

# order/persister.py
def persist(payload: dict) -> Order: ...
```

Three modules, three tests, three pass-throughs. The actual bugs live in *how the three combine* — but there's no test for the combination.

**After (deep, in-process):**

```python
# order/intake.py
class OrderIntake:
    """Accept raw order payloads, persist as Orders.

    Invariants:
      - validation errors raise OrderInvalid before any persistence happens
      - enrichment is idempotent across retries
    """
    def __init__(self, db: Database) -> None: ...

    def accept(self, payload: dict) -> Order:
        ...
```

One interface. One test surface. The validator/enricher/persister live as private functions inside `intake.py` if they help; otherwise they're just inlined steps. Tests assert outcomes through `OrderIntake.accept` — no test reaches past it.

Dependency category: **in-process**. No adapter needed.

### Example 2 — ports & adapters (remote-but-owned)

**Before (shallow + leaky):**

```python
# pricing/client.py — calls internal pricing service over HTTP
def fetch_price(sku: str) -> Decimal: ...

# order/intake.py
def accept(payload):
    ...
    price = fetch_price(payload["sku"])   # network in the middle of business logic
    ...
```

Logic and transport are tangled. Tests have to mock `requests` or hit a live service.

**After (deep, ports & adapters):**

```python
# pricing/port.py
class PricingPort(Protocol):
    def price_for(self, sku: str) -> Decimal: ...

# pricing/adapters/http.py
class HttpPricingAdapter:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None: ...
    def price_for(self, sku: str) -> Decimal: ...

# pricing/adapters/in_memory.py
class InMemoryPricingAdapter:
    def __init__(self, prices: dict[str, Decimal]) -> None: ...
    def price_for(self, sku: str) -> Decimal: ...

# order/intake.py
class OrderIntake:
    def __init__(self, pricing: PricingPort, db: Database) -> None: ...
```

Two adapters (HTTP for prod, in-memory for tests) justify the seam. Tests construct `OrderIntake(InMemoryPricingAdapter({"sku-1": Decimal("9.99")}), db)` and run real logic without a network.

Dependency category: **ports-and-adapters**.

### Example 3 — when *not* to deepen

```python
class _CacheKey(NamedTuple):
    user_id: int
    feature: str
```

Used in one place. Trivially understandable inline. Deletion test: deleting it concentrates nothing — there's nothing to concentrate. Leave it.

## Testing across the seam

| Dependency category | Test strategy |
|---|---|
| in-process | Test the deepened module's interface directly. No mocks. |
| local-substitutable | Use the stand-in (`pglite`-style → SQLite in tests, `pyfakefs` for filesystem, `freezegun` for time). The stand-in is part of the test suite, not part of the production interface. |
| ports-and-adapters | Inject an in-memory adapter in tests. Production wires the HTTP/gRPC/queue adapter. |
| mock | Inject a mock adapter for the third-party (Stripe, Twilio, OpenAI). Prefer recorded responses (`vcrpy`) over hand-built mocks for fidelity. |

Tests assert observable outcomes through the deepened module's interface — never on internal state.
