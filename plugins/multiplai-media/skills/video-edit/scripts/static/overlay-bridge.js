// Bridge for overlay pages the job page's Review tab plays live over a render.
//
// stages/review_server.py injects this into every overlay page it serves on
// /media/overlay, right after seek-shim.js and before the page's own scripts.
// The page runs in an <iframe sandbox="allow-scripts"> with an opaque origin,
// so the Review tab cannot reach into it; the two talk over postMessage:
//   - when the page has loaded and its fonts are ready, this calls
//     __veReady() (seek-shim.js) and tells the parent the page's size:
//     {type: "ready", width, height, seek};
//   - {type: "init", transparent} from the parent: when true (a keyed or an
//     alpha overlay), html and body lose their background, as the renderer
//     removes it (stages/overlay.py TRANSPARENT_CSS; the key colour for keyed);
//   - {type: "seek", t} on every time update: __veFrame(t), the renderer's seek;
//   - {type: "pick", on}: while on, a click on the page is not passed to it;
//     this answers {type: "picked", selector, box, styles, text} for the
//     element clicked: a CSS selector for it, its box in page pixels, its
//     computed PROPS and up to MAX_TEXT characters of its text;
//   - {type: "applyStyle", selector, changes: {property: value}}: sets each
//     of PROPS given on every element the selector matches, as an inline
//     !important declaration (so it wins over the page's animations too);
//   - {type: "clearStyle"}: takes back every declaration applyStyle set.
// Messages from anything but the parent window are ignored. Nothing here
// reaches the network or the job page: the frame's CSP and sandbox forbid it.
(function () {
  if (window.__veBridge) return;
  window.__veBridge = true;
  var PROPS = ["color", "background-color", "font-family", "font-size", "font-weight", "opacity", "transform"];
  var MAX_TEXT = 80, MAX_PX = 4096, MAX_SELECTOR = 300;
  var IDENT = /^-?[A-Za-z_][\w-]*$/;
  var TRANSPARENT_CSS = "html,body{background:transparent!important;" +
    "background-color:transparent!important;background-image:none!important}";
  var picking = false, ready = false, pendingT = null, transparentTag = null;
  var applied = [];   // [{el, prop, value, priority}]: what each property was before applyStyle

  function post(msg) { window.parent.postMessage(msg, "*"); }

  function size() {
    var h = document.documentElement.getBoundingClientRect();
    var b = document.body ? document.body.getBoundingClientRect() : { width: 0, height: 0 };
    function px(v) { return Math.max(1, Math.min(MAX_PX, Math.ceil(v) || 1)); }
    return { width: px(Math.max(h.width, b.width)), height: px(Math.max(h.height, b.height)) };
  }

  function seek(t) {
    if (!ready) { pendingT = t; return; }
    if (typeof window.__veFrame === "function") window.__veFrame(Math.max(0, t));
  }

  // A selector for `el` that names it alone: its id when that is unique, else the path from
  // body by tag, classes and position among siblings of the same tag.
  function selectorFor(el) {
    var parts = [];
    while (el && el.nodeType === 1) {
      if (el === document.documentElement) { parts.unshift("html"); break; }
      if (el === document.body) { parts.unshift("body"); break; }
      if (el.id && IDENT.test(el.id) && document.querySelectorAll("#" + el.id).length === 1) {
        parts.unshift("#" + el.id);
        break;
      }
      var part = el.tagName.toLowerCase();
      var cls = (el.getAttribute("class") || "").split(/\s+/).filter(function (c) { return IDENT.test(c); });
      if (cls.length) part += "." + cls.join(".");
      var parent = el.parentElement, same = 0, index = 0;
      if (parent) {
        for (var i = 0; i < parent.children.length; i++) {
          var sib = parent.children[i];
          if (sib.tagName === el.tagName) { same++; if (sib === el) index = same; }
        }
      }
      if (same > 1) part += ":nth-of-type(" + index + ")";
      parts.unshift(part);
      el = parent;
    }
    return parts.join(" > ");
  }

  function describe(el) {
    var cs = getComputedStyle(el), r = el.getBoundingClientRect(), styles = {};
    PROPS.forEach(function (p) { styles[p] = cs.getPropertyValue(p).trim(); });
    var text = (el.textContent || "").replace(/\s+/g, " ").trim();
    return {
      type: "picked", selector: selectorFor(el).slice(0, MAX_SELECTOR + 1),
      box: { x: r.left, y: r.top, w: r.width, h: r.height },
      styles: styles, text: text.length > MAX_TEXT ? text.slice(0, MAX_TEXT - 1) + "…" : text,
    };
  }

  function applyStyle(selector, changes) {
    var els;
    try { els = document.querySelectorAll(selector); } catch (e) { return; }
    PROPS.forEach(function (p) {
      if (typeof changes[p] !== "string") return;
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var known = applied.some(function (a) { return a.el === el && a.prop === p; });
        if (!known) applied.push({ el: el, prop: p, value: el.style.getPropertyValue(p),
                                   priority: el.style.getPropertyPriority(p) });
        el.style.setProperty(p, changes[p], "important");
      }
    });
  }

  function clearStyle() {
    applied.forEach(function (a) {
      if (a.value) a.el.style.setProperty(a.prop, a.value, a.priority);
      else a.el.style.removeProperty(a.prop);
    });
    applied = [];
  }

  function setTransparent(on) {
    if (on && !transparentTag) {
      transparentTag = document.createElement("style");
      transparentTag.textContent = TRANSPARENT_CSS;
      (document.head || document.documentElement).appendChild(transparentTag);
    } else if (!on && transparentTag) {
      transparentTag.remove();
      transparentTag = null;
    }
  }

  window.addEventListener("message", function (ev) {
    if (ev.source !== window.parent) return;
    var d = ev.data;
    if (!d || typeof d !== "object") return;
    if (d.type === "seek" && typeof d.t === "number" && isFinite(d.t)) seek(d.t);
    else if (d.type === "init") setTransparent(d.transparent === true);
    else if (d.type === "pick") {
      picking = d.on === true;
      document.documentElement.style.cursor = picking ? "crosshair" : "";
    } else if (d.type === "applyStyle" && typeof d.selector === "string" && d.changes && typeof d.changes === "object") {
      applyStyle(d.selector, d.changes);
    } else if (d.type === "clearStyle") clearStyle();
  });

  // Capture phase: the page's own handlers never see a pick.
  document.addEventListener("click", function (ev) {
    if (!picking) return;
    ev.preventDefault();
    ev.stopPropagation();
    var el = ev.target && ev.target.nodeType === 1 ? ev.target : ev.target && ev.target.parentElement;
    if (el) post(describe(el));
  }, true);

  function announce() {
    var mode = typeof window.__veReady === "function" ? window.__veReady() : null;
    ready = true;
    if (pendingT !== null) seek(pendingT);
    var s = size();
    post({ type: "ready", width: s.width, height: s.height, seek: mode });
  }

  window.addEventListener("load", function () {
    var fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    fonts.then(announce, announce);
  });
})();
