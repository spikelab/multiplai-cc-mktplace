# Go Code Review Reference

Load when reviewing Go codebases.

## Issue Types

| Category | What to Check |
|----------|--------------|
| **Error Handling** | Every error checked, wrapped with context, sentinel errors vs types |
| **Concurrency** | Goroutine leaks, data races, channel usage, context propagation |
| **Interface Design** | Small interfaces, accept interfaces return structs, no unnecessary abstraction |
| **Resource Management** | `defer Close()`, context cancellation, connection pools |
| **Package Design** | No circular deps, proper visibility, package-level documentation |
| **Performance** | Allocation patterns, slice pre-allocation, string building |

## Common Mistakes

| Mistake | Why It Matters | Fix |
|---------|---------------|-----|
| `_ = err` or ignoring error return | Silent failures in production | Handle every error: `if err != nil { return fmt.Errorf("context: %w", err) }` |
| Goroutine without cancellation | Goroutine leak, resource exhaustion | Pass `context.Context`, check `ctx.Done()` |
| Closing channel from receiver | Panic if sender writes after close | Only sender closes channels |
| `defer` in loop | Defers pile up until function returns | Extract loop body to separate function |
| Naked `return` in named return function | Confusing, error-prone | Explicit return values for clarity |
| `append()` without reassignment | Original slice unchanged | `s = append(s, item)` |
| Range loop variable capture in goroutine | All goroutines share same variable | Copy: `v := v` before goroutine, or use Go 1.22+ loop semantics |
| `sync.WaitGroup` Add inside goroutine | Race condition — goroutine may not start before `Wait()` | Call `wg.Add(1)` before `go func()` |
| Shared map without mutex | Concurrent map write panic | Use `sync.Map` or protect with `sync.RWMutex` |
| `time.After` in select loop | Memory leak — timer never garbage collected | Use `time.NewTimer` with `Stop()` |
| String concatenation in loop | O(n²) allocation | Use `strings.Builder` |
| `interface{}` / `any` everywhere | Loses type safety | Use generics (Go 1.18+) or specific interfaces |

## Error Handling Checks

| Pattern | Problem | Fix |
|---------|---------|-----|
| `if err != nil { return err }` | Lost context | `return fmt.Errorf("fetching user %d: %w", id, err)` |
| `log.Fatal()` in library code | Kills caller's process | Return error, let caller decide |
| `panic()` for expected errors | Should be errors, not panics | Reserve panic for truly unrecoverable situations |
| Error type assertion without `errors.As` | Misses wrapped errors | Use `errors.Is()` / `errors.As()` |
| Sentinel error as `var` not `const`-like | Can be modified | Define with `var ErrNotFound = errors.New(...)` at package level |
| Error messages starting with capital or ending with punctuation | Go convention violation | Lowercase, no period: `"user not found"` |

## Concurrency Checks

| Pattern | Problem | Fix |
|---------|---------|-----|
| Goroutine without `context.Context` | Can't cancel, can't set deadline | Pass context, check `ctx.Done()` in loops |
| Unbuffered channel in producer-consumer | Deadlock if consumer is slow | Buffer appropriately or use worker pool |
| `select` without `default` or timeout | Can block forever | Add `case <-ctx.Done()` or timeout |
| Shared slice append from goroutines | Data race, corruption | Use mutex or channel for collection |
| `sync.Mutex` copied (passed by value) | Each copy has own lock state | Always pass `*sync.Mutex` by pointer |
| Missing `runtime.GOMAXPROCS` awareness | CPU-bound goroutines starve others | Design for cooperative scheduling |

## Interface Design

| Principle | Detail |
|-----------|--------|
| Accept interfaces, return structs | Callers define what they need, implementations are concrete |
| Small interfaces (1-3 methods) | `io.Reader`, `io.Writer`, `fmt.Stringer` — compose don't bloat |
| Define interface at consumer, not provider | Consumer knows what it needs |
| No "Impl" suffix | If you need `UserServiceImpl`, your interface is wrong |
| Avoid empty interface `any` on public API | Loses all type safety for callers |

## Testing Checks

| Issue | Fix |
|-------|-----|
| Test names don't describe scenario | `TestGetUser_ReturnsError_WhenNotFound` not `TestGetUser2` |
| Table-driven tests without subtests | Use `t.Run(name, func(t *testing.T) {...})` |
| Test modifies package-level state | Race conditions in parallel tests | Use `t.Parallel()` only with isolated state |
| No `-race` in CI | Misses data races | Always run `go test -race ./...` |
| Mock everything | Tests don't verify real behavior | Mock boundaries (DB, HTTP), not internal functions |

## Project Structure

| Issue | Fix |
|-------|-----|
| `package utils` / `package helpers` / `package common` | Grab-bag packages | Name by what it does: `package auth`, `package storage` |
| Exported function only used internally | Unnecessary public API | Unexport: `lowercase` first letter |
| `init()` with side effects | Hard to test, surprising behavior | Explicit initialization function called from `main` |
| Circular imports | Architecture problem | Extract shared types to separate package |

## Do NOT Flag (Go)

- Short variable names in small scope (`i`, `n`, `err`, `ctx`, `ok`)
- `if err != nil` repetition (idiomatic Go, not boilerplate)
- No generics where concrete types work fine (generics aren't always better)
- `panic` in `init()` for truly required configuration
- Exported types in `internal/` packages (still properly scoped)
- Blank identifier `_` for intentionally unused values (e.g., `_ = conn.Close()` when you can't handle the error)
- Multiple return values (Go idiom, not excessive returns)
- `switch` without `default` when all cases are handled
- `context.TODO()` as temporary placeholder (as long as it's actually TODO'd)
