# Pictures

## When to draw one

Every page gets one picture of the whole: the stages as boxes, in the order things happen, numbered like
the cards below it. Draw a second one only for a deeper section where the parts talk to each other over
time (a sequence: who calls whom, in what order). Do not draw what a list or a table says as well.

## How to draw it

- Inline SVG inside `figure.fig`. Two versions: `svg.wide` (`viewBox="0 0 960 H"`, shown from 700 px)
  and `svg.tall` (`viewBox="0 0 420 H"`, shown under 700 px). The tall one stacks the boxes top to
  bottom.
- Each root `<svg>` has `role="img"` and an `aria-label` that says what the picture shows in one
  sentence.
- Style with the shell's classes, never colours typed in: boxes `rect.node` (`.accent`, `.ok`, `.warn`,
  `.ghost` for not built yet), arrows `path.edge` (`.accent`, `.back` for a reply or a loop back),
  arrowheads `.ah`, labels `text.lbl`, second lines `text.sub`, lane titles `text.lane`, step numbers
  `text.num`. `fill`/`stroke` attributes may only be `none`, `currentColor` or `var(--token)`.
- Ids must be unique on the whole page: the wide and tall versions need their own marker ids
  (`ah-w`, `ah-t`).
- Write well-formed XML: close every element, `&#160;` instead of `&nbsp;`.
- Text: at least 15 units high in the wide picture and 16 in the tall one, or it renders too small on a
  phone. Keep each label to the box's width: about 8 units per character at 15 units high. Never type a
  `path:line` into a picture.
- Each box: number, then a label of two to four words, then at most one `.sub` line.

## Look at it before you ship it

`python -m explainer_check render <page.html> --out <dir>` draws every SVG to `<n>-light.png` and
`<n>-dark.png`. `Read` each PNG and check:

- every label fits inside its box and no text overlaps another;
- every arrow starts and ends on a box and points the right way;
- the boxes and numbers match the stage cards, in the same order;
- the dark PNG is as readable as the light one.

Fix and render again until all four hold. When `render` exits 1 because `resvg-py` cannot be imported,
tell the user the pictures were not looked at and ask them to open the page in a browser.
