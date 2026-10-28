// Tests for the page's pure functions. Run: node tests/logic.test.js
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "..", "pipeline_viewer", "static", "logic.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --- sections -----------------------------------------------------------------

test("a document splits at its ## headings, full text kept", () => {
  const doc = "# Proposal\n\nIntro words.\n\n## Why\n\nPeople want it.\n\n### Detail\n\nMore.\n\n## What changes\n\nA thing.\n";
  const s = L.splitSections(doc, "proposal.md");
  assert.deepEqual(s.map((x) => x.heading), ["Proposal", "Why", "What changes"]);
  assert.equal(s[0].body, "# Proposal\n\nIntro words.\n");
  assert.equal(s[1].body, "## Why\n\nPeople want it.\n\n### Detail\n\nMore.\n");
  assert.equal(s[2].body, "## What changes\n\nA thing.\n");
  // Nothing is dropped: every line of the source is in some section.
  const joined = s.map((x) => x.body).join("");
  for (const line of doc.split("\n").filter(Boolean)) assert.ok(joined.includes(line), line);
});

test("a title with nothing under it opens the first section instead of its own screen", () => {
  const s = L.splitSections("# Proposal\n\n## Why\n\nBecause.\n", "proposal.md");
  assert.deepEqual(s.map((x) => x.heading), ["Why"]);
  assert.equal(s[0].body, "# Proposal\n\n## Why\n\nBecause.\n");
});

test("## inside a code fence is not a heading", () => {
  const doc = "## Steps\n\n```md\n## not a heading\n```\n\n~~~\n## nor this\n~~~\n\n## Next\n\nok\n";
  const s = L.splitSections(doc, "plan.md");
  assert.deepEqual(s.map((x) => x.heading), ["Steps", "Next"]);
  assert.ok(s[0].body.includes("## not a heading"));
  assert.ok(s[0].body.includes("## nor this"));
});

test("a document with no ## headings is one section", () => {
  const s = L.splitSections("# Tasks\n\nDo it.\n", "tasks.md");
  assert.equal(s.length, 1);
  assert.equal(s[0].heading, "Tasks");
  assert.equal(L.splitSections("", "empty.md").length, 1);
});

test("a file that is not markdown is one code section", () => {
  const s = L.splitSections('{"a": 1}\n', "tickets.json");
  assert.equal(s.length, 1);
  assert.equal(s[0].code, "json");
  assert.equal(s[0].body, '{"a": 1}\n');
});

test("headings with closing hashes and extra spaces are named cleanly", () => {
  const s = L.splitSections("##   Two   words  ##\n\ntext\n", "a.md");
  assert.equal(s[0].heading, "Two words");
});

test("review steps put the documents to approve first, skip missing ones", () => {
  const steps = L.reviewSteps([
    { path: "tasks.md", approve: false, text: "# Tasks\n\n## Block 1\n\nx\n" },
    { path: "implementation-plan.md", approve: true, text: "## Approach\n\na\n\n## Risks\n\nb\n" },
    { path: "plan-approval.md", approve: false, text: null },
  ]);
  assert.deepEqual(steps.map((s) => [s.doc, s.heading, s.approve]), [
    ["implementation-plan.md", "Approach", true],
    ["implementation-plan.md", "Risks", true],
    ["tasks.md", "Block 1", false],
  ]);
  assert.equal(steps[1].index, 1);
  assert.equal(steps[1].count, 2);
  assert.equal(new Set(steps.map((s) => s.key)).size, 3);
});

test("the server's comments file parses back to doc, section, person and text", () => {
  const text = "# Review of stop 1: add-export\n\n## proposal.md — Why\n\nAda Example, 2026-01-02T10:00:00Z:\n\nIs this needed?\n\n## proposal.md — What changes\n\nBo Example, 2026-01-02T11:00:00Z:\n\nLine one.\nLine two.\n";
  const rows = L.parseReviewFile(text);
  assert.deepEqual(rows, [
    { doc: "proposal.md", section: "Why", by: "Ada Example", at: "2026-01-02T10:00:00Z", text: "Is this needed?" },
    { doc: "proposal.md", section: "What changes", by: "Bo Example", at: "2026-01-02T11:00:00Z", text: "Line one.\nLine two." },
  ]);
  assert.deepEqual(L.parseReviewFile(""), []);
});

// --- sorting ------------------------------------------------------------------

function change(name, opts) {
  const o = opts || {};
  const stops = [1, 2, 3, 4, 5].map((n) => ({
    n, state: n === o.stop ? (o.state || "waiting") : "not_reached",
    waiting_on: n === o.stop ? (o.waiting_on || []) : [],
    waiting_since: n === o.stop ? (o.since || null) : null,
    acknowledged_by: [], approved_by: [], reviewers: [],
  }));
  return { name, current_stop: o.stop || null, waiting_on: o.waiting_on || [], stops, build: o.build || null };
}

test("changes waiting on the viewer come first, then other waits, then builds, then the rest", () => {
  const list = [
    change("zeta"),
    change("gamma", { stop: 4, state: "approved", build: { complete: false } }),
    change("beta", { stop: 2, waiting_on: ["Bo"], since: "2026-01-02T09:00:00Z" }),
    change("alpha", { stop: 1, waiting_on: ["Ada"], since: "2026-01-02T12:00:00Z" }),
    change("delta", { stop: 3, waiting_on: ["Ada", "Bo"], since: "2026-01-02T08:00:00Z" }),
  ];
  assert.deepEqual(L.sortChanges(list, "Ada").map((c) => c.name), ["delta", "alpha", "beta", "gamma", "zeta"]);
  assert.deepEqual(L.sortChanges(list, "Bo").map((c) => c.name), ["delta", "beta", "alpha", "gamma", "zeta"]);
  // The input is not reordered.
  assert.equal(list[0].name, "zeta");
});

test("ties sort by name", () => {
  assert.deepEqual(L.sortChanges([change("b"), change("a")], "x").map((c) => c.name), ["a", "b"]);
});

// --- estimate, budget, durations ---------------------------------------------

test("remaining time is elapsed / blocks done × blocks left", () => {
  assert.equal(L.estimateRemaining({ elapsed_s: 3600, blocks_done: 2, blocks_total: 5 }), 5400);
  assert.equal(L.estimateRemaining({ elapsed_s: 3600, blocks_done: 0, blocks_total: 5 }), null);
  assert.equal(L.estimateRemaining({ elapsed_s: 3600, blocks_done: 5, blocks_total: 5 }), null);
  assert.equal(L.estimateRemaining({ elapsed_s: 0, blocks_done: 1, blocks_total: 5 }), null);
  assert.equal(L.estimateRemaining(null), null);
});

test("budget share and money", () => {
  assert.equal(L.budgetShare({ spent_usd: 5, max_usd: 20 }), 0.25);
  assert.equal(L.budgetShare({ spent_usd: 5, max_usd: null }), null);
  assert.equal(L.formatUsd(3.2), "$3.20");
});

test("durations read as words", () => {
  assert.equal(L.formatDuration(30), "less than a minute");
  assert.equal(L.formatDuration(25 * 60), "25 min");
  assert.equal(L.formatDuration(3 * 3600), "3 h");
  assert.equal(L.formatDuration(5400), "1 h 30 min");
  assert.equal(L.formatDuration(50 * 3600), "2 days 2 h");
  assert.equal(L.formatDuration(null), "");
});

test("clock times say today, tomorrow or the date", () => {
  const now = new Date(2026, 0, 5, 9, 0).getTime();
  assert.equal(L.formatClock(new Date(2026, 0, 5, 18, 0).toISOString(), now), "18:00");
  assert.equal(L.formatClock(new Date(2026, 0, 6, 9, 30).toISOString(), now), "tomorrow 09:30");
  assert.equal(L.formatClock(new Date(2026, 0, 4, 17, 5).toISOString(), now), "yesterday 17:05");
  assert.equal(L.formatClock(new Date(2026, 0, 9, 8, 0).toISOString(), now), "Fri 9 Jan 08:00");
});

// --- stop labels and lines --------------------------------------------------

test("stop names and button labels", () => {
  assert.equal(L.stopTitle(1), "Proposal");
  assert.equal(L.stopTitle(4), "Implementation plan");
  assert.equal(L.approveLabel(1), "Approve the proposal");
  assert.equal(L.approveLabel(4), "Approve the implementation plan");
  assert.equal(L.reviewLabel(2), "Review the use cases and components");
  assert.equal(L.phaseLabel("tdd_build"), "TDD build");
  assert.equal(L.phaseLabel("complete"), "Finished");
});

test("a waiting stop says who, how long, and when Claude decides", () => {
  const now = new Date(2026, 0, 5, 15, 0).getTime();
  const stop = {
    n: 1, state: "waiting", waiting_on: ["Pat"], acknowledged_by: [],
    waiting_since: new Date(2026, 0, 5, 12, 0).toISOString(),
    decide_at: new Date(2026, 0, 5, 18, 0).toISOString(),
  };
  assert.equal(L.stopLine(stop, { now, agent: "Claude" }),
    "Waiting on Pat for 3 h — Claude decides at 18:00 unless someone replies");
  assert.equal(L.stopLine(stop, { now, me: "Pat" }),
    "Waiting on you for 3 h — Claude decides at 18:00 unless someone replies");
  const late = Object.assign({}, stop, { decide_at: new Date(2026, 0, 5, 14, 0).toISOString() });
  assert.equal(L.stopLine(late, { now }), "Waiting on Pat for 3 h — Claude was asked to decide at 14:00");
  const acked = Object.assign({}, stop, { state: "acknowledged", acknowledged_by: ["Pat"], decide_at: null });
  assert.equal(L.stopLine(acked, { now }), "Waiting on Pat for 3 h — Pat is reviewing it, so Claude waits for an answer");
});

test("finished stops say how they ended", () => {
  assert.equal(L.stopLine({ n: 2, state: "approved", approved_by: ["Pat"] }, {}), "Approved by Pat");
  assert.equal(L.stopLine({ n: 2, state: "approved", approved_by: [] }, {}),
    "Passed (no approval was needed or recorded on the page)");
  assert.equal(L.stopLine({ n: 3, state: "decided_by_agent" }, { agent: "Claude" }),
    "Claude decided, because nobody replied in time");
  assert.equal(L.stopLine({ n: 1, state: "rework_requested", rework_by: ["Pat"] }, { me: "Pat" }),
    "Changes asked for by you; Claude is reworking it");
  assert.equal(L.stopLine({ n: 4, state: "not_reached" }, {}), "Not reached yet");
});

test("names join as prose", () => {
  assert.equal(L.namesText(["A", "B", "C"]), "A, B and C");
  assert.equal(L.namesText(["A", "B"], "B"), "A and you");
  assert.equal(L.namesText([]), "");
});

test("the acknowledgement is sent only by a reviewer the stop waits on, once", () => {
  const stop = { n: 1, state: "waiting", waiting_on: ["Pat"], acknowledged_by: [], reviewers: [{ name: "Pat" }] };
  assert.equal(L.shouldAck(stop, "Pat"), true);
  assert.equal(L.shouldAck(stop, "Eli"), false);
  assert.equal(L.shouldAck(Object.assign({}, stop, { acknowledged_by: ["Pat"], state: "acknowledged" }), "Pat"), false);
  assert.equal(L.shouldAck(Object.assign({}, stop, { state: "approved" }), "Pat"), false);
  assert.equal(L.shouldAck(Object.assign({}, stop, { n: 5 }), "Pat"), false);
});

test("approve and rework are offered to a reviewer of an open stop who has not approved", () => {
  const stop = { n: 3, state: "acknowledged", reviewers: [{ name: "Pat" }, { name: "Eli" }], approved_by: ["Pat"] };
  assert.equal(L.canDecide(stop, "Eli"), true);
  assert.equal(L.canDecide(stop, "Pat"), false);
  assert.equal(L.canDecide(stop, "Zoe"), false);
  assert.equal(L.canDecide(Object.assign({}, stop, { state: "approved" }), "Eli"), false);
  assert.equal(L.canDecide(Object.assign({}, stop, { n: 5 }), "Eli"), false);
});

// --- answers, diffs, address ------------------------------------------------

test("answers group by the question they reply to", () => {
  const m = L.groupReplies([
    { reply_to: "q1", text: "Looking…", done: false },
    { reply_to: "q2", text: "Short.", done: true },
    { reply_to: "q1", text: "Done.", done: true, commit: "abc1234" },
    { bad: true },
  ]);
  assert.deepEqual([...m.keys()], ["q1", "q2"]);
  assert.equal(m.get("q1").text, "Looking…\n\nDone.");
  assert.equal(m.get("q1").done, true);
  assert.equal(m.get("q1").commit, "abc1234");
});

test("diff lines are classified", () => {
  assert.equal(L.diffLineKind("@@ -1 +1 @@"), "hunk");
  assert.equal(L.diffLineKind("+++ b/x"), "meta");
  assert.equal(L.diffLineKind("+added"), "add");
  assert.equal(L.diffLineKind("-gone"), "del");
  assert.equal(L.diffLineKind(" same"), "ctx");
});

test("the address round-trips, and Slack's links parse", () => {
  assert.deepEqual(L.parseHash("#change=add-export&stop=2"), { change: "add-export", stop: 2, review: false, s: null });
  const r = { change: "a b", stop: 3, review: true, s: 4 };
  assert.deepEqual(L.parseHash(L.makeHash(r)), r);
  assert.deepEqual(L.parseHash(""), { change: null, stop: null, review: false, s: null });
  assert.equal(L.makeHash({}), "#");
  assert.equal(L.parseHash("#stop=x").stop, null);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log("ok   " + name);
  } catch (err) {
    failed++;
    console.log("FAIL " + name + "\n" + (err && err.stack ? err.stack : err));
  }
}
console.log(`${tests.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
