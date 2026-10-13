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
