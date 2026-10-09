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
