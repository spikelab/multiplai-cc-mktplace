/* The pipeline page: the changes list, one change's stops and build, and the
 * step-by-step review of a stop. Talks only to the API in API.md. Pure
 * logic is in logic.js (window.PVLogic); this file is the DOM and the
 * network. */
(function () {
  "use strict";

  const L = window.PVLogic;
  const CHANGES_EVERY_MS = 15000;
  const POLL_FAST_MS = 3000;
  const POLL_SLOW_MS = 20000;
  const MAX_BACKOFF_MS = 120000;
  const REQUEST_TIMEOUT_MS = 20000;
  const TICK_MS = 30000;

  const state = {
    token: typeof PV_TOKEN === "string" ? PV_TOKEN : "",
    me: null,
    agent: "Claude",
    repo: "",
    changes: [],
    byName: new Map(),
    changesJson: "",
    changesAt: null,
    loaded: false,
    pollN: 0,
    pollRows: [],
    answers: new Map(),
    mine: new Set(),
    acked: new Set(),
    review: null,
    changesTimer: null,
    changesBackoff: CHANGES_EVERY_MS,
    pollTimer: null,
    pollBackoff: POLL_SLOW_MS,
    toastTimer: null,
    stopped: false,
    started: false,
  };

  const $ = (id) => document.getElementById(id);

  /* --- storage (per browser; the page works without it) ---------------- */

  const store = {
    get(key, fallback) {
      try {
        const v = localStorage.getItem("pipeline-viewer." + key);
        return v == null ? fallback : JSON.parse(v);
      } catch (err) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem("pipeline-viewer." + key, JSON.stringify(value)); } catch (err) { /* blocked */ }
    },
  };

  /* --- DOM helpers ------------------------------------------------------------ */

  /* h("a", {href, class, onclick}, "text", child, [more]) */
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, String(v));
    }
    append(el, kids);
    return el;
  }

  function append(el, kids) {
    for (const kid of kids) {
      if (kid == null || kid === false) continue;
      if (Array.isArray(kid)) append(el, kid);
      else el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
  }

  function chip(text, tone) {
    return h("span", { class: "chip" + (tone ? " " + tone : "") }, text);
  }

  function shortSha(sha) {
    return sha ? String(sha).slice(0, 7) : "";
  }

  function now() {
    return Date.now();
  }

  function setMain(...kids) {
    const main = $("app");
    main.replaceChildren();
    append(main, kids);
  }

  function toast(text, tone) {
    const t = $("toast");
    t.textContent = text;
    t.className = "toast" + (tone ? " " + tone : "");
    t.hidden = !text;
    clearTimeout(state.toastTimer);
    if (text) state.toastTimer = setTimeout(() => { t.hidden = true; }, tone === "bad" ? 12000 : 6000);
  }

  function banner(text) {
    const b = $("banner");
    b.textContent = text || "";
    b.hidden = !text;
  }

  /* Markdown from a document or an answer, sanitized. With the CDN
   * unreachable the text is shown as it is. */
  function markdown(text) {
    const box = h("div", { class: "md" });
    if (window.marked && window.DOMPurify) {
      box.innerHTML = window.DOMPurify.sanitize(window.marked.parse(String(text || "")));
      box.querySelectorAll("a[href]").forEach((a) => {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      });
      box.querySelectorAll("pre code").forEach(highlight);
    } else {
      box.appendChild(h("pre", { class: "plain" }, String(text || "")));
    }
    return box;
  }

  function codeBlock(text, lang) {
    const code = h("code", { class: "language-" + (lang || "plaintext") }, text);
    highlight(code);
    return h("pre", { class: "code" }, code);
  }

  function highlight(code) {
    if (!window.hljs) return;
    try { window.hljs.highlightElement(code); } catch (err) { /* leave it plain */ }
  }

  /* --- the API ---------------------------------------------------------------- */

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, body) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    const opts = { headers: { "X-Review-Token": state.token }, cache: "no-store", signal: ctrl.signal };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      throw new ApiError(0, "The page cannot reach the server. It may have stopped, or the network is down.");
    } finally {
      clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (res.status === 401) {
      showUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!res.ok) {
      const msg = data && data.error ? data.error : "The server answered " + res.status + ".";
      throw new ApiError(res.status, msg === "internal" ? "The server hit an error. Its log has the details." : msg);
    }
    return data;
  }

  function showUnauthorized() {
    state.stopped = true;
    clearTimeout(state.changesTimer);
    clearTimeout(state.pollTimer);
    banner("");
    setMain(h("section", { class: "card narrow" },
      h("h1", {}, "This link does not work"),
      h("p", {}, state.token
        ? "The server does not know this personal link. Ask the person who runs the pipeline for a new one: inviting someone again replaces their old link."
        : "Open the personal link you were sent (it ends in ?t=…). After that, other links to this page work in this browser."),
    ));
  }

  /* What a write did, in one sentence. */
  function describeWrite(res, done) {
    const parts = [res.already ? "This was already recorded" : done];
    if (res.commit) parts[0] += " (commit " + shortSha(res.commit) + ")";
    parts[0] += ".";
    if (res.unpushed) {
      parts.push("It is saved on the server's machine, but the push to GitHub failed" +
        (res.push_error ? ": " + res.push_error : "") + ". Use “Try the push again” on the change's page.");
      return { text: parts.join(" "), tone: "warn" };
    }
    return { text: parts.join(" "), tone: "ok" };
  }

  /* --- loading ---------------------------------------------------------------- */

  async function loadWhoami() {
    const who = await api("/api/whoami");
    state.me = who.person.name;
    state.agent = who.agent || "Claude";
    state.repo = who.repo || "";
    $("who").replaceChildren(h("span", { class: "who-name" }, who.person.name),
      h("span", { class: "muted" }, " · " + L.roleLabel(who.person.role)));
    $("repo").textContent = state.repo;
    document.title = "Pipeline · " + state.repo;
  }

  async function refreshChanges() {
    clearTimeout(state.changesTimer);
    if (state.stopped) return;
    try {
      const data = await api("/api/changes");
      const json = JSON.stringify(data.changes);
      const changed = json !== state.changesJson;
      state.changes = data.changes;
      state.byName = new Map(data.changes.map((c) => [c.name, c]));
      state.changesJson = json;
      state.changesAt = data.generated_at;
      state.loaded = true;
      state.changesBackoff = CHANGES_EVERY_MS;
      banner("");
      if (changed && state.started) render(false);
      if (state.review && state.review.steps) refreshQuestions(state.review);
    } catch (err) {
      if (err.status === 401) return;
      state.changesBackoff = Math.min(state.changesBackoff * 2, MAX_BACKOFF_MS);
      banner(err.message + " Trying again in " + Math.round(state.changesBackoff / 1000) + " s.");
    }
    if (!document.hidden) state.changesTimer = setTimeout(refreshChanges, state.changesBackoff);
  }

  /* A question this browser asked since it opened the page has no final
   * answer yet: poll often. */
  function pendingAsk() {
    return [...state.mine].some((id) => {
      const ans = state.answers.get(id);
      return !ans || !ans.done;
    });
  }

  async function pollAnswers() {
    clearTimeout(state.pollTimer);
    if (state.stopped) return;
    try {
      const data = await api("/api/poll?since=" + state.pollN);
      if (data.n < state.pollN) {
        // The server restarted with a fresh outbox count: read it all again.
        state.pollN = 0;
        state.pollRows = [];
        return pollAnswers();
      }
      const before = new Map([...state.answers].map(([k, v]) => [k, v.done]));
      state.pollRows = state.pollRows.concat(data.answers || []);
      state.pollN = data.n;
      state.answers = L.groupReplies(state.pollRows);
      state.pollBackoff = pendingAsk() ? POLL_FAST_MS : POLL_SLOW_MS;
      if ((data.answers || []).length && state.review) refreshQuestions(state.review);
      for (const [id, ans] of state.answers) {
        if (ans.done && before.get(id) !== true) answerArrived(id);
      }
    } catch (err) {
      if (err.status === 401) return;
      state.pollBackoff = Math.min(Math.max(state.pollBackoff, POLL_FAST_MS) * 2, MAX_BACKOFF_MS);
    }
    if (!document.hidden) state.pollTimer = setTimeout(pollAnswers, state.pollBackoff);
  }

  /* Tell the asker their answer arrived when it is not on screen. */
  function answerArrived(id) {
    if (!state.mine.has(id) || !state.loaded) return;
    if (document.querySelector('[data-answer="' + CSS.escape(id) + '"]')) return;
    toast(state.agent + " answered your question. Open its section to read it.", "ok");
  }

  /* --- routing ---------------------------------------------------------------- */

  function route() {
    return L.parseHash(location.hash);
  }

  function go(r) {
    location.hash = L.makeHash(r);
  }

  /* fresh: the address changed (scroll to the top, rebuild review mode). */
  function render(fresh) {
    if (state.stopped || !state.loaded) return;
    const r = route();
    if (r.change && r.review && r.stop) {
      if (fresh || !state.review || state.review.change !== r.change || state.review.stop !== r.stop) {
        startReview(r);
      } else if (state.review.atEnd) {
        renderReview();
      }
      return;
    }
    state.review = null;
    const scroll = window.scrollY;
    if (r.change) renderChange(r);
    else renderList();
    if (fresh) {
      if (r.change && r.stop) {
        const el = document.getElementById("stop-" + r.stop);
        if (el) el.scrollIntoView({ block: "start" });
        else window.scrollTo(0, 0);
      } else window.scrollTo(0, 0);
    } else window.scrollTo(0, scroll);
  }

  /* Waiting times move on without any new data: refresh those lines. */
  function tick() {
    document.querySelectorAll("[data-line-change]").forEach((el) => {
      const c = state.byName.get(el.dataset.lineChange);
      const s = c && L.stopOf(c, Number(el.dataset.lineStop));
      if (s) el.textContent = L.stopLine(s, { me: state.me, agent: state.agent, now: now() });
    });
  }

  function stopLineEl(change, stop, cls) {
    return h("p", {
      class: cls || "line",
      dataset: { lineChange: change.name, lineStop: String(stop.n) },
    }, L.stopLine(stop, { me: state.me, agent: state.agent, now: now() }));
  }

  /* --- C1: the changes list ------------------------------------------------- */

  const GROUPS = [
    ["Waiting on you", "Read these and approve them, or ask for changes."],
    ["Waiting on someone else", null],
    ["Being built", null],
    ["Everything else", null],
  ];

  function stateChip(stop) {
    if (!stop) return chip("Not started", "");
    const label = {
      not_reached: ["Not reached", ""],
      waiting: ["Waiting", "warn"],
      acknowledged: ["Being reviewed", "accent"],
      approved: ["Approved", "ok"],
      decided_by_agent: [state.agent + " decided", "ok"],
      rework_requested: ["Changes asked for", "bad"],
    }[stop.state] || [stop.state, ""];
    if (stop.state === "approved" && stop.n < 5 && !(stop.approved_by || []).length) label[0] = "Passed";
    if (stop.n === 5 && stop.state === "waiting") label[0] = "Pull request open";
    return chip(label[0], label[1]);
  }

  function renderList() {
    const sorted = L.sortChanges(state.changes, state.me);
    const groups = [[], [], [], []];
    for (const c of sorted) groups[L.changeGroup(c, state.me)].push(c);
    const updated = state.changesAt ? L.formatClock(state.changesAt, now()) : "";
    const kids = [
      h("div", { class: "page-head" },
        h("h1", {}, "Changes"),
        h("p", { class: "muted" }, state.changes.length + " change" + (state.changes.length === 1 ? "" : "s") +
          (updated ? " · updated " + updated : "") + " · this page refreshes itself")),
    ];
    if (!state.changes.length) {
      kids.push(h("p", { class: "card" }, "No changes yet. A change appears here once pm-spec starts one in specs/changes/."));
    }
    groups.forEach((list, i) => {
      if (!list.length) return;
      kids.push(h("section", { class: "group" + (i === 0 ? " mine" : "") },
        h("h2", {}, GROUPS[i][0] + " ", h("span", { class: "count" }, String(list.length))),
        GROUPS[i][1] ? h("p", { class: "muted" }, GROUPS[i][1]) : null,
        h("ul", { class: "cards" }, list.map((c) => h("li", {}, changeCard(c, i === 0))))));
    });
    setMain(kids);
  }

  function changeCard(c, mine) {
    const stop = L.currentStop(c);
    const b = c.build;
    let buildLine = null;
    if (b) {
      const bits = ["Build: " + L.phaseLabel(b.phase)];
      if (b.blocks_total) bits.push(b.blocks_done + " of " + b.blocks_total + " blocks done");
      if (b.max_usd) bits.push(L.formatUsd(b.spent_usd) + " of " + L.formatUsd(b.max_usd) + " spent");
      buildLine = h("p", { class: "muted small" }, bits.join(" · "));
    }
    return h("a", { class: "card change-card" + (mine ? " mine" : ""), href: L.makeHash({ change: c.name }) },
      h("div", { class: "row" },
        h("span", { class: "change-name" }, c.name),
        h("span", { class: "grow" }),
        stateChip(stop)),
      h("p", { class: "stop-of" }, stop ? "Stop " + stop.n + " of 5: " + L.stopTitle(stop.n) : "Not at a stop yet"),
      stop ? stopLineEl(c, stop) : null,
      buildLine,
      b && b.failed_stop ? h("p", { class: "bad-text small" }, "The build stopped on a failure.") : null,
      c.pm_spec && c.pm_spec.running ? h("p", { class: "muted small" }, "pm-spec is working on it now.") : null,
      c.unpushed ? h("p", { class: "warn-text small" }, "Some decisions are not on GitHub yet.") : null);
  }

  /* --- C2: one change --------------------------------------------------------- */

  const RECORD_VERBS = {
    approve: "approved",
    ack: "started reviewing",
    comment: "commented",
    rework: "asked for changes",
    decided: "decided",
  };

  function renderChange(r) {
    const c = state.byName.get(r.change);
    if (!c) {
      setMain(h("a", { class: "back", href: "#" }, "← All changes"),
        h("section", { class: "card narrow" }, h("h1", {}, "No change called “" + r.change + "”"),
          h("p", {}, "It may have been renamed or finished. The list shows every change the server can see.")));
      return;
    }
    const cur = L.currentStop(c);
    const kids = [
      h("a", { class: "back", href: "#" }, "← All changes"),
      h("div", { class: "page-head" },
        h("h1", {}, c.name),
        h("p", { class: "muted" }, "Branch ", h("code", {}, c.branch || "—"))),
    ];
    if (c.unpushed) {
      kids.push(h("div", { class: "alert warn" },
        h("p", {}, "Some decisions on this change are saved on the server's machine but are not on GitHub yet: the last push failed."),
        h("button", { class: "btn", type: "button", onclick: (e) => retryPush(c, e.currentTarget) }, "Try the push again")));
    }
    if (!c.writable) {
      kids.push(h("div", { class: "alert" },
        h("p", {}, "Comments and approvals cannot be saved right now: no checkout on the server's machine holds branch " +
          c.branch + ". You can still read everything.")));
    }
    if (cur && L.isWaitingOn(cur, state.me) && cur.n <= 4) {
      kids.push(h("div", { class: "callout" },
        h("p", { class: "callout-title" }, "This waits on you: " + L.stopTitle(cur.n).toLowerCase()),
        stopLineEl(c, cur),
        h("a", { class: "btn primary", href: L.makeHash({ change: c.name, stop: cur.n, review: true }) }, "Start the review")));
    }
    kids.push(h("section", { class: "block" },
      h("h2", {}, "Stops"),
      h("ol", { class: "timeline" }, c.stops.map((s) => stopItem(c, s, r.stop === s.n)))));
    if (c.build) kids.push(buildSection(c));
    if (c.epic) kids.push(epicSection(c));
    if (c.pm_spec) kids.push(pmSpecSection(c));
    setMain(kids);
  }

  function stopItem(c, s, focused) {
    const reached = s.state !== "not_reached";
    const docs = s.docs || [];
    const readable = docs.some((d) => d.exists);
    const mine = L.isWaitingOn(s, state.me);
    const reviewers = (s.reviewers || []).map((p) =>
      p.name + " (" + L.roleLabel(p.role) + (p.invited ? "" : ", not invited yet") + ")");
    let reminder = null;
    if ((s.state === "waiting") && !(s.acknowledged_by || []).length && s.ack_by && L.parseTime(s.ack_by) > now()) {
      reminder = h("p", { class: "muted small" },
        "If nobody opens it by " + L.formatClock(s.ack_by, now()) + ", Slack gets a reminder.");
    }
    const actions = [];
    if (reached && readable && s.n <= 4) {
      actions.push(h("a", {
        class: "btn" + (mine ? " primary" : ""),
        href: L.makeHash({ change: c.name, stop: s.n, review: true }),
      }, mine ? "Start the review" : L.reviewLabel(s.n)));
    }
    if (s.pr_url) actions.push(h("a", { class: "btn", href: s.pr_url, target: "_blank", rel: "noopener noreferrer" }, "Open the pull request"));
    return h("li", { class: "stop " + s.state + (focused ? " focused" : "") + (s.n === c.current_stop ? " current" : ""), id: "stop-" + s.n },
      h("span", { class: "marker", "aria-hidden": "true" }, String(s.n)),
      h("div", { class: "stop-body" },
        h("div", { class: "row" }, h("h3", {}, L.stopTitle(s.n)), stateChip(s)),
        stopLineEl(c, s),
        reminder,
        reviewers.length ? h("p", { class: "muted small" }, "Reviewers: " + reviewers.join(", ")) : null,
        reached && docs.length ? h("p", { class: "muted small" }, "Documents: ",
          docs.map((d, i) => [i ? ", " : "", h("code", {}, d.path),
            !d.exists ? " (not written)" : d.approve ? "" : " (for reading)"])) : null,
        actions.length ? h("div", { class: "actions" }, actions) : null,
        recordsList(c, s)));
  }

  function recordsList(c, s) {
    const recs = s.records || [];
    if (!recs.length) return null;
    const items = recs.map((r) => {
      const who = r.kind === "decided" ? state.agent : (r.by === state.me ? "You" : r.by || "Someone");
      let text = who + " " + (RECORD_VERBS[r.kind] || r.kind);
      if (r.kind === "answer") {
        text = state.agent + " answered " + (r.by === state.me ? "your" : (r.by || "someone") + "’s") + " question";
      }
      if (r.kind === "decided" && r.reason) text += " (" + r.reason + ")";
      return h("li", {},
        h("span", { class: "rec-time muted" }, L.formatClock(r.at, now())),
        " ", h("span", {}, text), " ",
        h("code", { class: "muted", title: r.subject || "" }, shortSha(r.sha)));
    });
    return h("details", { class: "records", open: recs.length <= 4 },
      h("summary", {}, "Decision record (" + recs.length + ")"),
      h("ul", {}, items));
  }

  async function retryPush(c, btn) {
    btn.disabled = true;
    try {
      const res = await api("/api/push", { name: c.name });
      if (res.pushed) toast("Pushed. GitHub has every decision now.", "ok");
      else toast("The push failed again" + (res.push_error ? ": " + res.push_error : "") + ".", "bad");
      refreshChanges();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
    }
  }

  function pmSpecSection(c) {
    const p = c.pm_spec;
    const tail = (p.progress_tail || []).join("\n");
    return h("section", { class: "block" },
      h("h2", {}, "pm-spec"),
      p.running ? h("p", {}, "pm-spec is working on this change now. Comments and approvals wait until it stops.") : null,
      p.interview_waiting ? h("p", { class: "warn-text" }, "pm-spec is waiting for answers to its interview questions in the Claude session.") : null,
      tail ? h("details", {}, h("summary", {}, "Latest lines of pm-spec's progress log"), h("pre", { class: "plain" }, tail)) : null);
  }

  /* --- C4: the build ---------------------------------------------------------- */

  function stat(label, value, note) {
    return h("div", { class: "stat" },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      note ? h("div", { class: "stat-note muted" }, note) : null);
  }

  function meter(share, tone) {
    const bar = h("span", { class: "meter-fill" + (tone ? " " + tone : "") });
    bar.style.width = Math.min(100, Math.max(0, share * 100)).toFixed(1) + "%";
    return h("span", { class: "meter", role: "presentation" }, bar);
  }

  function buildSection(c) {
    const b = c.build;
    const est = L.estimateRemaining(b);
    const share = L.budgetShare(b);
    const kids = [h("h2", {}, "Build")];
    if (b.failed_stop) {
      kids.push(h("div", { class: "alert bad" },
        h("p", {}, "The build stopped on a failure. The blocks below and the progress log show where.")));
    }
    const stats = [
      stat("Phase", L.phaseLabel(b.phase) || "—"),
      stat("Blocks", b.blocks_total ? b.blocks_done + " of " + b.blocks_total + " done" : "None planned yet"),
      stat("Spent", share != null ? L.formatUsd(b.spent_usd) + " of " + L.formatUsd(b.max_usd) : L.formatUsd(b.spent_usd) || "—",
        share != null ? meter(share, share >= 1 ? "bad" : share >= 0.8 ? "warn" : "") : null),
      stat("Time so far", L.formatDuration(b.elapsed_s) || "—",
        b.updated_at ? "to the last checkpoint, " + L.formatClock(b.updated_at, now()) : null),
      stat("Time left (estimate)", b.complete ? "None: the build finished" : est != null ? "About " + L.formatDuration(est) : "Not yet known",
        b.complete ? null : est != null
          ? "Time so far ÷ blocks done × blocks left. An estimate, not a promise."
          : "Shown once a block is done."),
    ];
    if (b.pr_url) stats.push(stat("Pull request", h("a", { href: b.pr_url, target: "_blank", rel: "noopener noreferrer" }, "Open it")));
    kids.push(h("div", { class: "stats" }, stats));
    if ((b.blocks || []).length) kids.push(blocksTable(c));
    const tail = (b.progress_tail || []).join("\n");
    if (tail) kids.push(h("details", {}, h("summary", {}, "Latest lines of build-progress.md"), h("pre", { class: "plain" }, tail)));
    return h("section", { class: "block" }, kids);
  }

  function blocksTable(c) {
    const rows = [];
    for (const blk of c.build.blocks) {
      const commits = [["Test", blk.test_commit], ["Code", blk.impl_commit], ["Refactor", blk.refactor_commit]]
        .filter(([, sha]) => sha)
        .map(([label, sha]) => h("button", {
          class: "btn small", type: "button", title: "Show the " + label.toLowerCase() + " commit " + shortSha(sha),
          onclick: () => showDiff(c, sha, label + " of block " + blk.number),
        }, label + " diff"));
      const rounds = (blk.review_rounds || []).map((p, i) => h("button", {
        class: "btn small quiet", type: "button", onclick: () => showDoc(c, p, "Review round " + (i + 1) + " of block " + blk.number),
      }, "Round " + (i + 1)));
      const flags = [];
      if (blk.timed_out) flags.push(chip("Timed out", "warn"));
      if (blk.fix_pass) flags.push(chip("Fix pass", "warn"));
      const tone = blk.status === "done" ? "ok" : blk.status === "failed" ? "bad" : blk.status === "pending" ? "" : "accent";
      rows.push(h("tr", { class: "blk " + blk.status },
        h("td", { "data-label": "Block" }, String(blk.number)),
        h("td", { "data-label": "Name" }, blk.name || "—"),
        h("td", { "data-label": "Status" }, chip(L.blockStatusLabel(blk.status), tone), flags),
        h("td", { "data-label": "Review rounds" }, String(blk.review_iterations || 0), rounds.length ? h("div", { class: "inline" }, rounds) : null),
        h("td", { "data-label": "Code changes" }, commits.length ? h("div", { class: "inline" }, commits) : h("span", { class: "muted" }, "none yet")),
        h("td", { "data-label": "Test output" }, evidence(blk))));
    }
    return h("div", { class: "table-wrap" },
      h("table", { class: "blocks" },
        h("thead", {}, h("tr", {}, ["Block", "Name", "Status", "Review rounds", "Code changes", "Test output"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows)));
  }

  function evidence(blk) {
    const parts = [];
    if (blk.red_evidence) parts.push(h("details", {}, h("summary", {}, "Failing first (RED)"), h("pre", { class: "plain" }, blk.red_evidence)));
    if (blk.green_evidence) parts.push(h("details", {}, h("summary", {}, "Passing after (GREEN)"), h("pre", { class: "plain" }, blk.green_evidence)));
    return parts.length ? parts : h("span", { class: "muted" }, "none yet");
  }

  function epicSection(c) {
    const e = c.epic;
    const totals = Object.entries(e.totals || {}).map(([k, v]) => v + " " + k).join(", ");
    const rows = (e.children || []).map((ch) => {
      const own = state.byName.get(ch.id);
      return h("tr", {},
        h("td", { "data-label": "Component" }, own ? h("a", { href: L.makeHash({ change: ch.id }) }, ch.id) : ch.id),
        h("td", { "data-label": "Status" }, chip(ch.status, ch.status === "merged" ? "ok" : ch.status === "failed" ? "bad" : ch.status === "waiting" ? "warn" : "")),
        h("td", { "data-label": "Pull request" }, ch.pr != null ? "#" + ch.pr : "—"),
        h("td", { "data-label": "Rounds" }, String(ch.rounds != null ? ch.rounds : "—")),
        h("td", { "data-label": "Why" }, ch.reason ? h("code", {}, ch.reason) : ""));
    });
    return h("section", { class: "block" },
      h("h2", {}, "Epic: one row per component"),
      h("p", { class: "muted" }, "Run " + e.run + (totals ? " · " + totals : "") +
        (e.spent_usd != null ? " · " + L.formatUsd(e.spent_usd) + " spent" : "")),
      h("div", { class: "table-wrap" }, h("table", { class: "blocks" },
        h("thead", {}, h("tr", {}, ["Component", "Status", "Pull request", "Rounds", "Why"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows))));
  }

  /* --- the dialog: diffs and raw files ---------------------------------------- */

  function openViewer(title, ...body) {
    $("viewer-title").textContent = title;
    $("viewer-body").replaceChildren();
    append($("viewer-body"), body);
    const d = $("viewer");
    if (!d.open) {
      if (d.showModal) d.showModal();
      else d.setAttribute("open", "");
    }
  }

  async function showDiff(c, sha, title) {
    openViewer(title + " · " + shortSha(sha), h("p", { class: "muted" }, "Loading…"));
    try {
      const d = await api("/api/diff?name=" + encodeURIComponent(c.name) + "&commit=" + encodeURIComponent(sha));
      const lines = String(d.diff || "").replace(/\n$/, "").split("\n").map((ln) =>
        h("span", { class: "dl " + L.diffLineKind(ln) }, ln + "\n"));
      openViewer(title + " · " + shortSha(d.commit),
        h("p", {}, h("strong", {}, d.subject)),
        h("p", { class: "muted small" }, d.author + " · " + L.formatClock(d.date, now())),
        h("ul", { class: "files" }, (d.files || []).map((f) =>
          h("li", {}, h("span", { class: "file-status" }, f.status), " ", h("code", {}, f.path)))),
        d.truncated ? h("p", { class: "warn-text" }, "The diff is longer than 1 MiB; it is cut here.") : null,
        h("pre", { class: "diff" }, lines));
    } catch (err) {
      openViewer(title, h("p", { class: "bad-text" }, err.message));
    }
  }

  async function showDoc(c, path, title) {
    openViewer(title, h("p", { class: "muted" }, "Loading…"));
    try {
      const d = await api("/api/doc?name=" + encodeURIComponent(c.name) + "&path=" + encodeURIComponent(path));
      let text = d.text;
      if (/\.json$/i.test(path)) {
        try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (err) { /* show as it is */ }
      }
      openViewer(title, h("p", { class: "muted small" }, h("code", {}, path)),
        L.isMarkdown(path) ? markdown(text) : codeBlock(text, L.codeLanguage(path)));
    } catch (err) {
      openViewer(title, h("p", { class: "bad-text" }, err.message));
    }
  }

  /* --- C3: review mode ------------------------------------------------------- */

  function readKey(rv) {
    return "read." + rv.change + "." + rv.stop + "." + (rv.revision || "none");
  }

  async function startReview(r) {
    const c = state.byName.get(r.change);
    const s = c && L.stopOf(c, r.stop);
    if (!c || !s || s.n > 4) {
      setMain(h("a", { class: "back", href: L.makeHash({ change: r.change }) }, "← Back"),
        h("section", { class: "card narrow" }, h("h1", {}, "Nothing to review here"),
          h("p", {}, "This stop has no documents to review.")));
      return;
    }
    const rv = {
      change: c.name, stop: s.n, revision: s.revision, steps: null, comments: [],
      idx: 0, atEnd: false, read: null, open: null, questions: [], qa: null,
    };
    state.review = rv;
    window.scrollTo(0, 0);
    setMain(h("a", { class: "back", href: L.makeHash({ change: c.name, stop: s.n }) }, "← Back to " + c.name),
      h("p", { class: "muted" }, "Loading the documents…"));
    const docs = await Promise.all((s.docs || []).map(async (d) => {
      if (!d.exists) return { path: d.path, approve: d.approve, text: null };
      try {
        const res = await api("/api/doc?name=" + encodeURIComponent(c.name) + "&path=" + encodeURIComponent(d.path));
        return { path: d.path, approve: d.approve, text: res.text };
      } catch (err) {
        return { path: d.path, approve: d.approve, text: null, error: err.message };
      }
    }));
    if (state.review !== rv) return;
    rv.steps = L.reviewSteps(docs);
    rv.failed = docs.filter((d) => d.error);
    await Promise.all([loadComments(rv), loadQuestions(rv)]);
    if (state.review !== rv) return;
    rv.read = new Set(store.get(readKey(rv), []));
    const saved = store.get("pos." + rv.change + "." + rv.stop, 0);
    rv.idx = Math.min(r.s != null ? r.s : saved, rv.steps.length);
    rv.atEnd = rv.idx >= rv.steps.length;
    renderReview();
    maybeAck(c, s);
  }

  /* Every question about this stop, from everyone, with its answer. */
  async function loadQuestions(rv) {
    try {
      const res = await api("/api/questions?name=" + encodeURIComponent(rv.change) + "&stop=" + rv.stop);
      rv.questions = res.questions || [];
    } catch (err) {
      rv.questions = rv.questions || [];
    }
  }

  async function refreshQuestions(rv) {
    const before = JSON.stringify(rv.questions || []);
    await loadQuestions(rv);
    if (state.review === rv && JSON.stringify(rv.questions) !== before) renderQA(rv);
  }

  async function loadComments(rv, force) {
    const c = state.byName.get(rv.change);
    const s = c && L.stopOf(c, rv.stop);
    // The file exists once someone commented (or asked for changes at stop 4).
    if (!force && s && !(s.records || []).some((r) => r.kind === "comment" || r.kind === "rework")) {
      rv.comments = [];
      return;
    }
    try {
      const res = await api("/api/doc?name=" + encodeURIComponent(rv.change) + "&path=" +
        encodeURIComponent("review/stop-" + rv.stop + ".md"));
      rv.comments = L.parseReviewFile(res.text);
    } catch (err) {
      rv.comments = [];
    }
  }

  async function maybeAck(c, s) {
    const key = c.name + "|" + s.n + "|" + s.revision;
    if (!L.shouldAck(s, state.me) || state.acked.has(key)) return;
    state.acked.add(key);
    try {
      const res = await api("/api/ack", { name: c.name, stop: s.n });
      if (!res.already) {
        toast(state.agent + " now knows you are reviewing this, so it waits for your answer instead of deciding alone.", "ok");
      }
      refreshChanges();
    } catch (err) {
      state.acked.delete(key);
      if (err.status !== 401) toast("Could not tell " + state.agent + " you are reviewing: " + err.message, "warn");
    }
  }

  function setPos(rv, idx) {
    rv.idx = Math.max(0, Math.min(idx, rv.steps.length));
    rv.atEnd = rv.idx >= rv.steps.length;
    rv.open = null;
    rv.qa = null;
    store.set("pos." + rv.change + "." + rv.stop, rv.idx);
    history.replaceState(null, "", L.makeHash({ change: rv.change, stop: rv.stop, review: true, s: rv.idx }));
    renderReview();
    window.scrollTo(0, 0);
  }

  function markRead(rv, step) {
    rv.read.add(step.key);
    store.set(readKey(rv), [...rv.read]);
  }

  function renderReview() {
    const rv = state.review;
    const c = state.byName.get(rv.change);
    const s = c && L.stopOf(c, rv.stop);
    if (!c || !s) return;
    const total = rv.steps.length;
    const readCount = rv.steps.filter((st) => rv.read.has(st.key)).length;
    const bar = h("span", { class: "meter-fill accent" });
    bar.style.width = (total ? (readCount / total) * 100 : 0).toFixed(1) + "%";
    const where = rv.atEnd ? "The end: approve or ask for changes" : "Section " + (rv.idx + 1) + " of " + total;
    const head = h("div", { class: "review-head" },
      h("a", { class: "back", href: L.makeHash({ change: c.name, stop: s.n }) }, "← Back to " + c.name),
      h("h1", {}, L.reviewLabel(s.n), h("span", { class: "muted" }, " · " + c.name)),
      h("div", { class: "progress" },
        h("span", { class: "meter wide", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(readCount) }, bar),
        h("span", { class: "progress-text" }, where + " · " + readCount + " of " + total + " read")));
    const failed = (rv.failed || []).length
      ? h("p", { class: "warn-text" }, "Could not load: " + rv.failed.map((d) => d.path + " (" + d.error + ")").join(", "))
      : null;
    const body = !total
      ? h("section", { class: "card" }, h("p", {}, "This stop's documents are not written yet."))
      : rv.atEnd ? endScreen(rv, c, s) : sectionScreen(rv, c, s);
    setMain(head, failed, h("div", { class: "review-grid" }, contents(rv), body));
  }

  function contents(rv) {
    const items = [];
    let doc = null;
    rv.steps.forEach((st, i) => {
      if (st.doc !== doc) {
        doc = st.doc;
        items.push(h("li", { class: "toc-doc" }, h("code", {}, st.doc), st.approve ? "" : h("span", { class: "muted" }, " for reading")));
      }
      items.push(h("li", { class: i === rv.idx && !rv.atEnd ? "here" : "" },
        h("button", { type: "button", class: "toc-link", onclick: () => setPos(rv, i) },
          h("span", { class: "tick", "aria-label": rv.read.has(st.key) ? "read" : "not read" }, rv.read.has(st.key) ? "✓" : "○"),
          " ", st.heading)));
    });
    items.push(h("li", { class: "toc-end" + (rv.atEnd ? " here" : "") },
      h("button", { type: "button", class: "toc-link", onclick: () => setPos(rv, rv.steps.length) }, "→ Approve or ask for changes")));
    const wide = window.matchMedia && window.matchMedia("(min-width: 900px)").matches;
    return h("details", { class: "toc", open: wide },
      h("summary", {}, "All sections"),
      h("ol", {}, items));
  }

  function sectionScreen(rv, c, s) {
    const st = rv.steps[rv.idx];
    const last = rv.idx === rv.steps.length - 1;
    const isRead = rv.read.has(st.key);
    const content = st.code ? codeBlock(st.body, st.code) : markdown(st.body);
    const next = () => setPos(rv, rv.idx + 1);
    return h("article", { class: "card section-card" },
      h("div", { class: "section-meta" },
        h("code", {}, st.doc), h("span", { class: "muted" }, " · part " + (st.index + 1) + " of " + st.count),
        isRead ? chip("Read", "ok") : null),
      st.approve ? null : h("p", { class: "note" }, "Shown for reading only. Approving this stop does not cover this document."),
      content,
      h("div", { class: "nav-row" },
        h("button", { class: "btn primary", type: "button", onclick: () => { markRead(rv, st); next(); } },
          last ? "Mark as read and finish" : "Mark as read and go on"),
        h("button", { class: "btn", type: "button", disabled: rv.idx === 0, onclick: () => setPos(rv, rv.idx - 1) }, "Previous section"),
        h("button", { class: "btn quiet", type: "button", onclick: next }, last ? "Go to the end" : "Skip for now")),
      talk(rv, c, s, st));
  }

  /* Comments and questions about one section. */
  function talk(rv, c, s, st) {
    const comments = rv.comments.filter((m) => m.doc === st.doc && m.section === st.heading);
    const box = h("section", { class: "talk" });
    const commentForm = composer({
      label: "Comment on this section",
      hint: "Saved in this change's review file, in your name, for everyone to see.",
      button: "Save the comment",
      disabled: !c.writable ? "Comments cannot be saved right now: no checkout holds this change's branch." : null,
      open: rv.open === "comment",
      onopen: () => { rv.open = "comment"; },
      send: async (text) => {
        const res = await api("/api/comment", { name: c.name, stop: s.n, doc: st.doc, section: st.heading.slice(0, 200), text });
        const msg = describeWrite(res, "Comment saved");
        toast(msg.text, msg.tone);
        await loadComments(rv, true);
        rv.open = null;
        if (state.review === rv) renderReview();
      },
    });
    const askForm = composer({
      label: "Ask " + state.agent + " about this section",
      hint: state.agent + " reads the whole change before answering. Everyone on this page sees your question and the answer, and both are saved in the change's review file. You can keep reading meanwhile. To change the text, ask for the change here.",
      button: "Ask " + state.agent,
      open: rv.open === "ask",
      onopen: () => { rv.open = "ask"; },
      send: async (text) => {
        const res = await api("/api/ask", { name: c.name, stop: s.n, doc: st.doc, section: st.heading.slice(0, 200), text });
        state.mine.add(res.id);
        toast("Sent to " + state.agent + ". Everyone on this page sees the question and the answer.", "ok");
        await loadQuestions(rv);
        rv.open = null;
        if (state.review === rv) renderReview();
        state.pollBackoff = POLL_FAST_MS;
        clearTimeout(state.pollTimer);
        state.pollTimer = setTimeout(pollAnswers, POLL_FAST_MS);
      },
    });
    box.appendChild(h("div", { class: "talk-buttons" }, commentForm.button, askForm.button));
    box.appendChild(commentForm.form);
    box.appendChild(askForm.form);
    if (comments.length) {
      box.appendChild(h("h3", {}, "Comments (" + comments.length + ")"));
      box.appendChild(h("ul", { class: "comments" }, comments.map((m) => h("li", {},
        h("p", { class: "muted small" }, (m.by === state.me ? "You" : m.by || "Someone") + (m.at ? " · " + L.formatClock(m.at, now()) : "")),
        markdown(m.text)))));
    }
    rv.qa = { el: h("div", { class: "qa" }), change: c.name, doc: st.doc, heading: st.heading };
    box.appendChild(rv.qa.el);
    renderQA(rv);
    return box;
  }

  /* The questions about the section on screen, everyone's, with answers. */
  function renderQA(rv) {
    if (!rv.qa) return;
    const qs = L.questionsFor(rv.questions, rv.qa.doc, rv.qa.heading);
    const el = rv.qa.el;
    el.replaceChildren();
    if (!qs.length) return;
    el.appendChild(h("h3", {}, "Questions (" + qs.length + ")"));
    el.appendChild(h("ul", { class: "comments" }, qs.map((q) => h("li", {},
      h("p", { class: "muted small" }, L.askerLabel(q.author && q.author.name, state.me) +
        (q.asked_at ? " · " + L.formatClock(q.asked_at, now()) : "")),
      h("p", { class: "question" }, q.text),
      answerEl(q, rv.qa.change)))));
  }

  function answerEl(q, changeName) {
    const el = h("div", { class: "answer", dataset: { answer: q.id } });
    if (q.answer == null) {
      el.appendChild(h("p", { class: "muted" }, "Waiting for " + state.agent + " to answer…"));
      return el;
    }
    el.appendChild(h("p", { class: "muted small" }, state.agent + " answered" +
      (q.answered_at ? " · " + L.formatClock(q.answered_at, now()) : "") + (q.done ? "" : " (still writing…)")));
    el.appendChild(markdown(q.answer));
    if (q.commit) {
      const c = state.byName.get(changeName);
      el.appendChild(c && c.build
        ? h("button", { class: "btn small", type: "button", onclick: () => showDiff(c, q.commit, "The edit") }, "See the edit")
        : h("p", { class: "muted small" }, "Edited the documents in commit ", h("code", {}, shortSha(q.commit)), "."));
    }
    return el;
  }

  /* A button that opens a text box with a send button. */
  function composer(o) {
    const area = h("textarea", { rows: "4", maxlength: "8000", "aria-label": o.label });
    const err = h("p", { class: "bad-text small", hidden: true });
    const send = h("button", { class: "btn primary", type: "submit" }, o.button);
    const cancel = h("button", { class: "btn quiet", type: "button" }, "Cancel");
    const form = h("form", { class: "composer", hidden: !o.open },
      h("label", {}, h("span", { class: "label" }, o.label), h("span", { class: "muted small hint" }, o.hint), area),
      err, h("div", { class: "actions" }, send, cancel));
    const button = h("button", { class: "btn", type: "button", disabled: !!o.disabled, title: o.disabled || "" }, o.label);
    button.addEventListener("click", () => {
      form.hidden = false;
      o.onopen();
      area.focus();
    });
    cancel.addEventListener("click", () => { form.hidden = true; });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = area.value.trim();
      if (!text) {
        err.textContent = "Write something first.";
        err.hidden = false;
        return;
      }
      send.disabled = true;
      err.hidden = true;
      try {
        await o.send(text);
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      } finally {
        send.disabled = false;
      }
    });
    return { button, form };
  }

  function endScreen(rv, c, s) {
    const total = rv.steps.length;
    const unread = rv.steps.map((st, i) => [st, i]).filter(([st]) => !rv.read.has(st.key));
    const kids = [
      h("h2", {}, "You reached the end"),
      h("p", {}, "You read " + (total - unread.length) + " of " + total + " sections."),
    ];
    if (unread.length) {
      kids.push(h("details", { class: "unread" },
        h("summary", {}, "Sections not marked as read (" + unread.length + ")"),
        h("ul", {}, unread.map(([st, i]) => h("li", {},
          h("button", { class: "toc-link", type: "button", onclick: () => setPos(rv, i) },
            h("code", {}, st.doc), " · " + st.heading))))));
    }
    kids.push(h("p", { class: "line" }, "Where this stop is: ",
      h("span", { dataset: { lineChange: c.name, lineStop: String(s.n) } }, L.stopLine(s, { me: state.me, agent: state.agent, now: now() }))));
    if (L.canDecide(s, state.me)) {
      if (!c.writable) {
        kids.push(h("p", { class: "warn-text" }, "Approvals cannot be saved right now: no checkout holds branch " + c.branch + "."));
      }
      kids.push(approveCard(c, s), reworkCard(c, s));
    } else if (!L.isReviewer(s, state.me)) {
      const names = (s.reviewers || []).map((p) => p.name);
      kids.push(h("p", { class: "note" }, (names.length ? "Only " + L.namesText(names) + " can approve this stop. " : "") +
        "Your comments and questions are saved for them to read."));
    } else if ((s.approved_by || []).includes(state.me)) {
      kids.push(h("p", { class: "note ok" }, "You approved this stop." +
        (s.state === "approved" ? "" : " It waits on " + L.namesText(s.waiting_on, state.me) + ".")));
    } else {
      kids.push(h("p", { class: "note" }, "This stop is closed: nothing to approve."));
    }
    kids.push(h("p", {}, h("a", { href: L.makeHash({ change: c.name, stop: s.n }) }, "Back to " + c.name)));
    return h("article", { class: "card section-card end" }, kids);
  }

  const NEXT_STEP = {
    1: "When every reviewer has approved, Claude goes on to the use cases and components.",
    2: "When every reviewer has approved, Claude goes on to the specs.",
    3: "When every reviewer has approved, the specs are ready for buildme.",
    4: "buildme then starts writing tests and code from this plan.",
  };

  function approveCard(c, s) {
    const btn = h("button", { class: "btn primary big", type: "button", disabled: !c.writable }, L.approveLabel(s.n));
    const err = h("p", { class: "bad-text small", hidden: true });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      err.hidden = true;
      try {
        const res = await api("/api/approve", { name: c.name, stop: s.n });
        const msg = describeWrite(res, "Approved");
        toast(msg.text, msg.tone);
        await refreshChanges();
        if (state.review) renderReview();
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        btn.disabled = false;
      }
    });
    return h("section", { class: "decide approve" },
      h("h3", {}, L.approveLabel(s.n)),
      h("p", {}, "Saves your approval as a commit in your name on branch ", h("code", {}, c.branch), ". " + NEXT_STEP[s.n]),
      btn, err);
  }

  function reworkCard(c, s) {
    const where = s.n <= 3
      ? "Your request goes into the change's spec-issues.md, and " + state.agent + " reworks the documents with pm-spec."
      : "Your request goes into review/stop-4.md, and " + state.agent + " edits the implementation plan. You are asked to approve the new plan.";
    const title = h("input", { type: "text", maxlength: "120", "aria-label": "A short title (optional)", placeholder: "A short title (optional)" });
    const text = h("textarea", { rows: "5", maxlength: "8000", "aria-label": "What should change, and why",
      placeholder: "What should change, and why" });
    const btn = h("button", { class: "btn danger", type: "submit", disabled: !c.writable }, "Send the change request");
    const err = h("p", { class: "bad-text small", hidden: true });
    const form = h("form", { class: "decide rework" },
      h("h3", {}, "Ask for changes"),
      h("p", {}, where),
      title, text, err, btn);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!text.value.trim()) {
        err.textContent = "Say what should change first.";
        err.hidden = false;
        return;
      }
      btn.disabled = true;
      err.hidden = true;
      try {
        const res = await api("/api/rework", { name: c.name, stop: s.n, text: text.value.trim(), title: title.value.trim() || null });
        const msg = res.already
          ? { text: "You already asked for changes on this version. Add more detail as comments on the sections.", tone: "warn" }
          : describeWrite(res, "Change request sent");
        toast(msg.text, msg.tone);
        await refreshChanges();
        if (state.review) renderReview();
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        btn.disabled = false;
      }
    });
    return form;
  }

  /* --- start ----------------------------------------------------------------- */

  function wireChrome() {
    const theme = $("theme");
    theme.textContent = PV_THEME.label();
    theme.addEventListener("click", () => {
      PV_THEME.next();
      theme.textContent = PV_THEME.label();
    });
    $("viewer-close").addEventListener("click", () => $("viewer").close());
    $("viewer").addEventListener("click", (e) => { if (e.target === $("viewer")) $("viewer").close(); });
    window.addEventListener("hashchange", () => render(true));
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !state.stopped) {
        refreshChanges();
        pollAnswers();
      }
    });
    setInterval(tick, TICK_MS);
  }

  async function main() {
    wireChrome();
    if (!state.token) {
      showUnauthorized();
      return;
    }
    try {
      await loadWhoami();
    } catch (err) {
      if (err.status !== 401) setMain(h("section", { class: "card narrow" }, h("h1", {}, "The page cannot load"), h("p", {}, err.message)));
      return;
    }
    await refreshChanges();
    state.started = true;
    render(true);
    pollAnswers();
  }

  main();
})();
