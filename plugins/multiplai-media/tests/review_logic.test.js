// Tests for the job page's pure functions (skills/video-edit/scripts/static/logic.js).
// Run: node --test tests/review_logic.test.js   (tests/test_review_logic_js.py runs it under pytest)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "..", "skills", "video-edit", "scripts", "static", "logic.js"));

const JOB = { status: "ok", data: { kind: "reels" } };
const DEMO = { status: "ok", data: { kind: "demo" } };
const CANDS = { status: "ok", data: [{ id: "c1", start: 10, end: 40 }, { id: "c2", start: 100, end: 140 }] };
const pick = (candidate, start, end, extra = {}) => ({ kind: "pick", candidate, decision: "accept", start, end, ...extra });

test("fmt shows minutes, seconds and tenths", () => {
  assert.equal(L.fmt(75.25), "1:15.3");
  assert.equal(L.fmt(0), "0:00.0");
  assert.equal(L.fmt(NaN), "0:00.0");
});

test("inputNotice: nothing for a valid file, a waiting line for a missing one, the error for an invalid one", () => {
  assert.equal(L.inputNotice("candidates", { status: "ok", data: [] }), null);
  const missing = L.inputNotice("candidates", { status: "missing" });
  assert.match(missing.text, /has not written candidates\.json yet/);
  assert.equal(missing.error, false);
  const bad = L.inputNotice("looks", { status: "invalid", error: "$.styles: expected array" });
  assert.equal(bad.error, true);
  assert.match(bad.text, /looks\.json has a problem[\s\S]*\$\.styles: expected array/);
  assert.match(L.inputNotice("job", null).text, /Loading job\.json/);
});

test("latestBy keeps the newest row per key", () => {
  const m = L.latestBy([{ candidate: "c1", decision: "accept" }, { candidate: "c2" }, { candidate: "c1", decision: "reject" }], "candidate");
  assert.deepEqual([...m.keys()], ["c1", "c2"]);
  assert.equal(m.get("c1").decision, "reject");
});

test("rowsOfKind treats a row without a kind as a point", () => {
  const rows = [{ text: "old" }, { kind: "pick" }, { kind: "point", text: "new" }];
  assert.deepEqual(L.rowsOfKind(rows, "point").map((r) => r.text), ["old", "new"]);
});

test("clipsOpen until every candidate has a pick row", () => {
  assert.equal(L.clipsOpen({ job: JOB, candidates: CANDS, rows: [] }), true);
  assert.equal(L.clipsOpen({ job: JOB, candidates: CANDS, rows: [pick("c1", 10, 40)] }), true);
  assert.equal(L.clipsOpen({ job: JOB, candidates: CANDS, rows: [pick("c1", 9.5, 41), pick("c2", 100, 140)] }), false);
  // c2's pick was for another span: candidates.json reused the id, so c2 is open again.
  assert.equal(L.clipsOpen({ job: JOB, candidates: CANDS, rows: [pick("c1", 10, 40), pick("c2", 500, 540)] }), true);
  assert.equal(L.clipsOpen({ job: JOB, candidates: { status: "missing" }, rows: [] }), false);
  assert.equal(L.clipsOpen({ job: DEMO, candidates: CANDS, rows: [] }), false);
});

test("speakersOpen until a panels row is sent, and never for a demo", () => {
  assert.equal(L.speakersOpen({ job: JOB, rows: [] }), true);
  assert.equal(L.speakersOpen({ job: JOB, rows: [{ kind: "panels" }] }), false);
  assert.equal(L.speakersOpen({ job: DEMO, rows: [] }), false);
  assert.equal(L.speakersOpen({ job: { status: "missing" }, rows: [] }), false);
});

test("firstOpenTab is the earliest open tab, else review", () => {
  assert.equal(L.firstOpenTab({ clips: true, speakers: true }), "clips");
  assert.equal(L.firstOpenTab({ clips: false, speakers: true, review: true }), "speakers");
  assert.equal(L.firstOpenTab({ music: true, review: true }), "music");
  assert.equal(L.firstOpenTab({}), "review");
});

// --- Clips ------------------------------------------------------------------------------

const SENTENCES = [
  { start: 0.6, end: 2.0 }, { start: 2.45, end: 4.14 }, { start: 4.94, end: 6.05 }, { start: 6.5, end: 8.48 },
];
const WORDS = [
  { text: "Small", start: 6.5, end: 6.74 }, { text: "releases", start: 6.79, end: 7.03 },
  { text: "changed", start: 7.08, end: 7.32 }, { text: "how", start: 7.37, end: 7.61 },
];

test("candidates sort by total score, ties in recording order", () => {
  const s = (id, start, v) => ({ id, start, scores: { hook: v, payoff: v, quotable: v, standalone: v, density: v } });
  const sorted = L.sortCandidates([s("a", 30, 3), s("b", 10, 5), s("c", 5, 3), s("d", 1, 1)]);
  assert.deepEqual(sorted.map((c) => c.id), ["b", "c", "a", "d"]);
  assert.equal(L.totalScore(s("x", 0, 4)), 20);
});

test("the timeline window pads the span and stays inside the recording", () => {
  assert.deepEqual(L.timelineWindow(20, 40), { start: 15, end: 45 });
  assert.deepEqual(L.timelineWindow(2, 6, 7), { start: 0, end: 7 });
  const win = { start: 10, end: 20 };
  assert.equal(L.toFrac(15, win), 0.5);
  assert.equal(L.toFrac(25, win), 1);
  assert.equal(L.fromFrac(0.25, win), 12.5);
});

test("a dropped start snaps to the nearest sentence start, an end to the nearest sentence end", () => {
  const span = { start: 0.6, end: 8.48 };
  assert.equal(L.snapEdge(2.3, "start", span, SENTENCES, WORDS, false), 2.45);
  assert.equal(L.snapEdge(5.0, "end", span, SENTENCES, WORDS, false), 4.14);
  assert.equal(L.snapEdge(5.8, "end", span, SENTENCES, WORDS, false), 6.05);
});

test("alt-drag snaps to a word instead", () => {
  const span = { start: 0.6, end: 8.48 };
  assert.equal(L.snapEdge(7.1, "start", span, SENTENCES, WORDS, true), 7.08);
  assert.equal(L.snapEdge(7.4, "end", span, SENTENCES, WORDS, true), 7.32);
});

test("snapping never makes the clip shorter than the minimum or flips its edges", () => {
  // The nearest sentence start (6.5) would leave 0.5 s before the end at 7: the next one back is taken.
  assert.equal(L.snapEdge(6.6, "start", { start: 0.6, end: 7.0 }, SENTENCES, WORDS, false), 4.94);
  // Nothing to snap to: the edge is only kept in order.
  assert.equal(L.snapEdge(9, "start", { start: 0, end: 8 }, [], [], false), 7);
  assert.equal(L.snapEdge(1, "end", { start: 3, end: 8 }, [], [], false), 4);
});

test("arrow keys step an edge to the previous or next snap target", () => {
  const span = { start: 2.45, end: 8.48 };
  assert.equal(L.stepEdge("start", span, 1, SENTENCES, WORDS, false), 4.94);
  assert.equal(L.stepEdge("start", span, -1, SENTENCES, WORDS, false), 0.6);
  assert.equal(L.stepEdge("end", span, -1, SENTENCES, WORDS, false), 6.05);
  assert.equal(L.stepEdge("end", span, 1, SENTENCES, WORDS, false), 8.48);   // nothing later: stays
});

test("itemAt finds the sentence under a time", () => {
  assert.equal(L.itemAt(3, SENTENCES), SENTENCES[1]);
  assert.equal(L.itemAt(2.2, SENTENCES), null);
});

test("unsentPicks skips undecided candidates and ones already sent as they are", () => {
  const decisions = {
    c1: { decision: "accept", start: 9.28, end: 15.6, note: " keep " },
    c2: { decision: "reject", start: 20.16, end: 30.53 },
    c3: { start: 0.6, end: 4.14 },
    c4: { decision: "accept", start: 31.333333, end: 37.65, note: "" },
  };
  const sent = new Map([["c2", { decision: "reject", start: 20.16, end: 30.53, text: "" }],
                        ["c4", { decision: "accept", start: 31.33, end: 37.65, text: "" }]]);
  assert.deepEqual(L.unsentPicks(decisions, sent), [
    { kind: "pick", candidate: "c1", decision: "accept", start: 9.28, end: 15.6, text: "keep" },
    { kind: "pick", candidate: "c4", decision: "accept", start: 31.333, end: 37.65, text: "" },
  ]);
});

// --- Speakers ---------------------------------------------------------------------------

test("rectangles: drawn from any corner, kept in the frame, moved and resized", () => {
  assert.deepEqual(L.normRect(100, 80, 20, 10), { x: 20, y: 10, w: 80, h: 70 });
  assert.deepEqual(L.clampRect({ x: -10, y: 700, w: 100, h: 100 }, 1280, 720), { x: 0, y: 620, w: 100, h: 100 });
  assert.deepEqual(L.clampRect({ x: 5, y: 5, w: 2, h: 2000 }, 1280, 720), { x: 5, y: 0, w: 8, h: 720 });
  assert.deepEqual(L.moveRect({ x: 1200, y: 0, w: 100, h: 50 }, 50, -10, 1280, 720), { x: 1180, y: 0, w: 100, h: 50 });
  const r = { x: 100, y: 100, w: 200, h: 100 };
  assert.deepEqual(L.resizeRect(r, "se", 400, 300, 1280, 720), { x: 100, y: 100, w: 300, h: 200 });
  assert.deepEqual(L.resizeRect(r, "nw", 50, 2000, 1280, 720), { x: 50, y: 200, w: 250, h: 520 });
  assert.deepEqual(L.roundRect({ x: 1.4, y: 2.6, w: 3.5, h: 4 }), { x: 1, y: 3, w: 4, h: 4 });
});

test("boxes are named A, B, … skipping names in use", () => {
  assert.equal(L.nextBoxName([]), "A");
  assert.equal(L.nextBoxName([{ name: "A" }, { name: "C" }]), "B");
});

test("panelsProblem explains what blocks sending", () => {
  const a = { name: "Host", x: 0, y: 0, w: 10, h: 10 }, b = { name: "Guest", x: 10, y: 0, w: 10, h: 10 };
  assert.equal(L.panelsProblem([], {}), "Draw at least one box.");
  assert.equal(L.panelsProblem([a, { ...b, name: " " }], {}), "Every box needs a name.");
  assert.equal(L.panelsProblem([a, { ...b, name: "Host" }], {}), "Two boxes have the same name.");
  assert.equal(L.panelsProblem([a, b], { SPEAKER_0: "Gone" }), "SPEAKER_0 is mapped to a box that no longer exists.");
  assert.equal(L.panelsProblem([a, b], { SPEAKER_0: "Host", SPEAKER_1: "" }), null);
});

test("panelsProblem: every transcript speaker label needs a box", () => {
  const a = { name: "Host", x: 0, y: 0, w: 10, h: 10 }, b = { name: "Guest", x: 10, y: 0, w: 10, h: 10 };
  const labels = ["SPEAKER_0", "SPEAKER_1", "SPEAKER_2"];
  assert.match(L.panelsProblem([a, b], { SPEAKER_0: "Host", SPEAKER_1: "Guest" }, labels),
    /^Pick a box for SPEAKER_2: /);
  assert.match(L.panelsProblem([a, b], { SPEAKER_0: "Host", SPEAKER_1: "" }, labels), /SPEAKER_1, SPEAKER_2/);
  assert.equal(L.panelsProblem([a, b], { SPEAKER_0: "Host", SPEAKER_1: "Guest", SPEAKER_2: "Guest" }, labels), null);
  assert.equal(L.panelsProblem([a], {}, []), null);    // no labels: boxes on their own
});

test("panelsRow sends whole source pixels and only the mapped labels", () => {
  const row = L.panelsRow([{ name: " Host ", x: 0.4, y: 0, w: 640.2, h: 720 }], { SPEAKER_0: "Host", SPEAKER_1: "" }, " left ");
  assert.deepEqual(row, { kind: "panels", panels: { Host: { x: 0, y: 0, w: 640, h: 720 } },
                          speakers: { SPEAKER_0: "Host" }, text: "left" });
});

// --- one poll at a time ---------------------------------------------------------------

/* A run that stays in flight until release() is called; `log` records starts and ends. */
function gatedRun() {
  const log = [], gates = [];
  let n = 0;
  const run = () => {
    const i = ++n;
    log.push(`start ${i}`);
    return new Promise((resolve) => gates.push(() => { log.push(`end ${i}`); resolve(i); }));
  };
  const release = async () => { await new Promise((r) => setImmediate(r)); gates.shift()(); await new Promise((r) => setImmediate(r)); };
  return { run, log, release };
}

test("oneAtATime: a call while idle runs at once", async () => {
  const g = gatedRun();
  const call = L.oneAtATime(g.run);
  const p = call();
  await g.release();
  assert.equal(await p, 1);
  assert.deepEqual(g.log, ["start 1", "end 1"]);
});

test("oneAtATime: a call during a run waits for one more run that starts after it", async () => {
  const g = gatedRun();
  const call = L.oneAtATime(g.run);
  const first = call();
  let secondDone = false;
  const second = call().then((v) => { secondDone = true; return v; });
  await g.release();                               // the first run ends
  assert.equal(await first, 1);
  assert.equal(secondDone, false, "the queued call must not settle when the run in flight ends");
  assert.deepEqual(g.log, ["start 1", "end 1", "start 2"]);
  await g.release();
  assert.equal(await second, 2);
  assert.deepEqual(g.log, ["start 1", "end 1", "start 2", "end 2"]);
});

test("oneAtATime: many calls during a run share one queued run", async () => {
  const g = gatedRun();
  const call = L.oneAtATime(g.run);
  call();
  const queued = [call(), call(), call()];
  await g.release();
  await g.release();
  assert.deepEqual(await Promise.all(queued), [2, 2, 2]);
  assert.deepEqual(g.log, ["start 1", "end 1", "start 2", "end 2"]);
});

test("oneAtATime: a failed run still lets the queued run go", async () => {
  let n = 0;
  const call = L.oneAtATime(async () => { n += 1; if (n === 1) throw new Error("down"); return n; });
  const first = call();
  const second = call();
  await assert.rejects(first, /down/);
  assert.equal(await second, 2);
});

// --- clip drafts --------------------------------------------------------------------------

test("pickFits: a pick belongs to a candidate while their spans overlap", () => {
  const c = { id: "c3", start: 100, end: 140 };
  assert.equal(L.pickFits(pick("c3", 99, 141), c), true);       // snapped edges
  assert.equal(L.pickFits(pick("c3", 500, 540), c), false);     // the id now names another span
  assert.equal(L.pickFits(pick("c3", 140, 150), c), false);     // touching is not overlapping
  assert.equal(L.pickFits(undefined, c), false);
});

test("clipDraft keeps a draft made for the candidate's current span", () => {
  const c = { id: "c3", start: 100, end: 140 };
  const draft = { decision: "accept", start: 102, end: 138, note: "tight", for: [100, 140] };
  assert.equal(L.clipDraft(c, draft, undefined), draft);
});

test("clipDraft drops a draft made for another span, so its old edges are never sent", () => {
  const moved = { id: "c3", start: 500, end: 540 };
  const old = { decision: "accept", start: 100, end: 140, note: "", for: [100, 140] };
  assert.deepEqual(L.clipDraft(moved, old, undefined),
    { decision: null, start: 500, end: 540, note: "", for: [500, 540] });
  // ...and a draft from before drafts recorded their span counts as another span.
  assert.equal(L.clipDraft(moved, { decision: "reject", start: 500, end: 540, note: "" }, undefined).decision, null);
  // Nothing to send for it until the user decides again.
  const decisions = { c3: L.clipDraft(moved, old, undefined) };
  assert.deepEqual(L.unsentPicks(decisions, new Map()), []);
});

test("clipDraft starts from the newest sent pick only while it fits the candidate", () => {
  const c = { id: "c3", start: 100, end: 140 };
  assert.deepEqual(L.clipDraft(c, null, pick("c3", 101, 139, { text: "keep" })),
    { decision: "accept", start: 101, end: 139, note: "keep", for: [100, 140] });
  assert.equal(L.clipDraft({ ...c, start: 500, end: 540 }, null, pick("c3", 101, 139)).decision, null);
});

// --- Look ------------------------------------------------------------------------------

const LOOKS = { status: "ok", file: "/job/looks.json", data: {
  styles: [{ name: "clean-tech" }, { name: "flat-vector" }],
  brands: [{ name: "house" }],
  captions: [{ name: "Bold", values: { size: 84, position_y: 0.66, words_per_line: 2, highlight: true } },
             { name: "Classic", values: { words_per_line: 5, highlight: false } }],
} };
const lookRowSent = (extra = {}) => ({ kind: "look", style: "flat-vector", brand: null,
  captions: { words_per_line: 5, highlight: false }, text: "", ...extra });

test("lookOpen: open while looks.json offers something and no look row was sent", () => {
  assert.equal(L.lookOpen({ looks: LOOKS, rows: [] }), true);
  assert.equal(L.lookOpen({ looks: LOOKS, rows: [lookRowSent()] }), false);
  assert.equal(L.lookOpen({ looks: { status: "ok", data: { styles: [] } }, rows: [] }), false);
  assert.equal(L.lookOpen({ looks: { status: "missing" }, rows: [] }), false);
  assert.equal(L.lookOpen({ looks: null, rows: [] }), false);
});

test("the first open tab follows Look and Music after Clips and Speakers", () => {
  assert.equal(L.firstOpenTab({ clips: false, speakers: false, look: true, music: true }), "look");
  assert.equal(L.firstOpenTab({ look: false, music: true }), "music");
});

test("lookDraft starts from the first of each list, then the newest sent row, and keeps a valid draft", () => {
  assert.deepEqual(L.lookDraft(LOOKS, null, null), { style: "clean-tech", brand: "house", caption: "Bold", note: "" });
  // the sent row's caption values find their option by value, whatever the key order
  assert.deepEqual(L.lookDraft(LOOKS, null, lookRowSent({ captions: { highlight: false, words_per_line: 5 }, text: "hi" })),
    { style: "flat-vector", brand: null, caption: "Classic", note: "hi" });
  const draft = { style: null, brand: "house", caption: "Classic", note: "x" };
  assert.deepEqual(L.lookDraft(LOOKS, draft, lookRowSent()), draft);
});

test("lookDraft drops a draft that names something no longer on offer", () => {
  const stale = { style: "neon", brand: "house", caption: "Bold", note: "" };
  assert.equal(L.lookDraft(LOOKS, stale, null).style, "clean-tech");
  assert.equal(L.lookDraft(LOOKS, stale, lookRowSent({ style: "gone" })).style, null);
  assert.equal(L.lookDraft(LOOKS, { style: undefined, brand: null, caption: null }, null).style, "clean-tech");
});

test("lookRow sends the caption option's values, and a look must pick something", () => {
  const row = L.lookRow({ style: "clean-tech", brand: null, caption: "Bold", note: " warm " }, LOOKS);
  assert.deepEqual(row, { kind: "look", style: "clean-tech", brand: null,
    captions: { size: 84, position_y: 0.66, words_per_line: 2, highlight: true }, text: "warm" });
  assert.equal(L.lookProblem(row), null);
  const none = L.lookRow({ style: null, brand: null, caption: null, note: "" }, LOOKS);
  assert.equal(none.captions, null);
  assert.match(L.lookProblem(none), /Pick a style, a brand or a caption option/);
});

test("lookProblem refuses a brand whose file cannot be used", () => {
  const brands = [{ name: "house", status: "invalid", error: "bad json" }, { name: "warm", status: "ok" }];
  const row = L.lookRow({ style: null, brand: "house", caption: null, note: "" }, LOOKS);
  assert.match(L.lookProblem(row, brands), /The house brand file cannot be used/);
  assert.equal(L.lookProblem({ ...row, brand: "warm" }, brands), null);
  assert.equal(L.lookProblem(row, []), null);           // brands not loaded yet: the server still checks the name
});

test("sameChoice: a row that repeats the last one sent is not sent again", () => {
  const keys = ["style", "brand", "captions"];
  const row = L.lookRow({ style: "flat-vector", brand: null, caption: "Classic", note: "" }, LOOKS);
  assert.equal(L.sameChoice(row, lookRowSent({ captions: { highlight: false, words_per_line: 5 } }), keys), true);
  assert.equal(L.sameChoice(row, lookRowSent({ text: "note" }), keys), false);
  assert.equal(L.sameChoice(row, lookRowSent({ brand: "house" }), keys), false);
  assert.equal(L.sameChoice(row, null, keys), false);
});

test("outputSize: 9:16 for reels and until job.json is known, 16:9 for a demo", () => {
  assert.deepEqual(L.outputSize(JOB), { width: 1080, height: 1920 });
  assert.deepEqual(L.outputSize(null), { width: 1080, height: 1920 });
  assert.deepEqual(L.outputSize(DEMO), { width: 1920, height: 1080 });
});

test("captionMetrics scales stages/captions.py's output-pixel sizes to the stage", () => {
  const out = { width: 1080, height: 1920 };
  const m = L.captionMetrics({ size: 84, position_y: 0.66 }, out, 960);
  assert.equal(m.fontPx, 42);
  assert.equal(m.centerY, 0.66);
  assert.equal(m.outlinePx, Math.round(84 * 0.07) / 2);
  const d = L.captionMetrics({}, out, 1920);                 // defaults: round(H * 0.036), 0.68
  assert.equal(d.fontPx, Math.round(1920 * 0.036));
  assert.equal(d.centerY, 0.68);
  assert.equal(L.captionMetrics({ size: 10 }, out, 1920).outlinePx, 2);   // never thinner than 2 px
});

test("captionLine takes words_per_line words within 22 characters and marks the spoken word", () => {
  const ws = ["We", "stopped", "fearing", "Fridays.", "Every", "change"].map((text) => ({ text }));
  assert.deepEqual(L.captionLine(ws, { words_per_line: 2 }), { words: ["We", "stopped"], spoken: 1 });
  assert.deepEqual(L.captionLine(ws, { words_per_line: 5 }).words, ["We", "stopped", "fearing"]);  // 22 chars
  assert.equal(L.captionLine(ws, { highlight: false }).spoken, -1);
  assert.deepEqual(L.captionLine([], {}), { words: [], spoken: -1 });
  assert.deepEqual(L.captionLine([{ text: "Supercalifragilisticexpialidocious" }], {}).words,
    ["Supercalifragilisticexpialidocious"]);                 // one long word still shows
});

test("brandColours fills in the defaults a render uses without a brand", () => {
  assert.deepEqual(L.brandColours(null), L.BRAND_DEFAULTS);
  assert.equal(L.brandColours({ status: "ok", colours: { ...L.BRAND_DEFAULTS, accent: "#3DDC97" } }).accent, "#3DDC97");
  assert.deepEqual(L.brandColours({ status: "invalid", error: "x" }), L.BRAND_DEFAULTS);
});

test("loopTime plays the sample, holds its last frame, and starts over", () => {
  assert.equal(L.loopTime(0.5, 2), 0.5);
  assert.equal(L.loopTime(2.5, 2), 2);                       // holding the last frame
  assert.equal(L.loopTime(3.25, 2), 0.25);                   // the next loop
  assert.equal(L.loopTime(4.5, 0), 4);                       // no duration: 4 s, then a 1 s hold
  assert.equal(L.loopTime(NaN, 2), 0);
});

test("fitScale fits a sample page into its card", () => {
  assert.equal(L.fitScale(1080, 1920, 270, 200), 200 / 1920);
  assert.equal(L.fitScale(900, 560, 225, 400), 0.25);
  assert.equal(L.fitScale(0, 10, 100, 100), 1);
});

test("readyMessage accepts only a well-formed ready message from a sample page", () => {
  assert.deepEqual(L.readyMessage({ type: "ready", width: 900, height: 560, duration: 2.1, seek: "default" }),
    { width: 900, height: 560, duration: 2.1 });
  assert.equal(L.readyMessage({ type: "ready", width: 900, height: 560, duration: 0 }).duration, 4);
  for (const bad of [null, "ready", { type: "seek", t: 1 }, { type: "ready", width: "900", height: 560, duration: 1 },
    { type: "ready", width: 900, height: 99999, duration: 1 }, { type: "ready", width: 900, height: 560, duration: NaN }]) {
    assert.equal(L.readyMessage(bad), null, JSON.stringify(bad));
  }
});

// --- Music -----------------------------------------------------------------------------

const MUSIC = { status: "ok", file: "/job/music.json", data: {
  beds: [{ name: "calm" }, { name: "bright" }], sfx_sets: [{ name: "soft" }], voice_sample: { start: 10, end: 18 },
} };

test("musicOpen: open while music.json offers something and no music row was sent", () => {
  assert.equal(L.musicOpen({ music: MUSIC, rows: [] }), true);
  assert.equal(L.musicOpen({ music: MUSIC, rows: [{ kind: "music", bed: "calm", volume_db: -10, sfx_set: null }] }), false);
  assert.equal(L.musicOpen({ music: { status: "ok", data: {} }, rows: [] }), false);
  assert.equal(L.musicOpen({ music: { status: "invalid", error: "x" }, rows: [] }), false);
});

test("musicDraft starts at the first bed, no sounds and the EDL's default level", () => {
  assert.deepEqual(L.musicDraft(MUSIC, null, null), { bed: "calm", sfx_set: null, volume_db: -10, note: "" });
  const last = { kind: "music", bed: "bright", volume_db: -18, sfx_set: "soft", text: "ok" };
  assert.deepEqual(L.musicDraft(MUSIC, null, last), { bed: "bright", sfx_set: "soft", volume_db: -18, note: "ok" });
  assert.equal(L.musicDraft(MUSIC, { bed: "jazz", sfx_set: null, volume_db: -5 }, null).bed, "calm");
  assert.equal(L.musicDraft(MUSIC, { bed: null, sfx_set: "soft", volume_db: -99 }, null).volume_db, -40);
});

test("musicRow rounds the level, and music must pick a bed or a sound set", () => {
  assert.deepEqual(L.musicRow({ bed: "calm", sfx_set: null, volume_db: -12.04, note: " " }),
    { kind: "music", bed: "calm", volume_db: -12, sfx_set: null, text: "" });
  assert.equal(L.musicProblem(L.musicRow({ bed: null, sfx_set: "soft", volume_db: -10 })), null);
  assert.match(L.musicProblem(L.musicRow({ bed: null, sfx_set: null, volume_db: -10 })), /Pick a music bed/);
});

test("dbToGain and fmtDb", () => {
  assert.equal(L.dbToGain(0), 1);
  assert.ok(Math.abs(L.dbToGain(-20) - 0.1) < 1e-12);
  assert.ok(Math.abs(L.dbToGain(-6) - 0.501) < 1e-3);
  assert.equal(L.dbToGain(NaN), 1);
  assert.equal(L.fmtDb(-10), "−10 dB");
  assert.equal(L.fmtDb(0), "0 dB");
});

// --- Review: drawing ---------------------------------------------------------------------

test("toPictureFrac gives fractions of the picture, clamped to it", () => {
  const rect = { left: 100, top: 50, width: 400, height: 200 };
  assert.deepEqual(L.toPictureFrac(300, 150, rect), [0.5, 0.5]);
  assert.deepEqual(L.toPictureFrac(50, 400, rect), [0, 1]);
  assert.deepEqual(L.toPictureFrac(1, 1, { left: 0, top: 0, width: 0, height: 0 }), [0, 0]);
});

test("simplifyStroke drops points too close to the last kept one and keeps the ends", () => {
  const pts = [[0, 0], [0.0005, 0], [0.001, 0], [0.01, 0], [0.0101, 0], [0.5, 0.5]];
  assert.deepEqual(L.simplifyStroke(pts), [[0, 0], [0.01, 0], [0.5, 0.5]]);
  assert.deepEqual(L.simplifyStroke([[0.123456, 0.5]]), [[0.1235, 0.5]]);
});

test("simplifyStroke thins a long stroke to the server's point limit", () => {
  const pts = Array.from({ length: 5000 }, (_, i) => [i / 5000, (i % 2) * 0.5]);
  const out = L.simplifyStroke(pts);
  assert.equal(out.length, L.MAX_PEN_POINTS);
  assert.deepEqual(out[0], [0, 0]);
  assert.deepEqual(out[out.length - 1], [0.9998, 0.5]);
});

test("finishShape keeps a pen dot but drops an arrow or box without a drag", () => {
  assert.deepEqual(L.finishShape("pen", "#FFD400", [[0.2, 0.2]]), { tool: "pen", color: "#FFD400", points: [[0.2, 0.2]] });
  assert.equal(L.finishShape("arrow", "#FFD400", [[0.2, 0.2], [0.201, 0.2]]), null);
  assert.equal(L.finishShape("box", "#FFD400", [[0.2, 0.2], [0.2, 0.2]]), null);
  assert.deepEqual(L.finishShape("box", "#FF3B30", [[0.1, 0.1], [0.3, 0.4]]).points, [[0.1, 0.1], [0.3, 0.4]]);
  assert.equal(L.finishShape("circle", "#FFD400", [[0.1, 0.1], [0.3, 0.4]]), null);
  assert.equal(L.finishShape("pen", "red", [[0.1, 0.1]]), null);
});

test("shapePaths draws a box outline, an arrow with a head, and a pen dot, in pixels", () => {
  const box = L.shapePaths({ tool: "box", color: "#FFD400", points: [[0.1, 0.2], [0.5, 0.6]] }, 1000, 500);
  assert.deepEqual(box.paths, [[[100, 100], [500, 100], [500, 300], [100, 300], [100, 100]]]);
  assert.equal(box.width, 5);
  const arrow = L.shapePaths({ tool: "arrow", color: "#FFD400", points: [[0, 0.5], [0.5, 0.5]] }, 1000, 500);
  assert.equal(arrow.paths.length, 2);
  assert.deepEqual(arrow.paths[0], [[0, 250], [500, 250]]);
  const [h1, tip, h2] = arrow.paths[1];
  assert.deepEqual(tip, [500, 250]);
  assert.ok(h1[0] < 500 && h2[0] < 500);                                 // both barbs point back,
  assert.ok(Math.min(h1[1], h2[1]) < 250 && Math.max(h1[1], h2[1]) > 250);  // one on each side
  const dot = L.shapePaths({ tool: "pen", color: "#FFD400", points: [[0.5, 0.5]] }, 200, 100);
  assert.deepEqual(dot.paths, [[[100, 50], [100, 50]]]);
  assert.equal(dot.width, 2);                                            // never thinner than 2 px
});

test("exportSize keeps the native size and only scales down a frame larger than 4K", () => {
  assert.deepEqual(L.exportSize(1080, 1920), { width: 1080, height: 1920 });
  assert.deepEqual(L.exportSize(7680, 4320), { width: 3840, height: 2160 });
  assert.equal(L.exportSize(0, 0), null);
});

test("drawingRow is the server's drawing row", () => {
  const shapes = [{ tool: "pen", color: "#FFD400", points: [[0.1, 0.1], [0.2, 0.2]] }];
  const row = L.drawingRow({ key: "k", kind: "drawing", video: "clip-01", version: 2, t: 3.14159, image: "a".repeat(32),
                             shapes, text: "  the logo  ", thumb: "data:image/jpeg;base64,xx" });
  assert.deepEqual(row, { kind: "drawing", video: "clip-01", version: 2, t: 3.142, image: "a".repeat(32), shapes, text: "the logo" });
});

// --- Review: the timeline ----------------------------------------------------------------

const SIDE = {
  duration: 8.97,
  edl: {
    headline: { text: "Ship small", start: 0, end: 2.5 },
    overlays: [{ html: "/jobs/x/looks/clean-tech.html", start: 1, end: 3, mode: "alpha", style: "clean-tech" },
               { html: "C:\\jobs\\x\\card.html", start: 4, end: 6 }],
    sfx: [{ at: 1, sound: "/jobs/x/sfx/pop.wav", volume_db: -14 }],
  },
  segments: [
    { index: 0, src_start: 20.16, src_end: 25.345, speed: 1, out_start: 0, out_end: 5.185, muted: false, xfade_in: 0 },
    { index: 1, src_start: 26.345, src_end: 30.53, speed: 1, out_start: 4.785, out_end: 8.97, muted: false, xfade_in: 0.4 },
  ],
  captions_events: [{ start: 0, end: 0.74, text: "But our deploys", words: [] }, { start: 0.79, end: 1.32, text: "doubled in", words: [] }],
};

test("timelineLanes has a lane per kind of block, each block naming its range target", () => {
  const tl = L.timelineLanes(SIDE);
  assert.equal(tl.duration, 8.97);
  assert.deepEqual(tl.lanes.map((l) => l.id), ["segments", "captions", "headline", "overlays", "sfx"]);
  const by = Object.fromEntries(tl.lanes.map((l) => [l.id, l.blocks]));
  assert.deepEqual(by.segments.map((b) => [b.start, b.end, b.xfade, b.target]), [[0, 5.185, 0, "segment:0"], [4.785, 8.97, 0.4, "segment:1"]]);
  assert.match(by.segments[1].title, /source 0:26\.3–0:30\.5, 0\.4 s crossfade in/);
  assert.deepEqual(by.captions.map((b) => [b.label, b.target]), [["But our deploys", "captions"], ["doubled in", "captions"]]);
  assert.deepEqual(by.headline.map((b) => [b.start, b.end, b.target]), [[0, 2.5, "headline"]]);
  assert.deepEqual(by.overlays.map((b) => [b.label, b.target]), [["clean-tech.html · clean-tech", "overlay:0"], ["card.html", "overlay:1"]]);
  assert.match(by.overlays[1].title, /\(keyed\)/);                           // an overlay without a mode is keyed
  assert.deepEqual(by.sfx.map((b) => [b.start, b.end, b.tick, b.label, b.target]), [[1, 1, true, "pop.wav", "sfx:0"]]);
});

test("the timeline is as long as the render, even when the last caption is held past its end", () => {
  const side = { ...SIDE, captions_events: [...SIDE.captions_events, { start: 8.5, end: 9.57, text: "impossible.", words: [] }] };
  const tl = L.timelineLanes(side);
  assert.equal(tl.duration, 8.97);
  assert.equal(L.timeAtFrac(0.5, tl.duration), 4.485);
  const last = tl.lanes[1].blocks[2];
  assert.equal(L.blockBox(last, tl.duration).left + L.blockBox(last, tl.duration).width, 100);   // cut at the edge
  assert.equal(L.timelineLanes({ ...side, duration: undefined }).duration, 9.57);                  // no duration: the blocks'
});

test("timelineLanes puts a title card first and survives a sidecar with nothing in it", () => {
  const tl = L.timelineLanes({ duration: 5, edl: { title: { line1: "Hello", line2: "World", duration: 2 } }, segments: [] });
  assert.deepEqual(tl.lanes[0].blocks.map((b) => [b.start, b.end, b.target, b.title]), [[0, 2, "title", "Hello / World"]]);
  const empty = L.timelineLanes({ edl: {} });
  assert.equal(empty.duration, 0);
  assert.ok(empty.lanes.every((l) => l.blocks.length === 0));
});

test("blockBox places a block in percent and clamps it to the timeline", () => {
  assert.deepEqual(L.blockBox({ start: 2, end: 4 }, 10), { left: 20, width: 20 });
  assert.deepEqual(L.blockBox({ start: 8, end: 14 }, 10), { left: 80, width: 20 });
  assert.deepEqual(L.blockBox({ start: 1, end: 2 }, 0), { left: 0, width: 0 });
});

test("rangeFromDrag orders the ends, keeps them in the render and ignores a click", () => {
  assert.deepEqual(L.rangeFromDrag(5.2, 1.04449, 8.97), { t_start: 1.044, t_end: 5.2 });
  assert.deepEqual(L.rangeFromDrag(-1, 20, 8.97), { t_start: 0, t_end: 8.97 });
  assert.equal(L.rangeFromDrag(3, 3.05, 8.97), null);
  assert.equal(L.timeAtFrac(0.5, 8.97), 4.485);
  assert.equal(L.timeAtFrac(1.5, 8.97), 8.97);
});

test("blockRange is the block, or a short stretch around a sound tick", () => {
  assert.deepEqual(L.blockRange({ start: 1, end: 3 }, 8.97), { t_start: 1, t_end: 3 });
  assert.deepEqual(L.blockRange({ start: 1, end: 1, tick: true }, 8.97), { t_start: 0.75, t_end: 1.25 });
  assert.deepEqual(L.blockRange({ start: 0.1, end: 0.1, tick: true }, 8.97), { t_start: 0, t_end: 0.35 });
});

test("rangeRow is the server's range row; no target is null", () => {
  const d = { key: "k", kind: "range", video: "clip-01", version: 1, t: 1, t_start: 1.00049, t_end: 3, target: "overlay:0", text: " too slow " };
  assert.deepEqual(L.rangeRow(d), { kind: "range", video: "clip-01", version: 1, t_start: 1, t_end: 3, target: "overlay:0", text: "too slow" });
  assert.equal(L.rangeRow({ ...d, target: undefined }).target, null);
  assert.equal(L.targetLabel("overlay:2"), "overlay 2");
  assert.equal(L.targetLabel(null), "");
});
