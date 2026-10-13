// Review tab, live overlays and click-to-restyle. When turned on, each overlay page the
// selected render's .edl.json names plays in an <iframe sandbox="allow-scripts"> over its
// region of the picture while it is on screen, served with seek-shim.js and
// overlay-bridge.js injected (GET /media/overlay, one key per page). Every time update
// seeks it to the overlay's own clock. In pick mode a click on an overlay names the
// element: its selector, its box and its computed styles. Edits show at once (the bridge
// applies them inside the frame), and "Send restyle" adds one `restyle` row. The frames
// get no same-origin rights, so they cannot reach this page or the server's API; only
// "ready" and "picked" messages from frames this file made are read.
// Builds on tab-review.js through App.review.
"use strict";

(() => {
  const { L } = App;
  const R = App.review;
  const COLOUR_PROPS = ["color", "background-color"];
  let layer, pickBox, liveToggle, pickBtn, notice, form, targetEl, textEl, fieldsEl, noteEl, sendBtn, undoBtn, sentEl;
  // frames: [{o, iframe, ready}]; picked: {frame, msg, t}; edits: {property: raw}
  const st = { key: null, live: false, picking: false, frames: [], picked: null, edits: {} };

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const post = (f, msg) => { if (f.iframe.contentWindow) f.iframe.contentWindow.postMessage(msg, "*"); };

  function outSize() {
    const side = R.sidecar();
    const o = (side && side.data.edl && side.data.edl.output) || {};
    return { width: Number(o.width) || 1080, height: Number(o.height) || 1920 };
  }

  // --- the frames --------------------------------------------------------------------------------

  function clearFrames() {
    setPicked(null);
    layer.replaceChildren();
    st.frames = [];
  }

  function build() {
    clearFrames();
    const r = R.render(), side = R.sidecar();
    const overlays = side ? L.liveOverlays(side.data, side.overlayKeys) : [];
    App.notice(notice, !r ? null
      : !side ? { text: "No live overlays for this render: it has no .edl.json." }
        : !overlays.length ? { text: "This render has no overlays." } : null);
    pickBtn.disabled = !st.live || !overlays.length;
    if (!st.live) return;
    for (const o of overlays) {
      const iframe = el("iframe", "live-frame");
      // Scripts only: the page gets an opaque origin, so it cannot reach this page,
      // its storage or the server's API (and its CSP lets it fetch nothing).
      iframe.setAttribute("sandbox", "allow-scripts");
      iframe.setAttribute("referrerpolicy", "no-referrer");
      iframe.title = `Overlay ${o.index}: ${o.html.split("/").pop()}`;
      iframe.style.width = `${o.rect.w}px`;
      iframe.style.height = `${o.rect.h}px`;
      iframe.src = `/media/overlay?${new URLSearchParams({ path: o.html, k: o.key })}`;
      layer.append(iframe);
      st.frames.push({ o, iframe, ready: false });
    }
    place();
  }

  /* Every frame over its region, shown and seeked while its overlay is on screen. */
  function place() {
    if (!st.frames.length) return;
    const video = R.video(), p = R.pictureRect(), s = R.stage().getBoundingClientRect(), out = outSize();
    for (const f of st.frames) {
      const b = L.regionBox(f.o.rect, out, p);
      f.iframe.style.left = `${p.left - s.left + b.left}px`;
      f.iframe.style.top = `${p.top - s.top + b.top}px`;
      f.iframe.style.transform = `scale(${b.scale})`;
      const t = L.overlayTime(f.o, video.currentTime);
      f.iframe.classList.toggle("on", f.ready && t !== null);
      if (f.ready && t !== null) post(f, { type: "seek", t });
    }
    placePickBox();
  }

  function placePickBox() {
    const pk = st.picked;
    pickBox.hidden = !pk || !pk.frame.iframe.classList.contains("on");
    if (pickBox.hidden) return;
    const p = R.pictureRect(), s = R.stage().getBoundingClientRect();
    const b = L.regionBox(pk.frame.o.rect, outSize(), p), m = pk.msg.box;
    pickBox.style.left = `${p.left - s.left + b.left + m.x * b.scale}px`;
    pickBox.style.top = `${p.top - s.top + b.top + m.y * b.scale}px`;
    pickBox.style.width = `${m.w * b.scale}px`;
    pickBox.style.height = `${m.h * b.scale}px`;
  }

  // Only "ready" and "picked", only from a frame this file made (event.source), and only
  // from an opaque origin: a sandboxed frame without same-origin rights.
  function onMessage(ev) {
    if (ev.origin !== "null") return;
    const f = st.frames.find((x) => x.iframe.contentWindow === ev.source);
    const m = f && L.bridgeMessage(ev.data);
    if (!m) return;
    if (m.type === "ready") {
      f.ready = true;
      post(f, { type: "init", transparent: f.o.transparent });
      post(f, { type: "pick", on: st.picking });
      place();
    } else if (st.picking) {
      setPicking(false);
      setPicked({ frame: f, msg: m, t: R.video().currentTime });
    }
  }

  // --- picking and restyling -------------------------------------------------------------------

  function setPicking(on) {
    st.picking = on && st.live;
    pickBtn.setAttribute("aria-pressed", String(st.picking));
    R.stage().classList.toggle("picking", st.picking);
    for (const f of st.frames) if (f.ready) post(f, { type: "pick", on: st.picking });
    if (st.picking) {
      R.video().pause();
      R.setTool("point");
      const on = st.frames.some((f) => L.overlayTime(f.o, R.video().currentTime) !== null);
      App.setStatus(on ? "Click an element of an overlay to restyle it."
        : "No overlay is on screen now: seek to one (the Overlays lane of the timeline), then click it.");
    }
  }

  function setPicked(pk) {
    if (st.picked) post(st.picked.frame, { type: "clearStyle" });
    st.picked = pk;
    st.edits = {};
    noteEl.value = "";
    renderForm();
    placePickBox();
  }

  /* Send the valid edits to the bridge: take everything back, then set what is there now. */
  function applyEdits() {
    const pk = st.picked;
    if (!pk) return;
    const changes = {};
    for (const [p, v] of Object.entries(st.edits)) if (v.trim() && L.isCssValue(v)) changes[p] = v.trim();
    post(pk.frame, { type: "clearStyle" });
    if (Object.keys(changes).length) post(pk.frame, { type: "applyStyle", selector: pk.msg.selector, changes });
  }

  function update() {
    const pk = st.picked, r = R.render();
    if (!pk || !r) { sendBtn.disabled = true; return; }
    const { row, problem } = L.restyleRow(r.clip, r.version, pk.t, pk.frame.o, pk.msg, st.edits, noteEl.value);
    sendBtn.disabled = !row;
    form.querySelector(".sent-status").textContent = problem && problem !== "Change a value to send it." ? problem : "";
    for (const inp of fieldsEl.querySelectorAll("input[data-prop]")) {
      inp.closest(".style-field").classList.toggle("changed", !!(row && row.changes[inp.dataset.prop]));
    }
  }

  function fieldEl(prop, computed) {
    const row = el("div", "style-field");
    const label = el("label", "", prop);
    label.htmlFor = `restyle-${prop}`;
    row.append(label);
    const input = el("input");
    input.type = "text";
    input.id = `restyle-${prop}`;
    input.dataset.prop = prop;
    input.spellcheck = false;
    input.value = computed;
    if (COLOUR_PROPS.includes(prop)) {
      const swatch = el("input");
      swatch.type = "color";
      swatch.setAttribute("aria-label", `${prop}: pick a colour`);
      const hex = L.cssColourToHex(computed);
      swatch.value = hex ? hex.toLowerCase() : "#000000";
      swatch.addEventListener("input", () => {
        input.value = swatch.value.toUpperCase();
        input.dispatchEvent(new Event("input"));
      });
      row.append(swatch);
    }
    input.addEventListener("input", () => {
      st.edits[prop] = input.value;
      applyEdits();
      update();
    });
    row.append(input);
    return row;
  }

  function renderForm() {
    const pk = st.picked;
    form.hidden = !pk;
    fieldsEl.replaceChildren();
    if (!pk) return;
    targetEl.textContent = `Overlay ${pk.frame.o.index} · ${pk.frame.o.html.split("/").pop()} · ${pk.msg.selector}`;
    textEl.textContent = pk.msg.text ? `“${pk.msg.text}”` : "";
    for (const p of L.RESTYLE_PROPS) fieldsEl.append(fieldEl(p, pk.msg.styles[p]));
    update();
  }

  async function send() {
    const pk = st.picked, r = R.render();
    if (!pk || !r) return;
    const { row } = L.restyleRow(r.clip, r.version, pk.t, pk.frame.o, pk.msg, st.edits, noteEl.value);
    if (!row) return;
    sendBtn.disabled = true;
    try {
      await App.send([row]);
      App.setStatus(`Sent a restyle of overlay ${row.overlay}. The next version appears here when it is rendered.`);
      if (st.picked === pk) setPicked(pk);           // back to the page as rendered, ready for another
    } catch (e) {
      App.setStatus(`Not sent: ${e.message}`, true);
      update();
    }
  }

  function renderSent() {
    const r = R.render();
    sentEl.replaceChildren();
    for (const row of r ? L.styleSent(App.state.rows, r.clip).filter((x) => x.kind === "restyle") : []) {
      const li = el("li");
      li.append(el("div", "meta", `v${row.version} · ${L.fmt(row.t)} · overlay ${row.overlay} · ${row.selector}`),
        el("p", "", L.changeSummary(row.changes)));
      if (row.text) li.append(el("p", "muted", row.text));
      sentEl.append(li);
    }
    sentEl.hidden = !sentEl.children.length;
  }

  // --- the render changed ------------------------------------------------------------------------

  function onRender() {
    const r = R.render(), side = R.sidecar();
    const key = r ? `${r.clip}@${r.version}:${side ? JSON.stringify(side.overlayKeys) : ""}` : null;
    document.getElementById("live-panel").hidden = !r;
    if (key !== st.key) {
      st.key = key;
      setPicking(false);
      build();
    }
    renderSent();
  }

  function init() {
    const stage = R.stage();
    layer = el("div");
    layer.id = "live-layer";
    stage.insertBefore(layer, document.getElementById("draw-layer"));
    pickBox = el("div");
    pickBox.id = "pick-box";
    pickBox.hidden = true;
    stage.append(pickBox);
    // In pick mode a click that misses the overlays must not open a comment on the picture.
    stage.addEventListener("click", (ev) => {
      if (!st.picking || ev.target !== R.video()) return;
      ev.stopPropagation();
      App.setStatus("That is the video: click an element of an overlay, or turn Pick off.");
    }, true);

    const box = document.getElementById("live-panel");
    const toggleLabel = el("label", "choice");
    liveToggle = el("input");
    liveToggle.type = "checkbox";
    liveToggle.id = "live-toggle";
    toggleLabel.append(liveToggle, el("span", "", "Play the overlays live over the picture"));
    liveToggle.addEventListener("change", () => {
      st.live = liveToggle.checked;
      setPicking(false);
      build();
    });
    pickBtn = el("button", "", "Pick an element");
    pickBtn.type = "button";
    pickBtn.id = "pick-toggle";
    pickBtn.disabled = true;
    pickBtn.setAttribute("aria-pressed", "false");
    pickBtn.addEventListener("click", () => setPicking(!st.picking));
    const bar = el("div", "row live-bar");
    bar.append(toggleLabel, pickBtn);
    notice = el("p", "notice");
    notice.hidden = true;
    const hint = el("p", "hint", "Live overlays play over the render's own, burned-in ones. They are drawn by your "
      + "browser at your screen size, so they may sit a little off from the render.");

    form = el("form");
    form.id = "restyle-form";
    form.hidden = true;
    targetEl = el("p", "restyle-target");
    targetEl.id = "restyle-target";
    textEl = el("p", "muted restyle-text");
    fieldsEl = el("div", "restyle-fields");
    noteEl = el("textarea");
    noteEl.id = "restyle-note";
    noteEl.rows = 2;
    noteEl.placeholder = "Note for this restyle (optional)";
    noteEl.setAttribute("aria-label", "Note for the restyle");
    noteEl.addEventListener("input", update);
    sendBtn = el("button", "primary", "Send restyle");
    sendBtn.type = "submit";
    sendBtn.id = "restyle-send";
    undoBtn = el("button", "ghost", "Undo changes");
    undoBtn.type = "button";
    undoBtn.addEventListener("click", () => { if (st.picked) setPicked(st.picked); });
    const close = el("button", "ghost", "Close");
    close.type = "button";
    close.addEventListener("click", () => setPicked(null));
    const actions = el("div", "row");
    actions.append(sendBtn, undoBtn, close);
    form.append(targetEl, textEl, fieldsEl, noteEl, actions, el("p", "sent-status"));
    form.addEventListener("submit", (ev) => { ev.preventDefault(); send(); });
    sentEl = el("ol", "style-sent");
    sentEl.hidden = true;
    box.append(el("h2", "", "Live overlays"), bar, notice, hint, form, sentEl);

    window.addEventListener("message", onMessage);
    R.on("render", onRender);
    R.on("time", place);
    R.on("resize", place);
    R.on("rows", renderSent);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
