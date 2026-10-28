/* Pure functions of the pipeline page: no DOM, no network, no clock (each
 * function that needs the time takes `now` in milliseconds). app.js uses them
 * in the browser as window.PVLogic; tests/logic.test.js runs them under node. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PVLogic = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const STOP_TITLES = {
    1: "Proposal",
    2: "Use cases and components",
    3: "Specs",
    4: "Implementation plan",
    5: "Pull request",
  };
  const STOP_OBJECTS = {
    1: "the proposal",
    2: "the use cases and components",
    3: "the specs",
    4: "the implementation plan",
    5: "the pull request",
  };
  const ROLE_LABELS = { pm: "PM", engineer: "engineer" };
  const BLOCK_STATUS = {
    pending: "Not started",
    testing: "Writing a failing test",
    implementing: "Writing the code",
    reviewing: "Being reviewed",
    done: "Done",
    failed: "Failed",
  };
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function stopTitle(n) {
    return STOP_TITLES[n] || "Stop " + n;
  }

  /* "Approve the proposal", "Approve the implementation plan". */
  function approveLabel(n) {
    return "Approve " + (STOP_OBJECTS[n] || "stop " + n);
  }

  function reviewLabel(n) {
    return "Review " + (STOP_OBJECTS[n] || "stop " + n);
  }

  function roleLabel(role) {
    return ROLE_LABELS[role] || role || "";
  }

  function blockStatusLabel(status) {
    return BLOCK_STATUS[status] || status || "";
  }

  /* buildme's phase names (`tdd_build`, `plan_review`) as words. */
  function phaseLabel(phase) {
    if (!phase) return "";
    if (phase === "complete") return "Finished";
    const words = String(phase).replace(/[_-]+/g, " ").trim();
    return words.replace(/\btdd\b/gi, "TDD").replace(/^./, (c) => c.toUpperCase());
  }

  /* --- names and people ------------------------------------------------- */

  /* ["Ada", "Bo", "Cy"] → "Ada, Bo and Cy"; the viewer's own name → "you". */
  function namesText(names, me) {
    const list = (names || []).map((n) => (me && n === me ? "you" : n));
    if (list.length === 0) return "";
    if (list.length === 1) return list[0];
    return list.slice(0, -1).join(", ") + " and " + list[list.length - 1];
  }

  function stopOf(change, n) {
    return ((change && change.stops) || []).find((s) => s.n === n) || null;
  }

  function currentStop(change) {
    return change && change.current_stop ? stopOf(change, change.current_stop) : null;
  }

  function isWaitingOn(stop, me) {
    return !!(stop && me && (stop.waiting_on || []).includes(me));
  }

  function isReviewer(stop, me) {
    return !!(stop && me && (stop.reviewers || []).some((r) => r.name === me));
  }

  /* Open review mode on a stop that waits on the viewer, who has not said
   * they are on it yet: the page sends the acknowledgement. */
  function shouldAck(stop, me) {
    if (!stop || stop.n > 4) return false;
    if (stop.state !== "waiting" && stop.state !== "acknowledged") return false;
    return isWaitingOn(stop, me) && !(stop.acknowledged_by || []).includes(me);
  }

  /* Whether the viewer is offered Approve / Ask for changes at the end of a
   * review. The server has the last word (409); this only hides buttons that
   * could not work. */
  function canDecide(stop, me) {
    if (!stop || stop.n > 4 || !isReviewer(stop, me)) return false;
    if (["not_reached", "approved", "decided_by_agent"].includes(stop.state)) return false;
    return !(stop.approved_by || []).includes(me);
  }

  /* --- time --------------------------------------------------------------- */

  function parseTime(iso) {
    if (!iso) return null;
    const t = Date.parse(iso);
    return Number.isNaN(t) ? null : t;
  }

  /* 45 → "less than a minute", 5400 → "1 h 30 min", 200000 → "2 days 7 h". */
  function formatDuration(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "";
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return "less than a minute";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60);
    if (h < 48) return m % 60 ? h + " h " + (m % 60) + " min" : h + " h";
    const d = Math.floor(h / 24);
    return h % 24 ? d + " days " + (h % 24) + " h" : d + " days";
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /* A moment in the viewer's local time: "18:00" today, "tomorrow 09:30",
   * "yesterday 17:05", else "Mon 5 Jan 18:00". */
  function formatClock(iso, now) {
    const t = parseTime(iso);
    if (t == null) return "";
    const d = new Date(t);
    const hm = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(d) - day(new Date(now))) / 86400000);
    if (diff === 0) return hm;
    if (diff === 1) return "tomorrow " + hm;
    if (diff === -1) return "yesterday " + hm;
    return DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()] + " " + hm;
  }

  function since(iso, now) {
    const t = parseTime(iso);
    return t == null ? null : (now - t) / 1000;
  }

  /* One line saying where a stop is, who it waits on and what happens next:
   * "Waiting on Pat Planner for 3 h — Claude decides at 18:00 unless someone replies". */
  function stopLine(stop, opts) {
    const o = opts || {};
    const me = o.me;
    const agent = o.agent || "Claude";
    const now = o.now;
    if (!stop) return "";
    switch (stop.state) {
      case "not_reached":
        return "Not reached yet";
      case "approved":
        if (stop.n === 5) return "Approved";
        return (stop.approved_by || []).length
          ? "Approved by " + namesText(stop.approved_by, me)
          : "Passed without an approval on this page";
      case "decided_by_agent":
        return agent + " decided, because nobody replied in time";
      case "rework_requested":
        return "Changes asked for by " + namesText(stop.rework_by, me) + "; " + agent + " is reworking it";
      default:
        break;
    }
    if (stop.n === 5) {
      return stop.waiting_on && stop.waiting_on.length
        ? "The pull request is open; " + namesText(stop.waiting_on, me) +
          (stop.waiting_on.length === 1 && stop.waiting_on[0] !== me ? " reviews" : " review") + " it on GitHub"
        : "The pull request is open";
    }
    const waited = since(stop.waiting_since, now);
    let line = stop.waiting_on && stop.waiting_on.length
      ? "Waiting on " + namesText(stop.waiting_on, me)
      : "Waiting";
    if (waited != null) line += " for " + formatDuration(waited);
    const acked = stop.acknowledged_by || [];
    if (acked.length) {
      const who = namesText(acked, me);
      line += " — " + who + (acked.length === 1 && who !== "you" ? " is" : " are") +
        " reviewing it, so " + agent + " waits for an answer";
      return line;
    }
    const decide = parseTime(stop.decide_at);
    if (decide != null) {
      line += decide > now
        ? " — " + agent + " decides at " + formatClock(stop.decide_at, now) + " unless someone replies"
        : " — " + agent + " was asked to decide at " + formatClock(stop.decide_at, now);
    }
    return line;
  }

  /* --- the changes list --------------------------------------------------- */

  /* 0 waits on the viewer, 1 waits on someone else, 2 is building, 3 the rest. */
  function changeGroup(change, me) {
    const stop = currentStop(change);
    if (stop && isWaitingOn(stop, me)) return 0;
    if (change.waiting_on && change.waiting_on.length) return 1;
    if (change.build && !change.build.complete) return 2;
    return 3;
  }

  /* Whatever waits on the viewer first; within a group the longest wait
   * first, then by name. Returns a new array. */
  function sortChanges(changes, me) {
    const waitedAt = (c) => {
      const t = parseTime((currentStop(c) || {}).waiting_since);
      return t == null ? Infinity : t;
    };
    return (changes || []).slice().sort((a, b) =>
      changeGroup(a, me) - changeGroup(b, me) ||
      waitedAt(a) - waitedAt(b) ||
      String(a.name).localeCompare(String(b.name)));
  }

  /* --- the build ---------------------------------------------------------- */

  /* Seconds left, estimated as elapsed / blocks done × blocks left; null
   * when nothing is done yet or nothing is left. */
  function estimateRemaining(build) {
    if (!build) return null;
    const done = build.blocks_done || 0;
    const total = build.blocks_total || 0;
    if (done < 1 || total <= done || !(build.elapsed_s > 0)) return null;
    return (build.elapsed_s / done) * (total - done);
  }

  /* Share of the budget spent, 0..1 (may pass 1); null with no budget. */
  function budgetShare(build) {
    if (!build || !(build.max_usd > 0) || build.spent_usd == null) return null;
    return build.spent_usd / build.max_usd;
  }

  function formatUsd(n) {
    return n == null ? "" : "$" + Number(n).toFixed(2);
  }

  /* --- documents and sections --------------------------------------------- */

  function isMarkdown(path) {
    return /\.(md|markdown)$/i.test(path || "");
  }

  function codeLanguage(path) {
    const m = /\.([A-Za-z0-9]+)$/.exec(path || "");
    if (!m) return "plaintext";
    const ext = m[1].toLowerCase();
    return { json: "json", yaml: "yaml", yml: "yaml", py: "python", js: "javascript",
      ts: "typescript", sh: "bash", toml: "ini", html: "xml", txt: "plaintext" }[ext] || "plaintext";
  }

  function collapse(s) {
    return String(s).split(/\s+/).filter(Boolean).join(" ");
  }

  /* A document split at its level-2 headings, outside code fences. Each
   * section keeps its full text, heading line included. Text before the
   * first `## ` is its own section, named after the `# ` title, unless it
   * holds nothing but that title: then it opens the first section instead.
   * A file that is not markdown is one section, shown as code. */
  function splitSections(text, path) {
    const src = String(text || "").replace(/\r\n?/g, "\n");
    if (!isMarkdown(path)) {
      return [{ heading: path || "File", body: src, code: codeLanguage(path) }];
    }
    const lines = src.split("\n");
    const parts = [];
    let cur = { heading: null, lines: [] };
    let fence = null;
    for (const line of lines) {
      const f = /^ {0,3}(`{3,}|~{3,})/.exec(line);
      if (f) {
        if (!fence) fence = f[1];
        else if (f[1][0] === fence[0] && f[1].length >= fence.length && /^ {0,3}[`~]+\s*$/.test(line)) fence = null;
      }
      const h = fence || f ? null : /^ {0,3}##[ \t]+(.+?)[ \t]*#*[ \t]*$/.exec(line);
      if (h && !/^ {0,3}###/.test(line)) {
        parts.push(cur);
        cur = { heading: collapse(h[1]), lines: [line] };
      } else {
        cur.lines.push(line);
      }
    }
    parts.push(cur);
    const intro = parts[0];
    const introText = intro.lines.join("\n").trim();
    const title = (/^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$/m.exec(introText) || [])[1];
    const rest = parts.slice(1);
    const out = [];
    const onlyTitle = introText === "" || /^ {0,3}#[ \t]+[^\n]*$/.test(introText);
    if (rest.length && onlyTitle) {
      if (introText) rest[0] = { heading: rest[0].heading, lines: [introText, ""].concat(rest[0].lines) };
    } else if (introText || !rest.length) {
      out.push({ heading: title ? collapse(title) : "Start of the document", lines: intro.lines });
    }
    for (const p of out.concat(rest)) {
      p.body = p.lines.join("\n").replace(/^\n+|\n+$/g, "") + "\n";
      delete p.lines;
    }
    return out.concat(rest).map((p) => ({ heading: p.heading, body: p.body, code: null }));
  }

  /* The comments file the server writes (`review/stop-<n>.md`): one `## `
   * block per comment, headed `<doc> — <section>`, then `<name>, <time>:`. */
  function parseReviewFile(text) {
    const out = [];
    const blocks = String(text || "").replace(/\r\n?/g, "\n").split(/^## /m).slice(1);
    for (const block of blocks) {
      const nl = block.indexOf("\n");
      const heading = (nl < 0 ? block : block.slice(0, nl)).trim();
      const rest = nl < 0 ? "" : block.slice(nl + 1).replace(/^\n+/, "");
      const sep = heading.indexOf(" — ");
      const doc = sep < 0 ? null : heading.slice(0, sep);
      const section = sep < 0 ? heading : heading.slice(sep + 3);
      const by = /^(.+?), (\S+):\n/.exec(rest);
      out.push({
        doc,
        section,
        by: by ? by[1] : null,
        at: by ? by[2] : null,
        text: (by ? rest.slice(by[0].length) : rest).trim(),
      });
    }
    return out;
  }

  /* Every section of a stop in reading order: the documents to approve
   * first, then the ones shown for reading only. `docs` is
   * [{path, approve, text}]; a document whose text is null is skipped. */
  function reviewSteps(docs) {
    const ordered = (docs || []).filter((d) => d.approve).concat((docs || []).filter((d) => !d.approve));
    const steps = [];
    for (const d of ordered) {
      if (d.text == null) continue;
      const sections = splitSections(d.text, d.path);
      sections.forEach((s, i) => steps.push({
        doc: d.path, approve: !!d.approve, heading: s.heading, body: s.body, code: s.code,
        index: i, count: sections.length, key: d.path + "\u0000" + s.heading + "\u0000" + i,
      }));
    }
    return steps;
  }

  /* --- answers from the session ------------------------------------------- */

  /* Outbox rows grouped by the question they answer, in file order. */
  function groupReplies(rows) {
    const out = new Map();
    for (const r of rows || []) {
      if (!r || typeof r.reply_to !== "string" || typeof r.text !== "string") continue;
      const prev = out.get(r.reply_to);
      if (!prev) {
        out.set(r.reply_to, { text: r.text, done: !!r.done, commit: r.commit || null });
      } else {
        prev.text = prev.text.endsWith("\n") ? prev.text + r.text : prev.text + "\n\n" + r.text;
        prev.done = !!r.done;
        if (r.commit) prev.commit = r.commit;
      }
    }
    return out;
  }

  /* --- diffs ---------------------------------------------------------------- */

  function diffLineKind(line) {
    if (line.startsWith("@@")) return "hunk";
    if (/^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity|rename |old mode|new mode)/.test(line)) return "meta";
    if (line.startsWith("+")) return "add";
    if (line.startsWith("-")) return "del";
    return "ctx";
  }

  /* --- the address ------------------------------------------------------------ */

  /* "#change=a&stop=2&review=1&s=4" → {change:"a", stop:2, review:true, s:4}. */
  function parseHash(hash) {
    const p = new URLSearchParams(String(hash || "").replace(/^#/, ""));
    const num = (k) => (p.has(k) && /^\d+$/.test(p.get(k)) ? Number(p.get(k)) : null);
    return {
      change: p.get("change") || null,
      stop: num("stop"),
      review: p.get("review") === "1",
      s: num("s"),
    };
  }

  function makeHash(route) {
    const p = new URLSearchParams();
    if (route.change) p.set("change", route.change);
    if (route.stop != null) p.set("stop", String(route.stop));
    if (route.review) p.set("review", "1");
    if (route.review && route.s != null) p.set("s", String(route.s));
    const s = p.toString();
    return s ? "#" + s : "#";
  }

  return {
    stopTitle, approveLabel, reviewLabel, roleLabel, blockStatusLabel, phaseLabel,
    namesText, stopOf, currentStop, isWaitingOn, isReviewer, shouldAck, canDecide,
    parseTime, formatDuration, formatClock, stopLine,
    changeGroup, sortChanges, estimateRemaining, budgetShare, formatUsd,
    isMarkdown, codeLanguage, splitSections, parseReviewFile, reviewSteps,
    groupReplies, diffLineKind, parseHash, makeHash,
  };
});
