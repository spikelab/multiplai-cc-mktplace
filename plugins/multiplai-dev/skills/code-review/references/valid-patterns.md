# Valid Patterns — Do NOT Flag

Load this file when unsure whether a pattern is a problem or an intentional choice. These patterns look wrong but are often correct.

## TypeScript / JavaScript

| Pattern | Why It's Valid |
|---------|---------------|
| `map.get(key) \|\| []` | Valid fallback for missing keys |
| `array.length === 0` check before `.map()` | Defensive but harmless |
| Array index as key in `.map()` for static lists | Valid when list doesn't reorder |
| Inline arrow in onClick (`onClick={() => fn()}`) | Fine — runs once per click, not per render |
| `any` type on event handlers or third-party interop | Sometimes the best option when types are missing |
| Empty dependency array with refs | Refs are stable, don't need deps |
| `useEffect` with empty deps for mount-only | Intentional — runs once |
| `// @ts-ignore` or `// @ts-expect-error` | Sometimes necessary for type system limitations |
| Re-export barrel files | Valid pattern for public APIs |
| Catch-all error boundary at app root | Intentional last-resort handler |

## Python

| Pattern | Why It's Valid |
|---------|---------------|
| `except Exception as e` with logging + re-raise | Valid when you need to log before propagating |
| `# type: ignore` | Sometimes necessary for mypy limitations |
| `**kwargs` passthrough | Valid delegation pattern |
| `if not x:` instead of `if x is None:` | Valid when falsy check is intentional |
| Single-method classes | Valid for dependency injection, strategy pattern |
| `noqa` comments on imports | Valid for side-effect imports or re-exports |
| `assert` in non-test code | Valid for invariant documentation (with proper error handling) |
| `pass` in exception handler | Valid when exception is expected and ignorable |

## React

| Pattern | Why It's Valid |
|---------|---------------|
| Props drilling 2-3 levels deep | Not everything needs context/state management |
| Inline styles for dynamic values | Valid when style depends on props/state |
| Multiple `useState` in one component | Often clearer than a reducer for unrelated state |
| Component file >200 lines | Length alone isn't a problem if well-organized |
| `dangerouslySetInnerHTML` with sanitized input | Valid when sanitization is verified |
| Fragment `<>...</>` with single child | No-op but harmless |

## General

| Pattern | Why It's Valid |
|---------|---------------|
| Magic numbers in tests | Tests prioritize readability over abstraction |
| Long function (>50 lines) with linear flow | If it's a sequence of steps, splitting adds indirection without clarity |
| Comments explaining "why" (not "what") | Good practice, not a code smell |
| Empty `__init__.py` files | Valid Python package markers |
| `TODO` with ticket/issue reference | Tracked debt, not forgotten debt |
| Multiple return statements | Often clearer than nested conditionals |
| Global constants at module level | Valid for configuration, not a code smell |
| Verbose variable names | Clarity > brevity |

## When in Doubt

If you're unsure whether a pattern is valid:

1. **Check if it's used consistently** elsewhere in the codebase (convention)
2. **Check if there's a comment** explaining the choice
3. **Check if it's documented** in CLAUDE.md, README, or contributing guide
4. **Downgrade to "Note"** instead of flagging as an issue
5. **Ask** — "Is this pattern intentional?" is better than a false positive
