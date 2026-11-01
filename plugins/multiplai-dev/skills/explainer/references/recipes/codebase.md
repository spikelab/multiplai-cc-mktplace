# Recipe: a codebase, a module, or a tool's code

**Facts come from codebase-walkthrough, not from reading the code yourself.** It finds every repository
that calls the code and everything the code calls, follows each entry point hop by hop, and keeps only
claims whose cited lines are at the commit. A page written from your own reading would skip all of that.

## Gather

1. Run `/multiplai-dev:codebase-walkthrough` on the directory (`--scope module`, or `--scope codebase`
   for a whole tool or plugin). It needs `--trust-repo`, which only the user can grant, and it spends
   model budget: tell the user its `--max-usd` before you start.
2. Read its `<name>-walkthrough.md` and `<name>-paths.json`. They are the only source for claims about
   the code. If a fact you want is not there, leave it out or say on the page that it was not checked.

## Turn the walkthrough into a page

| Walkthrough section | Goes to |
|---|---|
| Overview / "What it is" | the question and the answer (layer 0) |
| Its flow picture and "How it works" stages | the picture and the stage cards (layer 1) |
| Paths (`<name>-paths.json`) | pick one path as the worked example: one request, one command, one job, followed hop by hop |
| The part that does the main job | a deeper section, "How it <does its job>" |
| What it creates, Configuration | deeper sections, tables in fold-outs |
| Use cases | "Who uses it and when", as `.rows` |
| Gotchas | `.callout` for the one that matters most; the rest in a fold-out |
| Reference, Commits read, Coverage | fold-outs at the bottom; "Sources read" names the commits |

## Citations

Each walkthrough link looks like
``[`Repo/path:16-17`](https://github.com/org/Repo/blob/<sha>/path#L16-L17 "quoted line")``. On the page it
becomes:

```html
<a class="cite" href="https://github.com/org/Repo/blob/<sha>/path#L16-L17"
   data-cite="Repo@<sha>:path:16-17" data-quote="quoted line" title="path:16">1</a>
```

Then run `check` with one `--repo Repo=<checkout>` per repository in the walkthrough's "Commits read"
table.

## Worked example

One path from `<name>-paths.json`: its entry point (a route, a command, a scheduled job), what comes in,
and what it looks like after each hop.
