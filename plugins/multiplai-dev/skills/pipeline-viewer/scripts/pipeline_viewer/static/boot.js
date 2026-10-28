/* Runs first, before any other script or stylesheet request.
 *
 * 1. The personal link carries the person's token in `?t=`. Take it out of
 *    the address bar at once, so it is not left in history or sent as a
 *    Referer, and keep it for this origin, so later links without `?t=`
 *    (the ones in Slack) work in this browser. If storage is blocked the
 *    token lives only in this variable, and the personal link must be
 *    opened again next time.
 * 2. Apply the saved light/dark choice before the first paint. */
const PV_TOKEN = (function () {
  const KEY = "pipeline-viewer.token";
  const url = new URL(window.location.href);
  let token = url.searchParams.get("t") || "";
  if (url.searchParams.has("t")) {
    url.searchParams.delete("t");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    try { if (token) localStorage.setItem(KEY, token); } catch (err) { /* storage blocked */ }
  }
  if (!token) {
    try { token = localStorage.getItem(KEY) || ""; } catch (err) { /* storage blocked */ }
  }
  return token;
})();

/* "auto" follows the system; "light" and "dark" pin it. tokens.css reads
 * data-theme on <html>. */
const PV_THEME = (function () {
  const KEY = "pipeline-viewer.theme";
  const NEXT = { auto: "light", light: "dark", dark: "auto" };
  const LABEL = { auto: "Theme: auto", light: "Theme: light", dark: "Theme: dark" };
  const root = document.documentElement;
  const system = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function read() {
    try {
      const v = localStorage.getItem(KEY);
      return NEXT[v] ? v : "auto";
    } catch (err) { return "auto"; }
  }

  function isDark(mode) {
    return mode === "dark" || (mode === "auto" && !!(system && system.matches));
  }

  /* highlight.js ships one stylesheet per scheme; switch the pair to match. */
  function syncCode(mode) {
    const dark = isDark(mode);
    document.querySelectorAll("link[data-hl]").forEach((l) => {
      l.media = (l.dataset.hl === "dark") === dark ? "all" : "not all";
    });
  }

  function apply(mode) {
    if (mode === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
    syncCode(mode);
  }

  let mode = read();
  apply(mode);
  if (system && system.addEventListener) system.addEventListener("change", () => syncCode(mode));
  document.addEventListener("DOMContentLoaded", () => syncCode(mode));

  return {
    get mode() { return mode; },
    label() { return LABEL[mode]; },
    next() {
      mode = NEXT[mode];
      try { localStorage.setItem(KEY, mode); } catch (err) { /* storage blocked */ }
      apply(mode);
      return mode;
    },
  };
})();
