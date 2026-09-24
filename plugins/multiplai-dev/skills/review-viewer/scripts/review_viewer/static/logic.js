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
    let m;
    while ((m = re.exec(html)) !== null) {
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

  const api = {
    SEVERITIES: SEVERITIES, joinParts: joinParts, groupReplies: groupReplies,
    isPending: isPending, pollDelay: pollDelay, citationRows: citationRows,
    isHidden: isHidden, groupFindings: groupFindings, findingOrder: findingOrder,
    stepFinding: stepFinding, anchorLabel: anchorLabel, escapeHtml: escapeHtml,
    splitHighlighted: splitHighlighted, lineRange: lineRange,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ReviewLogic = api;
})(typeof window !== "undefined" ? window : this);
