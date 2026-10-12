# Screencast → landscape product demo

Load this when the job is a screen recording that becomes one 1–3 minute
landscape demo. The shared workflow (prep → EDL → render → check) is in
`SKILL.md`; this file holds what is specific to screencasts.

Typical request: "make a demo video from `/path/to/recording.mov` — keep it
90s, hook is the first 10s, the money shot is around 2:30, lo-fi vibe. Use
`https://pixabay.com/music/some-track`."

## Dead spans

Prep writes a **dead-span table** into `context.md`: black gaps, frozen
frames, and typing/cursor-only stretches where nothing watchable happens.

- **Honor the dead-span table first.** Segments must not contain a `black` span at any speed, and must not cross a `static`/`low` span at an ordinary fast-forward speed (4-8x) — at 6x a 30s wait is still 5s of a video where nothing moves. Place segment boundaries so dead spans fall in the cuts; when continuity genuinely needs one (text appearing in a field), cross it at `speed >= 20`. After drafting segments, re-check each one against the table. Nuance: dead means don't *linger*, not don't *show* — a motionless-but-readable frame (a results list) can still carry a few-second speed-1 zoom money shot.
- **The first 3–5 s carry one hook**: the result the viewer wants, a pain
  they know, or a question. Never open on a description of the company or
  a feature list. (HyperFrames `skills/product-launch-video/references/story-design.md:67`)
- Map each user beat to a segment with `src_start`/`src_end` from the transcript anchors.
- Use `speed > 1` for "fast-forward" sections (anything >4 auto-mutes audio; that's where the music bed carries).
- Use `speed = 1.0` and a `zoom: {scale, x, y}` for "money shot" moments.
- Use silencedetect timestamps as natural cut points.
- Total target duration: usually 60–120 s.
- Add a small title card and optional logo per the user's request.

## Zoom rules (a zoom is a crop — everything outside it is invisible)

- Zooms are short money shots (≤12 s). After every zoomed segment, return to a full-frame segment so the viewer regains context.
- **Never end the video zoomed** — render errors on a zoomed final segment. If a close-up ending is genuinely intended, set `"hold": true` inside that zoom.
- Keep most of the runtime full-frame; render warns when >50% is zoomed.

## Follow the clicks

A segment's `follow` pushes the camera in on each click and pans between
them, so the viewer sees what was clicked without a hand-placed zoom per
moment. (Pattern: HyperFrames registry block `cursor-zoom-follow`.)

1. **Find the clicks** after `prep`:
   `uv run --project <scripts> python3 <scripts>/pipeline.py cursor <source.mov>`.
   It writes `clicks.json` in the prep cache: `{t, x, y, sure}` per click,
   `t` in source seconds, `x`, `y` 0..1 in the source frame.
2. **Use only `sure` clicks.** A click is `sure` when a picture of the
   pointer was found at it. On the one recording measured, 6 of 8 sampled
   `sure` clicks were real clicks and 1 of 8 of the others was: the others
   come from page changes, spinners and progress bars.
3. **Few `sure` clicks?** The hand pointer (over buttons and links) is
   seldom learned from the recording, because it rarely moves alone. Ask
   the user to mark it: copy prep's `proxy_720p.mp4` (same timeline and
   shape as the source, and it plays in any browser) into an empty folder,
   open the review page on that folder, and ask them to click the hand
   pointer in one or two paused frames with the comment "pointer". Pass
   each comment's `t`, `x`, `y` back:
   `pipeline.py cursor <source.mov> --mark T,X,Y --mark …`.
4. **Write the targets** into the segments that should follow:
   `"follow": {"targets": [{"t": 111.2, "x": 0.47, "y": 0.75}, …]}`.
   Options: `scale` (zoom on a target, default 1.4), `lead` (output seconds
   the move takes, ending on the click, default 0.6), `hold` (output seconds
   it stays after, default 1.2). Targets with a gap over 2 s between them
   send the camera back to the whole frame; a target inside the previous
   hold is skipped.
5. **Review the render.** A target in the wrong place pushes in on the
   wrong part of the screen. When the user's review comment says so, its
   `x`, `y` are the right target: replace the wrong one.

Rules: `follow` needs fit `pad` or `blur` and no `frame` (render refuses a
crop or a panel, which would cut the clicked point out); a segment takes
`zoom` or `follow`, not both. The Zoom rules above still apply: keep the
follow segments to the parts of the demo where clicks matter.

## Overlays on a demo

- **Reveal each overlay when the narration names its subject**, not before:
  start a stat card on the word that introduces the number, and a label when
  the voice reaches the thing it labels. Find the time with
  `pipeline.py timeline <edl>`. A graphic shown before it is mentioned competes with
  the voice. (HyperFrames `skills/faceless-explainer/references/motion-language.md:106`)
- **Swap the text on one overlay with a zoom-through**: the outgoing text
  scales 1 → 1.2 over 0.2 s, easing in, then a hard swap, then the incoming
  text scales 0.75 → 1 over 0.5 s, easing out. This is a move inside the
  overlay page (`motion.md`, Motion rules), not a cut between segments: a
  segment join is a hard cut or a crossfade. (HyperFrames
  `skills/pr-to-video/references/cut-catalog.md:38-71`)

## Sound effects

A short sound on a click, or when a graphic appears, makes the action land.
Too many make a demo sound busy: the first version in Peverelli's video had
sounds everywhere, and he turned them down and removed some until it fit.
(Peverelli, YouTube HzXD4GVqXwM)

- **Use them on clicks and on graphic entrances**, and only the ones that
  matter. Never on every cut.
- **Get a first list from the EDL**: `pipeline.py sfx-suggest <edl> --clicks
  <file> --overlays <file>` prints the `--clicks` file at each `follow` target
  and the `--overlays` file at each overlay's start, in output time, and says on stderr which ones it skipped or dropped. Paste the
  list into the EDL as `sfx`, then remove what does not need a sound.
- **Sounds:** no sounds ship with the skill. Each `sound` is a path to an
  audio file; ask the user for the files they want (a click, a pop for an
  entrance).
- **Level:** the default `volume_db` is -14. Lower it (-18, -20) when the review says the sounds stand out. They are
  mixed in after the music, so they never duck it, and a limiter keeps them
  from clipping.
- **Spacing:** two sounds must be at least 0.5 s apart (render refuses
  closer ones), and render warns at more than one every 3 s on average.
- When the user settles on a level or a choice of sounds, offer it as a
  preference (`pipeline.py prefs add`, SKILL.md step 7).

## Choose music that fits

Music is part of the edit, not an afterthought. Before rendering:
- If the user named a track or vibe, honor it. If not, **propose a specific track (with source link) and say why it fits** — don't silently pick one.
- Match genre and tempo to the content, e.g.:

| Demo type | Fits | Avoid |
|---|---|---|
| Dev tool / terminal workflow | minimal electronic, lo-fi beats | orchestral, vocals |
| Business / hiring / SaaS pitch | upbeat corporate, light house | lo-fi sleepy beats, heavy EDM |
| Consumer app, playful | indie pop, funk | dark ambient |
| Data/AI "wow" reveal | cinematic electronic build | anything with lyrics |

- Tempo should roughly match cut density: fast-forward-heavy reels want energy; calm narrated walkthroughs want restraint.
- Search Pixabay Music by mood ("corporate upbeat", "lo-fi chill", "cinematic tech") rather than taking the first result.
- The `synth` pink-noise bed is a last-resort fallback — never ship it in a final deliverable without telling the user.

### Free-for-commercial music sources

Two URL types are supported by `--music-url`:
- **Direct CDN audio URLs** (`.mp3`/`.wav`/`.m4a`/`.ogg`/`.flac`) — downloaded with `curl` + realistic User-Agent.
- **Page URLs** (YouTube, Vimeo, etc.) — extracted via `yt-dlp`.

Recommended sources:
- **Pixabay Music** — https://pixabay.com/music/ — Pixabay Content License (commercial OK, no attribution). Open the track page, click ⋯ on the player → "Copy audio URL" (or right-click the player → "Copy audio address"). That gives you a `https://cdn.pixabay.com/audio/.../track.mp3` URL to pass to `--music-url`.
- **YouTube Audio Library** — https://studio.youtube.com/channel/UC/music — many CC0 tracks; paste the YouTube URL.
- **Uppbeat** — https://uppbeat.io/ — free with attribution; download and pass via `--music-file`.

## Quality check

- **Sharpness:** output is 1080p by default; screen text must be legible. If it isn't, check the EDL `source` points at the original recording.
- **Framing:** extract a frame from the last 2 s — it must be full-frame (no leftover zoom crop).
- **Music:** the bed fits the content type and ducks under narration.
