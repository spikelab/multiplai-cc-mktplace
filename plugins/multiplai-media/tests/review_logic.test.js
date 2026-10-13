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

// --- Review: sending ---------------------------------------------------------------------

test("finishShape thins a pen stroke to the room left in its drawing, and refuses one with no room", () => {
  const pts = Array.from({ length: 500 }, (_, i) => [i / 500, 0.5]);
  assert.equal(L.finishShape("pen", "#FFD400", pts, 50).points.length, 50);
  assert.equal(L.finishShape("pen", "#FFD400", pts, 1), null);
  assert.equal(L.finishShape("box", "#FFD400", [[0.1, 0.1], [0.3, 0.4]], null), null);
});

test("drawingRoom: the points one more shape may have, null when the drawing is full", () => {
  const pen = (n) => ({ tool: "pen", color: "#FFD400", points: Array.from({ length: n }, () => [0.1, 0.1]) });
  assert.equal(L.drawingRoom([]), L.MAX_PEN_POINTS);
  assert.equal(L.drawingRoom([pen(2000), pen(2000), pen(1500)]), 500);
  assert.equal(L.drawingRoom([pen(2000), pen(2000), pen(1999)]), null);
  assert.equal(L.drawingRoom(Array.from({ length: L.MAX_SHAPES }, () => pen(1))), null);
});

const POINT = { key: "p", video: "clip-01", version: 1, t: 1.5, x: 0.2, y: 0.3, text: " too dark " };
const DRAWING = { key: "d", kind: "drawing", video: "clip-01", version: 1, t: 2, image: "a".repeat(32),
                  shapes: [{ tool: "box", color: "#FFD400", points: [[0.1, 0.1], [0.3, 0.4]] }], text: "" };
const RANGE = { key: "r", kind: "range", video: "clip-01", version: 1, t: 3, t_start: 3, t_end: 5, target: "overlay:0", text: "late" };

test("reviewRow sends each pending comment as its own kind of row", () => {
  assert.deepEqual(L.reviewRow(POINT), { kind: "point", video: "clip-01", version: 1, t: 1.5, x: 0.2, y: 0.3, text: "too dark" });
  assert.equal(L.reviewRow(DRAWING).kind, "drawing");
  assert.deepEqual(L.reviewRow(DRAWING), L.drawingRow(DRAWING));
  assert.deepEqual(L.reviewRow(RANGE), L.rangeRow(RANGE));
  assert.ok(!("x" in L.reviewRow(RANGE)));
});

test("canSendReview: a drawing may go without text, a point or a range may not", () => {
  assert.equal(L.canSendReview([]), false);
  assert.equal(L.canSendReview([DRAWING]), true);
  assert.equal(L.canSendReview([DRAWING, RANGE, POINT]), true);
  assert.equal(L.canSendReview([DRAWING, { ...RANGE, text: "  " }]), false);
  assert.equal(L.canSendReview([{ ...POINT, text: "" }]), false);
});

test("reviewSent lists this clip's point, drawing and range rows, and nothing else", () => {
  const rows = [{ video: "clip-01", t: 1 }, { ...DRAWING }, { ...RANGE }, { ...RANGE, video: "clip-02" },
                { kind: "pick", candidate: "c1" }, { kind: "look", video: "clip-01" }];
  assert.deepEqual(L.reviewSent(rows, "clip-01").map((r) => r.kind || "point"), ["point", "drawing", "range"]);
  assert.deepEqual(L.reviewSent(undefined, "clip-01"), []);
});

test("sendBatches keeps every body under the limit, in order, and a too-big row alone", () => {
  const big = (n) => ({ kind: "point", text: "x".repeat(n) });
  const rows = [big(100), big(100), big(100), big(500), big(10)];
  const bytes = (idx) => Buffer.byteLength(JSON.stringify({ comments: idx.map((i) => rows[i]) }));
  const batches = L.sendBatches(rows, 400);                              // a 100-char row is 126 bytes
  assert.deepEqual(batches, [[0, 1, 2], [3], [4]]);
  for (const b of batches.filter((b) => !b.includes(3))) assert.ok(bytes(b) <= 400, String(bytes(b)));
  assert.deepEqual(L.sendBatches([]), []);
  assert.deepEqual(L.sendBatches(rows), [[0, 1, 2, 3, 4]]);                // the server's limit
  // a body exactly at the limit is one batch
  assert.deepEqual(L.sendBatches(rows.slice(0, 2), bytes([0, 1])), [[0, 1]]);
  assert.deepEqual(L.sendBatches(rows.slice(0, 2), bytes([0, 1]) - 1), [[0], [1]]);
});

test("sendBatches counts UTF-8 bytes, as the server does", () => {
  const rows = [{ text: "é".repeat(100) }, { text: "é".repeat(100) }];
  const one = Buffer.byteLength(JSON.stringify({ comments: [rows[0]] }));
  assert.deepEqual(L.sendBatches(rows, one + 120), [[0], [1]]);           // 100 chars, but 200 bytes
});

// --- Review: the style panel ---------------------------------------------------------------------

const STYLE_INFO = {
  status: "ok", video: "clip-01", version: 1, output: { width: 720, height: 1280 },
  captions: { words_per_line: 3, max_chars: 22, position_y: 0.68, highlight: true, size: null },
  brand: { file: "/job/brands/warm.json", status: "ok",
           colours: { primary: "#fff8ec", accent: "#FF6B4A", background: "#1F2A44", caption_outline: "#1F2A44" } },
  styles: [
    { name: "clean-tech", status: "ok", file: "/x/clean-tech.md",
      values: { "palette.accent": "#3DDC97", "fonts.display": "Inter, sans-serif", "fonts.weights.display": 800,
                "shape.radius_px": 18, "motion.enter_s": 0.5 } },
    { name: "/job/gone.md", status: "missing", error: "no style file" },
  ],
};

test("styleGroups: captions (default size from the output height), brand, then each style", () => {
  const groups = L.styleGroups(STYLE_INFO);
  assert.deepEqual(groups.map((g) => g.target), ["captions", "brand", "style:clean-tech", "style:/job/gone.md"]);
  const caps = groups[0];
  assert.equal(caps.saveAs, false);
  assert.deepEqual(caps.fields.map((f) => [f.key, f.value]),
    [["size", 46], ["position_y", 0.68], ["words_per_line", 3], ["max_chars", 22], ["highlight", true]]);
  assert.equal(groups[1].title, "Brand · warm.json");
  assert.equal(groups[1].fields[0].value, "#FFF8EC");                              // colours shown upper case
  const style = groups[2];
  assert.deepEqual(style.fields.map((f) => f.type), ["colour", "text", "int", "number", "number"]);
  assert.equal(style.fields[0].label, "accent colour");
  assert.equal(groups[3].title, "Style · gone.md");
  assert.equal(groups[3].error, "no style file");
  assert.deepEqual(groups[3].fields, []);
});

test("styleGroups: no captions group without captions; a brand it cannot read keeps its error", () => {
  const groups = L.styleGroups({ ...STYLE_INFO, captions: null, brand: { file: null, status: "invalid", error: "bad" }, styles: [] });
  assert.deepEqual(groups.map((g) => g.target), ["brand"]);
  assert.equal(groups[0].title, "Brand · none (the defaults)");
  assert.equal(groups[0].error, "bad");
  assert.deepEqual(L.styleGroups(null), []);
  assert.deepEqual(L.styleGroups({ status: "error" }), []);
});

test("checkField: bounds, whole numbers, colours and CSS text, as the server checks them", () => {
  const [size, posY, , , highlight] = L.CAPTION_FIELDS;
  assert.deepEqual(L.checkField(size, "60"), { value: 60 });
  assert.match(L.checkField(size, "60.5").error, /whole number/);
  assert.match(L.checkField(size, "4").error, /within 8–400/);
  assert.match(L.checkField(size, "").error, /a number/);
  assert.deepEqual(L.checkField(posY, "0.7"), { value: 0.7 });
  assert.deepEqual(L.checkField(highlight, 0), { value: false });
  const colour = L.styleField("palette.accent", "#3DDC97");
  assert.deepEqual(L.checkField(colour, "#ff0000"), { value: "#FF0000" });
  assert.match(L.checkField(colour, "red").error, /#RRGGBB/);
  const font = L.styleField("fonts.body", "Inter");
  assert.deepEqual(L.checkField(font, "  Georgia, serif "), { value: "Georgia, serif" });
  for (const bad of ["x; color: red", "a { b }", "x /* y */", "Evil, url(http://x/f.woff)", "a\\b", ""]) {
    assert.ok(L.checkField(font, bad).error, bad);
  }
  assert.equal(L.styleField("fonts.weights.body", 500).step, 100);
  assert.deepEqual([L.styleField("motion.stagger_s", 0.1).min, L.styleField("motion.stagger_s", 0.1).max], [0, 10]);
});

test("styleRows: one row per changed group, only the changed keys, [old, new]", () => {
  const groups = L.styleGroups(STYLE_INFO);
  const edits = {
    captions: { size: "60", position_y: "0.68", highlight: false },                  // position_y unchanged
    brand: { accent: "#ff6b4a" },                                                     // same colour, other case
    "style:clean-tech": { "palette.accent": "#FF0000", "shape.radius_px": "4" },
  };
  const { rows, problem } = L.styleRows("clip-01", 1, groups, edits, { "style:clean-tech": "clean-red", captions: "nope" }, " note ");
  assert.equal(problem, null);
  assert.deepEqual(rows, [
    { kind: "style", video: "clip-01", version: 1, target: "captions",
      changes: { size: [46, 60], highlight: [true, false] }, text: "note" },
    { kind: "style", video: "clip-01", version: 1, target: "style:clean-tech",
      changes: { "palette.accent": ["#3DDC97", "#FF0000"], "shape.radius_px": [18, 4] }, text: "note", save_as: "clean-red" },
  ]);
});

test("styleRows: nothing changed, a bad value or a bad file name is a problem", () => {
  const groups = L.styleGroups(STYLE_INFO);
  assert.deepEqual(L.styleRows("clip-01", 1, groups, {}, {}, ""), { rows: [], problem: "Change a value to send it." });
  assert.match(L.styleRows("clip-01", 1, groups, { captions: { size: "2" } }, {}, "").problem, /within/);
  const bad = L.styleRows("clip-01", 1, groups, { brand: { accent: "#000000" } }, { brand: "../evil" }, "");
  assert.match(bad.problem, /Save as/);
});

test("editedValues and groupEdited: valid edits apply, invalid ones keep the old value", () => {
  const caps = L.styleGroups(STYLE_INFO)[0];
  const v = L.editedValues(caps, { size: "80", words_per_line: "abc" });
  assert.equal(v.size, 80);
  assert.equal(v.words_per_line, 3);
  assert.equal(L.groupEdited(caps, { size: "80" }), true);
  assert.equal(L.groupEdited(caps, { size: "46" }), false);
  assert.equal(L.groupEdited(undefined, { size: "80" }), false);
});

const W = (text, start, end) => ({ text, start, end });
const EVENTS = [
  { start: 0.92, end: 2.0, text: "one two three", words: [W("one", 1.0, 1.2), W("two", 1.3, 1.5), W("three", 1.6, 1.8)] },
  { start: 2.0, end: 3.2, text: "four five.", words: [W("four", 2.1, 2.3), W("five.", 2.4, 2.6)] },
];
const ORIGINAL = { words_per_line: 3, max_chars: 22, highlight: true };

test("captionAt keeps the render's own lines while the line breaks are unchanged", () => {
  assert.deepEqual(L.captionAt(EVENTS, 1.35, { ...ORIGINAL, size: 80 }, ORIGINAL), { words: ["one", "two", "three"], spoken: 1 });
  assert.deepEqual(L.captionAt(EVENTS, 0.95, ORIGINAL, ORIGINAL), { words: ["one", "two", "three"], spoken: 0 });
  assert.deepEqual(L.captionAt(EVENTS, 2.5, { ...ORIGINAL, highlight: false }, ORIGINAL), { words: ["four", "five."], spoken: -1 });
  assert.equal(L.captionAt(EVENTS, 0.5, ORIGINAL, ORIGINAL), null);
  assert.equal(L.captionAt([], 1, ORIGINAL, ORIGINAL), null);
});

test("captionAt re-breaks the words when words_per_line or max_chars change", () => {
  const two = { ...ORIGINAL, words_per_line: 2 };
  assert.deepEqual(L.captionAt(EVENTS, 1.1, two, ORIGINAL).words, ["one", "two"]);
  assert.deepEqual(L.captionAt(EVENTS, 1.7, two, ORIGINAL).words, ["three"]);         // the pause after it ends the line
  assert.deepEqual(L.captionAt(EVENTS, 2.2, two, ORIGINAL).words, ["four", "five."]);
  // a sentence end always ends a line
  const run = [W("a", 0, 0.2), W("b.", 0.25, 0.4), W("c", 0.45, 0.6), W("d", 0.65, 0.8)];
  assert.deepEqual(L.regroupWords(run, { words_per_line: 10, max_chars: 80 }).map((l) => l.words.length), [2, 2]);
});

test("regroupWords: max_chars, pauses and the timing rules of stages/captions.py", () => {
  const words = [W("alpha", 1.0, 1.2), W("beta", 1.25, 1.4), W("gamma", 2.0, 2.2), W("delta", 2.25, 2.4)];
  const lines = L.regroupWords(words, { words_per_line: 5, max_chars: 22 });
  assert.deepEqual(lines.map((l) => l.words.map((w) => w.text)), [["alpha", "beta"], ["gamma", "delta"]]);   // 0.6 s pause
  assert.ok(Math.abs(lines[0].start - 0.92) < 1e-9);                       // LEAD_S before the first word
  assert.ok(Math.abs(lines[0].end - 1.87) < 1e-9);                         // CLEAR_S before the next line's lead
  assert.ok(Math.abs(lines[1].end - 3.0) < 1e-9);                          // HOLD_S after the last word
  const narrow = L.regroupWords(words.slice(0, 2), { words_per_line: 5, max_chars: 8 });
  assert.deepEqual(narrow.map((l) => l.words.length), [1, 1]);             // "alpha beta" is 10 characters
});

test("changeSummary and styleSent", () => {
  assert.equal(L.changeSummary({ size: [null, 60], highlight: [true, false] }), "size — → 60, highlight true → false");
  const rows = [{ kind: "style", video: "clip-01" }, { kind: "point", video: "clip-01" }, { kind: "style", video: "clip-02" }];
  assert.deepEqual(L.styleSent(rows, "clip-01"), [rows[0]]);
});

test("the style panel's CSS value check matches the server's", () => {
  assert.equal(L.isCssValue("Georgia, 'Times New Roman', serif"), true);
  for (const bad of ["a;b", "a{", "<b>", "a\\b", "x/*", "URL (x)", "", "x".repeat(201), "a\nb"]) assert.equal(L.isCssValue(bad), false, bad);
});

// --- Review: live overlays and click-to-restyle ------------------------------------------------------

const LIVE_SIDE = { edl: { output: { width: 720, height: 1280 }, overlays: [
  { html: "/job/a.html", start: 1, end: 3, mode: "alpha", region: { x: 40, y: 860, w: 640, h: 200 }, style: "clean-tech" },
  { html: "/job/b.html", start: 4, end: 6, mode: "panel", region: null },
  { html: "/job/c.html", start: 6, end: 7 },                                   // no key: not played
] } };
const LIVE_KEYS = { "/job/a.html": "ka", "/job/b.html": "kb" };

test("liveOverlays: the overlays with a key, their mode and region (the whole frame without one)", () => {
  const live = L.liveOverlays(LIVE_SIDE, LIVE_KEYS);
  assert.deepEqual(live.map((o) => [o.index, o.key, o.mode, o.transparent]), [[0, "ka", "alpha", true], [1, "kb", "panel", false]]);
  assert.deepEqual(live[1].rect, { x: 0, y: 0, w: 720, h: 1280 });
  assert.equal(L.liveOverlays({ edl: { overlays: [{ html: "/x.html", start: 0, end: 1 }] } }, { "/x.html": "k" })[0].mode, "keyed");
  assert.equal(L.liveOverlays({ edl: { overlays: [{ html: "/x.html", start: 0, end: 1 }] } }, { "/x.html": "k" })[0].transparent, true);
  assert.deepEqual(L.liveOverlays(null, LIVE_KEYS), []);
  assert.deepEqual(L.liveOverlays(LIVE_SIDE, {}), []);
});

test("overlayTime is the overlay's own clock while it is on screen", () => {
  const [o] = L.liveOverlays(LIVE_SIDE, LIVE_KEYS);
  assert.equal(L.overlayTime(o, 0.99), null);
  assert.equal(L.overlayTime(o, 1), 0);
  assert.equal(L.overlayTime(o, 2.5004), 1.5);
  assert.equal(L.overlayTime(o, 3), null);
  assert.equal(L.overlayTime(o, NaN), null);
});

test("regionBox scales a region from output pixels to the picture", () => {
  assert.deepEqual(L.regionBox({ x: 40, y: 860, w: 640, h: 200 }, { width: 720, height: 1280 }, { width: 360, height: 640 }),
    { left: 20, top: 430, width: 320, height: 100, scale: 0.5 });
  assert.equal(L.regionBox({ x: 1, y: 1, w: 1, h: 1 }, { width: 0 }, { width: 100 }).scale, 0);
});

const PICKED = { type: "picked", selector: "body > div.card > div.n:nth-of-type(2)", box: { x: 56, y: 76, w: 300, h: 150 },
  styles: { color: "rgb(255, 255, 255)", "background-color": "rgba(0, 0, 0, 0)", "font-family": "Inter, sans-serif",
            "font-size": "150px", "font-weight": "800", opacity: "1", transform: "none" }, text: "72%" };

test("bridgeMessage accepts ready and picked messages and nothing else", () => {
  assert.deepEqual(L.bridgeMessage({ type: "ready", width: 640, height: 200, seek: "default" }), { type: "ready", width: 640, height: 200 });
  assert.equal(L.bridgeMessage({ type: "ready", width: 0, height: 200 }), null);
  assert.deepEqual(L.bridgeMessage(PICKED), PICKED);
  for (const bad of [null, "picked", { type: "seek", t: 1 }, { ...PICKED, selector: "" }, { ...PICKED, selector: "a\nb" },
                     { ...PICKED, selector: "x".repeat(301) }, { ...PICKED, box: { x: 1, y: 1, w: -1, h: 1 } },
                     { ...PICKED, box: null }, { ...PICKED, styles: { color: "red" } }, { ...PICKED, styles: { ...PICKED.styles, opacity: 1 } }]) {
    assert.equal(L.bridgeMessage(bad), null, JSON.stringify(bad));
  }
  const long = L.bridgeMessage({ ...PICKED, text: "y".repeat(500), styles: { ...PICKED.styles, "font-family": "f".repeat(400) } });
  assert.equal(long.text.length, 80);
  assert.equal(long.styles["font-family"].length, 300);
});

test("cssColourToHex reads computed colours for the colour picker", () => {
  assert.equal(L.cssColourToHex("rgb(255, 59, 48)"), "#FF3B30");
  assert.equal(L.cssColourToHex("rgba(0, 0, 0, 1)"), "#000000");
  assert.equal(L.cssColourToHex("#3ddc97"), "#3DDC97");
  assert.equal(L.cssColourToHex("rgba(0, 0, 0, 0)"), null);              // transparent has no plain colour
  assert.equal(L.cssColourToHex("red"), null);
});

test("restyleRow: only changed, valid properties, with the computed value as old", () => {
  const [o] = L.liveOverlays(LIVE_SIDE, LIVE_KEYS);
  const picked = L.bridgeMessage(PICKED);
  const { row, problem } = L.restyleRow("clip-01", 1, 1.80049, o, picked,
    { color: "#FF3B30", opacity: "1", "font-size": "  ", transform: "scale(1.1)" }, " redder ");
  assert.equal(problem, null);
  assert.deepEqual(row, { kind: "restyle", video: "clip-01", version: 1, t: 1.8, overlay: 0, html: "/job/a.html",
    selector: PICKED.selector, changes: { color: ["rgb(255, 255, 255)", "#FF3B30"], transform: ["none", "scale(1.1)"] },
    text: "redder" });
  assert.deepEqual(L.restyleRow("clip-01", 1, 1.8, o, picked, { opacity: "1" }, ""), { row: null, problem: "Change a value to send it." });
  assert.match(L.restyleRow("clip-01", 1, 1.8, o, picked, { color: "red; display:none" }, "").problem, /one CSS value/);
  assert.match(L.restyleRow("clip-01", 1, 1.8, o, picked, { "background-color": "url(x)" }, "").problem, /url/);
  assert.deepEqual(L.restyleRow("clip-01", 1, 1.8, o, picked, { width: "1px" }, "").row, null);   // not a restyle property
});

test("the page and the bridge agree on the restyle properties", () => {
  const fs = require("node:fs");
  const bridge = fs.readFileSync(path.join(__dirname, "..", "skills", "video-edit", "scripts", "static", "overlay-bridge.js"), "utf8");
  const props = JSON.parse(/var PROPS = (\[[^\]]+\]);/.exec(bridge)[1]);
  assert.deepEqual(props, L.RESTYLE_PROPS);
});
