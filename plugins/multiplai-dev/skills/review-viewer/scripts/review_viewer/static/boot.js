/* Runs first, before any other script or stylesheet request.
 *
 * open.html redirects here with the access token in `?t=`. Take it out of the
 * address bar at once, so it is never bookmarked, synced or shown, and keep it
 * only in this variable, which app.js reads. */
const REVIEW_TOKEN = (function () {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("t") || "";
  if (url.searchParams.has("t")) {
    url.searchParams.delete("t");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  return token;
})();
