# JavaScript / TypeScript Code Review Reference

Load when reviewing JS/TS codebases.

## Issue Types

| Category | What to Check |
|----------|--------------|
| **Type Safety** | `strict: true` in tsconfig, no `any` leaks, proper generics, discriminated unions for state |
| **React Patterns** | Hook rules, proper memoization, effect cleanup, key props, error boundaries |
| **Async/Promise** | Unhandled rejections, race conditions, proper AbortController usage, error propagation |
| **Module System** | Tree-shaking friendly exports, no circular deps, barrel file impact on bundle |
| **Error Handling** | Error boundaries in React, try/catch on async ops, meaningful error types |
| **Performance** | Unnecessary re-renders, bundle size, lazy loading, memo usage |

## Common Mistakes

| Mistake | Why It Matters | Fix |
|---------|---------------|-----|
| `==` instead of `===` | Type coercion surprises (`"0" == false` is true) | Always use `===` |
| `any` type spreading | Disables type checking downstream | Use `unknown` then narrow, or proper generics |
| Missing `await` on async call | Returns Promise instead of value, no error | Check all async calls |
| `forEach` with async callback | Doesn't await — runs all in parallel, silently | Use `for...of` with `await` or `Promise.all(items.map(...))` |
| `JSON.parse()` without try/catch | Throws on invalid JSON | Wrap in try/catch or validate |
| Object spread for deep clone | Only shallow — nested objects share refs | Use `structuredClone()` or explicit deep copy |
| `Array.indexOf()` for existence | Returns -1 not false, error-prone | Use `Array.includes()` |
| String comparison for dates | Lexicographic, not chronological | Compare `Date` objects or timestamps |
| `delete obj.prop` | Deoptimizes V8 hidden classes | Set to `undefined` or use `Map` |
| Prototype pollution via `Object.assign` | Security vulnerability | Use `Object.create(null)` as target or validate keys |
| `innerHTML` with user data | XSS vulnerability | Use `textContent` or sanitize |
| `eval()` or `new Function()` | Code injection | Never with user input |

## React-Specific Checks

| Issue | Why | Fix |
|-------|-----|-----|
| Missing dependency in `useEffect` deps | Stale closure, bugs | Add to deps array or use `useRef` |
| Inline object/function in JSX props | New reference every render → child re-renders | Move outside component or `useMemo`/`useCallback` |
| State update in render body | Infinite re-render loop | Move to `useEffect` or event handler |
| Missing `key` prop in list | React can't track identity, buggy updates | Use stable unique ID (not array index if list reorders) |
| Missing error boundary | One component crash kills entire app | Wrap sections in `ErrorBoundary` |
| `useEffect` without cleanup | Memory leaks, stale subscriptions | Return cleanup function |
| Prop drilling 5+ levels | Hard to maintain | Extract to Context or state management |
| Direct DOM manipulation | Fights React's reconciler | Use refs and React APIs |

## TypeScript-Specific Checks

| Issue | Fix |
|-------|-----|
| `as` type assertion instead of narrowing | Use type guards: `if ('prop' in obj)` or `instanceof` |
| Non-null assertion `!` on uncertain values | Add proper null check |
| `enum` in library code | Use `as const` objects for tree-shaking |
| `interface` vs `type` inconsistency | Pick one convention per project and stick with it |
| `Partial<T>` where specific optionals intended | Define explicit optional fields |
| Missing `readonly` on arrays/objects that shouldn't mutate | Add `readonly` or `Readonly<T>` |
| Generic `Record<string, any>` | Define proper type or use `unknown` values |

## Async/Promise Checks

| Issue | Fix |
|-------|-----|
| Unhandled promise rejection | Add `.catch()` or wrap in try/catch with `await` |
| Missing `AbortController` on fetch | Memory leak, race condition on unmount | Pass `signal` to fetch, abort on cleanup |
| `Promise.all` without error strategy | One rejection kills all | Use `Promise.allSettled` when partial success OK |
| Sequential awaits that could be parallel | `const [a, b] = await Promise.all([fetchA(), fetchB()])` |
| Timeout missing on external calls | Hangs forever | Add `AbortSignal.timeout(ms)` |

## Node.js-Specific

| Issue | Fix |
|-------|-----|
| `process.exit()` in library code | Let caller decide lifecycle | Throw error instead |
| Sync file ops in request handler | Blocks event loop | Use `fs.promises` |
| `require()` at runtime | Blocks, defeats tree-shaking | Use dynamic `import()` |
| Missing `process.on('unhandledRejection')` | Silent failures in production | Add handler, log and exit |
| `Buffer.from()` with encoding issues | Security and data corruption | Specify encoding explicitly |

## Do NOT Flag (JS/TS)

- Array index as `key` for static, non-reorderable lists
- `any` on event handler params from third-party libraries without types
- `// @ts-ignore` or `// @ts-expect-error` with explanatory comment
- Inline arrow functions in `onClick` (not a real performance issue)
- Empty dependency array `[]` in `useEffect` for mount-only effects
- `useRef` without `.current` in deps array (refs are stable)
- Optional chaining chains (`a?.b?.c?.d`) — valid defensive access
- Template literals for simple string building
- `console.log` in development code (should be stripped in prod build)
- Barrel files (`index.ts` re-exports) in application code (tree-shaking is the bundler's job)
