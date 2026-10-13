// Review tab: play every render, click the picture to comment on that moment and spot.
"use strict";

(() => {
  const { $, L } = App;
  const fmt = L.fmt;
  let video, stage, markers, composer, composerText;
  const state = { clips: [], clip: null, version: null, pending: [], sent: [], draft: null };

  // --- pending comments survive a reload (per viewer, best effort) -------------

  const draftKey = () => `pending:${state.clip}`;
  const savePending = () => App.store.set(draftKey(), state.pending);
  const loadPending = () => { state.pending = App.store.get(draftKey(), []); };

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
    state.sent = L.rowsOfKind(App.state.rows, "point").filter((r) => r.video === state.clip);
    renderComments();
  }

  // --- the composer ---------------------------------------------------------------

  // The picture inside the <video> box (it letterboxes when the box's shape differs).
  function pictureRect() {
    const r = video.getBoundingClientRect();
    const vw = video.videoWidth || r.width, vh = video.videoHeight || r.height;
    const scale = Math.min(r.width / vw, r.height / vh);
    const w = vw * scale, h = vh * scale;
    return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
  }

  function openComposer(x, y) {
    state.draft = { t: video.currentTime, x, y };
    $("composer-label").textContent = `Comment at ${fmt(state.draft.t)}`;
    composer.hidden = false;
    composerText.value = "";
    composerText.focus();
    renderMarkers();
  }

  function closeComposer() {
    state.draft = null;
    composer.hidden = true;
    renderMarkers();
  }

  // --- comment lists --------------------------------------------------------------

  function renderMarkers() {
    markers.replaceChildren();
    const p = pictureRect(), s = stage.getBoundingClientRect();
    const shown = state.pending.filter((c) => c.version === state.version && Math.abs(c.t - video.currentTime) < 0.75);
    if (state.draft) shown.push({ ...state.draft, draft: true });
    shown.forEach((c) => {
      const m = document.createElement("div");
      m.className = "marker";
      m.textContent = c.draft ? "+" : String(state.pending.indexOf(c) + 1);
      m.style.left = `${p.left - s.left + c.x * p.width}px`;
      m.style.top = `${p.top - s.top + c.y * p.height}px`;
      markers.append(m);
    });
  }

  function tsButton(c) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ts";
    b.textContent = `${fmt(c.t)} · v${c.version}`;
    b.addEventListener("click", () => {
      if (c.version !== state.version) selectClip(state.clip, c.version);
      const seek = () => { video.currentTime = c.t; video.pause(); };
      if (video.readyState >= 1) seek(); else video.addEventListener("loadedmetadata", seek, { once: true });
    });
    return b;
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
      const ta = document.createElement("textarea");
      ta.rows = 2;
      ta.value = c.text;
      ta.setAttribute("aria-label", `Comment ${i + 1}`);
      ta.addEventListener("input", () => { c.text = ta.value; savePending(); updateSend(); });
      li.append(meta, ta);
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
      const p = document.createElement("p");
      p.textContent = c.text;
      li.append(meta, p);
      sent.append(li);
    }
    updateSend();
    renderMarkers();
  }

  function updateSend() {
    $("send").disabled = !state.pending.length || state.pending.some((c) => !c.text.trim());
  }

  function showTime() {
    const seek = $("seek");
    seek.value = String(video.currentTime);
    $("time").textContent = `${fmt(video.currentTime)} / ${fmt(video.duration || 0)}`;
    renderMarkers();
  }

  function init() {
    video = $("video"); stage = $("stage"); markers = $("markers");
    composer = $("composer"); composerText = $("composer-text");

    video.addEventListener("click", (ev) => {
      const p = pictureRect();
      const x = (ev.clientX - p.left) / p.width, y = (ev.clientY - p.top) / p.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) return;
      video.pause();
      openComposer(x, y);
    });
    composer.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const text = composerText.value.trim();
      if (!text || !state.draft) return;
      // crypto.randomUUID needs a secure origin, and a plain-http page on a LAN name is not one.
      state.pending.push({ key: `${Date.now()}-${Math.random().toString(36).slice(2)}`, video: state.clip, version: state.version, ...state.draft, text });
      savePending();
      closeComposer();
      renderComments();
    });
    $("composer-cancel").addEventListener("click", closeComposer);
    composerText.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closeComposer();
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) composer.requestSubmit();
    });

    $("send").addEventListener("click", async () => {
      const batch = state.pending.map(({ video: v, version, t, x, y, text }) => ({ kind: "point", video: v, version, t, x, y, text: text.trim() }));
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
    video.addEventListener("pause", () => { play.textContent = "▶"; play.setAttribute("aria-label", "Play"); });
    video.addEventListener("loadedmetadata", () => { seek.max = String(video.duration); showTime(); });
    video.addEventListener("timeupdate", showTime);
    video.addEventListener("seeked", showTime);
    seek.addEventListener("input", () => { video.currentTime = Number(seek.value); });
    $("version").addEventListener("change", (ev) => selectClip(state.clip, Number(ev.target.value)));
    window.addEventListener("resize", renderMarkers);
    document.addEventListener("keydown", (ev) => {
      if (App.current() !== "review" || !state.clip) return;
      if (ev.target instanceof HTMLTextAreaElement || ev.target instanceof HTMLSelectElement) return;
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
