---
name: deepen
description: Find deepening opportunities in a codebase — places where shallow modules can be collapsed into deep ones with cleaner seams, better tests, and clearer AI-navigability. Informed by the project's domain language (CONTEXT.md) and prior decisions (docs/adr/). Works across languages, with idiom packs for Python, Swift, TypeScript, and React. Use when the user wants to improve architecture, audit for refactoring opportunities, consolidate tightly-coupled modules, or harden a codebase before letting agents loose on it.
---

# Deepen

Surface architectural friction and propose **deepening opportunities** — refactors that turn shallow modules into deep ones. The aim is testability and AI-navigability.

> **Prerequisites:** the HTML report renderer runs via `uv` (https://docs.astral.sh/uv/) — `uv run` reads the script's inline PEP 723 header and fetches its deps (network needed on first run). The analysis itself needs no network.

Ported from Matt Pocock's `improve-codebase-architecture` skill; the vocabulary and process are his. What we changed and what we deliberately left behind is recorded under [Divergence from upstream](#divergence-from-upstream), with the commit this was last synced against.

## Glossary

Use these terms exactly in every suggestion. Consistent language is the point — don't drift into "component," "service," "API," or "boundary." Full definitions in [LANGUAGE.md](LANGUAGE.md).

- **Module** — anything with an interface and an implementation (function, class, package, slice).
- **Interface** — everything a caller must know to use the module: types, invariants, error modes, ordering, config. Not just the type signature.
- **Implementation** — the code inside.
- **Depth** — leverage at the interface: a lot of behaviour behind a small interface. **Deep** = high leverage. **Shallow** = interface nearly as complex as the implementation.
- **Seam** — where an interface lives; a place behaviour can be altered without editing in place. (Use this, not "boundary.")
- **Adapter** — a concrete thing satisfying an interface at a seam.
- **Leverage** — what callers get from depth.
- **Locality** — what maintainers get from depth: change, bugs, knowledge concentrated in one place.

Key principles (see [LANGUAGE.md](LANGUAGE.md) for the full list):

- **Deletion test**: imagine deleting the module. If complexity vanishes, it was a pass-through. If complexity reappears across N callers, it was earning its keep.
- **The interface is the test surface.**
- **One adapter = hypothetical seam. Two adapters = real seam.**

This skill is _informed_ by the project's domain model. The domain language gives names to good seams; ADRs record decisions the skill should not re-litigate. Both are optional — degrade gracefully if absent.

## Language detection

Before exploring, detect the repo's primary language(s) and load the matching idiom pack(s) from `idioms/`:

- `idioms/python.md` — Python: `Protocol`, `ABC`, DI, fixtures, packages.
- `idioms/swift.md` — Swift: `protocol`, associated types, actors, value vs reference.
- `idioms/typescript.md` — TypeScript: `interface`, discriminated unions, branded types, barrels.
- `idioms/react.md` — React: hook seams, providers, render props, RTL test seams.

Heuristic: count files by extension in the repo root and `src/` — `.py`, `.swift`, `.ts/.tsx/.js/.jsx`. Load every pack with a meaningful share (≥10% or ≥20 files). For mixed repos, load multiple. If no idiom pack matches, proceed with `LANGUAGE.md` vocabulary only — the glossary is language-agnostic.

The idiom pack tells you how the **glossary terms map to that language's natural constructs** — never replace the glossary, only translate it.

## Process

### 1. Explore

**Scope before you scan: YAGNI.** Deepening a module pays off by making future changes to it easier, so put extra weight on the parts of the codebase that have recently changed. Decide *where* to look before you look:

- If the user named a direction (a module, a subsystem, a pain point), take it, and skip the inference below.
- Otherwise, walk back a good stretch of the commit history (`git log --oneline`) to find the codebase's hot spots — the files and areas that keep coming up — and let those paths pull your attention first. If the changes are scattered with no clear hot spot, widen the net.

Read the project's domain glossary (`CONTEXT.md`) and any ADRs in the area you're touching first. Either may be absent — that's fine.

Then use the Agent tool with `subagent_type=Explore` to walk the codebase. Don't follow rigid heuristics — explore organically and note where you experience friction:

- Where does understanding one concept require bouncing between many small modules?
- Where are modules **shallow** — interface nearly as complex as the implementation?
- Where have pure functions been extracted just for testability, but the real bugs hide in how they're called (no **locality**)?
- Where do tightly-coupled modules leak across their seams?
- Which parts of the codebase are untested, or hard to test through their current interface?

Apply the **deletion test** to anything you suspect is shallow: would deleting it concentrate complexity, or just move it? A "yes, concentrates" is the signal you want.

### 2. Present candidates as an HTML report

The candidates are written as JSON, then rendered to HTML by `scripts/render_report.py`. You produce the JSON; the script handles all styling and opens the report in the user's browser.

**Step-by-step:**

1. Build a list of 3–8 candidates. Each candidate is an object with these fields (full schema in [HTML-REPORT.md](HTML-REPORT.md)):

   ```json
   {
     "title": "Collapse the Order intake pipeline",
     "files": ["src/order/handler.py", "src/order/validator.py"],
     "problem": "Order intake module is shallow — interface nearly matches the implementation.",
     "solution": "Deepen: one interface, one place to test.",
     "wins": ["locality: bugs concentrate in one module", "leverage: one interface, N call sites"],
     "strength": "Strong",
     "dependency_category": "in-process",
     "diagram": {
       "kind": "mermaid",
       "before": "flowchart LR\n  A[Handler] --> B[Validator]\n  B --> C[Repo]",
       "after":  "flowchart LR\n  A[Handler] --> D[OrderIntake]"
     },
     "adr_callout": null
   }
   ```

2. Write the JSON to `$TMPDIR/deepen-candidates-<timestamp>.json` (fall back to `/tmp`, or `%TEMP%` on Windows).

3. Run the renderer:

   ```
   uv run ${CLAUDE_PLUGIN_ROOT}/skills/deepen/scripts/render_report.py \
     --in <path-to-candidates.json> \
     --repo <repo-name> \
     --open
   ```

   (`uv run` reads the script's inline PEP 723 header and provides `jinja2`
   in an ephemeral env — no manual install.)

   The script writes `architecture-review-<timestamp>.html` to the same temp dir and opens it (`xdg-open` / `open` / `start`). Tell the user the absolute path.

**What goes in each card:** see [HTML-REPORT.md](HTML-REPORT.md) for the field-by-field spec, the badge colour for `strength`, and the diagram patterns to choose from.

**Use CONTEXT.md vocabulary for the domain, and [LANGUAGE.md](LANGUAGE.md) vocabulary for the architecture.** If `CONTEXT.md` defines "Order," talk about "the Order intake module" — not "the FooBarHandler," and not "the Order service."

**ADR conflicts**: if a candidate contradicts an existing ADR, only surface it when the friction is real enough to warrant revisiting the ADR. Use the `adr_callout` field to flag it. Don't list every theoretical refactor an ADR forbids.

Do NOT propose interfaces yet. After the file is open, ask the user: "Which of these would you like to explore?"

### 3. Grilling loop

Once the user picks a candidate, drop into a grilling conversation. Walk the design tree with them — constraints, dependencies, the shape of the deepened module, what sits behind the seam, what tests survive.

Use **glossary terms from LANGUAGE.md** for the architecture. Use **native idioms from `idioms/<lang>.md`** when sketching interfaces — Python `Protocol`, Swift `protocol`, TS `interface` — never default to TypeScript just because Matt's original demo was TS.

Side effects happen inline as decisions crystallize:

- **Naming a deepened module after a concept not in `CONTEXT.md`?** Add the term to `CONTEXT.md`. Create the file lazily at the repo root if it doesn't exist; format is a flat markdown glossary of domain nouns and verbs with one-sentence definitions.
- **Sharpening a fuzzy term during the conversation?** Update `CONTEXT.md` right there.
- **User rejects the candidate with a load-bearing reason?** Offer an ADR, framed as: _"Want me to record this as an ADR so future architecture reviews don't re-suggest it?"_ Only offer when the reason would actually be needed by a future explorer to avoid re-suggesting the same thing — skip ephemeral reasons ("not worth it right now") and self-evident ones. Default location: `docs/adr/NNNN-<slug>.md`, using the standard Michael Nygard format.
- **Want to explore alternative interfaces for the deepened module?** See [INTERFACE-DESIGN.md](INTERFACE-DESIGN.md).

## Divergence from upstream

Upstream is [`mattpocock/skills`](https://github.com/mattpocock/skills), last synced against `main` at `885e2ca4d842d139e9aef4e48d366c63cb1b8013` (2026-08-19). Diff against that SHA to see what has moved since:

| Ours | Upstream path |
|---|---|
| `SKILL.md` | `skills/engineering/improve-codebase-architecture/SKILL.md` |
| `LANGUAGE.md` | `skills/engineering/codebase-design/SKILL.md` |
| `DEEPENING.md` | `skills/engineering/codebase-design/DEEPENING.md` |
| `INTERFACE-DESIGN.md` | `skills/engineering/codebase-design/DESIGN-IT-TWICE.md` |
| `HTML-REPORT.md` | `skills/engineering/improve-codebase-architecture/HTML-REPORT.md` (same name, rewritten — ours specs the JSON contract, upstream's specs an HTML scaffold; don't merge it line by line) |

**Deliberately not adopted.** Re-read this list before pulling the next diff — each of these will show up as a difference every time, and none of them is drift:

- **Upstream's skill split.** Upstream moved the glossary out into a standalone `codebase-design` skill that others reach through the Skill tool. We keep one skill with `LANGUAGE.md` as a reference file. The per-language idiom packs are ours, they only make sense sitting next to the glossary, and one skill is one thing to install.
- **`disable-model-invocation: true`.** Upstream ships this command-only. We want the router to be able to reach it, so the frontmatter stays model-invocable.
- **Skill calls to `grilling` and `domain-modeling`.** Neither upstream skill is in this plugin. Their behaviour is inlined into step 3 — the grilling loop itself, the `CONTEXT.md` updates, and the ADR offer with its default path and format.
- **Tailwind and Mermaid over CDN, with the model writing the markup.** Replaced by `scripts/render_report.py`, a tested Jinja renderer that takes JSON. The model produces data, not markup, so the report is reproducible and its styling is covered by `tests/`.
- **Upstream's em-dash removal.** Upstream stripped em-dashes repo-wide; we keep house punctuation. Expect punctuation-only differences on lines that are otherwise identical, especially in `DEEPENING.md` and `INTERFACE-DESIGN.md`.
- **TypeScript as the default example language.** Upstream's sketches are TypeScript throughout. Where the text is upstream's we keep the examples verbatim so the diff stays small, and point the reader at `idioms/<lang>.md` for the native form.

**Ours, with no upstream counterpart** — `idioms/` (Python, Swift, TypeScript, React) and the language-detection step; `dependency_category` and `adr_callout` on each candidate; `scripts/` and `tests/`; the `Explore` subagent type in step 1.

**Adopted at the 2026-08-19 sync:** the YAGNI scoping rule in step 1; and in `LANGUAGE.md` the deep-vs-shallow contrast, the three interface-design questions, the designing-for-testability rules, and the "Going deeper" pointers.
