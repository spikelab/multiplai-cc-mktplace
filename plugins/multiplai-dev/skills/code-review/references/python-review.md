# Python Code Review Reference

Load when reviewing Python codebases.

## Issue Types

| Category | What to Check |
|----------|--------------|
| **Type Safety** | Type hints on public APIs, `mypy` or `pyright` compliance, `Any` overuse, missing return types |
| **Async Patterns** | `await` on all coroutines, no sync calls in async context, proper `asyncio.gather` usage, connection pool exhaustion |
| **Error Handling** | Specific exceptions (not bare `except`), context in error messages, `raise from` for chaining |
| **Import Hygiene** | No circular imports, no star imports, sorted/grouped imports (stdlib → third-party → local) |
| **Data Modeling** | Pydantic v2 for validation, dataclasses for simple data, proper `__init__` signatures |
| **Resource Management** | Context managers for files/connections/locks, `finally` blocks, proper cleanup |

## Common Mistakes

| Mistake | Why It Matters | Fix |
|---------|---------------|-----|
| Mutable default argument `def f(x=[])` | Shared state across calls | Use `None` sentinel: `def f(x=None): x = x or []` |
| `except Exception: pass` | Swallows all errors silently | Catch specific exceptions, log, or re-raise |
| `time.sleep()` in async code | Blocks the event loop | Use `await asyncio.sleep()` |
| `os.system()` or `subprocess(shell=True)` | Shell injection risk + harder to debug | Use `subprocess.run([...], shell=False)` |
| `dict[key]` without existence check | KeyError in production | Use `.get(key, default)` or check `if key in dict` |
| String concatenation for SQL | SQL injection | Use parameterized queries |
| `global` keyword usage | Hidden state, testing nightmare | Pass as parameter or use class state |
| `from module import *` | Namespace pollution, unclear dependencies | Import specific names |
| Catching `KeyboardInterrupt` or `SystemExit` | Prevents clean shutdown | Use `except Exception` (excludes these) |
| `== None` instead of `is None` | Can be overridden by `__eq__` | Always use `is None` / `is not None` |
| `pickle.loads()` on untrusted data | Remote code execution | Use `json.loads()` or validate source |
| `yaml.load()` without `Loader` | Arbitrary code execution | Use `yaml.safe_load()` |

## Async-Specific Checks

| Pattern | Problem | Fix |
|---------|---------|-----|
| `requests.get()` in `async def` | Blocks event loop | Use `httpx.AsyncClient` or `aiohttp` |
| Missing `await` on coroutine | Coroutine never executes, no error | Check all coroutine calls have `await` |
| `asyncio.run()` inside running loop | RuntimeError | Use `await` directly or `loop.create_task()` |
| Unbounded `asyncio.gather()` | Memory/connection exhaustion | Use `asyncio.Semaphore` for concurrency limits |
| `async for` without proper iterator | Silent failures | Verify `__aiter__` and `__anext__` |
| Fire-and-forget tasks without error handling | Lost exceptions | Store task reference, add `add_done_callback` |

## Type Hint Checks

| Issue | Example | Fix |
|-------|---------|-----|
| Missing return type on public function | `def get_user(id):` | `def get_user(id: int) -> User:` |
| `Any` on function boundary | `def process(data: Any)` | Use specific type or generic `T` |
| `Optional` without null handling | `name: Optional[str]` used without check | Guard with `if name is not None` |
| Mutable type hint on class var | `items: list[str] = []` | Use `field(default_factory=list)` |
| `dict` instead of `TypedDict` for structured data | Untyped access | Define `TypedDict` or Pydantic model |

## Pydantic-Specific (v2)

| Issue | Fix |
|-------|-----|
| Using `dict()` instead of `model_dump()` | Pydantic v2 deprecates `dict()` |
| `orm_mode` in Config | Use `model_config = ConfigDict(from_attributes=True)` |
| `@validator` | Use `@field_validator` (v2) |
| Schema without examples | Add `json_schema_extra` for API docs |
| Missing `model_validate()` for external data | Don't construct models without validation |

## FastAPI-Specific

| Issue | Fix |
|-------|-----|
| Sync endpoint doing I/O | Use `async def` + async client |
| Missing `Depends()` for shared logic | Extract to dependency injection |
| No response model on endpoint | Add `response_model=` for auto-docs and validation |
| Bare `HTTPException` without detail | Add meaningful `detail=` message |
| Missing status code on response | Explicit `status_code=` on all endpoints |

## Do NOT Flag (Python)

- `# type: ignore` with a comment explaining why
- `noqa` on side-effect imports (e.g., `import uvloop; uvloop.install()`)
- `**kwargs` passthrough in decorator/wrapper functions
- `assert` in tests (obviously)
- Single-letter variables in comprehensions (`[x for x in items]`)
- `pass` in abstract method bodies
- `...` (Ellipsis) as placeholder in type stubs or Protocol methods
- f-strings for logging (unless in hot path where lazy `%s` matters)
- `if not x:` when falsy check is intentional (empty list, zero, etc.)
