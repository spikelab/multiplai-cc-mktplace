# React idioms for `deepen`

How the glossary terms in [../LANGUAGE.md](../LANGUAGE.md) map to React constructs. The vocabulary doesn't change — only the constructs used to express it. Pairs with [typescript.md](typescript.md) for the language-level idioms.

## Mapping

| Glossary term | React construct |
|---|---|
| **Module** | A component file, a hook file, a feature folder, or a Context provider+consumer pair. Scale-agnostic. |
| **Interface** | For components: the `Props` type plus what the component renders and what events it raises. For hooks: the return shape + the parameters. Includes invariants ("`onChange` fires after state updates", "`data` is `undefined` while `isLoading`"). |
| **Implementation** | What's inside the component/hook — internal state, derived values, effects, helper sub-components. |
| **Adapter** | A concrete provider/hook implementation at a seam. Real `QueryClient` (prod) vs in-memory `QueryClient` (test). Real `AuthProvider` vs `MockAuthProvider`. |
| **Seam** | Where a hook or Context is consumed — `useX()`, `useContext(XContext)`, `<XProvider>` boundary, render props, slot props (`<X header={...} />`). |
| **Leverage** | Callers import one hook and get a lot. `useQuery`, `useForm` are leverage. `useDebounce` usually isn't. |
| **Locality** | A feature's state, effects, and UI live in one component or hook tree. Compare: state in Redux, effects in `useEffect`s scattered across views, UI in components that don't know which slice they read. |

## Shallow-module signals (React-specific)

1. **Pass-through components.** `<Wrapper>{props.children}</Wrapper>` that adds nothing observable. Deletion test usually says delete it.
2. **`useState` + `useEffect` ping-pong.** A `useEffect` whose only job is to derive state from props. Replace with derived values (compute on render) or `useMemo`. The hook was a shallow module hiding a one-liner.
3. **Custom hooks with one caller.** `useFooBar` used by exactly one component, with no plans for reuse. Inline it. (Custom hooks are deepening *only* when they consolidate logic across N callers.)
4. **Context provider with one consumer.** Hypothetical seam. Use props until a second consumer is real.
5. **Render-prop / HOC patterns to avoid a hook.** Render props can be a valid seam, but if the only reason for the indirection is "to make it testable" with no second adapter, it's just indirection.
6. **Sibling components that must agree.** `<MobileOrderForm>` and `<DesktopOrderForm>` with parallel validation logic. Two adapters = real seam — extract a `useOrderForm` hook (the deep module) that both consume.
7. **Effects that fetch.** `useEffect(() => { fetch(...) }, [...])` re-rolling a query library badly. The right answer is almost always `useQuery`/`useSWR` — those are deep modules; your hand-rolled fetch effect is shallow.

## Worked examples

### Example 1 — shallow → deep (custom hook)

**Before (shallow + leaky):**

```tsx
function OrderForm() {
  const [payload, setPayload] = useState<OrderPayload>(empty);
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => { setErrors(validate(payload)); }, [payload]);

  const submit = async () => {
    setSubmitting(true);
    try { await api.placeOrder(payload); }
    finally { setSubmitting(false); }
  };

  return <form>...</form>;
}
```

State, validation, submission, error handling — all in the view. No reuse possible; tests must render the whole form.

**After (deep, hook as the interface):**

```tsx
// useOrderForm.ts — the deep module

/**
 * Manage order form state, validation, and submission.
 *
 * Invariants:
 *   - errors updates synchronously with payload changes
 *   - submit() rejects if errors is non-empty
 *   - submitting flips false after submit() resolves or rejects
 */
export interface OrderFormState {
  payload: OrderPayload;
  errors: Errors;
  submitting: boolean;
  setField<K extends keyof OrderPayload>(k: K, v: OrderPayload[K]): void;
  submit(): Promise<Order>;
}

export function useOrderForm(deps: { api: OrderApi }): OrderFormState { ... }

// OrderForm.tsx — the view, now thin
function OrderForm() {
  const { payload, errors, submitting, setField, submit } = useOrderForm({ api });
  return <form>...</form>;
}
```

Tests target `useOrderForm` via `renderHook` with an in-memory `OrderApi` adapter. The view test is now a single render test ("renders fields and disables submit while submitting"), not a logic test.

Dependency category: **in-process** for the state logic, **ports-and-adapters** for the `OrderApi` seam.

### Example 2 — Context as a real seam

Only promote to Context when you have ≥2 consumers and the value genuinely sits above them in the tree. Otherwise, lift state to the common parent and pass props.

```tsx
// auth/AuthPort.ts
export interface AuthPort {
  user: User | null;
  signIn(creds: Credentials): Promise<void>;
  signOut(): Promise<void>;
}

// auth/HTTPAuthAdapter.tsx — real
export function HTTPAuthProvider({ children }: { children: ReactNode }) { ... }

// auth/InMemoryAuthAdapter.tsx — test
export function InMemoryAuthProvider({ user, children }: { user: User | null; children: ReactNode }) { ... }

// auth/useAuth.ts
export const useAuth = (): AuthPort => { ... };
```

Two adapters (HTTP prod, in-memory test) justify the seam. Tests wrap components in `<InMemoryAuthProvider user={fakeUser}>`.

Dependency category: **ports-and-adapters**.

### Example 3 — when *not* to deepen

A `<Button>` with `variant` and `size` props. Already the right depth — one prop, a lot of behaviour (focus, disabled, loading, variant). Don't deepen further; don't shallow it out into `<PrimaryButton>`/`<SecondaryButton>` either.

## Testing across the seam

| Dependency category | Test strategy |
|---|---|
| in-process | `renderHook` for hooks, `render` + `@testing-library/react` for components. Assert on what the user sees. |
| local-substitutable | `msw` for HTTP, `@testing-library/user-event` for interactions, `jsdom` defaults for storage/timers. |
| ports-and-adapters | Wrap in an in-memory provider in tests, real provider in prod. |
| mock | `msw` over hand-rolled `vi.mock(...)` for network. For browser APIs, use the official jsdom mocks rather than monkey-patching. |

Tests assert what the user sees through the interface — never on internal hook state or component instance variables.
