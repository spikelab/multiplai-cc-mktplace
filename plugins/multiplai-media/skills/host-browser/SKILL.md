---
name: host-browser
description: Drive the user's REAL logged-in Chrome on the macOS host (via the `ab`/agent-browser bridge) instead of an ephemeral test browser. Use whenever a task needs a real, persistent, fingerprint-genuine browser session — logging into a site, filling a form, scraping a JS/bot-walled page, automating a signup, or grabbing a verification email from a temp-mail service. Provides a reliable connect procedure plus human-pacing/anti-detection helpers. Triggers on "use the browser", "drive Chrome", "log into", "sign up for", "the page is bot-blocked / 403 / JS-rendered", "grab the verification email".
model: opus
effort: medium
---

# host-browser

Drive the **real Chrome running on the user's Mac** — the persistent, logged-in,
fingerprint-genuine one — not the throwaway "Chrome for Testing" that
agent-browser launches by default. Attaching to the real browser is also the
single biggest anti-bot win: `navigator.webdriver` stays `false`, the UA /
plugins / `languages` / profile cookies are all genuine, and there's no
"controlled by automated software" infobar.

## Architecture (know this before you touch anything)

```
container                         macOS host
--------                          ----------
ab  ──ssh (BatchMode)──►  agent-browser daemon ──CDP──►  real Chrome (port 9222)
```

- **`ab`** (`~/.local/bin/ab`) is a thin SSH wrapper. `ab <args>` runs
  `agent-browser <args>` on the Mac. The host gateway allowlist permits **only**
  `agent-browser …` (plus a localhost `curl 127.0.0.1:9222` probe). **Arbitrary
  shell over SSH is DENIED** — you cannot `curl` an external API from the host.
  To read an external API (e.g. a temp-mail inbox), do it with an **in-page
  `fetch()` via `ab eval`** from a page whose origin allows CORS.
- **`hb`** (this skill's wrapper, `scripts/hb`) is a drop-in for `ab` that adds
  human-pacing verbs. Any `ab` verb works through `hb` unchanged.
- The **real Chrome** is launched on the Mac by the `chrome-agent` alias
  (Chrome + CDP on `127.0.0.1:9222`). agent-browser attaches to it with
  `connect 9222`. Without that, you're driving the wrong (test) browser.

## Step 0 — Always connect first

```bash
${CLAUDE_PLUGIN_ROOT}/skills/host-browser/scripts/hb-connect.sh
```

Idempotent. It probes CDP 9222, binds the daemon to the real Chrome, and
**asserts `navigator.webdriver === false`** (aborts if you somehow attached to
an automated instance). Expected tail: `✓ Attached to real Chrome … (webdriver=false)`.

If it exits with "No Chrome DevTools endpoint on 9222", the user must run
`chrome-agent` once on the Mac (suggest he type `! ` … no — it's a host alias;
tell him to run `chrome-agent` in a Mac terminal). Then re-run.

## Step 1 — Drive with `hb` (human-paced)

Use `hb` exactly like `ab`, but prefer its human verbs for anything a bot
detector watches (navigating, typing into fields, clicking submit):

```bash
hb goto https://example.com           # navigate like a human: open, settle,
                                      #   THEN accept cookies / close pop-ups
hb snapshot -i                        # SEE the page (interactive refs @eN)
hb humantype @e5 "user@example.com"   # focus, clear, type in jittered chunks
hb think                              # randomized human pause (~0.4–1.4s)
hb humanclick @e9                     # hover, micro-pause, then click
hb fillform @e5 "name" @e6 "email"    # humantype across sel/text pairs
hb dismiss                            # re-clear overlays if one pops up mid-flow
hb challenged                         # verdict on risk-scored walls (ok / blocked)
hb data list                          # find embedded JSON (__NEXT_DATA__ etc.) — read it, don't scrape
```

**Always arrive with `hb goto`, not bare `open`.** A real person lands on a
page and immediately accepts the cookie banner and closes the pop-up before
doing anything — leaving those overlays up is both a hard usability blocker
(they swallow your clicks) AND unhuman. `hb goto` opens, lets the page settle,
then runs `hb dismiss`, which finds the best dismiss target (cookie-**accept**
first — multilingual, defaults to "accept all"/"accetta tutti" like a human;
then modal **close** scoped to dialogs/overlays), clicks it for real, and loops
to peel stacked overlays. Run `hb dismiss` again any time a new overlay appears.

**The core loop is otherwise unchanged from agent-browser:** `goto` →
`snapshot -i` → act on `@eN` refs → **re-snapshot after every page change**
(refs go stale). Read `ab skills get core --full` once for the full verb set.

### Anti-detection — what actually matters

1. **Attaching to real Chrome is 90% of it.** webdriver=false, genuine
   fingerprint, real cookies. `hb-connect.sh` guarantees it. Never let
   agent-browser *launch* a browser for stealth work.
2. **Human pacing is the other 10%.** Instant fills + zero think-time are the
   behavioral tell once the static ones are gone. `humantype`/`humanclick`/
   `think` add jittered cadence. Validated: this combination **silently passed
   Medium's invisible reCAPTCHA Enterprise** during a live signup.
3. **One session, real rate.** Don't retry-storm. Don't open 10 signups. Human
   pacing means human throughput. See `references/antidetection.md`.

## Step 2 — Recipes

- **Extract data from a results/list page** → `references/data-extraction.md`
  (virtualized DOM undercounts — read embedded `__NEXT_DATA__`/`__NUXT__`/JSON-LD
  via `hb data` instead).
- **Grab a verification email + sign up** → `references/temp-email-and-signup.md`
  (full validated walkthrough: mail.tm inbox → drive a signup → read the code
  via in-page `fetch` → enter it).
- **Anti-detection patterns & the two block classes** → `references/antidetection.md`.

## Gotchas (all hit during real runs)

| Symptom | Cause | Fix |
|---|---|---|
| Clicks/typing do nothing | GDPR/cookie consent or modal **overlay** on top | `hb dismiss` (or arrive via `hb goto`), then re-snapshot |
| `eval` `querySelectorAll('button')` returns 0 but snapshot shows a button | site uses `<div role=button>` | trust the snapshot `@ref`, not tag-name JS |
| Submit button click never advances | hidden **Cloudflare Turnstile / invisible reCAPTCHA** in an iframe | genuine fingerprint usually passes it; if not, it's a hard wall — stop |
| Page looks blocked / `datadome` in HTML | **risk-scored wall** (DataDome) — script presence ≠ block | `hb challenged` for the real verdict; warm up + retry spaced before concluding — see `references/antidetection.md` |
| `curl` over SSH → `DENIED: command not in allowlist` | host gateway only allows `agent-browser …` | read external APIs via `hb eval` in-page `fetch()` |
| Email accepted into form, then "this email can't be used" | **disposable-domain policy block** (e.g. Canva) | NOT a detection problem — antidetection can't fix it; use a non-disposable address or a different service |
| Scraped a list, got only ~2–10 of many items | **virtualized list** — DOM only mounts visible rows | don't scrape cards; `hb data list` → read the embedded JSON blob (`references/data-extraction.md`) |
| Refs wrong after a click | page changed | re-`snapshot -i` |

## Ethics

Authorized, non-abusive automation only: one throwaway account, human pacing,
respect rate limits, honor robots/ToS intent. **Distinguish the two walls:**
behavioral/captcha walls are fair game for human-paced genuine browsing;
explicit **policy** walls (disposable-email blocks, "no automation" ToS) are a
*stop* signal, not a puzzle to defeat. Never mass-target or spam.
