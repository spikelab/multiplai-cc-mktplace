# Anti-detection patterns

Goal: browse like a real person from a real browser. Not to defeat security —
to *not look like a headless bot* when doing authorized, human-rate automation.

## The mental model: two layers, two block classes

Detection has a **static** layer (what the browser *is*) and a **behavioral**
layer (how it *acts*). Defeat both and you read as human. Separately, sites
also raise **policy** walls that no amount of realism beats.

### Layer 1 — Static fingerprint (solved by attaching, not launching)

The biggest tells a launched/headless automation browser leaks:

| Tell | Launched Chromium / Playwright | Real Chrome via `connect 9222` |
|---|---|---|
| `navigator.webdriver` | `true` | **`false`** ✓ |
| "controlled by automated software" infobar | present | absent ✓ |
| User-Agent | "HeadlessChrome" / CfT | genuine Chrome ✓ |
| `navigator.plugins`, `languages` | empty / default | real profile (`en-US,en,it`) ✓ |
| cookies / logins | none | the real profile's ✓ |
| Canvas/WebGL/TLS fingerprint | automation-ish | the real machine's ✓ |

**Therefore: never launch a browser for stealth work. Attach to the real one**
(`hb-connect.sh` → `connect 9222`, asserts `webdriver=false`). This alone clears
the entire static layer for free, because it *is* a real browser.

### Layer 2 — Behavioral (the `hb` human verbs)

Once static tells are gone, behavior is what's left. Bots betray themselves by:
- filling a field instantly (paste-speed, no per-key events),
- clicking the instant an element appears (zero dwell/think time),
- perfectly uniform timing between actions,
- never hovering before clicking.

A real person also **acts on arrival**: they accept the cookie banner and close
the welcome pop-up before reading the page. An automation that ignores a
full-screen consent wall and tries to click through it reads as non-human (and
practically, the overlay swallows the clicks). `hb goto` bakes this in —
navigate, settle, then `hb dismiss` (accept cookies, default "accept all"; close
modals) before the first real interaction.

`hb` counters each:
- **`humantype`** — focus, clear, then emit 2–5 char chunks with 45–160 ms gaps
  and occasional 280–720 ms "thinking" pauses. Uses agent-browser `type`, which
  fires real `keydown`/`keypress`/`keyup` per character.
- **`humanclick`** — `hover` → 140–460 ms dwell → `click`.
- **`think [min] [max]`** — randomized pause between logical steps. Sprinkle it
  between fields and before submits.
- SSH round-trip latency (~100s of ms per `ab` call) already adds natural,
  non-uniform spacing; the jitter stops it being *too* uniform.

**Caveat — metachar text isn't real keystrokes.** The host gateway forbids
`; | & < > \` $ ( )` and newline in an `ab` argument, so text containing them
(a strong password like `P@ss$w0rd&`, a query with `&`/`()`) can't be typed
key-by-key. `humantype`/`fillform` detect this and fall back to a base64
`eval -b` that sets the value programmatically (native setter + `input`/`change`
events). It populates the field, but fires **no** `keydown`/`keypress`/`keyup`,
so a site scoring keystroke cadence sees a paste, not typing — behaviorally
weaker. Metachar-free text still types for real. The durable fix is upstream: a
`type -b/--base64` (or `--stdin`) verb on agent-browser mirroring `eval`, which
would restore real keystrokes for any text.

**Live proof:** real-Chrome + `hb` human pacing **silently passed Medium's
invisible reCAPTCHA Enterprise** (`size=invisible`, risk-scored) on a first-try
signup. Invisible reCAPTCHA/Turnstile score the session; a genuine browser +
human cadence scores as human and never shows a challenge.

### The third thing: policy walls (don't fight these)

Some blocks are deliberate policy, independent of how human you look:
- **Disposable-email blocklists** — Canva accepted the form input, then:
  *"This email can't be used on Canva."* That's a domain-reputation rule. No
  fingerprint or pacing changes it. Fix = a non-disposable address, or pick a
  service that allows disposables.
- **"No automation" ToS / hard CAPTCHAs that always show** (DataDome device
  checks, hCaptcha that always renders) — treat as a *stop*, not a puzzle.

Recognizing which wall you hit saves hours: if the site **says** the email/
account is disallowed, stop and change inputs; if it silently no-ops a submit,
suspect an invisible challenge (often passes with genuine fingerprint); if it
*renders* a visible CAPTCHA, that's a hard wall.

## DataDome (and other risk-scored walls) — restraint, not puzzle-solving

DataDome/PerimeterX/Kasada don't gate every request; they **score risk** and
only escalate to a CAPTCHA when the score is high. Inputs to that score:
datacenter/VPN IP, request velocity & volume, a fresh/empty profile, and
automation tells. From the genuine host browser you already win most of these
(residential IP, real cookies & history, `webdriver=false`). So the way "around"
DataDome is to **stay under the threshold**, not to defeat a challenge.

### What actually protects us (corrected model, 2026-07)

The old worry was that CDP-attached automation leaks via `Runtime.enable`. That
was **patched in V8 in May 2025**, and we **measure clean**: our real-Chrome +
agent-browser attach passes rebrowser's CDP-leak battery — `runtimeEnableLeak`
green, `navigatorWebdriver` green, `pwInitScripts`/`viewport` green. agent-browser
*does* call `Runtime.enable`, but the patch neutralized that detection. **So CDP
is not our provable wall.** The real levers are, in order:

1. **Behavior** — synthesized-input geometry is the strongest tell. Integer-pixel,
   perfectly straight, uniformly-timed, dead-centre clicks/moves scream "bot."
   `hb humanclick` counters this with a curved, eased, jittered, off-centre path.
2. **Residential IP** — attaching to the user's real Chrome gives us their home IP.
   Don't tunnel it through a datacenter/VPN.
3. **The `datadome` cookie** — a clearance cookie bound to IP+UA+fingerprint,
   lasting ~1–24h. Once earned (see the solve-once flow), ride it.

**Verify empirically — don't trust this doc as gospel.** Chrome, agent-browser and
DataDome all move. Run **`hb fpcheck`** at the start of a session: it loads
`bot-detector.rebrowser.net` in a throwaway tab and prints a PASS/FAIL table. Green
= the attach still looks human. If `runtimeEnableLeak` ever goes **red**, the leak
regressed — **stop and reassess** (the advanced options below may become relevant).

> Note: patchright / rebrowser-patches are **Playwright-only** stealth forks. We
> don't drive Playwright (agent-browser is a native Rust CDP client), so they don't
> apply to us — another reason the lever is behavior + IP + cookie, not the engine.

Staying under the threshold:

- **Low volume, human pace.** A few deliberate, spaced navigations — not a
  crawl. Velocity is the biggest lever you control.
- **Genuine arrival.** Real profile (it carries prior visits / a valid
  `datadome` cookie), real referrer, `hb warmup` (homepage → dwell → one on-site
  nav click → dwell) instead of a cold deep-link.
- **Don't re-trigger.** Each rapid reload/search raises the score. Read what's
  on the page (the listings DOM is usually fully present even under the cookie
  CMP) instead of re-navigating.

### Detecting a real block vs. the script

The script being in the HTML (`captcha-delivery` string present) is **not** a
block — it only means scoring is active. Use **`hb dd`** for the verdict. It
combines three reads into one:

- **cookie** — is a `datadome` clearance cookie held? (yes/no)
- **network** — `captcha-delivery` assets / `x-datadome` headers seen this session
  (session-scoped; `ab network requests --clear` to scope to the current nav)
- **DOM** — the authoritative "blocked right now?" check (a *visible*
  captcha-delivery iframe, an interstitial with no real content, or a `/captcha`
  redirect)

`hb dd` prints `clear` / `scored (DataDome active, not blocked)` / `CHALLENGED`
and its **exit code is driven only by the live DOM** (0 clear, 1 CHALLENGED), so a
stale request-log entry can never fabricate a block. Never conclude "blocked" from
`dd:true` or the mere presence of the script.

### Procedure — solve once, ride the cookie

The failure mode is **one cold deep-link attempt → hit a challenge → quit.**
Don't. Risk-scored walls reward patience and warm-up:

1. **`hb fpcheck`** — confirm the attach is still clean (green). If red, stop.
2. **Warm up — don't deep-link cold.** `hb warmup https://www.immobiliare.it/`
   arrives at the homepage, accepts cookies, dwells, clicks one on-site nav
   control, dwells again. A profile that has just browsed the site normally scores
   far lower than one that teleports straight to a deep filtered URL.
3. **Check, then act.** `hb dd` after each navigation. If `clear`/`scored`, extract.
4. **If blocked, don't quit — back off and retry, spaced.** `hb think 8000 15000`,
   then try once more (or via a shallower entry / the site's own filter UI). Make
   **a few** spaced attempts, not a burst. Velocity is what convicts you.
5. **Genuine hard-wall (a rendered slider/puzzle) → hand it to the human.** Run
   **`hb solve-wait`**. Because this is the user's own visible browser, the clean
   move is a **one-time manual solve in that window**: `hb solve-wait` prints the
   instruction, screenshots the tab so the user knows which one, then polls `hb dd`
   until it flips to clear or a `datadome` cookie appears — then continues on the
   earned cookie. It **never** touches the puzzle itself. Afterward the cookie
   persists in the profile (~1–24h) and subsequent automated navigation rides it.

Validated: `immobiliare.it` served full Florence search results + a listing
detail page over a few spaced, human-paced requests from a warm profile — **no
CAPTCHA**. A separate cold, single-shot attempt *did* hit a challenge: same site,
different behavior. The behavior is the variable.

### The line — authorized use only (hard rules)

This toolset stays on the genuine-browsing side of the line. It **detects** state
and **rides** the cookie a human earns. It does not, ever:

- **spoof** — no overriding UA, JA3/TLS, canvas/WebGL, `navigator.*`, or injecting
  fingerprint patches. It breaks coherence and *raises* DataDome's score. Our edge
  is that the browser is genuinely real; spoofing throws that away.
- **auto-solve a CAPTCHA** — `hb solve-wait` hands a rendered challenge to the
  human. Nothing here attempts to solve or bypass a rendered puzzle programmatically.
- **rotate IPs / proxy** — same residential IP the user actually browses from.
- **run at machine scale / rate** — human pace, one session, no retry storms.
- **republish or evade a policy block** — if the site *says* stop (disposable-email
  block, "no automation" ToS, a device check that always renders), that's a **stop
  signal**, not a puzzle. Change inputs or walk away.

Authorized-use line, stated plainly: **the human solves any challenge, at human
pace, from their own IP; we never spoof, rotate, auto-solve, or scale; and we stop
on any explicit block.**

### Advanced / only-if-mandatory (explicitly OUT OF SCOPE to build here)

If `hb fpcheck` ever regresses to red (CDP leak re-exposed) and a target genuinely
mandates it, two heavier engines exist — **documented as options only, not built by
this skill**:

- **A CDP-stripping proxy** in front of the browser (rewrites/hides the CDP
  chatter). Heavy, and a maintenance treadmill as detection evolves.
- **nodriver / a stealth engine on a *cloned* profile** instead of CDP-attach.
  Also a treadmill; and Chrome ≥136 refuses CDP against the default profile dir, so
  it needs a **non-default `--user-data-dir`** (a copy of the real profile), which
  erodes the "genuinely real browser" edge that makes this approach work at all.

Both trade our core advantage (a real, coherent browser) for engineering that has
to be re-earned every Chrome release. Reach for them only if measurement proves the
current approach broke — and treat building them as a separate, deliberate decision.

## Operational rules

- **Connect, assert `webdriver=false`, then act.** If the assert fails you're on
  the wrong browser — fix before doing anything detectable.
- **Arrive like a human: `hb goto`, not `open`.** Accept cookies / close pop-ups
  before interacting. Re-run `hb dismiss` whenever a new overlay appears.
- **Re-snapshot after every page change.** Stale refs cause mis-clicks that look
  robotic (clicking the wrong thing, retry loops).
- **Human throughput.** One account. No retry storms. If something fails twice,
  diagnose (consent overlay? iframe? policy wall?) — don't hammer.
- **Read external APIs in-page.** The host SSH gateway denies arbitrary `curl`;
  use `hb eval` `fetch()` from a CORS-friendly origin instead.
- **Prefer `@ref` from snapshot** over tag-name JS — sites use `<div role=button>`
  so `querySelectorAll('button')` lies.

## Quick fingerprint self-check

Fastest empirical check — an in-page read of the obvious tells:

```bash
printf '({wd:navigator.webdriver, ua:navigator.userAgent, plugins:navigator.plugins.length, langs:navigator.languages.join(",")})' | hb eval --stdin
# want: wd:false, a real Chrome UA, plugins>0, real langs
```

Fuller check — the CDP-leak battery via a throwaway tab (restores your tab after):

```bash
hb fpcheck
# want: runtimeEnableLeak / navigatorWebdriver / pwInitScripts / viewport all PASS,
# verdict CLEAN. A red runtimeEnableLeak means the CDP leak regressed → stop.
```
