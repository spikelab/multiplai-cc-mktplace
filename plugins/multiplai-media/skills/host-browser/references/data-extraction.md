# Extracting data: read embedded state, don't scrape virtualized DOM

## The trap: virtualized / lazy lists

Modern SPAs (React/Next, Vue/Nuxt) **virtualize** long lists — only the rows
near the viewport exist in the DOM at any moment (often just ~2–10), recycled as
you scroll. So `snapshot`/`querySelectorAll` over the cards **silently
undercounts**: you think you scraped "the results," you actually got whatever 2
rows happened to be mounted. This is a real bug that's bitten this skill on
`immobiliare.it` (≈2 cards in the DOM at a time).

Symptoms: a results page that clearly shows dozens of items, but your extraction
returns 2–10; counts that change when you scroll; `get count` ≠ what you see.

## The fix: read the page's embedded state

These same apps ship the **full dataset** as a JSON blob in the HTML, before any
virtualization. Read that instead — it's complete, stable, and usually richer
than the rendered card (fields the UI hides are in the JSON). Use **`hb data`**:

```bash
hb data list                 # which blobs exist on this page + sizes
hb data next  > /tmp/nd.json # dump Next.js __NEXT_DATA__ (then filter with jq)
hb data nuxt                 # window.__NUXT__
hb data apollo               # __APOLLO_STATE__
hb data ld                   # all <script type=application/ld+json> blocks
hb data initial              # window.__INITIAL_STATE__
```

Sources in priority order: `__NEXT_DATA__` (Next.js) → `__NUXT__` (Nuxt) →
`__APOLLO_STATE__` (Apollo/GraphQL) → JSON-LD (`ld`, good for a single
product/article, schema.org-typed) → `__INITIAL_STATE__` (Vuex/Redux).

### Transport tip (why `hb data` returns clean JSON)

`ab eval` JSON-serializes whatever the snippet returns. So **return the parsed
object/array, never a `JSON.stringify(...)` of it** — returning a string gets
double-encoded (escaped quotes you then have to unescape). `hb data next` does
`JSON.parse(...)` and returns the object; you get clean JSON on stdout.

### Targeted extraction (big blobs)

`__NEXT_DATA__` can be 100s of KB. Two good moves:

- Dump once to a file and `jq` it in the container:
  `hb data next > /tmp/nd.json; jq '.props.pageProps.results' /tmp/nd.json`
- Or extract just what you need with a targeted eval (return an object → clean
  JSON), walking to the results array and mapping the fields you want:

```bash
cat <<'JS' | hb eval --stdin
(() => {
  const nd = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
  const list = nd.props.pageProps.dehydratedState ?? nd.props.pageProps.results ?? nd; // inspect first
  // map to {price, rooms, bathrooms, surface, condition, url} ...
  return /* array of objects */;
})()
JS
```

The exact JSON path is site-specific — `hb data next > file` then `jq 'paths'`
or eyeball the top-level keys (`.props.pageProps…`) to find the results array.

## Pagination still applies

The blob holds **one page** of results. To go deeper, navigate the next page the
site's own way (e.g. immobiliare `…&pag=2`), **human-paced and spaced** (this is
also what keeps risk-scored walls calm — see `antidetection.md`), and read
`hb data next` again per page.

## Worked example — immobiliare.it

DOM scraping returns ~2 virtualized cards. `__NEXT_DATA__` returns the full page
of listings **with fields the cards don't render**: `condition`/state
(Buono/Abitabile, Ottimo/Ristrutturato, Nuovo/In costruzione, Da ristrutturare)
and bathroom counts, alongside price/rooms/surface/typology/url. (Aside:
immobiliare also carries more stock than idealista for the same town — e.g.
Omegna 24 vs 11 — so it's the better source despite the DataDome scoring.)

Rule of thumb: **on any data-heavy SPA, run `hb data list` first.** If there's an
embedded blob, prefer it over the DOM.
