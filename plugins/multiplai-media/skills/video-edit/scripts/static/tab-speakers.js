// Speakers tab: draw a box per speaker on a frame of the source, name the boxes, map each
// transcript speaker label to a box, and send them as one `panels` row (source pixels).
"use strict";

(() => {
  const { L } = App;
  const fmt = L.fmt;
  let notice, body, video, stage, layer, scrub, timeEl, lateBtn, backBtn, boxList, mapTable, noteEl,
    sendBtn, problemEl, sentEl;
  // This viewer's draft: boxes in source pixels, label → box name, the frame time, the note.
  let draft = { boxes: [], mapping: {}, t: null, note: "" };
  let draftKey = null;              // where the draft is stored: one key per source recording
  let speakers = [], speakersFor = null, selected = null, backTo = null, playUntil = null, nextId = 1;

  const W = () => video.videoWidth || 0;
  const H = () => video.videoHeight || 0;
  const save = () => { if (draftKey) App.store.set(draftKey, draft); };

  // Drafts are stored per source recording, so a later job served on the same port
  // does not inherit this one's boxes.
  function loadDraft(key) {
    draftKey = key;
    const saved = App.store.get(key, null);
    draft = saved && Array.isArray(saved.boxes)
      ? { boxes: saved.boxes, mapping: saved.mapping || {}, t: saved.t ?? null, note: saved.note || "" }
      : { boxes: [], mapping: {}, t: null, note: "" };
    for (const b of draft.boxes) { b.id = nextId++; }
    selected = null;
    noteEl.value = draft.note;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function lastSent() {
    const rows = L.rowsOfKind(App.state.rows, "panels");
    return rows.length ? rows[rows.length - 1] : null;
  }

  // --- the frame ---------------------------------------------------------------------

  function seek(t) {
    if (!Number.isFinite(video.duration)) return;
    draft.t = Math.max(0, Math.min(video.duration, t));
    video.currentTime = draft.t;
    save();
  }

  function showTime() {
    scrub.value = String(video.currentTime);
    timeEl.textContent = `${fmt(video.currentTime)} / ${fmt(video.duration || 0)}`;
  }

  function onMetadata() {
    stage.style.aspectRatio = `${W()} / ${H()}`;
    scrub.max = String(video.duration);
    seek(draft.t !== null ? draft.t : video.duration * 0.1);
    renderBoxes();
  }

  // --- boxes -------------------------------------------------------------------------

  function toSource(ev) {
    const r = layer.getBoundingClientRect();
    return { x: ((ev.clientX - r.left) / r.width) * W(), y: ((ev.clientY - r.top) / r.height) * H() };
  }

  function placeBox(div, b) {
    div.style.left = `${(b.x / W()) * 100}%`;
    div.style.top = `${(b.y / H()) * 100}%`;
    div.style.width = `${(b.w / W()) * 100}%`;
    div.style.height = `${(b.h / H()) * 100}%`;
  }

  function renderBoxes() {
    layer.replaceChildren();
    if (!W()) return;
    for (const b of draft.boxes) {
      const div = el("div", "spk-box");
      div.dataset.id = String(b.id);
      div.classList.toggle("selected", b.id === selected);
      div.append(el("span", "spk-box-name", b.name || "?"));
      for (const corner of ["nw", "ne", "sw", "se"]) {
        const h = el("span", `spk-handle ${corner}`);
        h.dataset.corner = corner;
        div.append(h);
      }
      placeBox(div, b);
      layer.append(div);
    }
    renderSide();
  }

  function renderSide() {
    boxList.replaceChildren();
    for (const b of draft.boxes) {
      const li = el("li", b.id === selected ? "selected" : "");
      const name = el("input");
      name.type = "text";
      name.value = b.name;
      name.maxLength = 40;
      name.setAttribute("aria-label", "Box name");
      name.addEventListener("focus", () => { selected = b.id; renderBoxesOnly(); });
      name.addEventListener("change", () => {
        const old = b.name;
        b.name = name.value.trim();
        for (const [label, n] of Object.entries(draft.mapping)) if (n === old) draft.mapping[label] = b.name;
        save();
        renderBoxes();
      });
      const r = L.roundRect(b);
      const coords = el("span", "muted coords", `x ${r.x} y ${r.y} · ${r.w}×${r.h}`);
      const del = el("button", "del", "✕");
      del.type = "button";
      del.setAttribute("aria-label", `Delete box ${b.name}`);
      del.addEventListener("click", () => removeBox(b.id));
      li.append(name, coords, del);
      boxList.append(li);
    }
    if (!draft.boxes.length) boxList.append(el("li", "muted", "No boxes yet: drag on the picture to draw one."));
    renderMap();
    renderSend();
  }

  // Redraw only the boxes on the picture (keeps focus in the side list).
  function renderBoxesOnly() {
    for (const div of layer.children) div.classList.toggle("selected", div.dataset.id === String(selected));
  }

  function removeBox(id) {
    const b = draft.boxes.find((x) => x.id === id);
    draft.boxes = draft.boxes.filter((x) => x.id !== id);
    if (b) for (const [label, n] of Object.entries(draft.mapping)) if (n === b.name) draft.mapping[label] = "";
    if (selected === id) selected = null;
    save();
    renderBoxes();
  }

  function startPointer(ev) {
    if (!W() || ev.button !== 0) return;
    ev.preventDefault();
    if (!video.paused) video.pause();
    layer.setPointerCapture(ev.pointerId);
    const p0 = toSource(ev);
    const boxDiv = ev.target.closest(".spk-box");
    const corner = ev.target.dataset ? ev.target.dataset.corner : undefined;
    let b, orig, mode, drawn = null;
    if (boxDiv) {
      b = draft.boxes.find((x) => String(x.id) === boxDiv.dataset.id);
      orig = { ...b };
      mode = corner ? "resize" : "move";
      selected = b.id;
      renderBoxesOnly();
    } else {
      mode = "draw";
      drawn = el("div", "spk-box drawing");
      layer.append(drawn);
    }
    const move = (e) => {
      const p = toSource(e);
      if (mode === "draw") {
        placeBox(drawn, L.clampRect(L.normRect(p0.x, p0.y, p.x, p.y), W(), H(), 1));
      } else {
        Object.assign(b, mode === "move" ? L.moveRect(orig, p.x - p0.x, p.y - p0.y, W(), H())
          : L.resizeRect(orig, corner, p.x, p.y, W(), H()));
        placeBox(layer.querySelector(`.spk-box[data-id="${b.id}"]`), b);
      }
    };
    const up = (e) => {
      layer.removeEventListener("pointermove", move);
      layer.removeEventListener("pointerup", up);
      layer.removeEventListener("pointercancel", up);
      if (mode === "draw") {
        const p = toSource(e);
        const r = L.normRect(p0.x, p0.y, p.x, p.y);
        // A click, or a box smaller than 1% of the frame, is not a box.
        if (r.w >= W() / 100 && r.h >= H() / 100) {
          const nb = { id: nextId++, name: L.nextBoxName(draft.boxes), ...L.clampRect(r, W(), H()) };
          draft.boxes.push(nb);
          selected = nb.id;
        }
      }
      save();
      renderBoxes();
    };
    layer.addEventListener("pointermove", move);
    layer.addEventListener("pointerup", up);
    layer.addEventListener("pointercancel", up);
  }

  // --- who is who --------------------------------------------------------------------

  function playLine(line) {
    seek(line.start);
    playUntil = line.end;
    video.muted = false;
    video.play().catch(() => {});
  }

  function renderMap() {
    mapTable.replaceChildren();
    if (!speakers.length) {
      mapTable.append(el("p", "muted", "The transcript has no speaker labels, so there is nothing to map. Send the boxes on their own."));
      return;
    }
    const names = draft.boxes.map((b) => b.name).filter(Boolean);
    for (const s of speakers) {
      const row = el("div", "map-row");
      const head = el("div", "map-head");
      head.append(el("strong", "", s.label));
      const sel = el("select");
      sel.setAttribute("aria-label", `Box for ${s.label}`);
      sel.dataset.label = s.label;
      for (const n of ["", ...names]) {
        const o = el("option", "", n || "— no box —");
        o.value = n;
        o.selected = (draft.mapping[s.label] || "") === n;
        sel.append(o);
      }
      sel.addEventListener("change", () => { draft.mapping[s.label] = sel.value; save(); renderSend(); });
      head.append(sel);
      const lines = el("ul", "plain lines");
      for (const line of s.lines) {
        const li = el("li");
        const b = el("button", "line", `${fmt(line.start)} ${line.text}`);
        b.type = "button";
        b.title = "Play this line from the source";
        b.addEventListener("click", () => playLine(line));
        li.append(b);
        lines.append(li);
      }
      row.append(head, lines);
      mapTable.append(row);
    }
  }

  // --- sending -----------------------------------------------------------------------

  function renderSend() {
    const problem = L.panelsProblem(draft.boxes, draft.mapping, speakers.map((x) => x.label));
    problemEl.textContent = problem || "";
    sendBtn.disabled = !!problem;
    const last = lastSent();
    sentEl.textContent = last
      ? `Sent: ${Object.keys(last.panels).join(", ")}${Object.keys(last.speakers).length
        ? ` · ${Object.entries(last.speakers).map(([l, n]) => `${l} → ${n}`).join(", ")}` : ""}`
      : "";
  }

  async function send() {
    const problem = L.panelsProblem(draft.boxes, draft.mapping, speakers.map((x) => x.label));
    if (problem) return;
    sendBtn.disabled = true;
    try {
      await App.send([L.panelsRow(draft.boxes, draft.mapping, draft.note)]);
      App.setStatus("Sent the speaker boxes. The session writes them into every EDL's layout.");
    } catch (e) {
      App.setStatus(`Not sent: ${e.message}`, true);
    }
    renderSend();
  }

  // --- the tab -----------------------------------------------------------------------

  async function update(s, changed) {
    const reels = L.isReels(s.job);
    const n = reels === false ? { text: "Not used for demos: this job is a screencast demo." } : L.inputNotice("job", s.job);
    App.notice(notice, n);
    body.hidden = !!n;
    if (n || !(changed.job || changed.rows)) return;
    const key = `speakers:${s.job.data.source}`;
    if (key !== draftKey) loadDraft(key);
    if (!video.src) video.src = App.mediaUrl("/media/source");
    if (speakersFor !== s.job.data.transcript || changed.job) {
      speakersFor = s.job.data.transcript;
      try { speakers = (await App.api("/api/transcript")).speakers; } catch (e) { speakers = []; }
    }
    if (!draft.boxes.length) {
      const last = lastSent();
      if (last) {
        draft.boxes = Object.entries(last.panels).map(([name, r]) => ({ id: nextId++, name, ...r }));
        draft.mapping = { ...last.speakers };
      }
    }
    renderBoxes();
  }

  function init(panel) {
    notice = panel.querySelector(".notice");
    body = panel.querySelector(".body");
    const grid = el("div", "speakers-grid");
    const frameCol = el("div", "frame-col");
    stage = el("div", "frame-stage");
    video = el("video");
    video.id = "speakers-video";
    video.preload = "auto";
    video.muted = true;
    video.playsInline = true;
    layer = el("div", "box-layer");
    stage.append(video, layer);
    const controls = el("div", "row");
    scrub = el("input");
    scrub.type = "range";
    scrub.min = "0";
    scrub.step = "0.04";
    scrub.value = "0";
    scrub.id = "speakers-scrub";
    scrub.setAttribute("aria-label", "Frame time");
    timeEl = el("span", "muted", "0:00.0");
    lateBtn = el("button", "", "Check late frame");
    lateBtn.type = "button";
    lateBtn.title = "Jump to 85% of the recording with the boxes kept, to check the layout holds";
    backBtn = el("button", "ghost", "Back");
    backBtn.type = "button";
    backBtn.hidden = true;
    controls.append(scrub, timeEl, lateBtn, backBtn);
    frameCol.append(stage, controls,
      el("p", "hint", "Drag on the picture to draw a box around each speaker, clear of name tags and logos. Drag a box to move it, a corner to resize it."));

    const side = el("div", "side-col");
    boxList = el("ol", "box-list");
    mapTable = el("div", "map");
    noteEl = el("textarea");
    noteEl.rows = 2;
    noteEl.placeholder = "Note for the session (optional)";
    noteEl.setAttribute("aria-label", "Note on the speaker boxes");
    noteEl.value = draft.note;
    sendBtn = el("button", "primary", "Send boxes");
    sendBtn.type = "button";
    sendBtn.id = "send-panels";
    problemEl = el("p", "muted");
    sentEl = el("p", "sent-status");
    side.append(el("h2", "", "Boxes"), boxList, el("h2", "", "Who is who"), mapTable, noteEl, sendBtn, problemEl, sentEl);
    grid.append(frameCol, side);
    body.append(grid);

    video.addEventListener("loadedmetadata", onMetadata);
    video.addEventListener("timeupdate", () => {
      showTime();
      if (playUntil !== null && video.currentTime >= playUntil) { video.pause(); playUntil = null; draft.t = video.currentTime; save(); }
    });
    video.addEventListener("seeked", showTime);
    scrub.addEventListener("input", () => seek(Number(scrub.value)));
    lateBtn.addEventListener("click", () => {
      backTo = video.currentTime;
      backBtn.textContent = `Back to ${fmt(backTo)}`;
      backBtn.hidden = false;
      seek(video.duration * 0.85);
    });
    backBtn.addEventListener("click", () => { if (backTo !== null) seek(backTo); backBtn.hidden = true; });
    layer.addEventListener("pointerdown", startPointer);
    noteEl.addEventListener("input", () => { draft.note = noteEl.value; save(); });
    sendBtn.addEventListener("click", send);
    window.addEventListener("resize", () => { if (App.current() === "speakers") renderBoxes(); });
    document.addEventListener("keydown", (ev) => {
      if (App.current() !== "speakers" || selected === null) return;
      if (ev.target instanceof HTMLInputElement || ev.target instanceof HTMLTextAreaElement
          || ev.target instanceof HTMLSelectElement) return;
      if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); removeBox(selected); }
    });
  }

  App.registerTab("speakers", {
    init,
    isOpen: L.speakersOpen,
    update,
    hide() { if (video && !video.paused) video.pause(); },
  });
})();
