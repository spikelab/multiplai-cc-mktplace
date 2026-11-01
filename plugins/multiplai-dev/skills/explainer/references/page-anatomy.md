# The page, layer by layer

`python -m explainer_check new <page.html> --design-system <dir>` writes the shell (`assets/shell.html`)
with the design system inlined. Fill its `__PLACEHOLDERS__`, delete the parts the page does not use, and
add the deeper sections from the components below. Styles go in `<style id="page">`, with tokens only.

## Layer 0 — what this is and the answer

| Part | Markup | Holds |
|---|---|---|
| Header line | `.eyebrow` | subject · source or repo · "as of" date |
| Question | `<h1>` (exactly one) | the question the page answers |
| Answer | `p.lede` | one or two sentences that answer it |
| Legend | `.legend` of `.chip` | the three or four status words the page uses |

The `<title>` is a name of 2 to 4 words ("DataScrub Pipeline Map", "Release Pipeline"), not the question.

## Layer 1 — the whole, then each stage

| Part | Markup | Holds |
|---|---|---|
| Picture | `figure.fig` with `svg.wide` and `svg.tall` | every stage as one box, in order (see `diagrams.md`) |
| Stages | `.spine` of `.stage` | one card per stage; the number in `.dot` matches the box in the picture |
| Card | `.card` › `.card-head`, `.where`, `p`, `.example`, `details.more` | see the writing guide |
| Card in focus | `.card.focus` | the one stage the page is about, when it is about one (e.g. what a PR changes) |

## Layer 2 — deeper sections

Each is a `<section>` with an `<h2>` that is a question. Pick what the reader needs:

| Component | Markup | Use for |
|---|---|---|
| Inside one step | `section` + a second `figure.fig` (sequence picture) | what happens inside one stage, between its parts |
| Before / after | `.pair` of `figure` + `pre` | a file, payload or output before and after |
| Options side by side | `.compare` of `.opt` | a trade-off the reader has to decide |
| Levers or rules | `.rows` of `.row` (`h3`, `p`, `.effect`, `.chip`) | a list of things, each with an effect |
| Numbers | `.tiles` of `.tile` (`b` + `span`) | costs, counts, timings |
| Road ahead | `.road` of `.road-row` (`.step`, text, `.who`) | what happens next, who does it |
| Warning | `.callout` (or `.callout.note`) | the one thing a reader must not miss |
| Table | `.scroll` › `table` | reference data; put long tables in a fold-out |

## Layer 3 — look-ups

| Part | Markup |
|---|---|
| Fold-out | `<details class="more"><summary>…</summary><div class="body">…</div></details>` |
| Citation | `<a class="cite" href="<link to the lines>" data-cite="<key>@<sha>:<path>:<start>-<end>" data-quote="<a few words from those lines>" title="<path>:<start>">1</a>` |
| Glossary | the "Words used on this page" fold-out, a `<dl>` |
| Sources | `footer`: what was read, at which commit or date; what could not be read |

`href` of a citation points at the lines where the reader can open them: a GitHub blob URL at the sha
with `#L<start>-L<end>` for a published page, a relative path for a local one. `data-quote` is optional
and makes `check` confirm those words are in the cited lines.

## Phone width

The shell's grids fall back to one column under 700 px. Give every picture a `.tall` version (see
`diagrams.md`). Long code and tables go in `pre` or `.scroll`, which scroll sideways inside themselves;
the page itself never scrolls sideways.
