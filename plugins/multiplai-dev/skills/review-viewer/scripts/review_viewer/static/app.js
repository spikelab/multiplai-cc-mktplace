/* review-viewer page: findings beside the code, questions to the live session.
 *
 * Every /api call carries the token boot.js took from the address bar.
 * Text written by a person or a model is only ever set with textContent, or
 * with innerHTML after DOMPurify has sanitised the rendered markdown. Code is
 * set with innerHTML only as highlighter output or escaped text.
 */
(function () {
  "use strict";

  const L = window.ReviewLogic;
  const $ = (id) => document.getElementById(id);
  const NOT_AUTHORISED = "This page is not authorised. Open the link the viewer printed " +
    "(the \"open: file://…\" line) again.";
  const SERVER_GONE = "The viewer server is not answering. It stops after 30 minutes " +
    "without an open page; ask the session to start it again.";

  const state = {
    token: typeof REVIEW_TOKEN === "string" ? REVIEW_TOKEN : "",
    who: null,
    targets: [],
    slug: null,
    detail: null,
    findingsById: new Map(),
    selected: null,
    filePath: null,
    view: null,
    views: new Map(),
    showHidden: false,
    fileFilter: "",
    questions: [],
    replyRows: [],
    replies: new Map(),
    since: 0,
    pick: null,
    pickAnchor: null,
    pollTimer: null,
    polling: null,
    pollAgain: false,
    pollGen: 0,
    tab: "finding",
    tabChosen: false,
    walk: null,
    walkKey: "",
    stepId: null,
    walkFocus: null,
  };

  // --- small helpers ---------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children || [])) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function showBanner(text) {
    const b = $("banner");
    b.textContent = text;
    b.hidden = !text;
  }

  async function api(path, body) {
    const opts = { headers: { "X-Review-Token": state.token }, cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      showBanner(SERVER_GONE);
      throw err;
    }
    if (res.status === 401) {
      showBanner(NOT_AUTHORISED);
      throw new Error("unauthorised");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    if ($("banner").textContent === SERVER_GONE) showBanner("");
    return data;
  }

  function targetUrl(suffix) {
    return "/api/targets/" + encodeURIComponent(state.slug) + (suffix || "");
  }

  function highlight(text, language) {
    const hljs = window.hljs;
    if (hljs && language && hljs.getLanguage(language)) {
      try {
        return L.splitHighlighted(hljs.highlight(text, { language: language, ignoreIllegals: true }).value);
      } catch (err) { /* fall through to plain text */ }
    }
    return L.escapeHtml(text).split("\n");
  }

  function renderMarkdown(node, text) {
    if (window.marked && window.DOMPurify) {
      node.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text));
      if (window.hljs) node.querySelectorAll("pre code").forEach((c) => window.hljs.highlightElement(c));
    } else {
      node.classList.add("plain");
      node.textContent = text;
    }
  }

  /* Diagrams. mermaid renders to an SVG string; DOMPurify cleans it (SVG
   * profile); the result is shown as an <img> with a data: URL. The CSP allows
   * `data:` images and no inline styles, so the SVG is never put into the page
   * itself. Any failure shows the diagram source instead. */
  let mermaidReady = false;
  let diagramSeq = 0;
  const diagramCache = new Map();

  function diagramFallback(node, source, why) {
    node.replaceChildren(
      el("p", { class: "muted", text: "Diagram not rendered (" + why + "); its source:" }),
      el("pre", { class: "sketch diagram-fallback", text: source }));
  }

  function sizedSvg(clean) {
    const doc = new DOMParser().parseFromString(clean, "image/svg+xml");
    const svg = doc.documentElement;
    if (!svg || svg.nodeName !== "svg") throw new Error("not an SVG");
    const box = (svg.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
    if (box.length === 4 && box[2] > 0 && box[3] > 0) {
      svg.setAttribute("width", String(Math.ceil(box[2])));
      svg.setAttribute("height", String(Math.ceil(box[3])));
    }
    svg.removeAttribute("style");
    if (!svg.getAttribute("xmlns")) svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    return new XMLSerializer().serializeToString(svg);
  }

  async function diagramUrl(source) {
    if (diagramCache.has(source)) return diagramCache.get(source);
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (!mermaidReady) {
      window.mermaid.initialize({
        startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default",
        htmlLabels: false, flowchart: { htmlLabels: false }, fontFamily: "sans-serif",
      });
      mermaidReady = true;
    }
    const id = "walk-diagram-" + (++diagramSeq);
    try {
      const out = await window.mermaid.render(id, source);
      const clean = window.DOMPurify.sanitize(out.svg, { USE_PROFILES: { svg: true, svgFilters: true } });
      const url = L.svgDataUrl(sizedSvg(clean));
      diagramCache.set(source, url);
      return url;
    } finally {
      // mermaid leaves its scratch element behind when a render fails.
      for (const stray of [document.getElementById(id), document.getElementById("d" + id)]) {
        if (stray) stray.remove();
      }
    }
  }

  async function renderDiagram(node, diagram) {
    const source = diagram.source;
    if (!window.mermaid || !window.DOMPurify) {
      diagramFallback(node, source, "mermaid did not load");
      return;
    }
    try {
      const url = await diagramUrl(source);
      const img = el("img", { class: "diagram", alt: "Diagram for this step", src: url });
      node.replaceChildren(img);
    } catch (err) {
      diagramFallback(node, source, String((err && err.message) || err).split("\n")[0]);
    }
  }

  // --- loading ---------------------------------------------------------------

  async function boot() {
    if (!state.token) {
      showBanner(NOT_AUTHORISED);
      return;
    }
    try {
      state.who = await api("/api/whoami");
    } catch (err) {
      return;
    }
    const sid = (state.who.session_id || "").slice(0, 8) || "no session id";
    $("who").textContent = "Answers come from " + state.who.agent + " (session " + sid + ")";
    const res = await api("/api/targets");
    state.targets = res.targets;
    const sel = $("target-select");
    if (state.targets.length > 1) {
      sel.hidden = false;
      for (const t of state.targets) sel.appendChild(el("option", { value: t.slug, text: t.label }));
      sel.addEventListener("change", () => loadTarget(sel.value));
    }
    bindEvents();
    setInterval(() => api("/api/alive").catch(() => {}), 10000);
    if (state.targets.length) await loadTarget(state.targets[0].slug);
    schedulePoll();
  }

  async function loadTarget(slug) {
    state.pollGen += 1;
    state.slug = slug;
    state.detail = await api(targetUrl());
    state.findingsById = new Map(state.detail.findings.findings.map((f) => [f.id, f]));
    state.questions = state.detail.questions || [];
    state.replyRows = [];
    state.replies = new Map();
    state.since = 0;
    state.views = new Map();
    state.selected = null;
    state.pick = null;
    state.pickAnchor = null;
    state.walk = null;
    state.walkKey = "";
    state.stepId = null;
    state.walkFocus = null;
    if (!state.tabChosen) state.tab = state.detail.findings.findings.length ? "finding" : "walk";
    const target = state.detail.findings.target;
    $("title").textContent = target.label;
    document.title = target.label + " · review";
    renderHeader();
    renderSidebar();
    renderTabs();
    renderWalk();
    await Promise.all([pollOnce(), pollWalk()]);
    const first = L.findingOrder(L.groupFindings(state.detail.findings.findings,
      state.detail.decisions, state.showHidden))[0];
    if (first) await selectFinding(first);
    else {
      renderDetail();
      if (state.detail.files.length) await openFile(state.detail.files[0]);
    }
  }

  // --- header, tabs ----------------------------------------------------------------

  function renderHeader() {
    const pr = state.detail.pr;
    const box = $("pr-info");
    box.hidden = !pr;
    if (pr) {
      const url = L.safePrUrl(pr.url);
      box.replaceChildren(
        el("span", { class: "pr-title", text: "PR #" + pr.number + " " + (pr.title || "") }),
        pr.author ? el("span", { class: "muted", text: " by " + pr.author }) : null,
        url ? el("a", { href: url, target: "_blank", rel: "noreferrer noopener", text: " open on GitHub" }) : null);
    }
    const notice = $("notice");
    notice.textContent = state.detail.notice || "";
    notice.hidden = !state.detail.notice;
  }

  function setTab(tab) {
    state.tab = tab;
    state.tabChosen = true;
    renderTabs();
    if (state.view) renderCode();
    renderThread();
  }

  function renderTabs() {
    const walk = state.tab === "walk";
    $("tab-finding").setAttribute("aria-selected", String(!walk));
    $("tab-walk").setAttribute("aria-selected", String(walk));
    $("tab-finding").classList.toggle("active", !walk);
    $("tab-walk").classList.toggle("active", walk);
    $("finding").hidden = walk;
    $("walkthrough").hidden = !walk;
    $("decide").hidden = walk || !(state.selected && state.findingsById.get(state.selected));
    const n = state.walk ? state.walk.steps.length : 0;
    $("walk-count").textContent = !state.walk ? "(waiting)" : (state.walk.complete ? "(" + n + ")" : "(" + n + ", in progress)");
  }

  // --- walkthrough -----------------------------------------------------------------

  async function pollWalk() {
    const gen = state.pollGen;
    let walk = null;
    try {
      walk = await api(targetUrl("/walkthrough"));
    } catch (err) {
      if (err.message !== "no walkthrough yet") return;
    }
    if (gen !== state.pollGen) return;
    const key = walk ? JSON.stringify(walk) : "";
    if (key === state.walkKey) return;
    state.walkKey = key;
    state.walk = walk;
    if (state.stepId && !L.stepOrder(walk).includes(state.stepId)) state.stepId = null;
    renderTabs();
    renderWalk();
    if (!state.tabChosen && walk && state.tab === "walk" && !state.stepId && walk.steps.length) {
      await selectStep(walk.steps[0].id, { open: !state.filePath || !state.detail.findings.findings.length });
    }
  }

  function currentStep() {
    if (!state.walk || !state.stepId) return null;
    return state.walk.steps.find((s) => s.id === state.stepId) || null;
  }

  function renderWalk() {
    $("walk-status").textContent = L.walkStatus(state.walk);
    $("walk-status").hidden = !L.walkStatus(state.walk);
    const overview = $("walk-overview");
    const list = $("walk-steps");
    list.replaceChildren();
    if (!state.walk) {
      overview.replaceChildren(el("p", { class: "muted", text: "The session writes the walkthrough after it reads the diff; it appears here as it is written." }));
      $("walk-step").replaceChildren();
      return;
    }
    renderMarkdown(overview, state.walk.overview_md);
    const cov = L.walkCoverage(state.walk, state.detail.files, state.detail.findings.findings);
    overview.appendChild(el("p", { class: "muted small", text: "Covers " + cov.files + " of " + cov.filesTotal +
      " changed files" + (cov.findingsTotal ? " and links " + cov.findings + " of " + cov.findingsTotal + " findings" : "") + "." }));
    state.walk.steps.forEach((s, i) => {
      list.appendChild(el("li", {}, [el("button", {
        class: "step-item" + (s.id === state.stepId ? " selected" : ""),
        "data-step": s.id,
        onclick: () => selectStep(s.id, { open: true }),
      }, [el("span", { class: "step-n", text: String(i + 1) }), el("span", { text: s.title })])]));
    });
    if (state.walk.skipped.length) {
      const ul = el("ul", { class: "skipped" });
      for (const k of state.walk.skipped) {
        ul.appendChild(el("li", {}, [el("span", { class: "mono", text: k.path }), " — " + k.reason]));
      }
      list.appendChild(el("li", { class: "skipped-li" }, [el("div", { class: "label", text: "Not explained" }), ul]));
    }
    renderStep();
  }

  function renderStep() {
    const box = $("walk-step");
    box.replaceChildren();
    const step = currentStep();
    if (!step) {
      if (state.walk && state.walk.steps.length) {
        box.appendChild(el("p", { class: "muted", text: "Pick a step, or press ] to start." }));
      }
      return;
    }
    const pos = L.stepPosition(state.walk, step.id);
    box.appendChild(el("div", { class: "step-nav" }, [
      el("button", { text: "◀ Previous", disabled: pos.index <= 1, onclick: () => moveStep(-1) }),
      el("span", { class: "muted", text: "Step " + pos.index + " of " + pos.total }),
      el("button", { text: "Next ▶", disabled: pos.index >= pos.total, onclick: () => moveStep(1) }),
    ]));
    box.appendChild(el("h2", { text: step.title }));
    const anchors = el("div", { class: "step-anchors" });
    for (const a of step.anchors) {
      anchors.appendChild(el("button", {
        class: "cite-link walk-anchor", text: L.walkAnchorLabel(a),
        onclick: () => openAnchor(a),
      }));
    }
    box.appendChild(anchors);
    const body = el("div", { class: "md" });
    renderMarkdown(body, step.body_md);
    box.appendChild(body);
    if (step.diagram) {
      const holder = el("div", { class: "diagram-box" }, [el("p", { class: "muted", text: "Rendering diagram…" })]);
      box.appendChild(holder);
      renderDiagram(holder, step.diagram);
    }
    const cards = step.finding_ids.map((id) => state.findingsById.get(id)).filter(Boolean);
    if (cards.length) {
      box.appendChild(el("div", { class: "label", text: "Findings in this step" }));
      for (const f of cards) {
        box.appendChild(el("button", {
          class: "finding-item finding-card", "data-id": f.id,
          onclick: () => { setTab("finding"); selectFinding(f.id); },
        }, [
          el("span", { class: "badge " + f.severity, text: f.severity }),
          el("span", { class: "badge", text: f.status }),
          el("span", { class: "claim", text: f.claim }),
          el("span", { class: "where", text: f.file + ":" + f.line_start }),
        ]));
      }
    }
  }

  async function selectStep(id, opts) {
    state.stepId = id;
    if (state.tab !== "walk") {
      state.tab = "walk";
      renderTabs();
    }
    clearPick();
    renderWalk();
    const step = currentStep();
    if (step && (!opts || opts.open !== false)) await openAnchor(step.anchors[0]);
    else if (state.view) renderCode();
    renderThread();
  }

  function moveStep(delta) {
    const next = L.moveStep(state.walk, state.stepId, delta);
    if (next) selectStep(next, { open: true });
  }

  async function openAnchor(anchor) {
    state.walkFocus = anchor;
    if (state.tab !== "walk") {
      state.tab = "walk";
      renderTabs();
    }
    await openFile(anchor.path);
    if (state.view && state.view.path === anchor.path) {
      renderCode();
      flashRows(L.anchorRows(state.view.rows, anchor));
    }
  }

  // --- sidebar ---------------------------------------------------------------

  function renderSidebar() {
    const box = $("findings");
    box.replaceChildren();
    const findings = state.detail.findings.findings;
    const grouped = L.groupFindings(findings, state.detail.decisions, state.showHidden);
    $("hidden-label").textContent = "Show refuted and rejected (" + grouped.hidden + ")";
    if (!findings.length) {
      box.appendChild(el("p", { class: "empty", text: "No findings: this is the plain diff. Pick a file, select lines, and ask about them." }));
    }
    for (const sev of L.SEVERITIES) {
      const items = grouped.groups[sev] || [];
      if (!items.length) continue;
      box.appendChild(el("h2", { class: "sev-h " + sev, text: sev + " (" + items.length + ")" }));
      for (const f of items) {
        const decision = state.detail.decisions[f.id];
        box.appendChild(el("button", {
          class: "finding-item" + (f.id === state.selected ? " selected" : "") +
            (L.isHidden(f, state.detail.decisions) ? " hidden-finding" : ""),
          "data-id": f.id,
          onclick: () => selectFinding(f.id),
        }, [
          el("span", { class: "badge " + sev, text: f.status }),
          decision ? el("span", { class: "badge " + decision.decision, text: decision.decision }) : null,
          el("span", { class: "claim", text: f.claim }),
          el("span", { class: "where", text: f.file + ":" + f.line_start }),
        ]));
      }
    }
    renderFiles();
  }

  function renderFiles() {
    const list = $("files");
    list.replaceChildren();
    const filter = state.fileFilter.toLowerCase();
    for (const path of state.detail.files) {
      if (filter && !path.toLowerCase().includes(filter)) continue;
      list.appendChild(el("li", {}, [el("button", {
        class: path === state.filePath ? "selected" : "",
        text: path,
        onclick: () => { clearPick(); openFile(path); },
      })]));
    }
  }

  // --- finding detail ----------------------------------------------------------

  async function selectFinding(id) {
    state.selected = id;
    if (state.tab !== "finding") {
      state.tab = "finding";
      renderTabs();
    }
    clearPick();
    renderSidebar();
    renderDetail();
    const f = state.findingsById.get(id);
    if (f) await openFile(f.file, f.line_start, f.line_end);
  }

  function citationLink(c) {
    return el("button", {
      class: "cite-link",
      text: L.anchorLabel(c) + (c.quote ? "  " + c.quote.split("\n")[0] : ""),
      onclick: () => openFile(c.path, c.line_start, c.line_end),
    });
  }

  function renderDetail() {
    const box = $("finding");
    box.replaceChildren();
    const f = state.selected && state.findingsById.get(state.selected);
    $("decide").hidden = !f || state.tab === "walk";
    if (!f) {
      box.appendChild(el("p", { class: "muted", text: state.detail.findings.findings.length
        ? "Pick a finding on the left, or select lines in the code to ask about them."
        : "Select lines in the code (drag, or click a line number and shift-click another) to ask about them." }));
      renderThread();
      return;
    }
    box.appendChild(el("div", {}, [
      el("span", { class: "badge " + f.severity, text: f.severity }),
      el("span", { class: "badge", text: f.status }),
    ]));
    box.appendChild(el("h2", { text: f.claim }));
    box.appendChild(el("div", { class: "label", text: "Failure scenario" }));
    box.appendChild(el("p", { text: f.failure_scenario }));
    if (f.verdict_reason) {
      box.appendChild(el("div", { class: "label", text: "Verdict" }));
      box.appendChild(el("p", { text: f.verdict_reason }));
    }
    box.appendChild(el("div", { class: "label", text: "Cited code" }));
    for (const c of f.citations) box.appendChild(citationLink(c));
    const steps = L.stepsForFinding(state.walk, f.id);
    if (steps.length) {
      box.appendChild(el("div", { class: "label", text: "Explained in the walkthrough" }));
      for (const s of steps) {
        box.appendChild(el("button", { class: "cite-link", text: s.title, onclick: () => selectStep(s.id, { open: true }) }));
      }
    }
    if (f.fix) {
      box.appendChild(el("div", { class: "label", text: "Fix" }));
      box.appendChild(el("p", { text: f.fix.description }));
      if (f.fix.patch_sketch) box.appendChild(el("pre", { class: "sketch", text: f.fix.patch_sketch }));
      if (f.fix.premises.length) {
        box.appendChild(el("div", { class: "label", text: "Premises" }));
        const ul = el("ul");
        for (const p of f.fix.premises) {
          ul.appendChild(el("li", {}, [
            p.kind === "external" ? el("span", { class: "assumption", text: "Assumption: " }) : null,
            p.statement,
            p.citation ? citationLink(p.citation) : null,
          ]));
        }
        box.appendChild(ul);
      }
      if (f.fix.open_questions.length) {
        box.appendChild(el("div", { class: "label", text: "Open questions" }));
        const ul = el("ul");
        for (const q of f.fix.open_questions) ul.appendChild(el("li", { text: q }));
        box.appendChild(ul);
      }
    }
    renderDecision();
    renderThread();
  }

  function renderDecision() {
    const f = state.selected && state.findingsById.get(state.selected);
    if (!f) return;
    const d = state.detail.decisions[f.id];
    $("decision-now").textContent = d ? "— " + d.decision + (d.note ? ": " + d.note : "") : "— none yet";
  }

  async function decide(decision) {
    const id = state.selected;
    if (!id) return;
    const note = $("decision-note").value.trim();
    try {
      const res = await api("/api/decision", { target: state.slug, finding_id: id, decision: decision, note: note });
      state.detail.decisions[id] = res.decision;
      $("decision-note").value = "";
      renderSidebar();
      renderDecision();
    } catch (err) {
      showBanner("Could not record the decision: " + err.message);
    }
  }

  // --- threads ---------------------------------------------------------------

  function askingAboutStep() {
    return state.tab === "walk" && !!currentStep();
  }

  function threadQuestions() {
    if (askingAboutStep()) return state.questions.filter((q) => q.step_id === state.stepId);
    if (state.pickAnchor || !state.selected) return state.questions.filter((q) => !q.finding_id && !q.step_id);
    return state.questions.filter((q) => q.finding_id === state.selected);
  }

  function renderThread() {
    const chip = $("anchor-chip");
    chip.hidden = !state.pickAnchor;
    if (state.pickAnchor) {
      chip.replaceChildren("About " + L.anchorLabel(state.pickAnchor) + " ",
        el("button", { class: "cite-link", text: "×", "aria-label": "Clear line selection", onclick: clearPick }));
    }
    $("thread-title").textContent = askingAboutStep() ? "Questions about this step"
      : (state.pickAnchor || !state.selected ? "Questions about the diff or selected lines"
        : "Questions about this finding");
    const list = $("thread");
    list.replaceChildren();
    const agent = state.who ? state.who.agent : "the session";
    for (const q of threadQuestions()) {
      const reply = state.replies.get(q.id);
      const answer = el("div", { class: "a" });
      if (reply && reply.text) renderMarkdown(answer, reply.text);
      if (L.isPending(q.id, state.replies)) {
        answer.appendChild(el("div", { class: "muted" }, [el("span", { class: "spinner" }), agent + " is answering…"]));
      }
      list.appendChild(el("li", {}, [
        el("div", { class: "q" }, [
          q.anchor ? el("span", { class: "anchor", text: L.anchorLabel(q.anchor) }) : null,
          q.text,
        ]),
        answer,
      ]));
    }
  }

  async function send() {
    const box = $("question");
    const text = box.value.trim();
    if (!text) return;
    const anchor = state.pickAnchor;
    const stepId = askingAboutStep() ? state.stepId : null;
    const body = { target: state.slug, text: text, finding_id: anchor || stepId ? null : state.selected,
      anchor: anchor, step_id: stepId };
    $("send").disabled = true;
    try {
      const res = await api("/api/ask", body);
      state.questions.push({ id: res.id, kind: "question", text: text, finding_id: body.finding_id,
        anchor: anchor, step_id: stepId });
      box.value = "";
      renderThread();
      schedulePoll(0);
    } catch (err) {
      showBanner("Could not send the question: " + err.message);
    } finally {
      $("send").disabled = false;
    }
  }

  /* At most one poll runs at a time. A poll asked for while one is in
   * flight runs once after it; a response that arrives after the target
   * changed is dropped (its generation no longer matches). */
  function pollOnce() {
    if (state.polling) {
      state.pollAgain = true;
      return state.polling;
    }
    state.polling = (async () => {
      try {
        do {
          state.pollAgain = false;
          await pollRequest();
        } while (state.pollAgain);
      } finally {
        state.polling = null;
      }
    })();
    return state.polling;
  }

  async function pollRequest() {
    const gen = state.pollGen;
    const asked = state.since;
    const res = await api("/api/poll?target=" + encodeURIComponent(state.slug) + "&since=" + asked);
    if (gen !== state.pollGen) return;
    const next = L.applyPoll({ rows: state.replyRows, since: state.since }, asked, res);
    state.replyRows = next.rows;
    state.since = next.since;
    if (next.reset) {
      state.pollAgain = true;
      return;
    }
    if (next.changed) {
      state.replies = L.groupReplies(state.replyRows);
      renderThread();
    }
  }

  function schedulePoll(delay) {
    clearTimeout(state.pollTimer);
    // An unfinished walkthrough counts as pending: its steps arrive while the
    // session writes them.
    const pending = state.questions.filter((q) => L.isPending(q.id, state.replies)).length +
      (state.walk && state.walk.complete ? 0 : 1);
    const wait = delay != null ? delay : L.pollDelay(pending);
    state.pollTimer = setTimeout(async () => {
      try { await Promise.all([pollOnce(), pollWalk()]); } catch (err) { /* banner already shown */ }
      schedulePoll();
    }, wait);
  }

  // --- code pane ---------------------------------------------------------------

  async function openFile(path, start, end) {
    let view = state.views.get(path);
    if (!view) {
      try {
        view = await api(targetUrl("/file") + "?path=" + encodeURIComponent(path));
      } catch (err) {
        $("file-path").textContent = path;
        $("file-flags").textContent = "";
        $("code").replaceChildren(el("p", { class: "notice", text: "Cannot show this file: " + err.message }));
        return;
      }
      state.views.set(path, view);
    }
    if (state.filePath !== path) {
      state.filePath = path;
      state.view = view;
      renderCode();
      renderFiles();
    }
    if (start != null) scrollToLines(start, end == null ? start : end);
  }

  function findingsIn(path) {
    return state.detail.findings.findings.filter((f) => f.file === path &&
      (state.showHidden || !L.isHidden(f, state.detail.decisions)));
  }

  function renderCode() {
    const view = state.view;
    $("file-path").textContent = view.path;
    const flags = [];
    if (view.deleted) flags.push("deleted at head; first lines shown");
    if (view.truncated && !view.deleted) flags.push("large file: changes and cited lines only");
    if (view.binary) flags.push("binary");
    $("file-flags").textContent = flags.join(" · ");
    const code = $("code");
    if (view.binary) {
      code.replaceChildren(el("p", { class: "notice", text: "Binary file: no text to show." }));
      return;
    }
    const textRows = view.rows.filter((r) => r.k === "ctx" || r.k === "add");
    const delRows = view.rows.filter((r) => r.k === "del");
    const textHtml = highlight(textRows.map((r) => r.t).join("\n"), view.language);
    const delHtml = highlight(delRows.map((r) => r.t).join("\n"), view.language);
    let ti = 0;
    let di = 0;
    const cited = view.cited_ranges || [];
    const selected = state.selected && state.findingsById.get(state.selected);
    const dots = new Map();
    for (const f of findingsIn(view.path)) {
      if (!dots.has(f.line_start)) dots.set(f.line_start, []);
      dots.get(f.line_start).push(f);
    }
    const wf = state.tab === "walk" && state.walkFocus && state.walkFocus.path === view.path ? state.walkFocus : null;
    const walkRows = new Set(wf ? L.anchorRows(view.rows, wf) : []);
    const tbody = el("tbody");
    view.rows.forEach((r, ri) => {
      let html;
      if (r.k === "gap") html = L.escapeHtml(r.t);
      else if (r.k === "del") html = delHtml[di++] || "";
      else html = textHtml[ti++] || "";
      const classes = [r.k];
      if (r.n != null && cited.some(([a, b]) => r.n >= a && r.n <= b)) classes.push("cited");
      if (selected && selected.file === view.path && r.n != null &&
          r.n >= selected.line_start && r.n <= selected.line_end) classes.push("focus");
      if (state.pick && state.pick.path === view.path && r.n != null &&
          r.n >= state.pick.start && r.n <= state.pick.end) classes.push("picked");
      if (walkRows.has(ri)) classes.push("walk-focus");
      const dotCell = el("td", { class: "mark" });
      for (const f of (r.n != null && dots.get(r.n)) || []) {
        dotCell.appendChild(el("span", {
          class: "dot " + f.severity, title: f.severity + ": " + f.claim,
          onclick: () => selectFinding(f.id),
        }));
      }
      const src = el("td", { class: "src" });
      src.innerHTML = html;
      tbody.appendChild(el("tr", { class: classes.join(" "), "data-n": r.n == null ? null : String(r.n),
        "data-o": r.o == null ? null : String(r.o) }, [
        dotCell,
        el("td", { class: "ln", text: r.o == null ? "" : String(r.o) }),
        el("td", { class: "ln new", text: r.n == null ? "" : String(r.n) }),
        el("td", { class: "mark" }),
        src,
      ]));
    });
    code.replaceChildren(el("table", {}, [tbody]));
  }

  function scrollToLines(start, end) {
    if (!state.view) return;
    flashRows(L.citationRows(state.view.rows, start, end));
  }

  function flashRows(idx) {
    if (!idx.length) return;
    const trs = $("code").querySelectorAll("tr");
    const first = trs[idx[0]];
    if (first) first.scrollIntoView({ block: "center" });
    for (const i of idx) {
      const tr = trs[i];
      if (!tr) continue;
      tr.classList.remove("flash");
      void tr.offsetWidth;  // restart the animation
      tr.classList.add("flash");
    }
  }

  // --- asking about selected lines ------------------------------------------------

  function setPick(a, b, x, y) {
    const [start, end] = L.lineRange(a, b);
    state.pick = { path: state.view.path, start: start, end: end };
    renderCode();
    const btn = $("ask-lines");
    const centre = btn.parentElement.getBoundingClientRect();
    btn.style.left = Math.max(8, Math.min(x - centre.left, centre.width - 200)) + "px";
    btn.style.top = Math.max(8, y - centre.top + 12) + "px";
    btn.hidden = false;
  }

  function clearPick() {
    state.pick = null;
    state.pickAnchor = null;
    $("ask-lines").hidden = true;
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
    if (state.view) renderCode();
    if (state.detail) renderThread();
  }

  function rowNumber(node) {
    const tr = node && (node.nodeType === 1 ? node : node.parentElement);
    const row = tr && tr.closest("tr[data-n]");
    return row ? Number(row.getAttribute("data-n")) : null;
  }

  function onCodeMouseUp(ev) {
    if (ev.target.closest("td.ln, .dot")) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const a = rowNumber(sel.anchorNode);
    const b = rowNumber(sel.focusNode);
    if (a == null || b == null) return;
    setPick(a, b, ev.clientX, ev.clientY);
  }

  function onCodeClick(ev) {
    const cell = ev.target.closest("td.ln");
    if (!cell) return;
    const n = rowNumber(cell);
    if (n == null) return;
    const start = ev.shiftKey && state.pick && state.pick.path === state.view.path ? state.pick.start : n;
    setPick(start, n, ev.clientX, ev.clientY);
  }

  function askAboutPick() {
    if (!state.pick) return;
    state.pickAnchor = { path: state.pick.path, line_start: state.pick.start, line_end: state.pick.end };
    $("ask-lines").hidden = true;
    renderThread();
    $("question").focus();
  }

  // --- events ------------------------------------------------------------------

  function bindEvents() {
    $("send").addEventListener("click", send);
    $("question").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); send(); }
    });
    for (const b of document.querySelectorAll("[data-decision]")) {
      b.addEventListener("click", () => decide(b.getAttribute("data-decision")));
    }
    $("show-hidden").addEventListener("change", (ev) => {
      state.showHidden = ev.target.checked;
      renderSidebar();
      if (state.view) renderCode();
    });
    $("file-search").addEventListener("input", (ev) => {
      state.fileFilter = ev.target.value;
      renderFiles();
    });
    $("code").addEventListener("mouseup", onCodeMouseUp);
    $("code").addEventListener("click", onCodeClick);
    $("ask-lines").addEventListener("click", askAboutPick);
    $("tab-finding").addEventListener("click", () => setTab("finding"));
    $("tab-walk").addEventListener("click", () => setTab("walk"));
    document.addEventListener("keydown", (ev) => {
      const typing = ev.target.closest && ev.target.closest("input, textarea, select");
      if (ev.key === "Escape") {
        if (typing) ev.target.blur();
        clearPick();
        return;
      }
      if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === "j" || ev.key === "k") {
        const order = L.findingOrder(L.groupFindings(state.detail.findings.findings,
          state.detail.decisions, state.showHidden));
        const next = L.stepFinding(order, state.selected, ev.key === "j" ? 1 : -1);
        if (state.tab !== "finding") setTab("finding");
        if (next && next !== state.selected) selectFinding(next);
      } else if (ev.key === "]" || ev.key === "[") {
        state.tabChosen = true;
        moveStep(ev.key === "]" ? 1 : -1);
      } else if (ev.key === "/") {
        ev.preventDefault();
        $("file-search").focus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    boot().catch((err) => showBanner("The viewer could not load: " + err.message));
  });
})();
