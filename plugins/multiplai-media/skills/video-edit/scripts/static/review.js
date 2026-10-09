// Video review page. No dependencies; served by stages/review_server.py.
"use strict";

const TOKEN_KEY = "video-edit-review-token";
const token = (() => {
  const fromUrl = new URLSearchParams(location.search).get("t");
  try {
    if (fromUrl) sessionStorage.setItem(TOKEN_KEY, fromUrl);
    return fromUrl || sessionStorage.getItem(TOKEN_KEY) || "";
  } catch (e) {
    return fromUrl || "";
  }
})();
// Keep the token out of the address bar (and out of screenshots of it).
if (location.search) history.replaceState(null, "", location.pathname);

const $ = (id) => document.getElementById(id);
const video = $("video"), stage = $("stage"), markers = $("markers");
const composer = $("composer"), composerText = $("composer-text");
const state = { clips: [], clip: null, version: null, pending: [], sent: [], draft: null };

function setStatus(text, isError) {
  $("status").textContent = text;
  $("status").classList.toggle("error", !!isError);
}

function fmt(t) {
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "X-Review-Token": token, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// --- pending comments survive a reload (per viewer, best effort) -------------

const draftKey = () => `video-edit-review-pending:${state.clip}`;
function savePending() {
  try { localStorage.setItem(draftKey(), JSON.stringify(state.pending)); } catch (e) { /* storage off */ }
}
function loadPending() {
  try { state.pending = JSON.parse(localStorage.getItem(draftKey()) || "[]"); } catch (e) { state.pending = []; }
}

// --- clips and versions ---------------------------------------------------------

function currentVersions() {
  const c = state.clips.find((x) => x.clip === state.clip);
  return c ? c.versions : [];
}

function renderClips() {
  const nav = $("clips");
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
  video.src = `/media/${encodeURIComponent(v.name)}?t=${encodeURIComponent(token)}`;
  closeComposer();
  loadPending();
  renderClips();
  renderComments();
  refreshSent();
}

async function refreshVideos() {
  try {
    const { videos } = await api("/api/videos");
    const before = JSON.stringify(state.clips);
    state.clips = videos;
    if (!state.clip && videos.length) return selectClip(videos[0].clip);
    if (before !== JSON.stringify(videos)) {
      renderClips();
      const latest = currentVersions().slice(-1)[0];
      if (latest && latest.version > state.version) setStatus(`${state.clip} version ${latest.version} is ready — pick it in the version menu.`);
    }
    if (!videos.length) setStatus("No videos in this directory yet.");
  } catch (e) {
    setStatus(`Cannot reach the review server: ${e.message}`, true);
  }
}

async function refreshSent() {
  try {
    const { comments } = await api(`/api/comments?video=${encodeURIComponent(state.clip)}`);
    state.sent = comments;
    renderComments();
  } catch (e) { /* shown by the next refreshVideos */ }
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
  state.pending.push({ key: crypto.randomUUID(), video: state.clip, version: state.version, ...state.draft, text });
  savePending();
  closeComposer();
  renderComments();
});
$("composer-cancel").addEventListener("click", closeComposer);
composerText.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeComposer();
  if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) composer.requestSubmit();
});

// --- comment lists --------------------------------------------------------------

function renderMarkers() {
  markers.replaceChildren();
  const p = pictureRect(), s = stage.getBoundingClientRect();
  const shown = state.pending.filter((c) => c.version === state.version && Math.abs(c.t - video.currentTime) < 0.75);
  if (state.draft) shown.push({ ...state.draft, draft: true });
  shown.forEach((c, i) => {
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

$("send").addEventListener("click", async () => {
  const batch = state.pending.map(({ video: v, version, t, x, y, text }) => ({ video: v, version, t, x, y, text: text.trim() }));
  $("send").disabled = true;
  try {
    const { ids } = await api("/api/comments", { method: "POST", body: JSON.stringify({ comments: batch }) });
    state.pending = [];
    savePending();
    setStatus(`Sent ${ids.length} comment${ids.length === 1 ? "" : "s"}. The next version appears here when it is rendered.`);
    renderComments();
    refreshSent();
  } catch (e) {
    setStatus(`Not sent: ${e.message}`, true);
    updateSend();
  }
});

// --- transport --------------------------------------------------------------------

const play = $("play"), seek = $("seek");
play.addEventListener("click", () => (video.paused ? video.play() : video.pause()));
video.addEventListener("play", () => { play.textContent = "❚❚"; play.setAttribute("aria-label", "Pause"); closeComposer(); });
video.addEventListener("pause", () => { play.textContent = "▶"; play.setAttribute("aria-label", "Play"); });
video.addEventListener("loadedmetadata", () => { seek.max = String(video.duration); renderMarkers(); });
video.addEventListener("timeupdate", () => {
  seek.value = String(video.currentTime);
  $("time").textContent = `${fmt(video.currentTime)} / ${fmt(video.duration || 0)}`;
  renderMarkers();
});
seek.addEventListener("input", () => { video.currentTime = Number(seek.value); });
$("version").addEventListener("change", (ev) => selectClip(state.clip, Number(ev.target.value)));
window.addEventListener("resize", renderMarkers);
document.addEventListener("keydown", (ev) => {
  if (ev.target instanceof HTMLTextAreaElement || ev.target instanceof HTMLSelectElement) return;
  if (ev.key === " ") { ev.preventDefault(); video.paused ? video.play() : video.pause(); }
  if (ev.key === "ArrowLeft") video.currentTime = Math.max(0, video.currentTime - 1);
  if (ev.key === "ArrowRight") video.currentTime = Math.min(video.duration || 0, video.currentTime + 1);
});

if (!token) setStatus("Open this page from open.html in the review directory: it carries the access token.", true);
refreshVideos();
setInterval(refreshVideos, 5000);
