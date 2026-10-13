// The job page's shell: the access token, the API, polling, tabs and the status line.
// No dependencies; served by stages/review_server.py. Each tab is its own file
// (tab-*.js) and registers itself with App.registerTab before the page starts.
"use strict";

const App = (() => {
  const L = window.JobLogic;
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
  const tabs = new Map();
  // Everything the server tells us; tabs read it and never write it.
  const state = { job: null, candidates: null, looks: null, music: null, videos: [], rows: [] };
  let current = null, chosenByUser = false;

  function setStatus(text, isError) {
    $("status").textContent = text;
    $("status").classList.toggle("error", !!isError);
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

  /* Post mailbox rows; returns their ids. Refreshes the rows every tab sees. */
  async function send(rows) {
    const { ids } = await api("/api/comments", { method: "POST", body: JSON.stringify({ comments: rows }) });
    await refresh();
    return ids;
  }

  /* A media URL a <video> or <audio> element can load (it cannot send headers). */
  function mediaUrl(path, params = {}) {
    const q = new URLSearchParams({ ...params, t: token });
    return `${path}?${q}`;
  }

  /* Per-viewer drafts that survive a reload (best effort: storage may be off). */
  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem(`video-edit-job:${key}`); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(`video-edit-job:${key}`, JSON.stringify(value)); } catch (e) { /* storage off */ }
    },
  };

  /* A tab's "nothing to show yet" line; null hides it. */
  function notice(el, n) {
    el.hidden = !n;
    if (!n) return;
    el.textContent = n.text;
    el.classList.toggle("error", !!n.error);
  }

  function registerTab(id, tab) { tabs.set(id, tab); }

  function isOpen(id) {
    const t = tabs.get(id);
    try { return !!(t && t.isOpen && t.isOpen(state)); } catch (e) { return false; }
  }

  function showTab(id, byUser) {
    if (byUser) chosenByUser = true;
    if (current === id) return;
    const prev = current;
    current = id;
    for (const t of L.TABS) {
      $(`panel-${t}`).hidden = t !== id;
      $(`tab-${t}`).setAttribute("aria-selected", String(t === id));
    }
    if (prev && tabs.get(prev) && tabs.get(prev).hide) tabs.get(prev).hide();
    if (tabs.get(id) && tabs.get(id).show) tabs.get(id).show(state);
  }

  function markOpenTabs() {
    for (const t of L.TABS) $(`tab-${t}`).classList.toggle("open", isOpen(t));
  }

  // One poll at a time. A refresh asked for during a poll (a send's) runs right after
  // it, and its promise settles only then, so send() returns with the rows it posted.
  const refresh = L.oneAtATime(poll);

  async function poll() {
    try {
      const [job, candidates, looks, music, videos, comments] = await Promise.all([
        api("/api/job"), api("/api/candidates"), api("/api/looks"), api("/api/music"),
        api("/api/videos"), api("/api/comments"),
      ]);
      const next = { job, candidates, looks, music, videos: videos.videos, rows: comments.comments };
      const changed = {};
      for (const k of Object.keys(next)) {
        if (JSON.stringify(next[k]) !== JSON.stringify(state[k])) { changed[k] = true; state[k] = next[k]; }
      }
      if ($("status").classList.contains("error")) setStatus("");
      if (Object.keys(changed).length) {
        for (const t of tabs.values()) if (t.update) t.update(state, changed);
        markOpenTabs();
      }
      if (!chosenByUser) showTab(L.firstOpenTab(Object.fromEntries(L.TABS.map((t) => [t, isOpen(t)]))));
    } catch (e) {
      setStatus(`Cannot reach the review server: ${e.message}`, true);
    }
  }

  function start() {
    for (const t of L.TABS) {
      $(`tab-${t}`).addEventListener("click", () => showTab(t, true));
      const tab = tabs.get(t);
      if (tab && tab.init) tab.init($(`panel-${t}`));
    }
    if (!token) {
      setStatus("Open this page from open.html in the review directory: it carries the access token.", true);
      return;
    }
    refresh();
    setInterval(refresh, 5000);
  }

  document.addEventListener("DOMContentLoaded", start);
  return { L, $, token, state, api, send, mediaUrl, store, notice, setStatus, registerTab, showTab,
           current: () => current };
})();
