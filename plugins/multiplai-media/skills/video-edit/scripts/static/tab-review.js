// Review tab: play every render, click the picture to comment on that moment and spot,
// or draw on the paused frame (pen, arrow, box) and send the marked-up frame.
"use strict";

(() => {
  const { $, L } = App;
  const fmt = L.fmt;
  let video, stage, markers, composer, composerText, layer;
  const state = {
    clips: [], clip: null, version: null, pending: [], sent: [], draft: null,
    tool: "point", colour: L.DRAW_COLOURS[0][1], stroke: null,
  };
  const COMMENT_KINDS = ["point", "drawing"];

  // --- pending comments survive a reload (per viewer, best effort) -------------

  const draftKey = () => `pending:${state.clip}`;
  const savePending = () => App.store.set(draftKey(), state.pending);
  const loadPending = () => { state.pending = App.store.get(draftKey(), []); };
  const kindOf = (c) => c.kind || "point";
  // A drawing's small preview, kept per viewer by image id so sent drawings show it too.
  const thumbOf = (image) => App.store.get(`thumb:${image}`, null);

  // --- clips and versions ---------------------------------------------------------

  function currentVersions() {
    const c = state.clips.find((x) => x.clip === state.clip);
    return c ? c.versions : [];
  }

  function renderClips() {
    const nav = $("renders");
    nav.replaceChildren();
    for (const c of state.clips) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = c.clip;
      const latest = c.versions[c.versions.length - 1].version;
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = `v${latest}`;
      b.append(badge);
      b.setAttribute("aria-current", String(c.clip === state.clip));
      b.addEventListener("click", () => selectClip(c.clip));
      nav.append(b);
    }
    const sel = $("version");
    sel.replaceChildren();
    for (const v of currentVersions()) {
      const o = document.createElement("option");
      o.value = String(v.version);
      o.textContent = `version ${v.version}`;
      o.selected = v.version === state.version;
      sel.append(o);
    }
  }

  function selectClip(clip, version) {
    state.clip = clip;
    const versions = currentVersions();
    state.version = version || versions[versions.length - 1].version;
    const v = versions.find((x) => x.version === state.version);
    video.src = App.mediaUrl(`/media/${encodeURIComponent(v.name)}`);
    closeComposer();
    loadPending();
    renderClips();
    refreshSent();
  }

  function updateVideos(videos) {
    const before = JSON.stringify(state.clips);
    state.clips = videos;
    App.notice($("review-notice"), videos.length ? null
      : { text: "No renders in the job directory yet. They appear here as soon as the session renders them." });
    $("review-body").hidden = !videos.length;
    if (!state.clip && videos.length) return selectClip(videos[0].clip);
    if (before !== JSON.stringify(videos)) {
      renderClips();
      const latest = currentVersions().slice(-1)[0];
      if (latest && latest.version > state.version) App.setStatus(`${state.clip} version ${latest.version} is ready — pick it in the version menu.`);
    }
  }

  function refreshSent() {
    state.sent = (App.state.rows || []).filter((r) => COMMENT_KINDS.includes(kindOf(r)) && r.video === state.clip);
    renderComments();
  }

  // --- the picture and the drawing layer --------------------------------------------

  // The picture inside the <video> box (it letterboxes when the box's shape differs).
  function pictureRect() {
    const r = video.getBoundingClientRect();
    const vw = video.videoWidth || r.width, vh = video.videoHeight || r.height;
    const scale = Math.min(r.width / vw, r.height / vh);
    const w = vw * scale, h = vh * scale;
    return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
  }

  function paint(ctx, shapes, w, h) {
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.shadowColor = "rgba(0, 0, 0, .55)";
    ctx.shadowBlur = Math.max(1, L.strokeWidth(w) / 2);
    for (const s of shapes) {
      const { color, width, paths } = L.shapePaths(s, w, h);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      for (const path of paths) {
        ctx.beginPath();
        ctx.moveTo(path[0][0], path[0][1]);
        for (const [x, y] of path.slice(1)) ctx.lineTo(x, y);
        ctx.stroke();
      }
    }
  }

  // Shapes on the layer: the drawing being made, else pending drawings near this moment.
  function layerShapes() {
    if (state.draft && state.draft.kind === "drawing") {
      return state.draft.shapes.concat(state.stroke ? [state.stroke] : []);
    }
    if (!video.paused) return [];
    return state.pending.filter((c) => kindOf(c) === "drawing" && c.version === state.version
      && Math.abs(c.t - video.currentTime) < 0.75).flatMap((c) => c.shapes);
  }

  function renderLayer() {
    const p = pictureRect(), s = stage.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    layer.style.left = `${p.left - s.left}px`;
    layer.style.top = `${p.top - s.top}px`;
    layer.style.width = `${p.width}px`;
    layer.style.height = `${p.height}px`;
    const W = Math.max(1, Math.round(p.width * dpr)), H = Math.max(1, Math.round(p.height * dpr));
    if (layer.width !== W || layer.height !== H) { layer.width = W; layer.height = H; }
    const ctx = layer.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    paint(ctx, layerShapes(), W, H);
    layer.classList.toggle("active", state.tool !== "point");
    const drawing = state.draft && state.draft.kind === "drawing" && state.draft.shapes.length > 0;
    $("draw-undo").disabled = !drawing;
    $("draw-clear").disabled = !drawing;
  }

  function setTool(tool) {
    state.tool = tool;
    for (const b of document.querySelectorAll("#draw-tools [data-tool]")) b.setAttribute("aria-pressed", String(b.dataset.tool === tool));
    renderLayer();
  }

  function setColour(colour) {
    state.colour = colour.toUpperCase();
    for (const b of document.querySelectorAll("#draw-colours button")) b.setAttribute("aria-pressed", String(b.dataset.colour === state.colour));
    if (state.tool === "point") setTool("pen");
  }

  function startStroke(ev) {
    if (state.tool === "point" || ev.button !== 0) return;
    ev.preventDefault();
    video.pause();
    if (!state.draft || state.draft.kind !== "drawing") openComposer({ kind: "drawing", t: video.currentTime, shapes: [] });
    const pt = L.toPictureFrac(ev.clientX, ev.clientY, layer.getBoundingClientRect());
    state.stroke = { tool: state.tool, color: state.colour, points: [pt, pt] };
    layer.setPointerCapture(ev.pointerId);
    renderLayer();
  }

  function moveStroke(ev) {
    if (!state.stroke) return;
    const pt = L.toPictureFrac(ev.clientX, ev.clientY, layer.getBoundingClientRect());
    if (state.stroke.tool === "pen") state.stroke.points.push(pt);
    else state.stroke.points[1] = pt;
    renderLayer();
  }

  function endStroke() {
    if (!state.stroke) return;
    const s = state.stroke;
    state.stroke = null;
    const shape = L.finishShape(s.tool, s.color, s.tool === "pen" ? s.points.slice(1) : s.points);
    if (shape && state.draft && state.draft.kind === "drawing") state.draft.shapes.push(shape);
    if (state.draft && state.draft.kind === "drawing" && !state.draft.shapes.length) closeComposer();
    else composerLabel();
    renderLayer();
  }

  /* The frame at native size with the shapes on it, as a JPEG blob and a small preview. */
  async function exportDrawing(shapes) {
    const size = L.exportSize(video.videoWidth, video.videoHeight);
    if (!size) throw new Error("the video has not loaded yet");
    const c = document.createElement("canvas");
    c.width = size.width; c.height = size.height;
    const ctx = c.getContext("2d");
    ctx.drawImage(video, 0, 0, size.width, size.height);
    paint(ctx, shapes, size.width, size.height);
    const blob = await new Promise((resolve) => c.toBlob(resolve, "image/jpeg", 0.85));
    if (!blob) throw new Error("the browser could not export the frame");
    if (blob.size > L.MAX_MARK_BYTES) throw new Error("the marked frame is larger than 4 MB");
    const t = document.createElement("canvas");
    t.width = 192; t.height = Math.max(1, Math.round(192 * size.height / size.width));
    t.getContext("2d").drawImage(c, 0, 0, t.width, t.height);
    return { blob, thumb: t.toDataURL("image/jpeg", 0.7) };
  }

  // --- the composer ---------------------------------------------------------------

  function composerLabel() {
    const d = state.draft;
    if (!d) return;
    const n = d.kind === "drawing" ? d.shapes.length : 0;
    $("composer-label").textContent = d.kind === "drawing"
      ? `Drawing at ${fmt(d.t)} (${n} shape${n === 1 ? "" : "s"})`
      : `Comment at ${fmt(d.t)}`;
    $("composer-submit").textContent = d.kind === "drawing" ? "Add drawing" : "Add comment";
    composerText.placeholder = d.kind === "drawing" ? "What should change here? (optional)" : "What should change here?";
  }

  function openComposer(draft) {
    state.draft = draft;
    composerLabel();
    composer.hidden = false;
    composerText.value = "";
    if (draft.kind !== "drawing") composerText.focus();
    renderMarkers();
  }

  function closeComposer() {
    state.draft = null;
    state.stroke = null;
    composer.hidden = true;
    renderMarkers();
  }

  const newKey = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  async function submitComposer() {
    const d = state.draft;
    if (!d) return;
    const text = composerText.value.trim();
    if (d.kind === "point") {
      if (!text) return;
      // crypto.randomUUID needs a secure origin, and a plain-http page on a LAN name is not one.
      state.pending.push({ key: newKey(), kind: "point", video: state.clip, version: state.version, t: d.t, x: d.x, y: d.y, text });
    } else if (d.kind === "drawing") {
      if (!d.shapes.length) return;
      $("composer-submit").disabled = true;
      try {
        const { blob, thumb } = await exportDrawing(d.shapes);
        const { image } = await App.postBlob("/api/marks", blob, "image/jpeg");
        App.store.set(`thumb:${image}`, thumb);
        state.pending.push({ key: newKey(), kind: "drawing", video: state.clip, version: state.version, t: d.t, image, shapes: d.shapes, text });
      } catch (e) {
        App.setStatus(`Drawing not added: ${e.message}`, true);
        return;
      } finally {
        $("composer-submit").disabled = false;
      }
    }
    savePending();
    closeComposer();
    renderComments();
  }

  // --- comment lists --------------------------------------------------------------

  function renderMarkers() {
    markers.replaceChildren();
    const p = pictureRect(), s = stage.getBoundingClientRect();
    const shown = state.pending.filter((c) => kindOf(c) === "point" && c.version === state.version && Math.abs(c.t - video.currentTime) < 0.75);
    if (state.draft && state.draft.kind === "point") shown.push({ ...state.draft, draft: true });
    shown.forEach((c) => {
      const m = document.createElement("div");
      m.className = "marker";
      m.textContent = c.draft ? "+" : String(state.pending.indexOf(c) + 1);
      m.style.left = `${p.left - s.left + c.x * p.width}px`;
      m.style.top = `${p.top - s.top + c.y * p.height}px`;
      markers.append(m);
    });
    renderLayer();
  }

  function seekTo(version, t) {
    if (version !== state.version) selectClip(state.clip, version);
    const seek = () => { video.currentTime = t; video.pause(); };
    if (video.readyState >= 1) seek(); else video.addEventListener("loadedmetadata", seek, { once: true });
  }

  function tsButton(c) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ts";
    b.textContent = `${kindOf(c) === "drawing" ? "✎ " : ""}${fmt(c.t)} · v${c.version}`;
    b.addEventListener("click", () => seekTo(c.version, c.t));
    return b;
  }

  function thumbImg(c) {
    const src = kindOf(c) === "drawing" ? (c.thumb || thumbOf(c.image)) : null;
    if (!src || !src.startsWith("data:image/jpeg;")) return null;
    const img = document.createElement("img");
    img.className = "thumb";
    img.alt = `Drawing with ${c.shapes.length} shape${c.shapes.length === 1 ? "" : "s"}`;
    img.src = src;
    return img;
  }

  function renderComments() {
    const list = $("pending");
    list.replaceChildren();
    state.pending.sort((a, b) => a.version - b.version || a.t - b.t);
    state.pending.forEach((c, i) => {
      const li = document.createElement("li");
      const meta = document.createElement("div");
      meta.className = "meta";
      const num = document.createElement("span");
      num.textContent = `${i + 1}`;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "del";
      del.textContent = "✕";
      del.setAttribute("aria-label", "Delete comment");
      del.addEventListener("click", () => {
        state.pending = state.pending.filter((x) => x.key !== c.key);
        savePending();
        renderComments();
      });
      meta.append(num, tsButton(c), del);
      li.append(meta);
      const img = thumbImg(c);
      if (img) li.append(img);
      const ta = document.createElement("textarea");
      ta.rows = 2;
      ta.value = c.text;
      ta.placeholder = kindOf(c) === "drawing" ? "Note (optional)" : "";
      ta.setAttribute("aria-label", `Comment ${i + 1}`);
      ta.addEventListener("input", () => { c.text = ta.value; savePending(); updateSend(); });
      li.append(ta);
      list.append(li);
    });
    $("pending-count").textContent = state.pending.length ? `(${state.pending.length})` : "";
    const sent = $("sent");
    sent.replaceChildren();
    for (const c of state.sent) {
      const li = document.createElement("li");
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.append(tsButton(c));
      li.append(meta);
      const img = thumbImg(c);
      if (img) li.append(img);
      if (c.text) {
        const p = document.createElement("p");
        p.textContent = c.text;
        li.append(p);
      }
      sent.append(li);
    }
    updateSend();
    renderMarkers();
  }

  function updateSend() {
    $("send").disabled = !state.pending.length || state.pending.some((c) => kindOf(c) === "point" && !c.text.trim());
  }

  function rowOf(c) {
    if (kindOf(c) === "drawing") return L.drawingRow(c);
    const { video: v, version, t, x, y, text } = c;
    return { kind: "point", video: v, version, t, x, y, text: text.trim() };
  }

  function showTime() {
    const seek = $("seek");
    seek.value = String(video.currentTime);
    $("time").textContent = `${fmt(video.currentTime)} / ${fmt(video.duration || 0)}`;
    renderMarkers();
  }

  function initTools() {
    const colours = $("draw-colours");
    for (const [name, value] of L.DRAW_COLOURS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "colour";
      b.dataset.colour = value;
      b.style.setProperty("--c", value);
      b.setAttribute("aria-label", name);
      b.title = name;
      b.addEventListener("click", () => setColour(value));
      colours.append(b);
    }
    setColour(state.colour);
    setTool("point");
    for (const b of document.querySelectorAll("#draw-tools [data-tool]")) b.addEventListener("click", () => setTool(b.dataset.tool));
    $("draw-custom").addEventListener("input", (ev) => setColour(ev.target.value));
    $("draw-undo").addEventListener("click", () => {
      if (!state.draft || state.draft.kind !== "drawing") return;
      state.draft.shapes.pop();
      if (!state.draft.shapes.length) closeComposer(); else { composerLabel(); renderLayer(); }
    });
    $("draw-clear").addEventListener("click", () => { if (state.draft && state.draft.kind === "drawing") closeComposer(); });
    layer.addEventListener("pointerdown", startStroke);
    layer.addEventListener("pointermove", moveStroke);
    layer.addEventListener("pointerup", endStroke);
    layer.addEventListener("pointercancel", endStroke);
  }

  function init() {
    video = $("video"); stage = $("stage"); markers = $("markers"); layer = $("draw-layer");
    composer = $("composer"); composerText = $("composer-text");

    video.addEventListener("click", (ev) => {
      if (state.tool !== "point") return;
      const p = pictureRect();
      const x = (ev.clientX - p.left) / p.width, y = (ev.clientY - p.top) / p.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) return;
      video.pause();
      openComposer({ kind: "point", t: video.currentTime, x, y });
    });
    composer.addEventListener("submit", (ev) => { ev.preventDefault(); submitComposer(); });
    $("composer-cancel").addEventListener("click", closeComposer);
    composerText.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closeComposer();
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) composer.requestSubmit();
    });
    initTools();

    $("send").addEventListener("click", async () => {
      const batch = state.pending.map(rowOf);
      $("send").disabled = true;
      try {
        const ids = await App.send(batch);
        state.pending = [];
        savePending();
        App.setStatus(`Sent ${ids.length} comment${ids.length === 1 ? "" : "s"}. The next version appears here when it is rendered.`);
        refreshSent();
      } catch (e) {
        App.setStatus(`Not sent: ${e.message}`, true);
        updateSend();
      }
    });

    const play = $("play"), seek = $("seek");
    play.addEventListener("click", () => (video.paused ? video.play() : video.pause()));
    video.addEventListener("play", () => { play.textContent = "❚❚"; play.setAttribute("aria-label", "Pause"); closeComposer(); });
    video.addEventListener("pause", () => { play.textContent = "▶"; play.setAttribute("aria-label", "Play"); renderLayer(); });
    video.addEventListener("loadedmetadata", () => { seek.max = String(video.duration); showTime(); });
    video.addEventListener("timeupdate", showTime);
    video.addEventListener("seeked", () => {
      // A drawing belongs to the frame it was drawn on.
      if (state.draft && state.draft.kind === "drawing" && Math.abs(video.currentTime - state.draft.t) > 0.01) closeComposer();
      showTime();
    });
    seek.addEventListener("input", () => { video.currentTime = Number(seek.value); });
    $("version").addEventListener("change", (ev) => selectClip(state.clip, Number(ev.target.value)));
    window.addEventListener("resize", renderMarkers);
    document.addEventListener("keydown", (ev) => {
      if (App.current() !== "review" || !state.clip) return;
      if (ev.target instanceof HTMLTextAreaElement || ev.target instanceof HTMLSelectElement
          || (ev.target instanceof HTMLInputElement && ev.target.type === "color")) return;
      if (ev.key === " ") { ev.preventDefault(); video.paused ? video.play() : video.pause(); }
      if (ev.key === "ArrowLeft") video.currentTime = Math.max(0, video.currentTime - 1);
      if (ev.key === "ArrowRight") video.currentTime = Math.min(video.duration || 0, video.currentTime + 1);
    });
  }

  App.registerTab("review", {
    init,
    isOpen: (s) => s.videos.length > 0,
    update(s, changed) {
      if (changed.videos) updateVideos(s.videos);
      if (changed.rows && state.clip) refreshSent();
    },
    hide() { if (video && !video.paused) video.pause(); },
    show() { renderMarkers(); },
  });
})();
