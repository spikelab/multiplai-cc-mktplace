// Review tab: play every render, click the picture to comment on that moment and spot,
// draw on the paused frame (pen, arrow, box) and send the marked-up frame, or mark a
// stretch on the render's timeline (from its .edl.json sidecar) and comment on it.
// review-style.js adds the style panel and live overlays through App.review (below).
"use strict";

(() => {
  const { $, L } = App;
  const fmt = L.fmt;
  let video, stage, markers, composer, composerText, layer;
  const state = {
    clips: [], clip: null, version: null, pending: [], sent: [], draft: null,
    tool: "point", colour: L.DRAW_COLOURS[0][1], stroke: null,
    timeline: null, timelineKey: null, drag: null, sidecar: null,
  };

  // review-style.js listens for "render" (the selected render or its sidecar changed),
  // "time" (the picture moved), "resize" and "rows" (the mailbox rows changed).
  const listeners = { render: [], time: [], resize: [], rows: [] };
  const emit = (event) => { for (const fn of listeners[event]) fn(); };

  // --- pending comments survive a reload (per viewer, best effort) -------------

  const draftKey = (clip = state.clip) => `pending:${clip}`;
  const savePending = () => App.store.set(draftKey(), state.pending);
  const loadPending = () => { state.pending = App.store.get(draftKey(), []); };
  const kindOf = (c) => c.kind || "point";

  /* Change `clip`'s pending list with `fn`, shown or not: an upload or a send can
   * outlast a switch to another clip, and its comments still belong to the first. */
  function editPending(clip, fn) {
    if (clip === state.clip) {
      state.pending = fn(state.pending);
      savePending();
      renderComments();
    } else {
      App.store.set(draftKey(clip), fn(App.store.get(draftKey(clip), [])));
    }
  }

  // A drawing's small preview, kept per viewer by image id so sent drawings show it too.
  // Only the newest MAX_THUMBS are kept (about 10 KB each), so they never fill the storage
  // the pending comments are saved in.
  const MAX_THUMBS = 40;
  const thumbOf = (image) => App.store.get(`thumb:${image}`, null);
  function keepThumb(image, thumb) {
    const ids = App.store.get("thumbs", []).filter((x) => x !== image);
    ids.push(image);
    for (const old of ids.splice(0, Math.max(0, ids.length - MAX_THUMBS))) App.store.remove(`thumb:${old}`);
    App.store.set("thumbs", ids);
    App.store.set(`thumb:${image}`, thumb);
  }

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
    loadTimeline();
  }

  // --- the timeline ----------------------------------------------------------------

  let retryTimer = null, retries = 0;
  const TIMELINE_RETRIES = 3;

  /* The selected render's sidecar, drawn as lanes; a render without one says why, and is
   * asked again every 5 s, three times (render writes the sidecar just after the mp4
   * appears; a render from before sidecars never gets one). */
  async function loadTimeline(retry = false) {
    clearTimeout(retryTimer);
    if (!retry) retries = 0;
    const key = `${state.clip}@${state.version}`;
    if (state.timelineKey !== key) { state.timeline = null; state.sidecar = null; renderTimeline(); emit("render"); }   // never another render's lanes
    state.timelineKey = key;
    try {
      const q = new URLSearchParams({ video: state.clip, version: String(state.version) });
      const res = await App.api(`/api/edl?${q}`);
      if (state.timelineKey !== key) return;
      const changed = JSON.stringify(res) !== JSON.stringify(state.sidecar && state.sidecar.res);
      state.timeline = L.timelineLanes(res.data);
      state.sidecar = { res, data: res.data, overlayKeys: res.overlay_keys || {} };
      App.notice($("timeline-notice"), null);
      if (changed) emit("render");
    } catch (e) {
      if (state.timelineKey !== key) return;
      state.timeline = null;
      if (state.sidecar) { state.sidecar = null; emit("render"); }
      App.notice($("timeline-notice"), { text: `No timeline for this render: ${e.message}` });
      if (retries++ < TIMELINE_RETRIES) retryTimer = setTimeout(() => { if (state.timelineKey === key) loadTimeline(true); }, 5000);
    }
    renderTimeline();
  }

  const tlDuration = () => (state.timeline ? state.timeline.duration : 0);

  function fracIn(el, clientX) {
    const r = el.getBoundingClientRect();
    return r.width > 0 ? (clientX - r.left) / r.width : 0;
  }

  function seekTo(version, t) {
    if (version !== state.version) selectClip(state.clip, version);
    const seek = () => { video.currentTime = t; video.pause(); };
    if (video.readyState >= 1) seek(); else video.addEventListener("loadedmetadata", seek, { once: true });
  }

  function trackChildren(track) {
    const sel = document.createElement("div");
    sel.className = "tl-sel";
    sel.hidden = true;
    const ph = document.createElement("div");
    ph.className = "tl-ph";
    track.append(sel, ph);
  }

  function blockEl(lane, b) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `tl-block lane-${lane.id}${b.tick ? " tick" : ""}`;
    el.dataset.target = b.target;
    const box = L.blockBox(b, tlDuration());
    el.style.left = `${box.left}%`;
    if (!b.tick) el.style.width = `${box.width}%`;
    el.title = b.title;
    el.setAttribute("aria-label", `${lane.label}: ${b.title}`);
    if (b.xfade > 0 && b.end > b.start) {
      const xf = document.createElement("span");
      xf.className = "xf";
      xf.style.width = `${Math.min(100, (b.xfade / (b.end - b.start)) * 100)}%`;
      el.append(xf);
    }
    if (!b.tick) {
      const t = document.createElement("span");
      t.className = "tl-text";
      t.textContent = b.label;
      if (el.firstChild) t.style.marginLeft = el.firstChild.style.width;   // the label starts past the crossfade
      el.append(t);
    }
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      pickBlock(b);
    });
    return el;
  }

  function renderTimeline() {
    const tl = state.timeline;
    $("tl-body").hidden = !tl;
    const lanes = $("tl-lanes");
    lanes.replaceChildren();
    const ruler = $("tl-ruler");
    ruler.replaceChildren();
    if (!tl) return;
    const ticks = Math.max(1, Math.floor(tl.duration));
    const every = ticks > 60 ? 10 : ticks > 20 ? 5 : 1;
    for (let t = 0; t <= tl.duration; t += every) {
      const m = document.createElement("span");
      m.className = "tl-tick";
      m.style.left = `${(t / tl.duration) * 100}%`;
      m.textContent = `${t}s`;
      ruler.append(m);
    }
    trackChildren(ruler);
    for (const lane of tl.lanes) {
      const row = document.createElement("div");
      row.className = "tl-row";
      const label = document.createElement("span");
      label.className = "tl-label";
      label.textContent = lane.label;
      const track = document.createElement("div");
      track.className = `tl-track lane-${lane.id}`;
      for (const b of lane.blocks) track.append(blockEl(lane, b));
      if (!lane.blocks.length) {
        const none = document.createElement("span");
        none.className = "tl-none";
        none.textContent = "none";
        track.append(none);
      }
      trackChildren(track);
      track.addEventListener("click", (ev) => seekTo(state.version, L.timeAtFrac(fracIn(track, ev.clientX), tlDuration())));
      row.append(label, track);
      lanes.append(row);
    }
    renderTimelineState();
  }

  /* The playhead, the range being marked and the block it names. */
  function renderTimelineState() {
    if (!state.timeline) return;
    const d = tlDuration();
    const ph = `${Math.min(100, Math.max(0, (video.currentTime / d) * 100))}%`;
    for (const el of document.querySelectorAll("#timeline .tl-ph")) el.style.left = ph;
    const r = state.drag ? L.rangeFromDrag(state.drag.t0, state.drag.t1, d, 0)
      : state.draft && state.draft.kind === "range" ? state.draft : null;
    for (const el of document.querySelectorAll("#timeline .tl-sel")) {
      el.hidden = !r;
      if (r) {
        const box = L.blockBox({ start: r.t_start, end: r.t_end }, d);
        el.style.left = `${box.left}%`;
        el.style.width = `${box.width}%`;
      }
    }
    const target = state.draft && state.draft.kind === "range" ? state.draft.target : null;
    for (const el of document.querySelectorAll("#timeline .tl-block")) el.setAttribute("aria-pressed", String(el.dataset.target === target && !!target));
  }

  /* A block click names it as the range's target; with no range marked, the block is the range. */
  function pickBlock(b) {
    video.pause();
    if (state.draft && state.draft.kind === "range") {
      state.draft.target = b.target;
      composerLabel();
      renderTimelineState();
      return;
    }
    const r = L.blockRange(b, tlDuration());
    video.currentTime = r.t_start;
    openComposer({ kind: "range", ...r, target: b.target });
  }

  function initTimeline() {
    const ruler = $("tl-ruler");
    ruler.addEventListener("pointerdown", (ev) => {
      if (!state.timeline || ev.button !== 0) return;
      ev.preventDefault();
      const t = L.timeAtFrac(fracIn(ruler, ev.clientX), tlDuration());
      state.drag = { t0: t, t1: t };
      ruler.setPointerCapture(ev.pointerId);
      renderTimelineState();
    });
    ruler.addEventListener("pointermove", (ev) => {
      if (!state.drag) return;
      state.drag.t1 = L.timeAtFrac(fracIn(ruler, ev.clientX), tlDuration());
      renderTimelineState();
    });
    const end = () => {
      if (!state.drag) return;
      const { t0, t1 } = state.drag;
      state.drag = null;
      const r = L.rangeFromDrag(t0, t1, tlDuration());
      if (!r) { seekTo(state.version, t0); renderTimelineState(); return; }
      video.pause();
      video.currentTime = r.t_start;
      if (state.draft && state.draft.kind === "range") {   // a new stretch for the range being written
        Object.assign(state.draft, r);
        composerLabel();
        renderTimelineState();
      } else {
        openComposer({ kind: "range", ...r, target: null });
      }
    };
    ruler.addEventListener("pointerup", end);
    ruler.addEventListener("pointercancel", end);
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
    state.sent = L.reviewSent(App.state.rows, state.clip);
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
    const room = state.draft && state.draft.kind === "drawing" ? L.drawingRoom(state.draft.shapes) : null;
    const shape = room ? L.finishShape(s.tool, s.color, s.tool === "pen" ? s.points.slice(1) : s.points, room) : null;
    if (!room) App.setStatus("This drawing is full: add it, then draw the rest as another.", true);
    if (shape && state.draft && state.draft.kind === "drawing") state.draft.shapes.push(shape);
    if (state.draft && state.draft.kind === "drawing" && !state.draft.shapes.length) closeComposer();
    else composerLabel();
    renderLayer();
    // The composer sits under the timeline, below the fold beside a tall 9:16 picture. Bring it
    // into view once, after the first shape (never mid-stroke, and without taking the focus).
    if (shape && state.draft && state.draft.kind === "drawing" && state.draft.shapes.length === 1) {
      composer.scrollIntoView({ block: "nearest" });
    }
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
      : d.kind === "range"
        ? `${fmt(d.t_start)}–${fmt(d.t_end)}${d.target ? ` on ${L.targetLabel(d.target)}` : ""} (click a block to name it)`
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
    renderTimelineState();
  }

  function closeComposer() {
    state.draft = null;
    state.stroke = null;
    composer.hidden = true;
    renderMarkers();
    renderTimelineState();
  }

  const newKey = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  async function submitComposer() {
    const d = state.draft;
    if (!d || $("composer-submit").disabled) return;   // Ctrl/Cmd+Enter while a drawing uploads
    const text = composerText.value.trim();
    if (d.kind === "point") {
      if (!text) return;
      // crypto.randomUUID needs a secure origin, and a plain-http page on a LAN name is not one.
      state.pending.push({ key: newKey(), kind: "point", video: state.clip, version: state.version, t: d.t, x: d.x, y: d.y, text });
    } else if (d.kind === "range") {
      if (!text) return;
      state.pending.push({ key: newKey(), kind: "range", video: state.clip, version: state.version, t: d.t_start,
                           t_start: d.t_start, t_end: d.t_end, target: d.target, text });
    } else if (d.kind === "drawing") {
      if (!d.shapes.length) return;
      // The render it was drawn on, read before the upload: the viewer may switch clips meanwhile.
      const item = { key: newKey(), kind: "drawing", video: state.clip, version: state.version, t: d.t, shapes: d.shapes, text };
      $("composer-submit").disabled = true;
      try {
        const { blob, thumb } = await exportDrawing(d.shapes);
        const { image } = await App.postBlob("/api/marks", blob, "image/jpeg");
        keepThumb(image, thumb);
        editPending(item.video, (list) => list.concat({ ...item, image }));
      } catch (e) {
        App.setStatus(`Drawing not added: ${e.message}`, true);
        return;
      } finally {
        $("composer-submit").disabled = false;
      }
      if (state.draft === d) closeComposer();
      return;
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

  function tsButton(c) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ts";
    const range = kindOf(c) === "range";
    b.textContent = range
      ? `${fmt(c.t_start)}–${fmt(c.t_end)} · v${c.version}${c.target ? ` · ${L.targetLabel(c.target)}` : ""}`
      : `${kindOf(c) === "drawing" ? "✎ " : ""}${fmt(c.t)} · v${c.version}`;
    b.addEventListener("click", () => seekTo(c.version, range ? c.t_start : c.t));
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
    const at = (c) => (kindOf(c) === "range" ? c.t_start : c.t);
    state.pending.sort((a, b) => a.version - b.version || at(a) - at(b));
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
    $("send").disabled = !L.canSendReview(state.pending);
  }

  function showTime() {
    const seek = $("seek");
    seek.value = String(video.currentTime);
    $("time").textContent = `${fmt(video.currentTime)} / ${fmt(video.duration || 0)}`;
    renderMarkers();
    renderTimelineState();
    emit("time");
  }

  // The playhead follows every frame while playing (timeupdate fires only ~4 times a second).
  function followPlayhead() {
    renderTimelineState();
    emit("time");
    if (!video.paused) requestAnimationFrame(followPlayhead);
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
    initTimeline();

    // Sent in bodies the server accepts (a few big drawings can outgrow one); each body's
    // comments leave the pending list as soon as it is sent, so a failure resends only the rest.
    $("send").addEventListener("click", async () => {
      const clip = state.clip, items = state.pending.slice(), rows = items.map(L.reviewRow);
      $("send").disabled = true;
      let sent = 0;
      try {
        for (const batch of L.sendBatches(rows)) {
          await App.send(batch.map((i) => rows[i]));
          const done = new Set(batch.map((i) => items[i].key));
          editPending(clip, (list) => list.filter((c) => !done.has(c.key)));
          sent += batch.length;
        }
        App.setStatus(`Sent ${sent} comment${sent === 1 ? "" : "s"}. The next version appears here when it is rendered.`);
      } catch (e) {
        App.setStatus(`${sent ? `Sent ${sent}; the other ${items.length - sent} not sent` : "Not sent"}: ${e.message}`, true);
      }
      refreshSent();
      updateSend();
    });

    const play = $("play"), seek = $("seek");
    play.addEventListener("click", () => (video.paused ? video.play() : video.pause()));
    video.addEventListener("play", () => {
      play.textContent = "❚❚"; play.setAttribute("aria-label", "Pause"); closeComposer(); requestAnimationFrame(followPlayhead);
    });
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
    window.addEventListener("resize", () => { renderMarkers(); emit("resize"); });
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
      if (changed.rows && state.clip) { refreshSent(); emit("rows"); }
    },
    hide() { if (video && !video.paused) video.pause(); },
    show() { renderMarkers(); emit("resize"); },
  });

  // What review-style.js builds on: the selected render, its sidecar, the picture and events.
  App.review = {
    on(event, fn) { listeners[event].push(fn); },
    video: () => video,
    stage: () => stage,
    pictureRect,
    setTool,
    render: () => (state.clip ? { clip: state.clip, version: state.version } : null),
    sidecar: () => state.sidecar,
  };
})();
