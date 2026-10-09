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

test("findings group by severity and hide refuted, rejected and decided", () => {
  const fs = [
    { id: "a", severity: "LOW", status: "confirmed" },
    { id: "b", severity: "HIGH", status: "confirmed" },
    { id: "c", severity: "HIGH", status: "refuted" },
    { id: "d", severity: "MEDIUM", status: "unverifiable" },
    { id: "e", severity: "MEDIUM", status: "confirmed" },
    { id: "f", severity: "LOW", status: "confirmed" },
  ];
  const decisions = { d: { decision: "reject" }, e: { decision: "accept" }, f: { decision: "defer" } };
  const shown = L.groupFindings(fs, decisions, false);
  assert.equal(shown.hidden, 4);
  assert.deepEqual(L.findingOrder(shown), ["b", "a"]);
  const all = L.groupFindings(fs, decisions, true);
  assert.deepEqual(L.findingOrder(all), ["b", "c", "d", "e", "a", "f"]);
  // j/k from a finding a decision just hid: it keeps its place, so j goes on to the next.
  assert.deepEqual(L.navOrder(fs, decisions, false, "e"), ["b", "e", "a"]);
  assert.deepEqual(L.navOrder(fs, decisions, false, "b"), ["b", "a"]);
  assert.equal(L.stepFinding(L.navOrder(fs, decisions, false, "e"), "e", 1), "a");
});

test("repeats and low-value findings fold after the rest, most severe first", () => {
  const fs = [
    { id: "a", severity: "LOW", status: "confirmed", assessment: { label: "low-value", reason: "[speculative] r" } },
    { id: "b", severity: "HIGH", status: "confirmed", assessment: { label: "useful" } },
    { id: "c", severity: "MEDIUM", status: "confirmed", assessment: { label: "repeat", earlier_id: "x" } },
    { id: "d", severity: "MEDIUM", status: "unverifiable", assessment: { label: "still-open", earlier_id: "y" } },
    { id: "e", severity: "LOW", status: "confirmed" },
  ];
  const g = L.groupFindings(fs, {}, false);
  assert.deepEqual(g.folded.map((f) => f.id), ["c", "a"]);
  assert.deepEqual(L.findingOrder(g), ["b", "d", "e", "c", "a"]);
  // A file written before the assess stage has no labels: nothing folds.
  const old = L.groupFindings([{ id: "e", severity: "LOW", status: "confirmed" }], {}, false);
  assert.deepEqual(old.folded, []);
  assert.deepEqual(L.findingOrder(old), ["e"]);
});

test("a repeat counts as decided (rejected) until the person decides otherwise", () => {
  const repeat = { id: "c", severity: "HIGH", status: "confirmed",
    assessment: { label: "repeat", earlier_id: "x", earlier_note: "by design" } };
  const plain = { id: "b", severity: "LOW", status: "confirmed" };
  const refuted = { id: "r", severity: "LOW", status: "refuted" };
  assert.deepEqual(L.effectiveDecision(repeat, {}), { decision: "reject", note: "by design", implied: true });
  assert.equal(L.effectiveDecision(plain, {}), null);
  assert.equal(L.effectiveDecision(repeat, { c: { decision: "accept" } }).decision, "accept");
  assert.deepEqual(L.undecidedCount([repeat, plain, refuted], {}), { open: 1, total: 2 });
  assert.deepEqual(L.undecidedCount([repeat, plain], { c: { decision: "accept" }, b: { decision: "defer" } }),
    { open: 0, total: 2 });
  // The merge-risk count treats it as rejected too.
  const walk = { risk: { tier: 1, tier_why: "w", revertable: true, revert_why: "x" }, assessments: [] };
  assert.equal(L.riskInputs({}, walk, [repeat], {}, []).openHigh, 0);
  assert.equal(L.riskInputs({}, walk, [repeat], { c: { decision: "accept" } }, []).openHigh, 1);
});

test("assessment text names the earlier round, decision and note", () => {
  assert.equal(L.assessmentText(null), "");
  assert.equal(L.assessmentText({ label: "useful", reason: "r" }), "");
  assert.equal(L.assessmentText({ label: "repeat", earlier_id: "abcdef0123", earlier_round: "1".repeat(40),
    earlier_decision: "reject", earlier_note: "by design", reason: "same defect" }),
  "Repeats abcdef0123 (round 111111111111), which you rejected: by design. same defect");
  assert.equal(L.assessmentText({ label: "still-open", earlier_id: "y", earlier_decision: "accept", reason: "" }),
    "Still open from y, your decision accept");
  assert.equal(L.assessmentText({ label: "low-value", reason: "[covered] by b" }), "Low value: [covered] by b");
  assert.equal(L.explanationText({ label: "useful", reason: "r" }), "r");
  assert.equal(L.explanationText({ label: "useful" }), "");
  assert.equal(L.explanationText(null), "");
  assert.equal(L.explanationText({ label: "low-value", reason: "[covered] by b" }), "Low value: [covered] by b");
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

test("overlapping polls never append the same rows twice", () => {
  const rows = [{ reply_to: "q", text: "a", done: false }, { reply_to: "q", text: "b", done: true }];
  let cur = { rows: [], since: 0 };
  // Two polls were sent with since=0; both come back with the same rows.
  const first = L.applyPoll(cur, 0, { answers: rows, n: 2 });
  cur = { rows: first.rows, since: first.since };
  const second = L.applyPoll(cur, 0, { answers: rows, n: 2 });
  assert.equal(second.changed, false);
  assert.equal(second.rows.length, 2);
  assert.equal(L.groupReplies(second.rows).get("q").text, "a\n\nb");
});

test("a shrunken outbox resets the read position", () => {
  const next = L.applyPoll({ rows: [{}, {}, {}], since: 3 }, 3, { answers: [], n: 1 });
  assert.deepEqual([next.rows.length, next.since, next.reset], [0, 0, true]);
});

// --- walkthrough -------------------------------------------------------------

const WALK = {
  complete: false,
  steps: [
    { id: "purpose", anchors: [{ path: "a.py", side: "head", line_start: 1, line_end: 2 }], finding_ids: [] },
    { id: "core", anchors: [{ path: "b.py", side: "head", line_start: 5, line_end: 9 }], finding_ids: ["f1"] },
    { id: "tests", anchors: [{ path: "old.py", side: "base", line_start: 3, line_end: 4 }], finding_ids: ["f1", "f2"] },
  ],
  skipped: [{ path: "uv.lock", reason: "generated" }],
};

test("steps keep the walkthrough's order", () => {
  assert.deepEqual(L.stepOrder(WALK), ["purpose", "core", "tests"]);
  assert.deepEqual(L.stepOrder(null), []);
});

test("[ and ] move one step and stop at the ends", () => {
  assert.equal(L.moveStep(WALK, "purpose", 1), "core");
  assert.equal(L.moveStep(WALK, "tests", 1), "tests");
  assert.equal(L.moveStep(WALK, "purpose", -1), "purpose");
  assert.equal(L.moveStep(WALK, null, 1), "purpose");
  assert.equal(L.moveStep(WALK, null, -1), "tests");
  assert.equal(L.moveStep({ steps: [] }, null, 1), null);
  assert.deepEqual(L.stepPosition(WALK, "core"), { index: 2, total: 3 });
});

test("coverage counts anchored or skipped files and linked findings", () => {
  const cov = L.walkCoverage(WALK, ["a.py", "b.py", "c.py", "uv.lock"], [
    { id: "f1", status: "confirmed" }, { id: "f3", status: "unverifiable" },
    { id: "f2", status: "refuted" },
  ]);
  assert.deepEqual(cov, { files: 3, filesTotal: 4, findings: 1, findingsTotal: 2 });
  assert.deepEqual(L.walkCoverage(null, ["a"], []), { files: 0, filesTotal: 1, findings: 0, findingsTotal: 0 });
});

test("anchors map to rows by new or old line number", () => {
  const rows = [
    { k: "ctx", o: null, n: 1 }, { k: "del", o: 3, n: null }, { k: "del", o: 4, n: null },
    { k: "add", o: null, n: 2 }, { k: "gap", o: null, n: null }, { k: "ctx", o: null, n: 3 },
  ];
  assert.deepEqual(L.anchorRows(rows, { side: "head", line_start: 2, line_end: 3 }), [3, 5]);
  assert.deepEqual(L.anchorRows(rows, { side: "base", line_start: 3, line_end: 4 }), [1, 2]);
  assert.equal(L.walkAnchorLabel({ path: "x", side: "base", line_start: 3, line_end: 4 }), "x:3–4 (base)");
});

test("a finding's steps, and the diagram image source", () => {
  assert.deepEqual(L.stepsForFinding(WALK, "f1").map((s) => s.id), ["core", "tests"]);
  assert.deepEqual(L.stepsForFinding(WALK, "zz"), []);
  const url = L.svgDataUrl('<svg xmlns="http://www.w3.org/2000/svg"><text>a & "b"</text></svg>');
  assert.ok(url.startsWith("data:image/svg+xml;charset=utf-8,%3Csvg"));
  assert.ok(!/[<>"&# ]/.test(url.slice(url.indexOf(",") + 1)));
});

test("only github.com links become PR links", () => {
  assert.equal(L.safePrUrl("https://github.com/o/r/pull/7"), "https://github.com/o/r/pull/7");
  assert.equal(L.safePrUrl("javascript:alert(1)"), null);
  assert.equal(L.safePrUrl("https://evil.example/o/r/pull/7"), null);
  assert.equal(L.safePrUrl(undefined), null);
});

test("files group by directory in first-seen order; root files have dir ''", () => {
  const g = L.groupFilesByDir(["a/b.txt", "a/c/d.txt", "a/e.txt", "top.md"]);
  assert.deepEqual(g.map((x) => x.dir), ["a", "a/c", ""]);
  assert.deepEqual(g[0].files, [{ path: "a/b.txt", name: "b.txt" }, { path: "a/e.txt", name: "e.txt" }]);
  assert.deepEqual(g[2].files, [{ path: "top.md", name: "top.md" }]);
  assert.deepEqual(L.groupFilesByDir([]), []);
});

test("a long directory keeps its last two folders after an ellipsis", () => {
  assert.equal(L.shortDir("plugins/multiplai-dev/skills/review-viewer/scripts"), "…/review-viewer/scripts/");
  assert.equal(L.shortDir("plugins/multiplai-dev"), "plugins/multiplai-dev/");
  assert.equal(L.shortDir(".claude-plugin"), ".claude-plugin/");
  assert.equal(L.shortDir(""), "/");
  assert.equal(L.shortDir("a/b/c/d", 3), "…/b/c/d/");
});

test("the sidebar width is clamped, and junk is refused", () => {
  assert.equal(L.clampWidth(100, 160, 700), 160);
  assert.equal(L.clampWidth(900, 160, 700), 700);
  assert.equal(L.clampWidth("321.6", 160, 700), 322);
  assert.equal(L.clampWidth("wide", 160, 700), null);
  assert.equal(L.clampWidth(null, 160, 700), 160);
});

const R = (ks) => ks.split("").map((k, i) => ({ k: { c: "ctx", a: "add", d: "del", g: "gap" }[k], n: i + 1 }));

test("unchanged runs fold, keeping context around changes and kept rows", () => {
  //            0123456789012345
  const rows = R("cccccccccaccccccccccc");
  const items = L.foldRows(rows, [], [], 2, 3);
  assert.deepEqual(items[0], { fold: [0, 6] });
  assert.deepEqual(items.slice(1, 6).map((x) => x.row), [7, 8, 9, 10, 11]);
  assert.deepEqual(items[6], { fold: [12, 20] });
  assert.equal(items.length, 7);
});

test("a short hidden run is shown, not folded; kept and opened rows show", () => {
  const rows = R("ccccacccc");
  assert.ok(L.foldRows(rows, [], [], 2, 3).every((x) => x.row != null));
  const kept = L.foldRows(R("cccccccccccccccc"), [8], [], 1, 3);
  assert.deepEqual(kept.filter((x) => x.row != null).map((x) => x.row), [7, 8, 9]);
  const opened = L.foldRows(R("cccccccccccc"), [], [0, 1], 3, 3);
  assert.deepEqual(opened, [{ row: 0 }, { row: 1 }, { fold: [2, 11] }]);
});

test("gap rows always show; an all-added file has no folds", () => {
  assert.ok(L.foldRows(R("aaaaaaaa"), [], [], 3, 4).every((x) => x.row != null));
  assert.deepEqual(L.foldRows(R("cccccgccccc"), [], [], 3, 4), [{ fold: [0, 4] }, { row: 5 }, { fold: [6, 10] }]);
});

test("expanding a fold opens all of it, or a step from its top or bottom", () => {
  assert.deepEqual(L.expandFold([10, 50], "down", 3), [10, 11, 12]);
  assert.deepEqual(L.expandFold([10, 50], "up", 3), [48, 49, 50]);
  assert.equal(L.expandFold([10, 50], "all", 3).length, 41);
  assert.deepEqual(L.expandFold([10, 11], "down", 20), [10, 11]);
});

test("files move in sidebar order, filtered, stopping at both ends", () => {
  const files = ["a/b.txt", "a/c/d.txt", "a/e.txt", "top.md"];
  assert.deepEqual(L.fileOrder(files, ""), ["a/b.txt", "a/e.txt", "a/c/d.txt", "top.md"]);
  assert.deepEqual(L.fileOrder(files, "TXT"), ["a/b.txt", "a/e.txt", "a/c/d.txt"]);
  const order = L.fileOrder(files, "");
  assert.equal(L.neighbourFile(order, "a/e.txt", 1), "a/c/d.txt");
  assert.equal(L.neighbourFile(order, "a/b.txt", -1), null);
  assert.equal(L.neighbourFile(order, "top.md", 1), null);
  assert.equal(L.neighbourFile(order, "gone.md", 1), null);
});

test("Prev / Next follow the open review step when it spans several files", () => {
  const files = ["a.py", "b.py", "c.py", "d.py"];
  const step = { anchors: [{ path: "c.py" }, { path: "a.py" }, { path: "c.py" }, { path: "gone.py" }] };
  assert.deepEqual(L.navFiles(step, files, "", "a.py"), { order: ["c.py", "a.py"], inStep: true });
  // A file outside the step, or a one-file step: every changed file.
  assert.equal(L.navFiles(step, files, "", "b.py").inStep, false);
  assert.equal(L.navFiles({ anchors: [{ path: "a.py" }] }, files, "", "a.py").inStep, false);
  assert.deepEqual(L.navFiles(null, files, "", "a.py").order, files);
});

test("a file's reviews: steps anchoring it, in order, with the matching anchor", () => {
  const walk = {
    steps: [
      { id: "a", anchors: [{ path: "x.py", line_start: 1, line_end: 2 }] },
      { id: "b", anchors: [{ path: "y.py", line_start: 3, line_end: 4 }, { path: "x.py", line_start: 9, line_end: 9 }] },
    ],
    skipped: [{ path: "uv.lock", reason: "lock file" }],
  };
  const hits = L.stepsForFile(walk, "x.py");
  assert.deepEqual(hits.map((h) => [h.step.id, h.anchor.line_start]), [["a", 1], ["b", 9]]);
  assert.deepEqual(L.stepsForFile(walk, "z.py"), []);
  assert.deepEqual(L.stepsForFile(null, "x.py"), []);
  assert.equal(L.skippedReason(walk, "uv.lock"), "lock file");
  assert.equal(L.skippedReason(walk, "x.py"), null);
  assert.deepEqual([...L.stepFiles(walk.steps[1])], ["y.py", "x.py"]);
  assert.equal(L.stepFiles(null).size, 0);
});

test("summary badges: measured first, then the session's assessments", () => {
  const stats = { badges: [{ id: "size", label: "Small: 10 lines", level: "good", detail: "d" }] };
  const walk = { assessments: [{ topic: "tests", verdict: "concern", title: "Thin tests", detail_md: "**why**" }] };
  const b = L.summaryBadges(stats, walk);
  assert.deepEqual(b.map((x) => [x.source, x.label, x.level]),
    [["measured", "Small: 10 lines", "good"], ["assessed", "Thin tests", "concern"]]);
  assert.notEqual(b[0].key, b[1].key);
  assert.deepEqual(L.summaryBadges(null, null), []);
});

test("clicking a changed row takes its whole contiguous block", () => {
  const rows = [
    { k: "ctx", o: 1, n: 1 }, { k: "del", o: 2, n: null }, { k: "del", o: 3, n: null },
    { k: "add", o: null, n: 2 }, { k: "add", o: null, n: 3 }, { k: "add", o: null, n: 4 },
    { k: "ctx", o: 4, n: 5 }, { k: "del", o: 5, n: null }, { k: "del", o: 6, n: null }, { k: "ctx", o: 7, n: 6 },
  ];
  // A modified block: the head lines of its added rows, from any row in it.
  assert.deepEqual(L.diffBlock(rows, 1), { side: "head", line_start: 2, line_end: 4 });
  assert.deepEqual(L.diffBlock(rows, 5), { side: "head", line_start: 2, line_end: 4 });
  // A block that only deletes: its base lines.
  assert.deepEqual(L.diffBlock(rows, 8), { side: "base", line_start: 5, line_end: 6 });
  assert.equal(L.diffBlock(rows, 0), null);
  assert.equal(L.diffBlock(rows, 99), null);
});

test("references format and parse back, and hand edits are honoured", () => {
  const files = ["a/b.py", "c.md"];
  assert.equal(L.formatRef("a/b.py", { side: "head", line_start: 2, line_end: 4 }), "@a/b.py:2-4");
  assert.equal(L.formatRef("a/b.py", { side: "head", line_start: 7, line_end: 7 }), "@a/b.py:7");
  assert.equal(L.formatRef("a/b.py", { side: "base", line_start: 5, line_end: 6 }), "@a/b.py:base:5-6");
  assert.equal(L.formatRef("c.md", null), "@c.md");
  const refs = L.parseRefs("why @a/b.py:9-3, and @c.md? not @nope.py:1 or me@a/b.py:1 @a/b.py:base:5.", files);
  assert.deepEqual(refs, [
    { path: "a/b.py", side: "head", line_start: 3, line_end: 9 },
    { path: "c.md", side: "head", line_start: null, line_end: null },
    { path: "a/b.py", side: "base", line_start: 5, line_end: 5 },
  ]);
  assert.deepEqual(L.refAnchor(refs), { path: "a/b.py", side: "head", line_start: 3, line_end: 9 });
  assert.equal(L.refAnchor(L.parseRefs("@c.md", files)), null);
  assert.deepEqual(L.parseRefs("@a/b.py:0", files), []);
});

test("@ completion finds the word being typed and ranks file names first", () => {
  assert.deepEqual(L.completion("look at @ser", 12), { start: 8, query: "ser" });
  assert.deepEqual(L.completion("@", 1), { start: 0, query: "" });
  assert.equal(L.completion("mail@x", 6), null);
  assert.equal(L.completion("@a.py:12", 8), null);
  assert.equal(L.completion("@a.py done", 10), null);
  const files = ["app/service.py", "tests/test_service.py", "docs/services.md", "server/app.py"];
  assert.deepEqual(L.matchFiles(files, "serv"), ["app/service.py", "docs/services.md", "tests/test_service.py", "server/app.py"]);
  assert.deepEqual(L.matchFiles(files, "SERVICE"), ["app/service.py", "docs/services.md", "tests/test_service.py"]);
  assert.equal(L.matchFiles(files, "", 2).length, 2);
});

test("each block of changed rows starts once, keyed by its line range", () => {
  const rows = [
    { k: "ctx", o: 1, n: 1 }, { k: "del", o: 2, n: null }, { k: "add", o: null, n: 2 },
    { k: "add", o: null, n: 3 }, { k: "ctx", o: 3, n: 4 }, { k: "del", o: 4, n: null }, { k: "ctx", o: 5, n: 5 },
  ];
  const starts = L.blockStarts(rows);
  assert.deepEqual([...starts.keys()], [1, 5]);
  assert.equal(L.blockKey(starts.get(1)), "head:2-3");
  assert.equal(L.blockKey(starts.get(5)), "base:4-4");
  assert.equal(L.blockStarts([]).size, 0);
});

test("block explanations: the latest light-bulb question per block of a file", () => {
  const a = { path: "x.py", side: "head", line_start: 2, line_end: 3 };
  const qs = [
    { id: "q1", explain: true, anchor: a },
    { id: "q2", explain: false, anchor: a },
    { id: "q3", explain: true, anchor: Object.assign({}, a, { path: "y.py" }) },
    { id: "q4", explain: true, anchor: a },
  ];
  const m = L.explainByBlock(qs, "x.py");
  assert.deepEqual([...m.keys()], ["head:2-3"]);
  assert.equal(m.get("head:2-3").id, "q4");
});

test("the chat holds every question but block explanations, oldest first", () => {
  const qs = [
    { id: "b", ts: "2026-09-27T10:00:05Z" },
    { id: "x", ts: "2026-09-27T10:00:01Z", explain: true },
    { id: "new" },
    { id: "a", ts: "2026-09-27T10:00:01Z" },
  ];
  assert.deepEqual(L.chatQuestions(qs).map((q) => q.id), ["a", "b", "new"]);
});

test("chat status counts answers being written and finished ones not yet seen", () => {
  const qs = [{ id: "q1", ts: "1" }, { id: "q2", ts: "2" }, { id: "q3", ts: "3" }, { id: "q4", ts: "4", explain: true }];
  const replies = new Map([["q1", { text: "done", done: true }], ["q2", { text: "part", done: false }]]);
  // q3 has no reply at all yet: pending too. q4 is a block explanation: not counted.
  assert.deepEqual(L.chatStatus(qs, replies, new Set()), { pending: 2, unread: 1 });
  assert.deepEqual(L.chatStatus(qs, replies, new Set(["q1"])), { pending: 2, unread: 0 });
});

test("adding a reference never repeats one; overlapping ones merge", () => {
  const files = ["a.py", "b.py"];
  const blk = { side: "head", line_start: 10, line_end: 14 };
  let r = L.mergeRef("", files, "a.py", blk);
  assert.equal(r.text, "@a.py:10-14 ");
  // The same block again: unchanged.
  assert.equal(L.mergeRef(r.text, files, "a.py", blk).text, "@a.py:10-14 ");
  // A line inside it: unchanged.
  assert.equal(L.mergeRef("why @a.py:10-14 here", files, "a.py", { side: "head", line_start: 12, line_end: 12 }).text,
    "why @a.py:10-14 here");
  // A line first, then its block: one reference, widened.
  r = L.mergeRef("why @a.py:12?", files, "a.py", blk);
  assert.equal(r.text, "why @a.py:10-14?");
  assert.equal(r.caret, "why @a.py:10-14".length);
  // Two references a new block bridges: merged into one.
  assert.equal(L.mergeRef("@a.py:3 and @a.py:20", files, "a.py", { side: "head", line_start: 4, line_end: 19 }).text,
    "@a.py:3-20 and");
  // Another file, another side, or a gap: added, not merged.
  assert.equal(L.mergeRef("@a.py:3", files, "b.py", blk).text, "@a.py:3 @b.py:10-14 ");
  assert.equal(L.mergeRef("@a.py:12", files, "a.py", { side: "base", line_start: 12, line_end: 12 }).text,
    "@a.py:12 @a.py:base:12 ");
  assert.equal(L.mergeRef("@a.py:1", files, "a.py", { side: "head", line_start: 5, line_end: 6 }, 0).text,
    "@a.py:5-6 @a.py:1");
});

test("reply threads keep the latest part's timestamp", () => {
  const t = L.groupReplies([{ reply_to: "q", text: "a", done: false, ts: "2026-01-01T10:00:00Z" },
    { reply_to: "q", text: "b", done: true, ts: "2026-01-01T10:01:00Z" }]);
  assert.equal(t.get("q").ts, "2026-01-01T10:01:00Z");
});

test("wordDiff marks the changed words of a changed line", () => {
  const d = L.wordDiff("const x = foo(a, b);", "const x = bar(a, c);");
  assert.deepEqual(d.del, [[10, 13], [17, 18]]);
  assert.deepEqual(d.add, [[10, 13], [17, 18]]);
  // Changed words with only a space between them become one mark.
  assert.deepEqual(L.wordDiff("return old value;", "return new thing;").add, [[7, 16]]);
  // A line rewritten from scratch gets no marks: the whole line is the change.
  assert.equal(L.wordDiff("alpha beta gamma", "one two three"), null);
  assert.equal(L.wordDiff("a ".repeat(400), "b ".repeat(400)), null);
});

test("changePairs pairs the i-th deleted row with the i-th added row", () => {
  const rows = [{ k: "ctx" }, { k: "del" }, { k: "del" }, { k: "add" }, { k: "ctx" }, { k: "add" }];
  const p = L.changePairs(rows);
  assert.equal(p.get(1), 3);
  assert.equal(p.get(3), 1);
  assert.equal(p.has(2), false);
  assert.equal(p.has(5), false);
});

test("markRanges wraps text offsets without breaking the highlighter's tags", () => {
  const html = '<span class="k">const</span> x &lt; y';
  assert.equal(L.markRanges(html, [[4, 9]], "wd"),
    '<span class="k">cons<mark class="wd">t</mark></span><mark class="wd"> x &lt;</mark> y');
  assert.equal(L.markRanges(html, [], "wd"), html);
});

test("splitLines pairs deletions left with additions right", () => {
  const rows = [{ k: "ctx" }, { k: "del" }, { k: "del" }, { k: "add" }, { k: "ctx" }, { k: "gap" }, { k: "add" }];
  const items = rows.map((_, i) => ({ row: i }));
  items.splice(4, 1, { fold: [4, 4] });
  assert.deepEqual(L.splitLines(rows, items), [
    { left: 0, right: 0 }, { left: 1, right: 3 }, { left: 2, right: null },
    { fold: [4, 4] }, { gap: 5 }, { left: null, right: 6 },
  ]);
});

test("oldNumbers fills base line numbers on unchanged rows", () => {
  const rows = [{ k: "ctx", n: 1 }, { k: "add", n: 2 }, { k: "ctx", n: 3 }, { k: "del", o: 3 },
    { k: "del", o: 4 }, { k: "ctx", n: 4 }, { k: "gap", t: "…" }, { k: "ctx", n: 40 }];
  assert.deepEqual(L.oldNumbers(rows), [1, null, 2, 3, 4, 5, null, null]);
});

test("enclosingScope finds the nearest opening line above", () => {
  const rows = [{ k: "ctx", t: "class A:" }, { k: "ctx", t: "    def run(self):" },
    { k: "ctx", t: "        x = 1" }, { k: "del", t: "def gone():" }, { k: "add", t: "        y = 2" }];
  assert.equal(L.enclosingScope(rows, 4), "def run(self):");
  assert.equal(L.enclosingScope(rows, 0), "class A:");
  assert.equal(L.enclosingScope([{ k: "ctx", t: "const go = async () => {" }], 0), "const go = async () => {");
  assert.equal(L.enclosingScope([{ k: "ctx", t: "x = 1" }], 0), null);
});

test("stepBlock moves between blocks of changes", () => {
  const starts = [5, 20, 40];
  assert.equal(L.stepBlock(starts, 0, 1), 0);
  assert.equal(L.stepBlock(starts, 5, 1), 1);
  assert.equal(L.stepBlock(starts, 25, 1), 2);
  assert.equal(L.stepBlock(starts, 40, 1), -1);
  assert.equal(L.stepBlock(starts, 25, -1), 1);
  assert.equal(L.stepBlock(starts, 20, -1), 0);
  assert.equal(L.stepBlock(starts, 3, -1), -1);
  assert.equal(L.stepBlock([], 3, 1), -1);
});

test("currentBlock keeps a block the pane cannot scroll up to the reference line", () => {
  const starts = [5, 20, 40];
  const mid = { top: false, bottom: false };
  const bottom = { top: false, bottom: true };
  const top = { top: true, bottom: false };
  // Away from the ends, the reference line decides.
  assert.equal(L.currentBlock(starts, 25, 40, mid), 1);
  assert.equal(L.currentBlock(starts, 2, null, top), -1);
  // At the bottom, the last block stepped to wins even though row 40 never
  // reaches the line.
  assert.equal(L.currentBlock(starts, 25, 40, bottom), 2);
  // At the top, a block above the line wins the same way.
  assert.equal(L.currentBlock(starts, 25, 5, top), 0);
  // A pin behind the line in the wrong direction is ignored.
  assert.equal(L.currentBlock(starts, 25, 5, bottom), 1);
  // A pin that is no longer a block start is ignored.
  assert.equal(L.currentBlock(starts, 25, 41, bottom), 1);
});

test("stepCurrent steps on from a pinned block at the end of the scroll range", () => {
  const starts = [5, 20, 40];
  const bottom = { top: false, bottom: true };
  const both = { top: true, bottom: true };
  // The reference line is stuck in block 1; Next reaches 2, then stops.
  assert.equal(L.stepCurrent(starts, 25, null, bottom, 1), 2);
  assert.equal(L.stepCurrent(starts, 25, 40, bottom, 1), -1);
  assert.equal(L.stepCurrent(starts, 25, 40, bottom, -1), 1);
  // A file that fits on screen: every block is reachable with Next and Prev.
  assert.equal(L.stepCurrent(starts, 6, 20, both, 1), 2);
  assert.equal(L.stepCurrent(starts, 21, 5, both, 1), 1);
  assert.equal(L.stepCurrent(starts, 21, 5, both, -1), -1);
  // Without a pin it behaves like stepBlock.
  assert.equal(L.stepCurrent(starts, 25, null, { top: false, bottom: false }, -1), 1);
  // At the top, the current block's start is already on screen above the
  // reference line: Prev goes to the block before it, and there is none
  // before the first, so Prev is disabled there.
  const top = { top: true, bottom: false };
  assert.equal(L.stepCurrent([1, 20, 40], 3, null, top, -1), -1);
  assert.equal(L.stepCurrent([1, 5, 40], 7, null, top, -1), 0);
  assert.equal(L.stepCurrent([1, 20, 40], 3, null, top, 1), 1);
});

test("paletteMatch ranks prefix, then substring, then scattered letters", () => {
  const e = [{ label: "server.py", sub: "review_viewer" }, { label: "observer.js", sub: "static" },
    { label: "setup_rv.sh", sub: "scripts" }, { label: "app.js", sub: "static/server" }];
  assert.deepEqual(L.paletteMatch(e, "serv").map((x) => x.label), ["server.py", "observer.js", "setup_rv.sh", "app.js"]);
  // Scattered letters: the tighter span ranks first.
  assert.deepEqual(L.paletteMatch(e, "srv").map((x) => x.label).slice(0, 3), ["server.py", "observer.js", "setup_rv.sh"]);
  assert.equal(L.paletteMatch(e, "").length, 4);
  assert.deepEqual(L.paletteMatch(e, "zzz"), []);
});

test("viewedCount counts only files in the change", () => {
  assert.deepEqual(L.viewedCount(["a", "b", "c"], { a: "t", gone: "t" }), { done: 1, total: 3 });
  assert.deepEqual(L.viewedCount(["a"], null), { done: 0, total: 1 });
});

const RISK_BASE = { tier: 1, tierWhy: "", revertable: true, revertWhy: "", tests: "good",
  checksFailing: false, large: false, openHigh: 0, openMedium: 0 };
const risk = (over) => L.riskLevel(Object.assign({}, RISK_BASE, over));

test("riskLevel is Low when no rule applies", () => {
  assert.deepEqual(risk({}), { level: "low", reasons: ["no rule for Medium or High applies"] });
  assert.equal(risk({ tier: 2 }).level, "low");
});

test("riskLevel High rules", () => {
  assert.deepEqual(risk({ openHigh: 2 }).reasons, ["2 open HIGH findings"]);
  assert.equal(risk({ tier: 3, revertable: false }).level, "high");
  assert.equal(risk({ tier: 3, tests: "concern" }).level, "high");
  assert.equal(risk({ checksFailing: true }).level, "high");
  // Every true High rule is a reason; Medium rules are not listed.
  assert.deepEqual(risk({ tier: 3, revertable: false, tests: "concern", openMedium: 1 }).reasons, [
    "tier 3 (auth, money, data, shared infra or deploy config) that a revert cannot undo",
    "tier 3 (auth, money, data, shared infra or deploy config) without tests"]);
});

test("riskLevel Medium rules", () => {
  assert.equal(risk({ tier: 3 }).level, "medium");
  assert.equal(risk({ tier: 2, tests: "note" }).level, "medium");
  assert.equal(risk({ tier: 2, tests: null }).level, "medium");
  assert.deepEqual(risk({ openMedium: 1 }).reasons, ["1 open MEDIUM finding"]);
  assert.deepEqual(risk({ large: true }).reasons, ["a large diff"]);
  assert.deepEqual(risk({ revertable: false }).reasons, ["a revert cannot undo it"]);
});

test("riskInputs combines the repo tiers, the session's tier and the findings", () => {
  const walk = { risk: { tier: 1, tier_why: "one feature", revertable: true, revert_why: "x" },
    assessments: [{ topic: "tests", verdict: "note" }] };
  const stats = { tiers: { "infra/main.tf": 3 },
    badges: [{ id: "size", level: "concern" }, { id: "checks", level: "good" }] };
  const findings = [
    { id: "a", status: "confirmed", severity: "HIGH" },
    { id: "b", status: "confirmed", severity: "HIGH" },
    { id: "c", status: "refuted", severity: "MEDIUM" },
    { id: "d", status: "confirmed", severity: "MEDIUM" },
  ];
  const r = L.riskInputs(stats, walk, findings, { b: { decision: "reject" }, a: { decision: "accept" } },
    ["infra/main.tf", "app.py"]);
  assert.deepEqual(r, { tier: 3, tierWhy: "set by the repo's .review-risk.toml", revertable: true,
    revertWhy: "x", tests: "note", checksFailing: false, large: true, openHigh: 1, openMedium: 1 });
  // The file only raises the tier: covering every changed file with a lower
  // tier leaves the session's tier and reason in place.
  const low = L.riskInputs({ tiers: { "a.md": 0 } }, walk, [], {}, ["a.md"]);
  assert.equal(low.tier, 1);
  assert.equal(low.tierWhy, "one feature");
  // With no file the session decides, and its reason is shown.
  assert.equal(L.riskInputs({}, walk, [], {}, ["app.py"]).tierWhy, "one feature");
  assert.equal(L.riskInputs({}, { assessments: [] }, [], {}, []), null);
});

test("a review whose base is the empty tree is a tree review", () => {
  assert.equal(L.isTreeReview({ base_sha: L.EMPTY_TREE, head_sha: "a".repeat(40) }), true);
  assert.equal(L.isTreeReview({ base_sha: "b".repeat(40), head_sha: "a".repeat(40) }), false);
  assert.equal(L.isTreeReview(null), false);
});

test("the Needs you block is hidden without needs, and each item says what it blocks", () => {
  assert.deepEqual(L.needsItems({ findings: [] }), []);
  assert.deepEqual(L.needsItems(null), []);
  assert.deepEqual(L.needsItems({ findings: [], needs: [] }), []);
  const ff = {
    findings: [{ id: "a1b2c3d4e5", file: "app.py", line_start: 7, claim: "The timeout is ignored." }],
    needs: [
      { what: "The vendor's probe defaults.", blocks: "a1b2c3d4e5", cause: "unreachable", command: "", source: "verifier" },
      { what: "Rules on main.", blocks: "review", cause: "lookup-failed", command: "gh api repos/o/r/rules/branches/main", source: "pipeline" },
      { what: "x", blocks: "ffffffffff", cause: "no-access", command: "", source: "verifier" },
    ],
  };
  const items = L.needsItems(ff);
  assert.equal(items.length, 3);
  assert.equal(items[0].findingId, "a1b2c3d4e5");
  assert.equal(items[0].blocks, "app.py:7 — The timeout is ignored.");
  assert.equal(items[0].cause, "not reachable on the web");
  assert.equal(items[1].findingId, null);
  assert.equal(items[1].blocks, "the review");
  assert.equal(items[1].command, "gh api repos/o/r/rules/branches/main");
  assert.equal(items[2].blocks, "finding ffffffffff");
  assert.equal(items[0].where, "");
});

test("the Needs you tab groups items by what they block, the review's own gaps first", () => {
  assert.deepEqual(L.needsGroups(null), []);
  const ff = {
    findings: [{ id: "a1b2c3d4e5", file: "app.py", line_start: 7, claim: "The timeout is ignored." }],
    needs: [
      { what: "Probe defaults.", blocks: "a1b2c3d4e5", cause: "unreachable", command: "", source: "verifier",
        where: "The vendor's docs, Health checks page" },
      { what: "Rules on main.", blocks: "review", cause: "lookup-failed", command: "gh api x", source: "pipeline" },
      { what: "Deployed config.", blocks: "a1b2c3d4e5", cause: "no-access", command: "", source: "verifier" },
    ],
  };
  const groups = L.needsGroups(ff);
  assert.deepEqual(groups.map((g) => [g.blocks, g.findingId, g.items.length]),
    [["the review", null, 1], ["app.py:7 — The timeout is ignored.", "a1b2c3d4e5", 2]]);
  assert.equal(groups[1].items[0].where, "The vendor's docs, Health checks page");
});

test("asking the session about a need names it and what it blocks", () => {
  const q = L.needQuestion({ what: "The column type.", blocks: "app.py:7 — The timeout is ignored." });
  assert.match(q, /The column type\./);
  assert.match(q, /It blocks: app\.py:7 — The timeout is ignored\./);
  assert.match(L.needQuestion({ what: "w", blocks: "the review" }), /It blocks: the review as a whole/);
});

const RUN = {
  started_at: "2026-10-01T10:00:00Z", ended_at: "2026-10-01T10:07:12Z", wall_seconds: 432.4,
  calls: 16, tokens: { input: 1200, output: 3400, cache_read: 1418951, cache_write: 0, total: 1423551 },
  cost_usd: 4.105, max_usd: 50, stopped_by_budget: false, errors: 0,
  stages: [
    { name: "find:diff-bugs", stage: "find", calls: 1, tokens: { total: 90000 }, cost_usd: 0.5, wall_seconds: 61,
      model: "session default", effort: "session default" },
    { name: "find:tests", stage: "find", calls: 2, tokens: { total: 1000 }, cost_usd: 0.25, wall_seconds: 30,
      model: "session default", effort: "session default" },
    { name: "verify", stage: "verify", calls: 13, tokens: { total: 1332551 }, cost_usd: 3.355, wall_seconds: 300,
      model: "claude-x", effort: "high" },
  ],
  counts: { found: 5, rejected: 1, refuted: 1, unverifiable: 0, merged: 0 },
};

test("run numbers: USD to 2 decimals, tokens with separators, seconds as Xm Ys", () => {
  assert.equal(L.formatUsd(4.105), "$4.11");
  assert.equal(L.formatUsd(0), "$0.00");
  assert.equal(L.formatTokens(1423551), "1,423,551");
  assert.equal(L.formatTokens(999), "999");
  assert.equal(L.formatTokens(1000), "1,000");
  assert.equal(L.formatSeconds(432.4), "7m 12s");
  assert.equal(L.formatSeconds(45), "45s");
  assert.equal(L.formatSeconds(60), "1m 0s");
});

test("the Run block is hidden when findings.json has no run", () => {
  assert.equal(L.runBlock(undefined), null);
  assert.equal(L.runBlock(null), null);
});

test("the Run block has totals, one model line per stage, and a row per stage label", () => {
  const b = L.runBlock(RUN);
  const totals = Object.fromEntries(b.totals);
  assert.equal(totals.Cost, "$4.11 of $50.00 ceiling");
  assert.equal(totals.Tokens, "1,423,551 (input 1,200, output 3,400, cache read 1,418,951, cache write 0)");
  assert.equal(totals["Agent calls"], "16");
  assert.equal(totals["Wall time"], "7m 12s");
  assert.equal(totals.Errors, undefined);
  assert.deepEqual(b.models, [{ stage: "find", model: "session default", effort: "session default" },
    { stage: "verify", model: "claude-x", effort: "high" }]);
  assert.deepEqual(b.stages.map((s) => [s.name, s.calls, s.tokens, s.cost, s.time]), [
    ["find:diff-bugs", "1", "90,000", "$0.50", "1m 1s"],
    ["find:tests", "2", "1,000", "$0.25", "30s"],
    ["verify", "13", "1,332,551", "$3.35", "5m 0s"],
  ]);
  const stopped = L.runBlock(Object.assign({}, RUN, { max_usd: null, stopped_by_budget: true, errors: 2 }));
  const t2 = Object.fromEntries(stopped.totals);
  assert.equal(t2.Cost, "$4.11 (no ceiling)");
  assert.match(t2["Wall time"], /stopped by the budget/);
  assert.equal(t2.Errors, "2");
});

test("the total line needs two reviews with a run, and skips those without", () => {
  assert.equal(L.runTotal([{ run: RUN }]), null);
  assert.equal(L.runTotal([{ run: RUN }, { run: null }]), null);
  assert.equal(L.runTotal([{ run: RUN }, { run: RUN }, { slug: "old" }]),
    "All 2 reviews: $8.21, 2,847,102 tokens, 32 agent calls, 14m 25s wall time");
});

// --- the Checked tab ------------------------------------------------------------

const cite = (gate, seen) => ({ path: "a.py", line_start: 1, line_end: 1, quote: "x", gate: gate, seen: seen });
const CHECKS = {
  agents: [
    { stage: "verify", subject: "aaaaaaaaaa", started_at: "2026-10-09T10:00:05Z", calls: [], findings: [],
      outcome: "unverifiable (answered confirmed)", error: "",
      verdict: { status: "unverifiable", reason: "r", citations: [], lowered: true } },
    { stage: "find", subject: "diff-bugs", started_at: "2026-10-09T10:00:00Z", error: "", outcome: "3 findings",
      calls: [{ tool: "Read", target: "a.py", detail: "whole file" }, { tool: "Read", target: "a.py", detail: "lines 1-5" },
        { tool: "Read", target: "b.py", detail: "whole file" }, { tool: "Grep", target: "x", detail: "in ." }],
      findings: [
        { id: "aaaaaaaaaa", claim: "A", severity: "HIGH", fate: "kept", citations: [cite("pass", "read")] },
        { id: "bbbbbbbbbb", claim: "B", severity: "LOW", fate: "rejected", rule: "quote not at cited lines",
          citations: [cite("fail", "not-seen")] },
        { id: "cccccccccc", claim: "C", severity: "MEDIUM", fate: "merged", into: "aaaaaaaaaa", citations: [] },
      ] },
    { stage: "find", subject: "callers", started_at: "2026-10-09T10:00:00Z", error: "x", outcome: "failed: timeout",
      calls: [], findings: [] },
    { stage: "find", subject: "tests", started_at: "2026-10-09T10:00:01Z", error: "", outcome: "1 finding", calls: [],
      findings: [{ id: "aaaaaaaaaa", claim: "A", severity: "HIGH", fate: "deduped", into: "aaaaaaaaaa", citations: [] }] },
    { stage: "merge", subject: "aaaaaaaaaa,cccccccccc", started_at: "2026-10-09T10:00:09Z", error: "",
      outcome: "merged cccccccccc into aaaaaaaaaa", calls: [], findings: [] },
  ],
  gates: [],
};

test("agents are ordered by start time, ties keeping file order", () => {
  assert.deepEqual(L.agentOrder(CHECKS).map((a) => a.subject),
    ["diff-bugs", "callers", "tests", "aaaaaaaaaa", "aaaaaaaaaa,cccccccccc"]);
  assert.deepEqual(L.agentOrder(null), []);
});

test("the checklist has one row per finder with files read and fates", () => {
  const rows = L.finderRows(CHECKS);
  assert.deepEqual(rows[0], { subject: "diff-bugs", ran: "ran", filesRead: 2, returned: 3,
    kept: 1, deduped: 0, merged: 1, rejected: 1 });
  assert.equal(rows[1].ran, "failed");
  assert.equal(rows[2].deduped, 1);
});

test("the checklist has one row per finding with its verdict and gate, deduped copies left out", () => {
  const rows = L.checkedFindingRows(CHECKS);
  assert.deepEqual(rows.map((r) => [r.id, r.verdict, r.gate]), [
    ["aaaaaaaaaa", "unverifiable", "lowered by verdict_gate"],
    ["bbbbbbbbbb", "not verified", "rejected: quote not at cited lines"],
    ["cccccccccc", "not verified", "passed"],
  ]);
  assert.equal(rows[2].into, "aaaaaaaaaa");
});

test("merge rows, the verifier of a finding, and which reads link to the diff", () => {
  assert.deepEqual(L.mergeRows(CHECKS), [{ subject: "aaaaaaaaaa,cccccccccc",
    outcome: "merged cccccccccc into aaaaaaaaaa", failed: false }]);
  assert.equal(L.verifierIndex(CHECKS, "aaaaaaaaaa"), 3);
  assert.equal(L.verifierIndex(CHECKS, "bbbbbbbbbb"), -1);
  assert.equal(L.callLinksToDiff({ tool: "Read", target: "a.py" }, ["a.py"]), true);
  assert.equal(L.callLinksToDiff({ tool: "Read", target: "c.py" }, ["a.py"]), false);
  assert.equal(L.callLinksToDiff({ tool: "WebFetch", target: "a.py" }, ["a.py"]), false);
});

test("a failed gate or an unseen citation is a warning", () => {
  assert.equal(L.citationWarning(cite("pass", "read")), false);
  assert.equal(L.citationWarning(cite("fail", "read")), true);
  assert.equal(L.citationWarning(cite("web", "not-seen")), true);
  assert.equal(L.citationWarning(cite("pass", "diff")), false);
});

// --- what a finding's detail shows ----------------------------------------------------

const FULL = { id: "ffffffffff", failure_scenario: "s", expected_behaviour: "e", verdict_reason: "v",
  citations: [cite("pass", "read")], needs: [{ what: "w", blocks: "ffffffffff", cause: "no-access", command: "" }],
  verifier_citations: [cite("pass", "read")] };
// The verifier of FULL started first, so its entry is agent 0 in agentOrder.
const CHECKS_FIRST = { agents: [
  { stage: "find", subject: "diff-bugs", started_at: "2026-10-09T10:00:05Z", calls: [], findings: [] },
  { stage: "verify", subject: "ffffffffff", started_at: "2026-10-09T10:00:00Z", calls: [], findings: [],
    verdict: { status: "confirmed", reason: "r", citations: [], lowered: false } },
], gates: [] };

test("Findings tab: a verifier entry at agent 0 gets the Checked tab link and no citation lists", () => {
  assert.equal(L.agentOrder(CHECKS_FIRST)[0].subject, "ffffffffff");
  assert.deepEqual(L.findingParts(FULL, CHECKS_FIRST, "finding"),
    { checkedBy: 0, parts: ["scenario", "expected", "verdict", "needs"] });
  // A later index works the same way.
  const f = Object.assign({}, FULL, { id: "aaaaaaaaaa" });
  assert.equal(L.findingParts(f, CHECKS, "finding").checkedBy, 3);
  assert.ok(!L.findingParts(f, CHECKS, "finding").parts.includes("cited"));
});

test("Findings tab: checks without this finding's verifier, or no checks at all, keep the citation lists", () => {
  const full = ["scenario", "expected", "verdict", "cited", "needs", "verifier-cited"];
  assert.deepEqual(L.findingParts(Object.assign({}, FULL, { id: "bbbbbbbbbb" }), CHECKS, "finding"),
    { checkedBy: -1, parts: full });
  assert.deepEqual(L.findingParts(FULL, null, "finding"), { checkedBy: -1, parts: full });
  assert.deepEqual(L.findingParts(FULL, undefined, "finding"), { checkedBy: -1, parts: full });
});

test("a walkthrough step always lists the citations and never links the Checked tab", () => {
  assert.deepEqual(L.findingParts(FULL, CHECKS_FIRST, "step"),
    { checkedBy: -1, parts: ["scenario", "expected", "verdict", "cited", "needs", "verifier-cited"] });
  assert.deepEqual(L.findingParts(FULL, null, "step").parts,
    ["scenario", "expected", "verdict", "cited", "needs", "verifier-cited"]);
});

test("parts a finding lacks are left out", () => {
  const bare = { id: "cccccccccc", failure_scenario: "s", citations: [cite("pass", "read")] };
  assert.deepEqual(L.findingParts(bare, null, "finding"), { checkedBy: -1, parts: ["scenario", "cited"] });
  assert.deepEqual(L.findingParts(Object.assign({}, bare, { needs: [], verifier_citations: [] }), null, "step").parts,
    ["scenario", "cited"]);
});

// --- the help dialog ----------------------------------------------------------------

const fs = require("node:fs");
const STATIC = path.join(__dirname, "..", "review_viewer", "static");
const SCHEMA = path.join(__dirname, "..", "..", "schema");
const pageHtml = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const helpHtml = (pageHtml.match(/<dialog id="help"[\s\S]*?<\/dialog>/) || [""])[0];
const helpIds = new Set([...helpHtml.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
const unescape = (t) => t.replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
// The words the help defines: every <dt> and <code> in it, as text.
const helpTerms = new Set([...helpHtml.matchAll(/<(dt|code)>([\s\S]*?)<\/\1>/g)].map((m) => unescape(m[2])));

test("every ? opens a section the help dialog has, and an unknown topic opens its top", () => {
  assert.ok(helpHtml, "index.html has a <dialog id=\"help\">");
  for (const [topic, id] of Object.entries(L.HELP_SECTIONS)) {
    assert.ok(helpIds.has(id), "help topic " + topic + " names #" + id + ", which the dialog lacks");
    assert.equal(L.helpSection(topic), id);
  }
  assert.equal(L.helpSection("no-such-topic"), "help-top");
  assert.equal(L.helpSection("constructor"), "help-top");
  assert.equal(L.helpSection(undefined), "help-top");
});

test("every link inside the help points at a section of the help", () => {
  const links = [...helpHtml.matchAll(/href="#([^"]+)"/g)].map((m) => m[1]);
  assert.ok(links.length > 5);
  for (const id of links) assert.ok(helpIds.has(id), "#" + id + " is linked from the help but missing");
});

test("the help explains every value the Checked and Findings tabs can show", () => {
  const checks = JSON.parse(fs.readFileSync(path.join(SCHEMA, "checks.v1.schema.json"), "utf8")).$defs;
  const findings = JSON.parse(fs.readFileSync(path.join(SCHEMA, "findings.v1.schema.json"), "utf8")).$defs;
  const groups = {
    seen: checks.MarkedCitation.properties.seen.enum.map((v) => "seen: " + v),
    gate: checks.MarkedCitation.properties.gate.enum.map((v) => "gate: " + v),
    fate: checks.CheckedFinding.properties.fate.enum,
    verdict: checks.CheckedVerdict.properties.status.enum,
    status: findings.Finding.properties.status.enum,
    // `useful` shows no badge, so the help names it in prose only.
    label: findings.Assessment.properties.label.enum.filter((v) => v !== "useful"),
  };
  for (const [name, values] of Object.entries(groups)) {
    assert.ok(values.length, name + " has values in the schema");
    for (const v of values) assert.ok(helpTerms.has(v), "the help does not define " + name + " \"" + v + "\"");
  }
  for (const column of ["ran", "files read", "returned", "kept", "deduped", "merged", "rejected",
    "finding", "finder", "verdict", "gate", "fate", "group", "outcome"]) {
    assert.ok(helpTerms.has(column), "the help does not explain the Checked tab's \"" + column + "\" column");
  }
});

test("a GitHub link to lines takes https and ssh remotes, and nothing else", () => {
  const sha = "454e493f82f28689553833f91c510af68e10eb7e";
  assert.equal(L.githubBlobUrl("https://github.com/DolceTech/DolceDataform.git", sha, "docs/history.md", 194, 198),
    "https://github.com/DolceTech/DolceDataform/blob/" + sha + "/docs/history.md#L194-L198");
  assert.equal(L.githubBlobUrl("git@github.com:o/r.git", sha, "a b.py", 3, 3),
    "https://github.com/o/r/blob/" + sha + "/a%20b.py#L3");
  assert.equal(L.githubBlobUrl("https://gitlab.com/o/r.git", sha, "a.py", 1, 1), null);
  assert.equal(L.githubBlobUrl("https://github.com/o/r", "not-a-sha", "a.py", 1, 1), null);
});

test("a finding copies as markdown with where, scenario, explanation and cited code", () => {
  const f = { id: "a1b2c3d4e5", severity: "MEDIUM", status: "confirmed", claim: "The timeout is ignored.",
    file: "app.py", line_start: 7, line_end: 9, topic: "code", failure_scenario: "A slow call hangs.",
    expected_behaviour: "It stops after 5 s.", verdict_reason: "Line 8 drops it.",
    citations: [{ path: "app.py", line_start: 8, line_end: 8, quote: "call(url)" }] };
  const md = L.findingMarkdown(f, { label: "o/r PR #1", head_sha: "a".repeat(40),
    remote_url: "https://github.com/o/r.git" }, "Worth fixing.");
  assert.match(md, /^### \[MEDIUM\] The timeout is ignored\.\n/);
  assert.match(md, /`app\.py:7-9` · code · https:\/\/github\.com\/o\/r\/blob\/a{40}\/app\.py#L7-L9/);
  assert.match(md, /\*\*Failure scenario:\*\* A slow call hangs\./);
  assert.match(md, /\*\*Explanation:\*\* Worth fixing\./);
  assert.match(md, /\*\*Verifier \(confirmed\):\*\* Line 8 drops it\./);
  assert.match(md, /- `app\.py:8`\n\n  ```\n  call\(url\)\n  ```/);
  assert.match(md, /_From the review of o\/r PR #1 at aaaaaaaa, finding a1b2c3d4e5\._$/);
  assert.doesNotMatch(L.findingMarkdown({ ...f, expected_behaviour: "" }, {}, ""), /Explanation|Expected/);
});


test("a finding's topic is the verifier's, else a guess from its path", () => {
  assert.deepEqual(L.findingTopic({ topic: "process", file: "docs/history.md" }), { topic: "process", guessed: false });
  const guess = (file) => L.findingTopic({ file: file }).topic;
  assert.equal(guess("tests/test_needs.py"), "tests");
  assert.equal(guess("src/app.test.ts"), "tests");
  assert.equal(guess("pkg/thing_test.go"), "tests");
  assert.equal(guess("docs/history.md"), "docs");
  assert.equal(guess("README.md"), "docs");
  assert.equal(guess("infra/main.tf"), "infra");
  assert.equal(guess(".github/workflows/ci.yml"), "infra");
  assert.equal(guess("Dockerfile"), "infra");
  assert.equal(guess("workflow_settings.yaml"), "config");
  assert.equal(guess("includes/pii_utils.js"), "code");
  assert.equal(L.findingTopic({ file: "a.py" }).guessed, true);
});

test("a downloaded finding is named by severity, file name and id", () => {
  assert.equal(L.findingFileName({ id: "a1b2c3d4e5", severity: "MEDIUM", file: "docs/history notes.md" }),
    "finding-medium-history-notes.md-a1b2c3d4e5.md");
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
