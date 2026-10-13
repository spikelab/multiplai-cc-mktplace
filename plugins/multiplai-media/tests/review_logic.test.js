// Tests for the job page's pure functions (skills/video-edit/scripts/static/logic.js).
// Run: node --test tests/review_logic.test.js   (tests/test_review_logic_js.py runs it under pytest)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "..", "skills", "video-edit", "scripts", "static", "logic.js"));

const JOB = { status: "ok", data: { kind: "reels" } };
const DEMO = { status: "ok", data: { kind: "demo" } };
const CANDS = { status: "ok", data: [{ id: "c1" }, { id: "c2" }] };

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
  assert.equal(L.clipsOpen({ job: JOB, candidates: CANDS, rows: [{ kind: "pick", candidate: "c1" }] }), true);
  assert.equal(L.clipsOpen({ job: JOB, candidates: CANDS,
    rows: [{ kind: "pick", candidate: "c1" }, { kind: "pick", candidate: "c2" }] }), false);
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

test("panelsRow sends whole source pixels and only the mapped labels", () => {
  const row = L.panelsRow([{ name: " Host ", x: 0.4, y: 0, w: 640.2, h: 720 }], { SPEAKER_0: "Host", SPEAKER_1: "" }, " left ");
  assert.deepEqual(row, { kind: "panels", panels: { Host: { x: 0, y: 0, w: 640, h: 720 } },
                          speakers: { SPEAKER_0: "Host" }, text: "left" });
});
