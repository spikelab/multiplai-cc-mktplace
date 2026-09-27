/* Runs in <head>, before the first paint, so a saved theme never flashes
 * the default colours. Fills the theme picker in the header once it exists.
 * The choice is kept per browser; the page works the same without it. */
(function () {
  const KEY = "review-viewer.theme";
  const THEMES = [
    ["", "Default"], ["phosphor", "Phosphor"], ["amber", "Amber"],
    ["turbo", "Turbo"], ["onebit", "1-bit"], ["gameboy", "Game Boy"],
  ];
  const root = document.documentElement;
  let current = "";
  try { current = localStorage.getItem(KEY) || ""; } catch (err) { /* private window */ }
  if (!THEMES.some((t) => t[0] === current)) current = "";

  function apply(name) {
    if (name) root.setAttribute("data-theme", name);
    else root.removeAttribute("data-theme");
  }
  apply(current);

  document.addEventListener("DOMContentLoaded", () => {
    const sel = document.getElementById("theme-select");
    if (!sel) return;
    for (const [value, label] of THEMES) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      sel.appendChild(opt);
    }
    sel.value = current;
    sel.addEventListener("change", () => {
      apply(sel.value);
      try { localStorage.setItem(KEY, sel.value); } catch (err) { /* private window */ }
    });
  });
})();
