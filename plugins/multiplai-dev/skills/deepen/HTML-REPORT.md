# HTML Report Format

The architectural review is rendered as a single self-contained HTML file in the OS temp directory. The LLM produces a JSON file; `scripts/render_report.py` turns it into HTML using a Jinja2 template (`scripts/templates/report.html.j2`) and opens it via `scripts/open_report.py`. Tailwind and Mermaid come from CDNs.

This file is the **spec for the JSON contract** and the **editorial guide** for what each card should contain. The Jinja template is the single source of truth for the HTML.

## JSON contract

```json
{
  "repo": "knowhere/PROJECTS/multiplai",
  "generated_at": "2026-05-21T10:30:00Z",
  "candidates": [
    {
      "title": "Collapse the Order intake pipeline",
      "files": ["src/order/handler.py", "src/order/validator.py"],
      "problem": "Order intake module is shallow — interface nearly matches the implementation.",
      "solution": "Deepen: one interface, one place to test.",
      "wins": [
        "locality: bugs concentrate in one module",
        "leverage: one interface, N call sites",
        "delete 4 shallow wrappers"
      ],
      "strength": "Strong",
      "dependency_category": "in-process",
      "diagram": {
        "kind": "mermaid",
        "before": "flowchart LR\n  A[Handler] --> B[Validator]\n  B --> C[Repo]\n  C -.leak.-> D[PricingClient]\n  classDef leak stroke:#dc2626,stroke-width:2px;\n  class C,D leak",
        "after":  "flowchart LR\n  A[Handler] --> D[OrderIntake]"
      },
      "adr_callout": null
    }
  ],
  "top_recommendation": {
    "index": 0,
    "why": "Highest locality gain, no ADR conflict, no cross-seam dependencies."
  }
}
```

### Field rules

- **title** — short, names the deepening (e.g. "Collapse the Order intake pipeline").
- **files** — list of file paths involved. Rendered monospace.
- **problem / solution** — one sentence each. Use glossary terms verbatim.
- **wins** — bullets, ≤6 words each. Frame in terms of locality / leverage / interface / seam. Avoid "easier to maintain," "cleaner code" — those aren't in the glossary.
- **strength** — one of `"Strong"` (emerald), `"Worth exploring"` (amber), `"Speculative"` (slate).
- **dependency_category** — one of `"in-process"`, `"local-substitutable"`, `"ports-and-adapters"`, `"mock"` (see [DEEPENING.md](DEEPENING.md)).
- **diagram.kind** — currently `"mermaid"` is the only supported kind. Hand-built SVG support may come later; for now, lean on Mermaid `flowchart`, `graph`, or `sequenceDiagram`.
- **diagram.before / diagram.after** — raw Mermaid source. The template wraps each in a `<pre class="mermaid">` inside a Tailwind card.
- **adr_callout** — `null` or a one-line string like `"Contradicts ADR-0007 — but worth reopening because the seam is now untested."` Rendered as an amber-tinted box.
- **top_recommendation.index** — zero-based index into `candidates`.

## Diagram patterns

Pick the pattern that fits the candidate. Mix them. Don't make every diagram look the same — variety is part of the point.

### Mermaid graph (the workhorse for dependencies / call flow)

Use a Mermaid `flowchart` or `graph` when the point is "X calls Y calls Z, and look at the mess." Use `classDef leak stroke:#dc2626,stroke-width:2px` for leakage edges.

### Sequence diagrams (for round-trips)

Use `sequenceDiagram` when the win is collapsing 6 round-trips into 1.

### Subgraph collapse

In the "after" diagram, wrap the now-internal modules in a Mermaid `subgraph` to show they sit inside the deep module.

## Editorial tone

Plain English, concise — but the architectural nouns and verbs come straight from [LANGUAGE.md](LANGUAGE.md). Concision is not an excuse to drift.

**Use exactly:** module, interface, implementation, depth, deep, shallow, seam, adapter, leverage, locality.

**Never substitute:** component, service, unit (for module) · API, signature (for interface) · boundary (for seam) · layer, wrapper (for module, when you mean module).

**Phrasings that fit the style:**

- "Order intake module is shallow — interface nearly matches the implementation."
- "Pricing leaks across the seam."
- "Deepen: one interface, one place to test."
- "Two adapters justify the seam: HTTP in prod, in-memory in tests."

**Wins bullets** name the gain in glossary terms: *"locality: bugs concentrate in one module"*, *"leverage: one interface, N call sites"*, *"interface shrinks; implementation absorbs the wrappers"*. Don't write *"easier to maintain"* or *"cleaner code"* — those terms aren't in the glossary and don't earn their place.

No hedging, no throat-clearing, no "it's worth noting that…". If a sentence could be a bullet, make it a bullet. If a bullet could be cut, cut it. If a term isn't in [LANGUAGE.md](LANGUAGE.md), reach for one that is before inventing a new one.
