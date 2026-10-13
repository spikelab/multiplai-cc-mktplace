/* Pure functions behind the video-edit job page.
 *
 * No DOM, no network: everything here takes data and returns data, so
 * `node --test tests/review_logic.test.js` can check it without a browser. In
 * the page these functions are available as `JobLogic.<name>`.
 */
(function (root) {
  "use strict";

  const TABS = ["clips", "speakers", "look", "music", "review"];
  const INPUT_FILES = { job: "job.json", candidates: "candidates.json", looks: "looks.json", music: "music.json" };

  /* 75.25 → "1:15.3" */
  function fmt(t) {
    const v = Number.isFinite(t) && t > 0 ? t : 0;
    const m = Math.floor(v / 60), s = v - m * 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
  }

  /* What a tab says instead of its content when an input file is not usable:
   * null when `result` is a valid file. `result` is an /api/<name> answer. */
  function inputNotice(name, result) {
    const file = INPUT_FILES[name];
    if (!result) return { text: `Loading ${file}…`, error: false };
    if (result.status === "ok") return null;
    if (result.status === "missing") {
      return { text: `The session has not written ${file} yet. It appears here as soon as it does.`, error: false };
    }
    return { text: `${file} has a problem, so this tab cannot use it:\n${result.error || "unknown error"}`, error: true };
  }

  /* Whether the job is a reels job; null until job.json is valid. */
  function isReels(job) {
    return job && job.status === "ok" ? job.data.kind === "reels" : null;
  }

  /* The newest row per value of `key` (rows are in file order, oldest first). */
  function latestBy(rows, key) {
    const out = new Map();
    for (const r of rows || []) if (r && r[key] !== undefined) out.set(r[key], r);
    return out;
  }

  function rowsOfKind(rows, kind) {
    return (rows || []).filter((r) => (r.kind || "point") === kind);
  }

  /* Whether a pick row still belongs to candidate `c`: its span overlaps c's.
   * The session may snap a picked clip's edges, but a candidates.json that
   * reuses an id for another part of the recording makes that a new candidate. */
  function pickFits(row, c) {
    return !!row && row.start < c.end && row.end > c.start;
  }

  /* Clips is open while a valid candidate has no pick row that fits it yet. */
  function clipsOpen(s) {
    if (!isReels(s.job) || !s.candidates || s.candidates.status !== "ok") return false;
    const picked = latestBy(rowsOfKind(s.rows, "pick"), "candidate");
    return s.candidates.data.some((c) => !pickFits(picked.get(c.id), c));
  }

  /* Speakers is open until a panels row has been sent. */
  function speakersOpen(s) {
    return !!isReels(s.job) && rowsOfKind(s.rows, "panels").length === 0;
  }

  /* The tab to show first: the earliest whose decision is still open, else Review.
   * `open` maps a tab id to a boolean. */
  function firstOpenTab(open) {
    for (const id of TABS) if (open[id]) return id;
    return "review";
  }

  /* `run` (an async function) wrapped so only one call runs at a time. A call
   * made while a run is in flight queues one more run right after it (calls
   * made meanwhile share that run), and its promise settles only when the
   * queued run has finished: a caller that awaits it sees data fetched after
   * its call. */
  function oneAtATime(run) {
    let running = null, queued = null;
    return function call() {
      if (!running) {
        running = Promise.resolve().then(run).finally(() => { running = null; });
        return running;
      }
      if (!queued) queued = running.catch(() => {}).then(() => { queued = null; return call(); });
      return queued;
    };
  }

  // --- Clips ---------------------------------------------------------------------------

  const SCORE_KEYS = ["hook", "payoff", "quotable", "standalone", "density"];

  function totalScore(c) {
    return SCORE_KEYS.reduce((sum, k) => sum + (Number(c.scores && c.scores[k]) || 0), 0);
  }

  /* Best first; equal totals in recording order. */
  function sortCandidates(list) {
    return [...list].sort((a, b) => totalScore(b) - totalScore(a) || a.start - b.start);
  }

  /* The stretch of the recording a candidate's mini timeline shows: the span
   * plus a margin on each side, so an edge can be dragged outwards. */
  function timelineWindow(start, end, duration) {
    const pad = Math.max(4, 0.25 * (end - start));
    const hi = Number.isFinite(duration) && duration > 0 ? duration : Infinity;
    return { start: Math.max(0, start - pad), end: Math.min(hi, end + pad) };
  }

  function toFrac(t, win) {
    return win.end > win.start ? Math.min(1, Math.max(0, (t - win.start) / (win.end - win.start))) : 0;
  }

  function fromFrac(f, win) {
    return win.start + Math.min(1, Math.max(0, f)) * (win.end - win.start);
  }

  /* Where an edge may land: a start at the start of a sentence (or of a word,
   * byWord), an end at the end of one. */
  function snapTargets(edge, sentences, words, byWord) {
    const list = byWord ? words : sentences;
    return (list || []).map((x) => (edge === "start" ? x.start : x.end)).filter(Number.isFinite);
  }

  /* The edge dropped at `t`, moved to the nearest snap target that keeps the
   * clip at least `minLen` long; unsnapped (but kept in order) when none does. */
  function snapEdge(t, edge, span, sentences, words, byWord, minLen = 1) {
    const ok = (v) => (edge === "start" ? v <= span.end - minLen && v >= 0 : v >= span.start + minLen);
    let best = null;
    for (const v of snapTargets(edge, sentences, words, byWord)) {
      if (ok(v) && (best === null || Math.abs(v - t) < Math.abs(best - t))) best = v;
    }
    if (best !== null) return best;
    return edge === "start" ? Math.max(0, Math.min(t, span.end - minLen)) : Math.max(t, span.start + minLen);
  }

  /* The next snap target before (dir -1) or after (dir +1) the edge, for arrow keys. */
  function stepEdge(edge, span, dir, sentences, words, byWord, minLen = 1) {
    const cur = span[edge];
    const targets = snapTargets(edge, sentences, words, byWord)
      .filter((v) => (dir < 0 ? v < cur - 1e-6 : v > cur + 1e-6))
      .filter((v) => (edge === "start" ? v >= 0 && v <= span.end - minLen : v >= span.start + minLen));
    if (!targets.length) return cur;
    return dir < 0 ? Math.max(...targets) : Math.min(...targets);
  }

  /* The sentence (or word) at time t, for the readout under a timeline. */
  function itemAt(t, list) {
    for (const x of list || []) if (x.start <= t + 1e-6 && t <= x.end + 1e-6) return x;
    return null;
  }

  /* This viewer's decision on candidate `c`: `draft` itself when it was made for
   * c's current span (`draft.for`, [start, end]), else the newest sent pick
   * when it fits c, else undecided at c's own edges. */
  function clipDraft(c, draft, lastSent) {
    const f = draft && draft.for;
    if (Array.isArray(f) && Math.abs(f[0] - c.start) < 0.001 && Math.abs(f[1] - c.end) < 0.001) return draft;
    const span = [c.start, c.end];
    if (pickFits(lastSent, c)) {
      return { decision: lastSent.decision, start: lastSent.start, end: lastSent.end, note: lastSent.text || "", for: span };
    }
    return { decision: null, start: c.start, end: c.end, note: "", for: span };
  }

  /* Pick rows for the candidates decided here whose decision, edges or note
   * differ from the newest pick row already sent for them. */
  function unsentPicks(decisions, sent) {
    const rows = [];
    for (const [id, d] of Object.entries(decisions || {})) {
      if (d.decision !== "accept" && d.decision !== "reject") continue;
      const row = { kind: "pick", candidate: id, decision: d.decision,
                    start: Math.round(d.start * 1000) / 1000, end: Math.round(d.end * 1000) / 1000,
                    text: (d.note || "").trim() };
      const last = sent && sent.get(id);
      if (last && last.decision === row.decision && Math.abs(last.start - row.start) < 0.001
          && Math.abs(last.end - row.end) < 0.001 && (last.text || "") === row.text) continue;
      rows.push(row);
    }
    return rows;
  }

  // --- Speakers -------------------------------------------------------------------------

  /* A rectangle from two corners, in either order. */
  function normRect(x0, y0, x1, y1) {
    return { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
  }

  /* `r` kept inside a W x H frame (source pixels), at least `min` on each side. */
  function clampRect(r, W, H, min = 8) {
    const w = Math.min(W, Math.max(min, r.w)), h = Math.min(H, Math.max(min, r.h));
    return { x: Math.min(W - w, Math.max(0, r.x)), y: Math.min(H - h, Math.max(0, r.y)), w, h };
  }

  function moveRect(r, dx, dy, W, H) {
    return clampRect({ ...r, x: r.x + dx, y: r.y + dy }, W, H);
  }

  /* `r` with one corner ("nw", "ne", "sw", "se") dragged to (x, y); the opposite corner stays. */
  function resizeRect(r, corner, x, y, W, H) {
    const fx = corner.includes("w") ? r.x + r.w : r.x;
    const fy = corner.includes("n") ? r.y + r.h : r.y;
    const cx = Math.min(W, Math.max(0, x)), cy = Math.min(H, Math.max(0, y));
    return clampRect(normRect(fx, fy, cx, cy), W, H);
  }

  /* Source pixels → whole source pixels. */
  function roundRect(r) {
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
  }

  /* "A", "B", … — the first letter no box uses yet. */
  function nextBoxName(boxes) {
    const used = new Set((boxes || []).map((b) => b.name));
    for (let i = 0; i < 26; i += 1) {
      const n = String.fromCharCode(65 + i);
      if (!used.has(n)) return n;
    }
    return `Box ${(boxes || []).length + 1}`;
  }

  /* Why the boxes cannot be sent yet, or null. `labels` are the transcript's
   * speaker labels: an EDL that follows the speaker (frame "speaker") is
   * refused unless every one of them maps to a box. */
  function panelsProblem(boxes, mapping, labels) {
    if (!boxes || !boxes.length) return "Draw at least one box.";
    if (boxes.length > 8) return "At most 8 boxes.";
    const names = boxes.map((b) => b.name.trim());
    if (names.some((n) => !n)) return "Every box needs a name.";
    if (names.some((n) => n.length > 40)) return "Box names are at most 40 characters.";
    if (new Set(names).size !== names.length) return "Two boxes have the same name.";
    for (const [label, name] of Object.entries(mapping || {})) {
      if (name && !names.includes(name)) return `${label} is mapped to a box that no longer exists.`;
    }
    const unmapped = (labels || []).filter((l) => !(mapping || {})[l]);
    if (unmapped.length) {
      return `Pick a box for ${unmapped.join(", ")}: a clip that follows the speaker needs every speaker mapped.`;
    }
    return null;
  }

  /* The panels row: boxes by name in whole source pixels, mapped labels only. */
  function panelsRow(boxes, mapping, note) {
    const panels = {};
    for (const b of boxes) panels[b.name.trim()] = roundRect(b);
    const speakers = {};
    for (const [label, name] of Object.entries(mapping || {})) if (name) speakers[label] = name;
    return { kind: "panels", panels, speakers, text: (note || "").trim() };
  }

  // --- Look and Music: shared ----------------------------------------------------------

  /* Whether a valid input file offers anything in any of the lists `keys`. */
  function offersAny(file, keys) {
    return !!file && file.status === "ok" && keys.some((k) => (file.data[k] || []).length > 0);
  }

  /* `v` with object keys sorted at every depth, so two values compare as JSON. */
  function canon(v) {
    if (Array.isArray(v)) return v.map(canon);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])]));
    }
    return v;
  }

  function same(a, b) {
    return JSON.stringify(canon(a === undefined ? null : a)) === JSON.stringify(canon(b === undefined ? null : b));
  }

  /* Whether `row` says what `last` (the newest row sent of its kind) already says. */
  function sameChoice(row, last, keys) {
    return !!last && keys.every((k) => same(row[k], last[k])) && (last.text || "") === row.text;
  }

  /* The newest row of `kind`, or null. */
  function lastOfKind(rows, kind) {
    const list = rowsOfKind(rows, kind);
    return list.length ? list[list.length - 1] : null;
  }

  /* The names in list `key` of a valid input file. */
  function namesIn(file, key) {
    return file && file.status === "ok" ? (file.data[key] || []).map((x) => x.name) : [];
  }

  /* A name still on offer in list `key`, or null (null means none chosen). */
  function onOffer(file, key, name) {
    return typeof name === "string" && namesIn(file, key).includes(name) ? name : null;
  }

  // --- Look ------------------------------------------------------------------------------

  /* What a render uses without a brand file (stages/brand.py Brand). */
  const BRAND_DEFAULTS = { primary: "#FFFFFF", accent: "#FFD400", background: "#0a0a0a", caption_outline: "#000000" };
  const MAX_CHARS = 22;                 // stages/edl.py Captions.max_chars
  const SAMPLE_DEFAULT_S = 4;

  /* Look is open while looks.json offers something and no look row has been sent. */
  function lookOpen(s) {
    return offersAny(s.looks, ["styles", "brands", "captions"]) && rowsOfKind(s.rows, "look").length === 0;
  }

  /* The output frame a caption option is judged on: 9:16 for reels, 16:9 for a demo. */
  function outputSize(job) {
    return isReels(job) === false ? { width: 1920, height: 1080 } : { width: 1080, height: 1920 };
  }

  /* The four colours a brand gives captions, defaults filled in. `b` is an /api/brands item. */
  function brandColours(b) {
    return { ...BRAND_DEFAULTS, ...(b && b.status === "ok" && b.colours ? b.colours : {}) };
  }

  /* Caption size and place on a stage `stageH` pixels tall that shows output `out`,
   * as stages/captions.py layout_metrics computes them in output pixels. */
  function captionMetrics(values, out, stageH) {
    const v = values || {};
    const size = Number.isFinite(v.size) ? v.size : Math.round(out.height * 0.036);
    const k = stageH / out.height;
    return {
      fontPx: size * k,
      centerY: Number.isFinite(v.position_y) ? v.position_y : 0.68,
      outlinePx: Math.max(2, Math.round(size * 0.07)) * k,
      sideMargin: 0.12,
    };
  }

  /* One caption line from `words` ({text}): up to words_per_line words within
   * MAX_CHARS characters. `spoken` is the highlighted word's index, -1 when
   * the option turns highlighting off. */
  function captionLine(words, values, maxChars = MAX_CHARS) {
    const v = values || {};
    const per = Number.isInteger(v.words_per_line) && v.words_per_line > 0 ? v.words_per_line : 5;
    const line = [];
    for (const w of words || []) {
      const text = String((w && w.text) || "").trim();
      if (!text) continue;
      if (line.length && (line.length >= per || [...line, text].join(" ").length > maxChars)) break;
      line.push(text);
    }
    return { words: line, spoken: v.highlight === false || !line.length ? -1 : Math.min(1, line.length - 1) };
  }

  /* This viewer's look choice ({style, brand, caption, note}; names, null for none):
   * the draft while everything it names is still on offer, else the newest sent
   * look row, else the first of each list. */
  function lookDraft(looks, draft, last) {
    const ok = (key, n) => n === null || (typeof n === "string" && namesIn(looks, key).includes(n));
    if (draft && ok("styles", draft.style) && ok("brands", draft.brand) && ok("captions", draft.caption)) {
      return { style: draft.style, brand: draft.brand, caption: draft.caption, note: draft.note || "" };
    }
    if (last) {
      const caps = looks && looks.status === "ok" ? looks.data.captions || [] : [];
      const cap = last.captions ? caps.find((o) => same(o.values, last.captions)) : null;
      return { style: onOffer(looks, "styles", last.style), brand: onOffer(looks, "brands", last.brand),
               caption: cap ? cap.name : null, note: last.text || "" };
    }
    const first = (key) => namesIn(looks, key)[0] ?? null;
    return { style: first("styles"), brand: first("brands"), caption: first("captions"), note: "" };
  }

  /* The look row for a draft: the caption option's values, not its name. */
  function lookRow(draft, looks) {
    const caps = looks && looks.status === "ok" ? looks.data.captions || [] : [];
    const opt = caps.find((o) => o.name === draft.caption);
    return { kind: "look", style: draft.style, brand: draft.brand, captions: opt ? opt.values : null,
             text: (draft.note || "").trim() };
  }

  /* Why a look row cannot be sent, or null. `brands` is /api/brands: a brand
   * whose file cannot be read would fail at render, so it cannot be sent. */
  function lookProblem(row, brands) {
    if (row.style === null && row.brand === null && row.captions === null) {
      return "Pick a style, a brand or a caption option.";
    }
    const bad = row.brand !== null && (brands || []).find((b) => b.name === row.brand && b.status !== "ok");
    return bad ? `The ${row.brand} brand file cannot be used; pick another brand or none.` : null;
  }

  /* The time to show in a looping sample page `elapsed` seconds after it started:
   * it plays its `duration`, holds the last frame for `hold` seconds, and starts over. */
  function loopTime(elapsed, duration, hold = 1) {
    const d = Number.isFinite(duration) && duration > 0 ? duration : SAMPLE_DEFAULT_S;
    const period = d + Math.max(0, hold);
    const e = Number.isFinite(elapsed) ? elapsed : 0;
    return Math.min(d, ((e % period) + period) % period);
  }

  /* The scale that fits a w x h page inside a boxW x boxH box. */
  function fitScale(w, h, boxW, boxH) {
    return w > 0 && h > 0 && boxW > 0 && boxH > 0 ? Math.min(boxW / w, boxH / h) : 1;
  }

  /* A sample page's "ready" message ({type, width, height, duration}, from
   * static/sample-loop.js), checked; null for anything else. */
  function readyMessage(data) {
    if (!data || typeof data !== "object" || data.type !== "ready") return null;
    const { width, height, duration } = data;
    const num = (x, lo, hi) => typeof x === "number" && Number.isFinite(x) && x >= lo && x <= hi;
    if (!num(width, 1, 4096) || !num(height, 1, 4096) || !num(duration, 0, 30)) return null;
    return { width, height, duration: duration > 0 ? duration : SAMPLE_DEFAULT_S };
  }

  // --- Music -----------------------------------------------------------------------------

  const DEFAULT_VOLUME_DB = -10;        // stages/edl.py Music.volume_db
  const SFX_VOLUME_DB = -14;            // stages/edl.py Sfx.volume_db
  const MIN_VOLUME_DB = -40, MAX_VOLUME_DB = 0;

  /* Music is open while music.json offers something and no music row has been sent. */
  function musicOpen(s) {
    return offersAny(s.music, ["beds", "sfx_sets"]) && rowsOfKind(s.rows, "music").length === 0;
  }

  /* Decibels → a Web Audio gain. */
  function dbToGain(db) {
    return Number.isFinite(db) ? Math.pow(10, db / 20) : 1;
  }

  function clampDb(db) {
    return Number.isFinite(db) ? Math.min(MAX_VOLUME_DB, Math.max(MIN_VOLUME_DB, db)) : DEFAULT_VOLUME_DB;
  }

  /* This viewer's music choice ({bed, sfx_set, volume_db, note}): as lookDraft. */
  function musicDraft(music, draft, last) {
    const ok = (key, n) => n === null || (typeof n === "string" && namesIn(music, key).includes(n));
    if (draft && ok("beds", draft.bed) && ok("sfx_sets", draft.sfx_set)) {
      return { bed: draft.bed, sfx_set: draft.sfx_set, volume_db: clampDb(draft.volume_db), note: draft.note || "" };
    }
    if (last) {
      return { bed: onOffer(music, "beds", last.bed), sfx_set: onOffer(music, "sfx_sets", last.sfx_set),
               volume_db: clampDb(last.volume_db), note: last.text || "" };
    }
    return { bed: namesIn(music, "beds")[0] ?? null, sfx_set: null, volume_db: DEFAULT_VOLUME_DB, note: "" };
  }

  function musicRow(draft) {
    return { kind: "music", bed: draft.bed, volume_db: Math.round(clampDb(draft.volume_db) * 10) / 10,
             sfx_set: draft.sfx_set, text: (draft.note || "").trim() };
  }

  /* Why a music row cannot be sent, or null. */
  function musicProblem(row) {
    return row.bed === null && row.sfx_set === null ? "Pick a music bed or a sound-effect set." : null;
  }

  /* "−10 dB" */
  function fmtDb(db) {
    const v = Math.round(db * 10) / 10;
    return `${v < 0 ? "−" : ""}${Math.abs(v)} dB`;
  }

  // --- Review: drawing on a paused frame -------------------------------------------------

  const DRAW_TOOLS = ["pen", "arrow", "box"];
  const DRAW_COLOURS = [["Yellow", "#FFD400"], ["Red", "#FF3B30"], ["Cyan", "#00C2FF"]];
  // The server's limits (stages/review_server.py; tests/test_review_logic_js.py checks they agree).
  const MAX_SHAPES = 100;
  const MAX_PEN_POINTS = 2000;
  const MAX_DRAWING_POINTS = 6000;        // all of one drawing's shapes together
  const MAX_MARK_BYTES = 4 * 1024 * 1024;
  const MAX_BODY_BYTES = 256 * 1024;      // one POST /api/comments
  const MAX_EXPORT_SIDE = 3840;

  const round4 = (v) => Math.round(v * 10000) / 10000;
  const clamp01 = (v) => Math.min(1, Math.max(0, v));

  function isColour(c) {
    return typeof c === "string" && /^#[0-9a-fA-F]{6}$/.test(c);
  }

  /* A pointer position as [x, y] fractions of the picture (`rect` in the same
   * coordinates as the pointer), clamped to the picture. */
  function toPictureFrac(clientX, clientY, rect) {
    if (!(rect.width > 0 && rect.height > 0)) return [0, 0];
    return [round4(clamp01((clientX - rect.left) / rect.width)), round4(clamp01((clientY - rect.top) / rect.height))];
  }

  /* A pen stroke without the points closer than `minDist` to the one kept
   * before them (the last point always stays), then thinned to at most
   * `max` points by keeping evenly spaced ones. */
  function simplifyStroke(points, minDist = 0.002, max = MAX_PEN_POINTS) {
    if (points.length <= 2) return points.map((p) => [round4(p[0]), round4(p[1])]);
    const kept = [points[0]];
    for (let i = 1; i < points.length - 1; i++) {
      const a = kept[kept.length - 1], b = points[i];
      if (Math.hypot(b[0] - a[0], b[1] - a[1]) >= minDist) kept.push(b);
    }
    kept.push(points[points.length - 1]);
    let out = kept;
    if (out.length > max) {
      const step = (out.length - 1) / (max - 1);
      out = Array.from({ length: max }, (_, i) => kept[Math.round(i * step)]);
    }
    return out.map((p) => [round4(p[0]), round4(p[1])]);
  }

  /* The shape a drag made, or null when it is too small to mean anything
   * (an arrow or a box needs a drag; a pen click is a dot). A pen stroke is
   * thinned to `maxPoints`; with fewer than 2 to spare there is no shape. */
  function finishShape(tool, color, points, maxPoints = MAX_PEN_POINTS) {
    if (!DRAW_TOOLS.includes(tool) || !isColour(color) || !points.length || !(maxPoints >= 2)) return null;
    if (tool === "pen") return { tool, color, points: simplifyStroke(points, undefined, Math.min(MAX_PEN_POINTS, maxPoints)) };
    const a = points[0], b = points[points.length - 1];
    if (Math.hypot(b[0] - a[0], b[1] - a[1]) < 0.01) return null;
    return { tool, color, points: [[round4(a[0]), round4(a[1])], [round4(b[0]), round4(b[1])]] };
  }

  /* What one more shape may use in a drawing that has `shapes`: null when the
   * drawing is full (MAX_SHAPES shapes, or under 2 of MAX_DRAWING_POINTS left),
   * else the most points it may have. */
  function drawingRoom(shapes) {
    const used = shapes.reduce((n, s) => n + s.points.length, 0);
    const left = MAX_DRAWING_POINTS - used;
    return shapes.length >= MAX_SHAPES || left < 2 ? null : Math.min(MAX_PEN_POINTS, left);
  }

  /* Line width for a picture `w` pixels wide: 0.5% of it, at least 2 px. */
  function strokeWidth(w) {
    return Math.max(2, Math.round(w * 0.005));
  }

  /* The polylines that draw `shape` on a w×h picture, in pixels: a pen is its
   * stroke (a dot for one point), a box its closed outline, an arrow its shaft
   * and a two-line head at the second point. */
  function shapePaths(shape, w, h) {
    const px = shape.points.map(([x, y]) => [x * w, y * h]);
    const width = strokeWidth(w);
    if (shape.tool === "pen") return { color: shape.color, width, paths: [px.length === 1 ? [px[0], px[0]] : px] };
    const [[x0, y0], [x1, y1]] = px;
    if (shape.tool === "box") {
      return { color: shape.color, width, paths: [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] };
    }
    const ang = Math.atan2(y1 - y0, x1 - x0), size = Math.max(10, width * 5), spread = Math.PI / 7;
    const head = [
      [x1 - size * Math.cos(ang - spread), y1 - size * Math.sin(ang - spread)],
      [x1, y1],
      [x1 - size * Math.cos(ang + spread), y1 - size * Math.sin(ang + spread)],
    ];
    return { color: shape.color, width, paths: [[[x0, y0], [x1, y1]], head] };
  }

  /* The size to export a frame at: its native size, scaled down only when a
   * side is longer than MAX_EXPORT_SIDE. */
  function exportSize(vw, vh, maxSide = MAX_EXPORT_SIDE) {
    if (!(vw > 0 && vh > 0)) return null;
    const k = Math.min(1, maxSide / Math.max(vw, vh));
    return { width: Math.round(vw * k), height: Math.round(vh * k) };
  }

  /* The mailbox row for a pending drawing (review_server: kind "drawing"). */
  function drawingRow(d) {
    return { kind: "drawing", video: d.video, version: d.version, t: Math.round(d.t * 1000) / 1000,
             image: d.image, shapes: d.shapes.map((s) => ({ tool: s.tool, color: s.color, points: s.points })),
             text: (d.text || "").trim() };
  }

  // --- Review: the timeline from a render's sidecar ----------------------------------------

  const LANES = [["segments", "Segments"], ["captions", "Captions"], ["headline", "Headline"],
                 ["overlays", "Overlays"], ["sfx", "Sounds"]];
  const MIN_RANGE_S = 0.1;
  const TICK_RANGE_S = 0.25;     // a sound effect's range: this long either side of it

  const fileName = (p) => String(p || "").split(/[\\/]/).pop();
  const r3 = (v) => Math.round(v * 1000) / 1000;

  /* The Review timeline from a render's sidecar (stages/render_sidecar.py):
   * {duration, lanes: [{id, label, blocks: [{start, end, label, title, target, xfade?, tick?}]}]}
   * in output seconds; `duration` is the render's. `target` is what a range row names for that block. */
  function timelineLanes(side) {
    const e = (side && side.edl) || {};
    const segs = [];
    if (e.title) {
      segs.push({ start: 0, end: Number(e.title.duration) || 0, label: "Title",
                  title: [e.title.line1, e.title.line2].filter(Boolean).join(" / "), target: "title" });
    }
    for (const s of (side && side.segments) || []) {
      const notes = [`source ${fmt(s.src_start)}–${fmt(s.src_end)}`];
      if (s.speed !== 1) notes.push(`${s.speed}× speed`);
      if (s.xfade_in > 0) notes.push(`${s.xfade_in} s crossfade in`);
      if (s.muted) notes.push("muted");
      segs.push({ start: s.out_start, end: s.out_end, xfade: s.xfade_in || 0, label: `seg ${s.index}`,
                  title: `seg ${s.index}: ${notes.join(", ")}`, target: `segment:${s.index}` });
    }
    const caps = ((side && side.captions_events) || []).map((c) => (
      { start: c.start, end: c.end, label: c.text, title: `${fmt(c.start)} ${c.text}`, target: "captions" }));
    const head = e.headline ? [{ start: e.headline.start, end: e.headline.end, label: e.headline.text,
                                 title: `Headline: ${e.headline.text}`, target: "headline" }] : [];
    const overlays = (e.overlays || []).map((o, i) => {
      const label = fileName(o.html) + (o.style ? ` · ${o.style}` : "");
      return { start: o.start, end: o.end, label, title: `overlay ${i}: ${label} (${o.mode || "keyed"})`, target: `overlay:${i}` };
    });
    const sfx = (e.sfx || []).map((x, i) => (
      { start: x.at, end: x.at, tick: true, label: fileName(x.sound), title: `${fmt(x.at)} ${fileName(x.sound)}`, target: `sfx:${i}` }));
    const blocks = { segments: segs, captions: caps, headline: head, overlays, sfx };
    // The render's length; a block past it (the last caption's hold can be) is cut off at
    // the edge, as the render cuts it. Only a sidecar without a duration takes the blocks'.
    const given = Number(side && side.duration);
    const duration = given > 0 ? given : Math.max(0, ...Object.values(blocks).flat().map((b) => b.end));
    return { duration, lanes: LANES.map(([id, label]) => ({ id, label, blocks: blocks[id] })) };
  }

  /* A block's left and width on a timeline `duration` long, in percent. */
  function blockBox(b, duration) {
    if (!(duration > 0)) return { left: 0, width: 0 };
    const left = clamp01(b.start / duration), right = clamp01(b.end / duration);
    return { left: round4(left * 100), width: round4(Math.max(0, right - left) * 100) };
  }

  /* The time at fraction `f` across a timeline `duration` long. */
  function timeAtFrac(f, duration) {
    return r3(clamp01(f) * (duration > 0 ? duration : 0));
  }

  /* A drag on the ruler from t0 to t1 → {t_start, t_end} in order and within
   * the render, or null when it is shorter than `minLen` (a click). */
  function rangeFromDrag(t0, t1, duration, minLen = MIN_RANGE_S) {
    const hi = duration > 0 ? duration : Infinity;
    const a = Math.min(hi, Math.max(0, Math.min(t0, t1))), b = Math.min(hi, Math.max(0, Math.max(t0, t1)));
    return b - a >= minLen ? { t_start: r3(a), t_end: r3(b) } : null;
  }

  /* The range a click on a block marks: the block, or TICK_RANGE_S either side of a tick. */
  function blockRange(b, duration) {
    const hi = duration > 0 ? duration : Infinity;
    if (b.end - b.start >= MIN_RANGE_S) return { t_start: r3(Math.max(0, b.start)), t_end: r3(Math.min(hi, b.end)) };
    return { t_start: r3(Math.max(0, b.start - TICK_RANGE_S)), t_end: r3(Math.min(hi, b.start + TICK_RANGE_S)) };
  }

  /* "overlay:2" → "overlay 2"; null → "" */
  function targetLabel(target) {
    return target ? target.replace(":", " ") : "";
  }

  /* The mailbox row for a pending range comment (review_server: kind "range"). */
  function rangeRow(d) {
    return { kind: "range", video: d.video, version: d.version, t_start: r3(d.t_start), t_end: r3(d.t_end),
             target: d.target || null, text: (d.text || "").trim() };
  }

  // --- Review: the style panel ------------------------------------------------------------------

  // What the panel edits, with the server's bounds (stages/review_server.py; looks.schema.json for captions).
  const CAPTION_FIELDS = [
    { key: "size", label: "Size (output px)", type: "int", min: 8, max: 400, step: 1 },
    { key: "position_y", label: "Height (0 top, 1 bottom)", type: "number", min: 0, max: 1, step: 0.005 },
    { key: "words_per_line", label: "Words a line", type: "int", min: 1, max: 20, step: 1 },
    { key: "max_chars", label: "Characters a line, at most", type: "int", min: 8, max: 80, step: 1 },
    { key: "highlight", label: "Colour the spoken word", type: "bool" },
  ];
  const BRAND_FIELDS = [["primary", "Text"], ["accent", "Spoken word, headline box"],
                        ["background", "Background"], ["caption_outline", "Outline"]];
  const STYLE_LABELS = { "fonts.display": "Display font", "fonts.body": "Body font",
                         "fonts.weights.display": "Display weight", "fonts.weights.body": "Body weight",
                         "shape.radius_px": "Corner radius (px)", "motion.enter_s": "Enter (s)",
                         "motion.exit_s": "Exit (s)", "motion.stagger_s": "Stagger (s)" };
  // One CSS value the session copies into a page (stages/review_server.py CSS_VALUE_RE and _css_text).
  const CSS_VALUE_RE = /^[^;{}<>\\\u0000-\u001f\u007f]{1,200}$/;
  const SAVE_AS_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/;
  // stages/captions.py: a line appears LEAD_S before its first word, stays HOLD_S after its last,
  // leaves CLEAR_S before the next, and a pause longer than MAX_GAP_S ends it.
  const LEAD_S = 0.08, HOLD_S = 0.6, CLEAR_S = 0.05, MAX_GAP_S = 0.3;

  /* Whether `v` is one CSS value the server accepts: no ; { } < > \, comment or url(). */
  function isCssValue(v) {
    return typeof v === "string" && CSS_VALUE_RE.test(v.trim()) && !v.includes("/*") && !/url\s*\(/i.test(v);
  }

  /* A style value's field: a colour for palette keys, text for fonts, a bounded number otherwise. */
  function styleField(key, value) {
    const label = STYLE_LABELS[key] || key.replace(/^palette\./, "").replace(/_/g, " ");
    if (key.startsWith("palette.")) return { key, label: `${label} colour`, type: "colour", value };
    if (key === "fonts.display" || key === "fonts.body") return { key, label, type: "text", value };
    if (key.startsWith("fonts.weights.")) return { key, label, type: "int", min: 100, max: 1000, step: 100, value };
    if (key === "shape.radius_px") return { key, label, type: "number", min: 0, max: 1000, step: 1, value };
    return { key, label, type: "number", min: 0, max: 10, step: 0.05, value };
  }

  /* The panel's groups from an /api/style answer: one per row target ("captions", "brand",
   * "style:<name>"), each with its fields and their current values, or an error. */
  function styleGroups(info) {
    if (!info || info.status !== "ok") return [];
    const groups = [];
    if (info.captions) {
      const height = (info.output && info.output.height) || 1920;
      const cur = { ...info.captions };
      if (!Number.isFinite(cur.size)) cur.size = Math.round(height * 0.036);   // captions.py layout_metrics
      groups.push({ target: "captions", title: "Captions", saveAs: false,
                    fields: CAPTION_FIELDS.filter((f) => cur[f.key] !== undefined).map((f) => ({ ...f, value: cur[f.key] })) });
    }
    const b = info.brand || {};
    const brandTitle = b.file ? `Brand · ${fileName(b.file)}` : "Brand · none (the defaults)";
    if (b.status === "ok") {
      groups.push({ target: "brand", title: brandTitle, saveAs: true,
                    fields: BRAND_FIELDS.map(([key, label]) => ({ key, label, type: "colour", value: String(b.colours[key]).toUpperCase() })) });
    } else {
      groups.push({ target: "brand", title: brandTitle, saveAs: true, fields: [], error: b.error || "cannot read the brand file" });
    }
    for (const s of info.styles || []) {
      const title = `Style · ${fileName(s.name)}`;
      if (s.status !== "ok") { groups.push({ target: `style:${s.name}`, title, saveAs: true, fields: [], error: s.error }); continue; }
      groups.push({ target: `style:${s.name}`, title, saveAs: true,
                    fields: Object.entries(s.values).map(([k, v]) => styleField(k, v)) });
    }
    return groups;
  }

  /* An edited field value, cleaned: {value} or {error}. */
  function checkField(f, raw) {
    if (f.type === "bool") return { value: !!raw };
    if (f.type === "colour") {
      return isColour(raw) ? { value: raw.toUpperCase() } : { error: `${f.label} must be a #RRGGBB colour` };
    }
    if (f.type === "text") {
      const v = String(raw).trim();
      return isCssValue(v) ? { value: v }
        : { error: `${f.label} must be 1–200 characters without ; { } < > \\ or url()` };
    }
    const n = typeof raw === "number" ? raw : Number(String(raw).trim());
    if (String(raw).trim() === "" || !Number.isFinite(n)) return { error: `${f.label} must be a number` };
    if (f.type === "int" && !Number.isInteger(n)) return { error: `${f.label} must be a whole number` };
    if (n < f.min || n > f.max) return { error: `${f.label} must be within ${f.min}–${f.max}` };
    return { value: n };
  }

  function sameValue(a, b) {
    return typeof a === "string" && typeof b === "string" ? a.toUpperCase() === b.toUpperCase() : a === b;
  }

  /* One group's changes from the edits for it ({key: raw}): {changes: {key: [old, new]}, errors}. */
  function groupChanges(group, edits) {
    const changes = {}, errors = [];
    for (const f of group.fields) {
      if (!edits || !(f.key in edits)) continue;
      const got = checkField(f, edits[f.key]);
      if (got.error) errors.push(got.error);
      else if (!sameValue(got.value, f.value)) changes[f.key] = [f.value, got.value];
    }
    return { changes, errors };
  }

  /* The style rows for the edits ({target: {key: raw}}) and save-as names ({target: name}),
   * one per group that changes something, and why they cannot be sent: {rows, problem}. */
  function styleRows(video, version, groups, edits, saveAs, note) {
    const rows = [], problems = [];
    for (const g of groups) {
      const { changes, errors } = groupChanges(g, (edits || {})[g.target]);
      problems.push(...errors);
      if (!Object.keys(changes).length) continue;
      const row = { kind: "style", video, version, target: g.target, changes, text: (note || "").trim() };
      const name = ((saveAs || {})[g.target] || "").trim();
      if (name && g.saveAs) {
        if (SAVE_AS_RE.test(name)) row.save_as = name;
        else problems.push(`"Save as" must be a file name of letters, digits, - or _ (${g.title})`);
      }
      rows.push(row);
    }
    if (!rows.length && !problems.length) problems.push("Change a value to send it.");
    return { rows, problem: problems.length ? problems[0] : null };
  }

  /* The caption values a preview draws with: the panel's captions group with its edits applied
   * where they are valid. */
  function editedValues(group, edits) {
    const out = {};
    for (const f of (group && group.fields) || []) {
      const got = edits && f.key in edits ? checkField(f, edits[f.key]) : { value: f.value };
      out[f.key] = got.error ? f.value : got.value;
    }
    return out;
  }

  /* Words into caption lines as the edited values would break them: a line ends at
   * words_per_line words, before it would pass max_chars characters, after a word that
   * ends a sentence, or at a pause longer than MAX_GAP_S; it shows from LEAD_S before its
   * first word until HOLD_S after its last, leaving CLEAR_S before the next. A rough copy
   * of stages/captions.py group_words and line_times (it does not join short lines), for
   * the preview only. */
  function regroupWords(words, values) {
    const per = values.words_per_line || 5, max = values.max_chars || 22;
    const lines = [];
    let cur = [];
    for (const w of words) {
      const prev = cur[cur.length - 1];
      if (cur.length && (cur.length >= per || w.start - prev.end > MAX_GAP_S
                         || [...cur, w].map((x) => x.text).join(" ").length > max)) { lines.push(cur); cur = []; }
      cur.push(w);
      if (/[.?!…]$/.test(w.text)) { lines.push(cur); cur = []; }
    }
    if (cur.length) lines.push(cur);
    const ins = lines.map((ws) => Math.max(0, ws[0].start - LEAD_S));
    return lines.map((ws, i) => {
      const out = ws[ws.length - 1].end + HOLD_S;
      return { start: ins[i], end: i + 1 < lines.length ? Math.min(out, ins[i + 1] - CLEAR_S) : out, words: ws };
    });
  }

  /* The caption line a preview shows at output time t: {words: [text], spoken} (spoken is the
   * index of the word being said, -1 without highlighting), or null between lines. The render's
   * own lines (captions_events) are used while the line breaks are unchanged. */
  function captionAt(events, t, values, original) {
    const evs = (events || []).filter((e) => Array.isArray(e.words) && e.words.length);
    const same = original && values.words_per_line === original.words_per_line && values.max_chars === original.max_chars;
    const lines = same ? evs : regroupWords(evs.flatMap((e) => e.words), values);
    const line = lines.find((l) => l.start <= t + 1e-6 && t < l.end);
    if (!line) return null;
    let spoken = -1;
    if (values.highlight !== false) {
      line.words.forEach((w, i) => { if (w.start <= t + 1e-6) spoken = i; });
      spoken = Math.max(0, spoken);
    }
    return { words: line.words.map((w) => w.text), spoken };
  }

  /* Whether a group has edits that change a value (for the caption preview). */
  function groupEdited(group, edits) {
    return !!group && Object.keys(groupChanges(group, edits).changes).length > 0;
  }

  /* {key: [old, new]} → "size 64 → 80, highlight true → false" */
  function changeSummary(changes) {
    return Object.entries(changes || {}).map(([k, [a, b]]) => `${k} ${a === null ? "—" : a} → ${b}`).join(", ");
  }

  /* The style and restyle rows sent about `clip`, oldest first. */
  function styleSent(rows, clip) {
    return (rows || []).filter((r) => ["style", "restyle"].includes(r.kind) && r.video === clip);
  }

  // --- Review: live overlays and click-to-restyle -----------------------------------------------

  // What the overlay bridge reports for a picked element, and what a restyle row may change
  // (stages/review_server.py RESTYLE_PROPS; static/overlay-bridge.js PROPS).
  const RESTYLE_PROPS = ["color", "background-color", "font-family", "font-size", "font-weight", "opacity", "transform"];
  const MAX_SELECTOR = 300, MAX_OLD_VALUE = 300, MAX_PICK_TEXT = 80;

  /* The overlays of a render's sidecar the page can play live: those it has a key for
   * (review_server overlay_keys), each with its place in output pixels.
   * [{index, html, key, start, end, mode, transparent, rect: {x, y, w, h}, style}] */
  function liveOverlays(side, keys) {
    const e = (side && side.edl) || {};
    const out = e.output || {};
    const W = Number(out.width) || 1080, H = Number(out.height) || 1920;
    return (e.overlays || []).map((o, index) => {
      const key = keys && typeof o.html === "string" ? keys[o.html] : null;
      if (!key) return null;
      const r = o.region || { x: 0, y: 0, w: W, h: H };
      const mode = o.mode || "keyed";
      return { index, html: o.html, key, start: Number(o.start), end: Number(o.end), mode,
               transparent: mode !== "panel", rect: { x: r.x, y: r.y, w: r.w, h: r.h }, style: o.style || null };
    }).filter(Boolean);
  }

  /* The overlay's own clock at render time t (its page starts at 0), or null when it is not on screen. */
  function overlayTime(o, t) {
    return Number.isFinite(t) && t >= o.start && t < o.end ? r3(t - o.start) : null;
  }

  /* Where an overlay's frame goes over the picture: CSS pixels from the picture's top left,
   * and the scale from output pixels. `out` is the render's size, `picture` the shown one. */
  function regionBox(rect, out, picture) {
    const scale = out && out.width > 0 ? picture.width / out.width : 0;
    return { left: rect.x * scale, top: rect.y * scale, width: rect.w * scale, height: rect.h * scale, scale };
  }

  /* A message from an overlay frame (static/overlay-bridge.js), checked:
   * {type: "ready", width, height} or {type: "picked", selector, box, styles, text}; null otherwise. */
  function bridgeMessage(data) {
    if (!data || typeof data !== "object") return null;
    const num = (x, lo, hi) => typeof x === "number" && Number.isFinite(x) && x >= lo && x <= hi;
    if (data.type === "ready") {
      return num(data.width, 1, 4096) && num(data.height, 1, 4096) ? { type: "ready", width: data.width, height: data.height } : null;
    }
    if (data.type !== "picked") return null;
    const { selector, box, styles, text } = data;
    if (typeof selector !== "string" || !selector || selector.length > MAX_SELECTOR || /[\u0000-\u001f\u007f]/.test(selector)) return null;
    if (!box || ![box.x, box.y].every((v) => num(v, -8192, 8192)) || ![box.w, box.h].every((v) => num(v, 0, 8192))) return null;
    if (!styles || typeof styles !== "object") return null;
    const clean = {};
    for (const p of RESTYLE_PROPS) {
      if (typeof styles[p] !== "string") return null;
      clean[p] = styles[p].slice(0, MAX_OLD_VALUE);
    }
    return { type: "picked", selector, box: { x: box.x, y: box.y, w: box.w, h: box.h }, styles: clean,
             text: typeof text === "string" ? text.slice(0, MAX_PICK_TEXT) : "" };
  }

  /* "rgb(61, 220, 151)" or "#3ddc97" → "#3DDC97" for a colour input; null when it is not a plain colour. */
  function cssColourToHex(v) {
    if (isColour(v)) return v.toUpperCase();
    const m = /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(String(v || "").trim());
    if (!m || (m[4] !== undefined && Number(m[4]) !== 1)) return null;
    const hex = m.slice(1, 4).map((x) => Math.min(255, Number(x)).toString(16).padStart(2, "0")).join("");
    return `#${hex.toUpperCase()}`;
  }

  /* The changes a restyle makes ({prop: [computed, new]}) from the edits ({prop: raw}); a blank or
   * unchanged edit is left out. {changes, problem}: problem names the first edit the server would refuse. */
  function restyleChanges(picked, edits) {
    const changes = {};
    for (const p of RESTYLE_PROPS) {
      if (!edits || typeof edits[p] !== "string") continue;
      const v = edits[p].trim();
      if (!v || v === picked.styles[p]) continue;
      if (!isCssValue(v)) return { changes: {}, problem: `${p} must be one CSS value of 1–200 characters without ; { } < > \\ or url()` };
      changes[p] = [picked.styles[p], v];
    }
    return { changes, problem: null };
  }

  /* The restyle row for a picked element of overlay `o` at render time t, or {problem}. */
  function restyleRow(video, version, t, o, picked, edits, note) {
    const { changes, problem } = restyleChanges(picked, edits);
    if (problem) return { row: null, problem };
    if (!Object.keys(changes).length) return { row: null, problem: "Change a value to send it." };
    return { row: { kind: "restyle", video, version, t: r3(t), overlay: o.index, html: o.html,
                    selector: picked.selector, changes, text: (note || "").trim() }, problem: null };
  }

  // --- Review: sending -----------------------------------------------------------------------

  const REVIEW_KINDS = ["point", "drawing", "range"];
  const kindOf = (c) => c.kind || "point";

  /* The mailbox row for a pending Review comment of any kind. */
  function reviewRow(c) {
    if (kindOf(c) === "drawing") return drawingRow(c);
    if (kindOf(c) === "range") return rangeRow(c);
    const { video, version, t, x, y } = c;
    return { kind: "point", video, version, t, x, y, text: (c.text || "").trim() };
  }

  /* Whether the pending comments can be sent: some, and each with text but a drawing. */
  function canSendReview(pending) {
    return pending.length > 0 && pending.every((c) => kindOf(c) === "drawing" || !!(c.text || "").trim());
  }

  /* The sent rows the Review tab lists for `clip`: its point, drawing and range rows. */
  function reviewSent(rows, clip) {
    return (rows || []).filter((r) => REVIEW_KINDS.includes(kindOf(r)) && r.video === clip);
  }

  /* `rows` split into POST /api/comments bodies of at most `maxBytes`, in order,
   * as lists of indexes into `rows`. A row too big for any body goes alone (the
   * server refuses it; the server's own limits keep a drawing under the cap). */
  function sendBatches(rows, maxBytes = MAX_BODY_BYTES) {
    const enc = new TextEncoder();
    const empty = enc.encode(JSON.stringify({ comments: [] })).length;
    const out = [];
    let cur = [], size = empty;
    rows.forEach((r, i) => {
      const n = enc.encode(JSON.stringify(r)).length;
      if (cur.length && size + 1 + n > maxBytes) { out.push(cur); cur = []; size = empty; }   // 1: the comma
      size += n + (cur.length ? 1 : 0);
      cur.push(i);
    });
    if (cur.length) out.push(cur);
    return out;
  }

  /* Whether a key pressed in an element with tag `tag` (and input `type`) is the element's
   * own: text, number, colour and checkbox inputs, lists and text areas take Space and the
   * arrows, so the Review tab's player keys leave them alone. The seek slider and buttons
   * do not. */
  function takesKeys(tag, type) {
    const t = String(tag || "").toUpperCase();
    if (t === "TEXTAREA" || t === "SELECT") return true;
    return t === "INPUT" && !["range", "button", "submit", "reset"].includes(String(type || "text").toLowerCase());
  }

  const api = {
    TABS, INPUT_FILES, fmt, inputNotice, isReels, latestBy, rowsOfKind, clipsOpen, speakersOpen, firstOpenTab,
    oneAtATime,
    SCORE_KEYS, totalScore, sortCandidates, timelineWindow, toFrac, fromFrac, snapTargets, snapEdge, stepEdge,
    itemAt, pickFits, clipDraft, unsentPicks,
    normRect, clampRect, moveRect, resizeRect, roundRect, nextBoxName, panelsProblem, panelsRow,
    offersAny, canon, sameChoice, lastOfKind, namesIn,
    BRAND_DEFAULTS, lookOpen, outputSize, brandColours, captionMetrics, captionLine, lookDraft, lookRow,
    lookProblem, loopTime, fitScale, readyMessage,
    DEFAULT_VOLUME_DB, SFX_VOLUME_DB, MIN_VOLUME_DB, MAX_VOLUME_DB, musicOpen, dbToGain, clampDb, musicDraft,
    musicRow, musicProblem, fmtDb,
    DRAW_TOOLS, DRAW_COLOURS, MAX_SHAPES, MAX_PEN_POINTS, MAX_DRAWING_POINTS, MAX_MARK_BYTES, MAX_BODY_BYTES,
    isColour, toPictureFrac, simplifyStroke, finishShape, drawingRoom, strokeWidth, shapePaths, exportSize, drawingRow,
    LANES, MIN_RANGE_S, timelineLanes, blockBox, timeAtFrac, rangeFromDrag, blockRange, targetLabel, rangeRow,
    reviewRow, canSendReview, reviewSent, sendBatches, takesKeys,
    CAPTION_FIELDS, BRAND_FIELDS, isCssValue, styleField, styleGroups, checkField, groupChanges, styleRows,
    editedValues, regroupWords, captionAt, groupEdited, changeSummary, styleSent,
    RESTYLE_PROPS, MAX_SELECTOR, liveOverlays, overlayTime, regionBox, bridgeMessage, cssColourToHex,
    restyleChanges, restyleRow,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JobLogic = api;
})(typeof window !== "undefined" ? window : this);
