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

  /* Clips is open while a valid candidate has no pick row yet. */
  function clipsOpen(s) {
    if (!isReels(s.job) || !s.candidates || s.candidates.status !== "ok") return false;
    const picked = latestBy(rowsOfKind(s.rows, "pick"), "candidate");
    return s.candidates.data.some((c) => !picked.has(c.id));
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

  const api = {
    TABS, INPUT_FILES, fmt, inputNotice, isReels, latestBy, rowsOfKind, clipsOpen, speakersOpen, firstOpenTab,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JobLogic = api;
})(typeof window !== "undefined" ? window : this);
