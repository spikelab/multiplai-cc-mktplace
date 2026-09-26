/* Pure functions behind the review-viewer page.
 *
 * No DOM, no network: everything here takes data and returns data, so
 * `node tests/logic.test.js` can check it without a browser. In the page these
 * functions are available as `ReviewLogic.<name>`.
 */
(function (root) {
  "use strict";

  const SEVERITIES = ["HIGH", "MEDIUM", "LOW"];
  const HIDDEN_STATUSES = new Set(["refuted", "rejected"]);

  /* Join the parts of one answer. Parts split by `reply` to fit the row size
   * limit end on a line break and are joined as they are; separate replies
   * (a `--more` part, then the rest) get a blank line between them. */
  function joinParts(parts) {
    let out = "";
    for (const part of parts) {
      if (out && !out.endsWith("\n")) out += "\n\n";
      out += part;
    }
    return out;
  }

  /* Outbox rows, in file order → Map(reply_to → {parts, text, done}).
   * A thread is done when its latest part says so: a `--more` part keeps the
   * spinner running until a `done: true` part arrives. */
  function groupReplies(rows) {
    const threads = new Map();
    for (const row of rows || []) {
      if (!row || typeof row.reply_to !== "string") continue;
      let t = threads.get(row.reply_to);
      if (!t) {
        t = { parts: [], text: "", done: false };
        threads.set(row.reply_to, t);
      }
      t.parts.push(String(row.text == null ? "" : row.text));
      t.text = joinParts(t.parts);
      t.done = row.done === true;
    }
    return threads;
  }

  /* Should the spinner for question `id` still run? */
  function isPending(id, threads) {
    const t = threads.get(id);
    return !t || !t.done;
  }

  /* Fold one /api/poll response into what the page already holds.
   * `current` is {rows, since}; `askedSince` is the `since` the request was
   * sent with. A response to an older request is ignored, so two overlapping
   * polls can never append the same rows twice. Returns the new {rows, since,
   * changed, reset}; `reset` means the outbox shrank and must be read again
   * from the start. */
  function applyPoll(current, askedSince, res) {
    if (askedSince !== current.since) return { rows: current.rows, since: current.since, changed: false, reset: false };
    if (res.n < askedSince) return { rows: [], since: 0, changed: true, reset: true };
    const answers = res.answers || [];
    return {
      rows: answers.length ? current.rows.concat(answers) : current.rows,
      since: res.n,
      changed: answers.length > 0,
      reset: false,
    };
  }

  /* Milliseconds until the next poll. */
  function pollDelay(pendingCount) {
    return pendingCount > 0 ? 2000 : 10000;
  }

  /* Indices of the file-view rows showing head lines start..end. */
  function citationRows(rows, start, end) {
    const out = [];
    (rows || []).forEach(function (row, i) {
      if (row.n != null && row.n >= start && row.n <= end) out.push(i);
    });
    return out;
  }

  function isHidden(finding, decisions) {
    const d = decisions && decisions[finding.id];
    return HIDDEN_STATUSES.has(finding.status) || (d && d.decision === "reject");
  }

  /* Findings by severity, in the input order within each severity. Refuted,
   * rejected, and findings the user rejected are left out unless showHidden. */
  function groupFindings(findings, decisions, showHidden) {
    const groups = { HIGH: [], MEDIUM: [], LOW: [] };
    let hidden = 0;
    for (const f of findings || []) {
      if (isHidden(f, decisions)) {
        hidden += 1;
        if (!showHidden) continue;
      }
      (groups[f.severity] || (groups[f.severity] = [])).push(f);
    }
    return { groups: groups, hidden: hidden };
  }

  /* Ids in the order the sidebar shows them. */
  function findingOrder(grouped) {
    const ids = [];
    for (const sev of SEVERITIES) for (const f of grouped.groups[sev] || []) ids.push(f.id);
    return ids;
  }

  /* The id `delta` steps from `current`, clamped to the list (j/k). */
  function stepFinding(order, current, delta) {
    if (!order.length) return null;
    const i = order.indexOf(current);
    if (i < 0) return delta > 0 ? order[0] : order[order.length - 1];
    return order[Math.max(0, Math.min(order.length - 1, i + delta))];
  }

  function anchorLabel(anchor) {
    if (!anchor) return "";
    const lines = anchor.line_start === anchor.line_end
      ? String(anchor.line_start) : anchor.line_start + "–" + anchor.line_end;
    return anchor.path + ":" + lines;
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Split highlighter HTML (spans only) into one string per source line,
   * closing the spans still open at each line end and reopening them on the
   * next line, so a multi-line string or comment stays coloured row by row. */
  function splitHighlighted(html) {
    const lines = [];
    const open = [];
    let current = "";
    const re = /(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+|<)/g;
    for (const m of String(html).matchAll(re)) {
      if (m[1]) { open.push(m[1]); current += m[1]; }
      else if (m[2]) { open.pop(); current += m[2]; }
      else if (m[3]) {
        lines.push(current + "</span>".repeat(open.length));
        current = open.join("");
      } else current += m[4];
    }
    lines.push(current + "</span>".repeat(open.length));
    return lines;
  }

  /* Line range covered by two row numbers picked in either order. */
  function lineRange(a, b) {
    return a <= b ? [a, b] : [b, a];
  }

  // --- walkthrough ------------------------------------------------------------

  /* Step ids in the order the walkthrough lists them: that order is the
   * session's reading order (purpose, core change, callers, tests, config). */
  function stepOrder(walk) {
    return ((walk && walk.steps) || []).map(function (s) { return s.id; });
  }

  /* The step id `delta` steps from `current`, clamped ([ and ]). With no
   * current step, ] starts at the first and [ at the last. */
  function moveStep(walk, current, delta) {
    return stepFinding(stepOrder(walk), current, delta);
  }

  /* 1-based position of a step and the total, for "Step 2 of 5". */
  function stepPosition(walk, id) {
    const order = stepOrder(walk);
    return { index: order.indexOf(id) + 1, total: order.length };
  }

  /* Changed files an anchor or a skip covers, and findings that must be
   * linked (confirmed, unverifiable) that a step links. */
  function walkCoverage(walk, files, findings) {
    const covered = new Set();
    const linked = new Set();
    for (const s of (walk && walk.steps) || []) {
      for (const a of s.anchors || []) covered.add(a.path);
      for (const id of s.finding_ids || []) linked.add(id);
    }
    for (const k of (walk && walk.skipped) || []) covered.add(k.path);
    const must = (findings || []).filter(function (f) {
      return f.status === "confirmed" || f.status === "unverifiable";
    });
    return {
      files: (files || []).filter(function (f) { return covered.has(f); }).length,
      filesTotal: (files || []).length,
      findings: must.filter(function (f) { return linked.has(f.id); }).length,
      findingsTotal: must.length,
    };
  }

  /* What the walkthrough tab says about its state. */
  function walkStatus(walk) {
    if (!walk) return "Waiting for the walkthrough";
    if (!walk.complete) return "Walkthrough in progress";
    return "";
  }

  /* Indices of the file-view rows an anchor covers: head-side anchors match
   * the new line number, base-side ones the old line number. */
  function anchorRows(rows, anchor) {
    const key = anchor && anchor.side === "base" ? "o" : "n";
    const out = [];
    (rows || []).forEach(function (row, i) {
      const v = row[key];
      if (v != null && v >= anchor.line_start && v <= anchor.line_end) out.push(i);
    });
    return out;
  }

  /* "path:3–5" plus " (base)" for a base-side anchor. */
  function walkAnchorLabel(anchor) {
    return anchorLabel(anchor) + (anchor.side === "base" ? " (base)" : "");
  }

  /* Steps that link a finding, in walkthrough order. */
  function stepsForFinding(walk, id) {
    return ((walk && walk.steps) || []).filter(function (s) {
      return (s.finding_ids || []).indexOf(id) >= 0;
    });
  }

  /* An SVG document as an <img> source. The page's CSP allows `data:` images
   * and no inline styles, so a diagram is shown as an image, never inlined. */
  function svgDataUrl(svg) {
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }

  /* Only GitHub PR links get an <a> in the header. */
  function safePrUrl(url) {
    return typeof url === "string" && /^https:\/\/github\.com\/[^\s"'<>]+$/.test(url) ? url : null;
  }

  const api = {
    SEVERITIES: SEVERITIES, joinParts: joinParts, groupReplies: groupReplies,
    isPending: isPending, pollDelay: pollDelay, applyPoll: applyPoll, citationRows: citationRows,
    isHidden: isHidden, groupFindings: groupFindings, findingOrder: findingOrder,
    stepFinding: stepFinding, anchorLabel: anchorLabel, escapeHtml: escapeHtml,
    splitHighlighted: splitHighlighted, lineRange: lineRange,
    stepOrder: stepOrder, moveStep: moveStep, stepPosition: stepPosition,
    walkCoverage: walkCoverage, walkStatus: walkStatus, anchorRows: anchorRows,
    walkAnchorLabel: walkAnchorLabel, stepsForFinding: stepsForFinding,
    svgDataUrl: svgDataUrl, safePrUrl: safePrUrl,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ReviewLogic = api;
})(typeof window !== "undefined" ? window : this);
