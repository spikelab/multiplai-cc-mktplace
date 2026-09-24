// Tests for the page's pure functions. Run: node tests/logic.test.js
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "..", "review_viewer", "static", "logic.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("replies group by reply_to in file order", () => {
  const threads = L.groupReplies([
    { reply_to: "q1", text: "Looking at it…", done: false },
    { reply_to: "q2", text: "Short answer.", done: true },
    { reply_to: "q1", text: "It is safe.", done: true },
  ]);
  assert.deepEqual([...threads.keys()], ["q1", "q2"]);
  assert.equal(threads.get("q1").text, "Looking at it…\n\nIt is safe.");
  assert.equal(threads.get("q1").done, true);
});

test("split chunks ending on a newline join without a gap", () => {
  const threads = L.groupReplies([
    { reply_to: "q", text: "line 1\nline 2\n", done: false },
    { reply_to: "q", text: "line 3", done: true },
  ]);
  assert.equal(threads.get("q").text, "line 1\nline 2\nline 3");
});

test("spinner runs until a done part arrives, and restarts on a later --more", () => {
  const rows = [{ reply_to: "q", text: "wait", done: false }];
  assert.equal(L.isPending("q", L.groupReplies(rows)), true);
  assert.equal(L.isPending("other", L.groupReplies(rows)), true);
  rows.push({ reply_to: "q", text: "done", done: true });
  assert.equal(L.isPending("q", L.groupReplies(rows)), false);
  rows.push({ reply_to: "q", text: "one more thing", done: false });
  assert.equal(L.isPending("q", L.groupReplies(rows)), true);
});

test("malformed rows are ignored", () => {
  const threads = L.groupReplies([null, { text: "x" }, { reply_to: "q", done: true }]);
  assert.equal(threads.size, 1);
  assert.equal(threads.get("q").text, "");
});

test("poll delay", () => {
  assert.equal(L.pollDelay(1), 2000);
  assert.equal(L.pollDelay(0), 10000);
});

test("citations map to row indices, skipping deleted rows", () => {
  const rows = [
    { k: "ctx", n: 7 }, { k: "ctx", n: 8 }, { k: "del", o: 5, n: null },
    { k: "add", n: 9 }, { k: "gap", n: null }, { k: "ctx", n: 10 },
  ];
  assert.deepEqual(L.citationRows(rows, 8, 9), [1, 3]);
  assert.deepEqual(L.citationRows(rows, 20, 30), []);
});

test("findings group by severity and hide refuted/rejected", () => {
  const fs = [
    { id: "a", severity: "LOW", status: "confirmed" },
    { id: "b", severity: "HIGH", status: "confirmed" },
    { id: "c", severity: "HIGH", status: "refuted" },
    { id: "d", severity: "MEDIUM", status: "unverifiable" },
  ];
  const decisions = { d: { decision: "reject" } };
  const shown = L.groupFindings(fs, decisions, false);
  assert.equal(shown.hidden, 2);
  assert.deepEqual(L.findingOrder(shown), ["b", "a"]);
  const all = L.groupFindings(fs, decisions, true);
  assert.deepEqual(L.findingOrder(all), ["b", "c", "d", "a"]);
});

test("j/k stepping clamps at both ends", () => {
  const order = ["a", "b", "c"];
  assert.equal(L.stepFinding(order, "a", 1), "b");
  assert.equal(L.stepFinding(order, "c", 1), "c");
  assert.equal(L.stepFinding(order, "a", -1), "a");
  assert.equal(L.stepFinding(order, null, 1), "a");
  assert.equal(L.stepFinding(order, null, -1), "c");
  assert.equal(L.stepFinding([], null, 1), null);
});

test("anchor labels", () => {
  assert.equal(L.anchorLabel({ path: "a.py", line_start: 3, line_end: 3 }), "a.py:3");
  assert.equal(L.anchorLabel({ path: "a.py", line_start: 3, line_end: 5 }), "a.py:3–5");
  assert.deepEqual(L.lineRange(9, 4), [4, 9]);
});

test("escapeHtml escapes markup", () => {
  assert.equal(L.escapeHtml(`<img src=x onerror="a">&'`),
    "&lt;img src=x onerror=&quot;a&quot;&gt;&amp;&#39;");
});

test("highlighted HTML splits into balanced lines", () => {
  const html = 'x = <span class="s">"""a\nb"""</span> <span class="c">#c</span>\ny';
  const lines = L.splitHighlighted(html);
  assert.deepEqual(lines, [
    'x = <span class="s">"""a</span>',
    '<span class="s">b"""</span> <span class="c">#c</span>',
    "y",
  ]);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`not ok - ${name}\n${err.stack}`);
  }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
