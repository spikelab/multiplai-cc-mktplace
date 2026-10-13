// Clips tab: the session's clip candidates (candidates.json). Play a candidate from the
// source, move its edges on a mini timeline (snapping to sentence edges, or words with
// Alt), accept or reject it with a note, and send the picks as `pick` rows.
"use strict";

(() => {
  const { L } = App;
  const fmt = L.fmt;
  let panel, notice, body, list, player, playerLabel, sendBtn, sendHint;
  let candidates = [], sentences = [], sentencesFor = null;
  const words = new Map();          // candidate id → {win, list}: the words fetched for a window
  let decisions = {};               // candidate id → {decision, start, end, note}; this viewer's draft
  let playUntil = null;

  const saveDecisions = () => App.store.set("picks", decisions);

  function sentPicks() {
    return L.latestBy(L.rowsOfKind(App.state.rows, "pick"), "candidate");
  }

  function decisionFor(c) {
    if (!decisions[c.id]) {
      const last = sentPicks().get(c.id);
      decisions[c.id] = last ? { decision: last.decision, start: last.start, end: last.end, note: last.text || "" }
        : { decision: null, start: c.start, end: c.end, note: "" };
    }
    return decisions[c.id];
  }

  // --- playback ------------------------------------------------------------------

  function play(c) {
    const d = decisionFor(c);
    const src = App.mediaUrl("/media/source");
    if (player.src !== new URL(src, location.href).href) player.src = src;
    player.hidden = false;
    playerLabel.textContent = `Playing ${c.id}: ${fmt(d.start)}–${fmt(d.end)}`;
    playUntil = d.end;
    const go = () => { player.currentTime = d.start; player.play().catch(() => {}); };
    if (player.readyState >= 1) go(); else player.addEventListener("loadedmetadata", go, { once: true });
  }

  // --- the mini timeline ----------------------------------------------------------

  function windowFor(c) {
    const d = decisionFor(c);
    return L.timelineWindow(Math.min(c.start, d.start), Math.max(c.end, d.end));
  }

  const wordsOf = (c) => (words.get(c.id) || { list: [] }).list;

  function timeline(c, card) {
    const d = decisionFor(c);
    const win = windowFor(c);
    const wrap = document.createElement("div");
    wrap.className = "tl";
    const track = document.createElement("div");
    track.className = "tl-track";
    const span = document.createElement("div");
    span.className = "tl-span";
    const readout = document.createElement("p");
    readout.className = "tl-readout";
    wrap.append(track, readout);

    for (const w of wordsOf(c)) {
      const el = document.createElement("div");
      el.className = "tl-word";
      el.style.left = `${L.toFrac(w.start, win) * 100}%`;
      el.style.width = `${Math.max(0.2, (L.toFrac(w.end, win) - L.toFrac(w.start, win)) * 100)}%`;
      el.title = w.text;
      track.append(el);
    }
    for (const s of sentences) {
      if (s.end < win.start || s.end > win.end) continue;
      const el = document.createElement("div");
      el.className = "tl-sentence-end";
      el.style.left = `${L.toFrac(s.end, win) * 100}%`;
      el.title = `sentence ends ${fmt(s.end)}`;
      track.append(el);
    }
    track.append(span);
    const handles = {};
    for (const edge of ["start", "end"]) {
      const h = document.createElement("div");
      h.className = `tl-handle ${edge}`;
      h.tabIndex = 0;
      h.setAttribute("role", "slider");
      h.setAttribute("aria-label", `${c.id} ${edge}`);
      track.append(h);
      handles[edge] = h;
    }

    function draw(live) {
      const cur = live ? { ...d, [live.edge]: live.t } : d;
      const a = L.toFrac(cur.start, win), b = L.toFrac(cur.end, win);
      span.style.left = `${a * 100}%`;
      span.style.width = `${(b - a) * 100}%`;
      handles.start.style.left = `${a * 100}%`;
      handles.end.style.left = `${b * 100}%`;
      handles.start.setAttribute("aria-valuetext", fmt(d.start));
      handles.end.setAttribute("aria-valuetext", fmt(d.end));
      const moved = Math.abs(d.start - c.start) > 0.001 || Math.abs(d.end - c.end) > 0.001;
      const s = live ? L.itemAt(live.t, sentences) : null;
      readout.textContent = live
        ? `${live.edge} ${fmt(live.t)}${s ? ` · “${s.text}”` : ""}`
        : `${fmt(d.start)}–${fmt(d.end)} (${(d.end - d.start).toFixed(1)} s)${moved ? ` · moved from ${fmt(c.start)}–${fmt(c.end)}` : ""}`;
      card.querySelector(".span-text").textContent = `${fmt(d.start)}–${fmt(d.end)} · ${(d.end - d.start).toFixed(1)} s`;
    }

    function drop(edge, t, byWord) {
      d[edge] = L.snapEdge(t, edge, d, sentences, wordsOf(c), byWord);
      saveDecisions();
      draw();
      renderFooter();
      afterEdgeMove(c);
    }

    for (const edge of ["start", "end"]) {
      const h = handles[edge];
      h.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        h.setPointerCapture(ev.pointerId);
        h.classList.add("dragging");
        const at = (e) => L.fromFrac((e.clientX - track.getBoundingClientRect().left) / track.getBoundingClientRect().width, win);
        const move = (e) => {
          const t = at(e);
          draw({ edge, t: edge === "start" ? Math.min(t, d.end - 1) : Math.max(t, d.start + 1) });
        };
        const up = (e) => {
          h.removeEventListener("pointermove", move);
          h.removeEventListener("pointerup", up);
          h.removeEventListener("pointercancel", up);
          h.classList.remove("dragging");
          drop(edge, at(e), e.altKey);
        };
        h.addEventListener("pointermove", move);
        h.addEventListener("pointerup", up);
        h.addEventListener("pointercancel", up);
      });
      h.addEventListener("keydown", (ev) => {
        if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
        ev.preventDefault();
        d[edge] = L.stepEdge(edge, d, ev.key === "ArrowLeft" ? -1 : 1, sentences, wordsOf(c), ev.altKey);
        saveDecisions();
        draw();
        renderFooter();
        afterEdgeMove(c);
      });
    }
    draw();
    return wrap;
  }

  // --- cards -----------------------------------------------------------------------

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function card(c, rank) {
    const d = decisionFor(c);
    const li = el("li", "card");
    li.dataset.id = c.id;
    const head = el("div", "card-head");
    head.append(el("span", "rank", `#${rank}`), el("strong", "", c.id), el("span", "span-text", ""),
      el("span", "muted", c.speaker), el("span", "total", `${L.totalScore(c)}/25`));
    const hook = el("p", "hook", c.hook);
    const scores = el("div", "scores");
    for (const k of L.SCORE_KEYS) scores.append(el("span", "score", `${k} ${c.scores[k]}`));
    const excerpt = el("details", "excerpt");
    excerpt.append(el("summary", "", "Transcript"), el("p", "", c.excerpt));
    const why = el("p", "why", c.why);

    const actions = el("div", "row");
    const playBtn = el("button", "", "▶ Play");
    playBtn.type = "button";
    playBtn.addEventListener("click", () => play(c));
    const accept = el("button", "decide accept", "Accept");
    const reject = el("button", "decide reject", "Reject");
    const reset = el("button", "ghost", "Reset edges");
    for (const b of [accept, reject, reset]) b.type = "button";
    const sync = () => {
      accept.setAttribute("aria-pressed", String(d.decision === "accept"));
      reject.setAttribute("aria-pressed", String(d.decision === "reject"));
      li.classList.toggle("accepted", d.decision === "accept");
      li.classList.toggle("rejected", d.decision === "reject");
    };
    const decide = (v) => { d.decision = d.decision === v ? null : v; saveDecisions(); sync(); renderFooter(); };
    accept.addEventListener("click", () => decide("accept"));
    reject.addEventListener("click", () => decide("reject"));
    reset.addEventListener("click", () => { d.start = c.start; d.end = c.end; saveDecisions(); render(); });
    actions.append(playBtn, accept, reject, reset);

    const note = el("textarea");
    note.rows = 1;
    note.placeholder = "Note for the session (optional)";
    note.setAttribute("aria-label", `Note on ${c.id}`);
    note.value = d.note || "";
    note.addEventListener("input", () => { d.note = note.value; saveDecisions(); renderFooter(); });

    const sent = sentPicks().get(c.id);
    const status = el("p", "sent-status", sent ? `Sent: ${sent.decision === "accept" ? "accepted" : "rejected"} ${fmt(sent.start)}–${fmt(sent.end)}` : "");
    li.append(head, hook, scores);      // timeline() writes the span into the head
    li.append(timeline(c, li), actions, note, status, excerpt, why);
    sync();
    return li;
  }

  function renderFooter() {
    const rows = L.unsentPicks(decisions, sentPicks());
    sendBtn.disabled = !rows.length;
    sendBtn.textContent = rows.length ? `Send picks (${rows.length})` : "Send picks";
    const undecided = candidates.filter((c) => !decisionFor(c).decision).length;
    sendHint.textContent = undecided ? `${undecided} candidate${undecided === 1 ? "" : "s"} not decided yet.` : "";
  }

  function render() {
    list.replaceChildren();
    L.sortCandidates(candidates).forEach((c, i) => list.append(card(c, i + 1)));
    renderFooter();
  }

  /* Fetch the words of every candidate whose timeline window is not covered yet. */
  async function loadWords() {
    const missing = candidates.filter((c) => {
      const have = words.get(c.id), win = windowFor(c);
      return !have || win.start < have.win.start || win.end > have.win.end;
    });
    await Promise.all(missing.map(async (c) => {
      const win = windowFor(c);
      try {
        const { words: ws } = await App.api(`/api/transcript?start=${win.start}&end=${win.end}`);
        words.set(c.id, { win, list: ws });
      } catch (e) {
        words.set(c.id, { win, list: [] });
      }
    }));
    return missing.length;
  }

  /* An edge moved outside the old window widens it: redraw that card with room to drag on. */
  async function afterEdgeMove(c) {
    const have = words.get(c.id), win = windowFor(c);
    if (have && win.start >= have.win.start && win.end <= have.win.end) return;
    await loadWords();
    const old = list.querySelector(`li[data-id="${CSS.escape(c.id)}"]`);
    if (old) old.replaceWith(card(c, Number(old.querySelector(".rank").textContent.slice(1))));
  }

  async function update(s, changed) {
    const reels = L.isReels(s.job);
    let n = reels === false ? { text: "Not used for demos: this job is a screencast demo." }
      : L.inputNotice("job", s.job) || L.inputNotice("candidates", s.candidates);
    if (!n && !s.candidates.data.length) n = { text: "candidates.json lists no candidates." };
    App.notice(notice, n);
    body.hidden = !!n;
    if (n) return;
    if (changed.job || sentencesFor !== s.job.data.sentences) {
      sentencesFor = s.job.data.sentences;
      try { sentences = (await App.api("/api/sentences")).sentences; } catch (e) { sentences = []; }
      words.clear();
    }
    if (changed.job || changed.candidates) {
      candidates = s.candidates.data;
      const ids = new Set(candidates.map((c) => c.id));
      for (const id of Object.keys(decisions)) if (!ids.has(id)) delete decisions[id];
      for (const id of [...words.keys()]) if (!ids.has(id)) words.delete(id);
      await loadWords();
      render();
    } else if (changed.rows) {
      render();
    }
  }

  function init(p) {
    panel = p;
    notice = panel.querySelector(".notice");
    body = panel.querySelector(".body");
    decisions = App.store.get("picks", {});
    body.innerHTML = "";
    const top = el("div", "clips-player");
    player = el("video");
    player.id = "clips-player";
    player.preload = "metadata";
    player.playsInline = true;
    player.controls = true;
    player.hidden = true;
    playerLabel = el("p", "muted", "Play a candidate to hear it from the source.");
    top.append(player, playerLabel);
    const hint = el("p", "hint", "Drag an edge to move it: it snaps to the nearest sentence edge, or to a word with Alt held. Arrow keys on an edge step it.");
    list = el("ol", "cards");
    const foot = el("div", "send-bar");
    sendBtn = el("button", "primary", "Send picks");
    sendBtn.type = "button";
    sendBtn.id = "send-picks";
    sendHint = el("span", "muted", "");
    foot.append(sendBtn, sendHint);
    body.append(top, hint, list, foot);

    player.addEventListener("timeupdate", () => {
      if (playUntil !== null && player.currentTime >= playUntil) { player.pause(); playUntil = null; }
    });
    sendBtn.addEventListener("click", async () => {
      const rows = L.unsentPicks(decisions, sentPicks());
      if (!rows.length) return;
      sendBtn.disabled = true;
      try {
        const ids = await App.send(rows);
        App.setStatus(`Sent ${ids.length} pick${ids.length === 1 ? "" : "s"}. The session snaps the accepted clips and writes their EDLs.`);
      } catch (e) {
        App.setStatus(`Not sent: ${e.message}`, true);
      }
      renderFooter();
    });
  }

  App.registerTab("clips", {
    init,
    isOpen: L.clipsOpen,
    update,
    hide() { if (player && !player.paused) player.pause(); },
  });
})();
