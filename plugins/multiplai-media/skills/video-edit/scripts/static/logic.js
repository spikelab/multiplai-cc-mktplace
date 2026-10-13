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

  /* Why the boxes cannot be sent yet, or null. */
  function panelsProblem(boxes, mapping) {
    if (!boxes || !boxes.length) return "Draw at least one box.";
    if (boxes.length > 8) return "At most 8 boxes.";
    const names = boxes.map((b) => b.name.trim());
    if (names.some((n) => !n)) return "Every box needs a name.";
    if (names.some((n) => n.length > 40)) return "Box names are at most 40 characters.";
    if (new Set(names).size !== names.length) return "Two boxes have the same name.";
    for (const [label, name] of Object.entries(mapping || {})) {
      if (name && !names.includes(name)) return `${label} is mapped to a box that no longer exists.`;
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

  const api = {
    TABS, INPUT_FILES, fmt, inputNotice, isReels, latestBy, rowsOfKind, clipsOpen, speakersOpen, firstOpenTab,
    oneAtATime,
    SCORE_KEYS, totalScore, sortCandidates, timelineWindow, toFrac, fromFrac, snapTargets, snapEdge, stepEdge,
    itemAt, pickFits, clipDraft, unsentPicks,
    normRect, clampRect, moveRect, resizeRect, roundRect, nextBoxName, panelsProblem, panelsRow,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JobLogic = api;
})(typeof window !== "undefined" ? window : this);
